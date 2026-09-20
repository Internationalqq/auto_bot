import time
import pytest
from autobot import supplier_catalog_store as store, supplier_catalog_jobs as jobs
from autobot.supplier_catalog_worker import run_once


@pytest.fixture
def catalog(tmp_path):
    path=tmp_path/'catalog.sqlite3'; store.initialize(path);store.seed_sources(path)
    src=next(s for s in store.sources(path) if 'gamma-beton' in s['url'])
    return path,src


def table(name='Бетон В15',price=4500):
    return f'<table><tr><th>Наименование</th><th>Ед. изм.</th><th>Цена</th></tr><tr><td>{name}</td><td>м3</td><td>{price} руб</td></tr></table>'


def test_budget_resume_walks_only_remaining_pages(catalog):
    path,src=catalog; root=src['url']; seen=[]; observed=time.time()
    def reader(url):
        seen.append(url)
        body=table()+('<a href="?page=2">2</a>' if url==root else '')
        return body,observed,url
    job_id=jobs.enqueue(src['id'],page_budget=1,path=path)
    first=run_once(path=path,reader=reader)
    assert first['status']=='paused' and first['processed']==1 and first['discovered']==2
    assert jobs.enqueue(src['id'],page_budget=1,path=path)==job_id
    last=run_once(path=path,reader=reader)
    assert last['status']=='completed' and last['processed']==2
    assert seen==[root,root+'?page=2']
    count=store.items(path=path)['total']
    jobs.enqueue(src['id'],path=path);run_once(path=path,reader=reader)
    assert store.items(path=path)['total']==count


def test_cancellation_during_fetch_cannot_publish(catalog):
    path,src=catalog;job_id=jobs.enqueue(src['id'],path=path)
    def reader(url):
        jobs.cancel(job_id,path=path)
        return table(),time.time(),url
    result=run_once(path=path,reader=reader)
    assert result['status']=='canceled'
    assert store.items(path=path)['total']==0


def test_failed_page_does_not_erase_previous_success(catalog):
    path,src=catalog;root=src['url']
    def reader(url):
        if url!=root: raise ValueError('Сайт вернул HTTP 502')
        return table()+'<a href="?page=2">2</a>',time.time(),url
    jobs.enqueue(src['id'],path=path)
    result=run_once(path=path,reader=reader)
    assert result['status']=='partial' and result['processed']==1 and result['errors']==1
    assert store.items(path=path)['items'][0]['price_kopecks']==450000


def test_worker_recovers_interrupted_fetch_after_restart(catalog):
    path,src=catalog;job_id=jobs.enqueue(src['id'],path=path)
    dead=jobs.claim(path=path);jobs.next_page(job_id,dead['owner'],path=path)
    with store.connect(path,write=True) as con:
        con.execute('UPDATE supplier_catalog_jobs SET heartbeat_at=? WHERE id=?',(time.time()-200,job_id))
    result=run_once(path=path,reader=lambda url:(table(),time.time(),url))
    assert result['status']=='completed' and result['processed']==1
    assert store.items(path=path)['total']==1


def test_production_startup_consumes_durable_catalog_job(tmp_path,monkeypatch):
    import runpy,sys,threading
    from pathlib import Path
    from types import SimpleNamespace
    from autobot import market_price_index as index,supplier_catalog_worker as worker

    monkeypatch.setattr(index,'INDEX_DB',tmp_path/'catalog.sqlite3')
    monkeypatch.setenv('SUPPLIER_CATALOG_WORKER','1')
    monkeypatch.setattr(worker,'_thread',None)
    monkeypatch.setattr(worker,'_stop',threading.Event())
    monkeypatch.setattr(worker,'SiteReader',lambda source:lambda url:(table(),time.time(),url))
    # Keep unrelated recovery jobs out of this isolated production-startup check.
    stubs={
        'autobot.estimate_publication_recovery':dict(recover_pending_publications=lambda path:None),
        'autobot.report_prompt':dict(REPORTS_DIR=tmp_path),
        'autobot.agent_market_delivery':dict(start_delivery_recovery=lambda:None),
        'autobot.market_web_worker':dict(start_web_worker=lambda:None),
        'autobot.main_job_runtime':dict(start_recovery=lambda *args,**kwargs:None),
        'autobot.web_ui':dict(DATA_DIR=tmp_path,_parse_env=lambda:{}),
    }
    for name,values in stubs.items(): monkeypatch.setitem(sys.modules,name,SimpleNamespace(**values))
    store.initialize();store.seed_sources()
    source=next(s for s in store.sources() if 'gamma-beton' in s['url'])
    job_id=jobs.enqueue(source['id'])
    config=runpy.run_path(str(Path(__file__).parents[1]/'tools/gunicorn_conf.py'))
    try:
        config['post_worker_init'](None)
        running=worker._thread
        assert running is not None and running.is_alive()
        config['post_worker_init'](None)
        assert worker._thread is running
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            job=next(j for j in jobs.list_jobs() if j['id']==job_id)
            if job['status']=='completed': break
            time.sleep(.02)
        assert job['status']=='completed'
        assert store.items()['items'][0]['price_kopecks']==450000
    finally:
        worker._stop.set()
        if worker._thread is not None: worker._thread.join(timeout=2)
