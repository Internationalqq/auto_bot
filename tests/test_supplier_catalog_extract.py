import socket
import time
import pytest

from autobot.supplier_catalog_extract import table_records,product_records,navigate
from autobot.supplier_catalog_worker import public_address


def test_parallel_price_columns_do_not_cross_products():
    html='''<table><tr><th>Марка бетона</th><th>Цена с НДС</th><th>Марка раствора</th><th>Цена с НДС</th></tr>
        <tr><td>В15 (М200)</td><td>6 150 руб/м 3</td><td>М100</td><td>5 200 руб/м 3</td></tr></table>'''
    rows=table_records(html,'https://supplier.example/price','materials')
    assert [(r['name'],r['price'],r['unit']) for r in rows]==[('Бетон В15 (М200)',6150,'м3'),('Раствор М100',5200,'м3')]


def test_service_subrows_keep_operation_and_own_price():
    html='''<table><tr><th>№</th><th>Наименование работ</th><th>Ед.изм.</th><th>Цена</th></tr>
        <tr><td>1</td><td>Монтаж кабель-канала:</td><td></td><td></td></tr>
        <tr><td>2</td><td>- бетон</td><td>пог.м</td><td>150р.</td></tr>
        <tr><td>3</td><td>- кирпич</td><td>пог.м</td><td>100р.</td></tr>
        <tr><td>4</td><td>Замена лампы</td><td>шт.</td><td></td></tr>
        <tr><td>5</td><td>Монтаж светильника</td><td>шт.</td><td>250р.</td></tr></table>'''
    rows=table_records(html,'https://supplier.example/price','works')
    assert rows[0]['name']=='Монтаж кабель-канала: бетон'
    assert rows[1]['name']=='Монтаж кабель-канала: кирпич'
    assert rows[0]['unit']=='м' and rows[0]['price']==150
    assert rows[2]['name']=='Замена лампы' and rows[2]['price'] is None
    assert rows[3]['price']==250


def test_from_price_and_global_conditions_stay_conditional():
    html='''<table><tr><th>Работы</th><th>Ед.изм.</th><th>Цена</th></tr>
      <tr><td>Укладка геотекстиля</td><td>м2</td><td>от 80 руб</td></tr></table>'''
    row=table_records(html,'https://supplier.example/price','works')[0]
    assert row['price']==80 and row['price_kind']=='conditional'
    html=html.replace('от 80','80')+'<p>Все цены в прайсе указаны как минимальная стоимость</p>'
    assert table_records(html,'https://supplier.example/price','works')[0]['price_kind']=='conditional'


def test_cable_variants_keep_individual_names_prices_and_selling_units():
    html='''<h1>Кабель АВБШв 4х50</h1><table class="offerTable">
      <tr><td class="title">АВБШв 4х50 ок (N)-0,66</td><td class="amount">907 м</td>
      <td class="price"><meta itemprop="price" content="517.24"><meta itemprop="priceCurrency" content="RUB"></td><td class="input"><div class="colWo">м.</div></td></tr>
      <tr><td class="title">АВБШв 4х50 мс (N)-0,66</td><td class="amount">233 м</td>
      <td class="price"><meta itemprop="price" content="549.55"><meta itemprop="priceCurrency" content="RUB"></td><td class="input"><div class="colWo">м.</div></td></tr></table>
      <table><tr><td>Сигнальная лента</td><td>15.82 руб</td></tr></table>'''
    rows=product_records(html,'https://supplier.example/cable','','ekc','materials')
    assert len(rows)==2 and rows[0]['item_key']!=rows[1]['item_key']
    assert [r['price'] for r in rows]==['517.24','549.55']
    assert all(r['unit']=='м' for r in rows)
    assert 'ок' in rows[0]['name'] and 'мс' in rows[1]['name']


def test_navigation_allows_observed_pagination_but_not_cart_or_external():
    config={'url':'https://supplier.example/price/cable','catalog':{'adapter':'ekc'}}
    html='''<a href="?page=2">2</a><a href="?page=3">3</a><a href="?sort=price">Цена</a>
       <table><tr><td><a href="/cena/cable-1">Кабель</a></td></tr></table>
       <a href="https://external.example/cena/cable">Кабель</a><a href="/cart/add">Заказать</a>'''
    links=navigate(html,config['url'],config)
    assert {r['url'] for r in links}=={'https://supplier.example/price/cable?page=2','https://supplier.example/price/cable?page=3','https://supplier.example/cena/cable-1'}


def test_private_addresses_and_cross_host_redirects_are_rejected(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError): public_address('https://supplier.example/','supplier.example')
    with pytest.raises(ValueError): public_address('https://evil.example/','supplier.example')
    with pytest.raises(ValueError): public_address('https://user:secret@supplier.example/','supplier.example')


def test_rental_lot_is_not_an_hourly_price():
    html='<table><tr><th>Техника</th><th>Ед. изм.</th><th>Цена</th></tr><tr><td>Аренда катка</td><td>8ч.</td><td>от 16 000₽</td></tr></table>'
    row=table_records(html,'https://supplier.example/price','works')[0]
    assert row['unit']=='8ч.' and row['price']==16000
    assert row['bucket']=='equipment' and row['price_kind']=='conditional'


def test_roll_price_does_not_inherit_square_metres_from_dimensions():
    html='''<h1>Геотекстиль Дорнит 200, 2*50м, 100 м2/рул</h1>
        <div itemscope itemtype="https://schema.org/Product"><meta itemprop="name" content="Геотекстиль Дорнит 200">
        <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
        <meta itemprop="price" content="41"><meta itemprop="priceCurrency" content="RUB"></div></div>
        <h3>Цена: 4100 р.</h3><div class="product-desc_short">Площадь покрытия: 100 м2. Цена указана за 1 рулон</div>'''
    row=product_records(html,'https://supplier.example/product','','geo76','materials')[0]
    assert row['unit']=='рулон' and row['price']==4100
    assert row['price_kind']=='published'
