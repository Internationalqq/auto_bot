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


def technical_specs(name: object) -> list[dict[str, str]]:
    """Return only written, recognisable traits and their original fragments."""
    original = _clean(name)
    folded = original.casefold().replace('ё', 'е')
    number = r'\d{1,5}(?:[.,]\d{1,3})?'
    patterns = [
        ('dimensions', 'Размеры / сечение', rf'\b{number}\s*[xх×]\s*{number}(?:\s*[xх×]\s*{number})?\b'),
        ('curb_model', 'Марка бордюра', r'\b(?:бр|бв)\s*\d{1,4}(?:[.,]\d{1,3}){2}\b'),
        ('protection', 'Степень защиты', r'\bip\s*\d{2}\b'),
        ('dimension_label', 'Указанный размер', rf'\b(?:диаметр\w*|толщин\w*|высот\w*|ширин\w*|длин\w*)\s*[:=]?\s*{number}\s*мм\b'),
        ('package', 'Масса / объём', rf'\b{number}\s*(?:кг|литр\w*|л)\b'),
        ('density', 'Поверхностная плотность', rf'\b{number}\s*г\s*/?\s*м[2²]\b'),
        ('brand', 'Производитель', r'\b(?:кнауф|knauf|церезит|ceresit|технониколь|isover|изовер|роквул|rockwool)\b'),
        ('product_line', 'Продукт', r'\b(?:ротбанд|rotband|гольдбанд|goldband|фуген|fugen)\b'),
    ]
    if 'щеб' in folded:
        patterns.append(('fraction', 'Фракция щебня', r'\b\d{1,3}\s*[-–—]\s*\d{1,3}\b'))
    if 'бетон' in folded:
        patterns.extend([
            ('concrete_grade', 'Марка бетона', r'\b[мm]\s*\d{2,3}\b'),
            ('concrete_class', 'Класс бетона', r'\b[вb]\s*\d{1,2}(?:[.,]\d+)?\b'),
            ('concrete_aggregate', 'Заполнитель бетона', r'\b(?:грави[яйи]\w*|гранит\w*|известняк\w*)\b'),
        ])
    if any(marker in folded for marker in ('кабел', 'провод', 'ввг', 'nym', 'пвс', 'шввп')):
        patterns.append(('cable_model', 'Марка кабеля',
                         r'\b(?:а?ввг(?:нг)?(?:\s*\([а-яa-z]+\))?(?:\s*[-–—]\s*[a-z]+)?|nym|пвс|шввп|кг)(?![\w(])'))
    result = []
    seen = set()
    for kind, label, pattern in patterns:
        for match in re.finditer(pattern, original, re.I):
            evidence = match.group(0)
            value = _canonical(evidence)
            if kind == 'cable_model':
                value = value.replace('а', 'a')
            if kind == 'density':
                value = value.replace('/', '')
            if kind == 'concrete_aggregate':
                value = 'гравий' if value.startswith('грави') else 'гранит' if value.startswith('гранит') else 'известняк'
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
    return {'schema_version': 1, 'original_name': _clean(name), 'original_unit': original_unit,
            'position_type': position_type, 'normalized_unit': normalized_unit,
            'specifications': technical_specs(name), 'issues': issues, 'can_search': can_search}


def technical_conflict(name: object, evidence: object) -> str:
    """An incompatible printed size must not pass a generic name match.

Explicit packaging belongs to the requested product variant. Labelled linear
sizes are retained for discovery; their comparison needs category-specific units.
"""
    wanted, found = technical_specs(name), technical_specs(evidence)
    required_kinds = ('dimensions', 'curb_model', 'protection', 'cable_model', 'density', 'package', 'brand', 'product_line', 'concrete_aggregate')
    for kind in required_kinds:
        left = {s['value'] for s in wanted if s['kind'] == kind}
        if not left:
            continue
        right = {s['value'] for s in found if s['kind'] == kind}
        label = next(s['label'].lower() for s in wanted if s['kind'] == kind)
        if not right:
            return f'Источник не подтверждает требование: {label}'
        if not left.issubset(right):
            return f'Не совпадает требование: {label}'
    return ''
