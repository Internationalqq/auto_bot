"""Bounded discovery and public-source verification; never sends messages."""
from __future__ import annotations

import base64
from decimal import Decimal, ROUND_HALF_UP
import http.client
import ipaddress
import logging
import os
import re
import socket
import ssl
import threading
import time
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote, quote
from bs4 import BeautifulSoup
from autobot.hermes_buyer import BuyerError
from autobot.buyer_needs import digest

log = logging.getLogger(__name__)


def public_url(value):
    try:
        if not isinstance(value,str) or len(value)>4096: raise ValueError()
        p = urlsplit(value)
        if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
            raise ValueError()
        host = p.hostname.encode('idna').decode('ascii').lower()
        if any(ord(c)<32 for c in value) or '\\' in value: raise ValueError()
        authority = ('['+host+']' if ':' in host else host) + (f':{p.port}' if p.port else '')
        return urlunsplit((p.scheme, authority, quote(p.path or '/',safe="/%:@!$&'()*+,;=-._~"),
                           quote(p.query,safe="=&%:@!$'()*+,;/?-._~"), ''))
    except (ValueError, UnicodeError):
        raise BuyerError('Недопустимый адрес источника') from None


def fetch_html(url):
    """Resolve and pin a public IP, retaining TLS SNI/Host. No ambient proxy/auth."""
    deadline = time.monotonic()+35
    for _ in range(4):
        url = public_url(url)
        p = urlsplit(url); port = p.port or (443 if p.scheme == 'https' else 80)
        addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(p.hostname, port, type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise BuyerError('Источник ведёт в локальную или служебную сеть')
        connection = http.client.HTTPConnection(p.hostname, port, timeout=6)
        try:
            connection.sock = socket.create_connection((addresses[0], port), timeout=6)
            if p.scheme == 'https':
                connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=p.hostname)
            connection.request('GET', urlunsplit(('', '', p.path or '/', p.query, '')),
                               headers={'Host': p.netloc, 'User-Agent': 'AutoBot/1.0 supplier verification', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location: raise BuyerError('Источник вернул пустое перенаправление')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise BuyerError(f'Источник недоступен: HTTP {response.status}')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise BuyerError('Источник вернул неподдерживаемое сжатие')
            content_type = response.getheader('Content-Type', '').lower()
            if not any(kind in content_type for kind in ('text/html', 'application/xhtml+xml')):
                raise BuyerError('Источник не является HTML-страницей')
            chunks, size = [], 0
            while True:
                chunk = response.read(32768)
                if not chunk: break
                size += len(chunk)
                if size > 2_000_000 or time.monotonic() > deadline:
                    raise BuyerError('Превышен размер или время загрузки источника')
                chunks.append(chunk)
            body = b''.join(chunks)
            charset = re.search(r'charset=([\w-]+)', content_type)
            encoding = charset[1] if charset else 'utf-8'
            try: html = body.decode(encoding, errors='replace')
            except LookupError: html = body.decode('utf-8', errors='replace')
            return url, html
        finally:
            connection.close()
    raise BuyerError('Слишком много перенаправлений источника')


def search_api(query):
    """Optional official Yandex API. Credentials never enter errors/results."""
    key = os.environ.get('BUYER_SEARCH_API_KEY', '').strip()
    folder = os.environ.get('BUYER_SEARCH_FOLDER_ID', '').strip()
    if not key and not folder: return None
    if not key or not folder: raise BuyerError('Для Search API нужны ключ и folder ID в настройках сервера')
    import requests
    payload = {'query':{'searchType':'SEARCH_TYPE_RU','queryText':query[:400],'page':'0'},
               'groupSpec':{'groupMode':'GROUP_MODE_DEEP','groupsOnPage':'20','docsInGroup':'1'},
               'folderId':folder,'responseFormat':'FORMAT_XML'}
    try:
        with requests.post('https://searchapi.api.cloud.yandex.net/v2/web/search', json=payload,
                           headers={'Authorization':'Api-Key '+key}, timeout=(5,20), stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                raise BuyerError(f'Search API временно недоступен: HTTP {response.status_code}')
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 4_000_000: raise BuyerError('Слишком большой ответ Search API')
                chunks.append(chunk)
        import json
        xml = base64.b64decode(json.loads(b''.join(chunks))['rawData'], validate=True)
        if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper(): raise ValueError()
        root = ET.fromstring(xml)
        if root.find('.//error') is not None: raise BuyerError('Search API вернул ошибку поиска; повторите позже')
        return [SimpleNamespace(url=node.findtext('url') or '', title=''.join(node.find('title').itertext()) if node.find('title') is not None else '') for node in root.findall('.//doc')]
    except requests.RequestException:
        raise BuyerError('Search API временно недоступен; запрос сохранён') from None
    except (ValueError, KeyError, TypeError, ET.ParseError):
        raise BuyerError('Search API вернул некорректный ответ') from None


def search(query):
    # Reuse the bounded provider lock, but retain search order, not price sorting.
    from autobot.real_market_scraper import _ddgs_text, _parse_bing_rss
    import requests
    found, error = search_api(query), ''
    official = found is not None
    if found is None:
        found = []
        try:
            from ddgs import DDGS
            items = _ddgs_text(DDGS, query, timeout=12, region='ru-ru', max_results=20, backend='brave')
            found = [SimpleNamespace(url=item.get('href') or item.get('url') or '', title=item.get('title') or '') for item in items]
        except Exception:
            error = 'резервный поисковик не ответил'
    if len(found) < 10 and not official:
        try:
            response = requests.get('https://www.bing.com/search', params={'format':'rss','q':query}, timeout=(4, 8))
            response.raise_for_status()
            found.extend(_parse_bing_rss(response.text[:1_000_000], max_results=20))
        except requests.RequestException:
            if not found: raise BuyerError('Поисковики временно недоступны. Поиск сохранён для повтора') from None
    links, seen = [], set()
    for item in found:
        try: url = public_url(item.url)
        except BuyerError: continue
        host = urlsplit(url).hostname.removeprefix('www.')
        identity = url if host == 'avito.ru' else host
        if identity in seen: continue
        seen.add(identity)
        links.append({'url': url, 'title': item.title[:240]})
        if len(links) >= 10: break
    if not links and error: raise BuyerError('Поиск временно недоступен: '+error[:250])
    return links


def page_facts(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup(['script', 'style', 'noscript']): node.decompose()
    text = soup.get_text(' ', strip=True)
    if re.search(r'подтвердите,? что вы не робот|доступ ограничен|checking your browser', text[:10000], re.I):
        raise BuyerError('Сайт ограничил автоматическую проверку')
    emails = set(re.findall(r'[\w.%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}', text))
    emails.update(unquote(a['href'][7:]).split('?')[0] for a in soup.select('a[href^="mailto:"]'))
    emails = sorted({v.lower() for v in emails if len(v) <= 254 and not re.search(r'\.(png|jpg|webp|svg)$', v, re.I)},
                    key=lambda v: (not bool(re.match(r'(info|sale|zakaz|order|office|mail)', v)), v))
    channels = []
    contact_pages = []
    for a in soup.select('a[href]'):
        href = a['href']
        if href.startswith('tel:'): channels.append({'channel':'phone','address':unquote(href[4:]),'source_url':url})
        elif href.startswith(('https://t.me/', 'https://wa.me/', 'https://max.ru/')):
            channels.append({'channel':'telegram' if 't.me/' in href else 'whatsapp' if 'wa.me/' in href else 'max','address':href,'source_url':url})
        elif re.search(r'контакт|contact|доставк|delivery', a.get_text(' ',strip=True)+' '+href, re.I):
            target = urljoin(url,href)
            if urlsplit(target).hostname == urlsplit(url).hostname and target not in contact_pages:
                contact_pages.append(target)
    for phone in re.findall(r'(?<!\d)(?:\+7|8)[ (\-]*\d{3}[ )\-]*\d{3}[ \-]*\d{2}[ \-]*\d{2}(?!\d)',text):
        normalized = '+7'+re.sub(r'\D','',phone)[1:]
        if not any(re.sub(r'\D','',c['address'])[-10:]==normalized[-10:] for c in channels if c['channel']=='phone'):
            channels.append({'channel':'phone','address':normalized,'source_url':url})
    name = soup.find('h1') or soup.find('title')
    site_name = soup.find('meta', attrs={'property':'og:site_name'})
    return {'text':text, 'emails':emails, 'channels':channels, 'links':contact_pages[:3],
            'name':str(site_name['content'])[:180] if site_name and site_name.get('content') else name.get_text(' ',strip=True)[:180] if name else urlsplit(url).hostname}


_CATEGORY_EVIDENCE = {
    'cable':r'кабел|провод', 'electrical':r'электро|выключател|распределительн|кабел',
    'lighting':r'светильник|освещен|прожектор|ламп', 'gravel':r'щеб', 'sand':r'пес[окч]',
    'soil':r'растительн\w* грунт|плодород|чернозем', 'concrete':r'бетон', 'steel':r'металлопрокат|сталь|стальн',
    'signs':r'дорожн\w* знак|знак\w* дорожн', 'curb':r'бордюр|бортов\w* кам',
    'dry_mix':r'сух\w* смес|строительн\w* смес', 'asphalt':r'асфальт', 'gotika':r'готика|gothic|gotika',
    'alfresco':r'alfresco|альфреско|опор\w* освещ', 'fiber':r'оптич|волс',
    'network_equipment':r'сетев\w* оборуд|коммутатор|видеонаблюд|ибп|сервер',
    'geosynthetics':r'геореш|геотекст|геосет', 'marking_material':r'разметк|стеклошар',
    'water_pipe':r'труб|пнд', 'electrical_work':r'электромонтаж|электрик|электропровод|прокладк\w* кабел',
    'electrical_testing':r'электролаборатор|электроизмер|измерени\w* сопротивлен',
    'network_work':r'волс|сет\w* связи|оптоволок|видеонаблюд', 'marking_work':r'разметк',
    'haulage':r'грузоперевоз|перевозк|самосвал', 'earthwork':r'землян|транше|экскаватор|разработк\w* грунта',
    'paving_work':r'тротуар|мощен|асфальтир', 'landscape_work':r'озеленен|газон|благоустройств',
    'concrete_work':r'бетонн\w* работ|монолит|фундамент', 'metal_work':r'металлоконструкц|сварочн',
    'geogrid_work':r'геореш|укреплен\w* откос', 'drilling':r'бурен|ямобур',
}


def inspect(task, source, *, fetch=fetch_html):
    from autobot.market_source_adapters import inspect_source_page, source_region_evidence
    url, html = fetch(task['url'])
    facts = page_facts(url, html)
    pages = [(url, html, facts)]
    for target in facts['links']:
        try:
            target, body = fetch(target)
            pages.append((target, body, page_facts(target, body)))
        except (BuyerError, OSError): continue
    combined = ' '.join(p[2]['text'] for p in pages)
    rows = [r for r in source['positions'] if r['position_key'] in task['position_keys']]
    # Category keys can be internal; ground relevance in the actual requested names.
    ignored = {'работы','устройство','установка','выполнение','материалы','монтаж','строительные','стоимость','согласно','типом','типа'}
    anchors = {w[:6].casefold() for r in rows for w in re.findall(r'[а-яё]{4,}', r['name'], re.I) if w.casefold() not in ignored}
    pattern = _CATEGORY_EVIDENCE.get(task['category'])
    relevant = bool(re.search(pattern, combined, re.I)) if pattern else bool(anchors) and sum(a in combined.casefold() for a in anchors) >= min(2,len(anchors))
    if not relevant:
        raise BuyerError('Страница не подтверждает нужный ассортимент или вид работ')
    if task['bucket'] == 'works' and not re.search(r'услуг|работ|монтаж|подряд|укладк|прокладк', combined, re.I):
        raise BuyerError('Не подтверждено выполнение работ')
    regional = next((source_region_evidence(p[1], source['region'], task['bucket']) for p in pages
                     if source_region_evidence(p[1], source['region'], task['bucket'])), '')
    emails = list(dict.fromkeys(e for p in pages for e in p[2]['emails']))
    email = emails[0] if emails else ''
    host = urlsplit(url).hostname.removeprefix('www.')
    if host == 'avito.ru' or host.endswith('.avito.ru'):
        # Platform support contacts are not the advertiser's contacts.
        email, emails = '', []
        facts['channels'] = [{'channel':'avito','address':url,'source_url':url}]
    identity = 'page:'+url if host == 'avito.ru' else 'site:'+host
    evidence_pages = [{'url': p[0], 'sha256': digest(p[1]), 'checked_at':time.time(),
                       'excerpt':p[2]['text'][:2000]} for p in pages]
    prices = []
    for row in rows:
        offer = inspect_source_page(html, url, name=row['name'], target_unit=row['unit'], position_bucket=task['bucket'], quantity=row['quantity'])
        if offer.accepted and offer.price is not None:
            prices.append({'position_key':row['position_key'], 'price_kopecks':int((Decimal(str(offer.price))*100).quantize(Decimal('1'), rounding=ROUND_HALF_UP)),
                           'unit':offer.unit,'source_url':url,'evidence':offer.evidence,'state':'published','observed_at':time.time()})
    return {'id':'discovered-'+digest(identity)[:24], 'company':facts['name'], 'url':url,
            'email':email,'emails':emails,'channels':[c for p in pages for c in p[2]['channels']],
            'position_keys':[r['position_key'] for r in rows], 'categories':[task['category']],
            'region':source['region'], 'region_note':regional or 'Регион поставки или выполнения работ нужно подтвердить',
            'evidence_pages':evidence_pages,'prices':prices,'discovered':True}


def verify_contact(supplier):
    email = supplier.get('email')
    if not email: raise BuyerError('Нет опубликованного email')
    for page in supplier.get('evidence_pages', [])[:4]:
        try:
            url, html = fetch_html(page['url'])
            if email in page_facts(url, html)['emails']: return email
        except (BuyerError, OSError): continue
    raise BuyerError('Email больше не подтверждается на сайте')


def run_once():
    from autobot import buyer_store as store
    step = store.claim()
    if not step:
        store.settle()
        return False
    try:
        if step['kind'] == 'search': store.finish(step, links=search(step['payload']['query']))
        elif step['kind'] == 'prepare':
            from autobot.buyer_workflow import prepare_run
            store.finish(step, prepared=prepare_run(step['source']['tender_id'], step['run_id']))
        else: store.finish(step, candidate=inspect(step['payload'], step['source']))
    except (BuyerError, OSError, ValueError) as error:
        store.finish(step,error=str(error),retry=isinstance(error,OSError) or 'временно' in str(error))
    except Exception:
        log.exception('Supplier discovery failed: %s', step['id'])
        store.finish(step,error='Не удалось проверить источник')
    store.settle()
    return True


_thread = None
_lock = threading.Lock()


def start_worker():
    global _thread
    if os.environ.get('BUYER_DISCOVERY_WORKER') != '1': return
    with _lock:
        if _thread and _thread.is_alive(): return
        def work():
            from autobot.atomic_output import output_lock
            from autobot.paths import DATA_DIR
            while True:
                try:
                    with output_lock(DATA_DIR/'buyer_discovery_worker', timeout=.1):
                        while True:
                            run_once()
                            time.sleep(1)
                except TimeoutError: time.sleep(5)
                except Exception:
                    log.exception('Supplier discovery worker unavailable')
                    time.sleep(5)
        _thread = threading.Thread(target=work,name='buyer-discovery',daemon=True)
        _thread.start()
