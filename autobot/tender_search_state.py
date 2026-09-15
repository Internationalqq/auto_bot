"""Explicit resume checkpoints and durable diagnostics for the EIS search."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import tempfile
import uuid

CHECKPOINT_TTL_SECONDS = 24 * 3600
MAX_STATE_BYTES = 4 * 1024 * 1024


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def atomic_json(path, value):
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    _atomic_bytes(path, data)


def _atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if len(data) > MAX_STATE_BYTES:
        raise ValueError('Слишком большой файл состояния поиска')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_state(path):
    try:
        path = Path(path)
        if path.stat().st_size > MAX_STATE_BYTES:
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def archive_checkpoint(path):
    """Preserve the exact old checkpoint before its pointer is replaced/closed."""
    path = Path(path)
    if not path.is_file():
        return
    if path.stat().st_size > MAX_STATE_BYTES:
        raise ValueError('Старый checkpoint слишком большой; он сохранён без изменений')
    raw = path.read_bytes()
    history = path.parent / 'search_runs'
    history.mkdir(parents=True, exist_ok=True)
    target = history / ('checkpoint-' + hashlib.sha256(raw).hexdigest() + '.json')
    if not target.exists() or target.read_bytes() != raw:
        _atomic_bytes(target, raw)


def checkpoint_for_resume(path, *, signature=None, now=None):
    value = read_state(path)
    if not value or value.get('schema_version') != 2:
        raise ValueError('Нет пригодного сохранённого поиска. Начните новый поиск закупок.')
    if not re.fullmatch(r'[0-9a-f]{32}', str(value.get('run_id') or '')):
        raise ValueError('Номер сохранённого поиска повреждён.')
    if value.get('completed'):
        raise ValueError('Сохранённое скачивание уже завершено.')
    try:
        started = datetime.fromisoformat(value['started_at'])
        if started.tzinfo is None:
            raise ValueError('timezone')
        age = (now or datetime.now(timezone.utc)).timestamp() - started.timestamp()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('Дата сохранённого поиска повреждена. Начните новый поиск.')
    if age < -300 or age > CHECKPOINT_TTL_SECONDS:
        raise ValueError('Сохранённый поиск старше 24 часов или имеет неверную дату. Начните новый поиск.')
    if signature is not None and value.get('signature') != signature:
        raise ValueError('Фильтры изменились после сохранённого поиска. Начните новый поиск.')
    parameters = value.get('parameters')
    if not isinstance(parameters, dict) or any(
        type(parameters.get(key)) is not int or not low <= parameters[key] <= high
        for key, low, high in [('max_pages', 1, 20), ('max_tenders', 1, 100), ('days_back', 1, 365)]
    ):
        raise ValueError('Параметры сохранённого поиска повреждены.')
    if 'search_filters' in value:
        from autobot.tender_search_profiles import validate_filters
        snapshot = validate_filters(value['search_filters'])
        if any(snapshot[key] != parameters[key] for key in ('max_pages', 'max_tenders', 'days_back')):
            raise ValueError('Снимок условий не соответствует параметрам сохранённого поиска.')
    tenders = value.get('filtered_tenders')
    completed = value.get('completed_ids')
    if (not isinstance(tenders, list) or not tenders or len(tenders) > 100 or
            not all(isinstance(item, dict) and item.get('tender_id') for item in tenders) or
            not isinstance(completed, list) or not all(isinstance(item, str) for item in completed)):
        raise ValueError('Список сохранённых закупок повреждён.')
    pending = {str(item['tender_id']) for item in tenders} - {str(item) for item in completed}
    if not pending:
        raise ValueError('В сохранённом поиске нет незавершённых скачиваний.')
    return value


def public_resume(root):
    if os.environ.get('SEARCH_RESUME', '1').strip().casefold() in {'0', 'false', 'no', 'off'}:
        return {'available': False, 'reason': 'Возобновление отключено настройкой SEARCH_RESUME.'}
    try:
        value = checkpoint_for_resume(Path(root) / 'search_resume_checkpoint.json')
    except ValueError as error:
        return {'available': False, 'reason': str(error)}
    pending = {str(item['tender_id']) for item in value['filtered_tenders']} - set(value['completed_ids'])
    return {'available': True, 'remaining': len(pending), 'parameters': value['parameters'],
            'started_at': value['started_at'], **({'search_filters': value['search_filters']} if 'search_filters' in value else {})}


def start_summary(mode):
    return {'schema_version': 1, 'run_id': uuid.uuid4().hex, 'mode': mode,
            'started_at': now_iso(), 'updated_at': now_iso(), 'state': 'searching',
            'message': 'Поиск в ЕИС выполняется', 'counts': {}, 'rejections': {},
            'source': {'pages_requested': 0, 'pages_loaded': 0, 'cards_seen': 0,
                       'empty_pages': 0, 'unknown_pages': 0, 'page_errors': 0,
                       'card_errors': 0, 'budget_exhausted': False, 'errors': []}}


def save_summary(root, summary):
    summary['updated_at'] = now_iso()
    root = Path(root)
    atomic_json(root / 'search_runs' / (summary['run_id'] + '.json'), summary)
    atomic_json(root / 'last_search_run.json', summary)


def public_summary(root, *, running=False):
    root = Path(root)
    summary = read_state(root / 'last_search_run.json')
    if not summary or running or summary.get('state') not in {'searching', 'downloading'}:
        return summary
    # The web process may have restarted, or a CLI/cron process may still own
    # the search. A saved status alone must not claim an active executor.
    from autobot.atomic_output import output_lock
    active = False
    if (root / 'eis_search.lock').is_file():
        try:
            with output_lock(root / 'eis_search', timeout=0):
                pass
        except TimeoutError:
            active = True
    if not active:
        summary = dict(summary, state='interrupted', message='Процесс поиска завершился до окончания. Начните новый поиск или продолжите сохранённое скачивание.')
    return summary


def record_source_error(diagnostics, message, *, kind='page_errors'):
    diagnostics[kind] = int(diagnostics.get(kind) or 0) + 1
    messages = diagnostics.setdefault('errors', [])
    if len(messages) < 20:
        messages.append(str(message)[:400])


def finish_discovery(summary):
    counts, source = summary['counts'], summary['source']
    uncertain = any(source.get(key) for key in ('page_errors', 'unknown_pages', 'card_errors', 'budget_exhausted'))
    unique, matched = counts.get('unique', 0), counts.get('matched', 0)
    if uncertain and not unique:
        summary.update(state='unavailable', message='ЕИС не вернула проверяемую выдачу. Пустой результат не означает отсутствие закупок.')
    elif uncertain:
        summary.update(state='partial', message=f'Поиск выполнен частично: уникальных закупок {unique}, подходят {matched}. Есть ошибки доступа или достигнут лимит времени.')
    elif not unique:
        summary.update(state='completed', message='ЕИС показала пустую выдачу по текущим условиям поиска.')
    elif not matched:
        summary.update(state='completed', message=f'Найдено уникальных закупок: {unique}. Все отсеяны; причины указаны в итогах поиска.')
    else:
        summary.update(state='completed', message=f'Найдено уникальных закупок: {unique}; подходят {matched}; выбрано {counts.get("selected", 0)}; новых {counts.get("new", 0)}.')
    return summary
