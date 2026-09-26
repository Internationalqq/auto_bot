"""Recheck regional supplier sites and enqueue one approved RFQ per company.

The first registry covers gravel in Yaroslavl. No paid search, inferred emails,
or claimed stock/prices. Network targets are fixed public supplier URLs.
"""
from contextlib import closing
import hashlib
import json
import re
import threading
import time
import uuid
from urllib.parse import unquote

from bs4 import BeautifulSoup
import requests
from autobot import buyer_outbox as outbox
from autobot import buyer_suppliers as suppliers
from autobot.hermes_buyer import BuyerError, encoded

SOURCES = (
    {'id':'yarstroyteh76', 'company':'Ярстройтех76', 'url':'https://yarstroyteh76.ru/',
     'email':'yarstroyteh76@mail.ru'},
    {'id':'yarkareer', 'company':'ЯР-Карьер', 'url':'https://yarkareer.ru/',
     'email':'yarkareer@yandex.ru'},
    {'id':'beton-yaroslavl24', 'company':'Бетонный завод — Промышленная, 20А',
     'url':'https://beton-yaroslavl24.ru/catalog/sheben', 'email':'yaroslavl.beton@yandex.ru'},
)
_lock = threading.Lock()
_running = False


def selected_sources(tid, job_id):
    job = next((j for j in outbox.buyer_jobs.jobs(tid) if j['id'] == job_id), None)
    supplier = (job or {}).get('payload', {}).get('draft_task', {}).get('supplier')
    if supplier:
        source = suppliers.source_for(supplier.get('id'))
        if source is None: raise BuyerError('Поставщик больше не подключён')
        return (source,)
    return SOURCES


def connect():
    db = outbox.connect()
    db.execute('''CREATE TABLE IF NOT EXISTS buyer_campaigns (
        id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, tender_id TEXT NOT NULL,
        draft_job_id TEXT NOT NULL, draft_index INTEGER NOT NULL, message TEXT NOT NULL,
        region TEXT NOT NULL, status TEXT NOT NULL, lease_until REAL, token TEXT,
        error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS buyer_campaign_contacts (
        campaign_id TEXT NOT NULL, source_id TEXT NOT NULL, company TEXT NOT NULL,
        source_url TEXT NOT NULL, channel TEXT NOT NULL, contact TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', checked_at REAL,
        outbox_id TEXT, PRIMARY KEY(campaign_id,source_id))''')
    db.commit()
    return db


def start(tid, job_id, index, message=None):
    payload, draft = outbox.draft_message(tid, job_id, index, message)
    sources = selected_sources(tid, job_id)
    positions = [p for p in payload['positions'] if p['position_key'] in draft['position_keys']]
    if payload.get('supplier'):
        if not positions or any(suppliers.category(p) not in sources[0]['categories'] for p in positions) or not re.search('ярослав', str(payload.get('region','')), re.I):
            raise BuyerError('Поставщик не соответствует позициям или региону')
    elif (not positions or any('щебень' not in p['name'].casefold() or p.get('type_slug') not in
                            ('material', 'product') for p in positions)
            or not re.search(r'ярослав', str(payload.get('region', '')), re.I)):
        raise BuyerError('Автоподбор пока подключён для щебня в Ярославской области. Для этой позиции укажите контакт вручную.')
    message = {k:draft[k] for k in ('subject','body')}
    fingerprint = hashlib.sha256(encoded([tid,job_id,index,message]).encode()).hexdigest()
    with closing(connect()) as db, db:
        now = time.time()
        db.execute('''INSERT OR IGNORE INTO buyer_campaigns
            (id,fingerprint,tender_id,draft_job_id,draft_index,message,region,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,'queued',?,?)''',
            (uuid.uuid4().hex,fingerprint,tid,job_id,index,encoded(message),payload['region'],now,now))
        saved = db.execute('SELECT id,status FROM buyer_campaigns WHERE fingerprint=?',(fingerprint,)).fetchone()
        key = saved['id']
        missing = db.execute('SELECT count(*) FROM buyer_campaign_contacts WHERE campaign_id=? AND outbox_id IS NOT NULL',(key,)).fetchone()[0] < len(sources)
        if saved['status'] in ('completed','failed') and missing:
            db.execute("UPDATE buyer_campaigns SET status='queued',error='',updated_at=? WHERE id=?",(now,key))
    launch()
    return key


def extract_contact(source, html):
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup(['script','style','noscript']): node.decompose()
    text = soup.get_text(' ', strip=True)
    if not re.search('ярослав', text, re.I) or not re.search(source.get('evidence','щеб'), text, re.I):
        raise BuyerError('На странице не подтверждены категория и регион')
    emails = set(re.findall(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}', text))
    emails.update(unquote(a['href'][7:]).split('?')[0] for a in soup.select('a[href^="mailto:"]'))
    if source['email'] not in {e.lower() for e in emails}:
        raise BuyerError('Опубликованный контакт изменился или отсутствует. Нужна проверка сайта')
    return source['email']


def fetch_contact(source):
    # No arbitrary caller URL or redirected/private network destination.
    if source not in SOURCES and source not in suppliers.REGISTRY: raise BuyerError('Неизвестный источник')
    with requests.get(source['url'], timeout=(4,8), allow_redirects=False, stream=True,
                      headers={'User-Agent':'AutoBot supplier contact verification/1.0'}) as response:
        if response.status_code != 200 or 'text/html' not in response.headers.get('Content-Type',''):
            raise BuyerError('Сайт не вернул страницу контактов; письмо не поставлено в очередь')
        content = bytearray()
        for chunk in response.iter_content(65536):
            content.extend(chunk)
            if len(content) > 2_000_000: raise BuyerError('Страница слишком большая')
        return extract_contact(source, content.decode(response.encoding if response.encoding and
            response.encoding.lower() != 'iso-8859-1' else 'utf-8', errors='replace'))


def run_one():
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("""SELECT * FROM buyer_campaigns WHERE status='queued'
            OR (status='checking' AND lease_until<?) ORDER BY created_at LIMIT 1""",(time.time(),)).fetchone()
        if row is None: return False
        row = dict(row); token = uuid.uuid4().hex
        db.execute("UPDATE buyer_campaigns SET status='checking',token=?,lease_until=?,updated_at=? WHERE id=?",
                   (token,time.time()+120,time.time(),row['id']))
    try:
        for source in selected_sources(row['tender_id'], row['draft_job_id']):
            with closing(connect()) as db:
                done = db.execute('SELECT outbox_id FROM buyer_campaign_contacts WHERE campaign_id=? AND source_id=?',
                                  (row['id'],source['id'])).fetchone()
            if done and done['outbox_id']: continue
            contact = ''; error = ''; outbound = None
            try:
                contact = fetch_contact(source)
            except (BuyerError, requests.RequestException) as exc:
                error = str(exc) if isinstance(exc, BuyerError) else 'Сайт временно недоступен'
            # Fence an expired search BEFORE creating any outbound records.
            with closing(connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                owned = db.execute('SELECT token FROM buyer_campaigns WHERE id=?',(row['id'],)).fetchone()
                if owned['token'] != token: return True
                db.execute('UPDATE buyer_campaigns SET lease_until=?,updated_at=? WHERE id=?',
                           (time.time()+120,time.time(),row['id']))
            if contact:
                outbound = outbox.enqueue(row['tender_id'],row['draft_job_id'],row['draft_index'],contact,
                                          message=json.loads(row['message']))
            with closing(connect()) as db, db:
                db.execute('''INSERT OR REPLACE INTO buyer_campaign_contacts
                    (campaign_id,source_id,company,source_url,channel,contact,status,error,checked_at,outbox_id)
                    VALUES (?,?,?,?,'email',?,?,?,?,?)''', (row['id'],source['id'],source['company'],source['url'],
                    contact,'queued' if outbound else 'unavailable',error,time.time(),outbound))
        with closing(connect()) as db, db:
            db.execute("UPDATE buyer_campaigns SET status='completed',updated_at=? WHERE id=? AND token=?",
                       (time.time(),row['id'],token))
    except Exception:
        # Do not lose the running record or silently enqueue another campaign.
        with closing(connect()) as db, db:
            db.execute("UPDATE buyer_campaigns SET status='failed',error=?,updated_at=? WHERE id=? AND token=?",
                       ('Проверка остановлена. Уже созданные письма сохранены в журнале.',time.time(),row['id'],token))
    return True


def launch():
    global _running
    with _lock:
        if _running: return
        with closing(connect()) as db:
            pending = db.execute("SELECT 1 FROM buyer_campaigns WHERE status IN ('queued','checking') LIMIT 1").fetchone()
        if not pending: return
        _running = True
    def work():
        global _running
        try:
            while True:
                if run_one(): continue
                with closing(connect()) as db:
                    waiting = db.execute("SELECT 1 FROM buyer_campaigns WHERE status IN ('queued','checking') LIMIT 1").fetchone()
                if not waiting: break
                time.sleep(5)
        finally:
            with _lock: _running = False
    threading.Thread(target=work, name='buyer-supplier-check', daemon=True).start()


def listing(tid):
    with closing(connect()) as db:
        result = []
        for row in db.execute('SELECT * FROM buyer_campaigns WHERE tender_id=? ORDER BY created_at DESC',(tid,)):
            result.append({k:row[k] for k in ('id','draft_job_id','draft_index','region','status','error','created_at','updated_at')} |
                {'contacts':[dict(c) for c in db.execute('SELECT * FROM buyer_campaign_contacts WHERE campaign_id=? ORDER BY source_id',(row['id'],))]})
    return result
