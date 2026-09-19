"""Bounded link discovery in explicitly configured supplier catalogues.

The registry scopes discovery only. It never supplies a price, unit, region
proof or verification flag; those still come from the opened source page.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from autobot.market_requirements import technical_specs


REGISTRY_PATH = Path(__file__).with_name('supplier_catalogs.json')


def is_catalog_price_url(url: str) -> bool:
    """A known landing-page price table is a source, not a search page."""
    try:
        rows = json.loads(REGISTRY_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return isinstance(rows, list) and any(isinstance(row, dict) and row.get('price_page') is True
        and url == row.get('url') for row in rows)


def _fold(value: str) -> str:
    return re.sub(r'\s+', ' ', str(value or '').casefold().replace('ё', 'е'))


def _words(value: str) -> set[str]:
    stop = {'цена', 'прайс', 'купит', 'стоим', 'доста', 'руб', 'матер', 'работ', 'строи'}
    return {word[:5] for word in re.findall(r'[a-zа-я]{3,}', _fold(value))
            if word[:5] not in stop}


def _specs(value: str) -> set[str]:
    folded = _fold(value).replace('m', 'м').replace('b', 'в')
    result = {re.sub(r'\s+', '', part).replace(',', '.') for part in re.findall(
        r'[а-я]\s*\d{2,}(?:[.,]\d+)?|\d+\s*[-–—]\s*\d+', folded)}
    return result | {spec['value'] for spec in technical_specs(value)}


def catalog_sources(query: str) -> list[dict]:
    """Only visit suppliers whose declared region and topic fit this query."""
    try:
        rows = json.loads(REGISTRY_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    folded = _fold(query)
    words = _words(query)
    selected = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        regions, topics = row.get('regions', []), row.get('topics', [])
        if (not isinstance(regions, list) or not isinstance(topics, list)
                or not all(isinstance(value, str) and value.strip() for value in regions + topics)
                or not regions or not any(_fold(region) in folded for region in regions)
                or not words.intersection(_words(' '.join(topics)))):
            continue
        start = str(row.get('url') or '')
        try:
            parsed = urlparse(start)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.port:
                continue
            if parsed.hostname == 'avito.ru' or parsed.hostname.endswith('.avito.ru'):
                continue
        except ValueError:
            continue
        selected.append(row)
    return selected[:3]


@dataclass(frozen=True)
class CatalogPage:
    url: str
    title: str
    snippet: str


def catalogue_page(page_html: str, url: str) -> CatalogPage:
    soup = BeautifulSoup(page_html or '', 'html.parser')
    title = soup.find('h1') or soup.title
    title_text = title.get_text(' ', strip=True)[:500] if title else urlparse(url).hostname
    for node in soup.select('script,style,noscript,nav,footer,form'):
        node.decompose()
    return CatalogPage(url, title_text,
                       soup.get_text(' ', strip=True)[:1000])


def catalog_links(page_html: str, base_url: str, query: str, *, limit: int = 2) -> list[str]:
    """Read actual same-site catalogue links; never invent product URLs."""
    soup = BeautifulSoup(page_html or '', 'html.parser')
    wanted = _words(query)
    specs = _specs(query)
    base = urlparse(base_url)
    found = {}
    for anchor in soup.select('a[href]'):
        title = anchor.get_text(' ', strip=True)
        try:
            parsed = urlparse(urljoin(base_url, str(anchor.get('href') or '')))
            if (parsed.scheme not in {'http', 'https'} or parsed.hostname != base.hostname
                    or parsed.username or parsed.port or parsed.query
                    or re.search(r'\.(?:pdf|xlsx?|docx?|zip|jpe?g|png)$', parsed.path, re.I)
                    or any(part in parsed.path.casefold() for part in
                           ('/order', '/cart', '/login', '/news', '/blog', '/stati', '/contact'))):
                continue
        except ValueError:
            continue
        url = parsed._replace(fragment='').geturl()
        if url.rstrip('/') == base_url.rstrip('/'):
            continue
        overlap = len(wanted & _words(title))
        is_catalog = bool(re.search(r'прайс|каталог|продукци|цены', _fold(title)))
        spec_hits = len(specs & _specs(title))
        if not overlap and not is_catalog and not spec_hits:
            continue
        score = overlap * 10 + spec_hits * 15 + int(is_catalog)
        # A metre-priced cable is easier to compare than an otherwise matching
        # 50/100 m coil. This only orders real links; it never converts a price.
        if re.search(r'кабел|провод', _fold(query)) and re.search(r'\b\d+(?:[.,]\d+)?\s*м\b', _fold(title)):
            score -= 5
        found[url] = max(found.get(url, 0), score)
    return sorted(found, key=lambda url: -found[url])[:max(0, min(3, limit))]


def discover_catalog_pages(query: str, load_page, *, limit: int = 8) -> list[CatalogPage]:
    """Read at most three catalogues; let verification open product links."""
    result = []
    for source in catalog_sources(query):
        start = source['url']
        html = load_page(start)
        if not html:
            continue
        if source.get('price_page') is True:
            result.append(catalogue_page(html, start))
        # Leave product navigation to the price verifier so a slow second
        # supplier cannot spend the entire budget before any price is checked.
        labels = {urljoin(start, str(anchor.get('href') or '')).split('#', 1)[0]:
                  anchor.get_text(' ', strip=True)[:500]
                  for anchor in BeautifulSoup(html, 'html.parser').select('a[href]')}
        for url in catalog_links(html, start, query, limit=1):
            if len(result) >= limit:
                break
            title = labels.get(url) or source.get('name') or urlparse(url).hostname
            result.append(CatalogPage(url, title, title))
        if len(result) >= limit:
            break
    return result[:limit]
