"""Pure RFQ formatting shared by the server and the Mac queue worker."""
from decimal import Decimal, InvalidOperation
import re

from autobot.hermes_buyer import BuyerError, supplier_brief, validate_draft


def quantity(value):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n <= 0: raise ValueError()
        return format(n, 'f').rstrip('0').rstrip('.') if '.' in format(n,'f') else format(n,'f')
    except (InvalidOperation, ValueError, TypeError):
        raise BuyerError('Для запроса нужны положительный объём и единица каждой позиции') from None


def draft(payload, *, allow_incomplete=False):
    lines, questions = [], []
    briefs = supplier_brief(payload)['positions']
    for i, p in enumerate(payload['positions'], 1):
        unit = p.get('unit')
        missing_unit = not unit or unit in ('—', '-')
        missing_quantity = p.get('quantity') is None
        if (missing_unit or missing_quantity) and not allow_incomplete:
            raise BuyerError('Не определены объём или единица позиции')
        if missing_unit:
            questions.append(f"Уточните единицу: {p['name']}")
            unit = 'единица уточняется'
        if missing_quantity:
            questions.append(f"Уточните количество: {p['name']}")
        amount = 'количество уточняется' if missing_quantity else quantity(p['quantity']).replace('.', ',')
        multiplier = ' × ' if not missing_quantity and re.match(r'^\d', unit) else ' '
        characteristics = '; '.join(f"{c.get('label') or c.get('kind')}: {c['value']}" for c in briefs[i-1]['characteristics'] if c.get('value') is not None)
        lines.append(f"{i}. {p['name']}{'; '+characteristics if characteristics else ''} — {amount}{multiplier}{unit}.")
    works = any(p.get('type_slug') in ('work','service') for p in payload['positions'])
    goods = any(p.get('type_slug') in ('material','product') for p in payload['positions'])
    intro = 'поставить оборудование и выполнить работы по списку' if works and goods else 'выполнить такие работы' if works else 'поставить такие материалы'
    terms = (' По работам укажите отдельно стоимость работ, материалов и техники, чтобы не посчитать их дважды.' if works else '')
    region = payload.get('region')
    if not region and allow_incomplete:
        questions.append('Уточните регион объекта')
    location = f'Объект — {region}.' if region else 'Местоположение объекта уточним.'
    body = (f'Добрый день!\n\nПодскажите, сможете {intro}:\n' + '\n'.join(lines)
            + f"\n\n{location} Напишите, пожалуйста, цену за указанную единицу по каждой строке, с НДС или без, и сроки.{terms}"
            + (' По материалам уточните наличие и доставку, её стоимость укажите отдельно.' if goods else '')
            + ' Если для расчёта нужны адрес или дополнительные характеристики — уточним. Если можете предложить только замену, обозначьте её отдельно.\n\nСпасибо!')
    result = {'drafts':[{'position_keys':[p['position_key'] for p in payload['positions']],
                         'subject':'Запрос стоимости работ' if works else 'Материалы — наличие и цены', 'body':body}], 'questions':questions}
    return validate_draft(result, payload)

