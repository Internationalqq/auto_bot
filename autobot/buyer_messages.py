"""User-authored replies in an existing, confirmed supplier conversation."""
from contextlib import closing
import hashlib
import re
import time
import uuid

from autobot import buyer_outbox as box
from autobot.hermes_buyer import BuyerError, encoded


def reply_target(parent, reply):
    sender = reply['sender'].lower()
    original = parent['recipient'].lower()
    domain = original.rsplit('@', 1)[-1]
    public = {'mail.ru', 'bk.ru', 'list.ru', 'inbox.ru', 'internet.ru', 'yandex.ru',
              'ya.ru', 'yandex.com', 'gmail.com', 'googlemail.com', 'outlook.com',
              'hotmail.com', 'live.com', 'icloud.com', 'rambler.ru', 'yahoo.com'}
    if not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}', sender): return None
    if sender != original and (domain in public or sender.rsplit('@', 1)[-1] != domain): return None
    mid = reply['message_id'].removeprefix('rfc822:')
    if not reply['message_id'].startswith('rfc822:') or not re.fullmatch(r'<[^\s<>]{3,460}>', mid): return None
    return sender, mid


def threading_headers(db, key):
    link = db.execute('SELECT reply_id FROM buyer_manual_reply_targets WHERE outbound_id=?', (key,)).fetchone()
    if not link: return {}
    row = db.execute('SELECT * FROM outbound WHERE id=?', (key,)).fetchone()
    manual = db.execute('SELECT parent_outbound_id FROM buyer_manual_messages WHERE outbound_id=?', (key,)).fetchone()
    if not row or not manual: raise BuyerError('Не найдена исходная переписка ответа')
    parent = root_message(db, row['tender_id'], manual['parent_outbound_id'])
    reply = db.execute('SELECT * FROM buyer_replies WHERE id=? AND outbound_id=?', (link['reply_id'], parent['id'])).fetchone()
    target = reply_target(parent, reply) if reply else None
    if not target or target[0] != row['recipient']:
        raise BuyerError('Получатель не совпадает с автором сохранённого ответа')
    return {'in_reply_to': target[1]}


def reply_subject(parent):
    return 'Re: ' + re.sub(r'^(?:Re:\s*)+', '', parent['subject'], flags=re.I)


def root_message(db, tid, key):
    # Both manual and automatic replies retain the original inbox search key.
    seen = set()
    while key not in seen and len(seen) < 10:
        seen.add(key)
        row = db.execute('SELECT * FROM outbound WHERE id=? AND tender_id=?', (key, tid)).fetchone()
        if not row or row['status'] != 'sent':
            raise BuyerError('Сначала дождитесь подтверждения отправки первого запроса')
        link = db.execute('SELECT parent_outbound_id FROM buyer_manual_messages WHERE outbound_id=?', (key,)).fetchone()
        if not link and db.execute("SELECT 1 FROM sqlite_master WHERE name='buyer_followups'").fetchone():
            link = db.execute('SELECT parent_outbound_id FROM buyer_followups WHERE outbound_id=?', (key,)).fetchone()
        if not link:
            return row
        key = link['parent_outbound_id']
    raise BuyerError('Не удалось определить исходное обращение')


def enqueue(tid, parent_id, body, request_id, actor_id):
    if not isinstance(parent_id, str) or not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', request_id):
        raise BuyerError('Некорректный ключ сообщения. Обновите страницу')
    if type(actor_id) is not int or actor_id <= 0:
        raise BuyerError('Не подтверждён автор сообщения')
    if not isinstance(body, str) or not body.strip() or len(body) > 10000 or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', body):
        raise BuyerError('Введите сообщение от 1 до 10 000 символов')
    body = body.replace('\r\n', '\n').replace('\r', '\n').strip()
    with closing(box.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        parent = root_message(db, tid, parent_id)
        previous = db.execute('''SELECT o.*, m.parent_outbound_id FROM buyer_manual_messages m
            JOIN outbound o ON o.id=m.outbound_id WHERE m.actor_id=? AND m.request_id=?''', (actor_id, request_id)).fetchone()
        if previous:
            if previous['tender_id'] != tid or previous['parent_outbound_id'] != parent['id'] or previous['body'] != body:
                raise BuyerError('Этот ключ уже использован для другого сообщения')
            return previous['id']
        key, now = uuid.uuid4().hex, time.time()
        target, selected = None, None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='buyer_replies'").fetchone():
            selected = db.execute('SELECT * FROM buyer_replies WHERE outbound_id=? ORDER BY received_at DESC,id DESC LIMIT 1', (parent['id'],)).fetchone()
            target = reply_target(parent, selected) if selected else None
        fingerprint = hashlib.sha256(encoded(['manual-message', actor_id, request_id]).encode()).hexdigest()
        db.execute('''INSERT INTO outbound
            (id,fingerprint,tender_id,draft_job_id,draft_index,recipient,subject,body,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,'queued',?,?)''',
            (key, fingerprint, tid, parent['draft_job_id'], parent['draft_index'], target[0] if target else parent['recipient'], reply_subject(parent), body, now, now))
        db.execute('INSERT INTO buyer_manual_messages VALUES (?,?,?,?,?)', (key, parent['id'], actor_id, request_id, now))
        if target: db.execute('INSERT INTO buyer_manual_reply_targets VALUES (?,?)', (key, selected['id']))
        return key


def validate(db, outbound):
    """Exempt only recorded human replies from RFQ revision/duplicate checks."""
    link = db.execute('SELECT parent_outbound_id FROM buyer_manual_messages WHERE outbound_id=?', (outbound['id'],)).fetchone()
    if not link:
        return False
    parent = root_message(db, outbound['tender_id'], link['parent_outbound_id'])
    headers = threading_headers(db, outbound['id'])
    if (not headers and outbound['recipient'] != parent['recipient']) or any(outbound[k] != parent[k] for k in ('tender_id', 'draft_job_id', 'draft_index')) or outbound['subject'] != reply_subject(parent):
        raise BuyerError('Получатель или тема не совпадают с исходной перепиской')
    return True
