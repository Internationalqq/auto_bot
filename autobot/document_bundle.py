"""Current EIS document set, independent from old files retained on disk."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
import uuid

from autobot.atomic_output import output_lock
from autobot.source_file_versions import sha256_file, store_downloaded_source_file
from autobot.tender_search_state import atomic_json

MAX_FILES = 100
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_SECONDS = 600


class DocumentBundleRejected(ValueError):
    pass


def bundle_path(reports, tender_id):
    if not re.fullmatch(r'\d{8,25}', str(tender_id)):
        raise DocumentBundleRejected('Некорректный номер тендера.')
    return Path(reports) / f'DOCUMENTS_{tender_id}.json'


def read_bundle(reports, tender_id):
    path = bundle_path(reports, tender_id)
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('large manifest')
        payload = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(payload, dict) or payload.get('schema_version') != 1 or payload.get('tender_id') != str(tender_id)
            or payload.get('state') not in {'loading', 'complete', 'failed'}
            or not isinstance(payload.get('files'), list) or len(payload['files']) > MAX_FILES
            or not all(isinstance(row, dict) for row in payload['files'])
            or not isinstance(payload.get('errors', []), list)):
            raise ValueError('invalid manifest')
        return payload
    except (OSError, ValueError):
        raise DocumentBundleRejected('Не удалось прочитать состав документов. Повторите загрузку из ЕИС.') from None


def current_files(downloads_dir, tender_id):
    folder = Path(downloads_dir) / str(tender_id)
    payload = read_bundle(Path(downloads_dir).parent / 'reports', tender_id)
    if payload is None:
        return [path for path in folder.iterdir() if path.is_file() and not path.is_symlink()
                and not path.name.startswith('.autobot-') and path.name not in {'download_log.json', 'desktop.ini'}] if folder.is_dir() else []
    if payload.get('state') != 'complete':
        raise DocumentBundleRejected('Загрузка комплекта не завершена. Сначала повторите скачивание из ЕИС; прежний отчёт сохранён.')
    rows = payload.get('files')
    if not isinstance(rows, list) or not rows or len(rows) > MAX_FILES:
        raise DocumentBundleRejected('В журнале нет проверенного комплекта документов. Повторите загрузку.')
    files, names = [], set()
    for row in rows:
        name = row.get('saved_name') if isinstance(row, dict) else None
        if not isinstance(name, str) or Path(name).name != name or '/' in name or '\\' in name or name.startswith('.') or name in names:
            raise DocumentBundleRejected('Некорректный состав документов. Повторите загрузку.')
        names.add(name)
        path = folder / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row.get('size_bytes') or sha256_file(path) != row.get('sha256'):
            raise DocumentBundleRejected('Документ изменился после загрузки: ' + name + '. Повторите скачивание из ЕИС.')
        files.append(path)
    return files


def display_status(reports, tender_id):
    try:
        payload = read_bundle(reports, tender_id)
        if payload is None:
            return {'checked': False, 'state': 'legacy', 'blocked': False, 'errors': []}
        state = payload.get('state', 'failed')
        errors = [str(value)[:500] for value in (payload.get('errors') or [])[:5]]
        if state == 'loading':
            errors = ['Загрузка не завершена. Если обработка уже остановлена, повторите скачивание.']
        return {'checked': True, 'state': state, 'blocked': state != 'complete', 'errors': errors,
                'total': payload.get('total', 0), 'downloaded': payload.get('downloaded', 0)}
    except DocumentBundleRejected as error:
        return {'checked': False, 'state': 'failed', 'blocked': True, 'errors': [str(error)]}


def download_batch(tender_id, links, downloads_dir, *, cookies, download, sanitize):
    root = Path(downloads_dir).parent
    folder = Path(downloads_dir) / str(tender_id)
    folder.mkdir(parents=True, exist_ok=True)
    journal = bundle_path(root / 'reports', tender_id)
    with output_lock(journal, timeout=.1):
        deadline = time.monotonic() + MAX_SECONDS
        state = {'schema_version': 1, 'tender_id': str(tender_id), 'run_id': uuid.uuid4().hex,
                 'started_at': datetime.now(timezone.utc).isoformat(), 'state': 'loading',
                 'total': len(links), 'downloaded': 0, 'files': [], 'errors': []}
        atomic_json(journal, state)
        incoming, log, saved = [], [], []
        try:
            if not links:
                raise DocumentBundleRejected('ЕИС не вернула ссылки на документы. Проверьте доступность источника и повторите загрузку.')
            if len(links) > MAX_FILES:
                raise DocumentBundleRejected('В комплекте больше 100 документов. Автоматическая загрузка не выполнена.')
            previous_log = []
            old_log = folder / 'download_log.json'
            if old_log.is_file() and old_log.stat().st_size <= 2 * 1024 * 1024:
                try:
                    value = json.loads(old_log.read_text(encoding='utf-8'))
                    previous_log = [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []
                except (OSError, ValueError):
                    pass
            total_size = 0
            for _, url in links:
                if time.monotonic() >= deadline:
                    raise DocumentBundleRejected('Время загрузки комплекта истекло. Повторите попытку позже.')
                extension = '.zip' if '.zip' in url.lower() else '.rar' if '.rar' in url.lower() else '.bin'
                destination = folder / ('.autobot-incoming-' + uuid.uuid4().hex + extension)
                reasons = []
                result = download(url, destination, cookies=cookies, diagnostics=reasons, timeout=deadline - time.monotonic())
                if result is None:
                    state['errors'].extend(reasons or ['Документ не скачался.'])
                    log.append({'tender_id': str(tender_id), 'url': url, 'status': 'failed', 'error': ' · '.join(reasons)})
                    continue
                path, original = result
                path = Path(path)
                incoming.append((path, original, url))
                total_size += path.stat().st_size
                if total_size > MAX_TOTAL_BYTES:
                    raise DocumentBundleRejected('Общий размер комплекта превышает 512 МиБ.')
                state['downloaded'] += 1
                atomic_json(journal, state)
            if state['errors']:
                raise DocumentBundleRejected('Комплект загружен не полностью. Прежние документы и отчёт сохранены.')
            # Activate only after every response has completed and passed validation.
            # Per-file storage keeps replaced originals and caches in recoverable versions.
            from collections import defaultdict
            names = defaultdict(set)
            staged = []
            for path, original, url in incoming:
                digest = sha256_file(path)
                preferred = sanitize(original, fallback='document' + path.suffix)
                if preferred.startswith('.'):
                    preferred = 'document-' + digest[:12] + path.suffix
                names[preferred.casefold()].add(digest)
                staged.append((path, original, url, preferred, digest))
            for path, original, url, preferred, digest in staged:
                if len(names[preferred.casefold()]) > 1:
                    name = Path(preferred)
                    preferred = name.stem + '_' + digest[:12] + name.suffix
                stored = store_downloaded_source_file(path, tender_id=str(tender_id),
                    preferred_name=preferred, source_url=url,
                    data_dir=root, previous_log=(*previous_log, *log))
                destination = Path(stored['path'])
                row = {'tender_id': str(tender_id), 'url': url, 'status': 'ok',
                       'saved_path': str(destination), 'saved_name': destination.name, 'original_name': original,
                       'size_bytes': destination.stat().st_size, 'sha256': stored['sha256'],
                       'storage_action': stored['action'], 'old_versions_moved': stored['old_versions_moved'], 'trash_path': stored['trash_path']}
                log.append(row)
                if destination not in saved:
                    saved.append(destination)
                    state['files'].append({key: row[key] for key in ('saved_name', 'size_bytes', 'sha256')})
            atomic_json(old_log, log)
            state['state'] = 'complete'
            atomic_json(journal, state)
            return current_files(downloads_dir, tender_id)
        except Exception as error:
            state['state'] = 'failed'
            state['errors'] = (state['errors'] + [str(error) if isinstance(error, DocumentBundleRejected)
                                else 'Не удалось сохранить комплект документов. Повторите загрузку.'])[:5]
            atomic_json(journal, state)
            # Do not replace download_log after failure: it is needed to match and
            # recover old source identities on the next attempt.
            print('[download] ' + ' · '.join(state['errors']))
            return []
        finally:
            for path, _, _ in incoming:
                if path.parent.resolve() == folder.resolve() and path.name.startswith('.autobot-incoming-'):
                    path.unlink(missing_ok=True)
