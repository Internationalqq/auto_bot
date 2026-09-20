"""Explicit technical requirements retained from an estimate, without inference.

The passport describes the request. It is never evidence of a supplier's price.
The same extracted constraints protect discovery and saved-price reuse.
"""
from __future__ import annotations

import re


def _clean(value: object) -> str:
    result = re.sub(r'\s+', ' ', str(value or '')).strip()
    return '' if result.casefold() in {'nan', 'none', 'nat', '<na>'} else result


def _canonical(value: str) -> str:
    value = value.casefold().replace('ё', 'е').replace(',', '.')
    value = value.translate(str.maketrans({'x': 'х', '×': 'х', '–': '-', '—': '-', '²': '2', '³': '3'}))
    return re.sub(r'\s+', '', value)


def comparable_wording(value: object) -> str:
    """Conservative grammar/terminology aliases, never numeric specifications."""
    text = _clean(value).casefold().replace('ё', 'е')
    text = re.sub(r'\bбортов\w*\s+кам(?:ень|н\w*)\b', 'бордюр', text)
    families = {
        'укладк': 'укладка', 'установк': 'установка', 'протяжк': 'протяжка',
        'затягиван': 'протяжка', 'затяжк': 'протяжка', 'бордюр': 'бордюр',
        'бетонн': 'бетонный', 'тротуарн': 'тротуарный', 'плитк': 'плитка',
        'растительн': 'растительный', 'полиэфирн': 'полиэфирный',
        'иглопробивн': 'иглопробивной', 'неткан': 'нетканый',
    }
    for stem, canonical in families.items():
        text = re.sub(r'\b' + stem + r'[а-я]*\b', canonical, text)
    for pattern, canonical in [
        (r'\bкабел(?:ь|я|ю|ем|е|и|ей|ям|ями|ях)\b','кабель'),
        (r'\bпровод(?:а|у|ом|е|ов|ам|ами|ах)?\b','провод'),
        (r'\bтруб(?:а|ы|е|у|ой|ам|ами|ах)?\b','труба'),
        (r'\bземл(?:я|и|е|ю|ей)\b','земля'),
    ]:
        text = re.sub(pattern, canonical, text)
    return text


def technical_specs(name: object) -> list[dict[str, str]]:
    """Return only written, recognisable traits and their original fragments."""
    original = _clean(name)
    folded = original.casefold().replace('ё', 'е')
    number = r'\d{1,5}(?:[.,]\d{1,3})?'
    patterns = [
        ('dimensions', 'Размеры / сечение', rf'\b{number}\s*[xх×]\s*{number}(?:\s*[xх×]\s*{number})?(?=\b|(?:ок|ос|мк|мс|ож|мн)\b)'),
        ('curb_model', 'Марка бордюра', r'\b(?:бр|бв)\s*\d{1,4}(?:[.,]\d{1,3}){2}\b'),
        ('protection', 'Степень защиты', r'\bip\s*\d{2}\b'),
        ('dimension_label', 'Указанный размер', rf'\b(?:диаметр\w*|толщин\w*|высот\w*|ширин\w*|длин\w*)\s*[:=]?\s*{number}\s*мм\b'),
        ('package', 'Масса / объём', rf'\b{number}\s*(?:кг|литр\w*|л)\b'),
        ('density', 'Поверхностная плотность', rf'\b{number}\s*г(?:р(?:амм(?:а|ов)?)?)?\.?\s*/?\s*м[2²]\b'),
        ('brand', 'Производитель', r'\b(?:кнауф|knauf|церезит|ceresit|технониколь|isover|изовер|роквул|rockwool)\b'),
        ('product_line', 'Продукт', r'\b(?:ротбанд|rotband|гольдбанд|goldband|фуген|fugen)\b'),
        ('hardware_model', 'Модель / артикул', r'(?<!\w)(?:[a-z]{2,}[a-z\d]*-[a-z\d./+-]*\d[a-z\d./+-]*|[a-z]{2,}\d{2,}[a-z\d]*)(?!\w)'),
        ('hardware_model', 'Модель / артикул', r'\b(?:wago\s+\d{3}-\d{3}|(?:ва|ис|щмп|щрн|огц|лсэ)\s*-?\s*\d{1,3}(?:-\d{1,3})*)\b'),
        ('hardware_model', 'Модель / артикул', r'\b(?:шрн-э-\d{1,2}\.\d{3}(?:\.\d)?|кп-ав-\d{4}|бон-\d{2}-\d-\d{2}-[а-я]|рбд-\d+[аa])\b'),
        ('hardware_model', 'Модель / артикул', r'\bduostation\s+\d{4}r\s+(?:af|anyip)\b'),
        ('hardware_model', 'Модель / артикул', r'\b[1-9]п[квнт][а-я]*(?:\([а-я]\))?-\d{1,2}-\d{1,3}/\d{1,3}(?:\([а-я]\))?'),
    ]
    if 'щеб' in folded:
        patterns.append(('fraction', 'Фракция щебня', r'\b\d{1,3}\s*[-–—]\s*\d{1,3}\b'))
        patterns.append(('stone_grade', 'Прочность щебня', r'\b[мm]\s*\d{2,4}\b'))
    if 'бордюр' in folded or 'бортов' in folded:
        patterns.append(('curb_model', 'Марка бордюра', r'\b\d{2,4}(?:[.,]\d{1,3}){2}\b'))
    if 'песок' in folded or 'песка' in folded:
        patterns.append(('sand_class', 'Класс песка', r'\b(?:[iI]{1,2}|[12])\s*класс\w*\b'))
        patterns.append(('sand_grain', 'Крупность песка', r'\b(?:мелк\w*|средн\w*|крупн(?!ост)\w*)\b'))
    if 'газон' in folded or 'травосмес' in folded:
        patterns.append(('grass_variety', 'Вид травосмеси', r'(?:газон|травосмесь)\s*[«"]([^»"]{2,60})[»"]'))
    if 'бетон' in folded:
        patterns.extend([
            ('concrete_grade', 'Марка бетона', r'\b[мm]\s*\d{2,3}\b'),
            ('concrete_class', 'Класс бетона', r'\b[вb]\s*\d{1,2}(?:[.,]\d+)?\b'),
            ('concrete_aggregate', 'Заполнитель бетона', r'\b(?:грави[яйи]\w*|гранит\w*|известняк\w*)\b'),
            ('concrete_frost', 'Морозостойкость бетона', r'\bf\s*(?:\([12]\)\s*)?\d{2,4}\b'),
            ('concrete_water', 'Водонепроницаемость бетона', r'\bw\s*\d{1,2}\b'),
        ])
    if any(marker in folded for marker in ('кабел', 'провод', 'ввг', 'nym', 'пвс', 'шввп')):
        patterns.append(('cable_model', 'Марка кабеля',
                         r'\b(?:а?(?:ввг|вбб?шв)(?:нг)?(?:\s*\([а-яa-z]+\))?(?:\s*[-–—]\s*[a-z]+)?|nym|пвс|шввп|кг)(?![\w(])'))
        patterns.extend([
            ('cable_voltage', 'Напряжение кабеля', r'\b(?:0[.,]66\s*кв|660\s*в|1\s*кв|1000\s*в)\b|(?<=-)\s*(?:660|0[.,]66)\b'),
            ('cable_stranding', 'Исполнение жилы', r'(?<![а-яa-z])(?:ок|ож|мк|мс|мн)\b|\b(?:однопроволочн\w*|многопроволочн\w*)'),
        ])
    result = []
    seen = set()
    for kind, label, pattern in patterns:
        for match in re.finditer(pattern, original, re.I):
            evidence = match.group(0)
            value = _canonical(evidence)
            if kind == 'grass_variety':
                value=_canonical(match.group(1))
                if re.fullmatch(r'универсальн(?:ый|ая|ое|ые)',value):value='универсальная'
            if kind == 'cable_model':
                value = value.replace('а', 'a')
            if kind == 'cable_voltage':
                value = '660' if value.startswith(('660', '0.66')) else '1000'
            if kind == 'cable_stranding':
                value = 'single' if value in {'ок', 'ож'} or value.startswith('однопроволочн') else 'multi'
            if kind == 'density':
                value = re.sub(r'гр(?:амм(?:а|ов)?)?\.?', 'г', value).replace('/', '')
            if kind == 'concrete_aggregate':
                value = 'гравий' if value.startswith('грави') else 'гранит' if value.startswith('гранит') else 'известняк'
            if kind == 'sand_class':
                value = '2' if value.startswith(('ii','2')) else '1'
            if kind == 'sand_grain':
                value = 'мелкий' if value.startswith('мелк') else 'средний' if value.startswith('средн') else 'крупный'
            value = {'кнауф': 'knauf', 'церезит': 'ceresit', 'изовер': 'isover', 'роквул': 'rockwool',
                     'ротбанд': 'rotband', 'гольдбанд': 'goldband', 'фуген': 'fugen'}.get(value, value)
            identity = kind, value
            if identity not in seen:
                result.append({'kind': kind, 'label': label, 'value': value, 'evidence': evidence})
                seen.add(identity)
    return result


def preserve_query_specs(original: object, simplified: str) -> str:
    """Keep required numeric traits even when a commercial alias is shorter."""
    result = simplified
    for spec in technical_specs(original):
        if spec['value'] not in _canonical(result) and _canonical(spec['evidence']) not in _canonical(result):
            result += ' ' + spec['evidence']
    # Model identifiers near the end of long names must survive truncation.
    for match in re.finditer(r'\b[a-zа-я][a-zа-я0-9.-]*\d[a-zа-я0-9.,/-]*\b', _clean(original), re.I):
        value = match.group(0)
        if _canonical(value) not in _canonical(result):
            result += ' ' + value
    return _clean(result)


def requirement_passport(name: object, unit: object, *, position_type: str,
                         normalized_unit: str, can_search: bool) -> dict:
    original_unit = _clean(unit)
    issues = []
    if not normalized_unit:
        issues.append('Единица не определена: проверьте исходную строку сметы')
    if position_type in {'aggregate', 'other'}:
        issues.append('Нужно определить самостоятельный предмет поиска')
    incomplete = incomplete_specification_reason(name)
    if incomplete:
        issues.append(incomplete)
    return {'schema_version': 1, 'original_name': _clean(name), 'original_unit': original_unit,
            'position_type': position_type, 'normalized_unit': normalized_unit,
            'specifications': technical_specs(name), 'issues': issues, 'can_search': can_search and not incomplete}


def incomplete_specification_reason(name: object) -> str:
    """Only visibly damaged/unfinished requirements, never invented traits."""
    value = _clean(name).casefold().replace('ё', 'е')
    if 'кабел' in value and re.search(r'\d\s*[xх×]\s*\d+(?:[бз]|[oо](?![кж]))\w*', value):
        return 'В смете повреждено сечение кабеля; нужна исходная маркировка'
    if ('геотекст' in value or 'геополот' in value) and re.search(r'поверхностн\w*(?:\s+плотност\w*)?\s*(?:ое)?$', value):
        return 'Название обрывается на плотности геотекстиля; укажите плотность из сметы'
    if re.search(r'степен[ьи](?:\s+защит\w*)?\s*$', value):
        return 'В смете обрезана степень защиты изделия; нужно значение IP'
    return ''


def technical_conflict(name: object, evidence: object, *, require_all: bool = True) -> str:
    """An incompatible printed size must not pass a generic name match.

Explicit packaging belongs to the requested product variant. Labelled linear
sizes are retained for discovery; their comparison needs category-specific units.
"""
    incomplete = incomplete_specification_reason(name)
    if incomplete:
        return incomplete
    wanted, found = technical_specs(name), technical_specs(evidence)
    required_kinds = ('dimensions', 'curb_model', 'protection', 'cable_model', 'cable_voltage', 'cable_stranding', 'density', 'package', 'brand', 'product_line', 'concrete_aggregate', 'concrete_frost', 'concrete_water', 'stone_grade', 'sand_class', 'sand_grain', 'hardware_model', 'grass_variety')
    for kind in required_kinds:
        left = {s['value'] for s in wanted if s['kind'] == kind}
        if not left:
            continue
        right = {s['value'] for s in found if s['kind'] == kind}
        if kind == 'sand_grain' and left == {'мелкий'} and not right:
            # Preserve the existing category check: an explicitly bounded
            # fineness module is evidence of the grain group, not its class.
            ranges = re.findall(r'(?:модул\w*\s+крупност\w*|\bмкр?\b)[^0-9]{0,20}(\d+(?:[.,]\d+)?)\s*[-–—]\s*(\d+(?:[.,]\d+)?)', _clean(evidence), re.I)
            if ranges and all(1.5 <= float(lo.replace(',', '.')) <= float(hi.replace(',', '.')) <= 2.0 for lo, hi in ranges):
                right = {'мелкий'}
            elif ranges:
                return 'Указанный модуль крупности не соответствует группе «мелкий песок»'
        label = next(s['label'].lower() for s in wanted if s['kind'] == kind)
        if not right:
            if not require_all:
                continue
            return f'Источник не подтверждает требование: {label}'
        if not left.issubset(right):
            return f'Не совпадает требование: {label}'
    return ''
