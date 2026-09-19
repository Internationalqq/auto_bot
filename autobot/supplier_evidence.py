"""Supplier discovery and terms. A search snippet is never price evidence."""
import re
import math
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


REFERENCE_HOSTS = ('fsnb2022.ru', 'fgisrf.ru', 'smetnoedelo.ru', 'meganorm.ru',
                   'docs.cntd.ru', 'base.garant.ru', 'consultant.ru',
                   'classinform.ru', 'stroyinf.ru')


def reference_source(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or '').casefold().removeprefix('www.')
    except ValueError:
        return True
    return any(host == root or host.endswith('.' + root) for root in REFERENCE_HOSTS)


def supplier_identity(page_html: str) -> str:
    """Keep a public contact and business label from this page, not the query."""
    soup = BeautifulSoup(page_html, 'html.parser')
    for node in soup.select('script,style,noscript'):
        node.decompose()
    text = soup.get_text(' ', strip=True)
    contact = soup.select_one('a[href^="tel:"]')
    phone = (str(contact.get('href'))[4:] if contact else '')
    if not phone:
        match = re.search(r'(?:\+7|8)\s*\(?\d{3,5}\)?[\s-]*\d[\d\s()-]{5,16}\d', text)
        phone = match.group() if match else ''
    heading = soup.find('h1') or soup.title
    label = heading.get_text(' ', strip=True)[:240] if heading else ''
    if not phone or not re.search(r'купить|продаж|производ|завод|постав|магазин|услуг|прайс|бетон|подряд', text, re.I):
        return ''
    return f'{label} · телефон {phone}'[:400]


def supplier_context_links(page_html: str, url: str) -> list[str]:
    """At most two existing same-host contact/delivery links, without forms."""
    base = urlsplit(url)
    found = []
    for a in BeautifulSoup(page_html, 'html.parser').select('a[href]'):
        label = a.get_text(' ', strip=True)
        try:
            target = urlsplit(urljoin(url, str(a.get('href') or '')))
            port = target.port
        except ValueError:
            continue
        if (target.scheme not in {'http', 'https'} or target.hostname != base.hostname
                or target.username or target.query or port
                or not re.search(r'контакт|доставк|реквизит', label, re.I)):
            continue
        value = target._replace(fragment='').geturl()
        if value.rstrip('/') != url.rstrip('/') and value not in found:
            found.append(value)
    return found[:2]


def delivery_terms(page_html: str, evidence: str) -> str:
    if re.search(r'без\s+(?:уч[её]та\s+)?доставки', evidence, re.I):
        return 'Цена без доставки. Доставка до объекта рассчитывается отдельно.'
    soup = BeautifulSoup(page_html, 'html.parser')
    for node in soup.select('p,li'):
        value = node.get_text(' ', strip=True)
        if len(value) <= 600 and re.search(r'цен[аы].{0,35}(?:указана с доставкой|включает доставку|без доставки)', value, re.I):
            return value[:500]
    return 'Стоимость доставки до объекта не подтверждена; в цену автоматически не добавляется.'


def quantity_terms_reason(terms: object, quantity: object, unit: str) -> str:
    """Read-only validation is repeated after reload as well as on capture."""
    from autobot.market_strategy import normalize_unit, estimate_unit_multiplier
    if not isinstance(terms, list) or not terms:
        return ''
    try:
        amount = float(quantity) * estimate_unit_multiplier('', unit)
    except (TypeError, ValueError):
        amount = 0
    if not math.isfinite(amount) or amount <= 0:
        return 'Цена зависит от партии; укажите объём для проверки условий поставщика'
    for condition in terms:
        if not isinstance(condition, dict):
            return 'Не удалось прочитать условия объёма поставщика'
        if normalize_unit(condition.get('unit')) != normalize_unit(unit):
            return 'Условия объёма поставщика указаны в другой единице; требуется уточнение'
        try:
            quote_from = float(condition['quote_from']) if condition.get('quote_from') is not None else None
            lot = float(condition['lot']) if condition.get('lot') is not None else None
            if any(value is not None and (not math.isfinite(value) or value <= 0) for value in (quote_from, lot)):
                return 'Не удалось прочитать условия объёма поставщика'
        except (ValueError, TypeError):
            return 'Не удалось прочитать условия объёма поставщика'
        if quote_from is not None and amount >= quote_from:
            return 'На объём сметы поставщик просит расчёт: ' + str(condition.get('evidence') or '')
        if lot is not None and abs(amount - lot) > 1e-6:
            return 'Опубликованная цена относится к другой партии: ' + str(condition.get('evidence') or '')
    return ''
