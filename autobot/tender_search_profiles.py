"""Validated search snapshots and a small, atomically updated profile catalogue."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re

from autobot.atomic_output import output_lock
from autobot.tender_search_state import atomic_json

MAX_BYTES = 128 * 1024
MAX_PROFILES = 20
MAX_PRICE_KOPECKS = 100_000_000_000_000
FILTER_FIELDS = {'regions', 'keywords', 'price_min_kopecks', 'price_max_kopecks',
                 'needed_stage', 'days_back', 'max_pages', 'max_tenders'}


class ProfileConflict(ValueError):
    pass


def _text(value, label, maximum):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(f'{label}: нужен текст длиной до {maximum} символов.')
    result = ' '.join(value.split())
    if not result:
        raise ValueError(f'{label}: заполните поле.')
    return result


def _terms(value, label, maximum):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ValueError(f'{label}: укажите от 1 до {maximum} значений.')
    result, seen = [], set()
    for term in value:
        text = _text(term, label, 120)
        if text.casefold() not in seen:
            result.append(text)
            seen.add(text.casefold())
    return result


def validate_filters(value):
    if not isinstance(value, dict) or set(value) != FILTER_FIELDS:
        raise ValueError('Передайте полный набор условий поиска без посторонних полей.')
    result = {'regions': _terms(value['regions'], 'Регионы', 10),
              'keywords': _terms(value['keywords'], 'Темы', 20)}
    if len(result['regions']) * len(result['keywords']) > 60:
        raise ValueError('Не более 60 сочетаний регионов и тем в одном профиле.')
    for key in ('price_min_kopecks', 'price_max_kopecks'):
        amount = value[key]
        if amount is not None and (type(amount) is not int or not 0 <= amount <= MAX_PRICE_KOPECKS):
            raise ValueError('Границы суммы: целое число копеек от нуля до 1 трлн рублей или пустое значение.')
        result[key] = amount
    low, high = result['price_min_kopecks'], result['price_max_kopecks']
    if low is not None and high is not None and low > high:
        raise ValueError('Минимальная сумма не может превышать максимальную.')
    if value['needed_stage'] != 'Подача заявок':
        raise ValueError('Сейчас поддерживается поиск на этапе «Подача заявок».')
    result['needed_stage'] = value['needed_stage']
    for key, maximum in (('days_back', 365), ('max_pages', 20), ('max_tenders', 100)):
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise ValueError(f'{key}: целое число от 1 до {maximum}.')
        result[key] = value[key]
    return result


def default_filters():
    return {'regions': ['Ставропольский край', 'Челябинская область', 'Ярославская область'],
            'keywords': ['строительство', 'благоустройство'],
            'price_min_kopecks': 2_000_000_000, 'price_max_kopecks': 10_000_000_000,
            'needed_stage': 'Подача заявок', 'days_back': 60, 'max_pages': 10, 'max_tenders': 100}


def _profile(value):
    if not isinstance(value, dict) or set(value) != {'id', 'name', 'filters'}:
        raise ValueError('Некорректный профиль поиска.')
    identifier = value['id']
    if not isinstance(identifier, str) or not re.fullmatch(r'legacy|[0-9a-f]{32}', identifier):
        raise ValueError('Некорректный идентификатор профиля.')
    return {'id': identifier, 'name': _text(value['name'], 'Название профиля', 80),
            'filters': validate_filters(value['filters'])}


def load_profiles(root):
    path = Path(root) / 'search_profiles.json'
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ValueError('Файл профилей слишком большой.')
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'schema_version': 1, 'revision': 0, 'profiles': [
            {'id': 'legacy', 'name': 'Строительство и благоустройство', 'filters': default_filters()}]}
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError('Не удалось прочитать профили поиска; сохранённый файл оставлен без изменений.') from error
    try:
        if not isinstance(data, dict) or set(data) != {'schema_version', 'revision', 'profiles'}:
            raise ValueError('schema')
        if type(data['schema_version']) is not int or data['schema_version'] != 1 or type(data['revision']) is not int or data['revision'] < 0:
            raise ValueError('version')
        if not isinstance(data['profiles'], list) or not 1 <= len(data['profiles']) <= MAX_PROFILES:
            raise ValueError('profiles')
        profiles = [_profile(row) for row in data['profiles']]
        if len({row['id'] for row in profiles}) != len(profiles) or len({row['name'].casefold() for row in profiles}) != len(profiles):
            raise ValueError('duplicates')
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('Профили поиска повреждены; сохранённый файл оставлен без изменений.') from error
    return dict(data, profiles=profiles)


def save_profile(root, value):
    if not isinstance(value, dict) or set(value) != {'revision', 'profile'} or type(value['revision']) is not int:
        raise ValueError('Передайте профиль и номер версии списка.')
    profile = _profile(value['profile'])
    path = Path(root) / 'search_profiles.json'
    with output_lock(path):
        current = load_profiles(root)
        existing = next((row for row in current['profiles'] if row['id'] == profile['id']), None)
        if existing == profile:
            return current  # Retry after a lost response does not create another revision.
        if value['revision'] != current['revision']:
            raise ProfileConflict('Профили уже изменены. Обновите список перед сохранением; введённые условия сохранены в форме.')
        if any(row['id'] != profile['id'] and row['name'].casefold() == profile['name'].casefold() for row in current['profiles']):
            raise ValueError('Профиль с таким названием уже есть. Укажите другое название.')
        if existing is None and len(current['profiles']) >= MAX_PROFILES:
            raise ValueError('Можно сохранить до 20 профилей. Измените один из существующих.')
        updated = copy.deepcopy(current)
        updated['profiles'] = [profile if row['id'] == profile['id'] else row for row in updated['profiles']]
        if existing is None:
            updated['profiles'].append(profile)
        updated['revision'] += 1
        if len(json.dumps(updated, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_BYTES:
            raise ValueError('Список профилей слишком большой. Сократите темы или регионы.')
        atomic_json(path, updated)
        return updated


def filters_for_profile(root, identifier):
    row = next((row for row in load_profiles(root)['profiles'] if row['id'] == identifier), None)
    if row is None:
        raise ValueError('Профиль поиска не найден.')
    return copy.deepcopy(row['filters'])
