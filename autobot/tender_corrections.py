"""Publish a tender correction and its history through the existing four-file journal."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid
import zipfile

import pandas as pd

from autobot.atomic_output import output_lock
from autobot.document_bundle import bundle_path, current_files
from autobot.estimate_parse_worker import EstimateParseRejected, validate_snapshot
from autobot.estimate_publication_recovery import consistent_report, output_names, activate
from autobot.market_contract import clean, position_identity, review_position_identity
from autobot.tender_search_state import atomic_json
from autobot.upload_admission import operation_key
from autobot import uploaded_corrections as common

CorrectionError = common.CorrectionError
FIELDS = {name: label for name, label in common.FIELDS.items() if name != 'type'}
COLUMNS = {'name': 'Название работы/услуги', 'basis_code': 'basis_code', 'unit': 'Ед. изм.',
           'qty': 'Кол-во', 'unit_price': 'Цена за ед., руб', 'total': 'Сумма, руб'}
COORDINATES = {'source_file': 'Файл ЛСР', 'sheet': 'Лист', 'excel_row': 'Строка Excel',
               'section': 'Раздел', 'item_no': '№ п/п'}
MAX_ROWS = 20000
MAX_EVENTS = 1000
LEDGER_KEY = 'tender_corrections'


def tender_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{8,25}', value):
        raise CorrectionError('Тендер не найден.', 404)
    return value


def _decimal(value):
    number = common._number(value)
    return format(number.normalize(), 'f') if number is not None else None


def rows(frame):
    result, used = [], set()
    for record in frame.to_dict('records'):
        key = review_position_identity(record)
        if key in used:
            raise CorrectionError('В отчёте есть неразличимые строки. Повторите разбор исходной сметы.', 409)
        used.add(key)
        item = {name: _decimal(record.get(column)) if name in {'qty', 'unit_price', 'total'}
                else clean(record.get(column)) for name, column in COLUMNS.items()}
        item.update({name: (_decimal(record.get(column)) or clean(record.get(column)))
                     if name in {'excel_row', 'item_no'} else clean(record.get(column))
                     for name, column in COORDINATES.items()})
        item.update(position_id=key, estimate_version=clean(record.get('estimate_version')))
        result.append(item)
    return result


def _read_json(path):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > common.LIMIT:
            raise ValueError('size or missing')
        result = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(result, dict):
            raise ValueError('object required')
        return result
    except (OSError, ValueError) as error:
        raise CorrectionError('Не удалось прочитать контроль разбора. Прежняя смета сохранена.', 503) from error


def _read_frame(path):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('report size')
        with zipfile.ZipFile(path) as book:
            members = book.infolist()
            if len(members) > 4096 or sum(item.file_size for item in members) > 128 * 1024 * 1024:
                raise ValueError('unpacked size')
        frame = pd.read_excel(path, dtype=object, nrows=MAX_ROWS + 1)
        if len(frame) > MAX_ROWS or frame.size > 500000 or not set(COLUMNS.values()) <= set(frame.columns):
            raise ValueError('report shape')
        return frame
    except Exception as error:
        raise CorrectionError('Не удалось прочитать сметные позиции. Прежняя смета сохранена.', 503) from error


def _source_digest(manifest):
    sources = manifest.get('parse_sources')
    if (not isinstance(sources, list) or not sources or len(sources) > 300
            or any(not isinstance(row, dict) or set(row) != {'path', 'size', 'sha256'}
                   or not isinstance(row['path'], str) or type(row['size']) is not int or row['size'] < 0
                   or not isinstance(row['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', row['sha256'])
                   for row in sources)):
        raise CorrectionError('У прежнего отчёта нет проверенного состава исходников. Сначала выполните разбор документов.', 409)
    return common.digest(sources)


def _checked_ledger(manifest, current):
    ledger = manifest.get(LEDGER_KEY)
    if ledger is None:
        if any(row['estimate_version'].startswith('correction:') for row in current):
            raise CorrectionError('В исправленной смете отсутствует история. Прежние значения сохранены.', 503)
        return None
    try:
        if (not isinstance(ledger, dict) or ledger.get('schema_version') != 1
                or ledger['checksum'] != common.digest({k: v for k, v in ledger.items() if k != 'checksum'})
                or ledger['source_digest'] != _source_digest(manifest)
                or ledger['current_digest'] != common.digest(current)
                or not isinstance(ledger['events'], list) or not 1 <= len(ledger['events']) <= MAX_EVENTS
                or set(ledger['originals']) != set(ledger['overrides'])
                or not set(ledger['overrides']) <= {row['position_id'] for row in current}):
            raise ValueError('ledger identity')
        parent = ledger['base_version']
        operations = set()
        for revision, event in enumerate(ledger['events'], 1):
            if (event['version'] != common.digest({k: v for k, v in event.items() if k != 'version'})
                    or event['revision'] != revision or event['parent_version'] != parent
                    or event['operation_id'] in operations or event['tender_id'] != manifest['tender_id']):
                raise ValueError('event chain')
            operations.add(event['operation_id'])
            parent = event['version']
        return ledger
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise CorrectionError('Повреждена история исправлений. Автоматическая замена сметы остановлена.', 503) from error


def snapshot_locked(reports, tid):
    """Caller holds consistent_report; no browser-supplied paths are accepted."""
    names = output_names(tender_id(tid))
    report = Path(reports) / names[1]
    manifest = _read_json(Path(reports) / names[2])
    if manifest.get('tender_id') != tid:
        raise CorrectionError('Контроль разбора относится к другому тендеру.', 503)
    source_digest = _source_digest(manifest)
    frame = _read_frame(report)
    current = rows(frame)
    ledger = _checked_ledger(manifest, current)
    base = common.digest({'rows': current, 'sources': source_digest})
    return {'frame': frame, 'rows': current, 'manifest': manifest, 'ledger': ledger,
            'version': ledger['events'][-1]['version'] if ledger else base,
            'revision': len(ledger['events']) if ledger else 0,
            'original_rows': [deepcopy(ledger['originals'].get(row['position_id'], row)) if ledger else dict(row)
                              for row in current]}


def snapshot(reports, tid):
    with consistent_report(reports, tender_id(tid)):
        return snapshot_locked(reports, tid)


def history(value, before=None):
    if before is not None and (type(before) is not int or before < 1):
        raise CorrectionError('Некорректная страница истории.', 400)
    events = value['ledger']['events'] if value['ledger'] else []
    return [{key: item[key] for key in ('revision', 'version', 'position_id', 'position_name', 'changes', 'actor', 'reason', 'created_at')}
            for item in reversed(events) if before is None or item['revision'] < before][:20]


def receipt(value, key, actor):
    key = operation_key(key)
    for event in (value['ledger']['events'] if value['ledger'] else []):
        if event['operation_id'] == key and event['actor']['id'] == actor['id']:
            return event
    return None


def _seal(ledger, frame):
    ledger['current_digest'] = common.digest(rows(frame))
    ledger['checksum'] = common.digest({k: v for k, v in ledger.items() if k != 'checksum'})
    common.encoded(ledger)
    return ledger


def _overridden(frame, overrides):
    frame = frame.copy().astype(object)
    for index, row in zip(frame.index, rows(frame)):
        changes = overrides.get(row['position_id'])
        if not changes:
            continue
        for name, value in changes.items():
            column = COLUMNS.get(name, name)
            if column not in frame.columns:
                frame[column] = None
            frame.at[index, column] = value
        if 'position_id' not in frame.columns:
            frame['position_id'] = None
        # Keep physical PDF IDs used by nested resources; the review ID adds
        # the document namespace without changing those source coordinates.
        frame.at[index, 'position_id'] = clean(frame.at[index, 'position_id']) or row['position_id']
        # The legacy display column must follow an explicitly edited unit/quantity.
        qty, unit = frame.at[index, COLUMNS['qty']], clean(frame.at[index, COLUMNS['unit']])
        frame.at[index, 'Объем'] = (str(qty) + ' ' + unit).strip() if _decimal(qty) is not None else ''
    return frame


def write_frame(path, frame):
    """Keep Excel amounts numeric and source text literal, then prove the round trip."""
    output = frame.copy()
    for name in ('qty','unit_price','total'):
        output[COLUMNS[name]] = output[COLUMNS[name]].map(
            lambda value: float(number) if (number := common._number(value)) is not None else None)
    with pd.ExcelWriter(path,engine='openpyxl') as writer:
        output.to_excel(writer,index=False)
        sheet = writer.sheets['Sheet1']
        for r, record in enumerate(output.itertuples(index=False,name=None),2):
            for c, value in enumerate(record,1):
                if isinstance(value,str):
                    sheet.cell(r,c).data_type='s'
    written = _read_frame(path)
    if rows(written) != rows(frame):
        raise CorrectionError('Число превышает точность Excel. Исправление не опубликовано; уменьшите число значащих цифр.',400)
    return written


def carry_forward(previous, new_manifest, frame):
    """Reparse can retain corrections only over the identical proven base."""
    if not previous or not previous['ledger']:
        return frame
    ledger = deepcopy(previous['ledger'])
    if (ledger['source_digest'] != _source_digest(new_manifest)
            or ledger['base_frame_digest'] != common.digest(rows(frame))):
        raise CorrectionError('Новый разбор отличается от основы ручных исправлений. Прежняя смета и история сохранены; нужна сверка строк.', 409)
    result = _overridden(frame, ledger['overrides'])
    new_manifest[LEDGER_KEY] = _seal(ledger, result)
    return result


def _validate_sources(out_paths, tid, manifest):
    expected = manifest['parse_sources']
    try:
        # Also validate the archive/download bundle that supplied extracted documents.
        current_files(out_paths['downloads'], tid)
        roots = [(Path(out_paths[name]) / tid).resolve() for name in ('downloads', 'extracted')]
        if any(not any(Path(row['path']).resolve().is_relative_to(root) for root in roots) for row in expected):
            raise CorrectionError('Исходник находится вне комплекта этого тендера.', 409)
        validate_snapshot(expected)
    except (EstimateParseRejected, ValueError) as error:
        if isinstance(error, CorrectionError):
            raise
        raise CorrectionError(str(error), 409) from error


def current_warnings(warnings, frame):
    suffix = 'не определены количество, единица или сумма части позиций; проверьте исходную смету.'
    result = [message for message in warnings if not str(message).endswith(suffix)]
    incomplete = sorted({row['source_file'] for row in rows(frame)
                         if row['qty'] is None or row['total'] is None or not row['unit']})
    return result + [Path(source).name + ': ' + suffix for source in incomplete]


def apply(tender, out_paths, *, position_id, changes, expected_version, operation_id, reason, actor):
    tid = tender_id(str(tender.tender_id))
    key = operation_key(operation_id)
    if (not key or not isinstance(changes, dict) or not set(changes) <= set(FIELDS)
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000
            or not isinstance(position_id, str) or not 1 <= len(position_id) <= 500
            or not isinstance(expected_version, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_version)):
        raise CorrectionError('Укажите позицию, причину, значения и текущую редакцию.', 400)
    changes = common.normalized_changes(changes)
    if (not isinstance(actor, dict) or type(actor.get('id')) is not int or actor['id'] <= 0
            or not isinstance(actor.get('name'), str)):
        raise CorrectionError('CRM не подтвердила автора изменения.', 503)
    fingerprint = common.digest({'tender_id': tid, 'actor_id': actor['id'], 'position_id': position_id,
                                 'changes': changes, 'expected_version': expected_version, 'reason': reason.strip()})
    reports = Path(out_paths['reports'])
    from autobot import main
    from autobot.estimate_publication import _staging
    with output_lock(bundle_path(reports, tid), timeout=.2), consistent_report(reports, tid):
        value = snapshot_locked(reports, tid)
        for event in (value['ledger']['events'] if value['ledger'] else []):
            if event['operation_id'] == key:
                if event['request_fingerprint'] != fingerprint:
                    raise CorrectionError('Этот ключ уже использован для другого исправления.', 409)
                return event, True
        if value['version'] != expected_version:
            raise CorrectionError('Смета уже исправлена в другой вкладке. Загрузите текущую строку и сравните значения.', 409)
        _validate_sources(out_paths, tid, value['manifest'])
        row = next((row for row in value['rows'] if row['position_id'] == position_id), None)
        if row is None:
            raise CorrectionError('Позиция больше не найдена в смете.', 404)
        changes = {name: v for name, v in changes.items() if not common._same(name, row.get(name), v)}
        if not changes:
            raise CorrectionError('Значения не изменились. Исправьте нужное поле перед сохранением.', 400)
        ledger = deepcopy(value['ledger']) if value['ledger'] else {
            'schema_version': 1, 'base_version': value['version'], 'base_frame_digest': common.digest(value['rows']),
            'source_digest': _source_digest(value['manifest']), 'originals': {}, 'overrides': {}, 'events': []}
        if len(ledger['events']) >= MAX_EVENTS:
            raise CorrectionError('Достигнут предел истории этой сметы. Сохранения не удаляются; требуется отдельный перенос истории.', 413)
        event = {'schema_version': 1, 'tender_id': tid, 'revision': value['revision'] + 1,
                 'parent_version': expected_version, 'operation_id': key, 'request_fingerprint': fingerprint,
                 'position_id': position_id, 'position_name': changes.get('name', row['name']),
                 'changes': {name: {'before': row.get(name), 'after': v} for name, v in changes.items()},
                 'actor': {'id': actor['id'], 'name': actor['name'][:160]}, 'reason': reason.strip(),
                 'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        event['version'] = common.digest(event)
        ledger['events'].append(event)
        ledger['originals'].setdefault(position_id, row)
        override = ledger['overrides'].setdefault(position_id, {})
        override.update(changes, estimate_version='correction:' + event['version'])
        for name in ('unit_price', 'total'):
            if name in changes:
                override[name + '_kopecks'] = common.money(changes[name])
        frame = _overridden(value['frame'], ledger['overrides'])
        manifest = deepcopy(value['manifest'])
        manifest[LEDGER_KEY] = _seal(ledger, frame)
        # The full control document, not just its ledger, must stay within the read limit.
        common.encoded(manifest)
        state = _read_json(reports / output_names(tid)[3])
        if state.get('state') == 'running':
            raise CorrectionError('Разбор ещё выполняется. Дождитесь его завершения.', 409)
        state.update(run_id=uuid.uuid4().hex, state='complete', normalization_revision=event['revision'],
                     finished_at=event['created_at'], error='', warnings=current_warnings(state.get('warnings',[]),frame))
        with _staging(reports) as staging:
            names = output_names(tid)
            write_frame(staging / names[1], frame)
            html_frame = frame.copy()
            for name in ('qty', 'unit_price', 'total'):
                html_frame[COLUMNS[name]] = pd.to_numeric(html_frame[COLUMNS[name]], errors='coerce')
            main.write_tender_estimate_html(tender, html_frame, {**out_paths, 'reports': staging})
            atomic_json(staging / names[2], manifest)
            atomic_json(staging / names[3], state)
            _validate_sources(out_paths, tid, manifest)
            activate([staging / name for name in names], reports, staging)
        return event, False
