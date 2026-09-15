"""Uploaded estimates use the existing durable market queue and evidence delivery."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from autobot import agent_market_queue as queue, uploaded_estimates as store
from autobot.atomic_output import output_lock
from autobot.paths import REPO_ROOT
from autobot.market_analytics import COL_NAME, COL_UNIT
from autobot.upload_admission import operation_key

ESTIMATES_ROOT = REPO_ROOT / 'data' / 'user_estimates'
ACTIVE = ('queued', 'leased', 'applying')
TYPES = {'work', 'service', 'product', 'material', 'other'}


class MarketError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def source_digest(rows):
    try:
        return hashlib.sha256(_json(rows).encode()).hexdigest()
    except (TypeError, ValueError) as error:
        raise MarketError('В сохранённой смете есть некорректные значения. Проверьте исходник.', 503) from error


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F-]{1,40}', value):
        raise MarketError('Смета не найдена.', 404)
    return value


def subject(estimate_id):
    return 'estimate:' + _id(estimate_id)


def source_lock(estimate_id, root=None):
    root = Path(root or ESTIMATES_ROOT).resolve()
    folder = root / _id(estimate_id)
    if folder.is_symlink() or folder.resolve().parent != root:
        raise MarketError('Недопустимая папка сметы.', 503)
    return output_lock(root / '.market_locks' / estimate_id)


@contextmanager
def _connection(path=None, *, write=False):
    path = Path(path or queue.DEFAULT_DB_PATH)
    connection = None
    try:
        if path.is_symlink():
            raise MarketError('Недопустимое хранилище заданий.', 503)
        if not write and not path.exists():
            yield None
            return
        if write:
            queue.init_db(path)
            connection = queue._connect(path)
            connection.execute('PRAGMA synchronous=FULL')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS uploaded_market_runs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    estimate_id TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS uploaded_market_latest
                    ON uploaded_market_runs(estimate_id,sequence DESC);
                CREATE TABLE IF NOT EXISTS uploaded_market_run_jobs (
                    run_id TEXT NOT NULL REFERENCES uploaded_market_runs(run_id),
                    job_id TEXT NOT NULL UNIQUE REFERENCES agent_market_jobs(id),
                    PRIMARY KEY(run_id,job_id));
            ''')
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('BEGIN IMMEDIATE')
        else:
            connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=20)
            connection.row_factory = sqlite3.Row
            connection.execute('BEGIN')
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='uploaded_market_runs' AND type='table'").fetchone():
                yield None
                return
        yield connection
        if write:
            connection.commit()
    finally:
        if connection is not None:
            connection.close()


def _run(row):
    if row is None:
        return None
    try:
        value = dict(row)
        plan = json.loads(value.pop('plan_json'))
        if (not isinstance(plan, dict) or plan.get('schema_version') != 1
                or plan.get('estimate_id') != value['estimate_id']
                or source_digest(plan) != value['digest']):
            raise ValueError('invalid run')
        value['plan'] = plan
        return value
    except (ValueError, TypeError, KeyError) as error:
        raise MarketError('Повреждено описание поиска. Нужна проверка хранилища.', 503) from error


def latest(estimate_id, *, path=None):
    with _connection(path) as connection:
        return _run(connection.execute('SELECT * FROM uploaded_market_runs WHERE estimate_id=? ORDER BY sequence DESC LIMIT 1',
                                      (_id(estimate_id),)).fetchone()) if connection else None


def settings(estimate_id, *, path=None):
    row = latest(estimate_id, path=path)
    if not row:
        return {}
    plan = row['plan']
    return {'market_city':plan['city'], 'market_sources':'web',
            'market_selected_types':plan['selected_types'], 'market_run_id':row['run_id']}


def enqueue(estimate_id, *, city='', selected_types=None, operation_id=None, root=None, path=None):
    estimate_id = _id(estimate_id)
    run_id = operation_key(operation_id)
    if not isinstance(city, str) or len(city) > 120:
        raise MarketError('Город должен быть текстом не длиннее 120 символов.', 400)
    city = re.sub(r'\s+', ' ', city).strip()
    types = [] if selected_types is None else selected_types
    if not isinstance(types, list) or len(types) > len(TYPES) or any(not isinstance(t,str) or t not in TYPES for t in types):
        raise MarketError('Некорректный список типов позиций.', 400)
    types = sorted(set(types))
    root = Path(root or ESTIMATES_ROOT)
    with _connection(path, write=True) as connection, source_lock(estimate_id, root):
        metadata = store.load_meta(root, estimate_id)
        if metadata is None:
            raise MarketError('Смета не найдена.', 404)
        rows = store.load_rows(root, estimate_id)
        if not rows or len(rows) > 50000:
            raise MarketError('В смете нет строк или превышен предел 50 000 позиций.', 400)
        chosen = [row for row in rows if (not types or row.get('type') in types) and str(row.get('name') or '').strip()]
        if not chosen:
            raise MarketError('По выбранным типам нет позиций для поиска.', 400)
        plan = {'schema_version':1, 'estimate_id':estimate_id, 'city':city, 'selected_types':types,
                'source_digest':source_digest(rows), 'row_count':len(rows), 'position_count':len(chosen), 'sources':['web']}
        digest = source_digest(plan)
        previous = _run(connection.execute('SELECT * FROM uploaded_market_runs WHERE run_id=?', (run_id,)).fetchone())
        if previous:
            if previous['digest'] != digest:
                raise MarketError('Этот запуск уже связан с другой сметой или условиями. Начните новый поиск.')
            return previous, True
        if connection.execute("SELECT 1 FROM agent_market_jobs WHERE tender_id=? AND status IN ('queued','leased','applying') LIMIT 1",
                              (subject(estimate_id),)).fetchone():
            raise MarketError('Поиск рынка уже выполняется. Дождитесь результата или остановите его.')
        from autobot.market_contract import position_identity
        frame = store.report_frame(chosen)
        positions = []
        keys = set()
        for _, row in frame.iterrows():
            key = position_identity(row)
            if key in keys:
                raise MarketError('У позиций повторяются исходные координаты. Проверьте смету.', 400)
            keys.add(key)
            positions.append({'target_kind':'uploaded_estimate','estimate_id':estimate_id,'run_id':run_id,
                'source_digest':plan['source_digest'],'position_key':key,'name':str(row[COL_NAME]),
                'unit':str(row.get(COL_UNIT) or ''),'region':city,'job_mode':'web','search_mode':'web',
                'max_attempts':2,'max_offers':3})
        connection.execute('INSERT INTO uploaded_market_runs(run_id,estimate_id,plan_json,digest,created_at) VALUES(?,?,?,?,?)',
                           (run_id,estimate_id,_json(plan),digest,queue._now()))
        added = queue.enqueue_in_transaction(connection, subject(estimate_id), positions)
        if added['skipped_active'] or len(added['created']) != len(chosen):
            raise MarketError('Не удалось принять весь поиск. Повторите запуск.')
        connection.executemany('INSERT INTO uploaded_market_run_jobs VALUES(?,?)',
                               [(run_id,job['id']) for job in added['created']])
        return _run(connection.execute('SELECT * FROM uploaded_market_runs WHERE run_id=?',(run_id,)).fetchone()), False


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds') if value else None


def status(estimate_id, *, run_id=None, path=None):
    with _connection(path) as connection:
        if not connection:
            return None
        if run_id is not None:
            run_id = operation_key(run_id)
            row = connection.execute('SELECT * FROM uploaded_market_runs WHERE estimate_id=? AND run_id=?',(_id(estimate_id),run_id)).fetchone()
        else:
            row = connection.execute('SELECT * FROM uploaded_market_runs WHERE estimate_id=? ORDER BY sequence DESC LIMIT 1',(_id(estimate_id),)).fetchone()
        run = _run(row)
        if not run:
            return None
        run_id = run['run_id']
        counts = {row['status']:row['n'] for row in connection.execute('''SELECT j.status,COUNT(*) n
            FROM uploaded_market_run_jobs r JOIN agent_market_jobs j ON j.id=r.job_id WHERE r.run_id=? GROUP BY j.status''',(run_id,))}
        total = sum(counts.values())
        if total != run['plan']['position_count']:
            raise MarketError('В очереди отсутствует часть позиций поиска. Нужна проверка хранилища.',503)
        remaining = sum(counts.get(name,0) for name in ACTIVE)
        failed,canceled = counts.get('failed',0),counts.get('canceled',0)
        recent = connection.execute('''SELECT j.position_name,j.status,j.error,j.updated_at,j.result_json
            FROM uploaded_market_run_jobs r JOIN agent_market_jobs j ON j.id=r.job_id WHERE r.run_id=?
            ORDER BY j.updated_at DESC,j.id LIMIT 8''',(run_id,)).fetchall()
        current = connection.execute('''SELECT j.position_name FROM uploaded_market_run_jobs r
            JOIN agent_market_jobs j ON j.id=r.job_id WHERE r.run_id=? AND j.status IN ('leased','applying')
            ORDER BY j.updated_at DESC LIMIT 1''',(run_id,)).fetchone()
        stage = ('Ищу цены' if current else 'В очереди') if remaining else ('Завершено с ошибками' if failed else 'Остановлено' if canceled else 'Готово')
        logs = []
        for item in reversed(recent):
            notes = ''
            if item['status'] == 'completed':
                try: notes = str(json.loads(item['result_json']).get('notes') or '')[:400]
                except (ValueError,AttributeError): pass
            label = {'queued':'В очереди','leased':'Поиск','applying':'Сохраняю','completed':'Проверено','failed':'Ошибка','canceled':'Отменено'}.get(item['status'],'Состояние')
            logs.append(label + ' · ' + item['position_name'][:160] + (' · ' + (item['error'] or notes)[:400] if item['error'] or notes else ''))
        return {'run_id':run_id,'running':bool(remaining),'ok':not remaining and not failed and not canceled,
                'progress':round((total-remaining)/max(1,total)*100),'done':total-remaining,'total':total,
                'completed':counts.get('completed',0),'failed':failed,'canceled':canceled,
                'city':run['plan']['city'],'selected_types':run['plan']['selected_types'],
                'stage':stage,'detail':f'Проверено {counts.get("completed",0)} из {total}' + (f' · ошибок: {failed}' if failed else '') + (f' · отменено: {canceled}' if canceled else '') + (' · ' + current['position_name'][:180] if current else ''),
                'error':'Часть позиций не удалось проверить. Причины — в журнале.' if failed else '',
                'log_lines':logs,'started_at':_stamp(run['created_at']),
                'ended_at':_stamp(max((r['updated_at'] for r in recent),default=0)) if not remaining else None}


def cancel(estimate_id, *, run_id=None, path=None):
    with _connection(path, write=True) as connection:
        latest_row = _run(connection.execute('SELECT * FROM uploaded_market_runs WHERE estimate_id=? ORDER BY sequence DESC LIMIT 1',(_id(estimate_id),)).fetchone())
        if not latest_row:
            raise MarketError('Поиск не найден.',404)
        if run_id is not None and operation_key(run_id) != latest_row['run_id']:
            raise MarketError('Уже начат другой поиск. Обновите статус перед остановкой.')
        cursor = connection.execute("""UPDATE agent_market_jobs SET status='canceled',lease_until=NULL,updated_at=?
            WHERE id IN (SELECT job_id FROM uploaded_market_run_jobs WHERE run_id=?) AND status IN ('queued','leased','applying')""",
            (queue._now(),latest_row['run_id']))
        return cursor.rowcount


def import_context(tender_id, payload, *, root=None, path=None):
    from autobot.real_market_scraper import _resolve_agent_source_row
    from autobot.market_contract import position_identity
    from autobot.market_strategy import build_search_plan
    estimate_id = _id(payload.get('estimate_id'))
    if tender_id != subject(estimate_id) or payload.get('target_kind') != 'uploaded_estimate':
        raise MarketError('Некорректная привязка поиска.')
    run = latest(estimate_id,path=path)
    if not run or run['run_id'] != payload.get('run_id'):
        raise MarketError('Этот поиск заменён новым запуском.')
    root = Path(root or ESTIMATES_ROOT)
    metadata = store.load_meta(root,estimate_id)
    if metadata is None:
        raise MarketError('Смета удалена. Результат не сохранён.')
    rows = store.load_rows(root,estimate_id)
    digest = source_digest(rows)
    if digest != run['plan']['source_digest'] or digest != payload.get('source_digest'):
        raise MarketError('Смета изменилась. Результат прежнего поиска не применён.')
    if payload.get('region') != run['plan']['city']:
        raise MarketError('Город поиска изменился.')
    frame = store.report_frame(rows)
    selected_types = run['plan']['selected_types']
    selected = [row for row in rows if not selected_types or row.get('type') in selected_types]
    row = _resolve_agent_source_row(store.report_frame(selected),payload).copy()
    row['Регион поиска'] = run['plan']['city']
    plan = build_search_plan(row[COL_NAME],row.get(COL_UNIT,''),row.get('basis_code',''),row.get('Раздел',''),row['Регион поиска'])
    return tender_id,str(row[COL_NAME]),position_identity(row),row,frame,metadata,plan,digest


def publish(tender_id,payload,prepared):
    """Called inside queue delivery's transaction; never acquire queue write locks here."""
    from autobot import real_market_scraper as scraper
    from autobot.atomic_output import write_excel
    from autobot.market_contract import merge_market_frames
    from autobot.market_evidence_policy import region_key
    estimate_id = _id(payload.get('estimate_id'))
    with source_lock(estimate_id):
        tid,name,key,row,frame,metadata,plan,digest = import_context(tender_id,payload)
        if (prepared.get('schema_version') != 1 or prepared.get('estimate_digest') != digest
                or prepared.get('position_key') != key or region_key(prepared.get('region')) != region_key(row['Регион поиска'])):
            raise MarketError('Смета или город изменились после поиска. Прежние цены не применены.')
        raw_path = ESTIMATES_ROOT / estimate_id / 'market_sources.xlsx'
        import pandas as pd
        if raw_path.is_symlink() or (raw_path.exists() and raw_path.stat().st_size > 128 * 1024 * 1024):
            raise MarketError('Сохранённый рынок недоступен или превышает допустимый размер.')
        # An unreadable workbook must survive for recovery, not become an empty
        # previous result which the next position silently overwrites.
        previous = pd.read_excel(raw_path) if raw_path.is_file() else pd.DataFrame()
        incoming = [scraper.MarketOffer(**offer) for offer in prepared.get('offers',[])]
        offers = scraper._latest_offers_for_row(row,scraper._saved_offers_for_key(previous,key),incoming)
        output = scraper._build_output_row(row,offers=offers,query=' | '.join(plan.queries),
            err='' if offers else 'Подтверждённых цен не найдено',plan=plan)
        combined = scraper._merge_rows(previous,[output])
        frame['Регион поиска'] = row['Регион поиска']
        # Raw evidence is canonical. Derived comparison is replaced first and is
        # rebuilt on every UI/API read from raw + current positions if both exist.
        write_excel(merge_market_frames(frame,combined),ESTIMATES_ROOT/estimate_id/'market_compare.xlsx')
        write_excel(combined,raw_path)
        return {'imported':len(incoming),'verified':sum(offer.verification=='verified' for offer in offers),
                'total_candidates':sum(offer.verification!='verified' for offer in offers)}
