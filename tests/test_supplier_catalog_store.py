import json
import time
import pytest

from autobot import supplier_catalog_store as store, supplier_catalog_jobs as jobs


@pytest.fixture
def catalog(tmp_path):
    path=tmp_path/'prices.sqlite3'
    store.initialize(path); store.seed_sources(path)
    source=next(s for s in store.sources(path) if s['enabled'])
    return path,source


def record(name='Кабель АВБШв 4х50',price='517.24'):
    return {'name':name,'price':price,'unit':'м','url':'https://supplier.example/cable',
            'item_key':'cable','evidence':name+' 517,24 руб/м'}


def test_empty_reads_do_not_create_database(tmp_path):
    path=tmp_path/'missing.sqlite3'
    assert store.items(path=path)['total']==0
    assert store.sources(path)==[]
    assert jobs.list_jobs(path=path)==[]
    assert not path.exists()


def test_idempotent_import_and_immutable_history(catalog):
    path,src=catalog; when=time.time()
    for _ in range(2):
        store.save_page(src['id'],src['url'],'<p>Цена</p>',when,[record()],path=path)
    item=store.items(path=path)['items'][0]
    assert item['price_kopecks']==51724
    assert len(store.history(item['id'],path))==1
    store.save_page(src['id'],src['url'],'<p>Новая цена</p>',when+1,[record(price='600.01')],path=path)
    current=store.items(path=path)['items'][0]
    assert current['id']==item['id'] and current['price_kopecks']==60001
    assert len(store.history(item['id'],path))==2
    assert store.observation_reason(item['id'],item['observation_id'],path)
    assert not store.observation_reason(current['id'],current['observation_id'],path)
    assert store.document(item['document_id'],path)['body']=='<p>Цена</p>'


def test_late_old_capture_cannot_overwrite_new_price(catalog):
    path,src=catalog; when=time.time()
    store.save_page(src['id'],src['url'],'new',when,[record(price='700')],path=path)
    store.save_page(src['id'],src['url'],'old',when-100,[record(price='500')],path=path)
    assert store.items(path=path)['items'][0]['price_kopecks']==70000


def test_atomic_page_does_not_leave_partial_prices(catalog):
    path,src=catalog
    with pytest.raises(ValueError):
        store.save_page(src['id'],src['url'],'page',time.time(),[record(),record('Bad','NaN')],path=path)
    assert store.items(path=path)['total']==0


def test_missing_price_not_zero_and_stale_not_current(catalog):
    path,src=catalog
    store.save_page(src['id'],src['url'],'по запросу',time.time()-100*86400,[record(price=None)],path=path)
    item=store.items(path=path)['items'][0]
    assert item['price_kopecks'] is None and item['price_kind']=='on_request'
    assert item['stale']
    assert store.observation_reason(item['id'],item['observation_id'],path)


def test_cyrillic_search_filters_and_schema_repeat(catalog):
    path,src=catalog
    store.save_page(src['id'],src['url'],'page',time.time(),[record()],path=path)
    store.initialize(path); store.seed_sources(path)
    assert store.items(query='кабель АВБШв',source_id=src['id'],path=path)['total']==1
    assert store.items(query="' OR 1=1 --",path=path)['total']==0
    assert store.items(query='несуществующий',path=path)['total']==0


def test_restart_reclaims_page_and_rejects_old_worker(catalog):
    path,src=catalog
    job_id=jobs.enqueue(src['id'],page_budget=1,path=path)
    assert jobs.enqueue(src['id'],path=path)==job_id
    first=jobs.claim(path=path); page=jobs.next_page(job_id,first['owner'],path=path)
    with store.connect(path,write=True) as con:
        con.execute('UPDATE supplier_catalog_jobs SET heartbeat_at=? WHERE id=?',(time.time()-200,job_id))
    second=jobs.claim(path=path)
    assert second['owner']!=first['owner']
    assert not jobs.complete_page(job_id,first['owner'],page,[],path=path)
    with pytest.raises(ValueError):
        store.save_page(src['id'],src['url'],'late',time.time(),[record()],job_id=job_id,owner=first['owner'],path=path)
    assert jobs.next_page(job_id,second['owner'],path=path)['url']==page['url']
    jobs.complete_page(job_id,second['owner'],page,[],path=path)
    jobs.finish(job_id,second['owner'],path=path)
    assert jobs.list_jobs(path=path)[0]['status']=='completed'


def test_cancel_prevents_publication(catalog):
    path,src=catalog
    job_id=jobs.enqueue(src['id'],path=path); job=jobs.claim(path=path)
    assert jobs.cancel(job_id,path=path)
    assert not jobs.owns(job_id,job['owner'],path=path)
    with pytest.raises(ValueError):
        store.save_page(src['id'],src['url'],'late',time.time(),[record()],job_id=job_id,owner=job['owner'],path=path)
    jobs.finish(job_id,job['owner'],path=path)
    assert jobs.list_jobs(path=path)[0]['status']=='canceled'


def test_disappearing_variant_is_withdrawn_without_losing_history(catalog):
    path,src=catalog; when=time.time()
    first=record(); second=dict(record('Другой кабель','600'),item_key='second')
    for r in (first,second): r['url']=src['url']
    store.save_page(src['id'],src['url'],'two variants',when,[first,second],kind='product',path=path)
    store.save_page(src['id'],src['url'],'one variant',when+1,[first],kind='product',path=path)
    values=store.items(price_kind='unknown',path=path)['items']
    missing=next(r for r in values if r['name']=='Другой кабель')
    assert missing['price_kopecks'] is None and missing['price_kind']=='unknown'
    assert len(store.history(missing['id'],path))==2
    assert next(s for s in store.sources(path) if s['id']==src['id'])['item_count']==1


def test_old_context_does_not_replace_new_supplier_or_delivery(catalog):
    path,src=catalog; now=time.time()
    store.save_contact(src['id'],'Новый адрес',src['url'],now,path)
    store.save_contact(src['id'],'Старый адрес',src['url'],now-10,path)
    assert store.source(src['id'],path)['contact_evidence']=='Новый адрес'
    store.save_coverage(src['id'],'Доставка по России',src['url'],now,path=path)
    store.save_coverage(src['id'],'',src['url'],now-10,path=path)
    assert next(s for s in store.sources(path) if s['id']==src['id'])['coverage']['national_delivery']


def test_future_schema_is_not_modified(tmp_path):
    path=tmp_path/'future.sqlite3'
    with store.connect(path,write=True) as con:
        con.execute('CREATE TABLE supplier_catalog_meta(version INTEGER PRIMARY KEY)')
        con.execute('INSERT INTO supplier_catalog_meta VALUES (2)')
    with pytest.raises(ValueError): store.initialize(path)
    with store.connect(path) as con:
        assert con.execute('SELECT version FROM supplier_catalog_meta').fetchall()[0][0]==2
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='supplier_catalog_items'").fetchone()
