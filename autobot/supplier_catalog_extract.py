"""Catalogue navigation and extraction. Links and page facts are separate."""
from datetime import datetime,timezone
import re
from urllib.parse import urljoin,urlparse,parse_qs

from bs4 import BeautifulSoup
from autobot import supplier_catalog_store as store
from autobot.market_source_adapters import inspect_source_page, detect_price_unit, parse_ruble_values
from autobot.market_strategy import normalize_unit
from autobot.market_evidence_policy import price_terms_reason
from autobot.supplier_evidence import outdated_price_notice


def clean_soup(body):
    soup=BeautifulSoup(body,'html.parser')
    for icon in soup.select('i.fa-rub, i.fa-ruble-sign, i.fa-ruble'):
        icon.replace_with(' руб. ' + icon.get_text(' ', strip=True))
    for node in soup.select('script,style,input,button,select,textarea,.related,.upsells,.recommendations,.analogs'):
        node.decompose()
    for node in soup.select('form'):
        node.unwrap()  # Price tables may sit inside a filter form; no submission.
    return soup


def navigate(body,url,config):
    soup=clean_soup(body)
    catalog=config.get('catalog') or {}
    start=urlparse(config['url'])
    adapter=catalog.get('adapter')
    result={}
    seeds=list(catalog.get('seed_urls') or [])
    if adapter=='yamck' and '/api/site/nerud' in body:
        seeds.append('/api/site/nerud')
    for candidate in seeds[:100]:
        link=urljoin(url,str(candidate))
        parsed=urlparse(link)
        if parsed.scheme in {'http','https'} and parsed.hostname==start.hostname and not parsed.username and not parsed.port and link!=url:
            result[link]={'url':link,'kind':'product','label':''}
    for a in soup.select('a[href]'):
        link=urljoin(url,str(a.get('href','')).strip())
        parsed=urlparse(link)
        if parsed.scheme not in {'http','https'} or parsed.hostname!=start.hostname or parsed.username or parsed.port:
            continue
        link=parsed._replace(fragment='').geturl()
        label=store.clean(a.get_text(' ',strip=True))
        kind=''
        if adapter=='svetelektro' and re.fullmatch(re.escape(start.path.rstrip('/'))+r'(?:/\d{1,5})?/?',parsed.path) and not parsed.query:
            kind='catalog'
        elif parsed.path==start.path and parsed.query:
            query=parse_qs(parsed.query)
            if set(query)<={'page'} and all(v.isdigit() and 1<=int(v)<=100 for v in query.get('page',[])):
                kind='catalog'
        elif any(w in label.casefold() for w in ('контакты','реквизиты','доставка и оплата')) or label.casefold()=='доставка':
            kind='context'
        elif adapter=='ekc' and parsed.path.startswith('/cena/') and a.find_parent('tr'):
            kind='product'
        elif adapter=='geo76' and parsed.path.startswith('/p/') and a.find_parent(class_='catalog-item-well'):
            kind='product'
        if kind and link!=url:
            result[link]={'url':link,'kind':kind,'label':label}
    return list(result.values())


def table_records(body,url,bucket):
    soup=clean_soup(body)
    result=[]
    price_notice=outdated_price_notice(body)
    notices=' '.join(str(p) for p in soup.select('p,small') if len(p.get_text())<700 and
        (price_terms_reason({'evidence':p.get_text(' ',strip=True)}) or 'без стоимости материал' in p.get_text().casefold()))
    for table_no,table in enumerate(soup.select('table')):
        layouts=[]; parents={}; headers=[]
        for row in table.select('tr'):
            cells=row.find_all(['td','th'],recursive=False)
            values=[store.clean(c.get_text(' ',strip=True)) for c in cells]
            if len(values)<2: continue
            price_cols=[i for i,v in enumerate(values) if re.search(r'\bцен[аы]\b|стоимость',v,re.I) and not parse_ruble_values(v)]
            if price_cols:
                headers=values
                layouts=[]
                for p in price_cols:
                    start=max([j+1 for j in price_cols if j<p],default=0)
                    unit_cols=[j for j in (range(len(values)) if len(price_cols)==1 else range(start,p)) if re.search(r'ед\.?\s*изм',values[j],re.I)]
                    names=[j for j in range(start,p) if j not in unit_cols and values[j] and not re.fullmatch(r'№|п/?п|номер',values[j],re.I)]
                    if names: layouts.append((names[-1],p,unit_cols[-1] if unit_cols else None,values[p],values[names[-1]]))
                continue
            for name_index,price_index,unit_index,heading,name_heading in layouts:
                if len(values)<=max(name_index,price_index): continue
                name=values[name_index]; price_text=values[price_index]
                if len(name)<3: continue
                if not price_text and name.endswith(':'):
                    parents[name_index]=name.rstrip(':'); continue
                if name.startswith(('-', '–','—')):
                    if name_index not in parents: continue
                    name=parents[name_index]+': '+name.lstrip('-–— ').strip()
                elif price_text:
                    parents.pop(name_index,None)
                if re.search(r'марка\s+бетона',name_heading,re.I) and not re.search(r'бетон',name,re.I):
                    name='Бетон '+name
                if re.search(r'марка\s+раствора',name_heading,re.I) and not re.search(r'раствор',name,re.I):
                    name='Раствор '+name
                raw_unit=values[unit_index] if unit_index is not None and len(values)>unit_index else ''
                raw_unit=re.sub(r'^пог\.?\s*м\.?$','м',raw_unit,flags=re.I)
                unit=normalize_unit(raw_unit) if raw_unit else ''
                lot=re.match(r'^(\d+(?:[.,]\d+)?)\s*(.+)$',raw_unit)
                if lot and float(lot[1].replace(',','.'))!=1:
                    unit=raw_unit  # 8 hours or 100 metres is not a unit price.
                unit=unit or detect_price_unit(re.sub(r'\bм\s+([23])\b',r'м\1',price_text+' '+heading))
                amounts=parse_ruble_values(price_text)
                if not amounts and re.fullmatch(r'\d[\d\s.,]*',price_text):
                    amounts=[float(price_text.replace(' ','').replace(',','.'))]
                amount=amounts[0] if amounts else None
                evidence=name+' | '+price_text+' | '+unit+' · '+heading
                reason=price_terms_reason({'evidence':evidence}) or price_notice
                if lot and float(lot[1].replace(',','.'))!=1:
                    reason=reason or 'Цена указана за '+raw_unit+'; пересчёт требует проверки условий'
                if not unit: reason=reason or 'В прайсе не указана единица цены'
                if len(set(amounts))>1: reason=reason or 'В строке указан диапазон или несколько цен'
                # Inspect only this column group, retaining the source's own headers and notices.
                import html
                fragment='<table><tr><th>Наименование</th><th>Ед. изм.</th><th>'+html.escape(heading)+'</th></tr><tr><td>'+html.escape(name)+'</td><td>'+html.escape(unit)+'</td><td>'+html.escape(price_text)+'</td></tr></table>'+notices
                inspection=inspect_source_page(fragment,url,name=name,target_unit=unit,position_bucket=bucket)
                if not inspection.accepted: reason=reason or inspection.reason
                if inspection.price is not None and amount is not None and abs(inspection.price-amount)>0.001:
                    reason=reason or 'Не удалось однозначно связать сумму со строкой прайса'
                item_bucket='equipment' if re.search(r'^аренда\b',name,re.I) else bucket
                published=''
                date_columns=[j for j,v in enumerate(headers) if re.search(r'актуаль|дата',v,re.I)]
                if len(date_columns)==1 and len(values)>date_columns[0]:
                    for pattern in ('%d.%m.%y','%d.%m.%Y'):
                        try: published=datetime.strptime(values[date_columns[0]],pattern).replace(tzinfo=timezone.utc).isoformat(); break
                        except ValueError: pass
                result.append({'name':name,'url':url,'unit':unit,'bucket':item_bucket,'price':amount,
                    'item_key':f'table:{table_no}|{store.folded(name)}|{unit}',
                    'price_kind':'on_request' if amount is None else 'conditional' if reason else 'published',
                    'reason':reason,'evidence':evidence,
                    'details':{'price_scope':inspection.price_scope,'quantity_terms':list(inspection.quantity_terms),'published_at':published,
                        'price_prefix':'от' if re.search(r'\bот\s*\d',price_text,re.I) else '', 'extractor':'supplier-table'}})
    return result


def product_records(body,url,label,adapter,bucket):
    soup=clean_soup(body)
    h1=soup.find('h1')
    name=store.clean(h1.get_text(' ',strip=True) if h1 else label)
    if not name: return []
    if adapter=='elektro':
        variants=[]
        for node in soup.select('.active_price_st'):
            text=store.clean(node.get_text(' ',strip=True))
            amounts=parse_ruble_values(text)
            pack=re.search(r'за\s+(\d+(?:[.,]\d+)?)\s+(м|кг|шт)\.?$',text,re.I)
            if len(amounts)==1 and pack:
                size=float(pack[1].replace(',','.'))
                if size>0: variants.append((amounts[0],size,normalize_unit(pack[2]),text))
        if len({v[:3] for v in variants})!=1:
            raise ValueError('Не удалось связать цену ЭКС с явно указанным объёмом упаковки')
        price,size,unit,text=variants[0]
        reason=price_terms_reason({'evidence':text}) or outdated_price_notice(body)
        package={'amount':size,'unit':unit,'evidence':text} if size!=1 else {}
        return [{'name':name,'url':url,'unit':'упак' if package else unit,'bucket':bucket,'price':price,
            'item_key':url,'price_kind':'conditional' if reason else 'published','reason':reason,
            'evidence':name+' · '+text,'details':{'price_scope':'product','extractor':'elektro-visible-package','package':package}}]
    if adapter=='geo76':
        price_nodes=[n for n in soup.select('h3') if re.match(r'^Цена\s*:',n.get_text(' ',strip=True),re.I)]
        description=soup.select_one('.product-desc_short')
        selling_terms=store.clean(description.get_text(' ',strip=True)) if description else ''
        if len(price_nodes)==1 and re.search(r'цена\s+указана\s+за\s+(?:1\s+)?рулон',selling_terms,re.I):
            price_text=store.clean(price_nodes[0].get_text(' ',strip=True))
            amounts=parse_ruble_values(price_text)
            if len(amounts)==1:
                # This site puts a rounded area price in microdata while its
                # visible purchase price is for a whole roll. Store the latter.
                reason=price_terms_reason({'evidence':price_text}) or outdated_price_notice(body)
                areas=re.findall(r'Площадь\s+покрытия\s*:\s*(\d+(?:[.,]\d+)?)\s*м[2²]',selling_terms,re.I)
                package={'amount':float(areas[0].replace(',','.')),'unit':'м2','evidence':selling_terms} if len(set(areas))==1 else {}
                return [{'name':name,'url':url,'unit':'рулон','bucket':bucket,'price':amounts[0],
                    'item_key':url,'price_kind':'conditional' if reason else 'published','reason':reason,
                    'evidence':name+' · '+price_text+' · '+selling_terms,
                    'details':{'price_scope':'product','extractor':'geo76-visible-roll','selling_terms':selling_terms,'package':package}}]
    if adapter=='ekc':
        result=[]
        table=soup.select_one('table.offerTable')
        for row in table.select('tr') if table else []:
            title=row.select_one('td.title')
            price=row.select_one('td.price [itemprop="price"]')
            currency=row.select_one('td.price [itemprop="priceCurrency"]')
            price_unit=row.select_one('td.input .colWo')
            if title is None: continue
            variant=store.clean(title.get_text(' ',strip=True))
            if not variant: continue
            if not re.search(r'кабель',variant,re.I): variant='Кабель '+variant
            unit=normalize_unit(price_unit.get_text(' ',strip=True)) if price_unit else ''
            amount=price.get('content') if price and currency and currency.get('content')=='RUB' else None
            date=row.select_one('td.date'); published=''
            if date:
                try: published=datetime.strptime(date.get_text(strip=True),'%d.%m.%Y').replace(tzinfo=timezone.utc).isoformat()
                except ValueError: pass
            stock=row.select_one('td.amount'); stock_text=store.clean(stock.get_text(' ',strip=True)) if stock else ''
            evidence=variant+' · Цена: '+str(amount or 'по запросу')+' руб / '+unit
            reason=price_terms_reason({'evidence':evidence}) or outdated_price_notice(body)
            if not unit: reason=reason or 'У предложения не указана единица цены'
            result.append({'name':variant,'url':url,'unit':unit,'bucket':bucket,'price':amount,
                'item_key':url+'|'+store.folded(variant),
                'price_kind':'on_request' if amount is None else 'conditional' if reason else 'published',
                'reason':reason,'evidence':evidence,
                'details':{'price_scope':'product','published_at':published,'stock':stock_text,'extractor':'ekc-offer-row'}})
        if result: return result
    # Source adapters establish their own selling unit; physical dimensions do not.
    unit='м' if adapter=='ekc' else 'м2' if adapter=='geo76' else ''
    check=inspect_source_page(body,url,name=name,target_unit=unit,position_bucket=bucket)
    evidence=check.evidence
    reason='' if check.accepted else check.reason
    reason=reason or outdated_price_notice(body)
    details={'price_scope':check.price_scope,'quantity_terms':list(check.quantity_terms),'extractor':check.extractor}
    # Product characteristics on EKC are in the card, outside its price block.
    # Keep them as a distinct proven text, never recommendation titles.
    properties=[]
    for row in soup.select('table tr'):
        value=store.clean(row.get_text(' ',strip=True))
        if len(value)<=400 and re.search(r'напряжение|число жил|сечение|тип жил|марка кабеля|номинальн',value,re.I):
            properties.append(value)
    details['specification_evidence']=' · '.join(properties)[:2400]
    if details['specification_evidence']:
        evidence += ' · '+details['specification_evidence']
    selling_unit=check.unit
    if not selling_unit:
        reason=reason or 'В карточке не указана единица цены'
    if adapter=='geo76':
        selling_unit=''
        reason='Не удалось связать цену с единицей продажи в карточке поставщика'
    return [{'name':name,'url':url,'unit':selling_unit,'bucket':bucket,'price':check.price,
        'item_key':url,'price_kind':'on_request' if check.price is None else 'conditional' if reason else 'published',
        'reason':reason,'evidence':evidence or name,'details':details}]


def extract(body,url,kind,label,config):
    adapter=(config.get('catalog') or {}).get('adapter','')
    bucket=(config.get('catalog') or {}).get('bucket') or (config.get('buckets') or ['materials'])[0]
    folded=body[:25000].casefold()
    if any(s in folded for s in ('servicepipe.tech','checking your browser','подтвердите, что вы не робот')):
        raise ValueError('Сайт ограничил доступ; импорт остановлен для этой страницы')
    if kind=='context': return []
    if adapter=='ak511':
        from autobot.supplier_catalog_sites import ak511_records
        return ak511_records(body,url)
    if adapter=='anbik':
        from autobot.supplier_catalog_sites import anbik_records
        return anbik_records(body,url)
    if adapter=='esg':
        from autobot.supplier_catalog_sites import esg_records
        return esg_records(body,url)
    if adapter=='yamck':
        if urlparse(url).path=='/api/site/nerud':
            from autobot.supplier_catalog_sites import yamck_records
            return yamck_records(body,config['url'])
        return []
    if adapter=='tinko':
        from autobot.supplier_catalog_sites import tinko_records
        return tinko_records(body,url)
    if kind=='product' or adapter in {'product','elektro'}: return product_records(body,url,label,adapter,bucket)
    if adapter in {'table','svetelektro'}:
        records=table_records(body,url,bucket)
        if not records: raise ValueError('Не удалось разобрать строки прайса; прежние данные сохранены')
        return records
    return []
