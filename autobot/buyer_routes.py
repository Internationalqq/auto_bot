"""CRM-authenticated tender drafts and separately authenticated Mac worker API."""
from functools import wraps
import re
import sqlite3
from pathlib import Path
from flask import Blueprint, jsonify, request, send_from_directory
from autobot import buyer_jobs as jobs, crm_actor
from autobot.hermes_buyer import BuyerError
from autobot.uploaded_corrections import CorrectionError
from autobot.estimate_publication_recovery import consistent_report, PublicationRecoveryRequired

blueprint = Blueprint('buyer', __name__)
WORKER_API = '/api/agent-market/v1/buyer'


@blueprint.after_request
def no_cache(response):
    response.headers['Cache-Control'] = 'private, no-store'
    return response


def user_route(fn):
    @wraps(fn)
    def wrapped(tid):
        try:
            crm_actor.resolve(request.headers)
            if not re.fullmatch(r'\d{8,25}', tid):
                raise BuyerError('Некорректный номер тендера')
            return fn(tid)
        except CorrectionError as error:
            return jsonify(ok=False, message=str(error)), error.status
        except BuyerError as error:
            return jsonify(ok=False, message=str(error)), 400
        except (TimeoutError, OSError, sqlite3.OperationalError, PublicationRecoveryRequired):
            return jsonify(ok=False, message='Не удалось прочитать смету. Повторите позже.'), 503
    return wrapped


@blueprint.route('/api/tenders/<tid>/buyer/jobs', methods=['GET', 'POST'])
@user_route
def tender_jobs(tid):
    if request.method == 'POST':
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise BuyerError('Ожидается объект параметров')
        if data.get('action') == 'cancel':
            return jsonify(ok=True, canceled=jobs.cancel(tid))
        if data.get('action', 'start') != 'start':
            raise BuyerError('Неизвестное действие')
        from autobot import web_ui as web
        with consistent_report(web.REPORTS_DIR, tid):
            if not (web.REPORTS_DIR / f'ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx').is_file():
                return jsonify(ok=False, message='Сначала загрузите и разберите смету'), 404
            metadata = web.load_tender_metadata().get(tid, {})
            tender = web.build_tender_detail(tid, metadata, {})
            keys = data.get('position_keys')
            if keys is not None and (not isinstance(keys, list) or not keys or
                                      any(not isinstance(k, str) for k in keys) or len(keys) > 2000):
                raise BuyerError('Некорректный список позиций')
            rows = [p for p in tender['positions'] if p.get('price_state') != 'excluded']
            if keys is not None:
                wanted = set(keys)
                rows = [p for p in rows if p['position_key'] in wanted]
                if {p['position_key'] for p in rows} != wanted:
                    raise BuyerError('Часть выбранных строк изменилась. Обновите смету')
            else:
                rows = [p for p in rows if not p.get('verified_count')]
            if not rows:
                raise BuyerError('Нет выбранных позиций, требующих запроса цены')
            # Copy authoritative requirements, including composition warnings.
            positions = [{**p, 'specification': {'requirements': p.get('requirements'),
                'resource_scope': p.get('resource_scope'), 'section_note': p.get('section_note')}} for p in rows]
            ids = jobs.enqueue({'tender_id': tid, 'region': tender.get('region'), 'positions': positions})
        return jsonify(ok=True, job_ids=ids, position_count=len(rows)), 202
    result = []
    for job in jobs.jobs(tid):
        task = job['payload']['draft_task']
        result.append({k: job[k] for k in ('id', 'position_name', 'status', 'error', 'created_at', 'updated_at', 'result')} |
                      {'positions': task['positions'], 'region': task['region']})
    return jsonify(ok=True, jobs=result)


@blueprint.get('/tenders/buyer.<ext>')
def asset(ext):
    if ext not in {'js', 'css'}:
        return '', 404
    return send_from_directory(Path(__file__).parent / 'static', 'buyer.' + ext)


def worker_route(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not jobs.authorized(request.headers.get('Authorization', '')):
            return jsonify(ok=False, message='Неверный ключ исполнителя'), 401
        if request.content_length is None or request.content_length > 500_000:
            return jsonify(ok=False, message='Пакет слишком большой'), 413
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get('worker_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', data['worker_id']):
            return jsonify(ok=False, message='Некорректный исполнитель'), 400
        if len(request.get_data()) > 500_000:
            return jsonify(ok=False, message='Пакет слишком большой'), 413
        try:
            return fn(data, *args, **kwargs)
        except BuyerError as error:
            return jsonify(ok=False, message=str(error)), 422
        except sqlite3.OperationalError:
            return jsonify(ok=False, message='Очередь временно недоступна'), 503
    return wrapped


@blueprint.post(WORKER_API + '/claim')
@worker_route
def claim(data):
    return jsonify(ok=True, job=jobs.claim(data['worker_id']))


@blueprint.post(WORKER_API + '/jobs/<job_id>/<action>')
@worker_route
def update(data, job_id, action):
    token = data.get('lease_token')
    if not isinstance(token, str) or not token:
        return jsonify(ok=False, message='Нужен ключ текущей попытки'), 409
    args = (job_id, data['worker_id'], token)
    if action == 'heartbeat':
        ok = jobs.heartbeat(*args)
    elif action == 'complete':
        ok = jobs.complete(*args, data.get('result'))
    elif action == 'fail':
        # Errors are bounded codes, never arbitrary model output or secrets.
        error = data.get('error')
        if error not in {'submission_uncertain', 'invalid_result', 'failed', 'cancelled', 'interrupted', 'timeout'}:
            return jsonify(ok=False, message='Неизвестный результат'), 400
        ok = jobs.fail(*args, error)
    else:
        return jsonify(ok=False), 404
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False, message='Попытка завершена или передана другому исполнителю'), 409)
