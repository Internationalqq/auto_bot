"""Explicit supplier sends, isolated from both draft and market-price queues.

An expired send is never reclaimed for execution. It becomes uncertain until
the original worker can report its receipt; a repeat click returns that row.
"""
from contextlib import closing
import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid

from autobot import buyer_jobs
from autobot.hermes_buyer import BuyerError, encoded, validate_draft
from autobot.paths import DATA_DIR

DB_PATH = DATA_DIR / 'buyer_outbox.sqlite3'


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS outbound (
        id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, tender_id TEXT NOT NULL,
        draft_job_id TEXT NOT NULL, draft_index INTEGER NOT NULL, recipient TEXT NOT NULL,
        subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL,
        worker TEXT, token TEXT, lease_until REAL, receipt TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS outbound_attempt_history (
        id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, previous_result TEXT NOT NULL, retried_at REAL NOT NULL)''')
    db.commit()
    return db


def draft_message(tid, job_id, index, message=None):
    job = next((j for j in buyer_jobs.jobs(tid) if j['id'] == job_id), None)
    if not job or job['status'] != 'completed' or not job['result']:
        raise BuyerError('Сначала подготовьте обращение')
    payload = job['payload']['draft_task']
    if payload.get('schema_version', 1) < 2:
        raise BuyerError('Это старый текст. Подготовьте обращение заново')
    if type(index) is not int or not 0 <= index < len(job['result']['drafts']):
        raise BuyerError('Обращение не найдено')
    result = json.loads(encoded(job['result']))
    if message is not None:
        if not isinstance(message, dict) or set(message) != {'subject', 'body'}:
            raise BuyerError('Нужны тема и текст обращения')
        result['drafts'][index].update(message)
    validate_draft(result, payload)
    return payload, result['drafts'][index]


def enqueue(tid, job_id, index, recipient, *, message=None):
    if (not isinstance(recipient, str) or len(recipient) > 254 or
            not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}', recipient.strip())):
        raise BuyerError('Укажите один email поставщика')
    recipient = recipient.strip().lower()
    _, draft = draft_message(tid, job_id, index, message)
    other_drafts = {j['id']:j.get('result') for j in buyer_jobs.jobs(tid)}
    fingerprint = hashlib.sha256(encoded([tid, recipient, draft['subject'], draft['body']]).encode()).hexdigest()
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute("""SELECT id FROM outbound WHERE tender_id=? AND draft_job_id=?
            AND draft_index=? AND recipient=? AND status IN ('queued','sending','sent','uncertain')
            ORDER BY created_at LIMIT 1""", (tid, job_id, index, recipient)).fetchone()
        if existing:
            return existing['id']
        for other in db.execute("SELECT * FROM outbound WHERE tender_id=? AND recipient=? AND draft_job_id<>? AND status IN ('queued','sending','sent','uncertain')", (tid,recipient,job_id)):
            result = other_drafts.get(other['draft_job_id']) or {}
            entries = result.get('drafts', [])
            if other['draft_index'] < len(entries) and set(entries[other['draft_index']]['position_keys']) & set(draft['position_keys']):
                raise BuyerError('Этому поставщику уже создан запрос с такими позициями. Проверьте существующую переписку.')
        now = time.time()
        db.execute('''INSERT OR IGNORE INTO outbound
            (id,fingerprint,tender_id,draft_job_id,draft_index,recipient,subject,body,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,'queued',?,?)''',
            (uuid.uuid4().hex, fingerprint, tid, job_id, index, recipient, draft['subject'], draft['body'], now, now))
        return db.execute('SELECT id FROM outbound WHERE fingerprint=?', (fingerprint,)).fetchone()['id']


def expire(db):
    db.execute("UPDATE outbound SET status='uncertain',updated_at=? WHERE status='sending' AND lease_until<?",
               (time.time(), time.time()))


def retry_blocked(tid, job_id):
    """Explicit retry after a proven no-send; uncertain/sent cannot be retried."""
    drafts = {j['id']:j.get('result') or {} for j in buyer_jobs.jobs(tid)}
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM outbound WHERE id=? AND tender_id=?', (job_id, tid)).fetchone()
        if not row:
            raise BuyerError('Письмо не найдено')
        if row['status'] == 'queued':
            return job_id  # double-click on retry
        if row['status'] != 'blocked' or not row['receipt']:
            raise BuyerError('Повтор разрешён только после подтверждения, что письмо не отправлялось')
        other = db.execute("""SELECT id FROM outbound WHERE tender_id=? AND draft_job_id=?
            AND draft_index=? AND recipient=? AND id<>? AND status IN ('queued','sending','sent','uncertain') LIMIT 1""",
            (tid,row['draft_job_id'],row['draft_index'],row['recipient'],job_id)).fetchone()
        if other:
            raise BuyerError('Для этого адресата уже есть другая отправка этого обращения. Проверьте её в журнале; повтор запрещён.')
        original = drafts.get(row['draft_job_id'],{}).get('drafts',[])
        if row['draft_index'] < len(original):
            keys = set(original[row['draft_index']]['position_keys'])
            for candidate in db.execute("SELECT draft_job_id,draft_index FROM outbound WHERE tender_id=? AND recipient=? AND id<>? AND status IN ('queued','sending','sent','uncertain')",(tid,row['recipient'],job_id)):
                entries = drafts.get(candidate['draft_job_id'],{}).get('drafts',[])
                if candidate['draft_index']<len(entries) and keys & set(entries[candidate['draft_index']]['position_keys']):
                    raise BuyerError('Эти позиции уже есть в другой переписке с поставщиком. Повтор запрещён.')
        db.execute('INSERT INTO outbound_attempt_history(job_id,previous_result,retried_at) VALUES (?,?,?)',
                   (job_id, row['receipt'], time.time()))
        db.execute("UPDATE outbound SET status='queued',receipt=NULL,worker=NULL,token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                   (time.time(), job_id))
        return job_id


def listing(tid):
    with closing(connect()) as db, db:
        expire(db)
        return [{k: row[k] for k in ('id', 'draft_job_id', 'draft_index', 'recipient', 'subject', 'body', 'status', 'created_at', 'updated_at')} |
                {'channel': 'email', 'receipt': json.loads(row['receipt']) if row['receipt'] else None,
                 'attempts': [{'retried_at': a['retried_at'], 'receipt': json.loads(a['previous_result'])}
                    for a in db.execute('SELECT previous_result,retried_at FROM outbound_attempt_history WHERE job_id=? ORDER BY id', (row['id'],))]}
                for row in db.execute('SELECT * FROM outbound WHERE tender_id=? ORDER BY created_at', (tid,))]


def claim(worker):
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        expire(db)
        # Reattach only to the SAME attempt, for local journal reconciliation.
        row = db.execute("SELECT * FROM outbound WHERE worker=? AND status IN ('sending','uncertain') AND receipt IS NULL ORDER BY created_at LIMIT 1", (worker,)).fetchone()
        if row:
            return dict(row) | {'attempt_number': db.execute('SELECT count(*) FROM outbound_attempt_history WHERE job_id=?', (row['id'],)).fetchone()[0]}
        # All workers share the same signed-in browser. A slow or disconnected
        # attempt must finish/reconcile before another letter uses that window.
        if db.execute("SELECT 1 FROM outbound WHERE status IN ('sending','uncertain') AND receipt IS NULL LIMIT 1").fetchone():
            return None
        row = db.execute("SELECT * FROM outbound WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return None
        token = secrets.token_urlsafe(32)
        now = time.time()
        db.execute("UPDATE outbound SET worker=?,token=?,status='sending',lease_until=?,updated_at=? WHERE id=?",
                   (worker, token, now+120, now, row['id']))
        return dict(db.execute('SELECT * FROM outbound WHERE id=?', (row['id'],)).fetchone()) | {
            'attempt_number': db.execute('SELECT count(*) FROM outbound_attempt_history WHERE job_id=?', (row['id'],)).fetchone()[0]}


def update(job_id, worker, token, receipt=None):
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM outbound WHERE id=?', (job_id,)).fetchone()
        if not row or not token or row['worker'] != worker or not secrets.compare_digest(row['token'] or '', token):
            return False
        if receipt is None:
            if row['status'] != 'sending':
                return False
            db.execute('UPDATE outbound SET lease_until=?,updated_at=? WHERE id=?', (time.time()+120, time.time(), job_id))
            return True
        if not isinstance(receipt, dict) or set(receipt) != {'status', 'detail', 'evidence'}:
            raise BuyerError('Некорректное подтверждение отправки')
        if receipt['status'] not in ('sent', 'blocked', 'uncertain'):
            raise BuyerError('Неизвестный результат отправки')
        if any(not isinstance(receipt[k], str) or len(receipt[k]) > 2000 for k in ('detail', 'evidence')):
            raise BuyerError('Некорректное подтверждение отправки')
        if receipt['status'] == 'sent' and not receipt['evidence'].strip():
            raise BuyerError('Нет подтверждения из отправленных писем')
        text = encoded(receipt)
        if row['receipt']:
            return row['receipt'] == text
        db.execute('UPDATE outbound SET status=?,receipt=?,lease_until=NULL,updated_at=? WHERE id=?',
                   (receipt['status'], text, time.time(), job_id))
        return True
