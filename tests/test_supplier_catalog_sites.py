import json
from autobot.supplier_catalog_sites import yamck_records,tinko_records
from autobot.supplier_evidence import quantity_terms_reason
from autobot.supplier_catalog_extract import navigate,extract


def test_anbik_retail_column_ignores_hidden_wholesale_and_other_currency():
    import pytest
    from autobot.supplier_catalog_sites import anbik_records
    body='''<h1 class="cart_caption">Изолятор ИС4-30 (М8) с болтом</h1>
      <link itemprop="availability" href="https://schema.org/InStock">
      <div class="item_detail_row"><div><p class="item_price">Цена:</p>
      <span class="main_price">426,37 ₽</span><sup>/ шт</sup><span>3.88$</span></div>
      <div><p class="item_price">От 25 000 рублей:</p><span class="main_price">402,68 ₽</span>
      <span itemprop="price" style="display:none">402.68</span><sup>/ шт</sup></div></div>'''
    records=anbik_records(body,'https://anbik.ru/setevye-skafy/item-5549/')
    assert len(records)==1 and records[0]['price']==426.37 and records[0]['unit']=='шт'
    assert '402' not in records[0]['evidence'] and records[0]['price_kind']=='published'
    assert anbik_records(body.replace('InStock','OutOfStock'),'https://anbik.ru/')[0]['price_kind']=='conditional'
    with pytest.raises(ValueError):anbik_records(body.replace('<sup>/ шт</sup>',''),'https://anbik.ru/')


def test_paint_colours_use_retail_price_and_whole_buckets_not_adjacent_glass_price():
    from autobot.supplier_catalog_sites import ak511_records
    from autobot.supplier_catalog_match import purchase_price
    body='''<div class="section-cell"><div>Фасовка: ведро 30 кг</div>
      <div class="blk-data">Краска дорожная АК "Колор-М", белая<br>опт от 5 тонн: 174 руб/кг<br>розница: 193 руб/кг</div>
      <div class="blk-data">Краска дорожная АК "Колор-М", черная<br>опт от 5 тонн: 175 руб/кг<br>розница: 195 руб/кг</div></div>
      <div class="section-cell"><div>Фасовка: 18 кг</div><div class="blk-data">Стеклошарики розница: 92 руб/кг</div></div>'''
    records=ak511_records(body,'https://ak511.ru/')
    assert len(records)==2
    assert records[0]['price']==5790 and records[1]['price']==5850
    first=records[0]
    cost,unit,evidence,terms=purchase_price(dict(first,price_kopecks=579000),first['details'],'кг',31)
    assert abs(cost*31-11580)<0.0001 and unit=='кг'
    assert quantity_terms_reason(terms,32,'кг')
    assert '174' not in evidence and '92 руб' not in evidence
    import pytest
    with pytest.raises(ValueError):
        ak511_records(body.replace('Фасовка: ведро 30 кг',''), 'https://ak511.ru/')


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


def test_tinko_short_description_supplies_item_identity_without_related_cards():
    body='''<h1>Лента 300мм х 100м (PR08.3855)</h1>
    <div class="product-detail__prices"><div class="product-detail__price-wrapper">
    <span class="product-detail__price-value">3 743,80</span><i class="fa-rub">/шт</i>
    <div class="product-detail__price-info">Розничная цена</div></div></div>
    <div class="product-detail__short-description">ЛСЭ-300 Лента сигнальная 300мм 100м</div>
    <div class="product-card">ЛСЭ-450 450мм 100м 5000 руб/шт</div>'''
    record=tinko_records(body,'https://www.tinko.ru/catalog/product/322999/')[0]
    assert 'ЛСЭ-300' in record['evidence'] and 'ЛСЭ-450' not in record['evidence']
    from autobot.market_requirements import technical_conflict
    assert not technical_conflict('Лента сигнальная ЛСЭ-300 длина 100 м ширина 300 мм',record['evidence'])


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
    assert source_region_evidence('<div>Доставка в другие регионы РФ проверенными перевозчиками.</div>','Ярославская область','materials')
    assert not source_region_evidence('<div>Не доставляем в другие регионы РФ.</div>','Ярославская область','materials')
