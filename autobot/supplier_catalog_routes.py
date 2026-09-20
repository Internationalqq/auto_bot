"""Catalogue UI/API under the existing authenticated /tenders route family."""
from datetime import datetime,timezone
from decimal import Decimal

from flask import Blueprint,jsonify,render_template,request
from autobot import supplier_catalog_store as store,supplier_catalog_jobs as jobs

blueprint=Blueprint('supplier_catalog',__name__)
PAGE='/tenders/suppliers'
API='/api/tenders/suppliers'


def integer(value,default=0):
    try: return max(0,min(100000,int(value)))
    except (ValueError,TypeError): return default


def money(value):
    if value is None: return 'По запросу'
    return f'{Decimal(value)/100:,.2f}'.replace(',',' ').replace('.',',').removesuffix(',00')+' ₽'


def date(value):
    if not value: return 'Ещё не загружен'
    return datetime.fromtimestamp(float(value),timezone.utc).strftime('%d.%m.%Y')


def filters():
    return {key:str(request.args.get(key,'')).strip()[:200] for key in ('query','source_id','bucket','region','price_kind')}


@blueprint.after_request
def no_cache(response):
    response.headers['Cache-Control']='private, no-store'
    return response


@blueprint.get(PAGE)
def page():
    args=filters(); offset=integer(request.args.get('offset'))
    result=store.items(**args,offset=offset)
    suppliers=store.sources()
    return render_template('supplier_catalog.html',catalog=result,suppliers=suppliers,filters=args,
        view='sources' if request.args.get('view')=='sources' else 'prices',jobs=jobs.list_jobs(),
        money=money,date=date,page_url=PAGE)


@blueprint.get(API+'/catalog')
def catalog():
    return jsonify(store.items(**filters(),offset=integer(request.args.get('offset')),limit=integer(request.args.get('limit'),50)))


@blueprint.get(API+'/sources')
def sources():
    # Config/adapter internals are unnecessary to the browser.
    rows=[{k:v for k,v in row.items() if k not in {'config','config_json','coverage_json'}} for row in store.sources()]
    return jsonify(sources=rows,jobs=jobs.list_jobs())


@blueprint.get(API+'/items/<item_id>/history')
def history(item_id):
    return jsonify(history=store.history(item_id))


@blueprint.post(API+'/sources/<source_id>/import')
def import_source(source_id):
    try:
        job_id=jobs.enqueue(source_id)
    except ValueError as error:
        return jsonify(ok=False,message=str(error)),400
    return jsonify(ok=True,job_id=job_id),202


@blueprint.post(API+'/import')
def import_all():
    ids=[jobs.enqueue(src['id']) for src in store.sources() if src['enabled']]
    if not ids: return jsonify(ok=False,message='Нет источников с настроенным импортом'),400
    return jsonify(ok=True,job_ids=ids),202


@blueprint.post(API+'/jobs/<job_id>/cancel')
def cancel(job_id):
    return jsonify(ok=jobs.cancel(job_id))


@blueprint.get(PAGE+'/catalog.css')
def css():
    from flask import current_app
    return current_app.send_static_file('supplier_catalog.css')


@blueprint.get(PAGE+'/catalog.js')
def script():
    from flask import current_app
    return current_app.send_static_file('supplier_catalog.js')
