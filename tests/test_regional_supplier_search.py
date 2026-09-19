import json

import pandas as pd
import pytest

from autobot import real_market_scraper as market
from autobot.market_contract import offers_for_row, BUNDLE_COLUMN
from autobot.market_source_adapters import inspect_source_page, source_region_evidence
from autobot.market_strategy import is_direct_source_url, build_search_plan
from autobot.supplier_evidence import supplier_context_links, quantity_terms_reason


TABLE = '''<h1>Бетон в Рыбинске</h1><a href="tel:+79301142303">Телефон завода</a>
<table><tr><td>№</td><td>Марка бетона</td><td>Цена (руб.куб/м) без доставки с НДС</td><td>Цена от 100 куб/м</td></tr>
<tr><td>1</td><td>Бетон М150 В12,5</td><td>5700</td><td>звоните</td></tr>
<tr><td>2</td><td>Бетон М200 В15</td><td>5900</td><td>звоните</td></tr>
<tr><td>3</td><td>Бетон М350 В25</td><td><s>7500</s>6900</td><td>звоните</td></tr></table>'''


def inspect(html=TABLE, quantity=10, name='Бетон М200 В15'):
    return inspect_source_page(html, 'https://supplier.example/', name=name,
        target_unit='м3', position_bucket='materials', quantity=quantity)


def test_numbered_price_list_binds_product_and_currency_header():
    result = inspect()
    assert result.accepted and result.price == 5900 and result.unit == 'м3'
    assert result.title == 'Бетон М200 В15'
    assert 'без доставки' in result.evidence
    assert inspect(name='Бетон М350 В25').price == 6900


def test_wholesale_quote_does_not_use_small_batch_price():
    result=inspect(quantity=110)
    assert not result.accepted and result.price == 5900
    assert result.status == 'quantity-terms' and 'объём сметы' in result.reason
    assert result.quantity_terms
    assert quantity_terms_reason(list(result.quantity_terms), 1.1, '100 м3')


def test_sand_tier_preserves_unit_and_uses_matching_lot():
    html='''<h1>Карьерный песок</h1><h3>Цена за 1 куб (м3)</h3><table>
    <tr><td>Количество /</td><td>7м3</td><td>20м3</td><td>опт от 100м3</td></tr>
    <tr><td>Мелкий (МКР 1,5 — 2,0)</td><td>1170 руб.</td><td>800 руб.</td><td>по договоренности</td></tr></table>'''
    assert inspect(html,quantity=7,name='Песок мелкий').price == 1170
    result=inspect(html,quantity=20,name='Песок мелкий')
    assert result.accepted and result.price == 800
    result=inspect(html,quantity=932,name='Песок мелкий')
    assert not result.accepted and 'объём сметы' in result.reason


def test_two_products_in_same_row_do_not_share_price():
    html='''<h1>Бетон и раствор</h1><table><tr><td>Марка бетона</td><td>Цена без доставки</td><td>Марка раствора</td><td>Цена</td></tr>
    <tr><td>В15 (М200)</td><td>6150 руб/м3</td><td>М100</td><td>5200 руб/м3</td></tr></table>'''
    result=inspect(html)
    assert result.accepted and result.price == 6150
    assert '5200' not in result.evidence


def test_oblast_accepts_explicit_city_address_but_never_guesses_delivery():
    page='<h1>Бетон от производителя</h1><a href="tel:+74852331221">Телефон</a><table><tr><td>Юридический адрес</td><td>город Ярославль, ул. Урицкого, дом 25</td></tr></table>'
    evidence=source_region_evidence(page,'Ярославская область','materials')
    assert 'Поставщик в регионе' in evidence and 'Урицкого' in evidence
    assert not source_region_evidence(page,'Рыбинск','materials')
    assert not source_region_evidence(page.replace('город Ярославль','Ярославский район, Москва'),'Ярославская область','materials')
    assert not source_region_evidence('<title>Не доставляем в Ярославль</title>','Ярославль','materials')


def test_reference_sites_rejected_at_discovery_and_read_boundary():
    for host in ['fsnb2022.ru','fgisrf.ru','classinform.ru','files.stroyinf.ru']:
        url=f'https://{host}/fsscm/123.html'
        offer=market.MarketOffer('Интернет','Камни бортовые БР бетон',4845,url,'Камни бортовые БР бетон 4845 руб/м3')
        assert not market._candidate_decision(offer,'Камни бортовые БР бетон')[0]
        assert not is_direct_source_url(url)


def test_supplier_homepage_without_snippet_price_is_discovered_not_verified():
    offer=market.MarketOffer('Интернет','Бетон М200 Рыбинск — завод',0,'https://new-supplier.example/','Производство и продажа бетона М200')
    assert market._candidate_decision(offer,'Бетон М200 Рыбинск')[0]
    assert not is_direct_source_url(offer.url)
    assert is_direct_source_url(offer.url,supplier_evidence='Завод · телефон +79301142303')
    assert offer.verification == 'candidate'


def test_context_links_are_same_site_bounded_and_read_only():
    html='''<a href="/contacts">Контакты</a><a href="https://evil.example/delivery">Доставка</a>
    <a href="/delivery?order=1">Доставка</a><a href="/delivery">Доставка</a><a href="/company">Реквизиты</a>'''
    assert supplier_context_links(html,'https://supplier.example/product') == ['https://supplier.example/contacts','https://supplier.example/delivery']


def test_verified_price_rechecked_against_quantity_after_reload():
    result=inspect()
    offer={'verification':'verified','price':5900,'url':'https://supplier.example/m200',
        'matched_unit':'м3','observed_at':market.datetime.now(market.timezone.utc).isoformat(),
        'evidence':result.evidence,'quantity_terms':list(result.quantity_terms)}
    row={market.COL_NAME:'Бетон М200 В15','Ед. изм.':'м3',market.COL_QTY:110,BUNDLE_COLUMN:json.dumps([offer])}
    assert offers_for_row(row)[0]['verification']=='candidate'
    assert 'объём сметы' in offers_for_row(row)[0]['verification_reason']


def test_region_can_come_from_contacts_and_evidence_keeps_both_urls(monkeypatch,tmp_path):
    monkeypatch.setattr(market,'_MARKET_CACHE_DIR',tmp_path)
    monkeypatch.setattr(market,'_MARKET_SEARCH_LOG_PATH',tmp_path/'log')
    page=TABLE+'<a href="/contacts">Контакты</a>'
    contacts='<address>Ярославская область, г. Рыбинск, ул. Заводская 1</address>'
    visited=[]
    def fetch(url,**kw):
        visited.append(url)
        return (contacts if url.endswith('contacts') else page),'','requests'
    monkeypatch.setattr(market,'_fetch_source_page',fetch)
    row=pd.Series({market.COL_NAME:'Бетон М200 В15','Ед. изм.':'м3',market.COL_QTY:10,'Регион поиска':'Ярославская область'})
    offer=market.MarketOffer('Интернет','Бетон М200 В15',0,'https://supplier.example/')
    result=market._verify_offers(row,[offer],build_search_plan('Бетон М200 В15','м3',region='Ярославская область'))
    assert result[0].verification=='verified', result[0].verification_reason
    assert visited==['https://supplier.example/','https://supplier.example/contacts']
    assert result[0].region_source_url.endswith('/contacts') and result[0].price==5900
    assert 'без доставки' in result[0].delivery_terms


def test_discovery_leaves_time_to_verify_useful_batch(monkeypatch):
    offers=[market.MarketOffer('Интернет','Бетон М200 Рыбинск',5900,f'https://s{i}.example/m200','Бетон М200 5900 руб/м3') for i in range(3)]
    monkeypatch.setattr(market,'_session_get',lambda *a,**kw:'page')
    monkeypatch.setattr(market,'_parse_bing_rss',lambda *a,**kw:offers)
    monkeypatch.setattr(market,'_parse_yahoo_html',lambda *a,**kw:[])
    monkeypatch.setattr(market,'_search_web_ddgs',lambda *a,**kw:pytest.fail('Useful batch must be verified before another slow provider'))
    monkeypatch.setattr(market,'_append_market_search_log',lambda *a,**kw:None)
    assert len(market.search_web('Бетон М200 Рыбинск',max_results=9))==3


def test_local_index_keeps_volume_terms_and_rechecks_other_tender(monkeypatch,tmp_path):
    from autobot import market_price_index as index
    for name,value in [('REPO_ROOT',tmp_path),('INDEX_ROOT',tmp_path/'index'),
                       ('INDEX_DB',tmp_path/'index/market.sqlite3'),('AUDIT_ROOT',tmp_path/'index/audit')]:
        monkeypatch.setattr(index,name,value)
    inspection=inspect()
    offer={'verification':'verified','price':5900,'url':'https://supplier.example/m200',
        'matched_unit':'м3','observed_at':market.datetime.now(market.timezone.utc).isoformat(),
        'evidence':inspection.evidence,'quantity_terms':list(inspection.quantity_terms),
        'supplier_evidence':'РБУ Рыбинск · телефон +79301142303',
        'delivery_terms':'Цена без доставки','region_source_url':'https://supplier.example/contacts'}
    assert index.record_verified_offers(tender_id='copy',name='Бетон М200 В15',unit='м3',offers=[offer])==1
    row=pd.Series({market.COL_NAME:'Бетон М200 В15','Ед. изм.':'м3',market.COL_QTY:110})
    result=market._offers_from_local_index(row,max_results=3)
    assert result and result[0].verification=='candidate'
    assert 'объём сметы' in result[0].verification_reason
    assert result[0].supplier_evidence == offer['supplier_evidence']
    assert result[0].region_source_url == offer['region_source_url']


def test_rejected_search_batch_does_not_hide_next_free_engine(monkeypatch):
    old=[market.MarketOffer('Интернет','Бетон М200 Рыбинск',5900,f'https://s{i}.example/m200','Бетон М200 5900 руб/м3') for i in range(3)]
    fresh=market.MarketOffer('Интернет','Бетон М200 Рыбинск',6100,'https://new.example/m200','Бетон М200 6100 руб/м3')
    monkeypatch.setenv('MARKET_SEARCH_PREFERRED_DOMAINS','0')
    monkeypatch.setattr(market,'_session_get',lambda *a,**kw:'page')
    monkeypatch.setattr(market,'_parse_bing_rss',lambda *a,**kw:old)
    monkeypatch.setattr(market,'_parse_yahoo_html',lambda *a,**kw:[])
    monkeypatch.setattr(market,'_search_web_searx',lambda *a,**kw:([],''))
    monkeypatch.setattr(market,'_search_web_ddgs',lambda *a,**kw:([fresh],''))
    monkeypatch.setattr(market,'_append_market_search_log',lambda *a,**kw:None)
    found=market.search_web('Бетон М200 Рыбинск',max_results=3,exclude_urls={o.url for o in old})
    assert [o.url for o in found]==[fresh.url]


def test_supplier_catalogue_opens_existing_matching_product_link(monkeypatch,tmp_path):
    monkeypatch.setattr(market,'_MARKET_CACHE_DIR',tmp_path)
    monkeypatch.setattr(market,'_MARKET_SEARCH_LOG_PATH',tmp_path/'log')
    visits=[]
    def fetch(url,**kw):
        visits.append(url)
        page=TABLE if url.endswith('/m200') else '<h1>Каталог бетона</h1><a href="/m200">Бетон М200 В15</a><a href="https://evil.example/m200">Бетон М200</a>'
        return page,'','requests'
    monkeypatch.setattr(market,'_fetch_source_page',fetch)
    row=pd.Series({market.COL_NAME:'Бетон М200 В15','Ед. изм.':'м3',market.COL_QTY:10})
    source=market.MarketOffer('Интернет','Бетон М200',0,'https://supplier.example/')
    result=market._verify_offers(row,[source],build_search_plan('Бетон М200 В15','м3'))
    assert visits==['https://supplier.example/','https://supplier.example/m200']
    assert result[0].url.endswith('/m200') and result[0].verification=='verified'


def test_concrete_aggregate_must_match_estimate_and_can_come_from_product_composition():
    from autobot.market_strategy import check_offer
    name='Смеси бетонные тяжелого бетона (БСТ) на щебне из гравия, класс В15'
    query_name=market.market_query_name(name)
    def check(result):
        return check_offer(name=query_name,unit='м3',title=result.title,snippet=result.evidence,
            url='https://supplier.example/m200',price=result.price,page_checked=True,source_unit=result.unit)
    unspecified=inspect(name=query_name)
    assert check(unspecified).status=='candidate'
    assert 'заполнитель' in check(unspecified).reason
    composition='<p>В состав М200 (В15) входят компоненты: цемент М400, щебень гравийный, песок, вода.</p>'
    precise=inspect(TABLE+composition,name=query_name)
    assert check(precise).status=='verified', check(precise).reason
    wrong=inspect(TABLE+composition.replace('гравийный','гранитный'),name=query_name)
    assert check(wrong).status=='candidate'
    unrelated=inspect(TABLE+composition.replace('М200 (В15)','М300 (В22,5)'),name=query_name)
    assert check(unrelated).status=='candidate'
    saved={'verification':'verified','price':5900,'url':'https://supplier.example/m200',
        'matched_unit':'м3','observed_at':market.datetime.now(market.timezone.utc).isoformat(),
        'evidence':unspecified.evidence}
    row={market.COL_NAME:name,'Ед. изм.':'м3',market.COL_QTY:10,BUNDLE_COLUMN:json.dumps([saved])}
    assert offers_for_row(row)[0]['verification']=='candidate'
    assert 'заполнитель' in offers_for_row(row)[0]['verification_reason']


def test_price_list_follows_product_page_to_confirm_required_aggregate(monkeypatch,tmp_path):
    monkeypatch.setattr(market,'_MARKET_CACHE_DIR',tmp_path)
    monkeypatch.setattr(market,'_MARKET_SEARCH_LOG_PATH',tmp_path/'log')
    visits=[]
    def fetch(url,**kw):
        visits.append(url)
        details='<p>В состав М200 (В15) входят компоненты: цемент М400, щебень гравийный, песок, вода.</p>'
        return TABLE+(details if url.endswith('/m200') else '<a href="/m200">Бетон М200 В15</a>'),'','requests'
    monkeypatch.setattr(market,'_fetch_source_page',fetch)
    name='Смеси бетонные тяжелого бетона (БСТ) на щебне из гравия, класс В15'
    row=pd.Series({market.COL_NAME:name,'Ед. изм.':'м3',market.COL_QTY:10})
    result=market._verify_offers(row,[market.MarketOffer('Интернет','Бетон М200',0,'https://supplier.example/')],build_search_plan(name,'м3'))
    assert visits==['https://supplier.example/','https://supplier.example/m200']
    assert result[0].verification=='verified' and 'гравийный' in result[0].evidence


def test_minimum_price_note_below_table_cannot_be_verified(monkeypatch,tmp_path):
    monkeypatch.setattr(market,'_MARKET_CACHE_DIR',tmp_path)
    monkeypatch.setattr(market,'_MARKET_SEARCH_LOG_PATH',tmp_path/'log')
    note='В таблице указана минимальная цена, для точного расчета позвоните нам.'
    page=TABLE+'<p>'+note+'</p>'
    monkeypatch.setattr(market,'_fetch_source_page',lambda *a,**kw:(page,'','requests'))
    parsed=inspect(page)
    assert parsed.price==5900 and not parsed.accepted and note in parsed.evidence
    row=pd.Series({market.COL_NAME:'Бетон М200 В15','Ед. изм.':'м3',market.COL_QTY:10})
    result=market._verify_offers(row,[market.MarketOffer('Интернет','Бетон М200',5900,'https://supplier.example/m200')],build_search_plan('Бетон М200 В15','м3'))
    assert result[0].verification=='candidate' and 'минимальную' in result[0].verification_reason


def test_previously_accepted_minimum_price_is_downgraded_on_reload():
    saved={'verification':'verified','price':3200,'url':'http://supplier.example/m250',
        'matched_unit':'м3','observed_at':market.datetime.now(market.timezone.utc).isoformat(),
        'evidence':'Бетон М250 В20 на гравии. Цена 1 м3: 3200 р.',
        'delivery_terms':'Цена включает доставку. В таблице указана минимальная цена, для точного расчета позвоните.'}
    row={market.COL_NAME:'Бетон М250 В20 на гравии','Ед. изм.':'м3',market.COL_QTY:9,BUNDLE_COLUMN:json.dumps([saved])}
    assert offers_for_row(row)[0]['verification']=='candidate'
    assert 'минимальную' in offers_for_row(row)[0]['verification_reason']
