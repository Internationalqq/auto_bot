"""Immutable procurement snapshots. No estimate money crosses this boundary."""
from __future__ import annotations

import hashlib
import re
from autobot.hermes_buyer import BuyerError, encoded, task_payload, supplier_brief
from autobot.buyer_drafts import quantity


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def revision(source):
    clean = snapshot(source)
    # Row order is presentation; requirements, identity, quantity and region are not.
    return digest({'tender_id': clean['tender_id'], 'region': clean['region'],
                   'positions': sorted(clean['positions'], key=lambda p: p['position_key'])})


def snapshot(source):
    rows = source.get('positions')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2000:
        raise BuyerError('Выберите от 1 до 2000 позиций')
    region = str(source.get('region') or '').strip()
    if not region or len(region) > 200:
        raise BuyerError('Сначала укажите регион объекта')
    result, rejected, seen = [], [], set()
    for offset in range(0, len(rows), 100):
        payload = task_payload({**source, 'positions': rows[offset:offset+100]})
        brief = supplier_brief(payload)
        for row, public in zip(payload['positions'], brief['positions']):
            key = row['position_key']
            if key in seen:
                raise BuyerError('Повторяющийся идентификатор позиции')
            seen.add(key)
            try:
                if row['type_slug'] not in ('material', 'product', 'work', 'service'):
                    raise BuyerError('Расчётная строка не является закупкой')
                amount = quantity(row['quantity'])
                if not row['unit'] or row['unit'] in ('—', '-'):
                    raise BuyerError('Не определена единица позиции')
            except BuyerError as error:
                rejected.append({'position_key': key, 'name': row['name'], 'reason': str(error)})
                continue
            result.append({'position_key': key, 'name': row['name'], 'quantity': amount,
                           'unit': row['unit'], 'type_slug': row['type_slug'],
                           'section': row.get('section'), 'parent_position_id': row.get('parent_position_id'),
                           'specification': {'requirements': {'specifications': public['characteristics']}}})
    if not result:
        raise BuyerError('Нет позиций с определёнными объёмом и единицей')
    return {'tender_id': source['tender_id'], 'region': region, 'positions': result,
            'rejected': rejected, 'schema_version': 1}


def product_identifiers(name):
    """Written SKU/model and dimensions, shared by discovery and qualification."""
    from autobot.market_requirements import technical_specs
    terms = [s['evidence'] for s in technical_specs(name)
             if s['kind'] in ('hardware_model','cable_model','dimensions','curb_model')]
    terms += re.findall(r'(?<!\w)[а-яa-z]{1,6}\d{2,}[а-яa-z]?(?!\w)', name, re.I)
    return list(dict.fromkeys(terms))


def identifier_matches(term, text):
    def canonical(value):
        value=value.casefold().replace('ё','е').replace('×','х').replace('–','-').replace('—','-')
        value=value.translate(str.maketrans('abcehkmoptxyu','авсенкмортхуу'))
        value=re.sub(r'(?<=\d),(?=\d)', '.', value)
        value=re.sub(r'\s*([/\-])\s*', r'\1', value)
        value=re.sub(r'\(\s*','(',value)
        value=re.sub(r'\s*\)',')',value)
        return re.sub(r'(?<=\d)\s*х\s*(?=\d)', 'х', value)
    return bool(re.search(r'(?<![\w.,])'+re.escape(canonical(term))+r'(?![\w.,])',canonical(text)))


WORK_PROFILES={
    'external_electrical':('монтаж наружных электрических сетей кабельных линий',
        r'наружн\w*\s+(?:электр\w*\s+)?(?:сет|освещ)|кабельн\w*\s+лини|силов\w*\s+кабел|электроснабжен\w*\s+(?:предприятий|объектов)|до\s*35\s*кв'),
    'lighting_installation':('монтаж опор наружного освещения',r'(?:монтаж|установк)\w*[^.!?]{0,60}опор|наружн\w*\s+освещ'),
}


def work_profile(row):
    name=row['name'].casefold().replace('ё','е')
    if re.search(r'(?:установк|монтаж)\w*\s+опор.*(?:наружн|освещ)',name):return 'lighting_installation'
    if re.search(r'кабель\s+до\s*35\s*кв|наружн\w*\s+электр|силов\w*\s+кабел|кабельн\w*\s+лини',name):return 'external_electrical'
    return ''


def queries(payload):
    from autobot.buyer_suppliers import category
    names = {'cable': 'кабель', 'lighting': 'светильники',
             'electrical': 'электроматериалы', 'electrical_work': 'электромонтажные работы',
             'electrical_testing': 'электролаборатория измерения', 'earthwork': 'земляные работы',
             'paving_work': 'укладка тротуарной плитки', 'gravel': 'щебень', 'sand': 'песок',
             'concrete': 'бетон', 'signs': 'дорожные знаки', 'network_work': 'монтаж сетей связи'}
    names.update(soil='растительный грунт', gotika='тротуарная плитка Готика', curb='бордюрный камень',
                 dry_mix='сухие строительные смеси', asphalt='асфальтобетонная смесь',
                 alfresco='опоры освещения ALFRESCO', fiber='оптический кабель ВОЛС',
                 network_equipment='сетевое оборудование', steel='металлопрокат',
                 geosynthetics='геотекстиль георешетка', marking_material='материалы дорожной разметки',
                 water_pipe='полиэтиленовые трубы ПНД', marking_work='дорожная разметка',
                 haulage='перевозка строительных грузов', landscape_work='благоустройство озеленение',
                 concrete_work='бетонные работы', metal_work='монтаж металлоконструкций',
                 geogrid_work='укладка георешетки', drilling='бурение ям ямобур')
    groups = {}
    for row in payload['positions']:
        kind = 'works' if row['type_slug'] in ('work', 'service') else 'materials'
        detected = category(row)
        if not detected and kind == 'works' and re.search(r'электромонтаж|электропровод|(?:монтаж|прокладка|подключение).*(?:кабел|провод|электро|розет|щит)', row['name'], re.I):
            detected = 'electrical_work'
        group = detected or re.sub(r'\s+', ' ', row['name']).strip()[:100]
        key = (kind, group, work_profile(row) if kind=='works' else '')
        groups.setdefault(key, []).append(row['position_key'])
    result = []
    for (kind, group, profile), keys in groups.items():
        title = WORK_PROFILES[profile][0] if profile else names.get(group, group)
        terms = 'подрядчик' if kind == 'works' else 'поставщик'
        result.append({'query': f'{title} {terms} {payload["region"]}'[:380],
                       'position_keys': keys, 'bucket': kind, 'category': group, 'intent':'supplier', 'work_profile':profile})
        if kind == 'works':
            result.append({'query': f'site:avito.ru {title} {payload["region"]}'[:380],
                           'position_keys': keys, 'bucket': kind, 'category': group, 'intent':'supplier', 'work_profile':profile})
    # Separate exact material queries from supplier-profile queries. A catalogue
    # home page can prove assortment, but cannot prove a particular item's price.
    exact = {}
    for row in payload['positions']:
        if row['type_slug'] not in ('material','product'): continue
        from autobot.market_strategy import market_query_name
        from autobot.market_requirements import preserve_query_specs, technical_specs
        identifiers = product_identifiers(row['name'])
        title = ' '.join('"'+term.replace('"','')+'"' for term in identifiers) if identifiers else market_query_name(row['name'],row['type_slug'])
        if identifiers and not any(s['kind'] in ('hardware_model','cable_model','curb_model') for s in technical_specs(row['name'])):
            # A dimension alone (210x210) may describe dozens of unrelated goods.
            title=names.get(category(row),market_query_name(row['name'],row['type_slug']))+' '+title
        title=preserve_query_specs(row['name'],title)
        key = (title.strip(), category(row) or row['name'][:100])
        exact.setdefault(key, []).append(row['position_key'])
    for (title, group), keys in exact.items():
        result.append({'query':f'{title[:270]} купить {payload["region"]}'[:380],
                       'position_keys':keys,'bucket':'materials','category':group,'intent':'product',
                       'fallback_query':f'{title[:270]} купить доставка'[:380]})
    return result
