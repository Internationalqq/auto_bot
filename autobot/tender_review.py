"""CRM-authenticated review of tender positions, including extracted source files."""
import base64
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

from flask import Blueprint, jsonify, make_response, render_template, request, send_file

from autobot import crm_actor, tender_corrections as corrections
from autobot.document_preview_worker import PreviewRejected, run_reader
from autobot.estimate_parse_worker import EstimateParseRejected, validate_snapshot
from autobot.estimate_publication_recovery import consistent_report
from autobot.market_contract import clean
from autobot.upload_admission import AdmissionError
from autobot.uploaded_review import position, source_page

blueprint = Blueprint('tender_review', __name__)


def checked(function):
    @wraps(function)
    def handler(tid, *args, **kwargs):
        actor = None
        try:
            actor = crm_actor.resolve(request.headers)
            corrections.tender_id(tid)
            response = make_response(function(tid, *args, actor=actor, **kwargs))
        except (corrections.CorrectionError, AdmissionError) as error:
            response = _error(tid, str(error), error.status, actor)
        except (TimeoutError, EstimateParseRejected) as error:
            response = _error(tid, str(error) if not isinstance(error, TimeoutError)
                else 'Документы или отчёт заняты. Повторите сохранение после завершения обработки.', 503, actor)
        except OSError:
            response = _error(tid, 'Не удалось подтвердить сохранение. Повторите запрос с прежним ключом.', 503, actor)
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    return handler


def _error(tid, message, status, actor):
    if actor and request.method == 'GET' and request.path.startswith('/tenders/'):
        return make_response(render_template('review_unavailable.html',tender_id=tid,message=message),status)
    return make_response(jsonify({'ok':False,'message':message}),status)


def paths():
    from autobot import web_ui as web
    return {'reports': web.REPORTS_DIR, 'downloads': web.DATA_DIR/'downloads', 'extracted': web.DATA_DIR/'extracted'}


def _tender(tid):
    from autobot import web_ui as web, main
    meta = web.load_tender_metadata().get(tid, {}) or {}
    return main.Tender(tid, str(meta.get('title') or 'Тендер № '+tid), str(meta.get('url') or ''),
                       str(meta.get('region') or ''), str(meta.get('stage') or ''),
                       main.to_float(meta.get('price_rub')), meta.get('publish_date'))


def _original(value, row, out_paths, tid):
    raw = str(row.get('source_file') or '')
    # Display rows normalize whitespace; archive paths may contain NBSP or
    # repeated spaces. Resolve only a unique, hash-checked manifest entry.
    matches = [item for item in value['manifest']['parse_sources'] if clean(item['path']) == clean(raw)]
    if len(matches) != 1:
        return None
    source = Path(matches[0]['path'])
    roots = [(Path(out_paths[key])/tid).resolve() for key in ('downloads','extracted')]
    if (not source.is_absolute() or source.is_symlink() or not source.is_file() or source.stat().st_size > 128*1024*1024
            or not any(source.resolve().is_relative_to(root) for root in roots)):
        return None
    validate_snapshot(matches)
    return source


@blueprint.get('/tenders/<tid>/review')
@checked
def review_page(tid, *, actor):
    out_paths = paths()
    value = corrections.snapshot(out_paths['reports'],tid)
    row, original = position(value,request.args.get('position_id'))
    source = _original(value,original,out_paths,tid)
    endpoint = '/tenders/'+tid+'/review-source?'+urlencode({'position_id':row['position_id']})
    return render_template('estimate_review.html', estimate_id=tid, meta={'title':_tender(tid).title},
        row=row,original=original,fields=corrections.FIELDS,types={},version=value['version'],revision=value['revision'],
        actor=actor,history=corrections.history(value),has_original=source is not None,
        original_url=endpoint+'&download=1',source_page=source_page(original),workspace_url='/tenders/'+tid,
        source_preview_url=endpoint+'&page='+str(source_page(original)),
        review_config={'kind':'tender','estimateId':tid,'positionId':row['position_id'],'version':value['version'],
                       'fields':list(corrections.FIELDS),'labels':corrections.FIELDS,
                       'values':{name:row.get(name) for name in corrections.FIELDS}})


@blueprint.route('/api/tender/<tid>/corrections',methods=['GET','POST'])
@checked
def correction_api(tid, *, actor):
    out_paths = paths()
    if request.method == 'GET':
        value = corrections.snapshot(out_paths['reports'],tid)
        if request.args.get('operation_id'):
            event = corrections.receipt(value,request.args['operation_id'],actor)
            if event is None:
                return jsonify({'ok':False,'message':'Сохранение с этим ключом ещё не найдено.'}),404
            return jsonify({'ok':True,'version':event['version'],'revision':event['revision']})
        before = request.args.get('before')
        if before is not None and (not before.isdigit() or len(before)>10):
            raise corrections.CorrectionError('Некорректная страница истории.',400)
        history = corrections.history(value,int(before) if before is not None else None)
        result = {'ok':True,'version':value['version'],'revision':value['revision'],'history':history,
                  'next_before':history[-1]['revision'] if len(history)==20 else None}
        if request.args.get('position_id'):
            result['row'],result['original'] = position(value,request.args['position_id'])
        return jsonify(result)
    if request.content_length is None or request.content_length>32768:
        raise corrections.CorrectionError('Исправление слишком большое.',413)
    body = request.get_json(silent=True)
    if not isinstance(body,dict) or set(body) != {'position_id','changes','expected_version','operation_id','reason'}:
        raise corrections.CorrectionError('Нужны позиция, значения, причина и версия. Автор определяется сессией PM.bi.',400)
    event,duplicate = corrections.apply(_tender(tid),out_paths,actor=actor,**body)
    return jsonify({'ok':True,'version':event['version'],'revision':event['revision'],'duplicate':duplicate})


@blueprint.get('/tenders/<tid>/review-source')
@checked
def source_preview(tid, *, actor):
    from autobot.source_documents import build_source_file_preview
    out_paths = paths()
    with consistent_report(out_paths['reports'],tid):
        value = corrections.snapshot_locked(out_paths['reports'],tid)
        _,original = position(value,request.args.get('position_id'))
        source = _original(value,original,out_paths,tid)
        if source is None:
            raise corrections.CorrectionError('Исходный файл недоступен. Откройте список документов тендера.',404)
    if request.args.get('download')=='1':
        return send_file(source,as_attachment=True,download_name=source.name,max_age=0)
    page = request.args.get('page','1')
    if not page.isdigit() or len(page)>3 or not 1<=int(page)<=250:
        raise corrections.CorrectionError('Укажите страницу PDF от 1 до 250.',400)
    try:
        if source.suffix.casefold()=='.pdf':
            preview = run_reader('pdf-page',path=source,page=int(page))
            preview['image'] = 'data:image/png;base64,'+base64.b64encode(preview.pop('data')).decode('ascii')
        else:
            preview = build_source_file_preview(source)
    except PreviewRejected as error:
        preview = {'kind':'unavailable','message':str(error)}
    if _original(value,original,out_paths,tid) is None:
        raise corrections.CorrectionError('Исходный файл изменился во время просмотра. Обновите документы.',409)
    endpoint = '/tenders/'+tid+'/review-source?'+urlencode({'position_id':original['position_id']})
    response = make_response(render_template('estimate_source_preview.html',preview=preview,title=source.name,
        actual_size=request.args.get('zoom')=='1',original_url=endpoint+'&download=1',source_position_id=original['position_id']))
    response.headers['Content-Security-Policy']="default-src 'none'; img-src data:; style-src 'self'; frame-ancestors 'self'; form-action 'self'; base-uri 'none'"
    return response
