"""Authenticated review of one physical uploaded-estimate position."""
from functools import wraps
import base64
import re
from flask import Blueprint, abort, jsonify, make_response, render_template, request
from autobot import crm_actor, uploaded_corrections as corrections
from autobot.upload_admission import AdmissionError, operation_key
from autobot.uploaded_estimates import StoreError

blueprint = Blueprint('uploaded_review', __name__)


def checked(function):
    @wraps(function)
    def handler(*args, **kwargs):
        try:
            actor = crm_actor.resolve(request.headers)
            response = make_response(function(*args, actor=actor, **kwargs))
        except (corrections.CorrectionError, AdmissionError) as error:
            response = make_response(jsonify({'ok': False, 'message': str(error)}), error.status)
        except (OSError, StoreError):
            response = make_response(jsonify({'ok':False,'message':'Документ временно недоступен.'}),503)
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    return handler


def current(eid):
    from autobot import web_ui as web
    return corrections.snapshot(web.USER_ESTIMATES_DIR, eid)


def position(snapshot, key):
    if not isinstance(key, str) or not key or len(key) > 500:
        raise corrections.CorrectionError('Выберите позицию в смете.', 400)
    for row, original in zip(snapshot['rows'], snapshot['original_rows']):
        if row['position_id'] == key:
            return row, original
    if re.fullmatch(r'pdf:native:\d+:\d+(?:\.\d+)?', key):
        matches=[(row,original) for row,original in zip(snapshot['rows'],snapshot['original_rows'])
                 if re.fullmatch(re.escape(key)+r':[a-f0-9]{16}',row['position_id'])]
        if len(matches)==1:return matches[0]
    raise corrections.CorrectionError('Позиция не найдена в смете.', 404)


def source_page(row):
    match = re.search(r'(?:стр\.|страница)\s*(\d{1,3})\b', str(row.get('sheet') or ''), re.I)
    return int(match[1]) if match and 1 <= int(match[1]) <= 250 else 1


@blueprint.get('/estimates/<eid>/review')
@checked
def review_page(eid, *, actor):
    from autobot import web_ui as web
    value = current(eid)
    row, original = position(value, request.args.get('position_id'))
    history = corrections.history(web.USER_ESTIMATES_DIR, eid)
    source = web._estimate_original_path(eid, value['meta'])
    return render_template('estimate_review.html', estimate_id=eid, meta=value['meta'], row=row, original=original,
        fields=corrections.FIELDS, types=corrections.TYPES, version=value['version'], revision=value['revision'],
        actor=actor, history=history, has_original=source is not None,
        original_url='/estimates/'+eid+'/original', source_page=source_page(original),
        review_config={'estimateId':eid,'positionId':row['position_id'],'version':value['version'],
                       'fields':list(corrections.FIELDS),'labels':corrections.FIELDS,'values':{name:row.get(name) for name in corrections.FIELDS}})


@blueprint.route('/api/estimates/<eid>/corrections', methods=['GET', 'POST'])
@checked
def correction_api(eid, *, actor):
    from autobot import web_ui as web
    root = web.USER_ESTIMATES_DIR
    if request.method == 'GET':
        value = current(eid)
        if request.args.get('operation_id'):
            key = operation_key(request.args['operation_id'])
            with corrections.connection(root) as con:
                saved = corrections.event(con.execute('SELECT * FROM estimate_revisions WHERE estimate_id=? AND operation_id=?',
                    (eid, key)).fetchone()) if con else None
            if saved is None or saved['actor']['id'] != actor['id']:
                return jsonify({'ok':False,'message':'Сохранение с этим ключом ещё не найдено.'}),404
            return jsonify({'ok':True,'version':saved['version'],'revision':saved['revision']})
        before = request.args.get('before')
        if before is not None and (not before.isdigit() or len(before) > 10):
            raise corrections.CorrectionError('Некорректная страница истории.',400)
        history = corrections.history(root, eid, before=int(before) if before is not None else None)
        result = {'ok':True,'version':value['version'],'revision':value['revision'],'history':history,
                  'next_before':history[-1]['revision'] if len(history)==20 else None}
        if request.args.get('position_id'):
            result['row'],result['original'] = position(value,request.args['position_id'])
        return jsonify(result)
    if request.content_length is None or request.content_length > 32768:
        raise corrections.CorrectionError('Исправление слишком большое.',413)
    body = request.get_json(silent=True)
    fields = {'position_id','changes','expected_version','operation_id','reason'}
    if not isinstance(body,dict) or set(body) != fields:
        raise corrections.CorrectionError('Нужны позиция, значения, причина и версия. Автор определяется сессией PM.bi.',400)
    result, duplicate = corrections.apply(root,eid,actor=actor,**body)
    return jsonify({'ok':True,'version':result['version'],'revision':result['revision'],'duplicate':duplicate})


@blueprint.get('/estimates/review.js')
def review_script():
    from autobot import web_ui as web
    return web.app.send_static_file('estimate_review.js')


@blueprint.get('/estimates/review.css')
def review_css():
    from autobot import web_ui as web
    return web.app.send_static_file('estimate_review.css')


@blueprint.get('/estimates/<eid>/source-preview')
@checked
def source_preview(eid, *, actor):
    from autobot import web_ui as web
    from autobot.source_documents import build_source_file_preview
    from autobot.document_preview_worker import PreviewRejected, run_reader
    corrections.estimate_id(eid)
    metadata = web._load_estimate_original_meta(eid)
    source = web._estimate_original_path(eid,metadata) if metadata else None
    if source is None:
        abort(404)
    page = request.args.get('page','1')
    if not page.isdigit() or len(page)>3 or not 1 <= int(page) <= 250:
        raise corrections.CorrectionError('Укажите страницу PDF от 1 до 250.',400)
    try:
        if source.suffix.casefold() == '.pdf':
            preview = run_reader('pdf-page',path=source,page=int(page))
            preview['image'] = 'data:image/png;base64,' + base64.b64encode(preview.pop('data')).decode('ascii')
        else:
            preview = build_source_file_preview(source)
    except PreviewRejected as error:
        preview = {'kind':'unavailable','message':str(error)}
    response = make_response(render_template('estimate_source_preview.html',preview=preview,title=metadata.get('original_filename'),
                           actual_size=request.args.get('zoom')=='1',original_url='/estimates/'+eid+'/original'))
    response.headers['Content-Security-Policy'] = "default-src 'none'; img-src data:; style-src 'self'; frame-ancestors 'self'; form-action 'self'; base-uri 'none'"
    return response
