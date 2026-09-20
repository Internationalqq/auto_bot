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
