"""Commercial work identities. A material name is never proof of its installation.

The passport retains written constraints. Aliases help retrieve a contractor's
price row; they do not supply missing dimensions, methods or price conditions.
"""
from __future__ import annotations

import re


def _text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip().casefold().replace('ё', 'е')


_OPERATIONS = (
    ('insulation-test', 'измерение сопротивления изоляции', r'(?:измерен|замер).*сопротивлен.*изоляц'),
    ('soil-test', 'измерение удельного сопротивления грунта', r'(?:измерен|определен).*удельн.*сопротивлен.*грунт'),
    ('ground-test', 'измерение сопротивления заземления', r'(?:измерен|замер).*сопротивлен.*(?:заземл|растекан)'),
    ('metal-test', 'проверка металлосвязи', r'металлосвяз|проверк.*цепи.*заземл'),
    ('cable-test', 'испытание силового кабеля', r'испытан.*кабел'),
    ('wire-pull', 'протяжка провода в трубе', r'(?:протяж|затягиван|затяж).*?(?:провод|кабел).*?(?:труб|гофр)'),
    ('signal-tape', 'укладка сигнальной ленты над кабелем', r'(?:покрыти|укладк|монтаж).*?(?:кабел.*лент|сигнальн.*лент|лент.*сигнальн)'),
    ('geotextile', 'укладка геотекстиля', r'(?:укладк|устройств).*?(?:геотекст|геополот|неткан|нсм)'),
    ('geogrid', 'укладка георешетки', r'(?:укладк|устройств|монтаж).*?георешет'),
    ('paving', 'укладка брусчатки', r'(?:укладк|устройств).*?(?:брусчат|тротуарн.*плит|плит.*тротуарн)'),
    ('kerb', 'установка бордюра', r'(?:установк|устройств|укладк|монтаж).*?(?:бордюр|бортов.*кам|кам.*бортов)'),
    ('lawn', 'посев газона', r'(?:посев|посадк).*?газон'),
    ('concrete-base', 'устройство бетонной подготовки', r'(?:устройств|заливк).*?(?:бетонн.*подготов|подбетон)'),
    ('sand-base', 'устройство песчаного основания', r'(?:устройств|отсыпк).*?(?:песчан.*(?:основан|подуш)|подстилающ.*пес)'),
    ('stone-base', 'устройство щебеночного основания', r'(?:устройств|отсыпк).*?(?:щебеночн.*(?:основан|подуш)|подстилающ.*щеб)'),
    ('backfill', 'обратная засыпка траншей', r'засыпк.*(?:транше|котлован|пазух)'),
    ('excavation', 'разработка грунта', r'(?:разработк.*грунт|копк.*(?:транше|котлован))'),
    ('compaction', 'уплотнение грунта', r'(?:уплотнен|трамбовк).*грунт'),
    ('grading', 'планировка участка', r'планировк.*(?:площад|участ|грунт|территор)'),
    ('loading', 'погрузка грунта', r'погрузк.*(?:грунт|земл)'),
    ('haulage', 'перевозка грунта самосвалом', r'(?:перевозк|транспортировк).*?(?:груз|грунт|земл)'),
    ('light-pole', 'монтаж опор освещения', r'(?:установк|монтаж).*опор.*освещен'),
    ('camera', 'монтаж видеокамеры', r'(?:установк|монтаж).*видеокамер'),
    ('switchboard', 'монтаж электрощита', r'(?:установк|монтаж).*?(?:электрощит|шкаф|щит\b)'),
    ('breaker', 'монтаж автоматического выключателя', r'(?:установк|монтаж).*?(?:автоматич.*выключат|автомат\b)'),
    ('box', 'монтаж распаечной коробки', r'(?:установк|монтаж).*?(?:распа[яе]чн.*короб|короб.*распа[яе]чн)'),
    ('cable-laying', 'прокладка кабеля', r'прокладк.*?(?:кабел|провод)'),
    ('pipe-laying', 'прокладка трубы', r'(?:прокладк|укладк).*?труб'),
    ('vertical-earth', 'монтаж вертикального заземлителя', r'(?:монтаж|установк).*?вертикальн.*?заземлит'),
    ('horizontal-earth', 'монтаж горизонтального заземлителя', r'(?:монтаж|установк).*?горизонтальн.*?заземлит'),
    ('cable-end', 'монтаж концевой кабельной муфты', r'(?:монтаж|установк).*?концев.*?муфт|заделк.*?концев'),
    ('fuse', 'монтаж предохранителя', r'(?:монтаж|установк).*?предохранит'),
    ('fixture', 'монтаж светильника', r'(?:монтаж|установк).*?светильник'),
    ('network-setup', 'настройка сетевого коммутатора', r'настройк.*?(?:коммутатор|сетев)'),
)


# These are noun-first installation descriptions from estimate norms. Apply
# the alias only to a row already classified as work, never to a seller card.
_INSTALLATION_NAMES = (
    (r'^заземлитель вертикальный', 'монтаж вертикального заземлителя'),
    (r'^заземлитель горизонтальный', 'монтаж горизонтального заземлителя'),
    (r'^предохранитель\b', 'монтаж предохранителя'),
    (r'^светильник, устанавливаемый', 'монтаж светильника'),
    (r'^кабель.*?в проложенных трубах', 'протяжка кабеля в трубе'),
    (r'^прибор измерения и защиты.*?автомат', 'монтаж автоматического выключателя'),
    (r'^шкаф.*?управления навесной', 'монтаж навесного электрощита'),
    (r'^пульт управления напольный', 'монтаж напольного электрощита'),
    (r'^аппаратура телевизионная.*?видеокамер', 'монтаж видеокамеры'),
)


def operation(name):
    value = _text(name)
    for family, title, pattern in _OPERATIONS:
        if re.search(pattern, value):
            return family, title
    return '', ''


def _constraints(name):
    value = _text(name)
    result = []
    # These affect the operation itself, rather than the brand of an item
    # bought in a separate material row. Keep fragments for the user/audit.
    traits = (
        ('manual', 'ручной способ', r'\b(?:вручную|ручн\w*)\b'),
        ('excavator', 'экскаватор', r'экскават\w*'),
        ('bulldozer', 'бульдозер', r'бульдозер\w*'),
        ('pneumatic', 'пневматическая трамбовка', r'пневмат\w*'),
        ('flanged', 'фланцевое крепление', r'фланцев\w*'),
        ('trench', 'в траншее', r'в\s+транше\w*'),
        ('corrugated', 'гофрированная труба', r'гофр\w*'),
        ('prepared-pipe', 'готовая труба', r'проложенн\w*\s+труб\w*'),
        ('open', 'открытая прокладка', r'открыт\w*'),
        ('no-fixing', 'без крепления', r'без\s+креплен\w*'),
        ('channel', 'кабельный канал', r'кабельн\w*\s+канал\w*'),
        ('ceiling', 'по потолку', r'по\s+потолк\w*'),
        ('brick', 'кирпич', r'кирпич\w*'),
        ('drywall', 'гипсокартон', r'гипсокарт\w*|\bгкл\b'),
    )
    for kind, label, pattern in traits:
        found = re.search(pattern, value)
        if found: result.append({'kind': kind, 'label': label, 'value': kind, 'evidence': found[0]})
    number = r'\d+(?:[.,]\d+)?'
    for kind, label, pattern in (
        ('height', 'высота', rf'высот\w*\s*[:=]?\s*(?:до\s*)?{number}\s*(?:мм|см|м)\b'),
        ('depth', 'глубина', rf'глубин\w*\s*[:=]?\s*(?:до\s*)?{number}\s*(?:мм|см|м)\b'),
        ('width', 'ширина', rf'ширин\w*\s*[:=]?\s*(?:до\s*)?{number}\s*(?:мм|см|м)\b'),
        ('thickness', 'толщина', rf'толщин\w*\s*[:=]?\s*(?:до\s*)?{number}\s*(?:мм|см|м)\b'),
        ('section', 'сечение', rf'сечени\w*\s*[:=]?\s*(?:до\s*)?{number}\s*мм[2²]'),
        ('diameter', 'диаметр', rf'диаметр\w*\s*[:=]?\s*(?:до\s*)?{number}\s*мм\b'),
        ('voltage', 'напряжение', rf'(?:напряжени\w*\s*[:=]?\s*)?(?:до\s*|свыше\s*){number}\s*кв\b'),
        ('layers', 'число слоёв', rf'{number}\s*сло[яйев]\w*'),
        ('distance', 'расстояние перевозки', rf'на\s+расстояние\s*[:=]?\s*(?:до\s*)?{number}\s*км\b'),
        ('mass', 'масса изделия', rf'масс\w*\s*[:=]?\s*(?:до\s*)?{number}\s*(?:кг|т)\b'),
        ('bucket', 'вместимость ковша', rf'ковш\w*\s+(?:(?:вместимостью|емкостью)\s*)?{number}\s*м[3³]'),
    ):
        for found in re.finditer(pattern, value):
            # Label inflection is irrelevant, but written bounds and units are not.
            numeric = re.search(r'(?:до\s*|свыше\s*)?\d', found[0])
            normalized = re.sub(r'\s+', '', found[0][numeric.start():]).replace(',', '.').replace('²', '2').replace('³', '3')
            result.append({'kind': kind, 'label': label, 'value': normalized, 'evidence': found[0]})
    soil = re.search(r'групп\w*\s+грунт\w*\s*[:=]?\s*(\d+)', value)
    if not soil:
        soil = re.search(r'грунт\w*\s+(\d+)\s*(?:-?й\s*)?групп\w*', value)
    if soil:
        result.append({'kind':'soil-group', 'label':'группа грунта', 'value':soil[1], 'evidence':soil[0]})
    return result


def work_passport(name, unit='', *, declared_work=False):
    family, title = operation(name)
    if not family and declared_work:
        for pattern, alias in _INSTALLATION_NAMES:
            if re.search(pattern, _text(name)):
                family, title = operation(alias)
                break
    return {'schema_version': 1, 'operation': family, 'query': title,
            'original_name': str(name or ''), 'original_unit': str(unit or ''),
            'constraints': _constraints(name)}


def work_match_reason(name, evidence, *, declared_work=False):
    """None means an unrecognised operation; an empty string is an exact match.

    This is an identity check only. Price, quantity, region and full composition
    must still pass the shared evidence checks.
    """
    wanted = work_passport(name, declared_work=declared_work)
    if not wanted['operation']: return None
    found = work_passport(evidence)
    if found['operation'] != wanted['operation']:
        return 'Источник не подтверждает ту же операцию: ' + wanted['query']
    right = {(c['kind'], c['value']) for c in found['constraints']}
    for trait in wanted['constraints']:
        if trait['kind'] == 'prepared-pipe' and wanted['operation'] == 'wire-pull':
            # Pulling is the operation inside a pipe. Installing the pipe
            # itself has a different operation identity.
            continue
        if (trait['kind'], trait['value']) not in right:
            return 'Прайс работы не подтверждает условие: ' + trait['label'] + ' (' + trait['evidence'] + ')'
    # Do not accept a narrower/more expensive method for a different route.
    variants = {'manual', 'excavator', 'bulldozer', 'pneumatic', 'corrugated', 'channel', 'ceiling', 'no-fixing',
                'height', 'depth', 'width', 'thickness', 'diameter', 'section', 'voltage', 'layers', 'distance',
                'mass', 'bucket', 'soil-group'}
    left_kinds = {c['kind'] for c in wanted['constraints']}
    if any(c['kind'] in variants and c['kind'] not in left_kinds for c in found['constraints']):
        return 'В прайсе ограничен способ выполнения; соответствие смете не подтверждено'
    return ''
