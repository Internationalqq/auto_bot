import time
import pytest
from autobot import supplier_catalog_store as store,market_price_index as index


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(index,'INDEX_DB',tmp_path/'catalog.sqlite3')
    store.initialize();store.seed_sources()
    from autobot.web_ui import app
    app.config.update(TESTING=True)
    return app.test_client()


def test_catalog_page_and_filters_escape_source_content(client):
    source=next(s for s in store.sources() if s['enabled'])
    store.save_page(source['id'],source['url'],'page',time.time(),[{'name':'<script>alert(1)</script> кабель','url':source['url'],'price':'517.24','unit':'м'}])
    result=client.get('/tenders/suppliers?query=кабель')
    assert result.status_code==200
    html=result.get_data(as_text=True)
    assert '&lt;script&gt;' in html and '<script>alert(1)</script>' not in html
    assert '517,24 ₽' in html and 'Поставщики и цены' in html
    assert client.get('/api/tenders/suppliers/catalog?query=кабель').json['total']==1
    assert client.get('/api/tenders/suppliers/catalog?query=несуществующий').json['total']==0
    assert 'no-store' in result.headers['Cache-Control']


def test_reads_do_not_create_import_jobs_and_posts_are_idempotent(client):
    source=next(s for s in store.sources() if s['enabled'])
    for url in ('/tenders/suppliers','/tenders/suppliers?view=sources','/api/tenders/suppliers/sources'):
        assert client.get(url).status_code==200
    assert client.get('/api/tenders/suppliers/sources').json['jobs']==[]
    url='/api/tenders/suppliers/sources/'+source['id']+'/import'
    first=client.post(url); second=client.post(url)
    assert first.status_code==second.status_code==202
    assert first.json['job_id']==second.json['job_id']
    assert client.post('/api/tenders/suppliers/jobs/'+first.json['job_id']+'/cancel').json['ok']


def test_import_preserves_cross_site_guard_and_config_is_private(client):
    source=next(s for s in store.sources() if s['enabled'])
    url='/api/tenders/suppliers/sources/'+source['id']+'/import'
    assert client.post(url,headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
    assert client.post(url,headers={'Origin':'https://untrusted.example'}).status_code==403
    rows=client.get('/api/tenders/suppliers/sources').json['sources']
    assert all('config_json' not in s and 'config' not in s for s in rows)
    assert client.post('/api/tenders/suppliers/sources/missing/import').status_code==400
