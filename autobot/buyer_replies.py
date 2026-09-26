"""Read-only inbox tasks and immutable supplier replies, separate from estimate money."""
from contextlib import closing
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
import secrets
import time

from autobot import buyer_outbox as box
from autobot.hermes_buyer import BuyerError, encoded


def connect():
    db = box.connect()
    db.executescript('''
    CREATE TABLE IF NOT EXISTS buyer_inbox_checks (
        outbound_id TEXT PRIMARY KEY, status TEXT NOT NULL, next_at REAL NOT NULL,
        worker TEXT, token TEXT, lease_until REAL, checked_at REAL, error TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS buyer_replies (
        id TEXT PRIMARY KEY, outbound_id TEXT NOT NULL, message_id TEXT NOT NULL,
        sender TEXT NOT NULL, raw_text TEXT NOT NULL, received_at REAL NOT NULL,
        evidence TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(outbound_id,message_id));
    CREATE TABLE IF NOT EXISTS buyer_reply_prices (
        reply_id TEXT NOT NULL, position_key TEXT NOT NULL, snapshot TEXT NOT NULL,
        price_kopecks INTEGER, unit TEXT NOT NULL, vat TEXT NOT NULL, availability TEXT NOT NULL,
        delivery TEXT NOT NULL, quote TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
        PRIMARY KEY(reply_id,position_key));
    ''')
    return db


def positions(outbound):
    payload, draft = box.draft_message(outbound['tender_id'],outbound['draft_job_id'],outbound['draft_index'])
    return [{**p,'_request_edited':outbound['body']!=draft['body'],'_request_region':payload.get('region')}
            for p in payload['positions'] if p['position_key'] in draft['position_keys']]


def request_check(tid, key):
    with closing(connect()) as db, db:
        row = db.execute("SELECT * FROM outbound WHERE id=? AND tender_id=? AND status='sent'",(key,tid)).fetchone()
        if row is None: raise BuyerError('Ответы проверяются только у подтверждённой отправки')
        db.execute("INSERT OR IGNORE INTO buyer_inbox_checks(outbound_id,status,next_at) VALUES (?,'waiting',0)",(key,))
        db.execute("UPDATE buyer_inbox_checks SET next_at=0 WHERE outbound_id=? AND status<>'checking'",(key,))


def claim(worker):
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        now = time.time()
        if db.execute("SELECT 1 FROM outbound WHERE status IN ('sending','uncertain') AND receipt IS NULL LIMIT 1").fetchone():
            return None  # First recover the potentially submitted message.
        active = db.execute("SELECT * FROM buyer_inbox_checks WHERE status='checking' LIMIT 1").fetchone()
        if active and active['worker'] != worker:
            return None  # The same signed-in browser is still occupied.
        # Only already-authorized, successfully sent RFQs. Never inspect unrelated mail.
        db.execute("""INSERT OR IGNORE INTO buyer_inbox_checks(outbound_id,status,next_at)
            SELECT id,'waiting',updated_at+300 FROM outbound WHERE status='sent' AND created_at>?""",(now-7*86400,))
        row = (db.execute('SELECT * FROM outbound WHERE id=?', (active['outbound_id'],)).fetchone() if active else
               db.execute("""SELECT o.* FROM buyer_inbox_checks c JOIN outbound o ON o.id=c.outbound_id
                  WHERE o.status='sent' AND c.next_at<=? AND c.status<>'checking'
                  ORDER BY c.next_at LIMIT 1""",(now,)).fetchone())
        if row is None: return None
        # Resume the same local journal/run after a lost connection or restart.
        token = active['token'] if active else secrets.token_urlsafe(32)
        db.execute("UPDATE buyer_inbox_checks SET status='checking',worker=?,token=?,lease_until=?,error='' WHERE outbound_id=?",
                   (worker,token,now+120,row['id']))
        out = {k:row[k] for k in ('id','recipient','subject','body','created_at')}
        out['positions'] = [{'line':i, **{k:p.get(k) for k in ('name','quantity','unit')}} for i,p in enumerate(positions(row),1)]
        out['mapping_trusted'] = not any(p['_request_edited'] for p in positions(row))
        out['token'] = token
        return out


def short(value, limit=2000, *, empty=True):
    if not isinstance(value,str) or len(value)>limit or (not empty and not value.strip()):
        raise BuyerError('Некорректное поле ответа')
    return value.strip()


def norm(value):
    return re.sub(r'\s+','',str(value).casefold().replace('ё','е').replace('³','3').replace('²','2'))


def unit_parts(value):
    text = norm(value).rstrip('.')
    aliases = {'пм':'м','пог.м':'м','кг':'кг','т':'кг','шт':'шт','м':'м','м2':'м2','м3':'м3'}
    if text in aliases: return aliases[text], Decimal(1000 if text=='т' else 1)
    match = re.fullmatch(r'(10|100|1000)(м|м2|м3|шт)',text)
    return (match[2],Decimal(match[1])) if match else (text,Decimal(1))


def comparison_amount(amount, source_unit, target_unit):
    source, sf = unit_parts(source_unit); target, tf = unit_parts(target_unit)
    if amount is None or not source or source!=target: return None
    return int((Decimal(amount)*tf/sf).quantize(Decimal('1'),rounding=ROUND_HALF_UP))


def purchase_signature(row):
    spec = row.get('specification') or {}
    requirements = spec.get('requirements',row.get('requirements')) if isinstance(spec,dict) else row.get('requirements')
    return encoded({'facts':{k:norm(row.get(k)) for k in ('name','quantity','unit')},
                    'requirements':requirements or {}})


def parse_price(item, row, raw):
    if not isinstance(item,dict): raise BuyerError('Некорректная строка цены')
    quote = short(item.get('quote'), 4000, empty=False)
    if quote not in raw: raise BuyerError('Цитата отсутствует в исходном ответе')
    price_text = short(item.get('price'),80)
    unit = short(item.get('unit'),80)
    vat = short(item.get('vat'),100)
    availability = short(item.get('availability',''),400)
    delivery = short(item.get('delivery',''),600)
    amount = None
    try:
        number = Decimal(price_text.replace(' ','').replace('\u00a0','').replace(',','.'))
        if number.is_finite() and 0<number<Decimal('10000000000') and number*100 == (number*100).to_integral_value():
            amount = int(number*100)
    except InvalidOperation: pass
    reasons = []
    if row.get('_request_edited'): reasons.append('Текст запроса изменён: вручную проверьте привязку ответа к позиции')
    quoted_numbers = [n.replace(' ','').replace('\u00a0','').replace(',','.') for n in re.findall(r'(?<!\w)\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?(?!\w)',quote)]
    if amount is None or not any(Decimal(n)*100==amount for n in quoted_numbers): reasons.append('Цена не подтверждена цитатой')
    if comparison_amount(amount,unit,row.get('unit')) is None or not re.search(r'(?<!\w)'+re.escape(norm(unit))+r'(?!\w)',quote.casefold().replace('³','3').replace('²','2')): reasons.append('Уточните единицу цены')
    if not vat or vat not in raw or not re.search(r'с ндс|без ндс|ндс включ|включая ндс|ндс не облага',vat,re.I): reasons.append('Не указан НДС')
    if item.get('exact_match') is not True: reasons.append('Нужно подтвердить характеристики позиции')
    if re.search(r'\bот\s*\d|ориентир|примерн',quote,re.I): reasons.append('Цена ориентировочная')
    if not re.search(r'руб|₽|RUB',quote,re.I): reasons.append('Не подтверждена валюта RUB')
    # Preserve terms verbatim, not model-invented descriptions.
    if availability and availability not in raw: raise BuyerError('Наличие отсутствует в ответе')
    if delivery and delivery not in raw: raise BuyerError('Доставка отсутствует в ответе')
    return (encoded(row),amount,unit,vat,availability,delivery,quote,
            'comparable' if not reasons else 'review','; '.join(reasons))


def update(key, worker, token, result=None):
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        check = db.execute('SELECT * FROM buyer_inbox_checks WHERE outbound_id=?',(key,)).fetchone()
        if not check or check['worker']!=worker or not token or not secrets.compare_digest(check['token'] or '',token): return False
        if check['status']!='checking' or check['lease_until']<time.time(): return False
        if result is None:
            db.execute('UPDATE buyer_inbox_checks SET lease_until=? WHERE outbound_id=?',(time.time()+120,key));return True
        if not isinstance(result,dict) or result.get('status') not in ('checked','blocked'):
            raise BuyerError('Нет результата проверки переписки')
        error = short(result.get('detail',''))
        messages = result.get('messages',[])
        if not isinstance(messages,list) or len(messages)>20 or (result['status']=='blocked' and messages):
            raise BuyerError('Некорректные сообщения')
        outbound = db.execute('SELECT * FROM outbound WHERE id=?',(key,)).fetchone()
        rows = positions(outbound)
        for message in messages:
            if not isinstance(message,dict): raise BuyerError('Некорректное сообщение')
            sender = short(message.get('sender'),254,empty=False).lower()
            if sender != outbound['recipient']: raise BuyerError('Ответ другого отправителя')
            mid = short(message.get('message_id'),500,empty=False)
            raw = short(message.get('text'),50000,empty=False)
            evidence = short(message.get('evidence'),2000,empty=False)
            received = message.get('received_at')
            if isinstance(received,bool) or not isinstance(received,(int,float)) or not outbound['created_at']<=received<=time.time()+300:
                raise BuyerError('Некорректная дата ответа')
            reply_id = hashlib.sha256(encoded([key,mid]).encode()).hexdigest()
            existing = db.execute('SELECT * FROM buyer_replies WHERE id=?',(reply_id,)).fetchone()
            if existing:
                if existing['raw_text']!=raw or existing['sender']!=sender: raise BuyerError('Содержимое известного сообщения изменилось')
                continue
            prices = message.get('prices',[])
            if not isinstance(prices,list) or len(prices)>len(rows): raise BuyerError('Некорректный список цен')
            db.execute('INSERT INTO buyer_replies VALUES (?,?,?,?,?,?,?,?)',
                       (reply_id,key,mid,sender,raw,received,evidence,time.time()))
            seen = set()
            for item in prices:
                line = item.get('line') if isinstance(item,dict) else None
                if type(line) is not int or not 1<=line<=len(rows) or line in seen:
                    raise BuyerError('Неизвестная или повторная строка запроса')
                seen.add(line); row = rows[line-1]
                values = parse_price(item,row,raw)
                db.execute('INSERT INTO buyer_reply_prices VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (reply_id,row['position_key'],*values))
        db.execute("UPDATE buyer_inbox_checks SET status=?,next_at=?,checked_at=?,error=?,lease_until=NULL WHERE outbound_id=?",
                   (result['status'],time.time()+1800,time.time(),error,key))
        return True


def listing(tid):
    with closing(connect()) as db:
        checks = {r['outbound_id']:{k:r[k] for k in ('status','checked_at','error')} for r in db.execute(
            'SELECT c.* FROM buyer_inbox_checks c JOIN outbound o ON c.outbound_id=o.id WHERE o.tender_id=?',(tid,))}
        replies = []
        for r in db.execute('SELECT r.* FROM buyer_replies r JOIN outbound o ON r.outbound_id=o.id WHERE o.tender_id=? ORDER BY r.received_at',(tid,)):
            prices = []
            for p in db.execute('SELECT * FROM buyer_reply_prices WHERE reply_id=?',(r['id'],)):
                snapshot = json.loads(p['snapshot'])
                prices.append(dict(p) | {'comparison_kopecks': comparison_amount(p['price_kopecks'],p['unit'],snapshot.get('unit')), 'estimate_unit':snapshot.get('unit')})
            replies.append(dict(r) | {'prices':prices})
        return {'checks':checks,'messages':replies}


def annotate(tid, rows, region=None):
    # No DB creation on ordinary tender reads; responses stay separate from scraped medians.
    if not box.DB_PATH.is_file(): return
    with closing(box.connect()) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='buyer_reply_prices'").fetchone(): return
        quotes = db.execute('''SELECT p.*,r.received_at,r.sender,r.outbound_id FROM buyer_reply_prices p
            JOIN buyer_replies r ON r.id=p.reply_id JOIN outbound o ON o.id=r.outbound_id
            WHERE o.tender_id=? ORDER BY r.received_at DESC''',(tid,)).fetchall()
    for row in rows:
        row['buyer_quotes'] = []
        seen = set()
        for quote in quotes:
            if quote['position_key']!=row['position_key'] or quote['sender'] in seen: continue
            snapshot = json.loads(quote['snapshot'])
            if snapshot.get('_request_edited') or purchase_signature(snapshot)!=purchase_signature(row): continue
            if region is not None and norm(snapshot.get('_request_region'))!=norm(region): continue
            seen.add(quote['sender'])
            row['buyer_quotes'].append({k:quote[k] for k in ('price_kopecks','unit','vat','availability','delivery','state','reason','received_at','sender')} |
                                      {'comparison_kopecks':comparison_amount(quote['price_kopecks'],quote['unit'],row.get('unit'))})
