"""Durable, idempotent receipt of one uploaded estimate before its parser starts."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from autobot.atomic_output import output_lock
from autobot import uploaded_estimates as store


class AdmissionError(RuntimeError):
    def __init__(self, message, status=503, *, retry_upload=False):
        super().__init__(message)
        self.status = status
        self.retry_upload = retry_upload


def operation_key(value):
    if value is None or value == '':
        return uuid.uuid4().hex
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value):
        raise AdmissionError('Некорректный идентификатор загрузки.', 400)
    return uuid.UUID(value).hex


def _invalid_constant(_):
    raise ValueError('Non-finite number in upload record')


def _read(path, maximum=64 * 1024):
    if not path.exists():
        return None
    try:
        if path.is_symlink() or path.stat().st_size > maximum:
            raise ValueError('invalid record')
        data = json.loads(path.read_text(encoding='utf-8'), parse_constant=_invalid_constant)
        if not isinstance(data, dict):
            raise ValueError('invalid record')
        return data
    except (OSError, ValueError, TypeError) as error:
        raise AdmissionError('Не удалось прочитать сохранённую загрузку. Повторите позже.') from error


def receipt(jobs_dir, key):
    if not re.fullmatch(r'[0-9a-f]{32}', str(key or '')):
        return None
    value = _read(Path(jobs_dir) / 'admissions' / (key + '.json'), 4096)
    if value is None:
        return None
    if (value.get('job_id') != key or value.get('target_estimate_id') != key
            or not re.fullmatch(r'[0-9a-f]{64}', str(value.get('source_sha256') or ''))
            or not isinstance(value.get('title_raw'), str)
            or len(value['title_raw']) > 160
            or not isinstance(value.get('started_at'), str) or not 1 <= len(value['started_at']) <= 40
            or not isinstance(value.get('original_name'), str)
            or not 1 <= len(value['original_name']) <= 120
            or any(ord(char) < 32 for char in value['original_name'])
            or '/' in value['original_name'] or '\\' in value['original_name']
            or Path(value['original_name']).suffix.lower() not in {'.xlsx', '.xls', '.xlsm', '.pdf'}):
        raise AdmissionError('Повреждено описание принятой загрузки.')
    return value


def _source(root, record):
    root = Path(root).resolve()
    folder = root / record['target_estimate_id']
    path = folder / record['original_name']
    if folder.is_symlink() or path.is_symlink() or path.resolve().parent != folder or folder.resolve().parent != root:
        raise AdmissionError('Исходный файл загрузки находится вне её папки.', 409)
    return path


def _digest(stream, maximum):
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > maximum:
            raise AdmissionError('Файл превышает допустимый размер загрузки.', 413)
        digest.update(chunk)
    return digest.hexdigest()


def _seed(record, source, repo_root):
    stamp = record['started_at']
    return {**record, 'estimate_id': None, 'source_path': str(source.relative_to(Path(repo_root).resolve())),
            'running': True, 'ok': False, 'progress': 26, 'progress_estimated': False,
            'stage': 'Файл получен', 'detail': 'Файл сохранён. Подготавливаю разбор', 'error': '',
            'ended_at': None, 'updated_at': stamp, 'elapsed_seconds': 0,
            'log_lines': [stamp + ' · Файл получен: ' + record['original_name']]}


def _ensure_job(record, source, jobs_dir, repo_root):
    path = Path(jobs_dir) / (record['job_id'] + '.json')
    job = _read(path)
    if job is not None:
        expected_source = str(source.relative_to(Path(repo_root).resolve()))
        if (any(job.get(name) != record[name] for name in ('job_id', 'target_estimate_id', 'source_sha256', 'original_name', 'title_raw'))
                or job.get('source_path') != expected_source
                or type(job.get('running')) is not bool or type(job.get('ok')) is not bool
                or type(job.get('progress')) is not int or not 0 <= job['progress'] <= 100
                or job.get('estimate_id') not in (None, record['target_estimate_id'])):
            raise AdmissionError('Сохранённое задание не соответствует загрузке.')
        return job
    if not source.is_file():
        raise AdmissionError('Передача файла не завершилась. Выберите тот же файл и повторите загрузку.', 409, retry_upload=True)
    with source.open('rb') as stream:
        if _digest(stream, 128 * 1024 * 1024) != record['source_sha256']:
            raise AdmissionError('Сохранённый исходник изменился. Загрузите файл новой операцией.', 409)
    job = _seed(record, source, repo_root)
    store.write_json(path, job)
    return job


def restore(jobs_dir, source_root, repo_root, key):
    """Recover the narrow receipt/source-to-job gap; never reset an existing job."""
    if receipt(jobs_dir, key) is None:
        return None
    lock = Path(jobs_dir) / 'admissions' / key
    with output_lock(lock):
        record = receipt(jobs_dir, key)
        if record is None:
            return None
        return _ensure_job(record, _source(source_root, record), jobs_dir, repo_root)


def receive(stream, *, key, original_name, title, source_root, jobs_dir, repo_root, max_bytes):
    """Acknowledge only a durable source plus job; same-key conflicts never overwrite."""
    digest = _digest(stream, min(int(max_bytes), 128 * 1024 * 1024))
    stream.seek(0)
    binding = {'job_id': key, 'target_estimate_id': key, 'source_sha256': digest,
               'original_name': original_name, 'title_raw': title}
    lock = Path(jobs_dir) / 'admissions' / key
    with output_lock(lock):
        saved = receipt(jobs_dir, key)
        duplicate = saved is not None
        if saved is not None:
            if any(saved.get(name) != value for name, value in binding.items()):
                raise AdmissionError('Эта загрузка уже связана с другим файлом или названием. Начните новую загрузку.', 409)
            record = saved
        else:
            record = dict(binding, started_at=datetime.now().isoformat(timespec='seconds'))
            # Reservation is immutable. A crash before source publication can resume
            # only with the exact same file/title, never with a different request.
            store.write_json(Path(jobs_dir) / 'admissions' / (key + '.json'), record)
        source = _source(source_root, record)
        # A killed receiver may leave its own temporary bytes; the per-operation
        # lock guarantees that no living receiver owns these files now.
        for temporary in source.parent.glob('.receiving-*.tmp'):
            if not temporary.is_symlink() and temporary.is_file() and temporary.resolve().parent == source.parent:
                temporary.unlink()
        existing_job = _read(Path(jobs_dir) / (key + '.json'))
        if existing_job is not None:
            if not source.is_file():
                raise AdmissionError('Исходник этой загрузки удалён. Начните новую загрузку.', 410)
            return _ensure_job(record, source, jobs_dir, repo_root), True
        if source.is_file():
            with source.open('rb') as current:
                if _digest(current, 128 * 1024 * 1024) != digest:
                    raise AdmissionError('Сохранённый исходник изменился. Начните новую загрузку.', 409)
        else:
            source.parent.mkdir(parents=True, exist_ok=True)
            handle, filename = tempfile.mkstemp(prefix='.receiving-', suffix='.tmp', dir=source.parent)
            temporary = Path(filename)
            try:
                with os.fdopen(handle, 'wb') as output:
                    while chunk := stream.read(1024 * 1024):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, source)
                if os.name != 'nt':
                    descriptor = os.open(source.parent, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            finally:
                temporary.unlink(missing_ok=True)
        return _ensure_job(record, source, jobs_dir, repo_root), duplicate
