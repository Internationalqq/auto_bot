"""Read-only matching of captured catalogue offers against a tender need."""
from datetime import datetime,timezone
from functools import lru_cache
import json
import re
import time

from autobot import supplier_catalog_store as store
from autobot.market_strategy import check_offer,normalize_unit,market_query_name
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


def lookup(*,name,unit,basis_code='',section='',region='',quantity=None,limit=5,path=None):
    if not store.ready(path): return []
    from autobot.market_price_index import build_price_identity,source_quality
    identity=build_price_identity(name,unit,basis_code,section,region)
    if identity.bucket not in {'materials','works'} or not identity.unit:
        return []
    tokens=[store.folded(word) for word in identity.category_tokens if len(word)>=3][:10]
    if not tokens: return []
    score=' + '.join("CASE WHEN i.search_text LIKE ? THEN 1 ELSE 0 END" for _ in tokens)
    with store.connect(path) as con:
        rows=con.execute('''SELECT i.*,s.name AS supplier_name,s.supplier_id,'''+score+''' AS relevance
            FROM supplier_catalog_items i JOIN supplier_catalog_sources s ON s.id=i.source_id
            WHERE i.bucket=? AND i.unit=? AND i.price_kind='published' AND i.price_kopecks>0 AND i.expires_at>?
            ORDER BY relevance DESC,i.observed_at DESC LIMIT 40''',
            [*['%'+word+'%' for word in tokens],identity.bucket,identity.unit,time.time()]).fetchall()
    contexts={}; accepted=[]
    path=str(store.db_path(path).resolve())
    for row in rows:
        if row['relevance']<min(2,len(tokens)): continue
        if identity.bucket=='materials':
            wanted_family,found_family=product_family(name),product_family(row['name'])
            if wanted_family!=found_family:
                continue
        if identity.bucket=='works' and not work_surfaces(name).issubset(work_surfaces(row['name'])):
            continue
        details=json.loads(row['details_json'])
        evidence=row['evidence']
        if specification_reason(name,evidence): continue
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
        if region and not region_evidence: continue
        check=check_offer(name=market_query_name(name),unit=unit,basis_code=basis_code,section=section,
            title=row['name'],snippet=evidence,url=row['url'],price=row['price_kopecks']/100,
            page_checked=True,source_unit=row['unit'],supplier_evidence=supplier)
        if check.status!='verified': continue
        offer={'title':row['name'],'price':row['price_kopecks']/100,'url':row['url'],
            'source':row['supplier_name'],'verification':'verified','confidence':check.confidence,
            'matched_unit':row['unit'],'unit':row['unit'],'observed_at':row['observed_at'],
            'published_at':details.get('published_at',''),'price_scope':details.get('price_scope',''),
            'evidence':evidence,'location':region_evidence,'search_region':str(region),
            'region_evidence':region_evidence,'region_source_url':region_url,'supplier_evidence':supplier,
            'delivery_terms':terms,'quantity_terms':details.get('quantity_terms') or [],
            'seller_id':row['supplier_id'],'position_type':identity.position_type,
            'source_weight':source_quality(row['url'],row['supplier_name']),
            'match_score':check.confidence,'index_hit':True,
            'catalog_item_id':row['id'],'catalog_observation_id':row['observation_id'],
            'catalog_document_id':row['document_id']}
        published=observed_timestamp(offer['published_at'])
        reason=freshness_reason(offer,identity.bucket) or price_terms_reason(offer)
        if published and (published>time.time()+900 or time.time()-published>evidence_ttl_days(identity.bucket,row['url'])*86400):
            reason='Дата прайса требует обновления цены'
        reason=reason or quantity_terms_reason(offer['quantity_terms'],quantity,str(unit))
        if reason: continue
        accepted.append(offer)
        if len(accepted)>=max(1,min(20,limit)): break
    return accepted
