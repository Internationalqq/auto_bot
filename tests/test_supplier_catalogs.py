import json
import threading
import time

import pandas as pd
import pytest

from autobot import supplier_catalogs as catalogs
from autobot import real_market_scraper as market
from autobot import agent_market_queue as queue, market_web_worker as worker
from autobot.market_contract import confirmed_prices, position_identity, BUNDLE_COLUMN
from autobot.market_strategy import build_search_plan, is_direct_source_url
from autobot.market_source_adapters import inspect_source_page


PRICE_PAGE = '''<html><h1>Бетон в Ярославле</h1>
<p>Производим и доставляем бетон по Ярославлю. Доставка оплачивается отдельно.</p>
<table><tr><th>Бетон</th><th>Цена руб/м3</th></tr>
<tr><td>Бетон В7,5 (М 100)</td><td>5400 руб/м3</td></tr>
<tr><td>Бетон В22,5 (М 300)</td><td>6700 руб/м3</td></tr>
<tr><td>Бетон В30 (М 400)</td><td>7200 руб/м3</td></tr></table></html>'''


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    registry = tmp_path / 'sources.json'
    registry.write_text(json.dumps([{'url':'https://supplier.example/', 'regions':['ярослав'],
                                    'topics':['бетон'], 'price_page':True}]), encoding='utf-8')
    monkeypatch.setattr(catalogs, 'REGISTRY_PATH', registry)
    monkeypatch.setenv('MARKET_BROWSER_CATALOGS', '1')
    monkeypatch.setenv('MARKET_BROWSER_DISCOVERY', '0')
    monkeypatch.setattr(market, '_MARKET_CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr(market, '_SOURCE_PAGE_CACHE_DIR', tmp_path / 'pages')
    monkeypatch.setattr(market, '_MARKET_SEARCH_LOG_PATH', tmp_path / 'events.jsonl')
    monkeypatch.setattr(queue, 'DEFAULT_DB_PATH', tmp_path / 'queue.sqlite3')
    monkeypatch.setattr(market, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(market, '_store_verified_offers_in_index', lambda *a, **kw: 0)
    return tmp_path


def test_registry_requires_both_topic_and_region(isolated):
    assert len(catalogs.catalog_sources('Бетон М300 Ярославль цена')) == 1
    assert not catalogs.catalog_sources('Бетон М300 Челябинск цена')
    assert not catalogs.catalog_sources('Бетон М300 цена')
    assert not catalogs.catalog_sources('Арматура Ярославль цена')
    assert is_direct_source_url('https://supplier.example/')
    assert not is_direct_source_url('https://unregistered.example/')
    assert not is_direct_source_url('https://user:pass@supplier.example/')


def test_discovery_uses_category_for_links_not_for_price_evidence(isolated):
    catalogs.REGISTRY_PATH.write_text(json.dumps([{'url': 'https://supplier.example/plaster/',
        'regions': ['ярослав'], 'topics': ['штукатурка']}]), encoding='utf-8')
    html = '<h1>Штукатурки</h1><a href="/rotband/">Штукатурка Ротбанд 30 кг</a>'
    pages = catalogs.discover_catalog_pages('Штукатурка Ротбанд 30 кг Ярославль', lambda url: html)
    assert [page.url for page in pages] == ['https://supplier.example/rotband/']


def test_links_prefer_exact_product_and_never_leave_site_or_submit_forms():
    html = '''<a href="/concrete/">Бетон</a><a href="/m100/">М100</a>
    <a href="/m300/">М 300</a><a href="/m300/#price">М300</a>
    <a href="https://avito.ru/m300">Бетон М300</a><a href="//evil.example/">Бетон М300</a>
    <a href="https://supplier.example.evil/a">Бетон М300</a>
    <a href="/order/">Бетон М300</a><a href="/cart?add=1">Бетон М300</a>
    <a href="javascript:alert(1)">Бетон М300</a><a href="/price.pdf">Прайс бетона</a>'''
    links = catalogs.catalog_links(html, 'https://supplier.example/', 'Бетон М300', limit=3)
    assert links == ['https://supplier.example/m300/', 'https://supplier.example/concrete/']
    assert catalogs.catalog_links('<a href="/wrong/">Кабель ВВГнг(А)-LS 3х1,5</a>'
        '<a href="/match/">Кабель ВВГнг(А)-LS 3х2,5</a>', 'https://supplier.example/',
        'Кабель ВВГнг(А)-LS 3x2.5 Ярославль', limit=1) == ['https://supplier.example/match/']
    assert catalogs.catalog_links('<a href="/coil/">Кабель ВВГнг(А)-LS 3х2,5 (50 м)</a>'
        '<a href="/metre/">Кабель ВВГнг(А)-LS 3х2,5</a>', 'https://supplier.example/',
        'Кабель ВВГнг(А)-LS 3x2.5 Ярославль', limit=1) == ['https://supplier.example/metre/']
    assert catalogs.catalog_links('<a href="/gravel/20-40/">Фракция 20-40</a>'
        '<a href="/granite/">Гранитный щебень</a>', 'https://supplier.example/',
        'Щебень гранитный фракции 20-40', limit=1) == ['https://supplier.example/granite/']


def test_query_keeps_rock_type_and_fraction():
    for rock in ['гранитный', 'гравийный', 'вторичный', 'известняковый']:
        plan = build_search_plan(f'Щебень {rock} 20-40', 'м3', region='Ярославль')
        assert all(rock in query and '20-40' in query for query in plan.queries)


def test_catalogue_selects_matching_grade_not_first_or_cheapest():
    inspection = inspect_source_page(PRICE_PAGE, 'https://supplier.example/',
                                    name='Бетон М300', target_unit='м3', position_bucket='materials')
    assert inspection.accepted and inspection.price == 6700 and inspection.unit == 'м3'
    assert 'М 300' in inspection.evidence


def test_discovery_never_makes_price_evidence_and_reuses_original_capture(isolated, monkeypatch):
    visits = []
    monkeypatch.setattr(market.WebBrowserFetcher, 'fetch_source_page', lambda self, url: visits.append(url) or PRICE_PAGE)
    with market.WebBrowserFetcher() as browser:
        offers = browser.search_web('Бетон М300 Ярославль', max_results=3)
        assert len(offers) == 1 and offers[0].price == 0
        assert not offers[0].page_checked and offers[0].verification == 'candidate'
    path = market._source_page_cache_path('https://supplier.example/')
    record = market._read_json(path)
    record['created_at'] = time.time() - 600
    market._write_json(path, record)
    with market.WebBrowserFetcher() as browser:
        browser.search_web('Бетон М300 Ярославль', max_results=3)
    assert len(visits) == 1
    assert market._read_json(path)['created_at'] == record['created_at']


def test_registry_changes_do_not_reuse_old_discovery_cache(isolated):
    previous = market.WebBrowserFetcher().cache_source
    catalogs.REGISTRY_PATH.write_text('[]', encoding='utf-8')
    assert market.WebBrowserFetcher().cache_source != previous


def test_catalog_challenge_pauses_subsequent_jobs_until_error_cache_expires(isolated, monkeypatch):
    visits = []
    def blocked(self, url):
        visits.append(url)
        self.last_error = 'страница защиты или капчи'
        return ''
    monkeypatch.setattr(market.WebBrowserFetcher, 'fetch_source_page', blocked)
    for _ in range(2):
        with market.WebBrowserFetcher() as browser:
            assert browser.search_web('Бетон М300 Ярославль', max_results=3) == []
            assert len(browser.catalog_errors) == 1
    assert len(visits) == 1
    path = market._source_page_cache_path('https://supplier.example/')
    record = market._read_json(path)
    record['created_at'] = time.time() - 31 * 60
    market._write_json(path, record)
    with market.WebBrowserFetcher() as browser:
        browser.search_web('Бетон М300 Ярославль', max_results=3)
    assert len(visits) == 2


def test_discovery_defers_product_navigation_to_verification(isolated):
    visits = []
    pages = catalogs.discover_catalog_pages('Бетон М300 Ярославль',
        lambda url: visits.append(url) or PRICE_PAGE + '<a href="/m300/">Бетон М300</a>')
    assert [page.url for page in pages] == ['https://supplier.example/', 'https://supplier.example/m300/']
    assert visits == ['https://supplier.example/']


@pytest.mark.parametrize('html', [
    '<title>Captcha - Brave Search</title>',
    '<p>Please complete the following challenge. Select all squares containing a duck.</p>',
    '<title>Access Denied - Startpage</title>',
    '<p>Проверка браузера перед переходом на сайт</p>',
])
def test_real_challenge_pages_are_not_successful_sources(html):
    assert market._generic_block_reason(html)


def test_catalog_cancellation_stops_before_second_navigation(isolated, monkeypatch):
    stop = threading.Event()
    opened = []
    def fetch(self, url):
        opened.append(url)
        stop.set()
        return PRICE_PAGE + '<a href="/m300/">Бетон М300</a>'
    monkeypatch.setattr(market.WebBrowserFetcher, 'fetch_source_page', fetch)
    plan = build_search_plan('Бетон М300', 'м3', region='Ярославль')
    row = pd.Series({market.COL_NAME:'Бетон М300','Ед. изм.':'м3','Регион поиска':'Ярославль'})
    with market.WebBrowserFetcher() as browser:
        offers, error = market._research_row_market(row, plan, sources=['web'], max_results=3,
                                                   browser_fetcher=browser, cancelled=stop.is_set)
    assert len(opened) == 1 and not offers and 'отменён' in error


def test_browser_catalog_queue_publishes_price_and_repeat_is_idempotent(isolated, monkeypatch):
    estimate, output = isolated / 'estimate.xlsx', isolated / 'market.xlsx'
    row = {market.COL_NAME:'Бетон М300','Ед. изм.':'м3',market.COL_QTY:10,
           market.COL_UNIT_PRICE:8000,market.COL_SUM:80000,'№ п/п':1}
    pd.DataFrame([row]).to_excel(estimate,index=False)
    monkeypatch.setattr(market, 'estimate_path_for_tender', lambda tid: estimate)
    monkeypatch.setattr(market, 'output_path_for_tender', lambda tid: output)
    monkeypatch.setattr(market, 'output_path_for_estimate', lambda path: output)
    monkeypatch.setattr(market, 'load_tender_metadata', lambda: {'12345678':{'region':'Ярославль'}})
    visits = []
    monkeypatch.setattr(market.WebBrowserFetcher, 'fetch_source_page', lambda self, url: visits.append(url) or PRICE_PAGE)
    monkeypatch.setattr(market, 'search_web', lambda *a, **kw: [])
    monkeypatch.setattr(market.AvitoBrowserFetcher, 'fetch', lambda *a: pytest.fail('Avito opened'))
    job = queue.enqueue_jobs('12345678',[{'position_key':position_identity(row),'name':'Бетон М300',
                                         'unit':'м3','region':'Ярославль','max_attempts':1}])['created'][0]
    done = worker.run_once('test-catalog')
    assert done['id'] == job['id'] and done['status'] == 'completed'
    assert confirmed_prices(pd.read_excel(output).iloc[0]) == [6700], [(o['price'], o['verification_reason']) for o in done['result']['offers']]
    original = output.read_bytes()
    assert worker.run_once('test-catalog') is None
    assert output.read_bytes() == original and visits == ['https://supplier.example/']
    assert queue.pending_deliveries() == []


def test_single_item_defaults_to_web_and_ignores_avito_switch(isolated, monkeypatch):
    from autobot import item_research
    monkeypatch.setenv('MARKET_AVITO_BROWSER', '0')
    monkeypatch.setattr(market.WebBrowserFetcher, 'fetch_source_page', lambda self, url: PRICE_PAGE)
    monkeypatch.setattr(market, 'search_web', lambda *a, **kw: [])
    result = item_research.research_item('Бетон М300',unit='м3',region='Ярославль')
    assert result.sources == ['web']
    assert any(offer.verification == 'verified' and offer.price == 6700 for offer in result.offers), [(o.price, o.verification_reason) for o in result.offers]
