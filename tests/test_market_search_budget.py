from dataclasses import replace
import json
import time

import pytest

from autobot import real_market_scraper as market


def candidate(number, host=None):
    return market.MarketOffer('Интернет', 'Бетон М300', 4700, f'https://{host or str(number) + ".example"}/m300/{number}')


@pytest.fixture
def search(monkeypatch, tmp_path):
    monkeypatch.setattr(market, '_MARKET_CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr(market, '_MARKET_SEARCH_LOG_PATH', tmp_path / 'search.jsonl')
    monkeypatch.setenv('MARKET_POSITION_MAX_PAGES', '12')
    monkeypatch.setenv('MARKET_POSITION_MAX_QUERIES', '3')
    monkeypatch.setenv('MARKET_POSITION_TIMEOUT_SEC', '90')
    monkeypatch.setenv('MARKET_INDEX_MIN_SOURCES', '3')
    monkeypatch.setattr(market, '_apply_market_consensus_guard', lambda offers, **kwargs: offers)
    checked = []
    def verify(row, offers, plan, **kwargs):
        checked.extend(offer.url for offer in offers)
        for offer in offers:
            offer.verification = 'verified' if offer.price == 4700 else 'candidate'
        return offers
    monkeypatch.setattr(market, '_verify_offers', verify)
    return checked


def research():
    return market.research_position_market('Бетон М300', unit='м3', region='Ярославль', sources=['web'], max_results=3)


def test_research_reaches_good_supplier_after_three_bad_results(search, monkeypatch):
    bad = [candidate(i) for i in range(3)]
    for offer in bad:
        offer.price = 1
    good = candidate(4)
    depths = []
    def discover(query, **kwargs):
        depths.append(kwargs['max_results'])
        return bad + [good], ''
    monkeypatch.setattr(market, 'search_market', discover)
    offers, _, _ = research()
    assert offers[0].url == good.url
    assert offers[0].verification == 'verified'
    assert search.count(good.url) == 1
    assert all(depth > 3 for depth in depths)


def test_page_budget_counts_unique_urls_and_keeps_partial_result(search, monkeypatch):
    monkeypatch.setenv('MARKET_POSITION_MAX_PAGES', '4')
    repeated = candidate(0)
    repeated.price = 1
    calls = []
    def discover(query, **kwargs):
        calls.append(query)
        return [repeated, repeated] + [candidate(i, 'one-seller.example') for i in range(1, 10)], ''
    monkeypatch.setattr(market, 'search_market', discover)
    offers, _, error = research()
    assert len(search) == len(set(search)) == 4
    assert len(calls) == 1
    assert 'лимит позиции' in error
    assert any(offer.verification == 'verified' for offer in offers)


def test_three_pages_from_one_seller_do_not_stop_search(search, monkeypatch):
    batch = [candidate(i, 'same.example') for i in range(4)] + [candidate(5), candidate(6)]
    monkeypatch.setattr(market, 'search_market', lambda *args, **kwargs: (batch, ''))
    offers, _, _ = research()
    assert len(search) == 6
    assert {market.urlparse(offer.url).netloc for offer in offers} == {'same.example', '5.example', '6.example'}


def test_deadline_stops_before_next_page_and_restores_context(search, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(market.time, 'monotonic', lambda: clock[0])
    monkeypatch.setenv('MARKET_POSITION_TIMEOUT_SEC', '10')
    monkeypatch.setattr(market, 'search_market', lambda *args, **kwargs: ([candidate(1), candidate(2)], ''))
    original = market._verify_offers
    def slow_verify(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] += 11
        return result
    monkeypatch.setattr(market, '_verify_offers', slow_verify)
    offers, _, error = research()
    assert len(search) == 1
    assert len(offers) == 1
    assert 'Лимит времени' in error
    assert market._SEARCH_DEADLINE.get() is None


def test_query_budget_and_duplicate_links(search, monkeypatch):
    plan = market.build_search_plan('Бетон М300', 'м3', region='Ярославль')
    plan = replace(plan, queries=('query1', 'query2', 'query3', 'query4'))
    monkeypatch.setattr(market, 'build_search_plan', lambda *args, **kwargs: plan)
    monkeypatch.setenv('MARKET_POSITION_MAX_QUERIES', '2')
    calls = []
    offer = candidate(1)
    offer.price = 1
    def discover(query, **kwargs):
        calls.append(query)
        return [offer], ''
    monkeypatch.setattr(market, 'search_market', discover)
    research()
    assert calls == ['query1', 'query2']
    assert len(search) == 1


def test_deeper_discovery_refreshes_shallow_cache(search, monkeypatch):
    market._save_search_cache('web', 'q', 'region', [candidate(1)], requested_results=3)
    calls = []
    def discover(query, **kwargs):
        calls.append(kwargs['max_results'])
        return [candidate(i) for i in range(8)]
    monkeypatch.setattr(market, 'search_web', discover)
    offers, error = market.search_market('q', region='region', sources=['web'], max_results=8)
    assert len(offers) == 8 and not error
    assert calls == [8]
    market.search_market('q', region='region', sources=['web'], max_results=8)
    assert calls == [8]


def test_empty_discovery_expires_soon_and_invalid_cache_is_ignored(search):
    market._save_search_cache('web', 'q', '', [], requested_results=9)
    path = market._search_cache_path('web', 'q', '')
    assert market._load_search_cache('web', 'q', '', required_results=9) == []
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['created_at'] = time.time() - 181
    path.write_text(json.dumps(payload), encoding='utf-8')
    assert market._load_search_cache('web', 'q', '', required_results=9) is None
    payload['created_at'] = 'not a date'
    path.write_text(json.dumps(payload), encoding='utf-8')
    assert market._load_search_cache('web', 'q', '') is None


def test_http_timeout_is_clamped_to_remaining_budget(monkeypatch):
    monkeypatch.setenv('MARKET_DOMAIN_MIN_INTERVAL_SEC', '0')
    calls = []
    class Response:
        text = 'ok'
        encoding = 'utf-8'
        def raise_for_status(self):
            pass
    def get(url, **kwargs):
        calls.append(kwargs['timeout'])
        return Response()
    monkeypatch.setattr(market.requests, 'get', get)
    token = market._SEARCH_DEADLINE.set(time.monotonic() + 2)
    try:
        assert market._session_get('https://supplier.example/price') == 'ok'
        assert 0 < calls[0] <= 2
    finally:
        market._SEARCH_DEADLINE.reset(token)
    token = market._SEARCH_DEADLINE.set(time.monotonic() - 1)
    try:
        with pytest.raises(market.SearchBudgetExceeded):
            market._session_get('https://supplier.example/price')
        assert len(calls) == 1
    finally:
        market._SEARCH_DEADLINE.reset(token)


def test_search_backends_contribute_results_independently(search, monkeypatch):
    import sys
    from types import SimpleNamespace
    calls = []
    class Search:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def text(self, query, **kwargs):
            backend = kwargs['backend']
            calls.append(backend)
            if backend == 'yandex':
                return [{'title': 'Бетон М300', 'href': 'https://one.example/beton', 'body': 'Бетон М300 за 4700 рублей/м3'}]
            return [{'title': 'Бетон М300', 'href': 'https://two.example/beton', 'body': 'Бетон М300 за 4800 рублей/м3'}]
    monkeypatch.delenv('MARKET_SEARCH_BACKENDS', raising=False)
    monkeypatch.setenv('MARKET_SEARCH_BACKEND_LIMIT', '2')
    monkeypatch.setattr(market, '_DDGS_BLOCKED_UNTIL', 0)
    monkeypatch.setitem(sys.modules, 'ddgs', SimpleNamespace(DDGS=Search))
    offers, error = market._search_web_ddgs('Бетон', max_results=3)
    assert calls == ['yandex', 'startpage']
    assert len(offers) == 2 and not error


def test_natural_query_keeps_grade_and_region_without_changing_verification_name():
    plan = market.build_search_plan('Бетон тяжелый М300', 'м3', region='Ярославль')
    assert plan.queries[1] == 'Бетон М300 Ярославль цена за м3'
    assert market.market_query_name('Бетон тяжелый М300') == 'Бетон тяжелый М300'


def test_distance_rate_is_not_a_volume_price():
    from autobot.market_evidence_policy import price_terms_reason
    assert price_terms_reason({'evidence': '/км Миксер 15 руб/м3 Бетон М300'})
    assert not price_terms_reason({'evidence': 'Бетон М300 4700 руб/м3 с доставкой'})


def test_exact_price_row_wins_over_conditional_metadata():
    from autobot.market_source_adapters import inspect_source_page
    html = '''<html><head><title>Бетон М300 от 4500 руб/м3</title>
      <meta property="product:price:amount" content="4500">
      <meta name="description" content="Бетон М300 от 4500 руб/м3"></head>
      <body><h1>Бетон М300</h1><table>
      <tr><th>Наименование</th><th>Цена</th></tr>
      <tr><td>Бетон М300</td><td>4700 руб/м3</td></tr></table></body></html>'''
    result = inspect_source_page(html, 'https://supplier.example/m300', name='Бетон М300', target_unit='м3', position_bucket='materials')
    assert result.accepted and result.price == 4700
