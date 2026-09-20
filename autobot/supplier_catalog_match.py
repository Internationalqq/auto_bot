"""Read-only matching of captured catalogue offers against a tender need."""
from datetime import datetime,timezone
from functools import lru_cache
import json
import re
import time
from decimal import Decimal,ROUND_CEILING,InvalidOperation

from autobot import supplier_catalog_store as store
from autobot.market_strategy import check_offer,normalize_unit,market_query_name,estimate_unit_multiplier
from autobot.market_evidence_policy import freshness_reason,specification_reason,price_terms_reason,observed_timestamp,evidence_ttl_days
from autobot.market_source_adapters import source_region_evidence
from autobot.supplier_evidence import supplier_identity,quantity_terms_reason,delivery_terms,outdated_price_notice


def product_family(name):
    """A grade describes a product; it does not turn a kerb into ready-mix."""
    value=store.folded(name)
    for family,pattern in (
        ('kerb',r'бордюр|бортов\w*\s+(?:кам|бетон)|камн\w*\s+бортов'),
        ('paving',r'брусчат|плитк\w*\s+тротуар|тротуар\w*\s+плитк'),
        ('mortar',r'раствор'),('concrete',r'бетон|\bбс[гтл]\b'),
        ('geotextile',r'геотекст|геополот|дорнит'),('cable',r'кабел|провод|авбшв|ввг'),
        ('sand',r'песок|песк[аиу]'),('stone',r'щебень|щебн'),
        ('steel-strip',r'полос\w*\s+стал|стал\w*\s+полос'),
        ('pipe',r'труб'),('reinforcement',r'арматур')):
        if re.search(pattern,value): return family
    return ''


def work_surfaces(name):
    value=store.folded(name)
    return {kind for kind,pattern in (
        ('concrete',r'бетон'),('brick',r'кирпич'),('drywall',r'гипсокарт|\bгкл\b'),
        ('wood',r'дерев|древес'),('metal',r'металл|сталь|стальн')) if re.search(pattern,value)}


@lru_cache(maxsize=256)
def page_facts(document_id,region,bucket,path):
    page=store.document(document_id,path)
    if not page: return {}
    return {'url':page['url'],'observed_at':page['observed_at'],
        'supplier':supplier_identity(page['body']),
        'region':source_region_evidence(page['body'],region,bucket) if region else '',
        'terms':delivery_terms(page['body'],''),'notice':outdated_price_notice(page['body'])}


def purchase_price(row,details,unit,quantity):
    """Return a price for this exact need, paying for whole proven packages."""
    evidence=row['evidence'];price=row['price_kopecks']/100
    terms=list(details.get('quantity_terms') or [])
    if row['unit']==normalize_unit(unit): return price,row['unit'],evidence,terms
    package=details.get('package') or {}
    if row['unit']!='рулон' or package.get('unit')!=normalize_unit(unit) or not package.get('evidence'): return None
    try:
        size=Decimal(str(package['amount']))
        required=Decimal(str(quantity))*Decimal(str(estimate_unit_multiplier('',unit)))
        if not size.is_finite() or not required.is_finite() or size<=0 or required<=0: return None
        count=int((required/size).to_integral_value(rounding=ROUND_CEILING))
        cost=Decimal(row['price_kopecks'])*count/100
        effective=cost/required
    except (KeyError,TypeError,ValueError,InvalidOperation): return None
    note=f'Расчёт закупки: {count} рулонов по {size} {package["unit"]}, всего {cost} руб; потребность {required} {package["unit"]}; {effective:.8f} руб / {package["unit"]} с учётом целых рулонов'
    terms.append({'lot':float(required),'unit':package['unit'],'evidence':note})
    return float(effective),package['unit'],evidence+' · '+note,terms


def lookup(*,name,unit,basis_code='',section='',region='',quantity=None,limit=5,path=None,include_candidates=False):
    if not store.ready(path): return []
    from autobot.market_price_index import build_price_identity,source_quality
    identity=build_price_identity(name,unit,basis_code,section,region)
    if identity.bucket not in {'materials','works'} or not identity.unit:
        return []
    tokens=sorted([store.folded(word) for word in identity.category_tokens if len(word)>=3],key=len,reverse=True)[:12]
    if not tokens: return []
    from autobot.market_requirements import technical_specs
    models={spec['value'] for spec in technical_specs(name) if spec['kind']=='hardware_model'}
    score=' + '.join(f"CASE WHEN i.search_text LIKE ? THEN {8 if token.replace(' ','') in models else 1} ELSE 0 END" for token in tokens)
    with store.connect(path) as con:
        rows=con.execute('''SELECT i.*,s.name AS supplier_name,s.supplier_id,'''+score+''' AS relevance
            FROM supplier_catalog_items i JOIN supplier_catalog_sources s ON s.id=i.source_id
            WHERE i.bucket=? AND (i.unit=? OR (i.unit='рулон' AND json_extract(i.details_json,'$.package.unit')=?))
            AND i.price_kind IN ('published','conditional') AND i.price_kopecks>0
            AND (? OR (i.price_kind='published' AND i.expires_at>?))
            ORDER BY relevance DESC,i.expires_at DESC LIMIT 60''',
            [*['%'+word+'%' for word in tokens],identity.bucket,identity.unit,identity.unit,include_candidates,time.time()]).fetchall()
    contexts={}; accepted=[]
    path=str(store.db_path(path).resolve())
    for row in rows:
        exact_model=bool(models) and all(model in store.folded(row['search_text']).replace(' ','') for model in models)
        if row['relevance']<min(2,len(tokens)) and not exact_model: continue
        if identity.bucket=='materials':
            wanted_family,found_family=product_family(name),product_family(row['name'])
            if wanted_family!=found_family:
                continue
        if identity.bucket=='works' and not work_surfaces(name).issubset(work_surfaces(row['name'])):
            continue
        details=json.loads(row['details_json'])
        purchase=purchase_price(row,details,str(unit),quantity)
        if purchase is None: continue
        price,selling_unit,evidence,quantity_terms=purchase
        reason=specification_reason(name,evidence)
        if reason and (not include_candidates or reason.startswith('Не совпадает')): continue
        page=page_facts(row['document_id'],str(region),identity.bucket,path)
        if not page: continue
        if row['source_id'] not in contexts:
            contexts[row['source_id']]=[page_facts(p['id'],str(region),identity.bucket,path) for p in store.context_documents(row['source_id'],path)]
        pages=[page,*contexts[row['source_id']]]
        supplier=next((p['supplier'] for p in pages if p.get('supplier')),'')
        region_evidence=''; region_url=''; terms=page['terms']
        for p in pages:
            notice=p.get('notice','')
            if notice: terms+=' '+notice
            if region and not region_evidence:
                region_evidence=p.get('region','')
                if region_evidence: region_url=p['url']
        if region and not region_evidence:
            reason=reason or 'Источник не подтверждает доставку или работу в регионе'
        check=check_offer(name=market_query_name(name),unit=unit,basis_code=basis_code,section=section,
            title=row['name'],snippet=evidence,url=row['url'],price=price,
            page_checked=True,source_unit=selling_unit,supplier_evidence=supplier)
        if check.status=='rejected' or check.reason=='Слабое совпадение с названием позиции': continue
        if check.status!='verified': reason=reason or check.reason
        offer={'title':row['name'],'price':price,'url':row['url'],
            'source':row['supplier_name'],'verification':'verified','confidence':check.confidence,
            'matched_unit':selling_unit,'unit':selling_unit,'observed_at':row['observed_at'],
            'published_at':details.get('published_at',''),'price_scope':details.get('price_scope',''),
            'evidence':evidence,'location':region_evidence,'search_region':str(region),
            'region_evidence':region_evidence,'region_source_url':region_url,'supplier_evidence':supplier,
            'delivery_terms':terms,'quantity_terms':quantity_terms,
            'seller_id':row['supplier_id'],'position_type':identity.position_type,
            'source_weight':source_quality(row['url'],row['supplier_name']),
            'match_score':check.confidence,'index_hit':True,
            'catalog_item_id':row['id'],'catalog_observation_id':row['observation_id'],
            'catalog_document_id':row['document_id']}
        published=observed_timestamp(offer['published_at'])
        reason=reason or (row['reason'] if row['price_kind']!='published' else '') or freshness_reason(offer,identity.bucket) or price_terms_reason(offer)
        if published and (published>time.time()+900 or time.time()-published>evidence_ttl_days(identity.bucket,row['url'])*86400):
            reason='Дата прайса требует обновления цены'
        reason=reason or quantity_terms_reason(offer['quantity_terms'],quantity,str(unit))
        if reason:
            if not include_candidates: continue
            offer.update(verification='candidate',verification_reason=reason)
        accepted.append(offer)
    accepted.sort(key=lambda offer:(offer['verification']!='verified',-offer['confidence']))
    return accepted[:max(1,min(20,limit))]
