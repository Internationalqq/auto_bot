import html
import json
import pytest
from autobot.supplier_catalog_extract import extract,navigate
from autobot.supplier_catalog_sites import electrical_records,megapolis_records
from autobot.market_source_adapters import inspect_source_page
from autobot.market_requirements import technical_conflict


def electrical_page(price=109,unit='м'):
    name='Кабель силовой ВВГнг(А)-LS 3х1.5(ок)(N, PE)-0.660 однопроволочный'
    url='https://electrical.ru/product/529136-cable'
    product={'@type':'Product','name':name,'url':url,'offers':{'price':price,'priceCurrency':'RUB','availability':'https://schema.org/PreOrder'}}
    body=f'<h1>{name}</h1><script type="application/ld+json">{json.dumps(product)}</script>'
    body+=f'<div class="flex items-baseline"><span>{price} ₽</span><span>/ {unit}</span></div>'
    body+='<section><h2>Похожие товары</h2><a href="/product/other">Кабель ВВГнг(А)-LS 3х1,5ок 97,90 ₽ / м</a></section>'
    return body,url,name


def test_main_price_never_borrows_similar_product_price_in_catalog_or_live_search():
    body,url,name=electrical_page()
    record=electrical_records(body,url)[0]
    assert record['price']==109 and record['unit']=='м'
    config={'catalog':{'adapter':'electrical'}}
    assert extract(body,url,'catalog','',config)[0]['price']==109
    inspection=inspect_source_page(body,url,name=name,target_unit='м',position_bucket='materials')
    assert inspection.accepted and inspection.price==109
    assert '97,90' not in inspection.evidence
    for changed in (body.replace('109 ₽',''),body.replace('RUB','USD'),body.replace('"price": 109','"price": 0')):
        with pytest.raises(ValueError): electrical_records(changed,url)
        result=inspect_source_page(changed,url,name=name,target_unit='м',position_bucket='materials')
        assert not result.accepted and result.price is None


def sign_page():
    variants=[]
    for ident,size,film,price in [(100,'II типоразмер','Тип А. Коммерческая',1530),(101,'III типоразмер','Тип В. Алмазная',5600)]:
        variants.append({'ID':ident,'NAME':'4.4.1 Велосипедная дорожка','ITEM_PRICE_SELECTED':0,
          'ITEM_PRICES':[{'BASE_PRICE':price,'PRICE':price-100,'CURRENCY':'RUB','MIN_QUANTITY':1}],
          'ITEM_MEASURE':{'TITLE':'шт'},'CAN_BUY':True,'ITEM_MEASURE_RATIOS':{'1':{'RATIO':1}},
          'PROPERTIES':{'SIZE':{'NAME':'Тип/размер','VALUE':size},'FILM':{'NAME':'Тип пленки','VALUE':film}}})
    params={'SIZE':{'NAME':'Тип/размер'},'FILM':{'NAME':'Тип пленки'}}
    body='<h1>4.4.1 Велосипедная дорожка</h1><p>от 1530 руб.</p>'
    body+=f'<catalog-offers :offers="{html.escape(json.dumps(variants))}" :params="{html.escape(json.dumps(params))}"></catalog-offers>'
    return body,variants,params


def test_each_sign_variant_keeps_own_price_film_size_and_identity():
    body,_,_=sign_page();url='https://pkmegapolis.ru/dorozhnye-znaki/4-4-1.html'
    rows=megapolis_records(body,url)
    assert [r['price'] for r in rows]==[1530,5600]
    assert all(r['unit']=='шт' and r['price_kind']=='published' for r in rows)
    assert rows[0]['item_key']!=rows[1]['item_key']
    assert 'III типоразмер' not in rows[0]['name'] and 'Алмазная' not in rows[0]['evidence']
    target='Дорожный знак 4.4.1 II типоразмер тип А коммерческая пленка'
    assert not technical_conflict(target,rows[0]['evidence'])
    assert technical_conflict(target,rows[1]['evidence'])
    assert technical_conflict(target,rows[0]['evidence'].replace('4.4.1','4.4.2'))
    assert technical_conflict(target,rows[0]['evidence'].replace('Коммерческая','Инженерная'))
    assert technical_conflict(target,'Дорожный знак 4.4.1')
    direct=inspect_source_page(body,url,name=target,target_unit='шт',quantity=1,position_bucket='materials')
    assert direct.accepted and direct.price==1530
    direct=inspect_source_page(body,url,name='Дорожный знак 4.4.1 III типоразмер тип В алмазная пленка',target_unit='шт',quantity=1,position_bucket='materials')
    assert direct.accepted and direct.price==5600
    assert megapolis_records('<h1>Знаки</h1><p>от 1000 руб.</p>',url)==[]


def test_sign_navigation_is_bounded_to_registered_section_and_same_host():
    config={'url':'https://pkmegapolis.ru/dorozhnye-znaki/','catalog':{'adapter':'megapolis'}}
    body='''<a href="/dorozhnye-znaki/predpisyvayushie/">Предписывающие</a>
    <a href="/dorozhnye-znaki/predpisyvayushie/4-4-1.html">Знак</a>
    <a href="https://other.ru/dorozhnye-znaki/fake.html">Другой сайт</a>
    <a href="/svetofory/">Другой раздел</a><a href="/kontakt/">Контакты</a>'''
    rows=navigate(body,config['url'],config)
    assert [r['kind'] for r in rows]==['catalog','product','context']
    assert all(r['url'].startswith('https://pkmegapolis.ru/') for r in rows)


def test_cable_voltage_trailing_zero_does_not_change_required_rating():
    assert not technical_conflict('Кабель ВВГнг(А)-LS 3х1,5ок-660','Кабель ВВГнг(А)-LS 3х1,5 ок-0.660')
    assert technical_conflict('Кабель ВВГнг(А)-LS 3х1,5ок-660','Кабель ВВГнг(А)-LS 3х1,5 ок 1кВ')
    assert technical_conflict('Кабель ВВГнг(А)-LS 3х1,5ок-660','Кабель ВВГнг(А)-LS 3х1,5 ок-0.661')


def test_keepmarket_rate_is_paid_in_whole_compulsory_reels():
    from autobot.supplier_catalog_sites import keepmarket_records
    from autobot.supplier_catalog_match import purchase_price
    body='''<h1>Кабель F/UTP 4х2х0,51 Cu</h1><div class="old-price">50 ₽</div>
      <div class="prod_price"><span id="block_price">67 ₽</span></div>
      <div class="block_efg">Минимальная кратность продажи: 0.305 км Цена указана за: 1 м.</div>
      <div class="block_efg">Функциональное назначение: кабель</div>
      <div class="block_efg">Минимальная кратность продажи: 0.305 км</div>
      <div class="related">Кабель за 30 ₽/м</div>'''
    row=keepmarket_records(body,'https://keepmarket.ru/catalog/cable')[0]
    assert row['price']==20435 and row['unit']=='упак'
    assert row['details']['package']['amount']==305
    price,unit,_,_=purchase_price(dict(row,price_kopecks=2043500),row['details'],'м',306)
    assert unit=='м' and abs(price*306-40870)<1e-6
    with pytest.raises(ValueError): keepmarket_records(body.replace('0.305 км','не указана'),row['url'])


def test_unavailable_optical_offer_keeps_own_metre_price_as_candidate():
    from autobot.supplier_catalog_sites import tdatm_records
    body='''<h1 class="changeName">Кабель ОГЦ-4А-7 (7кН)</h1>
      <meta itemprop="price" content="38.75"><meta itemprop="priceCurrency" content="RUB">
      <link itemprop="availability" href="http://schema.org/OutOfStock">
      <table class="stats"><tr><td>Базовая единица</td><td>м</td><td></td></tr></table>
      <aside>Рекомендуем другой кабель 51.70 руб/шт</aside>'''
    row=tdatm_records(body,'https://tdatm.ru/catalog/ogc')[0]
    assert row['price']==38.75 and row['unit']=='м' and row['price_kind']=='conditional'
    assert not technical_conflict('Кабель ОГЦ-4А-7',row['name'])
    assert technical_conflict('Кабель ОГЦ-4А-7','Кабель ОГЦ-4А-4')
    assert technical_conflict('Кабель ОГЦ-4А-7','Кабель ОГЦ-8А-7')
    direct=inspect_source_page(body,row['url'],name=row['name'],target_unit='м',position_bucket='materials')
    assert not direct.accepted and direct.price is None
    with pytest.raises(ValueError): tdatm_records(body.replace('Базовая единица','Диаметр'),row['url'])


def test_unqualified_sign_fitting_is_kept_for_review_without_inventing_unit():
    url='https://pkmegapolis.ru/dorozhnye-znaki/homut.html';name='Крепление хомут'
    product={'@type':'Product','name':name,'url':url,'offers':{'price':75,'priceCurrency':'RUB'}}
    body=f'<h1 class="cardInfo__title">{name}</h1><script type="application/ld+json">{json.dumps(product)}</script>'
    row=megapolis_records(body,url)[0]
    assert row['price']==75 and row['unit']=='' and row['price_kind']=='conditional'
    assert megapolis_records(body,'https://pkmegapolis.ru/other.html')==[]


def test_ekc_reads_visible_rows_without_microdata_and_preserves_stock_limit():
    from autobot.supplier_catalog_extract import product_records
    from autobot.supplier_evidence import quantity_terms_reason
    body='''<h1>Кабель АВБбШв 4х150</h1><p>АВБбШв 4х150 — устаревшая маркировка;
      конструкция и характеристики не изменились; современная маркировка: АВБШв 4х150.</p>
      <table class="offerTable"><tr><td class="title">АВБШв 4х150 1кВ</td>
      <td class="price">1 313,36 ₽</td><td class="input"><span class="colWo">м.</span></td>
      <td class="amount">1627 м.</td><td class="date">21.09.2026</td></tr>
      <tr><td class="title">АВБШв нг(А) 4х150 1кВ</td><td class="price">1 500 ₽</td>
      <td class="input"><span class="colWo">м.</span></td><td class="amount">100 м.</td></tr></table>'''
    rows=product_records(body,'https://e-kc.ru/cena/cable','','ekc','materials')
    assert [r['price'] for r in rows]==[1313.36,1500]
    assert 'устаревшая маркировка' in rows[0]['evidence']
    assert 'устаревшая маркировка' not in rows[1]['evidence']
    terms=rows[0]['details']['quantity_terms']
    assert not quantity_terms_reason(terms,1.627,'1000 м')
    assert quantity_terms_reason(terms,1.628,'1000 м')
    assert quantity_terms_reason([{'maximum':'nan','unit':'м'}],1,'м')
    empty=product_records(body.replace('1627 м.','0 м.'),'https://e-kc.ru/cena/cable','','ekc','materials')[0]
    assert empty['price_kind']=='conditional' and 'нулю' in empty['reason']
    missing_unit=product_records(body.replace('class="colWo"','class="missing-unit"'),'https://e-kc.ru/cena/cable','','ekc','materials')[0]
    assert missing_unit['price_kind']=='conditional' and missing_unit['unit']==''
