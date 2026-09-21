"""Public price feeds and retail cards with explicit, source-specific units."""
import json
import re
from bs4 import BeautifulSoup
from autobot.supplier_catalog_store import clean
from autobot.market_strategy import normalize_unit


def ak511_records(body, url):
    """Keep each colour's retail price with its own paint-section packaging."""
    from decimal import Decimal
    soup=BeautifulSoup(body,'html.parser')
    records=[]
    composition=re.search(r'Производится краска для дорог АК[^.!?]{0,180}на основе акриловых[^.!?]{0,100}',
                          clean(soup.get_text(' ',strip=True)),re.I)
    for section in soup.select('.section-cell'):
        sizes=re.findall(r'Фасовка:\s*ведро\s+(\d+(?:[.,]\d+)?)\s*кг', section.get_text(' ',strip=True), re.I)
        if len(sizes)!=1: continue
        size=Decimal(sizes[0].replace(',','.'))
        if size<=0: continue
        for card in section.select('.blk-data'):
            value=clean(card.get_text(' ',strip=True)).replace('\u200b','')
            match=re.fullmatch(r'(Краска дорожная АК\s+"Колор-М",\s*[а-яё]+)\s+'
                r'опт от \d+ тонн:\s*\d+(?:[.,]\d+)?\s*руб/кг\s+'
                r'розница:\s*(\d+(?:[.,]\d+)?)\s*руб/кг',value,re.I)
            if not match: continue
            name=match[1];rate=Decimal(match[2].replace(',','.'))
            if rate<=0: continue
            proof=f'Розница: {rate} руб/кг; фасовка: ведро {size} кг; стоимость ведра {rate*size} руб'
            records.append({'name':name,'url':url,'unit':'упак','price':float(rate*size),'bucket':'materials',
                'item_key':url+'|'+name.casefold(),'price_kind':'published','reason':'',
                'evidence':name+' · '+proof+(' · '+composition[0] if composition else ''),
                'details':{'price_scope':'product','extractor':'ak511-retail-bucket',
                    'package':{'amount':float(size),'unit':'кг','evidence':proof}}})
    if not records: raise ValueError('Не найдены розничные цены краски с фасовкой в том же разделе')
    return records


def anbik_records(body,url):
    """The visible retail column, never the hidden wholesale microdata price."""
    from autobot.market_source_adapters import parse_ruble_values
    soup=BeautifulSoup(body,'html.parser')
    heading=soup.select_one('h1.cart_caption')
    if not heading: return []
    name=clean(heading.get_text(' ',strip=True));records=[]
    for label in soup.select('.item_detail_row p.item_price'):
        if clean(label.get_text(' ',strip=True))!='Цена:': continue
        column=label.parent
        price_node=column.select_one('.main_price');unit_node=column.select_one('sup')
        if price_node is None or unit_node is None: continue
        price_text=clean(price_node.get_text(' ',strip=True))
        prices=parse_ruble_values(price_text)
        unit=normalize_unit(clean(unit_node.get_text(' ',strip=True)).lstrip('/ '))
        if len(prices)!=1 or prices[0]<=0 or unit not in {'шт','м','упак','компл'}: continue
        availability=soup.select_one('[itemprop="availability"]')
        available=availability and str(availability.get('href') or '').endswith('/InStock')
        records.append({'name':name,'unit':unit,'url':url,'price':prices[0],'bucket':'materials',
            'price_kind':'published' if available else 'conditional',
            'reason':'' if available else 'Наличие товара не подтверждено',
            'evidence':name+' · Цена: '+price_text+' / '+unit,
            'details':{'price_scope':'product','extractor':'anbik-retail'}})
    if len(records)!=1: raise ValueError('Не найдена однозначная розничная цена Анбик с единицей')
    return records


def esg_records(body, url):
    """Retail grass prices with the bag size from the same product column."""
    from decimal import Decimal
    soup=BeautifulSoup(body,'html.parser')
    records=[]
    for card in soup.select('.product_items > .wpb_column > .vc_column-inner'):
        headings=card.select('h2')
        if len(headings)!=1: continue
        title=clean(headings[0].get_text(' ',strip=True))
        text=clean(card.get_text(' ',strip=True))
        prices=re.findall(r'Цена\s+розница:\s*(\d[\d\s]*[.,]\d{2})\s*руб\.?\s*/\s*кг\.?',text,re.I)
        packs=re.findall(r'Мешки\s+по\s*(\d+(?:[.,]\d+)?)\s*кг',text,re.I)
        if len(prices)!=1 or len(packs)!=1: continue
        price=Decimal(prices[0].replace(' ','').replace(',','.'))
        size=Decimal(packs[0].replace(',','.'))
        if price<=0 or size<=0: continue
        descriptions=[clean(p.get_text(' ',strip=True)) for p in card.select('p')]
        description=next((v for v in descriptions if 'цена' not in v.casefold() and re.search(r'травосмесь|смесь семян|семена газон',v,re.I)),'')
        evidence=f'{title} · Розничная цена: {price} руб/кг · Мешки по {size} кг'
        if description: evidence+=' · '+description[:650]
        records.append({'name':title,'url':url,'unit':'упак','price':float(price*size),'bucket':'materials',
            'item_key':url+'|'+title.casefold(),'price_kind':'published','reason':'','evidence':evidence,
            'details':{'price_scope':'product','extractor':'esg-retail-bag',
                'package':{'amount':float(size),'unit':'кг','evidence':f'Розница {price} руб/кг. Мешки по {size} кг; стоимость мешка {price*size} руб'}}})
    if not records: raise ValueError('Не найдены розничные цены с размером мешка в карточках производителя')
    return records


def yamck_records(body, page_url):
    data=json.loads(body)
    if data.get('success') is not True or not isinstance(data.get('data'),list):
        raise ValueError('Поставщик не вернул действующий каталог')
    minimum=(data.get('delivery') or {}).get('minVolume')
    records=[]
    for item in data['data']:
        name=clean(item.get('name'));unit=normalize_unit(item.get('unit'))
        price=item.get('price')
        if not name or not item.get('id') or not unit:
            continue
        evidence=f'{name} · {price} руб / {unit}; без доставки'
        terms=[]
        if isinstance(minimum,(int,float)) and minimum>0:
            terms=[{'minimum':minimum,'unit':'м3','evidence':f'Минимальный заказ {minimum} м3'}]
        records.append({'name':name,'url':page_url,'unit':unit,'price':price,'bucket':'materials',
            'item_key':page_url+'|'+str(item['id']), 'price_kind':'published' if price else 'on_request',
            'reason':'','evidence':evidence,
            'details':{'price_scope':'product','quantity_terms':terms,'extractor':'yamck-public-feed'}})
    if not records:
        raise ValueError('Пустой каталог поставщика; прежние цены сохранены')
    return records


def tinko_records(body,url):
    soup=BeautifulSoup(body,'html.parser')
    title=soup.select_one('h1')
    if not title:
        raise ValueError('Не найдена карточка товара ТИНКО')
    name=clean(title.get_text(' ',strip=True))
    scope=soup.select_one('.product-detail__prices')
    if scope is None:
        raise ValueError('В карточке отсутствует блок цены')
    variants=[]
    for block in scope.select('.product-detail__price-wrapper'):
        label=block.select_one('.product-detail__price-info')
        if not label or 'Розничная цена' not in label.get_text(' ',strip=True):
            continue
        value=block.select_one('.product-detail__price-value')
        currency=block.select_one('.fa-rub')
        if not value or not currency:
            continue
        raw=clean(value.get_text(' ',strip=True))
        if not re.fullmatch(r'\d[\d\s]*[,.]\d{2}',raw):
            continue
        unit=normalize_unit(currency.get_text(' ',strip=True).lstrip('/').strip())
        if not unit:
            continue
        variants.append((float(raw.replace(' ','').replace(',','.')),unit))
    if len(set(variants))!=1:
        return [{'name':name,'url':url,'unit':'','price':None,'bucket':'materials','item_key':url,
            'price_kind':'on_request','reason':'Розничная цена с единицей продажи не опубликована',
            'evidence':name+' · '+clean(scope.get_text(' ',strip=True))[:500],'details':{'extractor':'tinko-retail'}}]
    price,unit=variants[0]
    # Related cards, wholesale tiers and the hidden large-order modal do not
    # belong to this retail offer. Characteristics stay attached to this item.
    summary=soup.select_one('.product-detail__short-description, .product-detail__description')
    evidence=f'{name} · Розничная цена: {price:.2f} руб / {unit}'
    if summary:
        evidence+=' · '+clean(summary.get_text(' ',strip=True))[:1800]
    return [{'name':name,'url':url,'unit':unit,'price':price,'bucket':'materials','item_key':url,
        'price_kind':'published','reason':'','evidence':evidence,
        'details':{'price_scope':'product','quantity_terms':[],'extractor':'tinko-retail'}}]
