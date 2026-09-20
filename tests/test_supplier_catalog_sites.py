import json
from autobot.supplier_catalog_sites import yamck_records,tinko_records
from autobot.supplier_evidence import quantity_terms_reason
from autobot.supplier_catalog_extract import navigate,extract


def test_public_feed_retains_source_units_and_minimum_order():
    raw=json.dumps({'success':True,'data':[{'id':'sand','name':'Песок карьерный','price':350,'unit':'м³'}],
                    'delivery':{'minVolume':3,'zones':[{'price':3500}]}},ensure_ascii=False)
    row=yamck_records(raw,'https://supplier.example/catalog')[0]
    assert row['price']==350 and row['unit']=='м3'
    assert row['url']=='https://supplier.example/catalog'
    terms=row['details']['quantity_terms']
    assert quantity_terms_reason(terms,2,'м3')
    assert not quantity_terms_reason(terms,.004,'1000 м3')


def test_retail_card_ignores_wholesale_related_products_and_hidden_modal():
    raw='''<h1>Dahua DH-IPC-HDBW3441FP-AS-0280B-S2</h1><div class="product-detail__prices">
    <div class="product-detail__price-wrapper"><span class="product-detail__price-value">22 021,80</span>
    <i class="fa-rub">/шт</i><div class="product-detail__price-info">Розничная цена</div></div>
    <div class="product-detail__price-wrapper"><span class="product-detail__price-value">17 617,44</span>
    <i class="fa-rub">/шт</i><div class="product-detail__price-info">Оптовая цена</div></div></div>
    <div class="modal">1 км за 22 021,80 рублей</div>
    <div class="product-card"><h2>Another camera</h2><span class="price">1000 руб/шт</span></div>'''
    row=tinko_records(raw,'https://supplier.example/product/1')[0]
    assert row['price']==22021.8 and row['unit']=='шт' and row['price_kind']=='published'
    assert '17 617' not in row['evidence'] and '1 км' not in row['evidence']


def test_feed_navigation_stays_on_the_registered_host():
    config={'url':'https://supplier.example/catalog','catalog':{'adapter':'yamck','seed_urls':['https://other.example/price']}}
    links=navigate("fetch('/api/site/nerud')",config['url'],config)
    assert [r['url'] for r in links]==['https://supplier.example/api/site/nerud']
    assert extract('<h1>Catalogue</h1>',config['url'],'catalog','',config)==[]


def test_explicit_national_shipping_is_a_region_proof_without_delivery_cost():
    from autobot.market_source_adapters import source_region_evidence
    body='<p>Доставка заказа в регионы России осуществляется через транспортные компании.</p>'
    assert source_region_evidence(body,'Ярославская область','materials')
    assert not source_region_evidence(body,'Ярославская область','works')
    assert not source_region_evidence('<p>Не доставляем в регионы России</p>','Ярославская область','materials')
