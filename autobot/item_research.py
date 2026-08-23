"""
Краткая справка по конкретной услуге/товару/материалу.

Использует уже подключённые рыночные источники из real_market_scraper:
Авито и обычный веб-поиск. Ничего не додумывает: характеристики,
назначение, преимущества/недостатки и сроки берутся только из найденных
названий/сниппетов/страниц выдачи.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import statistics
import sys
from dataclasses import dataclass
from typing import Iterable

from autobot.paths import REPO_ROOT
from autobot.real_market_scraper import AvitoBrowserFetcher, MarketOffer, research_position_market

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass


DEFAULT_SOURCES = ["web", "avito"]
VALID_SOURCES = {"avito", "web"}
REMOTE_CITY_TOKENS = (
    "москва",
    "московск",
    "санкт-петербург",
    "петербург",
    "ленинградск",
)

KNOWN_GEO_SCOPES: dict[str, dict[str, object]] = {
    "миасс": {
        "search_regions": ["Миасс", "Челябинск", "Челябинская область"],
        "allow_tokens": ["миасс", "челябинск", "челябинская", "златоуст", "чебаркуль", "карабаш"],
        "prefer_tokens": ["миасс", "челябинск", "челябинская"],
        "label": "Миасс + рядом до 150 км (Челябинск/область)",
    },
    "чебаркуль": {
        "search_regions": ["Чебаркуль", "Челябинск", "Челябинская область"],
        "allow_tokens": ["чебаркуль", "челябинск", "челябинская", "миасс", "златоуст"],
        "prefer_tokens": ["чебаркуль", "челябинск", "челябинская"],
        "label": "Чебаркуль + рядом до 150 км (Челябинск/область)",
    },
    "златоуст": {
        "search_regions": ["Златоуст", "Челябинск", "Челябинская область"],
        "allow_tokens": ["златоуст", "челябинск", "челябинская", "миасс", "чебаркуль"],
        "prefer_tokens": ["златоуст", "челябинск", "челябинская"],
        "label": "Златоуст + рядом до 150 км (Челябинск/область)",
    },
    "челябинск": {
        "search_regions": ["Челябинск", "Челябинская область"],
        "allow_tokens": ["челябинск", "челябинская", "миасс", "златоуст", "чебаркуль", "карабаш"],
        "prefer_tokens": ["челябинск", "челябинская"],
        "label": "Челябинск и область",
    },
}


@dataclass
class ItemResearchResult:
    query: str
    region: str
    sources: list[str]
    offers: list[MarketOffer]
    errors: str = ""
    unit: str = ""
    position_type: str = ""
    position_label: str = ""
    strategy: str = ""
    warning: str = ""


def parse_sources(raw: str | None) -> list[str]:
    """Разобрать список источников. При мусоре возвращает безопасный дефолт."""
    parts = [p.strip().lower() for p in str(raw or "").split(",") if p.strip()]
    good = [p for p in parts if p in VALID_SOURCES]
    return good or DEFAULT_SOURCES.copy()


def _uniq_keep_order(items: Iterable[str], *, limit: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        s = re.sub(r"\s+", " ", str(item or "").strip(" .;:,—-"))
        if not s:
            continue
        key = s.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _sentences(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = re.split(r"[\n;]+|(?<=[.!?])\s+", raw)
    clean_parts = (re.sub(r"\s+", " ", p).strip() for p in parts)
    return _uniq_keep_order((p for p in clean_parts if len(p.strip()) >= 12), limit=40)


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").casefold().replace("ё", "е")).strip()


def _geo_scope(region: str) -> dict[str, object]:
    raw = re.sub(r"\s+", " ", str(region or "").strip())
    if not raw:
        return {"search_regions": [""], "allow_tokens": [], "prefer_tokens": [], "label": ""}
    low = _norm_text(raw)
    for key, scope in KNOWN_GEO_SCOPES.items():
        if key in low:
            return dict(scope)
    token = low.split(",")[0].strip()
    return {
        "search_regions": [raw],
        "allow_tokens": [token] if token else [],
        "prefer_tokens": [token] if token else [],
        "label": raw,
    }


def _offer_geo_score(offer: MarketOffer, *, allow_tokens: list[str], prefer_tokens: list[str]) -> int:
    text = _norm_text(f"{offer.title} {offer.snippet} {offer.url}")
    score = 0
    if any(tok and tok in text for tok in prefer_tokens):
        score += 100
    elif any(tok and tok in text for tok in allow_tokens):
        score += 40
    if any(tok in text for tok in REMOTE_CITY_TOKENS) and not any(tok and tok in text for tok in allow_tokens):
        score -= 120
    return score


def _rank_geo_offers(offers: list[MarketOffer], *, allow_tokens: list[str], prefer_tokens: list[str], limit: int) -> list[MarketOffer]:
    if not offers:
        return []
    scored: list[tuple[int, int, MarketOffer]] = []
    for idx, offer in enumerate(offers):
        score = _offer_geo_score(offer, allow_tokens=allow_tokens, prefer_tokens=prefer_tokens)
        if score < 0:
            continue
        scored.append((score, idx, offer))
    if not scored:
        if allow_tokens or prefer_tokens:
            return []
        return offers[:limit]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [offer for _, _, offer in scored[:limit]]


def _all_text(offers: list[MarketOffer]) -> str:
    chunks: list[str] = []
    for offer in offers:
        chunks.append(offer.title or "")
        chunks.append(offer.snippet or "")
    return "\n".join(chunks)


def _extract_characteristics(offers: list[MarketOffer]) -> list[str]:
    text = _all_text(offers)
    found: list[str] = []
    patterns = [
        r"\b(?:ГОСТ|ТУ)\s*[A-ZА-Яа-я0-9.\-–—/ ]{2,30}",
        r"\b(?:М|M)\s?\d{2,4}\b",
        r"\bB\s?\d{1,2}(?:[,.]\d)?\b",
        r"\bC\s?\d{1,2}/\d{1,2}\b",
        r"\b\d+(?:[,.]\d+)?\s?(?:мм|см|м|м²|м2|м³|м3|кг|т|л|шт|кВт|Вт|В|А|час(?:а|ов)?|дн(?:я|ей)?|сут(?:ок)?)\b",
        r"\b(?:новый|б/у|бу|с гарантией|с доставкой|самовывоз|в наличии)\b",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            found.append(m.group(0))
    return _uniq_keep_order(found, limit=10)


def _extract_purpose(offers: list[MarketOffer]) -> list[str]:
    out: list[str] = []
    for sent in _sentences(_all_text(offers)):
        low = sent.casefold()
        if "назнач" in low or re.search(r"\bдля\s+[А-Яа-яA-Za-z0-9]", sent, flags=re.IGNORECASE):
            out.append(sent)
    return _uniq_keep_order(out, limit=4)


def _extract_advantages(offers: list[MarketOffer]) -> list[str]:
    keys = (
        "преимуществ",
        "плюс",
        "удобн",
        "надёж",
        "надеж",
        "прочн",
        "долговеч",
        "быстро",
        "эконом",
        "выгод",
        "гарант",
        "качест",
        "в наличии",
        "с доставкой",
    )
    out = [s for s in _sentences(_all_text(offers)) if any(k in s.casefold() for k in keys)]
    return _uniq_keep_order(out, limit=4)


def _extract_disadvantages(offers: list[MarketOffer]) -> list[str]:
    keys = (
        "недостат",
        "минус",
        "огранич",
        "не подходит",
        "нельзя",
        "риск",
        "требуется",
        "только самовывоз",
        "без доставки",
        "б/у",
        "бу",
    )
    out = []
    for s in _sentences(_all_text(offers)):
        low = s.casefold()
        if "не указан" in low or "не описан" in low:
            continue
        if any(k in low for k in keys):
            out.append(s)
    return _uniq_keep_order(out, limit=4)


def _extract_terms(offers: list[MarketOffer]) -> list[str]:
    text = _all_text(offers)
    found: list[str] = []
    patterns = [
        r"\bсрок[а-яё\s:—-]{0,40}\d+\s?(?:час(?:а|ов)?|дн(?:я|ей)?|сут(?:ок)?|недел[ияь])",
        r"\bдоставк[а-яё\s:—-]{0,40}\d+\s?(?:час(?:а|ов)?|дн(?:я|ей)?|сут(?:ок)?|недел[ияь])",
        r"\b(?:сегодня|завтра|в наличии|под заказ|самовывоз|доставка)\b",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            found.append(m.group(0))
    return _uniq_keep_order(found, limit=6)


def research_item(
    query: str,
    *,
    unit: str = "",
    basis_code: str = "",
    section: str = "",
    region: str = "",
    sources: list[str] | None = None,
    max_results: int = 5,
) -> ItemResearchResult:
    q = re.sub(r"\s+", " ", str(query or "").strip())
    if len(q) < 2:
        raise ValueError("Укажите услугу, продукт или материал.")
    src = sources or DEFAULT_SOURCES.copy()
    src = [s for s in src if s in VALID_SOURCES] or DEFAULT_SOURCES.copy()
    max_results = max(1, min(10, int(max_results or 5)))
    geo = _geo_scope(region)
    search_regions = [str(x or "").strip() for x in (geo.get("search_regions") or [""]) if str(x or "").strip() or not region]
    allow_tokens = [str(x) for x in (geo.get("allow_tokens") or [])]
    prefer_tokens = [str(x) for x in (geo.get("prefer_tokens") or [])]
    region_label = str(geo.get("label") or region or "")

    use_browser = (os.environ.get("MARKET_AVITO_BROWSER", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
    browser_headless = (os.environ.get("MARKET_AVITO_HEADLESS", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
    active_sources = list(src)
    all_offers: list[MarketOffer] = []
    error_parts: list[str] = []
    seen_keys: set[str] = set()
    last_plan = None

    def merge_offers(items: list[MarketOffer]) -> None:
        for offer in items or []:
            key = (offer.url or "").strip() or f"{offer.source}|{offer.title}|{offer.price}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_offers.append(offer)

    # The same limited browser session is also the JS fallback for ordinary
    # supplier sites.  It must therefore stay available in a web-only run.
    with AvitoBrowserFetcher(enabled=use_browser, headless=browser_headless) as browser:
        for idx, search_region in enumerate(search_regions or [""]):
            offers, plan, errors = research_position_market(
                q,
                unit=unit,
                basis_code=basis_code,
                section=section,
                region=search_region,
                sources=active_sources,
                max_results=max_results,
                browser_fetcher=browser,
            )
            last_plan = plan
            merge_offers(offers)
            if errors:
                error_parts.append(errors)

            err_low = str(errors or "").casefold()
            avito_blocked = "ограничил доступ" in err_low or "ip/vpn" in err_low or "captcha" in err_low or "429" in err_low
            if avito_blocked and "avito" in active_sources:
                active_sources = [s for s in active_sources if s != "avito"]

            ranked_now = _rank_geo_offers(all_offers, allow_tokens=allow_tokens, prefer_tokens=prefer_tokens, limit=max_results)
            if len(ranked_now) >= max_results:
                break
            if idx == 0 and not ranked_now and len(search_regions) > 1:
                error_parts.append(f"В самом городе не найдено — расширяю поиск рядом: {search_regions[1]}")

    final_sources = active_sources or ["web"]
    final_errors = "; ".join(_uniq_keep_order((e for e in error_parts if str(e or "").strip()), limit=6))
    final_offers = _rank_geo_offers(all_offers, allow_tokens=allow_tokens, prefer_tokens=prefer_tokens, limit=max_results)
    return ItemResearchResult(
        query=q,
        region=region_label,
        sources=final_sources,
        offers=final_offers[:max_results],
        errors=final_errors,
        unit=unit,
        position_type=last_plan.position.slug if last_plan else "",
        position_label=last_plan.position.label if last_plan else "",
        strategy=last_plan.strategy_label if last_plan else "",
        warning=last_plan.warning if last_plan else "",
    )


def _money(v: float) -> str:
    return f"{float(v):,.0f}".replace(",", " ") + " ₽"


def _bullet_html(items: list[str], empty: str) -> str:
    if not items:
        return "• " + html.escape(empty)
    return "\n".join("• " + html.escape(x) for x in items)


def format_research_html(result: ItemResearchResult) -> str:
    offers = result.offers
    prices = [float(o.price) for o in offers if o.verification == "verified" and o.price and o.price > 0]
    chars = _extract_characteristics(offers)
    purpose = _extract_purpose(offers)
    advantages = _extract_advantages(offers)
    disadvantages = _extract_disadvantages(offers)
    terms = _extract_terms(offers)

    if prices:
        price_line = (
            f"найдено цен: <b>{len(prices)}</b>; "
            f"диапазон: <b>{_money(min(prices))} — {_money(max(prices))}</b>; "
            f"медиана: <b>{_money(statistics.median(prices))}</b>"
        )
    else:
        price_line = "в найденных источниках цена не указана или не распознана."

    source_lines: list[str] = []
    for i, offer in enumerate(offers[:5], start=1):
        title = html.escape((offer.title or "Источник").strip()[:140])
        src = html.escape(offer.source or "web")
        price = f" · <b>{_money(offer.price)}</b>" if offer.price and offer.price > 0 else ""
        url = html.escape(offer.url or "", quote=True)
        if url:
            source_lines.append(f"{i}. [{src}] <a href=\"{url}\">{title}</a>{price}")
        else:
            source_lines.append(f"{i}. [{src}] {title}{price}")
    if not source_lines:
        source_lines.append("Источники не найдены.")

    lines = [
        f"🔎 <b>Сводка по запросу:</b> {html.escape(result.query)}",
        f"<b>Источники:</b> {html.escape(', '.join(result.sources))}"
        + (f" · регион: {html.escape(result.region)}" if result.region else ""),
        "",
        "<b>Что найдено</b>",
        f"• Найдено источников: <b>{len(offers)}</b>",
        f"• Цена/ориентир: {price_line}",
        "",
        "<b>Основные характеристики</b>",
        _bullet_html(chars, "в найденных источниках явно не указаны."),
        "",
        "<b>Назначение</b>",
        _bullet_html(purpose, "в найденных источниках явно не описано."),
        "",
        "<b>Преимущества</b>",
        _bullet_html(advantages, "в найденных источниках прямо не указаны."),
        "",
        "<b>Недостатки / ограничения</b>",
        _bullet_html(disadvantages, "в найденных источниках прямо не указаны."),
        "",
        "<b>Цена / сроки</b>",
        "• " + price_line,
        _bullet_html(terms, "сроки/условия в найденных источниках явно не указаны."),
        "",
        "<b>Источники</b>",
        "\n".join(source_lines),
    ]
    if result.errors:
        lines.extend(["", "<b>Ограничения поиска</b>", "• " + html.escape(result.errors[:900])])
    lines.extend(["", "<i>Если данных мало, я не заполняю пробелы догадками — лучше уточнить запрос или регион.</i>"])
    return "\n".join(lines)


def html_to_plain(text: str) -> str:
    s = re.sub(r"<a\s+[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"\2 (\1)", text, flags=re.IGNORECASE)
    s = re.sub(r"</?(?:b|i|code)>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def send_research_to_telegram(result: ItemResearchResult) -> None:
    from autobot.telegram_notify import send_message, telegram_config

    cfg = telegram_config()
    if not cfg:
        raise RuntimeError("Нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    token, chat_id = cfg
    send_message(token, chat_id, format_research_html(result), parse_mode="HTML", disable_web_page_preview=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Краткая сводка по услуге, товару или материалу")
    ap.add_argument("query_parts", nargs="*", help="Что ищем, например: бетон м300")
    ap.add_argument("--query", help="Что ищем")
    ap.add_argument("--region", default=os.environ.get("MARKET_SUMMARY_REGION", ""), help="Регион поиска")
    ap.add_argument(
        "--sources",
        default=os.environ.get("MARKET_SUMMARY_SOURCES") or os.environ.get("MARKET_SOURCES") or "web,avito",
        help="Источники через запятую: web,avito",
    )
    ap.add_argument("--max-results", type=int, default=int(os.environ.get("MARKET_SUMMARY_MAX_RESULTS", "5") or "5"))
    ap.add_argument("--send-telegram", action="store_true", help="Отправить результат в TELEGRAM_CHAT_ID")
    args = ap.parse_args()

    query = (args.query or " ".join(args.query_parts)).strip()
    result = research_item(
        query,
        region=args.region,
        sources=parse_sources(args.sources),
        max_results=args.max_results,
    )
    text = format_research_html(result)
    print(html_to_plain(text))
    if args.send_telegram:
        send_research_to_telegram(result)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Остановлено пользователем", file=sys.stderr)
        raise SystemExit(130)
