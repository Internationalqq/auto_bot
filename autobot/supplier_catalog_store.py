"""Supplier catalogue and immutable observations beside the existing price index.

Imports are explicit. Reading an empty/unmigrated database never creates it.
Published amounts are facts about a source, not approval for a tender.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlparse


SCHEMA_VERSION = 1


def clean(value):
    return re.sub(r'\s+', ' ', str('' if value is None else value)).strip()


def folded(value):
    value=clean(value).casefold().replace('ё', 'е').replace('×','х')
    return re.sub(r'(?<=\d)x(?=\d)','х',value)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:32]


def db_path(path=None):
    if path is not None:
        return Path(path)
    from autobot import market_price_index
    return Path(market_price_index.INDEX_DB)


@contextmanager
def connect(path=None, *, write=False):
    target = db_path(path)
    if write:
        target.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(target, timeout=15)
    else:
        con = sqlite3.connect(target.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.create_function('casefold', 1, folded, deterministic=True)
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=5000')
    try:
        with con:
            yield con
    finally:
        con.close()


def ready(path=None):
    if not db_path(path).is_file():
        return False
    with connect(path) as con:
        return bool(con.execute("SELECT 1 FROM sqlite_master WHERE name='supplier_catalog_meta'").fetchone())


def initialize(path=None):
    """Additive v1 migration. No edits to legacy index tables or estimate files."""
    with connect(path, write=True) as con:
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='supplier_catalog_meta'").fetchone():
            versions=[r[0] for r in con.execute('SELECT version FROM supplier_catalog_meta')]
            if versions != [SCHEMA_VERSION]:
                raise ValueError('Unsupported supplier catalogue schema')
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS supplier_catalog_meta (version INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS supplier_catalog_sources (
            id TEXT PRIMARY KEY, supplier_id TEXT NOT NULL, name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE, bucket TEXT NOT NULL, region_label TEXT NOT NULL,
            config_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
            contact_evidence TEXT NOT NULL DEFAULT '', contact_url TEXT NOT NULL DEFAULT '',
            checked_at REAL, updated_at REAL NOT NULL, coverage_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS supplier_catalog_documents (
            id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES supplier_catalog_sources(id),
            url TEXT NOT NULL, observed_at REAL NOT NULL, sha256 TEXT NOT NULL,
            media_type TEXT NOT NULL, body_gzip BLOB NOT NULL, kind TEXT NOT NULL DEFAULT 'source'
        );
        CREATE INDEX IF NOT EXISTS supplier_catalog_document_url ON supplier_catalog_documents(url,observed_at);
        CREATE TABLE IF NOT EXISTS supplier_catalog_items (
            id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES supplier_catalog_sources(id),
            item_key TEXT NOT NULL, name TEXT NOT NULL, search_text TEXT NOT NULL,
            url TEXT NOT NULL, bucket TEXT NOT NULL, unit TEXT NOT NULL,
            price_kopecks INTEGER, price_text TEXT NOT NULL, price_kind TEXT NOT NULL,
            reason TEXT NOT NULL, evidence TEXT NOT NULL, details_json TEXT NOT NULL,
            observed_at REAL NOT NULL, expires_at REAL NOT NULL,
            document_id TEXT NOT NULL REFERENCES supplier_catalog_documents(id),
            observation_id TEXT NOT NULL, UNIQUE(source_id,item_key)
        );
        CREATE INDEX IF NOT EXISTS supplier_catalog_lookup ON supplier_catalog_items(bucket,unit,expires_at);
        CREATE INDEX IF NOT EXISTS supplier_catalog_source_items ON supplier_catalog_items(source_id,observed_at);
        CREATE TABLE IF NOT EXISTS supplier_catalog_observations (
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES supplier_catalog_items(id),
            document_id TEXT NOT NULL REFERENCES supplier_catalog_documents(id),
            observed_at REAL NOT NULL, record_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS supplier_catalog_history ON supplier_catalog_observations(item_id,observed_at);
        CREATE TABLE IF NOT EXISTS supplier_catalog_jobs (
            id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES supplier_catalog_sources(id),
            status TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '', heartbeat_at REAL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            page_budget INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '',
            cancel_requested INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS supplier_catalog_active ON supplier_catalog_jobs(source_id)
            WHERE status IN ('queued','running','paused');
        CREATE TABLE IF NOT EXISTS supplier_catalog_frontier (
            job_id TEXT NOT NULL REFERENCES supplier_catalog_jobs(id), url TEXT NOT NULL,
            kind TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(job_id,url)
        );
        INSERT OR IGNORE INTO supplier_catalog_meta(version) VALUES (1);
        COMMIT;
        ''')
        versions = [r[0] for r in con.execute('SELECT version FROM supplier_catalog_meta')]
        if versions != [SCHEMA_VERSION]:
            raise ValueError('Unsupported supplier catalogue schema')
        columns={r[1] for r in con.execute('PRAGMA table_info(supplier_catalog_sources)')}
        if 'coverage_json' not in columns:
            con.execute("ALTER TABLE supplier_catalog_sources ADD COLUMN coverage_json TEXT NOT NULL DEFAULT '{}'")


def seed_sources(path=None):
    from autobot.supplier_catalogs import REGISTRY_PATH
    rows = json.loads(REGISTRY_PATH.read_text(encoding='utf-8'))
    with connect(path, write=True) as con:
        for row in rows:
            config = dict(row)
            catalog = dict(row.get('catalog') or {})
            config['catalog'] = catalog
            host = (urlparse(row['url']).hostname or '').removeprefix('www.')
            source_id = digest(row['url'])
            con.execute('''INSERT INTO supplier_catalog_sources
                (id,supplier_id,name,url,bucket,region_label,config_json,enabled,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,config_json=excluded.config_json,enabled=excluded.enabled,
                region_label=excluded.region_label,bucket=excluded.bucket,updated_at=excluded.updated_at''',
                (source_id, row.get('supplier_id') or host, row['name'], row['url'],
                 catalog.get('bucket') or (row.get('buckets') or ['materials'])[0],
                 catalog.get('region_label') or ('Россия, условия доставки уточняются' if '*' in row.get('regions',[]) else 'Ярославская область'),
                 json_text(config), int(bool(catalog.get('adapter'))), time.time()))


def sources(path=None):
    if not ready(path):
        return []
    with connect(path) as con:
        rows = con.execute('''SELECT s.*,count(i.id) AS item_count,
            sum(CASE WHEN i.price_kind='published' AND i.expires_at>? THEN 1 ELSE 0 END) AS price_count,
            max(i.observed_at) AS last_import_at FROM supplier_catalog_sources s
            LEFT JOIN supplier_catalog_items i ON i.source_id=s.id AND i.price_kind!='unknown'
            GROUP BY s.id ORDER BY s.name''', (time.time(),)).fetchall()
    return [dict(row, config=json.loads(row['config_json']),coverage=json.loads(row['coverage_json'])) for row in rows]


def source(source_id, path=None):
    if not ready(path):
        return None
    with connect(path) as con:
        row = con.execute('SELECT * FROM supplier_catalog_sources WHERE id=?', (source_id,)).fetchone()
    return dict(row, config=json.loads(row['config_json'])) if row else None


def save_contact(source_id, evidence, url, observed_at, path=None):
    with connect(path, write=True) as con:
        con.execute('UPDATE supplier_catalog_sources SET contact_evidence=?,contact_url=?,checked_at=? WHERE id=? AND (checked_at IS NULL OR checked_at<=?)',
                    (clean(evidence)[:1800], url, observed_at, source_id,observed_at))


def save_coverage(source_id,proof,url,observed_at,*,path=None):
    national=bool(re.search(r'по\s+(?:всей\s+)?(?:россии|рф)|во\s+все\s+регионы\s+россии',proof,re.I))
    coverage={'national_delivery':national,'evidence':clean(proof)[:1200],'url':url,'observed_at':observed_at}
    with connect(path,write=True) as con:
        old=con.execute('SELECT coverage_json FROM supplier_catalog_sources WHERE id=?',(source_id,)).fetchone()
        previous=json.loads(old[0]) if old else {}
        if previous.get('observed_at',0)>observed_at: return
        if not proof and previous.get('url')!=url: return
        if previous.get('national_delivery') and not national and previous.get('url')!=url: return
        con.execute('UPDATE supplier_catalog_sources SET coverage_json=? WHERE id=?',(json_text(coverage),source_id))


def money(value):
    if value is None or clean(value) == '':
        return None
    try:
        amount = Decimal(clean(value).replace(' ', '').replace(',', '.'))
        if not amount.is_finite() or amount < 0 or amount > Decimal('100000000000'):
            raise ValueError('Invalid catalogue price')
        return int((amount * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except InvalidOperation as error:
        raise ValueError('Invalid catalogue price') from error


def save_page(source_id, url, body, observed_at, records, *, media_type='text/html', kind='source', job_id=None, owner=None, path=None):
    """Publish a page and all its observations atomically; replay is idempotent."""
    from autobot.market_evidence_policy import evidence_ttl_days,observed_timestamp
    if not isinstance(observed_at, (int,float)) or not 0 < observed_at <= time.time()+900:
        raise ValueError('Invalid source capture time')
    raw = body.encode('utf-8') if isinstance(body,str) else body
    if len(raw)>8_000_000:
        raise ValueError('Source document too large')
    page_hash = hashlib.sha256(raw).hexdigest()
    document_id = digest(f'{source_id}|{url}|{observed_at}|{page_hash}')
    count = 0
    current_ids=[]
    with connect(path, write=True) as con:
        con.execute('BEGIN IMMEDIATE')
        if job_id and not con.execute("SELECT 1 FROM supplier_catalog_jobs WHERE id=? AND owner=? AND status='running' AND cancel_requested=0",(job_id,owner)).fetchone():
            raise ValueError('Импорт остановлен или передан другому исполнителю')
        con.execute('INSERT OR IGNORE INTO supplier_catalog_documents VALUES (?,?,?,?,?,?,?,?)',
                    (document_id,source_id,url,observed_at,page_hash,media_type,gzip.compress(raw),kind))
        for record in records:
            r = dict(record)
            name, item_url, unit = clean(r.get('name')), clean(r.get('url') or url), clean(r.get('unit'))
            if not name or len(name)>1600 or urlparse(item_url).scheme not in {'http','https'}:
                raise ValueError('Invalid catalogue item')
            bucket = r.get('bucket','materials')
            if bucket not in {'materials','works','equipment','other'}:
                raise ValueError('Invalid catalogue item type')
            price = money(r.get('price'))
            price_kind = r.get('price_kind') or ('published' if price is not None else 'on_request')
            if price_kind not in {'published','conditional','on_request','unknown'}:
                raise ValueError('Invalid price kind')
            item_key = clean(r.get('item_key')) or item_url + '|' + folded(name) + '|' + unit
            item_id = digest(source_id+'|'+item_key)
            current_ids.append(item_id)
            observation_id = digest(item_id+'|'+document_id+'|'+json_text(r))
            published=observed_timestamp((r.get('details') or {}).get('published_at'))
            valid_from=min(observed_at,published) if published else observed_at
            values = (item_id,source_id,item_key,name,folded(name+' '+str(r.get('evidence',''))),item_url,bucket,unit,
                      price,clean(r.get('price')),price_kind,clean(r.get('reason')),clean(r.get('evidence'))[:6000],
                      json_text(r.get('details') or {}),observed_at,valid_from+evidence_ttl_days(bucket,item_url)*86400,
                      document_id,observation_id)
            con.execute('''INSERT INTO supplier_catalog_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,search_text=excluded.search_text,url=excluded.url,
                bucket=excluded.bucket,unit=excluded.unit,price_kopecks=excluded.price_kopecks,price_text=excluded.price_text,
                price_kind=excluded.price_kind,reason=excluded.reason,evidence=excluded.evidence,details_json=excluded.details_json,
                observed_at=excluded.observed_at,expires_at=excluded.expires_at,document_id=excluded.document_id,
                observation_id=excluded.observation_id WHERE excluded.observed_at>=supplier_catalog_items.observed_at''', values)
            con.execute('INSERT OR IGNORE INTO supplier_catalog_observations VALUES (?,?,?,?,?)',
                        (observation_id,item_id,document_id,observed_at,json_text(r)))
            count += 1
        if current_ids and kind in {'catalog','product'}:
            missing=con.execute('SELECT * FROM supplier_catalog_items WHERE source_id=? AND url=? AND observed_at<=? AND id NOT IN ('+','.join('?' for _ in current_ids)+')',
                                [source_id,url,observed_at,*current_ids]).fetchall()
            for item in missing:
                reason='Предложение отсутствует в новом прайсе поставщика'
                withdrawal=digest(item['id']+'|'+document_id+'|withdrawn')
                r={'name':item['name'],'url':item['url'],'unit':item['unit'],'price':None,'price_kind':'unknown','reason':reason}
                con.execute("UPDATE supplier_catalog_items SET price_kopecks=NULL,price_text='',price_kind='unknown',reason=?,observed_at=?,expires_at=?,document_id=?,observation_id=? WHERE id=?",
                            (reason,observed_at,observed_at,document_id,withdrawal,item['id']))
                con.execute('INSERT OR IGNORE INTO supplier_catalog_observations VALUES (?,?,?,?,?)',
                            (withdrawal,item['id'],document_id,observed_at,json_text(r)))
    return count


def document(document_id, path=None):
    if not ready(path):
        return None
    with connect(path) as con:
        row = con.execute('SELECT * FROM supplier_catalog_documents WHERE id=?',(document_id,)).fetchone()
    if not row:
        return None
    result=dict(row)
    result['body']=gzip.decompress(result.pop('body_gzip')).decode('utf-8')
    return result


def context_documents(source_id,path=None):
    if not ready(path): return []
    with connect(path) as con:
        rows=con.execute("SELECT id,url,observed_at FROM supplier_catalog_documents WHERE source_id=? AND kind='context' ORDER BY observed_at DESC LIMIT 20",(source_id,)).fetchall()
    seen=set(); result=[]
    for row in rows:
        if row['url'] not in seen:
            seen.add(row['url']); result.append(document(row['id'],path))
    return result[:6]


def latest_capture_times(urls,path=None):
    urls=list(dict.fromkeys(urls))[:100]
    if not urls or not ready(path): return {}
    with connect(path) as con:
        rows=con.execute('''SELECT url,max(observed_at) FROM supplier_catalog_documents d WHERE url IN ('''+','.join('?' for _ in urls)+''')
            AND EXISTS (SELECT 1 FROM supplier_catalog_observations o WHERE o.document_id=d.id) GROUP BY url''',urls).fetchall()
    return dict(rows)


def items(*, query='', source_id='', bucket='', region='', price_kind='', limit=50, offset=0, path=None):
    empty={'items':[],'total':0,'limit':limit,'offset':offset}
    if not ready(path):
        return empty
    clauses, args = ([] if price_kind else ["i.price_kind!='unknown'"]), []
    for word in re.findall(r'[\w.,×х-]+',folded(query))[:10]:
        clauses.append("i.search_text LIKE ? ESCAPE '\\'")
        args.append('%'+word.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
    for field,value in [('i.source_id',source_id),('i.bucket',bucket),('i.price_kind',price_kind)]:
        if value:
            clauses.append(field+'=?'); args.append(value)
    if region:
        clauses.append("(casefold(s.region_label) LIKE ? OR (i.bucket='materials' AND json_extract(s.coverage_json,'$.national_delivery')=1))")
        args.append('%'+folded(region)+'%')
    where = ' WHERE '+' AND '.join(clauses) if clauses else ''
    limit=max(1,min(200,int(limit))); offset=max(0,min(100000,int(offset)))
    base=' FROM supplier_catalog_items i JOIN supplier_catalog_sources s ON s.id=i.source_id'
    with connect(path) as con:
        total=con.execute('SELECT count(*)'+base+where,args).fetchone()[0]
        rows=con.execute('''SELECT i.*,s.name AS supplier_name,s.supplier_id,s.region_label,
            s.contact_url,s.contact_evidence,s.coverage_json'''+base+where+' ORDER BY i.name,i.source_id LIMIT ? OFFSET ?',[*args,limit,offset]).fetchall()
    result=[]
    for row in rows:
        value=dict(row)
        value['details']=json.loads(value.pop('details_json'))
        value['coverage']=json.loads(value.pop('coverage_json'))
        value['stale']=value['expires_at']<=time.time()
        result.append(value)
    return dict(empty,items=result,total=total,limit=limit,offset=offset)


def observation_reason(item_id, observation_id, path=None):
    if not ready(path):
        return 'Каталог источника недоступен; требуется повторная проверка цены'
    with connect(path) as con:
        row=con.execute('SELECT observation_id,expires_at,price_kind FROM supplier_catalog_items WHERE id=?',(item_id,)).fetchone()
        unchanged=False
        if row and row['observation_id']!=observation_id:
            pair=con.execute('SELECT record_json FROM supplier_catalog_observations WHERE item_id=? AND id IN (?,?)',(item_id,observation_id,row['observation_id'])).fetchall()
            unchanged=len(pair)==2 and pair[0][0]==pair[1][0]
    if row is None or row['observation_id']!=observation_id and not unchanged:
        return 'Предложение поставщика обновилось; повторите сопоставление с каталогом'
    if row['expires_at']<=time.time():
        return 'Цена каталога устарела; требуется обновление источника'
    if row['price_kind']!='published':
        return 'У предложения есть условия; требуется точная цена'
    return ''


def history(item_id, path=None):
    if not ready(path):
        return []
    with connect(path) as con:
        rows=con.execute('SELECT id,observed_at,record_json FROM supplier_catalog_observations WHERE item_id=? ORDER BY observed_at DESC LIMIT 30',(item_id,)).fetchall()
    return [dict(row,record=json.loads(row['record_json'])) for row in rows]
