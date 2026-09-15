"""Durable admission and progress for the single web document/search executor."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from urllib.parse import urlparse

from autobot.atomic_output import output_lock

MAX_LOG_LINES = 300
MAX_LOG_LINE = 1500
MAX_BATCH = 10000
ACTIVE = ('queued', 'running')


class JobBusy(ValueError):
    pass


class JobConflict(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _tid(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{8,25}', value):
        raise ValueError('Некорректный номер тендера.')
    return value


def validate_plan(plan):
    """Only existing main.py operations; no shell, module or output path from data."""
    if not isinstance(plan, dict):
        raise ValueError('Некорректное задание.')
    if plan.get('kind') == 'batch':
        ids = plan.get('tender_ids')
        if set(plan) != {'kind', 'tender_ids'} or not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BATCH:
            raise ValueError('Некорректный список тендеров.')
        result = {'kind': 'batch', 'tender_ids': [_tid(value) for value in ids]}
        if len(set(ids)) != len(ids):
            raise ValueError('Повтор номера тендера в задании.')
        return result
    argv = plan.get('argv')
    if plan.get('kind') != 'main' or set(plan) != {'kind', 'argv'} or not isinstance(argv, list) or not 1 <= len(argv) <= 16:
        raise ValueError('Некорректные параметры задания.')
    if any(not isinstance(value, str) or len(value) > 16384 or '\x00' in value for value in argv):
        raise ValueError('Некорректные параметры задания.')
    flags, values = {'--catalog-only', '--resume-downloads'}, {}
    pairs = {'--max-pages', '--max-tenders', '--days-back', '--search-filters-json',
             '--from-tender-id', '--from-tender-url', '--from-downloaded-tender-id'}
    index = 0
    while index < len(argv):
        key = argv[index]
        if key in values or key not in flags | pairs:
            raise ValueError('Недопустимый параметр задания.')
        if key in flags:
            values[key] = True
            index += 1
        else:
            if index + 1 >= len(argv):
                raise ValueError('Отсутствует значение параметра.')
            values[key] = argv[index + 1]
            index += 2
    if '--from-downloaded-tender-id' in values:
        if set(values) != {'--from-downloaded-tender-id'}:
            raise ValueError('Несовместимые параметры разбора.')
        _tid(values['--from-downloaded-tender-id'])
    elif '--from-tender-id' in values or '--from-tender-url' in values:
        if set(values) != {'--from-tender-id', '--from-tender-url'}:
            raise ValueError('Несовместимые параметры скачивания.')
        _tid(values['--from-tender-id'])
        url = urlparse(values['--from-tender-url'])
        if url.scheme != 'https' or not url.hostname or not (url.hostname == 'zakupki.gov.ru' or url.hostname.endswith('.zakupki.gov.ru')) or url.username or url.password or url.port not in (None, 443):
            raise ValueError('Для скачивания требуется HTTPS-ссылка на ЕИС.')
    else:
        for key, maximum in (('--max-pages', 20), ('--max-tenders', 100), ('--days-back', 365)):
            value = values.get(key, '')
            if not isinstance(value, str) or not value.isascii() or not value.isdigit() or not 1 <= int(value) <= maximum:
                raise ValueError('Некорректные пределы поиска.')
        if '--catalog-only' in values and '--resume-downloads' in values:
            raise ValueError('Несовместимые режимы поиска.')
        if '--search-filters-json' in values:
            from autobot.tender_search_profiles import validate_filters
            filters = validate_filters(json.loads(values['--search-filters-json']))
            if any(filters[key] != int(values['--'+key.replace('_', '-')]) for key in ('max_pages', 'max_tenders', 'days_back')):
                raise ValueError('Снимок поиска не совпадает с пределами запуска.')
    return {'kind': 'main', 'argv': list(argv)}


def _connect(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Хранилище заданий не должно быть ссылкой.')
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=20, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA busy_timeout=20000')
        with output_lock(Path(str(path) + '.schema'), timeout=20):
            if connection.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
                connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS main_jobs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    plan_json TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    task TEXT NOT NULL,
                    tender_id TEXT,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','interrupted')),
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    exit_code INTEGER,
                    logs_json TEXT NOT NULL DEFAULT '[]',
                    done INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS main_jobs_one_active ON main_jobs((1))
                    WHERE status IN ('queued','running');
            ''')
        return connection
    except BaseException:
        connection.close()
        raise


def _record(row):
    if row is None:
        return None
    result = dict(row)
    result['plan'] = json.loads(result.pop('plan_json'))
    result['logs'] = json.loads(result.pop('logs_json'))
    return result


def enqueue(path, plan, task, tender_id=None, *, run_id=None):
    plan = validate_plan(plan)
    if not isinstance(task, str) or not 1 <= len(task) <= 500:
        raise ValueError('Некорректное название задания.')
    if tender_id is not None:
        _tid(tender_id)
    run_id = run_id or uuid.uuid4().hex
    if not isinstance(run_id, str) or not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ValueError('Некорректный номер запуска.')
    encoded = _json(plan)
    digest = hashlib.sha256(_json([plan, task, tender_id]).encode()).hexdigest()
    with closing(_connect(path)) as con:
        con.execute('BEGIN IMMEDIATE')
        try:
            prior = con.execute('SELECT * FROM main_jobs WHERE run_id=?', (run_id,)).fetchone()
            if prior:
                if prior['digest'] != digest:
                    raise JobConflict('Номер запуска уже использован с другими условиями.')
                con.commit()
                return _record(prior), True
            if con.execute("SELECT 1 FROM main_jobs WHERE status IN ('queued','running')").fetchone():
                raise JobBusy('Сейчас выполняется другая работа с документами.')
            con.execute('''INSERT INTO main_jobs(run_id,plan_json,digest,task,tender_id,status,created_at,total,logs_json)
                VALUES(?,?,?,?,?,'queued',?,?,?)''',
                (run_id, encoded, digest, task, tender_id, _now(), len(plan.get('tender_ids', [None])),
                 _json(['Задание сохранено. Ожидаем запуск исполнителя.'])))
            row = con.execute('SELECT * FROM main_jobs WHERE run_id=?', (run_id,)).fetchone()
            con.commit()
            return _record(row), False
        except BaseException:
            con.rollback()
            raise


def latest(path):
    if not Path(path).exists():
        return None
    with closing(_connect(path)) as con:
        return _record(con.execute('SELECT * FROM main_jobs ORDER BY sequence DESC LIMIT 1').fetchone())


def latest_for_tender(path, tender_id):
    if not Path(path).exists():
        return None
    with closing(_connect(path)) as con:
        return _record(con.execute('SELECT * FROM main_jobs WHERE tender_id=? ORDER BY sequence DESC LIMIT 1', (tender_id,)).fetchone())


def get(path, run_id):
    if not Path(path).exists():
        return None
    with closing(_connect(path)) as con:
        return _record(con.execute('SELECT * FROM main_jobs WHERE run_id=?', (run_id,)).fetchone())


def execution_lock(path, *, timeout=0):
    return output_lock(Path(str(path) + '.executor'), timeout=timeout)


def claim(path, run_id):
    """Caller owns execution_lock until finish; the web process never owns it."""
    with closing(_connect(path)) as con:
        changed = con.execute("UPDATE main_jobs SET status='running',started_at=? WHERE run_id=? AND status='queued'",
                              (_now(), run_id)).rowcount
        return _record(con.execute('SELECT * FROM main_jobs WHERE run_id=?', (run_id,)).fetchone()) if changed else None


def progress(path, run_id, logs, *, done=None):
    lines = [str(line)[:MAX_LOG_LINE] for line in logs[-MAX_LOG_LINES:]]
    with closing(_connect(path)) as con:
        if done is None:
            con.execute("UPDATE main_jobs SET logs_json=? WHERE run_id=? AND status='running'", (_json(lines), run_id))
        else:
            con.execute("UPDATE main_jobs SET logs_json=?,done=MIN(total,?) WHERE run_id=? AND status='running'", (_json(lines), max(0, int(done)), run_id))


def finish(path, run_id, exit_code):
    with closing(_connect(path)) as con:
        con.execute("UPDATE main_jobs SET status=?,exit_code=?,ended_at=? WHERE run_id=? AND status='running'",
                    ('completed' if exit_code == 0 else 'failed', exit_code, _now(), run_id))


def launch_failed(path, run_id):
    with closing(_connect(path)) as con:
        con.execute("UPDATE main_jobs SET status='failed',exit_code=-1,ended_at=?,logs_json=? WHERE run_id=? AND status='queued'",
                    (_now(), _json(['Не удалось запустить исполнителя. Повторите попытку.']), run_id))


def recover_interrupted(path):
    """A missing OS lock is evidence of a lost executor, not permission to replay."""
    if not Path(path).exists():
        return False
    try:
        with execution_lock(path), closing(_connect(path)) as con:
            row = con.execute("SELECT * FROM main_jobs WHERE status='running'").fetchone()
            if row is None:
                return False
            logs = json.loads(row['logs_json'])[-(MAX_LOG_LINES-1):]
            logs.append('Исполнитель остановился до подтверждения результата. Проверьте отчёт и повторите нужное действие.')
            con.execute("UPDATE main_jobs SET status='interrupted',exit_code=-1,ended_at=?,logs_json=? WHERE run_id=? AND status='running'",
                        (_now(), _json(logs), row['run_id']))
            return True
    except TimeoutError:
        return False


def public_status(row):
    if row is None:
        return None
    return {'running': row['status'] in ACTIVE, 'job_status': row['status'], 'task': row['task'],
            'run_id': row['run_id'], 'tender_id': row['tender_id'], 'command': '',
            'started_at': row['started_at'] or row['created_at'], 'ended_at': row['ended_at'],
            'exit_code': row['exit_code'], 'log_lines_count': len(row['logs']), 'log_tail': row['logs'][-80:],
            'done': row['done'], 'total': row['total']}
