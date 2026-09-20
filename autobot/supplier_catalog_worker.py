"""Explicit background catalogue worker. No network or threads on import."""
import http.client
import ipaddress
import logging
import os
import socket
import threading
import time
from urllib.parse import urljoin,urlparse
from urllib.robotparser import RobotFileParser

from bs4 import UnicodeDammit
from autobot import supplier_catalog_store as store, supplier_catalog_jobs as jobs
from autobot.supplier_catalog_extract import extract,navigate
from autobot.supplier_evidence import supplier_identity
from autobot.market_source_adapters import source_region_evidence

_log=logging.getLogger(__name__)
_thread=None
_lock=threading.Lock()
_stop=threading.Event()
USER_AGENT='PMbiCatalog/1.0'
MAX_BYTES=3_000_000


def public_address(url,allowed_host):
    parsed=urlparse(url)
    if (parsed.scheme not in {'https','http'} or parsed.hostname!=allowed_host or parsed.username
            or parsed.password or parsed.port not in {None,80,443}):
        raise ValueError('Ссылка находится за пределами выбранного сайта')
    addresses=socket.getaddrinfo(parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80),type=socket.SOCK_STREAM)
    candidates=list(dict.fromkeys(item[4][0] for item in addresses))
    if not candidates or any(not ipaddress.ip_address(ip).is_global for ip in candidates):
        raise ValueError('Источник должен находиться на публичном адресе')
    return parsed,candidates[0]


def fetch_page(url,allowed_host):
    """Pin the checked IP while preserving TLS hostname verification."""
    started=time.monotonic()
    for _ in range(4):
        parsed,address=public_address(url,allowed_host)
        klass=http.client.HTTPSConnection if parsed.scheme=='https' else http.client.HTTPConnection
        connection=klass(parsed.hostname,port=parsed.port,timeout=7)
        connection._create_connection=lambda target,timeout,source_address=None: socket.create_connection((address,target[1]),timeout,source_address)
        try:
            connection.request('GET',parsed.path+('?' + parsed.query if parsed.query else ''),headers={'User-Agent':USER_AGENT,'Accept-Encoding':'identity','Accept':'text/html,application/xhtml+xml'})
            response=connection.getresponse()
            if response.status in {301,302,303,307,308}:
                url=urljoin(url,response.getheader('Location',''))
                continue
            if response.status>=400:
                raise ValueError(f'Сайт вернул HTTP {response.status}')
            length=response.getheader('Content-Length')
            if length and int(length)>MAX_BYTES:
                raise ValueError('Страница превышает ограничение размера')
            blocks=[]; size=0
            while True:
                chunk=response.read(65536)
                if not chunk: break
                size+=len(chunk)
                if size>MAX_BYTES or time.monotonic()-started>25:
                    raise ValueError('Превышен лимит получения страницы')
                blocks.append(chunk)
            raw=b''.join(blocks)
            if response.getheader('Content-Encoding','identity') not in {'','identity'}:
                raise ValueError('Сайт прислал неподдерживаемое сжатие')
            content_type=response.getheader('Content-Type','text/html')
            if not any(t in content_type for t in ('text/','html','xml','application/json')):
                raise ValueError('Для этого формата документа нужен отдельный импорт')
            body=UnicodeDammit(raw,is_html=True).unicode_markup
            if not body: raise ValueError('Сайт вернул пустую страницу')
            return body,time.time(),url
        finally:
            connection.close()
    raise ValueError('Слишком много перенаправлений сайта')


class SiteReader:
    def __init__(self,source):
        self.host=urlparse(source['url']).hostname
        self.origin=urlparse(source['url'])._replace(path='',query='',fragment='').geturl()
        self.robot=None
        self.last_request=0

    def __call__(self,url):
        if self.robot is None:
            robot=RobotFileParser()
            try:
                body,_,_=fetch_page(self.origin+'/robots.txt',self.host)
                robot.parse(body.splitlines())
            except ValueError as error:
                if 'HTTP 404' not in str(error): raise
                robot.parse([])
            self.robot=robot
        if not self.robot.can_fetch(USER_AGENT,url):
            raise ValueError('Правила сайта запрещают автоматическое чтение этой страницы')
        pause=max(0.6,self.robot.crawl_delay(USER_AGENT) or self.robot.crawl_delay('*') or 0)
        if pause>30:
            raise ValueError('Правила сайта требуют более редкого обхода; нужен отдельный режим импорта')
        remaining=pause-(time.monotonic()-self.last_request)
        if remaining>0: _stop.wait(remaining)
        result=fetch_page(url,self.host)
        self.last_request=time.monotonic()
        return result


def run_once(*,path=None,reader=None):
    job=jobs.claim(path=path)
    if not job: return None
    source=store.source(job['source_id'],path)
    read=reader or SiteReader(source)
    error=''
    try:
        for _ in range(job['page_budget']):
            if _stop.is_set() or not jobs.owns(job['id'],job['owner'],path=path): break
            page=jobs.next_page(job['id'],job['owner'],path=path)
            if not page: break
            try:
                body,observed_at,final_url=read(page['url'])
                records=extract(body,final_url,page['kind'],page['label'],source['config'])
                links=navigate(body,final_url,source['config']) if page['kind']=='catalog' else []
                store.save_page(source['id'],final_url,body,observed_at,records,kind=page['kind'],
                                job_id=job['id'],owner=job['owner'],path=path)
                identity=supplier_identity(body)
                if identity and page['kind'] in {'catalog','context'}:
                    store.save_contact(source['id'],identity,final_url,observed_at,path)
                if page['kind'] in {'catalog','context','product'}:
                    proof=source_region_evidence(body,'Ярославская область',source['bucket'])
                    store.save_coverage(source['id'],proof,final_url,observed_at,path=path)
                jobs.complete_page(job['id'],job['owner'],page,links,path=path)
            except Exception as exc:
                error=store.clean(exc)[:700]
                jobs.complete_page(job['id'],job['owner'],page,[],error=error,path=path)
                _log.warning('Catalogue page %s: %s',page['url'],error)
                if any(word in error for word in ('HTTP 429','HTTP 403','запрещают','публичном адресе')): break
    finally:
        jobs.finish(job['id'],job['owner'],error=error,path=path)
    return next((r for r in jobs.list_jobs(path=path) if r['id']==job['id']),None)


def _run():
    while not _stop.is_set():
        try:
            if run_once() is not None: continue
        except Exception:
            _log.exception('Supplier catalogue worker failed')
        _stop.wait(5)


def start_worker():
    global _thread
    if os.environ.get('SUPPLIER_CATALOG_WORKER','1').lower() in {'0','off','false'}: return None
    with _lock:
        if _thread is None or not _thread.is_alive():
            store.initialize(); store.seed_sources()
            _stop.clear()
            _thread=threading.Thread(target=_run,name='supplier-catalog-worker',daemon=True)
            _thread.start()
    return _thread


if __name__=='__main__':
    import argparse,json
    parser=argparse.ArgumentParser()
    parser.add_argument('--source'); parser.add_argument('--db'); parser.add_argument('--page-budget',type=int,default=160)
    args=parser.parse_args()
    store.initialize(args.db); store.seed_sources(args.db)
    if args.source: jobs.enqueue(args.source,page_budget=args.page_budget,path=args.db)
    print(json.dumps(run_once(path=args.db),ensure_ascii=False))
