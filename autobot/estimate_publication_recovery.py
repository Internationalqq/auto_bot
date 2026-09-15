"""Recover an interrupted report publication without consuming previous copies.

The report's existing output lock serializes publication, recovery and readers.
Only journals written by this version are recovered; legacy scratch is untouched.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil

from autobot.atomic_output import output_lock
from autobot.document_bundle import bundle_path
from autobot.estimate_parse_worker import EstimateParseRejected
from autobot.tender_search_state import atomic_json

MAX_FILE_BYTES = 512 * 1024 * 1024
_held = ContextVar('autobot_consistent_reports', default=frozenset())


class PublicationRecoveryRequired(EstimateParseRejected):
    pass


def publication_path(reports, tender_id):
    return bundle_path(reports, tender_id).with_name(f'PUBLICATION_{tender_id}.json')


def output_names(tender_id):
    return [f'ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.html', f'ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.xlsx',
            f'ESTIMATE_PARSE_{tender_id}.json', f'PARSE_RUN_{tender_id}.json']


def _sha(path):
    if path.is_symlink():
        raise PublicationRecoveryRequired('Файл публикации оказался ссылкой. Автоматическое восстановление остановлено.')
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise PublicationRecoveryRequired('Не удалось проверить размер файла публикации: ' + path.name)
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _sync_file(path):
    # Windows FlushFileBuffers requires a handle opened for writing.
    with path.open('r+b') as stream:
        os.fsync(stream.fileno())


def _sync_directory(path):
    # Windows does not expose a directory fsync through Python's file API.
    if os.name == 'nt':
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _save(path, data):
    atomic_json(path, data)
    _sync_directory(path.parent)


def _stage(reports, name):
    if not isinstance(name, str) or not re.fullmatch(r'\.autobot-parse-[A-Za-z0-9_-]{6,80}', name):
        raise PublicationRecoveryRequired('Повреждён адрес резервной копии публикации.')
    stage = reports / name
    if stage.is_symlink() or stage.resolve().parent != reports.resolve():
        raise PublicationRecoveryRequired('Резервная копия находится вне каталога отчётов.')
    return stage


def _load(reports, tender_id):
    marker = publication_path(reports, tender_id)
    if not marker.exists() and not marker.is_symlink():
        return None
    try:
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 32 * 1024:
            raise ValueError('journal size')
        data = json.loads(marker.read_text(encoding='utf-8'))
        rows = data['files']
        if (data['schema_version'] != 1 or data['tender_id'] != str(tender_id)
                or data['phase'] not in {'prepared', 'committed', 'rolled_back'}
                or not re.fullmatch(r'[a-f0-9]{32}', data['run_id'])
                or not isinstance(rows, list) or [row['name'] for row in rows] != output_names(tender_id)):
            raise ValueError('journal identity')
        for row in rows:
            if (not re.fullmatch(r'[a-f0-9]{64}', row['new_sha256'])
                    or (row['old_sha256'] is not None and not re.fullmatch(r'[a-f0-9]{64}', row['old_sha256']))):
                raise ValueError('journal digest')
        _stage(reports, data['stage_dir'])
        return data
    except (ValueError, TypeError, KeyError, OSError):
        raise PublicationRecoveryRequired('Не удалось прочитать журнал публикации. Файлы и резервные копии сохранены для проверки.') from None


def _owned_failed_status(path, data):
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        if (value.get('schema_version') == 1 and value.get('tender_id') == data['tender_id']
                and value.get('run_id') == data['run_id'] and value.get('state') == 'failed'):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _cleanup(reports, data):
    stage = _stage(reports, data['stage_dir'])
    if stage.exists():
        allowed = set(output_names(data['tender_id']))
        allowed |= {name + '.lock' for name in allowed}
        previous = stage / 'previous'
        # Validate the entire private directory before removing even one file.
        for child in stage.iterdir():
            if child.is_symlink() or child.resolve().parent != stage.resolve():
                raise PublicationRecoveryRequired('Неожиданная ссылка в резервной копии. Копия сохранена.')
            if child.name != 'previous' and (not child.is_file() or child.name not in allowed):
                raise PublicationRecoveryRequired('Неизвестный файл в резервной копии. Копия сохранена.')
        if previous.exists():
            if not previous.is_dir():
                raise PublicationRecoveryRequired('Повреждён каталог резервной копии.')
            for child in previous.iterdir():
                if (not child.is_file() or child.is_symlink() or child.resolve().parent != previous.resolve()
                        or child.name not in allowed | {'.restore-' + name for name in allowed}):
                    raise PublicationRecoveryRequired('Неизвестный файл в резервной копии. Копия сохранена.')
            for child in previous.iterdir():
                child.unlink()
            previous.rmdir()
        for child in stage.iterdir():
            child.unlink()
        stage.rmdir()
    marker = publication_path(reports, data['tender_id'])
    marker.unlink(missing_ok=True)
    try:
        _sync_directory(reports)
    except OSError:
        # Keep a completed phase if cleanup itself cannot be made durable.
        atomic_json(marker, data)
        raise


def _restore_copy(backup, target):
    temporary = backup.with_name('.restore-' + backup.name)
    if temporary.is_symlink():
        raise PublicationRecoveryRequired('Временный файл восстановления оказался ссылкой.')
    try:
        shutil.copyfile(backup, temporary)
        _sync_file(temporary)
        os.replace(temporary, target)
        _sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def recover_locked(reports, tender_id):
    """Caller holds the authoritative XLSX output lock."""
    reports = Path(reports)
    data = _load(reports, tender_id)
    if data is None:
        return False
    if data['phase'] in {'committed', 'rolled_back'}:
        _cleanup(reports, data)
        return True
    stage = _stage(reports, data['stage_dir'])
    previous = stage / 'previous'
    if not stage.is_dir() or not previous.is_dir() or previous.is_symlink() or previous.resolve().parent != stage.resolve():
        raise PublicationRecoveryRequired('Не найдена целая резервная копия прерванной публикации. Отчёт требует проверки.')
    journal_name = output_names(tender_id)[-1]
    failed = _owned_failed_status(reports / journal_name, data)
    # All preconditions are checked before a rollback changes any target.
    for row in data['files']:
        target = reports / row['name']
        current = _sha(target)
        if current not in {row['old_sha256'], row['new_sha256']} and not (row['name'] == journal_name and failed):
            raise PublicationRecoveryRequired('Файл отчёта изменён после сбоя. Автоматическое восстановление остановлено: ' + row['name'])
        if row['old_sha256'] is not None and _sha(previous / row['name']) != row['old_sha256']:
            raise PublicationRecoveryRequired('Повреждена резервная копия отчёта: ' + row['name'])
    for row in data['files']:
        if row['name'] == journal_name:
            continue
        target = reports / row['name']
        current = _sha(target)
        if current == row['old_sha256']:
            continue
        if current != row['new_sha256']:
            raise PublicationRecoveryRequired('Файл отчёта изменился во время восстановления: ' + row['name'])
        if row['old_sha256'] is None:
            target.unlink(missing_ok=True)
            _sync_directory(reports)
        else:
            _restore_copy(previous / row['name'], target)
    if not failed or failed.get('publication_recovered') != data['run_id']:
        _save(reports / journal_name, {
            'schema_version': 1, 'tender_id': str(tender_id), 'run_id': data['run_id'], 'state': 'failed',
            'warnings': [], 'publication_recovered': data['run_id'], 'finished_at': datetime.now(timezone.utc).isoformat(),
            'error': 'Прерванная публикация отменена; согласованный предыдущий отчёт восстановлен, если он существовал. Повторите разбор документов.',
        })
    data['phase'] = 'rolled_back'
    _save(publication_path(reports, tender_id), data)
    _cleanup(reports, data)
    return True


@contextmanager
def consistent_report(reports, tender_id, *, timeout=.2):
    reports = Path(reports)
    tender_id = str(tender_id)
    # Legacy CLI reports may have short/custom IDs. They predate this publisher
    # and have no recoverable journal, but still need the same reader lock.
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', tender_id):
        raise ValueError('Некорректный номер отчёта.')
    key = str((reports / output_names(tender_id)[1]).resolve())
    if key in _held.get():
        yield
        return
    with output_lock(reports / output_names(tender_id)[1], timeout=timeout):
        token = _held.set(_held.get() | {key})
        try:
            if re.fullmatch(r'[0-9]{8,25}', tender_id):
                recover_locked(reports, tender_id)
            yield
        finally:
            _held.reset(token)


def recover_publication(reports, tender_id, *, timeout=.2):
    marker = publication_path(reports, tender_id)
    if not marker.exists() and not marker.is_symlink():
        return
    with consistent_report(reports, tender_id, timeout=timeout):
        pass


def recover_pending_publications(reports):
    """Run before workers start; one blocked tender must not stop other work."""
    recovered, blocked = [], []
    for path in sorted(Path(reports).glob('PUBLICATION_*.json')):
        match = re.fullmatch(r'PUBLICATION_([0-9]{8,25})\.json', path.name)
        if not match:
            continue
        tid = match[1]
        try:
            recover_publication(reports, tid)
            recovered.append(tid)
        except (EstimateParseRejected, OSError, TimeoutError) as error:
            blocked.append(tid)
            logging.getLogger(__name__).warning('Publication recovery blocked tender=%s: %s', tid, error)
    return {'recovered': recovered, 'blocked': blocked}


def activate(staged, destination, staging_root):
    """Caller holds the report lock; commit a recoverable, fixed four-file set."""
    destination, staging_root = Path(destination), Path(staging_root)
    match = re.fullmatch(r'PARSE_RUN_(\d{8,25})\.json', staged[-1].name)
    if not match:
        raise ValueError('Не определён тендер публикации.')
    tender_id = match[1]
    if [path.name for path in staged] != output_names(tender_id):
        raise ValueError('Неполный набор файлов публикации.')
    if _stage(destination, staging_root.name).resolve() != staging_root.resolve():
        raise ValueError('Каталог публикации находится вне отчётов.')
    if any(path.is_symlink() or path.resolve().parent != staging_root.resolve() for path in staged):
        raise ValueError('Файлы публикации находятся вне её каталога.')
    recover_locked(destination, tender_id)
    completed = json.loads(staged[-1].read_text(encoding='utf-8'))
    run_id = completed['run_id']
    if not re.fullmatch(r'[a-f0-9]{32}', run_id):
        raise ValueError('Не определён запуск публикации.')
    backups = staging_root / 'previous'
    backups.mkdir()
    rows = []
    for path in staged:
        target = destination / path.name
        old_sha = _sha(target)
        if old_sha is not None:
            shutil.copyfile(target, backups / path.name)
            _sync_file(backups / path.name)
            if _sha(backups / path.name) != old_sha or _sha(target) != old_sha:
                raise ValueError('Отчёт изменился во время создания резервной копии.')
        _sync_file(path)
        rows.append({'name': path.name, 'old_sha256': old_sha, 'new_sha256': _sha(path)})
    _sync_directory(backups)
    _sync_directory(staging_root)
    data = {'schema_version': 1, 'tender_id': tender_id, 'run_id': run_id,
            'stage_dir': staging_root.name, 'phase': 'prepared', 'files': rows}
    marker = publication_path(destination, tender_id)
    try:
        _save(marker, data)
        for path in staged:
            os.replace(path, destination / path.name)
            _sync_directory(destination)
        data['phase'] = 'committed'
        _save(marker, data)
    except Exception:
        try:
            saved = _load(destination, tender_id)
            recover_locked(destination, tender_id)
        except Exception:
            raise PublicationRecoveryRequired('Ошибка сохранения и восстановления отчёта. Резервная копия оставлена для восстановления: ' + staging_root.name) from None
        if saved and saved['phase'] == 'committed':
            # The commit rename succeeded but a later sync reported failure.
            # Recovery has now synced/cleaned it: keep the completed generation.
            return
        raise
    try:
        _cleanup(destination, data)
    except Exception:
        raise PublicationRecoveryRequired('Отчёт сохранён; очистка резервной копии прервана. Копия оставлена для восстановления: ' + staging_root.name) from None
