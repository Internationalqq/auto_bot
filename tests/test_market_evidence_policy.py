from datetime import datetime, timezone
import pytest

from autobot.market_evidence_policy import (
    freshness_reason, observed_timestamp, specification_reason, select_independent_offers, price_terms_reason,
)

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize('date', ['', None, 'yesterday', '2026-09-14T12:00:00', float('nan')])
def test_unknown_observation_date_does_not_become_fresh(date):
    assert observed_timestamp(date) is None
    assert freshness_reason({'observed_at': date}, 'works', now=NOW)


def test_avito_freshness_is_rechecked_at_read_time(monkeypatch):
    monkeypatch.delenv('MARKET_INDEX_TTL_DAYS', raising=False)
    offer = {'observed_at': NOW - 13 * 86400, 'url': 'https://www.avito.ru/item'}
    assert freshness_reason(offer, 'materials', now=NOW) == ''
    assert 'устарела' in freshness_reason(offer, 'materials', now=NOW + 2 * 86400)
    offer['observed_at'] = NOW + 86400
    assert 'будущем' in freshness_reason(offer, 'materials', now=NOW)


@pytest.mark.parametrize('wanted,found', [
    ('Разработка грунта с погрузкой на автомобили-самосвалы', 'Ручная разработка грунта от 200 рублей за куб'),
    ('Ручная разработка траншеи', 'Разработка траншеи экскаватором'),
    ('Демонтаж рулонных штор', 'Монтаж рулонных штор'),
    ('Установка трубопровода', 'Демонтаж трубопровода'),
    ('Бетон М300', 'Бетон М200 за куб'),
    ('Бетон B25', 'Бетон В15'),
    ('Щебень фракция 20-40', 'Щебень 5-20 мм'),
])
def test_conflicting_work_method_or_material_spec_is_not_an_equivalent(wanted, found):
    assert specification_reason(wanted, found)


def test_item_number_and_delivery_distance_are_not_product_specifications():
    assert specification_reason('Бетон М300, 20 м3', '2. Бетон M300 — доставка до 50 км') == ''


def test_every_search_query_keeps_requested_region():
    from autobot.market_strategy import build_search_plan
    for name in ['Щебень гранитный', 'Разработка грунта экскаватором']:
        plan = build_search_plan(name, 'м3', region='Ярославль')
        assert plan.queries and all('Ярославль' in query for query in plan.queries)


@pytest.mark.parametrize('evidence', [
    'Бетон М300 — от 5 200 ₽/м³',
    'Бетон М300 — около 5900–6800 руб./м³',
    'Бетон М300 — 5900–6800 руб./м³',
    'Укладка плитки от 150 руб./м2',
])
def test_lower_bounds_and_price_ranges_are_not_exact_quotes(evidence):
    assert price_terms_reason({'evidence': evidence})


@pytest.mark.parametrize('evidence', [
    'Бетон М300 — 6000 руб/м3',
    'Щебень фракция 20-40 — 2650 руб/м3',
    'Укладка плитки — 500 руб/м2',
])
def test_specification_followed_by_price_is_not_a_price_range(evidence):
    assert price_terms_reason({'evidence': evidence}) == ''


def test_editorial_price_is_not_a_supplier_offer():
    assert price_terms_reason({'url': 'https://supplier.example/articles/concrete', 'evidence':'Бетон М300 6000 руб/м3'})


@pytest.mark.parametrize('region,label,matched', [
    ('Ярославль', 'Бетон в Ярославле с доставкой', True),
    ('Ярославль', 'Бетон в Ярославском районе Москвы', False),
    ('Ярославль', 'Доставка в Ярославский', False),
    ('Ярославская область', 'Работаем по Ярославской области', True),
    ('Московская область', 'Московская улица', False),
    ('Москва', 'Доставляем по Москве', True),
    ('Москва', 'Доставка в Московской области', False),
    ('Екатеринбург', 'Экскаватор в Екатеринбурге', True),
])
def test_place_inflections_do_not_merge_different_locations(region, label, matched):
    from autobot.market_source_adapters import region_matches_label
    assert region_matches_label(region, label) is matched


def test_many_pages_of_one_vendor_do_not_outvote_an_independent_quote():
    duplicate = [{'price': 100, 'url': f'https://supplier.example/item/{i}', 'observed_at': NOW}
                 for i in range(10)]
    independent = {'price': 200, 'url': 'https://other.example/item', 'observed_at': NOW}
    result = select_independent_offers(duplicate + [independent])
    assert len(result) == 2
    assert {offer['price'] for offer in result} == {100, 200}


def test_cached_page_keeps_capture_date_and_moscow_quote_is_not_local(monkeypatch, tmp_path):
    import json
    import time
    import pandas as pd
    from autobot import real_market_scraper as market
    from autobot.market_strategy import build_search_plan

    monkeypatch.setattr(market, '_SOURCE_PAGE_CACHE_DIR', tmp_path)
    monkeypatch.setattr(market, '_append_market_search_log', lambda *a, **kw: None)
    url = 'https://supplier.example/concrete'
    page = '<h1>Бетон М300 в Ярославле</h1><p>Бетон М300 — 6000 руб/м3</p>'
    market._save_source_page_cache(url, page_html=page)
    path = market._source_page_cache_path(url)
    cached = json.loads(path.read_text(encoding='utf-8'))
    captured = int(time.time()) - 7200
    cached['created_at'] = captured
    path.write_text(json.dumps(cached), encoding='utf-8')
    row = pd.Series({market.COL_NAME:'Бетон М300', 'Ед. изм.':'м3', 'Регион поиска':'Ярославль'})
    plan = build_search_plan('Бетон М300', 'м3', region='Ярославль')
    fresh = lambda: market.MarketOffer(source='Интернет', title='Бетон М300', price=6000, url=url)
    checked = market._verify_offers(row, [fresh()], plan)
    assert checked[0].verification == 'verified'
    assert observed_timestamp(checked[0].observed_at) == captured
    assert 'Ярославле' in checked[0].region_evidence

    market._save_source_page_cache(url, page_html=page.replace('Ярославле', 'Москве'))
    checked = market._verify_offers(row, [fresh()], plan)
    assert checked[0].verification == 'candidate'
    assert checked[0].rejection_code == 'region_unknown'


@pytest.mark.parametrize('captured', ['invalid', float('nan'), None])
def test_broken_cache_timestamp_is_a_cache_miss(monkeypatch, tmp_path, captured):
    import json
    from autobot import real_market_scraper as market
    monkeypatch.setattr(market, '_SOURCE_PAGE_CACHE_DIR', tmp_path)
    market._source_page_cache_path('https://supplier.example/item').write_text(
        json.dumps({'created_at':captured, 'html':'<h1>Old page</h1>', 'ok':True}), encoding='utf-8')
    assert market._load_source_page_cache('https://supplier.example/item') is None
