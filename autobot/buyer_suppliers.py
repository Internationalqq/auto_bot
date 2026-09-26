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
    dict(id='blagostroy', company='Благострой — дорожные работы', url='https://blagostr.ru/services/asfaltirovanie/',
         email='blagostr@bk.ru', categories=['earthwork','paving_work','landscape_work','marking_work'], evidence='асфальтирован'),
    dict(id='orion', company='Орион — дорожные и бетонные работы', url='https://orion76.ru/czeny/',
         email='skorion77@mail.ru', categories=['paving_work','concrete_work','metal_work'], evidence='тротуарн'),
    dict(id='chistov', company='Чистов — благоустройство', url='https://chistov.biz/blagoustroystvo-territorii',
         email='info@chistov.biz', categories=['landscape_work','earthwork','haulage'], evidence='благоустройств'),
    dict(id='spectr', company='СБ Спектр — электромонтаж и системы связи', url='https://sbspectr.ru/',
         email='info@sbspectr.ru', categories=['electrical_work','network_work','network_equipment','fiber'], evidence='электромонтаж'),
    dict(id='elektriki', company='Электрики Ярославль', url='https://npekpacho.ru/kontakty/',
         email='privet@npekpacho.ru', categories=['electrical_work','network_work'], evidence='электромонтаж'),
    dict(id='betl', company='БЭТЛ — электроизмерения', url='https://betl.ru/',
         email='info@betl.ru', categories=['electrical_testing'], evidence='измерени'),
    dict(id='vigert', company='Вигерт — бетон', url='https://www.vigert.ru/',
         email='vigert76@mail.ru', categories=['concrete'], evidence='бетон'),
    dict(id='investpostavka', company='Инвестпоставка — металлопрокат', url='https://www.investpostavka.ru/',
         email='investpostavka@mail.ru', categories=['steel'], evidence='металлопрокат'),
    dict(id='geodorstroy', company='Геодорстрой — геосинтетика', url='https://yaroslavl.geodorstroy.ru/',
         email='zakaz@geodorstroy.ru', categories=['geosynthetics'], evidence='георешетк'),
    dict(id='asv', company='ТД АСВ — кабель и ВОЛС', url='https://yaroslavl.td-asv.ru/',
         email='yaroslavl@td-asv.ru', categories=['fiber'], evidence='волс'),
    dict(id='megapolis', company='Мегаполис — материалы разметки', url='https://pkmegapolis.ru/materialy-dlya-dorozhnoj-razmetki/',
         email='zakaz@pkmegapolis.ru', categories=['marking_material'], evidence='стеклошарик'),
    dict(id='imperiasnab', company='Империя снабжения — трубы', url='https://imperiasnab.ru/',
         email='imperia.snab@yandex.ru', categories=['water_pipe'], evidence='пнд'),
    dict(id='alfresco', company='ALFRESCO — производитель, Москва', url='https://www.allfresco.ru/',
         email='info@allfresco.ru', categories=['alfresco'], evidence='опоры освещения',
         region_evidence='российский производитель', region_note='Возможность и стоимость доставки в Ярославскую область уточняем у производителя.'),
    dict(id='td-souz', company='ТД СОЮЗ — Готика, доставка из Москвы',
         url='https://td-souz.ru/trotuarnaya-plitka-parket-gold-20-chastichnyj-prokras-300x100x80mm-fabrika-gotika/',
         email='zakaz@td-souz.ru', categories=['gotika'], evidence='фабрика готика',
         region_evidence='доставкой в регионы', region_note='Доставку в Ярославскую область нужно подтвердить.'),
    dict(id='siyan', company='СИЯН — бордюр и благоустройство', url='https://siyan.ru/contacts/',
         email='sbit@siyan.ru', categories=['curb'], evidence='бордюр'),
    dict(id='proplus', company='Профиль-Плюс — асфальтобетон', url='https://proplus77.ru/',
         email='info@proplus77.ru', categories=['asphalt'], evidence='асфальт'),
    dict(id='road-stroy', company='Вектор — дорожное строительство', url='https://yaroslavl.road-stroy.com/services/dorstroy',
         email='info.vectors@yandex.ru', categories=['geogrid_work'], evidence='георешет'),
    dict(id='ab-rent', company='АБ РЕНТ — бурение', url='https://www.ab-rent.ru/bkm-rent/',
         email='ab-rent@yandex.ru', categories=['drilling'], evidence='услуги ямобура'),
    dict(id='megastroy', company='Мегастрой — строительные смеси', url='https://www.megastroy-yar.ru/',
         email='megastroy76@yandex.ru', categories=['dry_mix'], evidence='сухие смеси'),
)
PATTERNS = (
    ('gravel', r'^щебень\b'), ('sand', r'^песок\b'),
    ('soil', r'^(грунт растительн|земля растительн|плодородн)'),
    ('gotika', r'готика|тиманфайа'),
    ('curb', r'^камни бортовые бетонные'),
    ('dry_mix', r'^смеси сухие.*цемент'),
    ('asphalt', r'^смеси асфальтобетонные|^плотная мелкозернистая асфальтобетонная смесь'),
    ('alfresco', r'\baf\d{9,}\b|^опора окк'),
    ('fiber', r'^(оптический (кабель|бокс)|кабель.*оптическ)'),
    ('network_equipment', r'^(источник бесперебойн|коммутатор|сетевой видеорегистратор|коннектор|карта памяти|квм консоль)'),
    ('concrete', r'^смеси бетонные'),
    ('steel', r'^прокат стальной'),
    ('geosynthetics', r'^(георешетка|геотекстиль|геосетка)'),
    ('marking_material', r'^(стеклошарики|краска для дорожной разметки)'),
    ('water_pipe', r'^трубы напорные полиэтиленовые'),
    ('cable', r'^(кабел[ьи]|провод\b|провода\b)'),
    ('signs', r'^(знак[и]? дорожн|дорожн\w* знак|стойк\w* дорожн)'),
    ('lighting', r'^(светильник|прожектор|лампа\b)'),
    ('electrical', r'^(лента сигнальн|муфта кабельн|выключател|автоматическ\w* выключател|щит\b|щиток\b|коробк\w* (распределительн|соединительн)|труба (гофр|ekf)|трубы гофр|узип\b|комплект расширительных выводов|сжим типа|предохранитель проходной|блок распределительн)'),
)
WORK_PATTERNS = (
    ('geogrid_work', r'^армирование грунтовых насыпей георешетками'),
    ('drilling', r'^бурение ям'),
    ('electrical_testing', r'^(испытание кабеля силового|измерение сопротивления|определение удельного сопротивления)'),
    ('network_work', r'^(прокладка волоконно|настройка простых сетевых|аппаратура телевизионная|ящик для трубных|устройство ультразвуковое|монтаж оптического|съемные и выдвижные|включение в аппаратуру|измерение на (кабельной|смонтированном))|коммутатор'),
    ('electrical_work', r'^(установка опор наружного|устройство трубопроводов из полиэтиленовых|покрытие кабеля|светильник, устанавливаемый|предохранитель|заделка концевая|затягивание провода|прокладка резинобитумных|кабель до 35 кв|прибор измерения|заземлитель|шкаф \(пульт\)|пульт управления|электрические проводки)'),
    ('marking_work', r'^нанесение.*дорожной разметки'),
    ('haulage', r'^(перевозка грузов|погрузка в автотранспортное)'),
    ('earthwork', r'^(разработка грунта|разработка траншей|планировка площадей|уплотнение грунта|засыпка траншей)'),
    ('paving_work', r'^(установка бортовых|устройство подстилающих|устройство прослойки|устройство покрыти|резка тротуарной)'),
    ('landscape_work', r'^посев газонов'),
    ('concrete_work', r'^(устройство бетонной подготовки|устройство основания под фундаменты)'),
    ('metal_work', r'^(установка стальных конструкций|металлические конструкции)'),
)


def category(row):
    name = str(row.get('name','')).casefold().replace('ё','е').strip()
    kind = row.get('type_slug')
    patterns = PATTERNS if kind in ('material','product') else WORK_PATTERNS if kind in ('work','service') else ()
    return next((key for key, pattern in patterns if re.search(pattern, name)), None)


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
    works = any(p.get('type_slug') in ('work','service') for p in payload['positions'])
    goods = any(p.get('type_slug') in ('material','product') for p in payload['positions'])
    intro = 'поставить оборудование и выполнить работы по списку' if works and goods else 'выполнить такие работы' if works else 'поставить такие материалы'
    terms = (' По работам укажите отдельно стоимость работ, материалов и техники, чтобы не посчитать их дважды.' if works else '')
    body = (f'Добрый день!\n\nПодскажите, сможете {intro}:\n' + '\n'.join(lines)
            + f"\n\nОбъект — {payload['region']}. Напишите, пожалуйста, цену за указанную единицу по каждой строке, с НДС или без, и сроки.{terms}"
            + (' По материалам уточните наличие и доставку, её стоимость укажите отдельно.' if goods else '')
            + ' Если для расчёта нужны адрес или дополнительные характеристики — уточним. Если можете предложить только замену, обозначьте её отдельно.\n\nСпасибо!')
    result = {'drafts':[{'position_keys':[p['position_key'] for p in payload['positions']],
                         'subject':'Запрос стоимости работ' if works else 'Материалы — наличие и цены', 'body':body}], 'questions':[]}
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
    covered, ids, prepared, contacts = set(), [], [], set()
    jobs.queue.init_db(jobs.DB_PATH)
    # Keep existing conversations and request only the new rows for that contact.
    # enqueue() still fences races / overlapping requests transactionally.
    from autobot import buyer_outbox as outbox
    protected = {}
    prior = {j['id']:j for j in jobs.jobs(source['tender_id'])}
    valid_keys = {p['position_key'] for p in payload['positions']} - set(invalid)
    if outbox.DB_PATH.is_file():
        for item in outbox.listing(source['tender_id']):
            if item['status'] not in ('queued','sending','sent','uncertain'): continue
            entries = (prior.get(item['draft_job_id'],{}).get('result') or {}).get('drafts',[])
            if item['draft_index'] >= len(entries): continue
            keys = set(entries[item['draft_index']]['position_keys']) & valid_keys
            if not keys: continue
            protected.setdefault(item['recipient'],set()).update(keys)
            contacts.add(item['recipient'])
            covered.update(keys)
            if item['draft_job_id'] not in ids: ids.append(item['draft_job_id'])
    for supplier in REGISTRY:
        rows = [p for p in payload['positions'] if p['position_key'] not in invalid
                and p['position_key'] not in protected.get(supplier['email'],set())
                and category(p) in supplier['categories']]
        if not rows: continue
        selected = {**payload, 'positions':rows, 'supplier':supplier}
        result = draft(selected)
        fingerprint = hashlib.sha256(encoded(selected).encode()).hexdigest()
        prepared.append((supplier, selected, result, fingerprint))
        contacts.add(supplier['email'])
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
        result = {'job_ids':ids, 'supplier_count':len(contacts), 'position_count':len(covered), 'updated_at':time.time(),
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
