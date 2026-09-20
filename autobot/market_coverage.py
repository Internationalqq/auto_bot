"""Explain every estimate row without treating missing evidence as a zero cost."""
from collections import Counter


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
