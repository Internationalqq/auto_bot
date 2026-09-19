"""Display-only comparison of prices already approved by the market contract."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def price_difference(estimate, market):
    empty = {'difference_kopecks': None, 'difference_fmt': '—',
             'difference_percent_fmt': '', 'difference_tone': 'empty'}
    try:
        if estimate is None or market is None or isinstance(estimate, bool) or isinstance(market, bool):
            return empty
        left, right = Decimal(str(estimate)), Decimal(str(market))
        if not left.is_finite() or not right.is_finite() or left < 0 or right <= 0:
            return empty
        delta = (left - right).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        percent = (delta / left * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP) if left else None
    except (InvalidOperation, ValueError, TypeError):
        return empty
    sign = '+' if delta > 0 else '−' if delta < 0 else ''
    amount = f'{abs(delta):,.2f}'.replace(',', ' ').replace('.', ',')
    percent_text = (('+' if percent > 0 else '−' if percent < 0 else '') +
                    f'{abs(percent):.1f}'.replace('.', ',') + '% от сметы') if percent is not None else ''
    return {'difference_kopecks': int(delta * 100), 'difference_fmt': f'{sign}{amount} ₽',
            'difference_percent_fmt': percent_text,
            'difference_tone': 'good' if delta > 0 else 'bad' if delta < 0 else 'equal'}
