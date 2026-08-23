"""Direct-page adapters used after search discovers a candidate URL.

Search results are discovery only.  An adapter must find the matching price and
unit on the source page before that number may enter a market calculation.
"""

from __future__ import annotations

import html as html_mod
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from autobot.market_strategy import normalize_unit, units_compatible


_ANTIBOT_HARD_MARKERS = ("servicepipe.tech", "checking your browser", "cf-chl-", "id_spinner")
_ANTIBOT_PAGE_MARKERS = ("подтвердите, что вы не робот", "доступ ограничен", "слишком много запросов")
_LISTING_PATH_MARKERS = ("/tag-page/", "/search/", "/category/")
_NOISE_PRICE_MARKERS = (
    "стоимость доставки", "доставка от", "доставка:",
    "рассрочка", "кредит", "кешбэк", "кэшбэк", "экономия",
    "скидка по карте", "бонус", "монтаж от", "выезд от",
)
_RUBLE_VALUE_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[\s\u00a0\u202f]\d{3})*(?:[.,]\d{1,2})?|\d{2,9}(?:[.,]\d{1,2})?)\s*(?:₽|руб(?:\.|лей|ля)?|р\.)",
    re.IGNORECASE,
)
_STOP_WORDS = {
    "для", "при", "под", "над", "без", "или", "как", "что", "это", "его",
    "она", "они", "из", "от", "до", "по", "на", "в", "во", "с", "со", "и",
    "к", "а", "не", "за", "у", "работ", "работы", "услуг", "услуги", "цена",
    "стоимость", "купить", "монтаж", "устройство", "укладка", "установка",
    "демонтаж", "разработка", "перевозка", "погрузка", "вывоз",
}


@dataclass(frozen=True)
class PageInspection:
    accepted: bool
    status: str
    adapter: str
    price: float | None = None
    unit: str = ""
    price_scope: str = ""
    title: str = ""
    evidence: str = ""
    reason: str = ""
    extractor: str = ""
    facts_found: int = 0


@dataclass(frozen=True)
class _PriceFact:
    title: str
    price: float
    unit: str
    scope: str
    evidence: str
    extractor: str
    overlap: float


def _clean(value: object) -> str:
    text = html_mod.unescape(str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def _fold(value: object) -> str:
    return _clean(value).casefold().replace("ё", "е")


def _tokens(value: object) -> set[str]:
    return {
        word for word in re.findall(r"[0-9a-zа-я]{2,}", _fold(value))
        if word not in _STOP_WORDS
    }


def _overlap(name: str, evidence: str) -> float:
    wanted = _tokens(name)
    found = _tokens(evidence)
    if not wanted:
        return 0.0
    common = wanted & found
    return len(common) / max(1, min(len(wanted), 8))


def _range_specs(value: object) -> set[tuple[int, int]]:
    folded = _fold(value)
    return {
        (int(match.group(1)), int(match.group(2)))
        for match in re.finditer(r"(?<!\d)(\d{1,4})\s*[-–—]\s*(\d{1,4})(?!\d)", folded)
    }


def _specification_compatible(name: str, evidence: str) -> bool:
    """Keep a priced variant from replacing another numeric variant.

    A 5–40 row on a 20–40 aggregate page can otherwise win on generic words
    such as "щебень" and "фракция".  If the estimate contains a numeric range,
    the local price evidence must contain that exact range.
    """

    wanted = _range_specs(name)
    if not wanted:
        return True
    found = _range_specs(evidence)
    return bool(wanted & found)


def _parse_number(raw: object) -> float | None:
    value = re.sub(r"[^0-9,.]", "", str(raw or "")).strip(".,")
    if "," in value and "." in value:
        decimal = "," if value.rfind(",") > value.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        value = value.replace(thousands, "").replace(decimal, ".")
    elif value.count(",") == 1 and len(value.rsplit(",", 1)[1]) <= 2:
        value = value.replace(",", ".")
    elif value.count(".") == 1 and len(value.rsplit(".", 1)[1]) <= 2:
        pass
    else:
        value = value.replace(",", "").replace(".", "")
    try:
        number = float(value)
    except ValueError:
        return None
    return number if 10 <= number <= 500_000_000 else None


def parse_ruble_values(text: object) -> list[float]:
    raw = _clean(text).replace("−", "-")
    out: list[float] = []
    for match in _RUBLE_VALUE_RE.finditer(raw):
        number = _parse_number(match.group(1).replace(" ", "").replace("\u00a0", "").replace("\u202f", ""))
        if number is not None and number not in out:
            out.append(number)
    return out


def _is_noise_price_context(context: object, name: object) -> bool:
    folded = _fold(context)
    wanted = _fold(name)
    for marker in _NOISE_PRICE_MARKERS:
        if marker not in folded or marker in wanted:
            continue
        if marker.startswith("достав") or "доставк" in marker:
            # "Щебень с доставкой — 750 ₽/м³" is still the product price;
            # "Доставка от 500 ₽" without product words is not.
            if _overlap(str(name or ""), str(context or "")) >= 0.28:
                continue
        return True
    return False


def _is_ruble_currency(value: object) -> bool:
    currency = _fold(value).strip(" .")
    return not currency or currency in {"rub", "rur", "руб", "рубль", "₽", "643"}


def detect_price_unit(text: object) -> str:
    value = _fold(text).replace("²", "2").replace("³", "3")
    checks = (
        ("м2", (r"(?:руб(?:\.|лей|ля|ль)?|₽)\s*(?:/|за)?\s*(?:1\s*)?(?:кв\.?\s*м|м\s*2|m\s*2|mtk)", r"за\s+(?:квадрат\w*\s+метр|м\s*2)")),
        ("м3", (r"(?:руб(?:\.|лей|ля|ль)?|₽)\s*(?:/|за)?\s*(?:1\s*)?(?:куб(?:\.?\s*м)?|м\s*3|m\s*3|mtq)\b", r"за\s+(?:кубичес\w*\s+метр|м\s*3)")),
        ("пог.м", (r"(?:руб(?:\.|лей|ля|ль)?|₽)\s*(?:/|за)?\s*(?:пог\.?\s*м|погон\w*\s+метр)",)),
        ("кг", (r"(?:руб(?:\.|лей|ля|ль)?|₽)\s*(?:/|за)?\s*(?:кг|килограмм\w*|kgm)",)),
        ("т", (r"(?:руб(?:\.|лей|ля|ль)?|₽)\s*(?:/|за)?\s*(?:\bт\b|тонн\w*)",)),
        ("л", (r"(?:руб(?:\.|лей|ля|ль)?|₽)\s*(?:/|за)?\s*(?:\bл\b|литр\w*)",)),
        ("шт", (r"(?:руб(?:\.|лей|ля|ль)?|₽)\s*(?:/|за)?\s*(?:шт\.?|штук\w*|единиц\w*|pce)",)),
        ("час", (r"(?:руб(?:\.|лей|ля|ль)?|₽)\s*(?:/|за)?\s*(?:час\w*|чел\.?-?ч|маш\.?-?ч)",)),
        ("смена", (r"(?:руб(?:\.|лей|ля|ль)?|₽)\s*(?:/|за)?\s*смен\w*",)),
        ("м", (r"(?:руб(?:\.|лей|ля|ль)?|₽)\s*(?:/|за)?\s*(?:1\s*)?(?:метр\w*|\bм\.?\b|mtr)(?!\s*[23])",)),
    )
    for unit, patterns in checks:
        if any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns):
            return unit
    # Price lists often put the unit in a separate table cell before the price.
    if re.search(r"\b(?:кв\.?\s*м|м\s*2)\b", value):
        return "м2"
    if re.search(r"\b(?:куб(?:\.?\s*м)?|м\s*3)\b", value):
        return "м3"
    if re.search(r"\b(?:пог\.?\s*м|м\.?п\.?)\b", value):
        return "пог.м"
    return ""


def _scope(text: object, page_text: object = "") -> str:
    local = _fold(text)
    whole = _fold(page_text)
    if "без материал" in local or "материал заказчика" in local:
        return "work_only"
    if "под ключ" in local or "включен" in local and "материал" in local or "с материал" in local:
        return "with_materials"
    if "работы проводим с материалом заказчика" in whole or "без стоимости материал" in whole:
        return "work_only"
    # A rate for the named operation on an already prepared base is a labour
    # rate.  Keep this narrower than a generic "укладка" check: contractor
    # pages frequently advertise turnkey packages in the same price list.
    if "на готовое основание" in local and "под ключ" not in local:
        return "work_only"
    # Строка прайса на конкретную услугу является ценой работы, если рядом нет
    # явного указания, что в неё входят материалы. Иначе обычные прайсы
    # подрядчиков без фразы «без материалов» отбрасывались целиком.
    work_markers = (
        "работ", "услуг", "монтаж", "укладк", "установк", "демонтаж",
        "разработк", "перевозк", "погрузк", "вывоз", "аренд",
    )
    material_markers = ("под ключ", "с материал", "материалы включ", "материал включ")
    if any(marker in local for marker in work_markers) and not any(marker in local for marker in material_markers):
        return "work_only"
    return "unknown"


def _price_segments(tag: object) -> list[str]:
    """Return visual price-list lines without splitting inline ``sup`` units.

    A surprising number of contractor sites put an entire price table into one
    paragraph separated only by ``<br>``.  Treating that paragraph as one fact
    pairs the first (cheapest) number with a later, better-matching operation.
    """

    raw = str(tag)
    marker = " AUTOBOT_PRICE_LINE_BREAK "
    raw = re.sub(r"<br\s*/?>", marker, raw, flags=re.IGNORECASE)
    fragment = BeautifulSoup(raw, "lxml")
    text = fragment.get_text(" ", strip=True)
    parts = [_clean(part) for part in text.split(marker)]
    return [part for part in parts if part]


def _jsonld_facts(soup: BeautifulSoup, name: str) -> list[_PriceFact]:
    facts: list[_PriceFact] = []

    def walk(
        node: object,
        inherited_title: str = "",
        inherited_unit: str = "",
        inherited_currency: str = "",
    ) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, inherited_title, inherited_unit, inherited_currency)
            return
        if not isinstance(node, dict):
            return
        node_title = _clean(node.get("name") or inherited_title)
        node_unit = _clean(node.get("unitText") or node.get("unitCode") or inherited_unit)
        node_currency = _clean(node.get("priceCurrency") or inherited_currency)
        raw_type = node.get("@type")
        node_types = {_fold(item) for item in raw_type} if isinstance(raw_type, list) else {_fold(raw_type)}
        price_context = bool(node_types & {"offer", "aggregateoffer", "unitpricespecification", "pricespecification"}) or "priceCurrency" in node
        if price_context and _is_ruble_currency(node_currency):
            for key in ("price", "lowPrice"):
                price = _parse_number(node.get(key))
                if price is None:
                    continue
                evidence = _clean(f"{node_title} {price} руб. {node_unit}")
                facts.append(_PriceFact(node_title or name, price, normalize_unit(node_unit) or detect_price_unit(evidence), "product", evidence, "json-ld", _overlap(name, evidence)))
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value, node_title, node_unit, node_currency)

    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            walk(json.loads(script.get_text(" ", strip=True)))
        except Exception:
            continue
    return facts


def _microdata_facts(soup: BeautifulSoup, name: str) -> list[_PriceFact]:
    facts: list[_PriceFact] = []
    seen: set[tuple[float, str]] = set()
    for price_tag in soup.select("[itemprop='price']", limit=250):
        raw_price = price_tag.get("content") or price_tag.get("value") or price_tag.get_text(" ", strip=True)
        price = _parse_number(raw_price)
        if price is None:
            continue
        container = price_tag.find_parent(attrs={"itemscope": True}) or price_tag.parent
        currency_tag = container.select_one("[itemprop='priceCurrency']") if container else None
        currency = ""
        if currency_tag is not None:
            currency = _clean(currency_tag.get("content") or currency_tag.get_text(" ", strip=True))
        if not _is_ruble_currency(currency):
            continue
        name_tag = container.select_one("[itemprop='name']") if container else None
        title = _clean(
            (name_tag.get("content") if name_tag and name_tag.get("content") else name_tag.get_text(" ", strip=True) if name_tag else "")
            or name
        )
        unit_tag = container.select_one("[itemprop='unitText'], [itemprop='unitCode']") if container else None
        unit_text = _clean(
            unit_tag.get("content") or unit_tag.get_text(" ", strip=True)
            if unit_tag is not None else ""
        )
        evidence = _clean(f"{title} {price} руб. {unit_text}")
        key = (price, evidence)
        if key in seen:
            continue
        seen.add(key)
        facts.append(_PriceFact(title, price, normalize_unit(unit_text) or detect_price_unit(evidence), "product", evidence, "microdata", _overlap(name, evidence)))
    return facts


def _meta_fact(soup: BeautifulSoup, name: str, position_bucket: str) -> list[_PriceFact]:
    title = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    descriptions: list[str] = []
    for attr in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attr)
        if tag and tag.get("content"):
            descriptions.append(_clean(tag.get("content")))
    facts: list[_PriceFact] = []
    seen: set[tuple[str, float]] = set()
    for source_text in (title, *descriptions):
        # Не связываем первую попавшуюся цену страницы с чужой услугой из
        # дальнейшей части description. Каждая фраза становится отдельным фактом.
        segments = re.split(r"(?<=[.!?;])\s+|\s+[|•]\s+", source_text)
        for segment in segments:
            evidence = _clean(segment)
            if not evidence or _is_noise_price_context(evidence, name):
                continue
            unit = detect_price_unit(evidence)
            scope = _scope(evidence) if position_bucket == "works" else "product"
            overlap = _overlap(name, evidence)
            for price in parse_ruble_values(evidence):
                key = (evidence, price)
                if key in seen:
                    continue
                seen.add(key)
                facts.append(_PriceFact(title or name, price, unit, scope, evidence[:1200], "metadata", overlap))
    return facts


def _block_facts(soup: BeautifulSoup, name: str, page_text: str, position_bucket: str) -> list[_PriceFact]:
    facts: list[_PriceFact] = []
    seen: set[str] = set()
    selectors = (
        "tr, li, p, [itemprop='offers'], [itemprop='priceSpecification'], "
        ".price-wrapper, .product__pr-price-new, .product-price, .product_price, .price-current"
    )
    # На справочных порталах бывают десятки тысяч p/li. Для подтверждения цены
    # достаточно первых релевантных блоков; полный обход только замораживает UI.
    for tag in soup.select(selectors, limit=600):
        header = tag.find_previous(["h1", "h2", "h3", "h4"])
        context = _clean(header.get_text(" ", strip=True) if header else "")
        if getattr(tag, "name", "") == "tr":
            table = tag.find_parent("table")
            if table is not None:
                table_heading = _clean(" ".join(cell.get_text(" ", strip=True) for cell in table.select("caption, thead th, thead td")))
                context = _clean(f"{context}. {table_heading}")
        for text in _price_segments(tag):
            if len(text) < 8 or len(text) > 900 or text in seen:
                continue
            if _is_noise_price_context(text, name):
                continue
            values = parse_ruble_values(text)
            meta_price = tag.find("meta", attrs={"itemprop": "price"})
            if meta_price and meta_price.get("content"):
                parsed_meta = _parse_number(meta_price.get("content"))
                if parsed_meta is not None:
                    values.insert(0, parsed_meta)
            values = list(dict.fromkeys(values))
            if not values:
                continue
            seen.add(text)
            evidence = _clean(f"{context}. {text}")[:1600]
            unit = detect_price_unit(evidence)
            fact_scope = _scope(evidence, page_text) if position_bucket == "works" else "product"
            # The surrounding page heading only identifies the service family.
            # The priced row itself must match the requested operation; otherwise
            # a cheap neighbouring line (for example geotextile installation)
            # can be attached to a paving query.
            # Service tables must match the priced row itself, but product cards
            # commonly keep the product name in a heading and only the amount in
            # the price block.  Include that heading for materials.
            overlap = _overlap(name, text if position_bucket == "works" else evidence)
            for price in values[:3]:
                facts.append(_PriceFact(text[:240], price, unit, fact_scope, evidence, "price-block", overlap))
    return facts


def _table_row_facts(soup: BeautifulSoup, name: str, position_bucket: str) -> list[_PriceFact]:
    """Bind each table price to its own column header and product row.

    Supplier price lists often expose both RUB/m3 and RUB/tonne in the same row.
    Treating the whole row as one text block can attach the tonne price to the
    cubic-metre header, so preserve the visual column relationship here.
    """

    facts: list[_PriceFact] = []
    seen: set[tuple[str, float, str]] = set()
    for table in soup.select("table")[:120]:
        header_row = table.select_one("thead tr")
        if header_row is None:
            header_row = next((row for row in table.select("tr")[:3] if row.find("th")), None)
        headers = (
            [_clean(cell.get_text(" ", strip=True)) for cell in header_row.find_all(["th", "td"], recursive=False)]
            if header_row is not None
            else []
        )
        for row in table.select("tbody tr") or table.select("tr"):
            if header_row is not None and row is header_row:
                continue
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) < 2:
                continue
            title = _clean(cells[0].get_text(" ", strip=True))
            if not title:
                continue
            for index, cell in enumerate(cells[1:], start=1):
                cell_text = _clean(cell.get_text(" ", strip=True))
                values = parse_ruble_values(cell_text)
                if not values:
                    continue
                header = headers[index] if index < len(headers) else ""
                unit = detect_price_unit(f"{header} {cell_text}")
                evidence = _clean(f"{title}. {header}: {cell_text}")[:1600]
                scope = _scope(evidence) if position_bucket == "works" else "product"
                overlap = _overlap(name, title)
                for price in values[:3]:
                    key = (title.casefold(), price, unit)
                    if key in seen:
                        continue
                    seen.add(key)
                    facts.append(_PriceFact(title[:240], price, unit, scope, evidence, "table-row", overlap))
    return facts


def _inline_price_facts(page_text: str, name: str, position_bucket: str) -> list[_PriceFact]:
    """Extract every explicit RUB mention with a narrow local context."""

    facts: list[_PriceFact] = []
    seen: set[tuple[float, str]] = set()
    matches = list(_RUBLE_VALUE_RE.finditer(page_text or ""))
    for index, match in enumerate(matches):
        price = _parse_number(match.group(1))
        if price is None:
            continue
        previous_end = matches[index - 1].end() if index else 0
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(page_text)
        # Never borrow a unit or product title through a neighbouring price.
        start = max(previous_end, match.start() - 90)
        end = min(next_start, match.end() + 90)
        evidence = _clean(page_text[start:end])
        if not evidence or _is_noise_price_context(evidence, name):
            continue
        key = (price, evidence)
        if key in seen:
            continue
        seen.add(key)
        unit = detect_price_unit(evidence)
        scope = _scope(evidence, page_text) if position_bucket == "works" else "product"
        local_title = _clean(page_text[start:match.start()])[-240:] or evidence[:240]
        facts.append(_PriceFact(local_title, price, unit, scope, evidence, "inline-regex", _overlap(name, evidence)))
        if len(facts) >= 250:
            break
    return facts


def _best_fact(facts: list[_PriceFact], target_unit: str, position_bucket: str, name: str) -> _PriceFact | None:
    target = normalize_unit(target_unit)

    def rank(fact: _PriceFact) -> tuple[float, float, float, float]:
        unit_score = 1.0 if target and fact.unit == target else 0.0
        scope_score = 1.0 if position_bucket != "works" or fact.scope == "work_only" else 0.0
        extractor_score = {
            "json-ld": 1.0,
            "table-row": 0.96,
            "microdata": 0.92,
            "price-block": 0.78,
            "metadata": 0.64,
            "inline-regex": 0.55,
        }.get(fact.extractor, 0.4)
        relevance = fact.overlap * 0.78 + extractor_score * 0.22
        return (unit_score, scope_score, relevance, extractor_score)

    suitable = [
        fact
        for fact in facts
        if fact.overlap >= 0.28 and _specification_compatible(name, fact.title)
    ]
    if target:
        same_unit = [fact for fact in suitable if units_compatible(fact.unit, target)]
        if same_unit:
            suitable = same_unit
    if not suitable:
        return None
    return max(suitable, key=rank)


def inspect_source_page(
    page_html: str,
    url: str,
    *,
    name: str,
    target_unit: str,
    position_bucket: str,
) -> PageInspection:
    host = urlparse(url).netloc.casefold().split(":", 1)[0]
    path = urlparse(url).path.casefold()
    raw_fold = _fold((page_html or "")[:80_000])
    if not page_html:
        return PageInspection(False, "unavailable", "http", reason="Источник вернул пустую страницу")
    looks_blocked = any(marker in raw_fold for marker in _ANTIBOT_HARD_MARKERS)
    looks_blocked = looks_blocked or any(marker in raw_fold[:18_000] for marker in _ANTIBOT_PAGE_MARKERS)
    # A normal shop page may have a captcha only in its callback form.
    looks_blocked = looks_blocked or ("captcha" in raw_fold and len(page_html) < 15_000)
    if looks_blocked:
        adapter = "avito" if "avito.ru" in host else "catalog"
        return PageInspection(False, "blocked", adapter, reason="Источник показал антибот-защиту")
    soup = BeautifulSoup(page_html, "lxml")
    page_text = _clean(soup.get_text(" ", strip=True))[:180_000]
    listing_path = (
        any(marker in path for marker in _LISTING_PATH_MARKERS)
        or path.rstrip("/").endswith("/catalog")
        or "/catalog/" in path
    )
    has_product_signal = bool(
        soup.select_one("[itemtype*='Product'], [itemprop='price'], [data-product-id], .product-card, .product-detail")
        or (soup.find("h1") and re.search(r"(?:₽|руб(?:\.|ля|лей)?)", page_text, flags=re.IGNORECASE))
    )
    if position_bucket == "materials" and listing_path and not has_product_signal:
        return PageInspection(False, "listing", "material-catalog", reason="Найдена категория, а не карточка конкретного товара")

    adapter = "avito" if "avito.ru" in host else "work-price-list" if position_bucket == "works" else "material-product"
    facts = _jsonld_facts(soup, name)
    facts.extend(_microdata_facts(soup, name))
    facts.extend(_meta_fact(soup, name, position_bucket))
    facts.extend(_table_row_facts(soup, name, position_bucket))
    facts.extend(_block_facts(soup, name, page_text, position_bucket))
    facts.extend(_inline_price_facts(page_text, name, position_bucket))
    best = _best_fact(facts, target_unit, position_bucket, name)
    if best is None:
        reason = "На странице не найдена рублёвая цена" if not facts else f"Найдено цен: {len(facts)}, но ни одна не относится к позиции и единице"
        return PageInspection(False, "no-match", adapter, reason=reason, facts_found=len(facts))
    target = normalize_unit(target_unit)
    if target and not units_compatible(best.unit, target):
        return PageInspection(False, "unit-mismatch", adapter, best.price, best.unit, best.scope, best.title, best.evidence, "Единица цены не совпала со сметой", best.extractor, len(facts))
    if position_bucket == "works" and best.scope != "work_only":
        reason = "Цена включает материалы" if best.scope == "with_materials" else "Не удалось отделить работу от материалов"
        return PageInspection(False, "scope-unknown", adapter, best.price, best.unit, best.scope, best.title, best.evidence, reason, best.extractor, len(facts))
    return PageInspection(True, "verified", adapter, best.price, best.unit, best.scope, best.title, best.evidence, "Цена подтверждена на странице источника", best.extractor, len(facts))
