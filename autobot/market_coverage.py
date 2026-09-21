"""Explain every estimate row without treating missing evidence as a zero cost."""
from collections import Counter
import hashlib
import json
import math


def search_result_reason(notes: object) -> str:
    text = str(notes or '').casefold()
    if 'лимит времени' in text or 'searchbudgetexceeded' in text:
        return 'Поиск выполнен; времени на проверку всех источников не хватило'
    if any(value in text for value in ('captcha', 'капч', 'антибот', 'доступ ограничен', '403', '429')):
        return 'Поиск выполнен; доступ к части сайтов ограничен'
    if any(value in text for value in ('httperror', 'runtimeerror', 'timeout', 'ddgsexception')):
        return 'Поиск выполнен; часть источников не ответила или не дала результатов'
    return 'Поиск выполнен; сопоставимая цена пока не найдена'


def reconcile_search_history(positions: list[dict], jobs: list[dict], *, region: str = '') -> None:
    """Explain older empty publications without rewriting historical data."""
    from autobot.market_evidence_policy import region_key
    latest = {}
    for job in sorted(jobs, key=lambda j: float(j.get('created_at') or 0), reverse=True):
        latest.setdefault(job.get('position_key'), job)
    for position in positions:
        job = latest.get(position.get('position_key'))
        if not job or position.get('market_processed') or job.get('status') != 'completed':
            continue
        payload = job.get('payload') or {}
        if region_key(payload.get('region')) != region_key(region):
            continue
        position['market_processed'] = True
        position['market_status'] = search_result_reason((job.get('result') or {}).get('notes'))


def position_outcome(position: dict) -> tuple[str, str]:
    if position.get('quantity') is not None and position['quantity']<=0:
        return 'excluded','Вычет или нулевой объём сметы: закупка не требуется'
    if position.get('type_slug') == 'aggregate':
        return 'excluded', 'Расчётная строка: отдельная рыночная цена не требуется'
    if not position.get('can_auto_price'):
        issues = (position.get('requirements') or {}).get('issues') or []
        return 'needs_details', position.get('warning') or '; '.join(issues) or 'Уточните название и единицу'
    if position.get('verified_count'):
        return 'verified', 'Есть сопоставимая цена из проверенного источника'
    if position.get('candidate_count'):
        return 'candidate', 'Предложения найдены, но требуют уточнения перед расчётом'
    if position.get('market_processed'):
        reason = str(position.get('market_status') or '')
        if any(marker in reason.casefold() for marker in ('капч', 'captcha', 'защит', '403', '429', 'доступ ограничен', 'доступ к части сайтов ограничен')):
            return 'blocked', reason
        return 'no_quote', reason or 'Поиск завершён без подтверждённой цены'
    return 'pending', 'Поиск ещё не выполнялся'


def annotate_coverage(positions: list[dict]) -> dict:
    counts = Counter()
    for row in positions:
        state, reason = position_outcome(row)
        row['price_state'], row['price_reason'] = state, reason
        counts[state] += 1
    return {'total': len(positions), 'priceable': len(positions) - counts['excluded'], **{key: counts[key] for key in (
        'verified', 'candidate', 'needs_details', 'blocked', 'no_quote', 'pending', 'excluded')}}


def coverage_plan(positions: list[dict], *, target_percent: int = 90) -> dict:
    """Plan discovery for every unmet need, without promoting its candidates.

    Group identical discovery requirements but retain all row identities and
    quantities: a price for one pack/lot cannot be copied to another volume.
    """
    if not isinstance(target_percent,int) or not 1<=target_percent<=100:
        raise ValueError('Target must be between 1 and 100 percent')
    counts=Counter();groups={}
    for position in positions:
        state,_=position_outcome(position)
        counts[state]+=1
        if state in {'excluded','verified'}: continue
        scope=position.get('resource_scope') or {'kind':'unknown' if position.get('has_resources') else 'none'}
        passport=position.get('requirements') or {}
        component_types=[{'name':c['name'],'unit':c['unit'],'kind':c['kind']} for c in scope.get('components',[])]
        signature=json.dumps([position.get('name'),passport.get('original_unit') or position.get('unit'),
            position.get('bucket'),scope['kind'],component_types],ensure_ascii=False,separators=(',',':'))
        key=hashlib.sha256(signature.encode()).hexdigest()[:24]
        need=groups.setdefault(key,{'id':key,'name':position.get('name',''),
            'unit':passport.get('original_unit') or position.get('unit',''),
            'bucket':position.get('bucket',''),'requirements':passport,
            'resource_scope':{'kind':scope['kind'],'components':component_types},
            'queries':list(position.get('queries') or []),'positions':[],'reasons':[],
            'next_step':('clarify_requirements' if state=='needs_details' else
                'service_with_consumables' if scope['kind']=='auxiliary_only' else
                'complete_composition' if scope['kind']!='none' else 'supplier_catalogue')})
        need['positions'].append({'position_key':position.get('position_key',''),
            'quantity':position.get('quantity'),'state':state,'resource_scope':scope})
        for source in position.get('sources') or []:
            reason=source.get('reason') or ''
            if reason and reason not in need['reasons']:need['reasons'].append(reason)
    priceable=len(positions)-counts['excluded']
    required=math.ceil(priceable*target_percent/100)
    needs=sorted(groups.values(),key=lambda g:(-len(g['positions']),g['bucket'],g['name']))
    return {'target_percent':target_percent,'priceable':priceable,'verified':counts['verified'],
        'required_verified':required,'missing_to_target':max(0,required-counts['verified']),
        'target_reached':bool(priceable and counts['verified']>=required),
        'uncovered_rows':priceable-counts['verified'],'unique_needs':len(needs),'needs':needs}
