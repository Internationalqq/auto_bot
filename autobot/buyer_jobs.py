"""Procurement drafts in a separate queue, reusing the proven lease mechanism."""
from collections import defaultdict
from contextlib import closing
import hashlib
import json
import os
import secrets
import time

from autobot import agent_market_queue as queue
from autobot.hermes_buyer import task_payload, validate_draft, encoded
from autobot.paths import DATA_DIR

DB_PATH = DATA_DIR / 'buyer_jobs.sqlite3'
TOKEN_PATH = DATA_DIR / 'buyer_worker.token'


def authorized(header):
    # Provisioned explicitly at deployment; reading status never creates a key.
    configured = os.environ.get('BUYER_WORKER_TOKEN', '')
    if not configured:
        try:
            configured = TOKEN_PATH.read_text().strip()
        except OSError:
            return False
    supplied = header[7:] if header.startswith('Bearer ') else ''
    return bool(len(configured) >= 32 and supplied and secrets.compare_digest(configured, supplied))


def enqueue(source):
    groups = defaultdict(list)
    for row in source['positions']:
        groups[(str(row.get('section') or 'Без раздела'), str(row.get('type_slug') or 'other'))].append(row)
    tasks = []
    for (section, kind), rows in sorted(groups.items()):
        for offset in range(0, len(rows), 25):
            payload = task_payload({**source, 'positions': rows[offset:offset+25]})
            fingerprint = hashlib.sha256(encoded(payload).encode()).hexdigest()
            label = {'material': 'Материалы', 'product': 'Оборудование и изделия',
                     'work': 'Работы', 'service': 'Услуги', 'aggregate': 'Составные позиции'}.get(kind, 'Прочее')
            tasks.append({'position_key': fingerprint, 'name': section + ' · ' + label,
                          'draft_task': payload, 'max_attempts': 3})
    queue.init_db(DB_PATH)
    ids = []
    with closing(queue._connect(DB_PATH)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        for task in tasks:
            row = db.execute("""SELECT id FROM agent_market_jobs WHERE tender_id=? AND position_key=?
                AND status IN ('queued','leased','completed') ORDER BY created_at DESC LIMIT 1""",
                (source['tender_id'], task['position_key'])).fetchone()
            if row:
                ids.append(row['id'])
            else:
                result = queue.enqueue_in_transaction(db, source['tender_id'], [task])
                ids.extend(x['id'] for x in result['created'])
    return ids


def jobs(tid):
    return queue.list_jobs(tid, path=DB_PATH)


def claim(worker):
    return queue.claim_job(worker, include_uploaded=True, path=DB_PATH, lease_seconds=120)


def heartbeat(job_id, worker, token):
    queue.init_db(DB_PATH)
    return bool(token) and queue.heartbeat_job(job_id, worker, lease_token=token,
                                              path=DB_PATH, lease_seconds=120)


def complete(job_id, worker, token, draft):
    queue.init_db(DB_PATH)
    with closing(queue._connect(DB_PATH)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM agent_market_jobs WHERE id=?', (job_id,)).fetchone()
        if not token or not queue._owns_attempt(row, worker, token):
            return False
        validated = validate_draft(draft, json.loads(row['payload_json'])['draft_task'])
        text = encoded(validated)
        if row['status'] == 'completed':
            return text == row['result_json']
        if not queue._owns_current_lease(row, worker, token):
            return False
        now = time.time()
        db.execute("""UPDATE agent_market_jobs SET status='completed',result_json=?,error='',
                      lease_until=NULL,updated_at=?,completed_at=? WHERE id=?""", (text, now, now, job_id))
        return True


def fail(job_id, worker, token, error):
    queue.init_db(DB_PATH)
    return bool(token) and queue.fail_job(job_id, worker, error, lease_token=token, path=DB_PATH)


def cancel(tid):
    return queue.cancel_pending_jobs(tid, path=DB_PATH)
