import pytest
import requests

from autobot import real_market_scraper as market


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(market, '_SEARCH_PROVIDER_FAILURES', {})
    monkeypatch.setattr(market.time, 'monotonic', lambda: now[0])
    return now


def test_repeated_engine_timeouts_skip_network_then_recover(clock, monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if len(calls) <= 2:
            raise requests.Timeout('timeout')
        return '<html></html>'
    monkeypatch.setattr(market, '_session_get', get)
    for _ in range(2):
        with pytest.raises(requests.Timeout):
            market._search_provider_get('Yahoo', 'https://search.yahoo.com/search', timeout=6)
    with pytest.raises(RuntimeError, match='пауза'):
        market._search_provider_get('Yahoo', 'https://search.yahoo.com/search', timeout=6)
    assert len(calls) == 2
    assert market._search_provider_get('Bing RSS', 'https://www.bing.com/search', timeout=6)
    clock[0] += 301
    assert market._search_provider_get('Yahoo', 'https://search.yahoo.com/search', timeout=6)
    assert 'Yahoo' not in market._SEARCH_PROVIDER_FAILURES


@pytest.mark.parametrize('status', [403, 429])
def test_provider_refusal_backs_off_after_first_response(clock, monkeypatch, status):
    response = requests.Response()
    response.status_code = status
    def get(*args, **kwargs):
        raise requests.HTTPError('unavailable', response=response)
    monkeypatch.setattr(market, '_session_get', get)
    with pytest.raises(requests.HTTPError):
        market._search_provider_get('Yahoo', 'https://search.yahoo.com/search', timeout=6)
    assert market._SEARCH_PROVIDER_FAILURES['Yahoo'] == (1, 1900.0)


def test_position_deadline_and_cancel_do_not_poison_engine_health(clock, monkeypatch):
    def get(*args, **kwargs):
        raise market.SearchBudgetExceeded('position deadline')
    monkeypatch.setattr(market, '_session_get', get)
    with pytest.raises(market.SearchBudgetExceeded):
        market._search_provider_get('Yahoo', 'https://search.yahoo.com/search', timeout=6)
    assert not market._SEARCH_PROVIDER_FAILURES
    token = market._SEARCH_CANCELLED.set(lambda: True)
    try:
        with pytest.raises(market.SearchBudgetExceeded):
            market._search_provider_get('Yahoo', 'https://search.yahoo.com/search', timeout=6)
        assert not market._SEARCH_PROVIDER_FAILURES
    finally:
        market._SEARCH_CANCELLED.reset(token)


def test_empty_successful_search_resets_consecutive_errors(clock, monkeypatch):
    market._SEARCH_PROVIDER_FAILURES['Yahoo'] = (1, 0.0)
    monkeypatch.setattr(market, '_session_get', lambda *args, **kwargs: '')
    assert market._search_provider_get('Yahoo', 'https://search.yahoo.com/search', timeout=6) == ''
    assert not market._SEARCH_PROVIDER_FAILURES


def test_cooling_engine_does_not_block_other_fallbacks(clock, monkeypatch):
    market._SEARCH_PROVIDER_FAILURES['Yahoo'] = (2, 2000.0)
    monkeypatch.setenv('MARKET_SEARCH_PREFERRED_DOMAINS', '0')
    monkeypatch.setattr(market, '_DDG_BLOCKED_UNTIL', 2000.0)
    monkeypatch.setattr(market, '_search_web_ddgs', lambda *args, **kwargs: ([], ''))
    monkeypatch.setattr(market, '_search_web_searx', lambda *args, **kwargs: ([], ''))
    monkeypatch.setattr(market, '_append_market_search_log', lambda *args, **kwargs: None)
    seen = []
    def get(url, **kwargs):
        seen.append(url)
        return ''
    monkeypatch.setattr(market, '_session_get', get)
    offer = market.MarketOffer('Интернет', 'Щебень 20-40', 2000, 'https://supplier.example/stone')
    monkeypatch.setattr(market, '_parse_bing_html', lambda *args, **kwargs: [offer])
    assert market.search_web('Щебень 20-40') == [offer]
    assert seen and all('yahoo' not in url for url in seen)
