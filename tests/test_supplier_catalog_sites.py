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


def test_packaged_pipe_cost_includes_whole_packs_in_catalogue_and_live_search():
    from autobot.supplier_catalog_extract import product_records
    from autobot.supplier_catalog_match import purchase_price
    from autobot.market_source_adapters import inspect_source_page
    from autobot.supplier_evidence import quantity_terms_reason
    body='''<h1>Труба гладкая жесткая ПНД d50мм черная PROxima EKF tpndg-50</h1>
    <div class="active_price_st"><span>27 423</span><span>₽</span><span>за 100 м.</span></div>
    <div>Единица измерения метр</div>'''
    url='https://www.elektro.ru/product/pipe/'
    record=product_records(body,url,'','elektro','materials')[0]
    assert record['price']==27423 and record['unit']=='упак'
    row=dict(record,price_kopecks=2742300)
    price,unit,evidence,terms=purchase_price(row,record['details'],'м',150)
    assert price==365.64 and unit=='м' and '54846' in evidence
    assert not quantity_terms_reason(terms,150,'м')
    assert quantity_terms_reason(terms,151,'м')
    result=inspect_source_page(body,url,name=record['name'],target_unit='м',position_bucket='materials',quantity=150)
    assert result.accepted and result.price==365.64 and result.quantity_terms
    assert not inspect_source_page(body,url,name=record['name'],target_unit='м',position_bucket='materials').accepted


def test_font_ruble_in_price_table_is_a_currency_not_a_missing_price():
    from autobot.supplier_catalog_extract import table_records
    body='''<table><tr><th>Наименование</th><th>Цена</th></tr>
    <tr><td>Труба EKF tpndg-50</td><td>192,46 <i class="fa fa-rub" title="руб."></i> / м</td></tr></table>'''
    records=table_records(body,'https://supplier.example/prices','materials')
    assert len(records)==1 and records[0]['price']==192.46 and records[0]['unit']=='м'


def test_grass_supplier_binds_retail_price_and_bag_to_same_card():
    from autobot.supplier_catalog_sites import esg_records
    from autobot.supplier_catalog_match import purchase_price
    from autobot.market_source_adapters import source_region_evidence
    card=lambda title,price,size:f'''<div class="wpb_column"><div class="vc_column-inner"><h2>{title}</h2>
        <p>Цена розница: {price} руб./кг. Цена опт: от 10.00 руб/кг.</p><p>Мешки по {size} кг.</p>
        <p>Травосмесь для газона</p></div></div>'''
    body='<p>Поставляем по всей России</p><div class="product_items">'+card('Газон Универсальный','350.00',20)+card('Газон Спорт','380.00',25)+'</div>'
    records=esg_records(body,'https://gazony-esg.ru/')
    assert [r['price'] for r in records]==[7000,9500]
    assert all('от 10' not in r['evidence'] for r in records)
    first=records[0]
    price,unit,evidence,terms=purchase_price(dict(first,price_kopecks=700000),first['details'],'кг',25)
    assert price==560 and unit=='кг' and '14000' in evidence
    assert source_region_evidence(body,'Ярославская область','materials')
    assert not source_region_evidence(body,'Ярославская область','works')
    assert not source_region_evidence('<p>Не поставляем по всей России</p>','Ярославская область','materials')
    from autobot.market_requirements import technical_conflict
    wanted='Семена газонной травы, травосмесь «Универсальная»'
    assert not technical_conflict(wanted,'Газон "Универсальный" — семена травы')
    assert technical_conflict(wanted,'Газон "Коттедж" — смесь семян газонной травы')
    assert technical_conflict(wanted,'Семена для газона')


def test_explicit_national_shipping_is_a_region_proof_without_delivery_cost():
    from autobot.market_source_adapters import source_region_evidence
    body='<p>Доставка заказа в регионы России осуществляется через транспортные компании.</p>'
    assert source_region_evidence(body,'Ярославская область','materials')
    assert not source_region_evidence(body,'Ярославская область','works')
    assert not source_region_evidence('<p>Не доставляем в регионы России</p>','Ярославская область','materials')
