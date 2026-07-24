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
from autobot.real_market_scraper import AvitoBrowserFetcher, MarketOffer, search_market

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass


DEFAULT_SOURCES = ["avito", "web"]
VALID_SOURCES = {"avito", "web"}


@dataclass
class ItemResearchResult:
    query: str
    region: str
    sources: list[str]
    offers: list[MarketOffer]
    errors: str = ""


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

    use_browser = (os.environ.get("MARKET_AVITO_BROWSER", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
    browser_headless = (os.environ.get("MARKET_AVITO_HEADLESS", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
    with AvitoBrowserFetcher(enabled=use_browser and "avito" in src, headless=browser_headless) as browser:
        offers, errors = search_market(
            q,
            region=region,
            sources=src,
            max_results=max_results,
            browser_fetcher=browser,
        )
    return ItemResearchResult(query=q, region=region, sources=src, offers=offers, errors=errors)


def _money(v: float) -> str:
    return f"{float(v):,.0f}".replace(",", " ") + " ₽"


def _bullet_html(items: list[str], empty: str) -> str:
    if not items:
        return "• " + html.escape(empty)
    return "\n".join("• " + html.escape(x) for x in items)


def format_research_html(result: ItemResearchResult) -> str:
    offers = result.offers
    prices = [float(o.price) for o in offers if o.price and o.price > 0]
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
        default=os.environ.get("MARKET_SUMMARY_SOURCES") or os.environ.get("MARKET_SOURCES") or "avito,web",
        help="Источники через запятую: avito,web",
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
