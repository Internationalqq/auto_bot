"""Supplier-first RFQs. Scripts retain line identities; no accounting in messages."""
from contextlib import closing
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import time

from autobot import buyer_jobs as jobs
from autobot.hermes_buyer import BuyerError, encoded, task_payload, validate_draft, supplier_brief

# Public business contacts checked 2026-09-26. Revalidated before every send.
# Assortment is a search lead, never a claim of a specific model being in stock.
REGISTRY = (
    dict(id='yarstroyteh76', company='Ярстройтех76', url='https://yarstroyteh76.ru/',
         email='yarstroyteh76@mail.ru', categories=['gravel','sand'], evidence='щеб|пес'),
    dict(id='yarkareer', company='ЯР-Карьер', url='https://yarkareer.ru/',
         email='yarkareer@yandex.ru', categories=['gravel','sand','soil'], evidence='щеб|пес|грунт'),
    dict(id='beton-yaroslavl24', company='Бетонный завод — Промышленная, 20А',
         url='https://beton-yaroslavl24.ru/catalog/sheben', email='yaroslavl.beton@yandex.ru',
         categories=['gravel'], evidence='щеб'),
    dict(id='technolight', company='Технолайт', url='https://tl-electro.ru/contacts/',
         email='info@tl-electro.ru', categories=['cable','electrical','lighting'], evidence='кабел'),
    dict(id='ruselectrica', company='Русэлектрика', url='https://ruselectrica.ru/',
         email='info@ruselectrica.ru', categories=['cable','electrical','lighting'], evidence='кабел'),
    dict(id='dorservis', company='ДорСервис', url='https://dorservisyar.ru/',
         email='terra-2002@yandex.ru', categories=['signs'], evidence='дорожные знаки'),
)
PATTERNS = (
    ('gravel', r'^щебень\b'), ('sand', r'^песок\b'),
    ('soil', r'^(грунт растительн|земля растительн|плодородн)'),
    ('cable', r'^(кабел[ьи]|провод\b|провода\b)'),
    ('signs', r'^(знак[и]? дорожн|дорожн\w* знак|стойк\w* дорожн)'),
    ('lighting', r'^(светильник|прожектор|лампа\b)'),
    ('electrical', r'^(лента сигнальн|муфта кабельн|выключател|автоматическ\w* выключател|щит\b|щиток\b|коробк\w* (распределительн|соединительн)|труба гофр|трубы гофр)'),
)


def category(row):
    if row.get('type_slug') not in ('material','product'):
        return None
    name = str(row.get('name','')).casefold().replace('ё','е').strip()
    return next((key for key, pattern in PATTERNS if re.search(pattern, name)), None)


def source_for(key):
    return next((dict(s) for s in REGISTRY if s['id'] == key), None)


def quantity(value):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n <= 0: raise ValueError()
        return format(n, 'f').rstrip('0').rstrip('.') if '.' in format(n,'f') else format(n,'f')
    except (InvalidOperation, ValueError, TypeError):
        raise BuyerError('Для запроса нужны положительный объём и единица каждой позиции') from None


def draft(payload):
    lines = []
    briefs = supplier_brief(payload)['positions']
    for i, p in enumerate(payload['positions'], 1):
        if not p.get('unit'): raise BuyerError('Не определена единица позиции')
        multiplier = ' × ' if re.match(r'^\d',p['unit']) else ' '
        characteristics = '; '.join(f"{c.get('label') or c.get('kind')}: {c['value']}" for c in briefs[i-1]['characteristics'] if c.get('value') is not None)
        lines.append(f"{i}. {p['name']}{'; '+characteristics if characteristics else ''} — {quantity(p['quantity']).replace('.', ',')}{multiplier}{p['unit']}.")
    body = ('Добрый день!\n\nПодскажите, сможете поставить такие материалы:\n' + '\n'.join(lines)
            + f"\n\nРегион доставки — {payload['region']}. Напишите, пожалуйста, цену за указанную единицу по каждой строке, с НДС или без, наличие и сроки. Сможете привезти? Стоимость доставки укажите отдельно, если для расчёта нужен адрес — уточним.\n\nСпасибо!")
    result = {'drafts':[{'position_keys':[p['position_key'] for p in payload['positions']],
                         'subject':'Материалы — наличие и цены', 'body':body}], 'questions':[]}
    return validate_draft(result, payload)


def prepare(source):
    if not re.search('ярослав', str(source.get('region','')), re.I):
        raise BuyerError('В реестре пока поставщики Ярославской области. Другие регионы ещё не подключены.')
    # Keep authoritative position IDs and quantities even across estimate sections.
    payload = task_payload({**source, 'positions':source['positions'][:100]})
    for offset in range(100, len(source['positions']), 100):
        payload['positions'].extend(task_payload({**source, 'positions':source['positions'][offset:offset+100]})['positions'])
    invalid = {}
    for p in payload['positions']:
        try:
            quantity(p['quantity'])
            if not p.get('unit') or p['unit'] in ('—','-'): raise BuyerError('Не определена единица')
        except BuyerError:
            invalid[p['position_key']] = 'Нужны положительное количество и единица; отрицательные корректировки не заказываем'
    covered, ids, prepared = set(), [], []
    jobs.queue.init_db(jobs.DB_PATH)
    for supplier in REGISTRY:
        rows = [p for p in payload['positions'] if p['position_key'] not in invalid and category(p) in supplier['categories']]
        if not rows: continue
        selected = {**payload, 'positions':rows, 'supplier':supplier}
        result = draft(selected)
        fingerprint = hashlib.sha256(encoded(selected).encode()).hexdigest()
        prepared.append((supplier, selected, result, fingerprint))
        covered.update(p['position_key'] for p in rows)
    with closing(jobs.queue._connect(jobs.DB_PATH)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS buyer_supplier_plans (
            tender_id TEXT PRIMARY KEY, result_json TEXT NOT NULL, updated_at REAL NOT NULL)''')
        for supplier, selected, result, fingerprint in prepared:
            previous = db.execute('SELECT id FROM agent_market_jobs WHERE tender_id=? AND position_key=?',
                                  (source['tender_id'], fingerprint)).fetchone()
            if previous:
                key = previous['id']
            else:
                created = jobs.queue.enqueue_in_transaction(db,source['tender_id'],[{
                    'position_key':fingerprint, 'name':supplier['company'], 'draft_task':selected}])
                key = created['created'][0]['id']
                now = time.time()
                db.execute("UPDATE agent_market_jobs SET status='completed',result_json=?,updated_at=?,completed_at=? WHERE id=?",
                           (encoded(result),now,now,key))
            ids.append(key)
        result = {'job_ids':ids, 'position_count':len(covered), 'updated_at':time.time(),
                  'uncovered':[{'position_key':p['position_key'],'name':p['name'],
                               'reason':invalid.get(p['position_key'],'В реестре пока нет подходящего поставщика или подрядчика')}
                              for p in payload['positions'] if p['position_key'] not in covered]}
        db.execute('INSERT OR REPLACE INTO buyer_supplier_plans VALUES (?,?,?)',(source['tender_id'],encoded(result),result['updated_at']))
    return result


def coverage(tid):
    if not jobs.DB_PATH.is_file(): return None
    with closing(jobs.queue._connect(jobs.DB_PATH)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='buyer_supplier_plans'").fetchone(): return None
        row = db.execute('SELECT result_json FROM buyer_supplier_plans WHERE tender_id=?',(tid,)).fetchone()
        return json.loads(row['result_json']) if row else None
