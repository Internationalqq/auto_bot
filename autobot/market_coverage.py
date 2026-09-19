"""Explain every estimate row without treating missing evidence as a zero cost."""
from collections import Counter


def position_outcome(position: dict) -> tuple[str, str]:
    if position.get('type_slug') == 'aggregate':
        return 'excluded', 'Сводная строка: отдельная рыночная цена не требуется'
    if not position.get('can_auto_price'):
        issues = (position.get('requirements') or {}).get('issues') or []
        return 'needs_details', position.get('warning') or '; '.join(issues) or 'Уточните название и единицу'
    if position.get('verified_count'):
        return 'verified', 'Есть сопоставимая цена из проверенного источника'
    if position.get('candidate_count'):
        return 'candidate', 'Предложения найдены, но требуют уточнения перед расчётом'
    if position.get('market_processed'):
        reason = str(position.get('market_status') or '')
        if any(marker in reason.casefold() for marker in ('капч', 'captcha', 'защит', '403', '429', 'доступ ограничен')):
            return 'blocked', reason
        return 'no_quote', reason or 'Поиск завершён без подтверждённой цены'
    return 'pending', 'Поиск ещё не выполнялся'


def annotate_coverage(positions: list[dict]) -> dict:
    counts = Counter()
    for row in positions:
        state, reason = position_outcome(row)
        row['price_state'], row['price_reason'] = state, reason
        counts[state] += 1
    return {'total': len(positions), **{key: counts[key] for key in (
        'verified', 'candidate', 'needs_details', 'blocked', 'no_quote', 'pending', 'excluded')}}
