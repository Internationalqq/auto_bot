import time

import pytest

from autobot import real_market_scraper as market
from autobot.browser_search_results import google_result_links


def test_browser_launch_retry_reuses_driver_and_always_stops_it(monkeypatch):
    from types import SimpleNamespace
    import playwright.sync_api
    calls=[]
    def launch(**kwargs):
        calls.append('launch')
        raise RuntimeError('Chromium unavailable')
    driver=SimpleNamespace(chromium=SimpleNamespace(launch=launch),stop=lambda:calls.append('stop'))
    def start():
        calls.append('start')
        return driver
    monkeypatch.setattr(playwright.sync_api,'sync_playwright',lambda:SimpleNamespace(start=start))
    with market.WebBrowserFetcher() as browser:
        for _ in range(2):
            with pytest.raises(RuntimeError,match='Chromium unavailable'):browser._ensure_page()
    assert calls==['start','launch','launch','stop']


GOOGLE = '''<html><body><main>
<div><a href="https://supplier.example/m300/"><h3>Бетон М300 в Ярославле</h3></a>
<span>Бетон М300 — 6 700 руб/м3, доставка отдельно.</span></div>
<div><a href="/url?q=https%3A%2F%2Fother.example%2Fm300%2F"><h3>Бетон М300 купить</h3></a>
<span>Цена 6 750 руб/м3</span></div>
<a href="/goto?url=opaque"><h3>Недоступный для разбора переход</h3></a>
<a href="javascript:alert(1)"><h3>Небезопасная ссылка</h3></a>
<a href="https://user:secret@hidden.example/"><h3>Не публичная ссылка</h3></a>
<a href="/search?q=m300"><h3>Повторный поиск</h3></a>
<a href="https://supplier.example/m300/#part"><h3>Дубликат</h3></a>
</main></body></html>'''


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('MARKET_BROWSER_DISCOVERY', '1')
    monkeypatch.setenv('MARKET_BROWSER_CATALOGS', '0')
    monkeypatch.setattr(market, '_MARKET_CACHE_DIR', tmp_path / 'search')
    monkeypatch.setattr(market, '_SOURCE_PAGE_CACHE_DIR', tmp_path / 'pages')
    monkeypatch.setattr(market, '_MARKET_SEARCH_LOG_PATH', tmp_path / 'events.jsonl')
    monkeypatch.setattr(market, '_WEB_BROWSER_BLOCKED_UNTIL', 0)


def test_each_position_receives_page_budget_without_resetting_blocked_cache(monkeypatch):
    browser = market.WebBrowserFetcher()
    market._save_source_page_cache('https://supplier.example/', error='CAPTCHA', method='playwright')
    browser.source_browser_count = 8
    browser.source_domain_failures['supplier.example'] = 2
    browser._catalog_pages['https://supplier.example/'] = ''
    browser.begin_position()
    assert browser.source_browser_count == 0 and not browser.source_domain_failures
    assert not browser._catalog_pages
    monkeypatch.setattr(browser, 'fetch_source_page', lambda url: pytest.fail('Repeated a blocked page'))
    assert browser._load_catalog_page('https://supplier.example/') == ''


def test_google_result_links_do_not_mix_sellers_or_follow_opaque_redirects():
    rows = google_result_links(GOOGLE)
    assert [row['url'] for row in rows] == ['https://supplier.example/m300/', 'https://other.example/m300/']
    assert '6 700' in rows[0]['snippet'] and '6 750' not in rows[0]['snippet']
    assert '6 750' in rows[1]['snippet'] and '6 700' not in rows[1]['snippet']
    assert len(google_result_links(GOOGLE, limit=1)) == 1


def test_web_context_is_lazy_separate_and_does_not_reset_avito_limits(monkeypatch):
    def forbidden():
        pytest.fail('A web job touched the Avito guard')
    monkeypatch.setattr(market, '_reset_avito_run_budget', forbidden)
    monkeypatch.setenv('MARKET_AVITO_USER_DATA_DIR', 'private-existing-profile')
    monkeypatch.delenv('MARKET_USER_AGENT', raising=False)
    with market.WebBrowserFetcher() as browser:
        assert browser.enabled and browser.headless
        assert browser.user_data_dir == '' and browser.user_agent == ''
        assert browser._playwright is None


def test_browser_discovery_stays_unverified_and_has_a_separate_cache(monkeypatch):
    called = []
    browser = market.WebBrowserFetcher()
    monkeypatch.setattr(browser, 'fetch_source_page', lambda url: called.append(url) or GOOGLE)
    def forbidden(*args, **kwargs):
        pytest.fail('Unneeded HTTP search')
    monkeypatch.setattr(market, 'search_web', forbidden)
    # An earlier empty HTTP cache must not suppress the new browser route.
    market._save_search_cache('web', 'Бетон М300', 'Ярославль', [], requested_results=2)
    for _ in range(2):
        offers, error = market.search_market('Бетон М300', region='Ярославль', sources=['web'],
                                            max_results=2, browser_fetcher=browser)
        assert len(offers) == 2 and not error
        assert all(not offer.page_checked and offer.verification != 'verified' for offer in offers)
        assert all(offer.discovery_engine == 'Google browser' for offer in offers)
    assert len(called) == 1 and '%D0%AF' in called[0]


@pytest.mark.parametrize('error', ['браузер не установлен', 'страница защиты или капчи'])
def test_browser_unavailable_uses_http_without_repeating_challenge(monkeypatch, error):
    browser = market.WebBrowserFetcher()
    visits = []
    def failed(url):
        visits.append(url)
        browser.last_error = error
        return ''
    monkeypatch.setattr(browser, 'fetch_source_page', failed)
    fallback = market.MarketOffer('Интернет', 'Бетон М300', 6700, 'https://supplier.example/m300/')
    monkeypatch.setattr(market, 'search_web', lambda *a, **kw: [fallback])
    for query in ['Бетон М300', 'Бетон М300 прайс']:
        offers, error_text = market.search_market(query, sources=['web'], max_results=3, browser_fetcher=browser)
        assert offers == [fallback] and not error_text
    assert len(visits) == 1
    if 'капчи' in error:
        with market.WebBrowserFetcher() as another:
            monkeypatch.setattr(another, 'fetch_source_page', lambda url: pytest.fail('CAPTCHA cooldown ignored'))
            assert another.search_web('Бетон М300', max_results=3) == []


def test_disabled_browser_keeps_the_previous_search_path(monkeypatch):
    monkeypatch.setenv('MARKET_WEB_BROWSER', '0')
    with market.WebBrowserFetcher() as browser:
        assert not browser.enabled
        monkeypatch.setattr(browser, 'search_web', lambda *a, **kw: pytest.fail('Disabled browser opened'))
        monkeypatch.setattr(market, 'search_web', lambda *a, **kw: [])
        assert market.search_market('Бетон М300', sources=['web'], max_results=3, browser_fetcher=browser) == ([], '')


def test_production_default_renders_sources_without_polling_blocked_google(monkeypatch):
    monkeypatch.delenv('MARKET_BROWSER_DISCOVERY', raising=False)
    monkeypatch.delenv('MARKET_BROWSER_CATALOGS', raising=False)
    with market.WebBrowserFetcher() as browser:
        assert browser.enabled and browser.catalogs_enabled and not browser.google_enabled
        monkeypatch.setattr(browser, 'fetch_source_page', lambda *a: pytest.fail('Google should not be opened by default'))
        assert browser.search_web('Бетон М300', max_results=3) == []


def test_discovered_supplier_uses_the_same_browser_fallback(monkeypatch):
    browser = market.WebBrowserFetcher()
    page = '<html><h1>Бетон М300</h1><p>6 700 руб/м3. Ярославль.</p></html>'
    opened = []
    def unavailable(*args, **kwargs):
        raise RuntimeError('HTTP unavailable')
    monkeypatch.setattr(market, '_session_get', unavailable)
    monkeypatch.setattr(browser, 'fetch_source_page', lambda url: opened.append(url) or page)
    result = market._fetch_source_page('https://supplier.example/m300/', timeout=10, browser_fetcher=browser)
    assert result == (page, '', 'playwright')
    assert opened == ['https://supplier.example/m300/']


def test_expired_budget_stops_before_opening_browser(monkeypatch):
    with market.WebBrowserFetcher() as browser:
        monkeypatch.setattr(browser, '_ensure_page', lambda: pytest.fail('Expired browser opened'))
        token = market._SEARCH_DEADLINE.set(time.monotonic() - 1)
        try:
            with pytest.raises(market.SearchBudgetExceeded):
                browser.search_web('Бетон М300', max_results=3)
        finally:
            market._SEARCH_DEADLINE.reset(token)


def test_challenge_ends_browser_session_for_that_query(monkeypatch):
    browser = market.WebBrowserFetcher()
    monkeypatch.setattr(browser, 'fetch_source_page', lambda url: '')
    browser.last_error = 'страница защиты или капчи'
    assert browser.search_web('Бетон М300', max_results=3) == []
    assert browser.search_unavailable
    assert market._WEB_BROWSER_BLOCKED_UNTIL > time.monotonic()


def test_listing_heading_cannot_hide_a_conflicting_concrete_characteristic():
    from autobot.market_evidence_policy import specification_reason
    evidence = ('Бетон М300. Цена 3 900 ₽ за м³. Характеристики. '
                'Марка бетона / Класс прочности: м200 / в15. Ярославль.')
    assert 'марка бетона' in specification_reason('Бетон М300', evidence)
    assert not specification_reason('Бетон М300 В22,5',
        'Бетон М300. Марка бетона / Класс прочности: м300 / в22.5. Цена 6700 ₽/м3.')
    assert not specification_reason('Бетон М300',
        'Бетон М300 — 6700 ₽/м3; также доступны М200, М250.')


def test_queue_status_distinguishes_server_from_external_browser():
    from autobot.web_ui import _agent_market_compact_reason
    assert _agent_market_compact_reason({'status': 'queued', 'job_mode': 'web'}, []) == 'Ждёт серверного поиска'
    assert _agent_market_compact_reason({'status': 'leased', 'job_mode': 'web'}, []) == 'Проверяются страницы поставщиков'
    assert 'подключения' in _agent_market_compact_reason({'status': 'queued', 'job_mode': 'avito'}, [])
    assert 'проверяет' in _agent_market_compact_reason({'status': 'leased', 'job_mode': 'avito'}, [])
