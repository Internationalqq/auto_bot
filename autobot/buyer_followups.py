"""Bounded, idempotent replies to delivery-address questions; no model involved."""
from contextlib import closing
import hashlib
import json
import os
import re
import time
import uuid
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError

from autobot import buyer_outbox as box
from autobot.hermes_buyer import encoded, BuyerError
from autobot.paths import DATA_DIR


def connect():
    db = box.connect()
    db.execute('''CREATE TABLE IF NOT EXISTS buyer_followups (
        reply_id TEXT PRIMARY KEY, parent_outbound_id TEXT NOT NULL,
        kind TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '{}', outbound_id TEXT UNIQUE,
        checked_at REAL NOT NULL, next_at REAL NOT NULL)''')
    db.commit()
    return db


def address_question(raw):
    from autobot.buyer_mailru_inbox import unquoted
    text = unquoted(raw).casefold().replace('ё', 'е')
    text = re.split(r'с уважением|_{3,}|-{3,}', text)[0].strip()
    if len(text) > 1200 or re.search(r'автоматическ|auto.?reply|out of office|mailer.daemon', text):
        return False
    if re.search(r'адрес.{0,40}(получен|уже указан|нам известен)|не (?:нужен|нужно|требуется).{0,30}адрес',text):
        return False
    request = re.search(r'скаж|уточн|укаж|пришл|сообщ|нужен|нужн|какой|каков|куда|\?',text)
    return bool(request and re.search(r'адрес.{0,100}(объект|достав|везти)|(?:объект|достав).{0,100}адрес|куда.{0,40}(везти|привезти|достав)', text, re.S))


def delivery_address(tid):
    """Only explicitly labelled jobsite addresses, never customer legal details.

    All extracted DOCX sources must agree. Ambiguous/unsupported documents go
    to review, with no inferred city or fabricated unloading location.
    """
    from autobot.source_documents import _docx_preview, repair_filename
    if not re.fullmatch(r'\d{8,25}', str(tid)):
        return None, 'Некорректный тендер'
    root = DATA_DIR / 'extracted' / tid
    label = re.compile(r'^(?:\d+[.\s]*)*(?:место выполнения работ|место поставки (?:товара|товаров)|адрес объекта|местонахождение объекта)\s*[:\-]?\s*', re.I)
    matches = {}
    files = sorted(root.rglob('*.docx')) if root.is_dir() else []
    if len(files) > 200: return None, 'Слишком много документов для автоматической проверки адреса'
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file() or path.stat().st_size > 16*1024*1024: continue
        try:
            paragraphs = _docx_preview(path)['paragraphs']
        except (OSError, ValueError, KeyError, BadZipFile, ParseError):
            continue
        for index, paragraph in enumerate(paragraphs):
            match = label.match(paragraph)
            if not match: continue
            address = paragraph[match.end():].strip()
            if not address and index + 1 < len(paragraphs): address = paragraphs[index+1].strip()
            address = re.sub(r'\s+', ' ', address).strip(' .;')
            if not 20 <= len(address) <= 800 or not re.search(r'област|город|\bг[ .]|район|республик|край', address, re.I): continue
            if not re.search(r'улиц|\bул[ .]|набережн|шоссе|проспект|пер[ .]|территор|участок|посел|село|деревн|\bд[ .]', address, re.I): continue
            key = re.sub(r'[^\w\d]', '', address.casefold().replace('ё', 'е'))
            matches.setdefault(key, {'address':address, 'document':repair_filename(path.name),
                'path':str(path.relative_to(DATA_DIR)), 'paragraph':index+1,
                'quote':paragraph if paragraph[match.end():].strip() else paragraph+' — '+address,
                'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    if len(matches) != 1:
        return None, 'В документах несколько адресов объекта — нужна проверка' if matches else 'В документах пока не найден однозначный адрес объекта'
    return next(iter(matches.values())), ''


def process_pending():
    if os.environ.get('BUYER_AUTO_ADDRESS_REPLY', '1') != '1': return
    with closing(connect()) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='buyer_replies'").fetchone(): return
        pending = [dict(r) for r in db.execute('''SELECT r.*, o.tender_id, o.recipient, o.subject,
            o.draft_job_id, o.draft_index FROM buyer_replies r JOIN outbound o ON o.id=r.outbound_id
            LEFT JOIN buyer_followups f ON f.reply_id=r.id
            WHERE o.status='sent' AND r.received_at>? AND (f.reply_id IS NULL OR (f.status='review' AND f.next_at<?))
            ORDER BY r.received_at LIMIT 5''', (time.time()-7*86400,time.time()))]
    for reply in pending:
        is_question = address_question(reply['raw_text'])
        source, reason = (delivery_address(reply['tender_id']) if is_question else (None, 'Ответ не требует автоматического уточнения адреса'))
        if reply['sender'].lower() != reply['recipient'].lower():
            source = None; reason = 'Отправитель отличается от адресата запроса — нужна проверка'
        status = 'queued' if source else 'review' if is_question else 'ignored'
        with closing(connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM buyer_followups WHERE reply_id=?', (reply['id'],)).fetchone()
            if existing and existing['status'] != 'review': continue
            prior = db.execute("SELECT 1 FROM buyer_followups WHERE parent_outbound_id=? AND outbound_id IS NOT NULL AND reply_id<>?", (reply['outbound_id'], reply['id'])).fetchone()
            if prior and is_question:
                source = None; status = 'review'; reason = 'Адрес уже направлен; повторный вопрос требует проверки'
            manual = db.execute('''SELECT 1 FROM buyer_manual_messages m JOIN outbound o ON o.id=m.outbound_id
                WHERE m.parent_outbound_id=? AND m.created_at>=? AND o.status IN ('queued','sending','sent','uncertain') LIMIT 1''',
                (reply['outbound_id'],reply['received_at'])).fetchone()
            if manual and is_question:
                source = None; status = 'review'; reason = 'Пользователь уже написал в эту переписку после ответа поставщика'
            key = None
            if source:
                subject = 'Re: '+re.sub(r'^(?:Re:\s*)+', '', reply['subject'], flags=re.I)
                body = ('Добрый день!\n\nАдрес объекта по документации: '+source['address']+'.\n\n'
                        'Пожалуйста, рассчитайте доставку по этому адресу для объёма из нашего запроса. '
                        'Стоимость доставки укажите отдельно от стоимости материалов.\n\nСпасибо!')
                fingerprint = hashlib.sha256(encoded(['address-reply',reply['id']]).encode()).hexdigest()
                now = time.time()
                db.execute('''INSERT OR IGNORE INTO outbound
                    (id,fingerprint,tender_id,draft_job_id,draft_index,recipient,subject,body,status,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,'queued',?,?)''',
                    (uuid.uuid4().hex, fingerprint, reply['tender_id'],reply['draft_job_id'],reply['draft_index'],reply['recipient'],subject,body,now,now))
                key = db.execute('SELECT id FROM outbound WHERE fingerprint=?',(fingerprint,)).fetchone()[0]
                reason = 'Адрес найден в документах; ответ поставлен в очередь'
            db.execute('''INSERT OR REPLACE INTO buyer_followups
                (reply_id,parent_outbound_id,kind,status,reason,source,outbound_id,checked_at,next_at)
                VALUES (?,?,'delivery_address',?,?,?,?,?,?)''',
                (reply['id'],reply['outbound_id'],status,reason,encoded(source or {}),key,time.time(),time.time()+600))


def listing(tid):
    with closing(connect()) as db:
        return [dict(r) | {'source':json.loads(r['source'])} for r in db.execute('''
            SELECT f.* FROM buyer_followups f JOIN outbound o ON o.id=f.parent_outbound_id
            WHERE o.tender_id=? AND f.status<>'ignored' ORDER BY f.checked_at''',(tid,))]


def validate_source(outbound):
    with closing(connect()) as db:
        row = db.execute('SELECT source FROM buyer_followups WHERE outbound_id=?',(outbound['id'],)).fetchone()
    if not row: return
    previous = json.loads(row['source'])
    current, error = delivery_address(outbound['tender_id'])
    if not current or current['address'] != previous.get('address') or current['sha256'] != previous.get('sha256'):
        raise BuyerError('Документы с адресом изменились; автоматический ответ остановлен. '+error)
