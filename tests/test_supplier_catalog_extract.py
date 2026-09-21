import socket
import time
import pytest

from autobot.supplier_catalog_extract import table_records,product_records,navigate
from autobot.supplier_catalog_worker import public_address


def test_supplier_set_price_cannot_enter_tonne_catalogue():
    html='''<table><tr><th>Товар</th><th>Цена (руб с НДС)</th><th>Ед. измер.</th></tr>
      <tr><td>Муфта концевая 5ПКТп-1-16/25</td><td>2400</td><td>к-т.</td></tr></table>'''
    row=table_records(html,'https://supplier.example/price','materials')[0]
    assert row['unit']=='шт' and row['price']==2400


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
    assert row['details']['package']['amount']==100


def test_price_table_unit_after_price_and_actual_date_stay_with_product():
    html='''<form><table><tr><th>Товар<input name="filter"></th><th>Цена (руб с НДС)</th><th>Ед. измер.</th><th>Актуальность</th></tr>
       <tr><td>Труба EKF tpndg-50</td><td>169.16</td><td>м</td><td>26.06.26</td></tr></table></form>'''
    item=table_records(html,'https://supplier.example/prices','materials')[0]
    assert item['unit']=='м' and item['price']==169.16 and item['price_kind']=='published'
    assert item['details']['published_at']=='2026-06-26T00:00:00+00:00'


def test_price_list_pagination_stays_inside_selected_brand():
    config={'url':'https://supplier.example/prajs-list/price/ekf','catalog':{'adapter':'svetelektro'}}
    html='<a href="/prajs-list/price/ekf/100">2</a><a href="/prajs-list/price/iek/100">IEK</a><a href="/prajs-list/price/ekf?format=xls">XLS</a>'
    assert [link['url'] for link in navigate(html,config['url'],config)]==['https://supplier.example/prajs-list/price/ekf/100']


@pytest.mark.parametrize('unit_heading', ['Единицы измерения', 'Единица измерения', 'Ед. измер.', 'Ед.изм.'])
def test_full_unit_header_cannot_replace_service_name(unit_heading):
    html=f'''<table><tr><th>Список оказываемых услуг</th><th>{unit_heading}</th><th>Стоимость</th></tr>
      <tr><td>Измерение сопротивления изоляции кабеля</td><td>линия</td><td>90 руб</td></tr>
      <tr><td>Измерение сопротивления заземления</td><td>измерение</td><td>500 руб</td></tr></table>'''
    rows=table_records(html,'https://supplier.example/price','works')
    assert [(r['name'],r['price'],r['unit']) for r in rows]==[
        ('Измерение сопротивления изоляции кабеля',90,'линия'),
        ('Измерение сопротивления заземления',500,'измерение')]
    assert all(r['price_kind']=='published' and r['details']['price_scope']=='work_only' for r in rows)


def test_saved_table_evidence_keeps_global_minimum_notice():
    html='''<table><tr><th>Работы</th><th>Ед.изм.</th><th>Цена</th></tr>
      <tr><td>Укладка геотекстиля</td><td>м2</td><td>80 руб</td></tr></table>
      <p>Все цены в прайсе указаны как минимальная стоимость</p>'''
    row=table_records(html,'https://supplier.example/price','works')[0]
    assert 'минимальная стоимость' in row['evidence']
    assert row['price_kind']=='conditional'


def test_old_price_effective_date_is_not_replaced_by_new_capture_date():
    from autobot.market_evidence_policy import freshness_reason
    html='''<p>Расценки на услуги действительны с 1-го марта 2020 года.</p>
      <table><tr><th>Работы</th><th>Ед. изм.</th><th>Цена</th></tr>
      <tr><td>Разработка грунта</td><td>м3</td><td>380 руб</td></tr></table>'''
    row=table_records(html,'https://supplier.example/price','works')[0]
    assert row['details']['published_at']=='2020-03-01T00:00:00+00:00'
    assert '2020 года' in row['evidence']
    assert freshness_reason({'observed_at':'2026-09-21T00:00:00+00:00', **row['details']},'works')


@pytest.mark.parametrize('text', ['© 2020 Компания', 'Компания работает с 01.03.2020', 'Новости от 01.03.2020',
                                  'Цены действуют с 40.15.2020'])
def test_unrelated_or_invalid_date_is_not_price_publication_date(text):
    from autobot.supplier_evidence import price_list_date
    assert price_list_date('<p>'+text+'</p>')==('', '')
