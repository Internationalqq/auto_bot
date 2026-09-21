"""Public price feeds and retail cards with explicit, source-specific units."""
import json
import re
from bs4 import BeautifulSoup
from autobot.supplier_catalog_store import clean
from autobot.market_strategy import normalize_unit


def keepmarket_records(body,url):
    """A cable's metre rate and compulsory reel are separate source facts."""
    from decimal import Decimal
    from autobot.market_source_adapters import parse_ruble_values
    from autobot.market_evidence_policy import price_terms_reason
    from autobot.supplier_evidence import outdated_price_notice
    soup=BeautifulSoup(body,'html.parser')
    heading=soup.find('h1');prices=soup.select('.prod_price #block_price')
    selling=[node for node in soup.select('.block_efg') if 'Цена указана за:' in clean(node.get_text(' ',strip=True))
             and 'Минимальная кратность продажи:' in clean(node.get_text(' ',strip=True))]
    if heading is None or len(prices)!=1 or len(selling)!=1:
        raise ValueError('Не выделен блок цены и кратности KeepMarket')
    name=clean(heading.get_text(' ',strip=True));price_text=clean(prices[0].get_text(' ',strip=True))
    amounts=parse_ruble_values(price_text);conditions=clean(selling[0].get_text(' ',strip=True))
    unit=re.search(r'Цена указана за:\s*1\s+м\.',conditions)
    package=re.search(r'Минимальная кратность продажи:\s*(\d+(?:[.,]\d+)?)\s*(км|м)\b',conditions)
    if len(amounts)!=1 or amounts[0]<=0 or not unit or not package:
        raise ValueError('Нет однозначной цены за метр и кратности KeepMarket')
    size=Decimal(package[1].replace(',','.'))*(1000 if package[2]=='км' else 1)
    if not size.is_finite() or size<=0: raise ValueError('Некорректная кратность KeepMarket')
    rate=Decimal(str(amounts[0]));total=rate*size
    reason=price_terms_reason({'evidence':price_text}) or outdated_price_notice(body)
    proof=f'{conditions}; {rate} руб/м; стоимость партии {size} м = {total} руб'
    return [{'name':name,'url':url,'unit':'упак','price':float(total),'bucket':'materials','item_key':url,
        'price_kind':'conditional' if reason else 'published','reason':reason,'evidence':name+' · '+proof,
        'details':{'price_scope':'product','extractor':'keepmarket-cable-reel',
                   'package':{'amount':float(size),'unit':'м','evidence':proof}}}]


def electrical_records(body, url):
    """Bind the displayed selling unit to the current Product's own offer."""
    from decimal import Decimal, InvalidOperation
    from urllib.parse import urlparse
    from autobot.supplier_evidence import outdated_price_notice
    soup=BeautifulSoup(body,'html.parser')
    heading=soup.find('h1')
    name=clean(heading.get_text(' ',strip=True)) if heading else ''
    products=[]
    for node in soup.select('script[type="application/ld+json"]'):
        try: value=json.loads(node.get_text())
        except (ValueError,TypeError): continue
        for product in value if isinstance(value,list) else [value]:
            if not isinstance(product,dict) or product.get('@type')!='Product': continue
            if clean(product.get('name'))!=name: continue
            if urlparse(str(product.get('url','')))._replace(fragment='',query='')!=urlparse(url)._replace(fragment='',query=''): continue
            products.append(product)
    if len(products)!=1: raise ValueError('Не удалось выделить предложение основной карточки Электрики')
    offer=products[0].get('offers') or {}
    if not isinstance(offer,dict) or offer.get('priceCurrency')!='RUB':
        raise ValueError('В основной карточке нет рублёвого предложения')
    try: amount=Decimal(str(offer['price']))
    except (KeyError,InvalidOperation,TypeError): raise ValueError('В основной карточке нет цены')
    if not amount.is_finite() or amount<=0: raise ValueError('Некорректная цена основной карточки')
    units=set()
    for block in soup.select('div.items-baseline'):
        text=clean(block.get_text(' ',strip=True))
        match=re.fullmatch(r'([\d\s]+(?:[.,]\d+)?)\s*₽\s*/\s*(м|шт|кг|упак)\.?',text)
        if match and Decimal(re.sub(r'\s','',match[1]).replace(',','.'))==amount:
            units.add(normalize_unit(match[2]))
    if len(units)!=1: raise ValueError('Цена основной карточки не связана с видимой единицей продажи')
    unit=units.pop()
    availability=str(offer.get('availability','')).rsplit('/',1)[-1]
    reason=outdated_price_notice(body)
    if availability not in {'InStock','PreOrder','BackOrder'}: reason=reason or 'Доступность товара не подтверждена'
    return [{'name':name,'url':url,'unit':unit,'price':float(amount),'bucket':'materials',
        'item_key':url,'price_kind':'conditional' if reason else 'published','reason':reason,
        'evidence':f'{name} · Цена основной карточки: {amount} руб / {unit}',
        'details':{'price_scope':'product','extractor':'electrical-main-product',
                   'availability':availability,'sku':products[0].get('sku','')}}]


def megapolis_records(body, url):
    """Each displayed selector offer keeps its own dimensions, film and price."""
    from decimal import Decimal,InvalidOperation
    from autobot.supplier_evidence import outdated_price_notice
    soup=BeautifulSoup(body,'html.parser')
    widget=soup.find('catalog-offers')
    if widget is None:
        # Preserve single-card prices for review; do not invent a selling unit.
        heading=soup.select_one('h1.cardInfo__title')
        if heading is None: return []
        name=clean(heading.get_text(' ',strip=True));records=[]
        for script in soup.select('script[type="application/ld+json"]'):
            try: product=json.loads(script.get_text())
            except (ValueError,TypeError): continue
            if not isinstance(product,dict) or product.get('@type')!='Product': continue
            if clean(product.get('name'))!=name or product.get('url')!=url: continue
            offer=product.get('offers') or {}
            if not isinstance(offer,dict) or offer.get('priceCurrency')!='RUB': continue
            try: amount=Decimal(str(offer.get('price')))
            except (InvalidOperation,TypeError): continue
            if not amount.is_finite() or amount<=0: continue
            records.append({'name':name,'url':url,'unit':'','price':float(amount),'bucket':'materials',
                'item_key':url,'price_kind':'conditional','reason':'Единица продажи не опубликована',
                'evidence':f'{name} · Цена карточки: {amount} руб; единица продажи не указана',
                'details':{'price_scope':'product','extractor':'megapolis-single-product','sku':product.get('sku','')}})
        if len(records)>1: raise ValueError('Неоднозначная основная карточка ПК Мегаполис')
        return records
    try: variants=json.loads(widget.get(':offers','')); params=json.loads(widget.get(':params','{}'))
    except (ValueError,TypeError): raise ValueError('Не удалось разобрать варианты ПК Мегаполис')
    if not isinstance(variants,list) or not variants or not isinstance(params,dict):
        raise ValueError('Нет вариантов товара ПК Мегаполис')
    records=[];notice=outdated_price_notice(body)
    for variant in variants:
        prices=variant.get('ITEM_PRICES') or []
        selected=variant.get('ITEM_PRICE_SELECTED',0)
        if not isinstance(selected,int) or not 0<=selected<len(prices):
            raise ValueError('Не определена цена варианта ПК Мегаполис')
        price=prices[selected]
        if price.get('CURRENCY')!='RUB': raise ValueError('Неизвестная валюта варианта ПК Мегаполис')
        try: amount=Decimal(str(price.get('BASE_PRICE')))
        except (InvalidOperation,TypeError): raise ValueError('Некорректная цена варианта ПК Мегаполис')
        if not amount.is_finite() or amount<=0: raise ValueError('Нет положительной цены варианта ПК Мегаполис')
        unit=normalize_unit((variant.get('ITEM_MEASURE') or {}).get('TITLE',''))
        properties=variant.get('PROPERTIES') or {}
        traits={key:{'label':clean(properties.get(key,{}).get('NAME') or params[key].get('NAME')),
                     'value':clean(properties.get(key,{}).get('VALUE'))} for key in params}
        name=clean(variant.get('NAME'))
        if re.match(r'^\d+\.\d+',name): name='Дорожный знак '+name
        detail='; '.join(f'{v["label"]}: {v["value"]}' for v in traits.values() if v['value'])
        name+='; '+detail if detail else ''
        reason=notice
        if not all(v['value'] for v in traits.values()): reason=reason or 'Не заполнены характеристики варианта'
        if unit not in {'шт','м','кг','компл'}: reason=reason or 'Не указана единица продажи'
        if variant.get('CAN_BUY') is not True: reason=reason or 'Вариант недоступен к заказу'
        ratios=variant.get('ITEM_MEASURE_RATIOS') or {}
        if any(float(r.get('RATIO',0))!=1 for r in ratios.values()):
            reason=reason or 'Требуется проверка кратности продажи'
        terms=[]
        minimum=price.get('QUANTITY_FROM') or price.get('MIN_QUANTITY')
        if minimum and float(minimum)>1: terms.append({'minimum':float(minimum),'unit':unit,'evidence':f'Минимальная партия {minimum} {unit}'})
        if price.get('QUANTITY_TO') is not None: reason=reason or 'Цена ограничена верхним объёмом партии'
        records.append({'name':name,'url':url,'unit':unit,'price':float(amount),'bucket':'materials',
            'item_key':url+'|offer:'+str(variant['ID']),'price_kind':'conditional' if reason else 'published','reason':reason,
            'evidence':f'{name} · Цена варианта: {amount} руб / {unit}',
            'details':{'price_scope':'product','extractor':'megapolis-offer-selector',
                       'variant_id':str(variant['ID']),'properties':traits,'quantity_terms':terms}})
    return records


def tdatm_records(body,url):
    """Use the current offer and base unit, retaining its stock restriction."""
    from decimal import Decimal,InvalidOperation
    from autobot.supplier_evidence import outdated_price_notice
    soup=BeautifulSoup(body,'html.parser')
    heading=soup.select_one('h1.changeName')
    prices=soup.select('meta[itemprop="price"]');currencies=soup.select('meta[itemprop="priceCurrency"]')
    if heading is None or len(prices)!=1 or len(currencies)!=1 or currencies[0].get('content')!='RUB':
        raise ValueError('Не выделено рублёвое предложение основной карточки ТД АТМ')
    try: amount=Decimal(str(prices[0].get('content')))
    except (InvalidOperation,TypeError): raise ValueError('Некорректная цена ТД АТМ')
    if not amount.is_finite() or amount<=0: raise ValueError('Нет положительной цены ТД АТМ')
    units=[]
    for row in soup.select('table.stats tr'):
        cells=row.find_all('td',recursive=False)
        if len(cells)>=2 and clean(cells[0].get_text())=='Базовая единица':
            units.append(normalize_unit(cells[1].get_text(' ',strip=True)))
    if len(units)!=1 or units[0] not in {'м','шт','упак'}: raise ValueError('Нет единицы продажи ТД АТМ')
    availability=soup.select('[itemprop="availability"]')
    available=len(availability)==1 and str(availability[0].get('href','')).endswith('/InStock')
    reason=outdated_price_notice(body) or ('' if available else 'Нет подтверждённого наличия: поставщик указал OutOfStock или не указал статус')
    name=clean(heading.get_text(' ',strip=True)).split(', КАБЕЛЬНАЯ ПРОДУКЦИЯ')[0]
    return [{'name':name,'url':url,'unit':units[0],'price':float(amount),'bucket':'materials',
        'item_key':url,'price_kind':'conditional' if reason else 'published','reason':reason,
        'evidence':f'{name} · Цена карточки {amount} руб / {units[0]} · '+reason,
        'details':{'price_scope':'product','extractor':'tdatm-main-product','availability':'InStock' if available else 'unconfirmed'}}]


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
