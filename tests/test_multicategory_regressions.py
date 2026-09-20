from datetime import datetime, timezone
import json

import pandas as pd
import pytest

from autobot import real_market_scraper as market
from autobot.market_contract import BUNDLE_COLUMN, confirmed_prices
from autobot.market_coverage import annotate_coverage, reconcile_search_history, search_result_reason
from autobot.market_evidence_policy import price_terms_reason
from autobot.market_source_adapters import inspect_source_page
from autobot.tender_detail import _parse_bundle
from autobot.tender_viability import compute_viability_stats
from autobot.market_strategy import build_search_plan, market_query_name
from autobot.market_requirements import technical_specs, technical_conflict


def quote(**extra):
    return dict(price=839, verification='verified', matched_unit='м2',
        url='https://supplier.example/', supplier_evidence='Подрядчик, телефон +74852123456',
        observed_at=datetime.now(timezone.utc).isoformat(),
        evidence='Укладка тротуарной плитки 839 руб/м2', **extra)


@pytest.mark.parametrize('header', ['Цена руб. от', 'Цена от (руб./м²)', 'Цена, руб., от'])
def test_table_lower_bound_survives_capture_and_reload(header):
    evidence = header + ' Ед. Изм. Укладка тротуарной плитки 839 руб. м2'
    assert price_terms_reason({'evidence': evidence})
    saved = quote()
    saved['evidence'] = evidence
    row = {market.COL_NAME:'Укладка тротуарной плитки', 'Ед. изм.':'м2', BUNDLE_COLUMN:json.dumps([saved])}
    assert confirmed_prices(row) == []
    html = f'<h1>Стоимость работ по укладке тротуарной плитки</h1><table><tr><th>Наименование</th><th>{header}</th><th>Ед. изм.</th></tr><tr><td>Укладка тротуарной плитки</td><td>839 руб.</td><td>м2</td></tr></table>'
    found = inspect_source_page(html, saved['url'], name=row[market.COL_NAME], target_unit='м2', position_bucket='works')
    assert found.price == 839 and not found.accepted
    assert 'от' in found.reason


def test_quantity_tier_is_not_mistaken_for_lower_bound_price():
    assert not price_terms_reason({'evidence':'Цена от 20 м3: Песок 900 руб/м3'})


def test_completed_blocked_search_keeps_explainable_state():
    row={'can_auto_price':True,'market_processed':True,'market_status':search_result_reason('HTTP 429')}
    assert annotate_coverage([row])['blocked']==1


@pytest.mark.parametrize('density',['200 г/м2','200 гр./м²','200 грамм/м2','200 граммов/м2'])
def test_density_spelling_preserves_exact_product_requirement(density):
    name='Геотекстиль иглопробивной ' + density
    assert not technical_conflict(name,'Геотекстиль иглопробивной 200 г/м2')
    assert technical_conflict(name,'Геотекстиль иглопробивной 150 г/м2')
    assert '200 г/м²' in market_query_name(name,'material')


def test_corrected_estimate_unit_does_not_scale_market_a_second_time():
    row = {market.COL_NAME:'Устройство покрытий тротуаров из бетонной плитки типа Брусчатка',
        'Ед. изм.':'м2', market.COL_QTY:633.44, market.COL_UNIT_PRICE:63365.49002273301,
        market.COL_SUM:401382.36, BUNDLE_COLUMN:json.dumps([quote()])}
    stats = compute_viability_stats(pd.DataFrame([row]))
    assert stats.comparable_market_total == 531456.16
    assert stats.comparable_estimate_total == 401382.36


def test_supplier_homepage_has_same_acceptance_in_card_and_calculation():
    bundle = json.dumps([quote()])
    sources = _parse_bundle(bundle, name='Укладка тротуарной плитки', unit='м2', estimate_price=900)
    assert len(sources) == 1 and sources[0]['verified']


def test_completed_empty_history_is_visible_without_rewriting_report():
    rows = [{'position_key':'a','can_auto_price':True,'market_processed':False},
            {'position_key':'b','can_auto_price':True,'market_processed':False},
            {'position_key':'c','can_auto_price':True,'market_processed':False}]
    jobs = [{'position_key':'a','status':'completed','payload':{'region':'Ярославль'},
             'result':{'offers':[],'notes':'SearchBudgetExceeded: Лимит времени'},'created_at':10},
            {'position_key':'b','status':'completed','payload':{'region':'Москва'},'created_at':10},
            {'position_key':'c','status':'completed','payload':{'region':'Ярославль'},'created_at':10},
            {'position_key':'c','status':'queued','payload':{'region':'Ярославль'},'created_at':11}]
    reconcile_search_history(rows,jobs,region='Ярославль')
    coverage = annotate_coverage(rows)
    assert coverage['no_quote'] == 1 and coverage['pending'] == 2
    assert 'времени' in rows[0]['price_reason'] and 'market_unit' not in rows[0]


def test_discovery_deadline_leaves_time_for_checking_collected_supplier(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(market.time, 'monotonic', lambda:clock[0])
    monkeypatch.setenv('MARKET_POSITION_TIMEOUT_SEC','90')
    monkeypatch.setenv('MARKET_DISCOVERY_TIMEOUT_SEC','10')
    monkeypatch.setenv('MARKET_INDEX_MIN_SOURCES','1')
    monkeypatch.setattr(market,'_MARKET_SEARCH_LOG_PATH',tmp_path/'events')
    monkeypatch.setattr(market,'_apply_market_consensus_guard',lambda offers,**kw:offers)
    def search(*a,**kw):
        assert market._SEARCH_DEADLINE.get() == 110
        clock[0] = 111
        return [market.MarketOffer('Интернет','Земля растительная',1000,'https://supplier.example/grunt')], 'Лимит времени обнаружения'
    def verify(row, offers, plan, **kw):
        assert market._SEARCH_DEADLINE.get() == 190
        offers[0].verification = 'verified'
        return offers
    monkeypatch.setattr(market,'search_market',search)
    monkeypatch.setattr(market,'_verify_offers',verify)
    offers, _, _ = market.research_position_market('Земля растительная',unit='м3',sources=['web'],max_results=1)
    assert offers[0].verification == 'verified'
    assert market._SEARCH_DEADLINE.get() is None


def test_cable_query_preserves_attached_size_voltage_and_stranding():
    original = 'Кабель силовой с алюминиевыми жилами АВБШВ 4х50ок(№)-660 1000'
    query = market_query_name(original, 'material')
    assert query.startswith('кабель АВБШВ 4х50')
    assert {s['kind']:s['value'] for s in technical_specs(original)} == {
        'dimensions':'4х50','cable_model':'aвбшв','cable_voltage':'660','cable_stranding':'single'}
    exact = 'Кабель АВБШв 4х50 ок (N)-0,66'
    assert not technical_conflict(original,exact)
    for wrong in [exact.replace('4х50','4х10'),exact.replace('ок','мс'),exact.replace('0,66','1 кВ'),exact.replace('АВБШв','АВВГ')]:
        assert technical_conflict(original,wrong)
    assert not any(s['kind']=='dimensions' for s in technical_specs(original.replace('4х50','4х1б')))


def test_cable_table_picks_exact_variant_and_binds_separate_unit():
    html='''<h1>Кабель АВБШв</h1><table><tr><th>Название</th><th>Ед. изм.</th><th>Цена</th></tr>
    <tr><td>Кабель АВБШв 4х10 ок-0,66</td><td>м</td><td>165 р.</td></tr>
    <tr><td>Кабель АВБШв 4х50 мс-0,66</td><td>м</td><td>340 р.</td></tr>
    <tr><td>Кабель АВБШв 4х50 ок-0,66</td><td>м</td><td>415 р.</td></tr></table>'''
    result=inspect_source_page(html,'https://supplier.example/price',name='Кабель АВБШв 4х50ок-660',target_unit='м',position_bucket='materials')
    assert result.accepted and result.price==415 and result.unit=='м'


@pytest.mark.parametrize('name,unit',[
    ('Кабель силовой АВБШВ 4х1бок-660','м'),
    ('Геополотно нетканое полиэфирное, иглопробивное, поверхностная','м2'),
    ('Щит с монтажной панелью 800х600х250 мм, степень защиты','шт'),
])
def test_truncated_specification_requires_original_data(name,unit):
    plan=build_search_plan(name,unit,'ФСБЦ')
    assert not plan.can_auto_price and plan.requirements['issues']
    assert plan.requirements['original_name']==name
    saved=quote()
    saved['matched_unit']=unit
    assert not confirmed_prices({market.COL_NAME:name,'Ед. изм.':unit,BUNDLE_COLUMN:json.dumps([saved])})


def test_explicit_nationwide_delivery_and_local_address_in_layout():
    from autobot.market_source_adapters import source_region_evidence
    html='<div><h5>Доставка по всей РФ</h5><p>Бережная упаковка</p></div>'
    assert source_region_evidence(html,'Ярославская область','materials')
    assert not source_region_evidence(html,'Ярославская область','works')
    assert not source_region_evidence(html.replace('Доставка','Не доставляем'),'Ярославская область','materials')


def test_work_catalogue_is_not_used_to_price_a_cable_product():
    from autobot.supplier_catalogs import catalog_sources
    material=catalog_sources('Кабель АВБШВ 4х50 Ярославль',bucket='materials')
    work=catalog_sources('Протяжка провода в трубе Ярославль',bucket='works')
    assert any('e-kc.ru' in s['url'] for s in material)
    assert all('stroygarant76' not in s['url'] for s in material)
    assert any('stroygarant76' in s['url'] for s in work)


def test_curbs_and_wiring_price_lists_match_grammar_without_losing_operation():
    cases=[('Установка бетонного бордюра','Установка бортовых камней бетонных',300),
           ('Протяжка провода в трубе','Затягивание проводов в трубы',70)]
    for wanted, offered, price in cases:
        html=f'<h1>Цены на работы</h1><table><tr><th>Наименование</th><th>Ед. изм.</th><th>Цена</th></tr><tr><td>{offered}</td><td>м</td><td>{price} руб.</td></tr></table>'
        result=inspect_source_page(html,'https://supplier.example/work',name=wanted,target_unit='м',position_bucket='works')
        assert result.accepted and result.price==price
        check=market.check_offer(name=wanted,unit='м',basis_code='ГЭСН',title=offered,
            snippet=result.evidence,url='https://supplier.example/work',price=price,page_checked=True,source_unit='м')
        assert check.status=='verified',check.reason


def test_checked_candidate_precedes_unopened_search_snippet():
    good=market.MarketOffer('Интернет','Песок мелкий',1000,'https://one.example/sand',evidence='Песок мелкий от 1000 руб/м3',extractor='table-row',matched_unit='м3',region_evidence='Ярославль')
    blocked=market.MarketOffer('Интернет','Песок мелкий',800,'https://two.example/sand',discovery_score=10)
    assert market._diverse_market_offers([blocked,good],1)==[good]


def test_flattened_work_price_cannot_borrow_the_next_numbered_service():
    html='<h1>Цены на электромонтажные работы</h1><div>6 Протяжка кабеля в гофре пог.м 80р. 7 Прокладка провода на клипсах пог.м 130р.</div>'
    wanted='Протяжка провода в трубе'
    result=inspect_source_page(html,'https://supplier.example/work',name=wanted,target_unit='м',position_bucket='works')
    check=market.check_offer(name=wanted,unit='м',basis_code='ГЭСН',title=result.title,
        snippet=result.evidence,url='https://supplier.example/work',price=result.price,
        page_checked=result.accepted,source_unit=result.unit)
    assert check.status!='verified'
    if result.price==80:
        assert 'Прокладка провода' not in result.evidence


@pytest.mark.parametrize('with_area_price',[True,False])
def test_roll_price_and_density_cannot_become_square_metre_price(with_area_price):
    html='''<h1>Геотекстиль иглопробивной 200 гр./м2, 2*50м, 100м2/рул</h1>
    <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
    <meta itemprop="price" content="4490"><meta itemprop="priceCurrency" content="RUB"></div>
    <h3>Цена: 4490 р.</h3>'''
    if with_area_price:
        html+='<h3>Цена за м²: <span>45</span> р.</h3>'
    result=inspect_source_page(html,'https://supplier.example/geotextile',name='Геотекстиль иглопробивной 200 г/м2',target_unit='м2',position_bucket='materials')
    if with_area_price:
        assert result.accepted and result.price==45 and result.unit=='м2'
    else:
        assert not result.accepted
    old=quote()
    old.update(price=4490,evidence='Геотекстиль 200 гр. м2, 100м2 рул 4490.0 руб.')
    assert not confirmed_prices({market.COL_NAME:'Геотекстиль 200 г/м2','Ед. изм.':'м2',BUNDLE_COLUMN:json.dumps([old])})


def test_catalogue_timeout_keeps_already_collected_pages(monkeypatch,tmp_path):
    from autobot import supplier_catalogs as catalogs
    monkeypatch.setattr(catalogs,'catalog_sources',lambda query,**kw:[{'url':'https://one.example','price_page':True},{'url':'https://two.example'}])
    def load(url):
        if 'two.' in url: raise TimeoutError('discovery deadline')
        return '<h1>Песок мелкий</h1><p>800 руб/м3</p>'
    pages=catalogs.discover_catalog_pages('Песок мелкий',load)
    assert len(pages)==1 and pages[0].url=='https://one.example'


def test_http_catalogue_cache_is_reused_without_opening_browser(monkeypatch,tmp_path):
    monkeypatch.setattr(market,'_SOURCE_PAGE_CACHE_DIR',tmp_path)
    calls=[]
    monkeypatch.setattr(market,'_session_get',lambda *a,**kw:calls.append(a[0]) or '<h1>Песок</h1>')
    monkeypatch.setattr(market.WebBrowserFetcher,'fetch_source_page',lambda *a:pytest.fail('Static page already loaded'))
    with market.WebBrowserFetcher() as browser:
        assert browser._load_catalog_page('https://supplier.example/')
        browser.begin_position()
        assert browser._load_catalog_page('https://supplier.example/')
    assert len(calls)==1


def test_outdated_notice_from_contacts_invalidates_quote(monkeypatch,tmp_path):
    monkeypatch.setattr(market,'_MARKET_CACHE_DIR',tmp_path)
    monkeypatch.setattr(market,'_MARKET_SEARCH_LOG_PATH',tmp_path/'log')
    html='<h1>Земля растительная</h1><p>Земля растительная 900 руб/м3</p><a href="/contacts">Контакты</a>'
    contact='<title>Поставщик</title><p>Продажа грунта +7 4852 123456</p><p>Адрес: г. Ярославль, ул. Земляная 1</p><p>Цены указанные на продукцию в данный момент времени не совсем актуальны!</p>'
    monkeypatch.setattr(market,'_fetch_source_page',lambda url,**kw:(contact if '/contacts' in url else html,'','http'))
    row=pd.Series({market.COL_NAME:'Земля растительная','Ед. изм.':'м3',market.COL_QTY:40,'Регион поиска':'Ярославская область'})
    found=market._verify_offers(row,[market.MarketOffer('Интернет','Земля растительная',0,'https://supplier.example/grunt')],build_search_plan('Земля растительная','м3',region='Ярославская область'))
    assert found[0].verification=='candidate' and 'устарели' in found[0].verification_reason
