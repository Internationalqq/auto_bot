"""Versioned corrections over immutable uploaded estimates, with exact money."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from autobot.upload_admission import operation_key

FIELDS = {'name': 'Наименование', 'basis_code': 'Шифр', 'type': 'Тип позиции', 'unit': 'Единица',
          'qty': 'Количество', 'unit_price': 'Цена за единицу, ₽', 'total': 'Сумма строки, ₽'}
TYPES = {'work': 'Работа', 'service': 'Услуга', 'product': 'Товар/изделие', 'material': 'Материал', 'other': 'Другое'}
ROLES = {'main_admin', 'admin', 'director', 'foreman'}
LIMIT = 16 * 1024 * 1024


class CorrectionError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def encoded(value):
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError) as error:
        raise CorrectionError('В сохранённой смете есть некорректные значения.', 503) from error
    if len(text.encode('utf-8')) > LIMIT:
        raise CorrectionError('Превышен допустимый размер редакции сметы.', 413)
    return text


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def estimate_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F-]{1,40}', value):
        raise CorrectionError('Смета не найдена.', 404)
    return value


def actor_from_user(user):
    if not isinstance(user, dict):
        raise CorrectionError('Войдите в PM.bi, чтобы исправить смету.', 401)
    roles = user.get('roles')
    roles = roles if isinstance(roles, list) else [user.get('role')]
    if not any(isinstance(role, str) and role in ROLES for role in roles):
        raise CorrectionError('У вашей роли нет доступа к исправлению смет.', 403)
    uid = user.get('id')
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise CorrectionError('CRM не подтвердила автора изменения.', 503)
    return {'id': uid, 'name': str(user.get('name') or ('Пользователь #' + str(uid)))[:160]}


@contextmanager
def connection(root, *, write=False):
    path = Path(root) / '.corrections.sqlite3'
    con = None
    try:
        if path.is_symlink():
            raise CorrectionError('Недопустимое хранилище исправлений.', 503)
        if not write and not path.exists():
            yield None
            return
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path if write else path.resolve().as_uri() + '?mode=ro', uri=not write, timeout=20)
        con.row_factory = sqlite3.Row
        if con.execute('PRAGMA user_version').fetchone()[0] not in (0, 1):
            raise CorrectionError('Версия истории исправлений не поддерживается.', 503)
        if write:
            con.execute('PRAGMA synchronous=FULL')
            con.execute('BEGIN IMMEDIATE')
            con.execute('''CREATE TABLE IF NOT EXISTS estimate_revisions (
                estimate_id TEXT NOT NULL, revision INTEGER NOT NULL, version TEXT NOT NULL UNIQUE,
                operation_id TEXT NOT NULL UNIQUE, event_json TEXT NOT NULL,
                PRIMARY KEY(estimate_id, revision))''')
            con.execute('PRAGMA user_version=1')
        else:
            con.execute('BEGIN')
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='estimate_revisions'").fetchone():
                yield None
                return
        yield con
        if write:
            con.commit()
    except (sqlite3.Error, OSError) as error:
        raise CorrectionError('История исправлений временно недоступна. Изменения не подтверждены.', 503) from error
    finally:
        if con is not None:
            con.close()


def event(row):
    if row is None:
        return None
    try:
        value = json.loads(row['event_json'])
        if (not isinstance(value, dict) or value.get('schema_version') != 1
                or value.get('estimate_id') != row['estimate_id'] or value.get('revision') != row['revision']
                or value.get('operation_id') != row['operation_id'] or digest(value) != row['version']
                or not isinstance(value.get('overrides'), dict) or not isinstance(value.get('changes'), dict)):
            raise ValueError('invalid event')
        return dict(value, version=row['version'])
    except (ValueError, TypeError, KeyError) as error:
        raise CorrectionError('Повреждена история исправлений. Прежние значения не заменены.', 503) from error


def latest(con, eid):
    return event(con.execute('SELECT * FROM estimate_revisions WHERE estimate_id=? ORDER BY revision DESC LIMIT 1',
                             (eid,)).fetchone()) if con else None


def _number(value):
    if value in (None, ''):
        return None
    try:
        result = Decimal(str(value).replace(' ', '').replace('\xa0', '').replace(',', '.'))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def money(value):
    number = _number(value)
    return int((number * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP)) if number is not None else None


def display(value):
    return '' if value is None else str(value)


def base_digest(original):
    metadata, rows = original
    return digest({'rows':rows,'source_sha256':metadata.get('source_sha256'),'original_filename':metadata.get('original_filename')})


def verify_source(root, eid, metadata):
    expected = metadata.get('source_sha256')
    if not expected:
        return  # Legacy metadata has no byte digest; the immutable row digest still applies.
    from autobot.paths import REPO_ROOT
    source = Path(str(metadata.get('source_path') or ''))
    if not source.is_absolute():
        source = REPO_ROOT / source
    if (not isinstance(expected,str) or not re.fullmatch(r'[0-9a-f]{64}',expected)
            or source.is_symlink() or not source.is_file() or source.resolve().parent != (Path(root)/eid).resolve()
            or source.stat().st_size > 128*1024*1024):
        raise CorrectionError('Не удалось проверить исходный файл. Исправление не сохранено.',409)
    actual = hashlib.sha256()
    with source.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            actual.update(block)
    if actual.hexdigest() != expected:
        raise CorrectionError('Исходный файл изменился после распознавания. Исправление не сохранено.',409)


def snapshot(root, eid, *, original=None, saved=None, supplied=False):
    from autobot import uploaded_estimates as store
    eid = estimate_id(eid)
    metadata, originals = original if original is not None else store.original_document(root, eid)
    if metadata is None:
        raise CorrectionError('Смета не найдена.', 404)
    base = base_digest((metadata, originals))
    if not supplied:
        with connection(root) as con:
            saved = latest(con, eid)
    if saved and saved['base_digest'] != base:
        raise CorrectionError('Исходная смета изменилась. Нельзя применить прежние исправления.', 409)
    overrides = saved['overrides'] if saved else {}
    keys = [str(row.get('position_id') or '') for row in originals]
    if len(keys) != len(set(keys)) or not set(overrides) <= set(keys):
        raise CorrectionError('Не удалось однозначно сопоставить исправленные позиции.', 503)
    current = [dict(row, **overrides.get(str(row['position_id']), {})) for row in originals]
    metadata = dict(metadata)
    version = saved['version'] if saved else base
    if saved:
        metadata['normalization'] = {'version': version, 'revision': saved['revision'],
            'changed_count': len(overrides), 'updated_at': saved['created_at']}
        amounts = [money(row.get('total')) for row in current]
        missing = sum(amount is None for amount in amounts)
        total = sum(amount or 0 for amount in amounts)
        metadata.update(total_sum=total / 100, total_sum_kopecks=total)
        reconciliation = dict(metadata.get('reconciliation') or {})
        declared = money(reconciliation.get('declared_total'))
        reconciliation.update(signed_position_total=total / 100, signed_position_total_kopecks=total,
            missing_total_count=missing, normalization_revision=saved['revision'],
            unallocated_total=(declared-total)/100 if declared is not None and not missing else None)
        metadata['reconciliation'] = reconciliation
    return {'meta': metadata, 'rows': current, 'original_rows': originals, 'version': version,
            'revision': saved['revision'] if saved else 0, 'saved': saved}


def normalized_changes(changes):
    if not isinstance(changes, dict) or not changes or not set(changes) <= set(FIELDS):
        raise CorrectionError('Переданы неподдерживаемые поля исправления.', 400)
    result = {}
    for name, value in changes.items():
        if name in {'qty', 'unit_price', 'total'}:
            if value is None or value == '':
                result[name] = None
                continue
            number = _number(value)
            places = 6 if name == 'qty' else 2
            if (isinstance(value, (bool, list, dict)) or number is None or number.copy_abs() > Decimal('1000000000000')
                    or number != number.quantize(Decimal(1).scaleb(-places))):
                raise CorrectionError(FIELDS[name] + ': укажите число с точностью ' + str(places) + ' знаков.', 400)
            # Bound even a zero with an enormous exponent before rendering it.
            result[name] = format(number.quantize(Decimal(1).scaleb(-places)).normalize(), 'f')
        else:
            limit = {'name': 2000, 'basis_code': 200, 'unit': 80, 'type': 20}[name]
            if not isinstance(value, str) or len(value) > limit:
                raise CorrectionError(FIELDS[name] + ': значение слишком длинное или имеет неверный формат.', 400)
            result[name] = re.sub(r'\s+', ' ', value).strip()
            if name == 'name' and not result[name]:
                raise CorrectionError('Название позиции не может быть пустым.', 400)
            if name == 'type' and result[name] not in TYPES:
                raise CorrectionError('Выберите тип позиции из списка.', 400)
    return result


def _same(name, before, after):
    return _number(before) == _number(after) if name in {'qty', 'unit_price', 'total'} else display(before) == display(after)


def apply(root, eid, *, position_id, changes, expected_version, operation_id, reason, actor):
    from autobot import uploaded_market as market, uploaded_estimates as store
    eid = estimate_id(eid)
    key = operation_key(operation_id)
    if not operation_id or not isinstance(expected_version, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_version):
        raise CorrectionError('Нужны ключ сохранения и версия открытой сметы.', 400)
    if not isinstance(position_id, str) or not position_id or len(position_id) > 500:
        raise CorrectionError('Не указана исходная позиция.', 400)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise CorrectionError('Укажите причину исправления, до 1000 символов.', 400)
    if not isinstance(actor, dict) or set(actor) != {'id', 'name'} or type(actor['id']) is not int or actor['id'] <= 0:
        raise CorrectionError('Автор изменения не подтверждён.', 403)
    changes = normalized_changes(changes)
    request_fingerprint = digest({'estimate_id': eid, 'position_id': position_id, 'changes': changes,
        'expected_version': expected_version, 'reason': reason.strip(), 'actor_id': actor['id']})
    # Never take the queue's write lock under this source lock.
    with market.source_lock(eid, root), connection(root, write=True) as con:
        original = store.original_document(root, eid)
        current = snapshot(root, eid, original=original, saved=latest(con, eid), supplied=True)
        previous = event(con.execute('SELECT * FROM estimate_revisions WHERE operation_id=?', (key,)).fetchone())
        if previous:
            if previous['request_fingerprint'] != request_fingerprint:
                raise CorrectionError('Этот ключ уже использован для другого исправления.')
            return previous, True
        if current['version'] != expected_version:
            raise CorrectionError('Смета уже исправлена в другой вкладке. Обновите строку и сравните значения.')
        verify_source(root, eid, original[0])
        row = next((row for row in current['rows'] if row['position_id'] == position_id), None)
        if row is None:
            raise CorrectionError('Позиция больше не найдена в смете.', 404)
        changes = {name: value for name, value in changes.items() if not _same(name, row.get(name), value)}
        if not changes:
            raise CorrectionError('Значения не изменились. Исправьте нужное поле перед сохранением.', 400)
        saved = current['saved']
        overrides = dict(saved['overrides']) if saved else {}
        override = dict(overrides.get(position_id) or {})
        override.update(changes)
        if 'type' in changes:
            override['type_label'] = TYPES[changes['type']]
        for field in ('unit_price', 'total'):
            if field in changes:
                override[field + '_kopecks'] = money(changes[field])
        override['estimate_version'] = 'correction:' + digest([expected_version, key, position_id, changes])
        overrides[position_id] = override
        value = {'schema_version': 1, 'estimate_id': eid, 'revision': current['revision'] + 1,
            'operation_id': key, 'request_fingerprint': request_fingerprint, 'parent_version': expected_version,
            'base_digest': base_digest(original), 'position_id': position_id,
            'position_name': changes.get('name',row['name']), 'overrides': overrides,
            'changes': {name: {'before': row.get(name), 'after': value} for name, value in changes.items()},
            'actor': actor, 'reason': reason.strip(), 'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        version = digest(value)
        con.execute('INSERT INTO estimate_revisions VALUES (?,?,?,?,?)', (eid, value['revision'], version, key, encoded(value)))
        return dict(value, version=version), False


def history(root, eid, *, before=None):
    eid = estimate_id(eid)
    if before is not None and (type(before) is not int or before < 1):
        raise CorrectionError('Некорректная страница истории.', 400)
    with connection(root) as con:
        rows = con.execute('SELECT * FROM estimate_revisions WHERE estimate_id=? AND (? IS NULL OR revision<?) ORDER BY revision DESC LIMIT 20',
                           (eid, before, before)).fetchall() if con else []
    return [{key: value for key, value in event(row).items() if key in
             {'revision', 'version', 'position_id', 'position_name', 'changes', 'actor', 'reason', 'created_at'}} for row in rows]
