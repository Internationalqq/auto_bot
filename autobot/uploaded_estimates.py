"""Atomic publication of new uploaded estimates; legacy JSON stays readable."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile


class StoreError(RuntimeError):
    pass


def _path(root):
    return Path(root) / 'estimates.sqlite3'


@contextmanager
def _connection(root, *, write=False):
    path = _path(root)
    connection = None
    try:
        if path.is_symlink():
            raise StoreError('Хранилище смет не может быть символической ссылкой.')
        if not write and not path.exists():
            yield None
            return
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path if write else path.resolve().as_uri() + '?mode=ro',
                                     uri=not write, timeout=20)
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            raise StoreError('Версия хранилища смет не поддерживается.')
        if write:
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('''CREATE TABLE IF NOT EXISTS uploaded_estimates (
                id TEXT PRIMARY KEY, meta_json TEXT NOT NULL, rows_json TEXT NOT NULL,
                source_sha256 TEXT NOT NULL, published_at TEXT NOT NULL)''')
            connection.execute('PRAGMA user_version=1')
        elif not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='uploaded_estimates'").fetchone():
            # A process can stop during first creation, before any publication.
            yield None
            return
        yield connection
        if write:
            connection.commit()
    except (sqlite3.Error, OSError) as error:
        raise StoreError('Не удалось прочитать или сохранить хранилище смет.') from error
    finally:
        if connection is not None:
            connection.close()


def _decode(value, kind):
    try:
        result = json.loads(value)
    except (ValueError, TypeError) as error:
        raise StoreError('Повреждена сохранённая смета.') from error
    if not isinstance(result, kind):
        raise StoreError('Неверный формат сохранённой сметы.')
    return result


def _read(root, estimate_id, column, kind):
    with _connection(root) as connection:
        record = connection.execute(f'SELECT {column} FROM uploaded_estimates WHERE id=?',
                                    (estimate_id,)).fetchone() if connection else None
    return _decode(record[0], kind) if record else None


def meta(root, estimate_id):
    return _read(root, estimate_id, 'meta_json', dict)


def rows(root, estimate_id):
    return _read(root, estimate_id, 'rows_json', list)


def _legacy(root, estimate_id, name, kind, default):
    folder = Path(root) / estimate_id
    path = folder / name
    if folder.is_symlink() or path.is_symlink() or folder.resolve().parent != Path(root).resolve():
        raise StoreError('Недопустимая папка сметы.')
    if not path.is_file():
        return default
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise StoreError('Сохранённая смета превышает допустимый размер.')
        return _decode(path.read_text(encoding='utf-8'), kind)
    except OSError as error:
        raise StoreError('Не удалось прочитать исходную смету.') from error


def original_document(root, estimate_id):
    with _connection(root) as connection:
        record = connection.execute('SELECT meta_json,rows_json FROM uploaded_estimates WHERE id=?',
                                    (estimate_id,)).fetchone() if connection else None
    if record:
        metadata, value = _decode(record[0], dict), _decode(record[1], list)
    else:
        metadata = _legacy(root, estimate_id, 'meta.json', dict, None)
        value = _legacy(root, estimate_id, 'rows.json', list, [])
    if any(not isinstance(row, dict) for row in value):
        raise StoreError('Повреждены позиции сохранённой сметы.')
    # Old uploads did not persist physical IDs. The original ordinal is stable;
    # never derive identity from a title or from a filtered table's row number.
    return metadata, [dict(row, position_id=row.get('position_id') or f'upload:{estimate_id}:{index}')
                      for index, row in enumerate(value, 1)]


def load_original_meta(root, estimate_id):
    value = meta(root, estimate_id)
    return value if value is not None else _legacy(root, estimate_id, 'meta.json', dict, None)


def load_document(root, estimate_id):
    from autobot import uploaded_corrections as corrections
    original = original_document(root, estimate_id)
    if original[0] is None:
        return original
    with corrections.connection(root) as con:
        saved = corrections.latest(con, estimate_id)
    if saved is None:
        return original
    current = corrections.snapshot(root, estimate_id, original=original, saved=saved, supplied=True)
    return current['meta'], current['rows']


def load_meta(root, estimate_id):
    return load_document(root, estimate_id)[0]


def load_rows(root, estimate_id):
    return load_document(root, estimate_id)[1]


def report_frame(positions):
    import math
    import pandas as pd
    from autobot.market_analytics import COL_ITEM, COL_NAME, COL_QTY, COL_SUM, COL_UNIT, COL_UNIT_PRICE
    def number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError):
            return None
    return pd.DataFrame([{
        COL_ITEM: str(row.get('item_no') or ''), COL_NAME: str(row.get('name') or ''),
        COL_UNIT: str(row.get('unit') or ''), COL_QTY: number(row.get('qty')),
        COL_UNIT_PRICE: number(row.get('unit_price')), COL_SUM: number(row.get('total')),
        'Лист': str(row.get('sheet') or ''), 'basis_code': str(row.get('basis_code') or ''),
        'position_id': str(row.get('position_id') or ''), 'estimate_version': str(row.get('estimate_version') or ''),
        'Строка Excel': row.get('excel_row'), 'Раздел': str(row.get('section') or ''),
        'Тип': str(row.get('type_label') or '')} for row in positions])


def catalogue(root):
    with _connection(root) as connection:
        records = connection.execute('SELECT meta_json FROM uploaded_estimates ORDER BY published_at DESC, rowid DESC').fetchall() if connection else []
    return [_decode(record[0], dict) for record in records]


def publish(root, metadata, positions):
    estimate_id = str(metadata.get('id') or '')
    source_sha = str(metadata.get('source_sha256') or '')
    if (not re.fullmatch(r'[0-9a-f]{16,40}', estimate_id)
            or not re.fullmatch(r'[0-9a-f]{64}', source_sha)
            or not positions or metadata.get('row_count') != len(positions)):
        raise StoreError('Нет целой проверенной сметы для сохранения.')
    try:
        encoded_meta = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
        encoded_rows = json.dumps(positions, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as error:
        raise StoreError('Смета содержит значения, которые нельзя сохранить.') from error
    if len((encoded_meta + encoded_rows).encode('utf-8')) > 16 * 1024 * 1024:
        raise StoreError('Смета превышает допустимый размер результата.')
    with _connection(root, write=True) as connection:
        previous = connection.execute('SELECT meta_json, rows_json, source_sha256 FROM uploaded_estimates WHERE id=?', (estimate_id,)).fetchone()
        if previous:
            previous_meta = _decode(previous[0], dict)
            if previous[1] != encoded_rows or previous[2] != source_sha or any(previous_meta.get(key) != value for key, value in metadata.items()):
                raise StoreError('Под этим номером уже сохранена другая версия сметы.')
            return False
        connection.execute('INSERT INTO uploaded_estimates VALUES (?,?,?,?,?)',
            (estimate_id, encoded_meta, encoded_rows, source_sha, datetime.now(timezone.utc).isoformat()))
    return True


def update_market_meta(root, estimate_id, changes):
    allowed = {'market_city', 'market_sources', 'market_selected_types', 'market_updated_at'}
    if not changes.keys() <= allowed:
        raise StoreError('Нельзя изменить исходную смету через результат поиска.')
    if not _path(root).exists():
        return False
    with _connection(root, write=True) as connection:
        record = connection.execute('SELECT meta_json FROM uploaded_estimates WHERE id=?', (estimate_id,)).fetchone()
        if not record:
            return False
        value = _decode(record[0], dict)
        value.update(changes)
        connection.execute('UPDATE uploaded_estimates SET meta_json=? WHERE id=?',
                           (json.dumps(value, ensure_ascii=False, allow_nan=False), estimate_id))
    return True


def remove(root, estimate_id):
    if not _path(root).exists():
        return False
    with _connection(root, write=True) as connection:
        return connection.execute('DELETE FROM uploaded_estimates WHERE id=?', (estimate_id,)).rowcount > 0


def export_legacy_snapshot(root, destination):
    """Prepare a new rollback artifact; never write back into live estimate data."""
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if destination == root or root in destination.parents or destination.exists():
        raise StoreError('Для выгрузки нужен новый каталог вне рабочих данных смет.')
    with _connection(root) as connection:
        records = connection.execute('SELECT id, meta_json, rows_json FROM uploaded_estimates ORDER BY published_at DESC, rowid DESC').fetchall() if connection else []
    legacy_path = root / 'index.json'
    legacy = _decode(legacy_path.read_text(encoding='utf-8'), list) if legacy_path.exists() else []
    documents = []
    for estimate_id, encoded_meta, encoded_rows in records:
        metadata, positions = _decode(encoded_meta, dict), _decode(encoded_rows, list)
        if not re.fullmatch(r'[0-9a-f]{16,40}', estimate_id) or metadata.get('id') != estimate_id:
            raise StoreError('Некорректный номер сохранённой сметы.')
        documents.append((estimate_id, metadata, positions))
    if any(not isinstance(item, dict) for item in legacy):
        raise StoreError('Повреждён прежний каталог смет.')
    ids = {item[0] for item in documents}
    catalogue_items = [item[1] for item in documents] + [item for item in legacy if item.get('id') not in ids]
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    for estimate_id, metadata, positions in documents:
        write_json(destination / estimate_id / 'meta.json', metadata)
        write_json(destination / estimate_id / 'rows.json', positions)
    write_json(destination / 'index.json', catalogue_items)
    return len(documents)


def write_json(path, value):
    """Unique temporary file, atomic replacement, durable bytes on Linux."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix='.upload-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        if os.name != 'nt':
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        Path(name).unlink(missing_ok=True)
