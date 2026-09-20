"""Public price feeds and retail cards with explicit, source-specific units."""
import json
import re
from bs4 import BeautifulSoup
from autobot.supplier_catalog_store import clean
from autobot.market_strategy import normalize_unit


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
    summary=soup.select_one('.product-detail__description')
    evidence=f'{name} · Розничная цена: {price:.2f} руб / {unit}'
    if summary:
        evidence+=' · '+clean(summary.get_text(' ',strip=True))[:1800]
    return [{'name':name,'url':url,'unit':unit,'price':price,'bucket':'materials','item_key':url,
        'price_kind':'published','reason':'','evidence':evidence,
        'details':{'price_scope':'product','quantity_terms':[],'extractor':'tinko-retail'}}]
