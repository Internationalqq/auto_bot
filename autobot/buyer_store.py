"""Durable supplier discovery, sharing the existing outbox database.

Only additive tables; no outbound message is created by this module.
"""
from contextlib import closing
import json
import secrets
import time
import uuid
from autobot import buyer_outbox as outbox
from autobot.business_time import today_iso
from autobot.hermes_buyer import BuyerError, encoded
from autobot.buyer_needs import digest, snapshot, queries


def initialize():
    with closing(outbox.connect()) as db:
        db.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS buyer_search_runs (
          id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, tender_id TEXT NOT NULL,
          payload TEXT NOT NULL, status TEXT NOT NULL, business_date TEXT NOT NULL,
          created_at REAL NOT NULL, updated_at REAL NOT NULL, prepared TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS buyer_search_steps (
          id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, scope TEXT NOT NULL,
          payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
          token TEXT, lease_until REAL, next_at REAL NOT NULL DEFAULT 0,
          error TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '', UNIQUE(run_id,kind,scope));
        CREATE INDEX IF NOT EXISTS buyer_search_claim ON buyer_search_steps(status,next_at);
        CREATE TABLE IF NOT EXISTS buyer_search_candidates (
          run_id TEXT NOT NULL, party_id TEXT NOT NULL, data TEXT NOT NULL,
          PRIMARY KEY(run_id,party_id));
        COMMIT;
        ''')


def enqueue(source, *, delivery='draft'):
    if delivery not in ('draft', 'email'):
        raise BuyerError('Поддерживаются подготовка сообщений и отправка email')
    payload = snapshot(source) | {'delivery': delivery}
    fingerprint = digest(payload)
    initialize()
    with closing(outbox.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT id FROM buyer_search_runs WHERE fingerprint=?', (fingerprint,)).fetchone()
        if old:
            return old['id']
        key, now = uuid.uuid4().hex, time.time()
        db.execute('INSERT INTO buyer_search_runs VALUES (?,?,?,?,?,?,?,?,?)',
                   (key, fingerprint, payload['tender_id'], encoded(payload), 'searching', today_iso(), now, now, ''))
        for query in queries(payload):
            add_step(db, key, 'search', digest(query), query)
        add_step(db, key, 'prepare', 'company-requests', {})
        return key


def add_step(db, run_id, kind, scope, payload):
    db.execute('INSERT OR IGNORE INTO buyer_search_steps(id,run_id,kind,scope,payload) VALUES (?,?,?,?,?)',
               (uuid.uuid4().hex, run_id, kind, scope, encoded(payload)))


def claim():
    if not outbox.DB_PATH.is_file():
        return None
    initialize()
    with closing(outbox.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        now = time.time()
        db.execute("UPDATE buyer_search_steps SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END,token=NULL,error='Предыдущая проверка прервалась' WHERE status='leased' AND lease_until<?", (now,))
        step = db.execute("""SELECT s.*,r.payload AS source FROM buyer_search_steps s
            JOIN buyer_search_runs r ON r.id=s.run_id
            WHERE r.status='searching' AND s.status='queued' AND s.next_at<=?
            AND (s.kind<>'prepare' OR NOT EXISTS (SELECT 1 FROM buyer_search_steps pending
              WHERE pending.run_id=s.run_id AND pending.kind<>'prepare' AND pending.status IN ('queued','leased')))
            ORDER BY r.created_at,s.rowid LIMIT 1""", (now,)).fetchone()
        if step is None:
            return None
        token = secrets.token_urlsafe(24)
        db.execute("UPDATE buyer_search_steps SET status='leased',token=?,lease_until=?,attempts=attempts+1 WHERE id=?", (token, now+180, step['id']))
        return dict(step) | {'token': token, 'payload': json.loads(step['payload']), 'source': json.loads(step['source'])}


def finish(step, *, candidate=None, links=(), prepared=None, error='', retry=False):
    with closing(outbox.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT s.*,r.status AS run_status FROM buyer_search_steps s JOIN buyer_search_runs r ON r.id=s.run_id WHERE s.id=?', (step['id'],)).fetchone()
        if not current or current['run_status'] != 'searching' or current['status'] != 'leased' or current['token'] != step['token'] or current['lease_until'] <= time.time():
            return False
        state = 'queued' if error and retry and current['attempts'] < 3 else 'failed' if error else 'completed'
        db.execute('UPDATE buyer_search_steps SET status=?,token=NULL,lease_until=NULL,error=?,next_at=?,result=? WHERE id=?',
                   (state, error[:700], time.time()+min(180, 15*2**current['attempts']), encoded(candidate or {}), step['id']))
        for link in links:
            count = db.execute("SELECT count(*) FROM buyer_search_steps WHERE run_id=? AND kind='inspect'", (step['run_id'],)).fetchone()[0]
            if count >= 300:
                db.execute("UPDATE buyer_search_steps SET status='failed',error=? WHERE id=?", ('Достигнут лимит 300 проверок сайтов; часть источников не проверена', step['id']))
                break
            add_step(db, step['run_id'], 'inspect', digest([link['url'], step['payload']['position_keys']]), {**step['payload'], **link})
        if candidate:
            from urllib.parse import urlsplit
            for old in db.execute('SELECT data FROM buyer_search_candidates WHERE run_id=?', (step['run_id'],)).fetchall():
                previous = json.loads(old['data'])
                same_host = (urlsplit(previous['url']).hostname == urlsplit(candidate['url']).hostname
                             and 'avito.ru' not in urlsplit(candidate['url']).hostname)
                if (previous['id'] == candidate['id'] or same_host or
                        set(previous.get('emails', [])) & set(candidate.get('emails', []))):
                    db.execute('DELETE FROM buyer_search_candidates WHERE run_id=? AND party_id=?', (step['run_id'], previous['id']))
                    candidate['id'] = previous['id']
                    candidate['company'] = previous['company']
                    candidate['email'] = previous.get('email') or candidate.get('email', '')
                    for field in ('position_keys', 'categories', 'emails'):
                        candidate[field] = list(dict.fromkeys(previous.get(field, [])+candidate.get(field, [])))
                    for field in ('prices', 'channels'):
                        candidate[field] = list({encoded(p): p for p in previous.get(field, [])+candidate.get(field, [])}.values())
                    candidate['evidence_pages'] = list({p['url']: p for p in previous['evidence_pages']+candidate['evidence_pages']}.values())
            db.execute('INSERT OR REPLACE INTO buyer_search_candidates VALUES (?,?,?)', (step['run_id'], candidate['id'], encoded(candidate)))
        if prepared is not None:
            db.execute('UPDATE buyer_search_runs SET prepared=? WHERE id=?', (encoded(prepared), step['run_id']))
        db.execute('UPDATE buyer_search_runs SET updated_at=? WHERE id=?', (time.time(), step['run_id']))
        return True


def settle():
    if not outbox.DB_PATH.is_file(): return
    with closing(outbox.connect()) as db, db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='buyer_search_runs'").fetchone(): return
        db.execute("""UPDATE buyer_search_runs SET status=CASE WHEN EXISTS
          (SELECT 1 FROM buyer_search_steps s WHERE s.run_id=buyer_search_runs.id AND s.status='failed')
          THEN 'partial' ELSE 'completed' END,updated_at=? WHERE status='searching' AND NOT EXISTS
          (SELECT 1 FROM buyer_search_steps s WHERE s.run_id=buyer_search_runs.id AND s.status IN ('queued','leased'))""", (time.time(),))


def listing(tid):
    if not outbox.DB_PATH.is_file(): return []
    with closing(outbox.connect()) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='buyer_search_runs'").fetchone(): return []
        result = []
        for run in db.execute('SELECT * FROM buyer_search_runs WHERE tender_id=? ORDER BY created_at DESC LIMIT 10', (tid,)):
            steps = [dict(r) for r in db.execute('SELECT kind,status,error FROM buyer_search_steps WHERE run_id=?', (run['id'],))]
            result.append({k: run[k] for k in ('id','status','business_date','created_at','updated_at')} |
                          {'position_count': len(json.loads(run['payload'])['positions']), 'steps': steps,
                           'candidates': [json.loads(r['data']) for r in db.execute('SELECT data FROM buyer_search_candidates WHERE run_id=? ORDER BY party_id', (run['id'],))],
                           'prepared': json.loads(run['prepared']) if run['prepared'] else None})
        return result


def source(tid, key):
    if not isinstance(key, str) or not key or len(key)>80: raise BuyerError('Некорректный идентификатор поиска')
    if not outbox.DB_PATH.is_file(): raise BuyerError('Поиск не найден')
    with closing(outbox.connect()) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='buyer_search_runs'").fetchone():
            raise BuyerError('Поиск не найден')
        row = db.execute('SELECT * FROM buyer_search_runs WHERE id=? AND tender_id=?', (key, tid)).fetchone()
        if not row: raise BuyerError('Поиск не найден')
        return dict(row) | {'payload': json.loads(row['payload'])}


def candidates(tid, key):
    source(tid, key)
    with closing(outbox.connect()) as db:
        return [json.loads(r[0]) for r in db.execute('SELECT data FROM buyer_search_candidates WHERE run_id=? ORDER BY party_id', (key,))]


def cancel(tid):
    if not outbox.DB_PATH.is_file(): return
    with closing(outbox.connect()) as db, db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='buyer_search_runs'").fetchone(): return
        db.execute("UPDATE buyer_search_runs SET status='canceled',updated_at=? WHERE tender_id=? AND status='searching'", (time.time(), tid))


def save_prepared(tid, key, result):
    with closing(outbox.connect()) as db, db:
        db.execute('UPDATE buyer_search_runs SET prepared=? WHERE id=? AND tender_id=?', (encoded(result), key, tid))


def retry(tid, key):
    source(tid, key)
    with closing(outbox.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT status FROM buyer_search_runs WHERE id=? AND tender_id=?', (key,tid)).fetchone()
        if current['status'] == 'searching': return
        if current['status'] not in ('partial','canceled'):
            raise BuyerError('Повторить можно остановленный поиск или неудачные проверки')
        db.execute("UPDATE buyer_search_steps SET status='queued',attempts=0,next_at=0,token=NULL,lease_until=NULL WHERE run_id=? AND status IN ('failed','leased')", (key,))
        db.execute("UPDATE buyer_search_steps SET status='queued',attempts=0,next_at=0,token=NULL WHERE run_id=? AND kind='prepare'", (key,))
        db.execute("UPDATE buyer_search_runs SET status='searching',updated_at=? WHERE id=? AND tender_id=?", (time.time(),key,tid))
