"""
Реальный поиск рыночных источников по строкам сметы.

В отличие от старого real_market_scraper.py этот модуль не просит модель
придумать/свести цены. Он сохраняет только то, что смог вытащить из страниц:
цена, название объявления/страницы и ссылка.

Основной поиск идёт по прямым страницам поставщиков и подрядчиков. Авито
используется только как ограниченный резерв. Результат пишется построчно в:
  data/reports/РЫНОК_ИСТОЧНИКИ_ОТЧЕТ_ПО_СМЕТАМ_<tender_id>.xlsx

Формат совместим со старой сводкой через колонку «Цена-сайт-телефон (json)».
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import random
import re
import statistics
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from urllib.parse import parse_qs, parse_qsl, quote_plus, unquote, urlencode, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

from autobot.market_analytics import COL_DUP, COL_NAME, COL_QTY, COL_SUM, COL_UNIT_PRICE
from autobot.market_source_adapters import inspect_source_page
from autobot.market_price_index import (
    lookup_verified_offers,
    record_parser_run,
    record_verified_offers,
    source_quality,
    weighted_median,
)
from autobot.market_strategy import (
    MarketSearchPlan,
    assess_market_median_anomaly,
    assess_price_plausibility,
    build_search_plan,
    check_offer,
    is_direct_source_url,
    market_query_name,
    normalize_unit,
)
from autobot.merge_estimate_market import _norm_key
from autobot.paths import REPO_ROOT
from autobot.report_prompt import REPORTS_DIR, load_tender_metadata

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

MARKET_PREFIX = "РЫНОК_ИСТОЧНИКИ_"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)

# Селекторы собраны в одном месте: при изменении вёрстки Авито достаточно
# поправить этот блок, не трогая логику скролла и проверки ограничений.
AVITO_CARD_SELECTOR = "[data-marker='item']"
AVITO_TITLE_SELECTORS = (
    "a[data-marker='item-title']",
    "a[itemprop='url']",
    "a[href*='_']",
)
AVITO_PRICE_SELECTORS = (
    "[data-marker='item-price']",
    "[itemprop='price']",
)
AVITO_DATE_SELECTORS = (
    "[data-marker='item-date']",
    "[data-marker='item-date/relative']",
    "p[data-marker*='date']",
)
AVITO_LOCATION_SELECTORS = (
    "[data-marker='item-address']",
    "[data-marker='item-location']",
    "p[data-marker*='address']",
)
AVITO_MORE_SELECTORS = (
    "button:has-text('Показать ещё')",
    "button:has-text('Показать еще')",
    "[data-marker*='show-more']",
)
_AVITO_BLOCKED_UNTIL = 0.0
_AVITO_BLOCK_COOLDOWN_SEC = max(60 * 60, int(os.environ.get("MARKET_AVITO_BLOCK_COOLDOWN_SEC", str(12 * 60 * 60)) or 12 * 60 * 60))
_AVITO_MIN_INTERVAL_SEC = max(30.0, float(os.environ.get("MARKET_AVITO_MIN_INTERVAL_SEC", "90") or 90))
_AVITO_MAX_REQUESTS_PER_RUN = max(1, int(os.environ.get("MARKET_AVITO_MAX_REQUESTS_PER_RUN", "2") or 2))
_AVITO_MAX_REQUESTS_PER_DAY = max(1, int(os.environ.get("MARKET_AVITO_MAX_REQUESTS_PER_DAY", "3") or 3))
_AVITO_REQUEST_COUNT = 0
_AVITO_LAST_REQUEST_AT = 0.0
_AVITO_STATE_LOCK = threading.Lock()
_DDG_BLOCKED_UNTIL = 0.0
_DDGS_BLOCKED_UNTIL = 0.0
_MARKET_CACHE_DIR = REPO_ROOT / "data" / "market_cache"
_SOURCE_PAGE_CACHE_DIR = _MARKET_CACHE_DIR / "source_pages"
_SOURCE_PAGE_CACHE_VERSION = "2"
_AVITO_GUARD_PATH = _MARKET_CACHE_DIR / "avito_guard.json"
_AVITO_LOG_PATH = REPO_ROOT / "data" / "logs" / "avito_playwright.jsonl"
_MARKET_SEARCH_LOG_PATH = REPO_ROOT / "data" / "logs" / "market_search_candidates.jsonl"
_SEARCH_CACHE_VERSIONS = {"web": "9", "avito": "2", "avito_index": "1"}
_DOMAIN_LAST_REQUEST_AT: dict[str, float] = {}
_DOMAIN_RATE_LOCK = threading.Lock()

_SEARCH_ENGINE_HOSTS = (
    "bing.com", "duckduckgo.com", "google.com", "yandex.ru", "ya.ru",
    "search.yahoo.com", "r.search.yahoo.com", "startpage.com", "brave.com",
)
_SEARCH_BLOCKED_HOSTS = (
    "avito.ru", "youla.ru", "irr.ru", "farpost.ru", "barahla.net",
    "ozon.ru", "wildberries.ru", "wb.ru", "aliexpress.ru", "megamarket.ru",
    "pulscen.ru", "tiu.ru", "all.biz", "promportal.su",
    "promindex.ru",
    "smetnoedelo.ru", "meganorm.ru", "docs.cntd.ru", "base.garant.ru",
)
_SEARCH_NOISE_PATHS = (
    "/forum", "/news", "/blog", "/article", "/articles", "/stati/", "/wiki", "/journal",
)
_SEARCH_NOISE_TEXT = (
    "как выбрать", "своими руками", "обзор рынка", "новости", "форум",
    "инструкция по выбору", "что лучше", "что это такое", "википедия", "реферат",
)
_PREFERRED_GENERAL_HOSTS = (
    "lemanapro.ru",
    "petrovich.ru",
    "vseinstrumenti.ru",
    "saturn.net",
    "baucenter.ru",
    "maxidom.ru",
)
_PREFERRED_SPECIALTY_HOSTS = {
    "bulk": (
        "postavka76.ru", "samosval76.ru", "pesko.ru", "smit76.ru",
        "renta76.ru", "chistov.biz", "tdagro.ru",
    ),
    "geotextile": ("tentisib.ru", "tdagro.ru", "tstn.ru"),
    "roofing": ("tstn.ru", "lemanapro.ru", "petrovich.ru"),
    "tools": ("vseinstrumenti.ru", "lemanapro.ru", "petrovich.ru", "baucenter.ru"),
}


@dataclass
class MarketOffer:
    source: str
    title: str
    price: float
    url: str
    snippet: str = ""
    verification: str = "candidate"
    confidence: float = 0.0
    verification_reason: str = "Ещё не проверено"
    matched_unit: str = ""
    observed_at: str = ""
    position_type: str = ""
    page_checked: bool = False
    page_error: str = ""
    adapter: str = ""
    price_scope: str = ""
    evidence: str = ""
    published_at: str = ""
    location: str = ""
    estimate_ratio: float | None = None
    plausibility: str = "unknown"
    identity_verified: bool = False
    source_weight: float = 0.0
    index_hit: bool = False
    index_match_score: float = 0.0
    audit_record_path: str = ""
    snapshot_path: str = ""
    discovery_engine: str = ""
    discovery_score: float = 0.0
    discovery_reason: str = ""
    rejection_code: str = ""
    rejection_stage: str = ""
    extractor: str = ""
    price_facts_found: int = 0
    consensus_status: str = "unknown"
    consensus_median: float | None = None
    consensus_ratio: float | None = None
    agent_price: float | None = None
    agent_unit: str = ""
    agent_evidence: str = ""


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_avito_log(kind: str, **payload: object) -> None:
    event = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        **payload,
    }
    try:
        _AVITO_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AVITO_LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        # Журнал не должен ронять сам поиск, например при временно read-only volume.
        return


def _append_market_search_log(kind: str, **payload: object) -> None:
    """Append machine-readable discovery/verification diagnostics.

    Logging must never make a market run fail, including on a read-only data
    volume.  URL-level reasons are intentionally retained even when a rejected
    candidate does not enter the Excel report.
    """

    event = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": str(kind or "candidate")[:40],
        **payload,
    }
    try:
        _MARKET_SEARCH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _MARKET_SEARCH_LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        return


def _clear_avito_guard_after_manual_check() -> None:
    """Снять только нашу локальную паузу после ручного прохождения проверки."""
    global _AVITO_BLOCKED_UNTIL
    _AVITO_BLOCKED_UNTIL = 0.0
    state = _read_json(_AVITO_GUARD_PATH)
    state.update(
        {
            "blocked_until": 0,
            "reason": "Сброшено после ручной проверки пользователем",
            "consecutive_blocks": 0,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    _write_json(_AVITO_GUARD_PATH, state)
    _append_avito_log("guard_reset", reason=state["reason"])


def _avito_guard_message() -> str:
    status = avito_guard_status()
    blocked_until = float(status["blocked_until"] or 0)
    if blocked_until <= time.time():
        return ""
    until_text = datetime.fromtimestamp(blocked_until).strftime("%d.%m.%Y %H:%M")
    return f"Авито на паузе до {until_text} после ограничения; поиск продолжается по другим сайтам"


def avito_guard_status() -> dict[str, object]:
    """Public read-only status for CLI and the tender card."""

    state = _read_json(_AVITO_GUARD_PATH)
    blocked_until = max(float(state.get("blocked_until") or 0), float(_AVITO_BLOCKED_UNTIL or 0))
    today = datetime.now().date().isoformat()
    try:
        daily_limit = max(1, int(os.environ.get("MARKET_AVITO_MAX_REQUESTS_PER_DAY", str(_AVITO_MAX_REQUESTS_PER_DAY)) or _AVITO_MAX_REQUESTS_PER_DAY))
    except ValueError:
        daily_limit = _AVITO_MAX_REQUESTS_PER_DAY
    daily_used = int(state.get("daily_requests") or 0) if str(state.get("daily_date") or "") == today else 0
    daily_remaining = max(0, daily_limit - daily_used)
    if daily_remaining <= 0:
        tomorrow = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 24 * 60 * 60
        blocked_until = max(blocked_until, tomorrow)
    try:
        min_interval = max(30.0, float(os.environ.get("MARKET_AVITO_MIN_INTERVAL_SEC", str(_AVITO_MIN_INTERVAL_SEC)) or _AVITO_MIN_INTERVAL_SEC))
    except ValueError:
        min_interval = _AVITO_MIN_INTERVAL_SEC
    last_request_at = max(float(state.get("last_request_at") or 0), float(_AVITO_LAST_REQUEST_AT or 0))
    next_request_in = max(0, int(round(min_interval - (time.time() - last_request_at))))
    remaining = max(0, int(round(blocked_until - time.time())))
    reason = str(state.get("reason") or "")
    if daily_remaining <= 0 and remaining > 0:
        reason = f"Достигнут бережный дневной лимит Авито: {daily_limit}"
    return {
        "blocked": remaining > 0,
        "blocked_until": blocked_until,
        "remaining_seconds": remaining,
        "reason": reason,
        "last_request_at": last_request_at,
        "next_request_in_seconds": next_request_in,
        "daily_date": today,
        "daily_used": daily_used,
        "daily_limit": daily_limit,
        "daily_remaining": daily_remaining,
        "profile_ready": Path(os.environ.get("MARKET_AVITO_USER_DATA_DIR") or (REPO_ROOT / "data" / "avito_profile")).is_dir(),
    }


def _block_avito(reason: str) -> None:
    global _AVITO_BLOCKED_UNTIL
    state = _read_json(_AVITO_GUARD_PATH)
    consecutive_blocks = max(1, int(state.get("consecutive_blocks") or 0) + 1)
    # Повторное ограничение означает, что IP ещё не восстановился. Удваиваем
    # паузу, но не более трёх суток, вместо повторных проверок сайта.
    cooldown = min(3 * 24 * 60 * 60, _AVITO_BLOCK_COOLDOWN_SEC * (2 ** min(3, consecutive_blocks - 1)))
    _AVITO_BLOCKED_UNTIL = time.time() + cooldown
    state.update(
        {
            "blocked_until": _AVITO_BLOCKED_UNTIL,
            "reason": str(reason or "ограничение доступа")[:300],
            "consecutive_blocks": consecutive_blocks,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    _write_json(_AVITO_GUARD_PATH, state)
    _append_avito_log("guard_blocked", reason=reason, cooldown_seconds=cooldown, consecutive_blocks=consecutive_blocks)


def _record_avito_page_success(*, cards: int, query: str) -> None:
    state = _read_json(_AVITO_GUARD_PATH)
    state.update(
        {
            "last_success_at": time.time(),
            "last_success_cards": max(0, int(cards or 0)),
            "last_success_query": str(query or "")[:300],
            "consecutive_blocks": 0,
            "reason": "",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    _write_json(_AVITO_GUARD_PATH, state)
    _append_avito_log("page_success", query=query, cards=max(0, int(cards or 0)))


def _before_avito_request() -> str:
    global _AVITO_LAST_REQUEST_AT, _AVITO_REQUEST_COUNT
    with _AVITO_STATE_LOCK:
        blocked = _avito_guard_message()
        if blocked:
            return blocked
        try:
            request_limit = max(1, int(os.environ.get("MARKET_AVITO_MAX_REQUESTS_PER_RUN", str(_AVITO_MAX_REQUESTS_PER_RUN)) or _AVITO_MAX_REQUESTS_PER_RUN))
        except ValueError:
            request_limit = _AVITO_MAX_REQUESTS_PER_RUN
        if _AVITO_REQUEST_COUNT >= request_limit:
            return f"Авито пропущен: достигнут безопасный лимит {request_limit} запросов за один запуск"
        state = _read_json(_AVITO_GUARD_PATH)
        today = datetime.now().date().isoformat()
        try:
            daily_limit = max(1, int(os.environ.get("MARKET_AVITO_MAX_REQUESTS_PER_DAY", str(_AVITO_MAX_REQUESTS_PER_DAY)) or _AVITO_MAX_REQUESTS_PER_DAY))
        except ValueError:
            daily_limit = _AVITO_MAX_REQUESTS_PER_DAY
        daily_used = int(state.get("daily_requests") or 0) if str(state.get("daily_date") or "") == today else 0
        if daily_used >= daily_limit:
            return f"Авито пропущен: достигнут бережный дневной лимит {daily_limit} запросов"
        last_request_at = max(float(state.get("last_request_at") or 0), _AVITO_LAST_REQUEST_AT)
        try:
            min_interval = max(30.0, float(os.environ.get("MARKET_AVITO_MIN_INTERVAL_SEC", str(_AVITO_MIN_INTERVAL_SEC)) or _AVITO_MIN_INTERVAL_SEC))
        except ValueError:
            min_interval = _AVITO_MIN_INTERVAL_SEC
        wait_for = min_interval - (time.time() - last_request_at)
        if wait_for > 0:
            time.sleep(wait_for + random.uniform(1.0, 4.0))
        now = time.time()
        _AVITO_LAST_REQUEST_AT = now
        _AVITO_REQUEST_COUNT += 1
        daily_used += 1
        state.update(
            {
                "last_request_at": now,
                "request_count_last_run": _AVITO_REQUEST_COUNT,
                "daily_date": today,
                "daily_requests": daily_used,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        _write_json(_AVITO_GUARD_PATH, state)
        _append_avito_log(
            "request_reserved",
            run_request=_AVITO_REQUEST_COUNT,
            run_limit=request_limit,
            daily_request=daily_used,
            daily_limit=daily_limit,
        )
        return ""


def _reset_avito_run_budget() -> None:
    global _AVITO_REQUEST_COUNT
    _AVITO_REQUEST_COUNT = 0


def _search_cache_path(source: str, query: str, region: str) -> Path:
    source_key = str(source or "").casefold()
    version = _SEARCH_CACHE_VERSIONS.get(source_key, "1")
    key = hashlib.sha256(f"{version}|{source_key}|{query}|{region}".casefold().encode("utf-8")).hexdigest()
    return _MARKET_CACHE_DIR / source / f"{key}.json"


def _load_search_cache(source: str, query: str, region: str) -> list[MarketOffer] | None:
    path = _search_cache_path(source, query, region)
    payload = _read_json(path)
    created_at = float(payload.get("created_at") or 0)
    if str(source or "").casefold() == "avito":
        ttl_sec = max(
            24 * 60 * 60,
            int(os.environ.get("MARKET_AVITO_CACHE_TTL_SEC", str(30 * 24 * 60 * 60)) or 30 * 24 * 60 * 60),
        )
    else:
        ttl_sec = max(3600, int(os.environ.get("MARKET_CACHE_TTL_SEC", str(7 * 24 * 60 * 60)) or 7 * 24 * 60 * 60))
    if not created_at or time.time() - created_at > ttl_sec:
        return None
    raw_offers = payload.get("offers")
    if not isinstance(raw_offers, list):
        return None
    fields = set(MarketOffer.__dataclass_fields__)
    offers: list[MarketOffer] = []
    for raw in raw_offers:
        if not isinstance(raw, dict):
            continue
        try:
            offers.append(MarketOffer(**{key: value for key, value in raw.items() if key in fields}))
        except (TypeError, ValueError):
            continue
    return offers


def _save_search_cache(source: str, query: str, region: str, offers: list[MarketOffer]) -> None:
    if not offers:
        return
    _write_json(
        _search_cache_path(source, query, region),
        {
            "created_at": time.time(),
            "source": source,
            "query": query,
            "region": region,
            "offers": [asdict(offer) for offer in offers],
        },
    )


def _canonical_offer_url(url: str) -> str:
    raw = str(url or "").strip()
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw
    tracking_keys = {
        "srsltid",
        "yclid",
        "ysclid",
        "yadclid",
        "gclid",
        "fbclid",
        "from",
    }
    clean_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in tracking_keys and not key.casefold().startswith("utm_")
    ]
    return parsed._replace(query=urlencode(clean_query, doseq=True), fragment="").geturl()


def _source_page_cache_path(url: str) -> Path:
    key = hashlib.sha256(f"{_SOURCE_PAGE_CACHE_VERSION}|{_canonical_offer_url(url)}".encode("utf-8")).hexdigest()
    return _SOURCE_PAGE_CACHE_DIR / f"{key}.json"


def _load_source_page_cache(url: str) -> tuple[str, str, str] | None:
    payload = _read_json(_source_page_cache_path(url))
    created_at = float(payload.get("created_at") or 0)
    if not created_at:
        return None
    ok = bool(payload.get("ok"))
    env_name = "MARKET_SOURCE_CACHE_TTL_SEC" if ok else "MARKET_SOURCE_ERROR_CACHE_TTL_SEC"
    default_ttl = 24 * 60 * 60 if ok else 30 * 60
    try:
        ttl_sec = max(60, int(os.environ.get(env_name, str(default_ttl)) or default_ttl))
    except ValueError:
        ttl_sec = default_ttl
    if time.time() - created_at > ttl_sec:
        return None
    return (
        str(payload.get("html") or ""),
        str(payload.get("error") or ""),
        str(payload.get("method") or "cache"),
    )


def _save_source_page_cache(
    url: str,
    *,
    page_html: str = "",
    error: str = "",
    method: str = "http",
) -> None:
    try:
        html_limit = max(500_000, min(5_000_000, int(os.environ.get("MARKET_SOURCE_HTML_MAX_BYTES", "2000000") or 2_000_000)))
    except ValueError:
        html_limit = 2_000_000
    html_text = str(page_html or "")[:html_limit]
    _write_json(
        _source_page_cache_path(url),
        {
            "created_at": time.time(),
            "url": str(url or ""),
            "ok": bool(html_text),
            "html": html_text,
            "error": str(error or "")[:500],
            "method": str(method or "")[:40],
        },
    )


def _generic_block_reason(page_html: str, final_url: str = "") -> str:
    folded = f"{final_url} {(page_html or '')[:120_000]}".casefold()
    markers = (
        "cf-chl-",
        "challenge-platform",
        "just a moment...",
        "доступ ограничен",
        "подозрительный трафик",
        "unusual traffic",
        "showcaptcha",
        "smartcaptcha",
    )
    return "страница защиты или капчи" if any(marker in folded for marker in markers) else ""


class AvitoBrowserFetcher:
    """Ленивая Playwright-сессия: один Chromium-профиль на весь прогон."""

    def __init__(self, *, enabled: bool = True, headless: bool = True, timeout_ms: int = 45_000) -> None:
        self.enabled = enabled
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.last_error = ""
        self.proxy = (os.environ.get("MARKET_PROXY") or "").strip()
        self.user_data_dir = (
            os.environ.get("MARKET_AVITO_USER_DATA_DIR")
            or str(REPO_ROOT / "data" / "avito_profile")
        ).strip()
        try:
            self.manual_wait_sec = max(0, int((os.environ.get("MARKET_AVITO_MANUAL_WAIT_SEC") or "0").strip() or "0"))
        except ValueError:
            self.manual_wait_sec = 0
        try:
            self.scroll_delay_min_sec = max(2.0, float(os.environ.get("MARKET_AVITO_SCROLL_DELAY_MIN_SEC", "5") or 5))
            self.scroll_delay_max_sec = max(
                self.scroll_delay_min_sec,
                float(os.environ.get("MARKET_AVITO_SCROLL_DELAY_MAX_SEC", "9") or 9),
            )
            self.max_scrolls = max(1, min(5, int(os.environ.get("MARKET_AVITO_MAX_SCROLLS", "2") or 2)))
            self.scroll_target_cards = max(
                10,
                min(60, int(os.environ.get("MARKET_AVITO_SCROLL_TARGET_CARDS", "25") or 25)),
            )
        except ValueError:
            self.scroll_delay_min_sec = 5.0
            self.scroll_delay_max_sec = 9.0
            self.max_scrolls = 2
            self.scroll_target_cards = 25
        self.last_scroll_counts: list[int] = []
        self.last_skipped_cards = 0
        self.last_loaded_url = ""
        self.source_browser_count = 0
        self.source_browser_last_at = 0.0
        self.source_domain_failures: dict[str, int] = {}
        try:
            self.source_browser_max = max(
                0,
                min(30, int(os.environ.get("MARKET_SOURCE_BROWSER_MAX_PER_RUN", "8") or 8)),
            )
            self.source_browser_interval_sec = max(
                1.0,
                float(os.environ.get("MARKET_SOURCE_BROWSER_INTERVAL_SEC", "2") or 2),
            )
        except ValueError:
            self.source_browser_max = 8
            self.source_browser_interval_sec = 2.0

    def __enter__(self) -> "AvitoBrowserFetcher":
        if self.enabled:
            _reset_avito_run_budget()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        for obj in (self._page, self._context, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    def _ensure_page(self):
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        launch_kwargs = {"headless": self.headless}
        if self.proxy:
            launch_kwargs["proxy"] = {"server": self.proxy}
        context_kwargs = {
            "locale": "ru-RU",
            "viewport": {"width": 1366, "height": 900},
        }
        # Один обычный desktop User-Agent на весь профиль. Не меняем его между
        # запросами: резкие изменения внутри одной cookie-сессии выглядят подозрительнее.
        context_kwargs["user_agent"] = (os.environ.get("MARKET_USER_AGENT") or DEFAULT_USER_AGENT).strip()
        if self.user_data_dir:
            Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
            self._context = self._playwright.chromium.launch_persistent_context(
                self.user_data_dir,
                **launch_kwargs,
                **context_kwargs,
            )
            pages = list(self._context.pages)
            self._page = pages[0] if pages else self._context.new_page()
        else:
            self._browser = self._playwright.chromium.launch(**launch_kwargs)
            self._context = self._browser.new_context(**context_kwargs)
            self._page = self._context.new_page()
        self._page.set_default_timeout(min(self.timeout_ms, 12_000))
        return self._page

    def fetch_source_page(self, url: str) -> str:
        """Limited browser fallback for a supplier page that plain HTTP could not inspect."""
        if not self.enabled or self.source_browser_max <= 0:
            self.last_error = "Playwright fallback для источников отключён"
            return ""
        host = urlparse(str(url or "")).netloc.casefold().split(":", 1)[0]
        if self.source_domain_failures.get(host, 0) >= 2:
            self.last_error = f"домен {host or '-'} временно пропущен после двух ошибок"
            return ""
        if self.source_browser_count >= self.source_browser_max:
            self.last_error = f"достигнут лимит Playwright fallback: {self.source_browser_max} страниц"
            return ""
        wait_for = self.source_browser_interval_sec - (time.time() - self.source_browser_last_at)
        if wait_for > 0:
            time.sleep(wait_for)
        self.source_browser_count += 1
        self.source_browser_last_at = time.time()
        try:
            page = self._ensure_page()
            page.goto(url, wait_until="domcontentloaded", timeout=min(self.timeout_ms, 30_000))
            try:
                page.wait_for_load_state("networkidle", timeout=7_000)
            except Exception:
                pass
            page.wait_for_timeout(800)
            content = page.content()
            block = _generic_block_reason(content, page.url)
            if block:
                self.last_error = block
                self.source_domain_failures[host] = self.source_domain_failures.get(host, 0) + 1
                return ""
            if len(content) < 800:
                self.last_error = "браузер вернул слишком короткую страницу"
                self.source_domain_failures[host] = self.source_domain_failures.get(host, 0) + 1
                return ""
            print(f"Источник · Playwright: {host or url} ({len(content):,} байт)", flush=True)
            return content
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:400]
            self.source_domain_failures[host] = self.source_domain_failures.get(host, 0) + 1
            return ""

    def _wait_between_actions(self, page) -> None:
        delay_ms = int(random.uniform(self.scroll_delay_min_sec, self.scroll_delay_max_sec) * 1000)
        page.wait_for_timeout(delay_ms)

    @staticmethod
    def _card_count(page) -> int:
        try:
            return int(page.locator(AVITO_CARD_SELECTOR).count())
        except Exception:
            return 0

    @staticmethod
    def _first_locator(root, selectors: tuple[str, ...]):
        for selector in selectors:
            try:
                candidate = root.locator(selector).first
                if candidate.count() > 0:
                    return candidate
            except Exception:
                continue
        return None

    @classmethod
    def _first_text(cls, root, selectors: tuple[str, ...]) -> str:
        node = cls._first_locator(root, selectors)
        if node is None:
            return ""
        try:
            return _clean_text(node.inner_text())
        except Exception:
            return ""

    def _click_show_more(self, page) -> bool:
        for selector in AVITO_MORE_SELECTORS:
            try:
                button = page.locator(selector).first
                if button.count() and button.is_visible():
                    button.click(timeout=4_000)
                    return True
            except Exception:
                continue
        return False

    def _scroll_search_results(self, page, url: str) -> None:
        self.last_scroll_counts = []
        stable_rounds = 0
        previous = self._card_count(page)
        self.last_scroll_counts.append(previous)
        _append_avito_log(
            "scroll",
            url=url,
            step=0,
            cards=previous,
            target=self.scroll_target_cards,
            action="initial",
        )
        for step in range(1, self.max_scrolls + 1):
            if previous >= self.scroll_target_cards:
                break
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception as exc:
                _append_avito_log("scroll_error", url=url, step=step, error=f"{type(exc).__name__}: {exc}")
                break
            self._wait_between_actions(page)
            clicked = self._click_show_more(page)
            if clicked:
                self._wait_between_actions(page)
            try:
                page.wait_for_load_state("networkidle", timeout=6_000)
            except Exception:
                # У Авито есть фоновые запросы, поэтому networkidle не всегда достижим.
                pass
            current = self._card_count(page)
            self.last_scroll_counts.append(current)
            _append_avito_log(
                "scroll",
                url=url,
                step=step,
                cards=current,
                previous_cards=previous,
                target=self.scroll_target_cards,
                action="show_more" if clicked else "scroll",
            )
            print(f"Авито · скролл {step}: карточек {current}", flush=True)
            if current <= previous:
                stable_rounds += 1
            else:
                stable_rounds = 0
            previous = current
            if stable_rounds >= 3:
                break

    def _read_card_offer(self, card, *, base_url: str) -> MarketOffer | None:
        anchor = self._first_locator(card, AVITO_TITLE_SELECTORS)
        if anchor is None:
            return None
        href = anchor.get_attribute("href") or ""
        url = _normalize_avito_url(href, base_url)
        if not url:
            return None
        title = _clean_text(anchor.get_attribute("title") or anchor.inner_text())
        if not title:
            title = _clean_text(card.inner_text())[:220]
        price_text = ""
        try:
            price_meta = card.locator("meta[itemprop='price']").first
            if price_meta.count():
                price_text = price_meta.get_attribute("content") or ""
        except Exception:
            pass
        if not price_text:
            price_node = self._first_locator(card, AVITO_PRICE_SELECTORS)
            if price_node is not None:
                price_text = price_node.get_attribute("content") or price_node.inner_text()
        price = _parse_price(price_text)
        if price is None:
            return None
        snippet = _clean_text(card.inner_text())[:1200]
        return MarketOffer(
            "Авито",
            title or "Объявление Авито",
            price,
            url,
            snippet,
            published_at=self._first_text(card, AVITO_DATE_SELECTORS),
            location=self._first_text(card, AVITO_LOCATION_SELECTORS),
        )

    def fetch(self, url: str) -> str:
        if not self.enabled:
            return ""
        self.last_error = ""
        self.last_loaded_url = url
        try:
            page = self._ensure_page()
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=min(self.timeout_ms, 15_000))
            except Exception:
                _append_avito_log("networkidle_timeout", url=url)
            try:
                page.locator(AVITO_CARD_SELECTOR).first.wait_for(state="attached", timeout=10_000)
            except Exception:
                page.wait_for_timeout(1800)
            content = page.content()
            block_reason = _avito_block_reason(content)
            if (not self.headless) and self.manual_wait_sec > 0 and block_reason:
                print(
                    "Авито просит проверку/ограничил доступ. Открыл видимый браузер: "
                    f"пройдите проверку вручную, жду до {self.manual_wait_sec} сек…",
                    flush=True,
                )
                deadline = time.time() + self.manual_wait_sec
                while time.time() < deadline:
                    page.wait_for_timeout(5000)
                    content = page.content()
                    if not _avito_block_reason(content):
                        print("Проверка Авито пройдена, cookies сохранены в профиль.", flush=True)
                        break
                block_reason = _avito_block_reason(content)
            if block_reason:
                self.last_error = block_reason
                _append_avito_log("blocked", url=url, reason=block_reason)
                return content
            self._scroll_search_results(page, url)
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            content = page.content()
            _append_avito_log(
                "loaded",
                url=url,
                cards=self._card_count(page),
                scroll_counts=self.last_scroll_counts,
            )
            return content
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            _append_avito_log("navigation_error", url=url, error=self.last_error)
            return ""

    def current_avito_offers(self, *, base_url: str, max_results: int) -> list[MarketOffer]:
        """Read rendered search cards; raw HTML parsing remains a compatibility fallback."""
        if self._page is None:
            return []
        offers: list[MarketOffer] = []
        self.last_skipped_cards = 0
        try:
            cards = self._page.locator(AVITO_CARD_SELECTOR)
            count = min(cards.count(), max(self.scroll_target_cards, max_results))
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        for index in range(count):
            offer = None
            last_error = ""
            for attempt in range(2):
                try:
                    offer = self._read_card_offer(cards.nth(index), base_url=base_url)
                    if offer is not None:
                        break
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    try:
                        self._page.wait_for_timeout(250)
                    except Exception:
                        pass
            if offer is None:
                self.last_skipped_cards += 1
                _append_avito_log("card_skip", url=base_url, card=index + 1, error=last_error or "нет цены/ссылки")
                continue
            offers.append(offer)
        _append_avito_log(
            "parsed",
            url=base_url,
            cards=count,
            parsed=len(offers),
            skipped=self.last_skipped_cards,
        )
        # Для Авито важен порядок поисковой выдачи. Не выбираем самые дешёвые
        # карточки из всей страницы: это часто «цена от», аренда или приманка.
        result: list[MarketOffer] = []
        seen_urls: set[str] = set()
        for offer in offers:
            if offer.url in seen_urls:
                continue
            seen_urls.add(offer.url)
            result.append(offer)
            if len(result) >= max_results:
                break
        return result


def _safe_name(name: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-я_.-]+", "_", str(name or "").strip())[:120]


def market_web_event_path(tender_id: str) -> Path:
    safe = _safe_name(tender_id)
    return REPO_ROOT / "data" / "logs" / f"market_web_events_{safe}.jsonl"


def append_market_web_event(
    tender_id: str,
    kind: str,
    seq: int,
    total: int,
    *,
    work_name: str = "",
    detail: str = "",
) -> None:
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": "market",
        "kind": kind,
        "seq": int(seq or 0),
        "total": int(total or 0),
        "tender_id": tender_id,
        "work_name": work_name,
        "detail": detail,
    }
    if kind == "begin":
        payload["text"] = f"Ищу реальные источники: {seq}/{total} · {work_name}"
    elif kind == "done":
        payload["text"] = f"✅ {seq}/{total} · источники сохранены" + (f": {detail}" if detail else "")
    elif kind in ("warn", "error"):
        payload["text"] = f"⚠️ {seq}/{total} · {detail or work_name}"
    else:
        payload["text"] = detail or work_name
    try:
        path = market_web_event_path(tender_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def estimate_path_for_tender(tender_id: str) -> Path:
    return REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.xlsx"


def output_path_for_estimate(est_path: Path) -> Path:
    return REPORTS_DIR / f"{MARKET_PREFIX}{est_path.stem}.xlsx"


def output_path_for_tender(tender_id: str) -> Path:
    return output_path_for_estimate(estimate_path_for_tender(tender_id))


def _clean_text(raw: str) -> str:
    s = re.sub(r"<script\b.*?</script>", " ", raw or "", flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<style\b.*?</style>", " ", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[\s\u00a0\u202f]+", " ", s)
    return s.strip()


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    # Берём рубли рядом с ₽/руб. Слишком мелкие и фантастически большие числа отбрасываем.
    patterns = [
        r"(\d[\d\s\u00a0\u202f]{1,14})(?:[,.]\d{1,2})?\s*(?:₽|руб\.?|р\.)",
        r"(?:₽|руб\.?|р\.)\s*(\d[\d\s\u00a0\u202f]{1,14})(?:[,.]\d{1,2})?",
    ]
    vals: list[float] = []
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            raw = re.sub(r"[\s\u00a0\u202f]+", "", m.group(1))
            try:
                v = float(raw.replace(",", "."))
            except ValueError:
                continue
            if 10 <= v <= 500_000_000:
                vals.append(v)
    return min(vals) if vals else None


def _compact_query(work_name: str) -> str:
    q = re.sub(r"\([^)]*\)", " ", str(work_name or ""))
    q = re.sub(r"\b(?:по|при|для|из|с|и|в|на|от)\b", " ", q, flags=re.IGNORECASE)
    q = re.sub(r"\s+", " ", q).strip()
    words = q.split()
    return " ".join(words[:10]) if words else str(work_name or "").strip()


def _session_get(url: str, timeout: int = 25) -> str:
    host = urlparse(url).netloc.casefold().split(":", 1)[0]
    try:
        min_interval = max(0.0, float(os.environ.get("MARKET_DOMAIN_MIN_INTERVAL_SEC", "0.8") or 0.8))
    except ValueError:
        min_interval = 0.8
    if host and min_interval > 0:
        with _DOMAIN_RATE_LOCK:
            previous = _DOMAIN_LAST_REQUEST_AT.get(host, 0.0)
            wait_for = min_interval - (time.monotonic() - previous)
            if wait_for > 0:
                time.sleep(wait_for)
            _DOMAIN_LAST_REQUEST_AT[host] = time.monotonic()
    proxy = (os.environ.get("MARKET_PROXY") or "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = requests.get(
        url,
        headers={
            "User-Agent": os.environ.get("MARKET_USER_AGENT", DEFAULT_USER_AGENT),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.5",
        },
        proxies=proxies,
        timeout=timeout,
    )
    r.raise_for_status()
    if not r.encoding or str(r.encoding).casefold() in {"iso-8859-1", "latin-1"}:
        apparent = str(r.apparent_encoding or "").strip()
        if apparent:
            r.encoding = apparent
    return r.text


def _decode_avito_escapes(text: str) -> str:
    s = str(text or "")
    if not s:
        return ""
    return (
        s.replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
        .replace("\\u0026", "&")
        .replace("\\u0026amp;", "&")
        .replace("\\/", "/")
    )


def _normalize_avito_url(raw_url: str, base_url: str) -> str:
    href = _decode_avito_escapes(html.unescape(raw_url or "")).strip()
    if not href or href.startswith("#"):
        return ""
    if "avito.ru" not in href and not href.startswith("/"):
        return ""
    url = urljoin(base_url, href.split("?")[0])
    parsed = urlparse(url)
    if "avito.ru" not in parsed.netloc:
        return ""
    if not re.search(r"_\d{6,}(?:$|/)", parsed.path):
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _extract_avito_title(fragment: str) -> str:
    patterns = [
        r'"(?:title|name|itemTitle)"\s*:\s*"([^"]+)"',
        r"\btitle=[\"']([^\"']+)[\"']",
        r"<h3[^>]*>(.*?)</h3>",
        r"<a\b[^>]*>(.*?)</a>",
    ]
    for pat in patterns:
        m = re.search(pat, fragment or "", flags=re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        title = _clean_text(_decode_avito_escapes(m.group(1)))
        if len(title) >= 3:
            return title[:220]
    text = _clean_text(fragment)
    if len(text) >= 3:
        return " ".join(text.split()[:12])[:220]
    return ""


def _extract_avito_price_from_json_node(node: object) -> float | None:
    if isinstance(node, (int, float)) and 10 <= float(node) <= 500_000_000:
        return float(node)
    if isinstance(node, str):
        parsed = _parse_price(_decode_avito_escapes(node))
        if parsed is not None:
            return parsed
        raw = re.sub(r"[^\d.,]", "", node)
        if raw:
            try:
                value = float(raw.replace(",", "."))
            except ValueError:
                value = 0
            if 10 <= value <= 500_000_000:
                return value
        return None
    if isinstance(node, dict):
        for key in ("price", "priceRub", "amount", "value"):
            if key in node:
                parsed = _extract_avito_price_from_json_node(node.get(key))
                if parsed is not None:
                    return parsed
    return None


def _extract_avito_json_offers(page_html: str, base_url: str, *, max_results: int) -> list[MarketOffer]:
    offers: list[MarketOffer] = []
    seen: set[str] = set()
    script_re = re.compile(
        r"<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>(?P<body>.*?)</script>",
        flags=re.IGNORECASE | re.DOTALL,
    )

    def visit(node: object) -> None:
        if len(offers) >= max_results * 4:
            return
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return

        raw_url = ""
        for key in ("url", "itemUrl", "urlPath", "uri", "href"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                raw_url = value
                break
        url = _normalize_avito_url(raw_url, base_url) if raw_url else ""
        if url and url not in seen:
            title = ""
            for key in ("title", "name", "itemTitle"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    title = _clean_text(_decode_avito_escapes(value))[:220]
                    break
            price = _extract_avito_price_from_json_node(node)
            if title and price is not None:
                seen.add(url)
                snippet = _clean_text(json.dumps(node, ensure_ascii=False))[:500]
                offers.append(MarketOffer(source="Авито", title=title, price=price, url=url, snippet=snippet))

        for value in node.values():
            if isinstance(value, (dict, list)):
                visit(value)

    for match in script_re.finditer(page_html or ""):
        body = (match.group("body") or "").strip()
        if not body or "avito" not in body.lower():
            continue
        try:
            visit(json.loads(_decode_avito_escapes(body)))
        except Exception:
            continue

    return offers


def _extract_avito_fragment_offers(page_html: str, base_url: str, *, max_results: int) -> list[MarketOffer]:
    offers: list[MarketOffer] = []
    seen: set[str] = set()
    decoded = _decode_avito_escapes(page_html)
    url_re = re.compile(
        r"(https?://(?:www\.)?avito\.ru/[^\"'<>\\\s]+?_\d{6,}(?:/)?(?:\?[^\"'<>\\\s]*)?|/[^\"'<>\\\s]+?_\d{6,}(?:/)?(?:\?[^\"'<>\\\s]*)?)",
        flags=re.IGNORECASE,
    )
    for match in url_re.finditer(decoded):
        url = _normalize_avito_url(match.group(1), base_url)
        if not url or url in seen:
            continue
        frag = decoded[max(0, match.start() - 1600) : min(len(decoded), match.end() + 2400)]
        title = _extract_avito_title(frag)
        if len(title) < 3:
            continue
        price = _parse_price(_clean_text(frag))
        if price is None:
            price = _extract_avito_price_from_json_node({"price": frag})
        if price is None:
            continue
        seen.add(url)
        offers.append(MarketOffer(source="Авито", title=title, price=price, url=url, snippet=_clean_text(frag)[:500]))
        if len(offers) >= max_results * 4:
            break
    return offers


def _parse_avito_html(page_html: str, base_url: str, *, max_results: int) -> list[MarketOffer]:
    offers: list[MarketOffer] = []
    seen: set[str] = set()

    # Авито часто меняет классы, поэтому не завязываемся на них жёстко:
    # ищем ссылки объявлений и цену в ближайшем фрагменте HTML.
    link_re = re.compile(
        r"<a\b(?=[^>]*\bhref=[\"'](?P<href>[^\"']+)[\"'])(?P<attrs>[^>]*)>(?P<body>.*?)</a>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for m in link_re.finditer(page_html or ""):
        url = _normalize_avito_url(m.group("href") or "", base_url)
        if not url:
            continue
        if url in seen:
            continue
        seen.add(url)

        title = _clean_text(m.group("body"))
        attr_title = re.search(r"\btitle=[\"']([^\"']+)[\"']", m.group("attrs") or "", flags=re.IGNORECASE)
        if attr_title and len(_clean_text(attr_title.group(1))) > len(title):
            title = _clean_text(attr_title.group(1))
        if len(title) < 3:
            continue

        frag = page_html[max(0, m.start() - 1800) : min(len(page_html), m.end() + 2600)]
        text = _clean_text(frag)
        price = _parse_price(text)
        if price is None:
            continue
        offers.append(MarketOffer(source="Авито", title=title[:220], price=price, url=url, snippet=text[:500]))
        if len(offers) >= max_results * 4:
            break

    for extra in (
        _extract_avito_json_offers(page_html, base_url, max_results=max_results),
        _extract_avito_fragment_offers(page_html, base_url, max_results=max_results),
    ):
        for offer in extra:
            if offer.url in seen:
                continue
            seen.add(offer.url)
            offers.append(offer)

    return _dedupe_and_sort(offers, max_results=max_results)


def _avito_block_reason(page_html: str) -> str:
    text = _clean_text((page_html or "")[:40_000]).lower()
    if "доступ ограничен" in text and ("проблема с ip" in text or "ip" in text):
        return "Авито ограничил доступ по IP/VPN"
    if "подтвердите" in text and "вы не робот" in text:
        return "Авито просит антибот-проверку"
    if "captcha" in text:
        return "Авито просит captcha"
    return ""


def _parse_duckduckgo_html(page_html: str, *, max_results: int) -> list[MarketOffer]:
    offers: list[MarketOffer] = []
    seen: set[str] = set()
    block_re = re.compile(
        r"<a[^>]+class=[\"'][^\"']*result__a[^\"']*[\"'][^>]+href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<title>.*?)</a>(?P<tail>.{0,1600})",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for m in block_re.finditer(page_html or ""):
        raw_href = html.unescape(m.group("href") or "")
        href = raw_href
        qs = parse_qs(urlparse(raw_href).query)
        if "uddg" in qs and qs["uddg"]:
            href = unquote(qs["uddg"][0])
        if not href.startswith(("http://", "https://")):
            continue
        if href in seen:
            continue
        seen.add(href)
        title = _clean_text(m.group("title"))[:220]
        snippet = _clean_text(m.group("tail"))[:650]
        price = _parse_price(f"{title} {snippet}")
        offers.append(MarketOffer(source="Интернет", title=title, price=float(price or 0), url=href, snippet=snippet))
        if len(offers) >= max_results:
            break
    return _dedupe_and_sort(offers, max_results=max_results)


def _parse_bing_html(page_html: str, *, max_results: int) -> list[MarketOffer]:
    offers: list[MarketOffer] = []
    seen: set[str] = set()
    block_re = re.compile(
        r"<li[^>]+class=[\"'][^\"']*\bb_algo\b[^\"']*[\"'][^>]*>(?P<body>.*?)</li>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for m in block_re.finditer(page_html or ""):
        body = m.group("body") or ""
        link_m = re.search(r"<a[^>]+href=[\"'](?P<href>https?://[^\"']+)[\"'][^>]*>(?P<title>.*?)</a>", body, flags=re.IGNORECASE | re.DOTALL)
        if not link_m:
            continue
        href = html.unescape(link_m.group("href") or "").strip()
        parsed_href = urlparse(href)
        if parsed_href.netloc.casefold().endswith("bing.com"):
            encoded = (parse_qs(parsed_href.query).get("u") or [""])[0]
            if encoded.startswith("a1"):
                try:
                    payload = encoded[2:]
                    payload += "=" * (-len(payload) % 4)
                    decoded = base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8", errors="ignore")
                    if decoded.startswith(("http://", "https://")):
                        href = decoded
                except Exception:
                    pass
        if not href or href in seen:
            continue
        seen.add(href)
        title = _clean_text(link_m.group("title"))[:220]
        snippet = _clean_text(body)[:700]
        price = _parse_price(f"{title} {snippet}")
        offers.append(MarketOffer(source="Интернет", title=title, price=float(price or 0), url=href, snippet=snippet))
        if len(offers) >= max_results:
            break
    return _dedupe_and_sort(offers, max_results=max_results)


def _parse_bing_rss(page_xml: str, *, max_results: int) -> list[MarketOffer]:
    """Parse Bing's compact RSS search output into ordinary discovery offers."""
    offers: list[MarketOffer] = []
    seen: set[str] = set()
    try:
        root = ET.fromstring(page_xml or "")
    except ET.ParseError:
        return []
    for item in root.findall(".//item"):
        href = _clean_text(item.findtext("link") or "")
        if not href.startswith(("http://", "https://")) or href in seen:
            continue
        seen.add(href)
        title = _clean_text(item.findtext("title") or "")[:220]
        snippet = _clean_text(html.unescape(item.findtext("description") or ""))[:700]
        price = _parse_price(f"{title} {snippet}")
        offers.append(
            MarketOffer(
                source="Интернет",
                title=title or "Источник",
                price=float(price or 0),
                url=href,
                snippet=snippet,
            )
        )
        if len(offers) >= max_results:
            break
    return _dedupe_and_sort(offers, max_results=max_results)


def _decode_yahoo_result_url(raw_url: str) -> str:
    href = html.unescape(str(raw_url or "")).strip()
    parsed = urlparse(href)
    if not parsed.netloc.casefold().endswith("r.search.yahoo.com"):
        return href
    match = re.search(r"/RU=([^/]+)(?:/RK=|/RS=|$)", parsed.path)
    if not match:
        return ""
    decoded = unquote(match.group(1))
    return decoded if decoded.startswith(("http://", "https://")) else ""


def _parse_yahoo_html(page_html: str, *, max_results: int) -> list[MarketOffer]:
    offers: list[MarketOffer] = []
    soup = BeautifulSoup(page_html or "", "lxml")
    for heading in soup.select("h3"):
        anchor = heading.find_parent("a")
        if anchor is None:
            continue
        href = _decode_yahoo_result_url(str(anchor.get("href") or ""))
        if not href.startswith(("http://", "https://")):
            continue
        container = heading.find_parent("div", class_=re.compile(r"\balgo\b"))
        title = _clean_text(heading.get_text(" ", strip=True))[:220]
        snippet = _clean_text(container.get_text(" ", strip=True) if container else "")[:900]
        price = _parse_price(f"{title} {snippet}") or 0
        offers.append(
            MarketOffer(
                source="Интернет",
                title=title or "Источник",
                price=float(price),
                url=href,
                snippet=snippet,
                discovery_engine="Yahoo",
            )
        )
        if len(offers) >= max_results:
            break
    return _dedupe_and_sort(offers, max_results=max_results)


def _search_tokens(text: str) -> set[str]:
    stop = {
        "купить", "цена", "цены", "стоимость", "руб", "рублей", "за", "для",
        "работ", "услуги", "прайс", "интернет", "магазин", "москва", "область",
        "петрович", "всеинструменты", "лемана", "про", "site", "поставщик",
        "подрядчик", "авито", "avito", "ozon", "wildberries", "youla",
    }

    def stem(word: str) -> str:
        if word.isdigit() or len(word) <= 5:
            return word
        return re.sub(
            r"(?:иями|ями|ами|ого|ему|ому|ыми|ими|ий|ый|ая|яя|ое|ее|ые|ие|ов|ев|ам|ям|ах|ях|ом|ем|у|ю|а|я|ы|и|е|о)$",
            "",
            word,
        ) or word

    return {
        stem(word)
        for word in re.findall(r"[0-9a-zа-я]{2,}", str(text or "").casefold().replace("ё", "е"))
        if word not in stop and word not in {"ru", "м2", "м3"}
    }


def _host_matches(host: str, roots: tuple[str, ...] | list[str]) -> bool:
    clean = str(host or "").casefold().strip(".")
    return any(clean == root or clean.endswith("." + root) for root in roots)


def _configured_search_blocked_hosts() -> tuple[str, ...]:
    extra = [
        item.strip().casefold().lstrip(".")
        for item in (os.environ.get("MARKET_SEARCH_BLOCKED_DOMAINS") or "").split(",")
        if item.strip()
    ]
    return tuple(dict.fromkeys((*_SEARCH_BLOCKED_HOSTS, *extra)))


def _preferred_domains_for_query(query: str) -> tuple[str, ...]:
    folded = str(query or "").casefold().replace("ё", "е")
    priority: list[str] = []
    if any(marker in folded for marker in ("щеб", "песок", "грав", "отсев", "пгс", "асфальт", "неруд")):
        priority.extend(_PREFERRED_SPECIALTY_HOSTS["bulk"])
    if any(marker in folded for marker in ("геотекст", "геополот", "геосет", "дорнит")):
        priority.extend(_PREFERRED_SPECIALTY_HOSTS["geotextile"])
    if any(marker in folded for marker in ("кров", "утепл", "гидроизол", "пароизол", "мембран")):
        priority.extend(_PREFERRED_SPECIALTY_HOSTS["roofing"])
    if any(marker in folded for marker in ("инструмент", "дрель", "перфорат", "шурупов", "диск", "станок", "крепеж")):
        priority.extend(_PREFERRED_SPECIALTY_HOSTS["tools"])
    priority.extend(_PREFERRED_GENERAL_HOSTS)
    return tuple(dict.fromkeys(priority))


def _preferred_domain_bonus(host: str, query: str) -> tuple[float, str]:
    preferred = _preferred_domains_for_query(query)
    for index, root in enumerate(preferred):
        if _host_matches(host, [root]):
            # Категорийные и местные поставщики идут первыми; федеральные сети
            # всё равно получают заметный приоритет перед случайными SEO-сайтами.
            return max(1.4, 3.2 - index * 0.18), root
    return 0.0, ""


def _failure_code(reason: object) -> str:
    folded = str(reason or "").casefold()
    if any(marker in folded for marker in ("captcha", "капч", "антибот", "доступ ограничен", "защиты")):
        return "blocked"
    if "429" in folded or "too many requests" in folded:
        return "rate_limited"
    if "403" in folded or "forbidden" in folded:
        return "http_forbidden"
    if "404" in folded or "not found" in folded:
        return "not_found"
    if any(marker in folded for marker in ("timeout", "timed out", "таймаут", "не открылась", "не загруз")):
        return "timeout"
    if "единиц" in folded or "ед. изм" in folded:
        return "unit_mismatch"
    if "материал" in folded and ("включ" in folded or "отделить" in folded):
        return "scope_mismatch"
    if any(marker in folded for marker in ("нет цены", "не найдена рубл", "не распознана положительная цена")):
        return "no_price"
    if any(marker in folded for marker in ("не относится", "слабое совпадение", "совпадающей позиции")):
        return "irrelevant"
    if "не прямая" in folded or "категори" in folded:
        return "listing"
    if "медиан" in folded or "аномальн" in folded:
        return "price_anomaly"
    return "other"


def _candidate_decision(offer: MarketOffer, query: str) -> tuple[bool, float, str, str]:
    parsed = urlparse(offer.url or "")
    host = parsed.netloc.casefold().split(":", 1)[0]
    if parsed.scheme not in {"http", "https"} or not host:
        return False, 0.0, "Нет прямой HTTP-ссылки на источник", "invalid_url"
    if _host_matches(host, list(_SEARCH_ENGINE_HOSTS)):
        return False, 0.0, "Ссылка ведёт на поисковую выдачу, а не на источник", "search_page"
    if _host_matches(host, list(_configured_search_blocked_hosts())):
        return False, 0.0, f"Домен {host} исключён из универсального поиска", "blocked_domain"

    evidence = f"{offer.title} {offer.snippet}"
    folded = evidence.casefold().replace("ё", "е")
    wanted = _search_tokens(query)
    found = _search_tokens(evidence)
    common = wanted & found
    required = 1 if len(wanted) <= 2 else 2
    if len(common) < required:
        return False, 0.0, f"Слабое совпадение со сметной позицией: {len(common)} из {required} обязательных слов", "low_relevance"

    explicit_price = bool(re.search(r"\d[\d\s\u00a0\u202f.,]{0,14}\s*(?:₽|руб(?:\.|ля|лей)?|р\.)", evidence, flags=re.IGNORECASE))
    has_digits = bool(re.search(r"\d", evidence))
    noise_hits = [marker for marker in _SEARCH_NOISE_TEXT if marker in folded]
    path_noise = any(marker in parsed.path.casefold() for marker in _SEARCH_NOISE_PATHS)
    if (noise_hits or path_noise) and not explicit_price:
        marker = noise_hits[0] if noise_hits else "информационный раздел"
        return False, 0.0, f"Информационная страница без цены: {marker}", "informational"
    if (not parsed.path or parsed.path == "/") and not explicit_price:
        return False, 0.0, "Главная страница без цены — низкая вероятность карточки или прайса", "not_direct"
    if any(marker in parsed.path.casefold() for marker in ("/category/", "/search/", "/tag-page/")):
        return False, 0.0, "Страница категории или поиска, а не карточка товара/строка прайса", "listing"

    overlap = len(common) / max(1, min(len(wanted), 8))
    score = overlap * 5.0
    score += 3.0 if explicit_price else 0.0
    score += 0.8 if has_digits else -1.2
    score += 0.5 if host.endswith(".ru") else 0.0
    score += 0.8 if any(marker in parsed.path.casefold() for marker in ("price", "prais", "product", "tovar", "uslug", "service")) else 0.0
    score -= 0.9 if any(marker in parsed.path.casefold() for marker in ("/catalog", "/category", "/search")) else 0.0
    score -= 0.8 * len(noise_hits)
    preferred_bonus, preferred_root = _preferred_domain_bonus(host, query)
    score += preferred_bonus
    reasons = [f"совпадение {len(common)} слов"]
    reasons.append("цена есть в сниппете" if explicit_price else "цена будет проверена на странице")
    if preferred_root:
        reasons.append(f"приоритетный поставщик {preferred_root}")
    if not has_digits:
        reasons.append("без цифр в сниппете — понижен приоритет")
    return True, round(score, 3), "; ".join(reasons), "accepted"


def _relevant_search_offers(
    offers: list[MarketOffer],
    query: str,
    *,
    max_results: int,
    write_log: bool = True,
) -> list[MarketOffer]:
    ranked: list[MarketOffer] = []
    for offer in offers:
        accepted, score, reason, code = _candidate_decision(offer, query)
        offer.discovery_score = score
        offer.discovery_reason = reason
        if write_log:
            _append_market_search_log(
                "prefilter",
                query=query,
                engine=offer.discovery_engine,
                url=offer.url,
                title=offer.title[:220],
                accepted=accepted,
                score=score,
                reason_code=code,
                reason=reason,
            )
        if not accepted:
            continue
        ranked.append(offer)

    ranked = _dedupe_and_sort(ranked, max_results=max(1, len(ranked)))
    ranked.sort(key=lambda item: (-float(item.discovery_score or 0), -float(item.price > 0), item.title))
    try:
        per_domain = max(1, int(os.environ.get("MARKET_SEARCH_MAX_PER_DOMAIN", "2") or 2))
    except ValueError:
        per_domain = 2
    selected: list[MarketOffer] = []
    domain_counts: dict[str, int] = {}
    for offer in ranked:
        host = urlparse(offer.url or "").netloc.casefold().split(":", 1)[0]
        if domain_counts.get(host, 0) >= per_domain:
            if write_log:
                _append_market_search_log(
                    "prefilter",
                    query=query,
                    engine=offer.discovery_engine,
                    url=offer.url,
                    title=offer.title[:220],
                    accepted=False,
                    score=offer.discovery_score,
                    reason_code="domain_limit",
                    reason=f"Лимит {per_domain} перспективных страниц с одного домена",
                )
            continue
        domain_counts[host] = domain_counts.get(host, 0) + 1
        selected.append(offer)
        if len(selected) >= max_results:
            break
    return selected


def _dedupe_and_sort(offers: list[MarketOffer], *, max_results: int) -> list[MarketOffer]:
    by_url: dict[str, MarketOffer] = {}
    for offer in offers:
        if not offer.url:
            continue
        canonical_url = _canonical_offer_url(offer.url)
        prev = by_url.get(canonical_url)
        if prev is None:
            offer.url = canonical_url
            by_url[canonical_url] = offer
            continue
        raw_engines = [part.strip() for part in f"{prev.discovery_engine},{offer.discovery_engine}".split(",") if part.strip()]
        engines: list[str] = []
        for engine in raw_engines:
            if any(engine == current or engine.startswith(current + "/") for current in engines):
                continue
            engines = [current for current in engines if not current.startswith(engine + "/")]
            engines.append(engine)
        combined_engines = ", ".join(engines)
        best_discovery_score = max(float(prev.discovery_score or 0), float(offer.discovery_score or 0))
        best_discovery_reason = offer.discovery_reason if float(offer.discovery_score or 0) > float(prev.discovery_score or 0) else prev.discovery_reason
        verification_rank = {"verified": 0, "candidate": 1, "rejected": 2}
        prev_rank = verification_rank.get(prev.verification, 3)
        offer_rank = verification_rank.get(offer.verification, 3)
        if offer_rank < prev_rank:
            offer.url = canonical_url
            by_url[canonical_url] = offer
        elif offer_rank > prev_rank:
            pass
        elif prev.price <= 0 < offer.price:
            offer.url = canonical_url
            by_url[canonical_url] = offer
        elif offer.price > 0 and prev.price > 0 and offer.price < prev.price:
            offer.url = canonical_url
            by_url[canonical_url] = offer
        selected = by_url[canonical_url]
        selected.discovery_engine = combined_engines
        selected.discovery_score = best_discovery_score
        selected.discovery_reason = best_discovery_reason
    return sorted(
        by_url.values(),
        key=lambda x: (
            0 if x.verification == "verified" else 1 if x.verification == "candidate" else 2,
            -float(x.discovery_score or 0),
            0 if x.price > 0 else 1,
            -float(x.source_weight or 0),
            -float(x.confidence or 0),
            x.price if x.price > 0 else 10**18,
            0 if x.source == "Авито" else 1,
            x.title,
        ),
    )[:max_results]


def search_avito(
    query: str,
    *,
    region: str = "",
    max_results: int = 5,
    browser_fetcher: AvitoBrowserFetcher | None = None,
) -> tuple[list[MarketOffer], str]:
    force_refresh = (os.environ.get("MARKET_AVITO_FORCE_REFRESH", "0") or "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    cached = None if force_refresh else _load_search_cache("avito", query, region)
    if cached is not None:
        return _dedupe_and_sort(cached, max_results=max_results), ""
    guard_error = _before_avito_request()
    if guard_error:
        try:
            indexed = search_avito_index(query, region=region, max_results=max_results)
        except Exception as exc:
            indexed = []
            guard_error = f"{guard_error}; индекс Авито: {type(exc).__name__}: {exc}"
        if indexed:
            return indexed, f"{guard_error}; показаны неподтверждённые данные поискового индекса без открытия Авито"
        return [], guard_error
    q = query
    if region:
        q = f"{query} {region}"
    url = "https://www.avito.ru/all?" + urlencode({"q": q})
    if browser_fetcher is None or not browser_fetcher.enabled:
        return [], "Авито: браузерный поиск Playwright отключён"
    page = browser_fetcher.fetch(url)
    if not page:
        detail = browser_fetcher.last_error or "страница не загрузилась"
        return [], f"Авито Playwright: {detail}"
    block = _avito_block_reason(page)
    if block:
        _block_avito(block)
        try:
            indexed = search_avito_index(query, region=region, max_results=max_results)
        except Exception:
            indexed = []
        suffix = "; показаны неподтверждённые данные поискового индекса" if indexed else ""
        return indexed, block + "; очередь Авито поставлена на паузу" + suffix
    offers = browser_fetcher.current_avito_offers(base_url=url, max_results=max_results)
    if not offers:
        offers = _parse_avito_html(page, url, max_results=max_results)
    scroll_counts = list(getattr(browser_fetcher, "last_scroll_counts", []) or [])
    seen = scroll_counts[-1] if scroll_counts else 0
    _record_avito_page_success(cards=seen, query=q)
    if offers:
        _save_search_cache("avito", query, region, offers)
        return offers, ""
    skipped = int(browser_fetcher.last_skipped_cards or 0)
    return [], (
        "Авито Playwright: страница получена, но пригодных объявлений с ценой не найдено "
        f"(карточек в DOM: {seen}, пропущено без цены/ссылки: {skipped})"
    )


def _search_web_ddgs(query: str, *, max_results: int) -> tuple[list[MarketOffer], str]:
    global _DDGS_BLOCKED_UNTIL
    if time.monotonic() < _DDGS_BLOCKED_UNTIL:
        return [], "Резервный поиск временно пропущен после ошибки соединения"
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
    except ImportError:
        return [], "DDGS не установлен"

    offers: list[MarketOffer] = []
    seen: set[str] = set()
    last_err = ""
    connection_failed = False
    configured = (os.environ.get("MARKET_SEARCH_BACKENDS") or "brave,mojeek,startpage").split(",")
    backends = [backend.strip().casefold() for backend in configured if backend.strip()]
    try:
        backend_limit = max(1, min(4, int(os.environ.get("MARKET_SEARCH_BACKEND_LIMIT", "2") or 2)))
        ddgs_timeout = max(5, min(25, int(os.environ.get("MARKET_SEARCH_DDGS_TIMEOUT_SEC", "12") or 12)))
    except ValueError:
        backend_limit = 2
        ddgs_timeout = 12
    configured_regions = [
        item.strip() for item in (os.environ.get("MARKET_SEARCH_REGIONS") or "ru-ru").split(",") if item.strip()
    ] or ["ru-ru"]
    for backend in backends[:backend_limit]:
        for region in configured_regions[:2]:
            try:
                with DDGS(timeout=ddgs_timeout) as ddgs:
                    items = ddgs.text(
                        query,
                        region=region,
                        max_results=max(max_results, 10),
                        backend=backend,
                    )
                for item in items:
                    title = _clean_text(str(item.get("title") or ""))[:220]
                    snippet = _clean_text(str(item.get("body") or item.get("snippet") or ""))[:700]
                    url = str(item.get("href") or item.get("url") or "").strip()
                    if not title and not snippet:
                        continue
                    key = url or f"{title}|{snippet[:80]}"
                    if key in seen:
                        continue
                    seen.add(key)
                    price = _parse_price(f"{title} {snippet}") or 0
                    offers.append(
                        MarketOffer(
                            source="Интернет",
                            title=title or "Источник",
                            price=float(price),
                            url=url,
                            snippet=snippet,
                            discovery_engine=f"DDGS/{backend}",
                        )
                    )
                    if len(offers) >= max_results:
                        return _dedupe_and_sort(offers, max_results=max_results), ""
            except Exception as e:
                last_err = f"{backend}: {type(e).__name__}: {e}"[:300]
                folded_error = str(e).casefold()
                connection_failed = connection_failed or "connect" in folded_error or "timeout" in folded_error or "ssl" in folded_error
                continue
        if offers:
            break
    if not offers and connection_failed:
        _DDGS_BLOCKED_UNTIL = time.monotonic() + 5 * 60
    return _dedupe_and_sort(offers, max_results=max_results), last_err


def _search_web_searx(query: str, *, max_results: int) -> tuple[list[MarketOffer], str]:
    endpoint = (os.environ.get("MARKET_SEARX_URL") or "").strip()
    if not endpoint:
        return [], ""
    url = endpoint.rstrip("/") + "/search?" + urlencode(
        {"q": query, "format": "json", "language": "ru-RU", "safesearch": "0"}
    )
    try:
        payload = json.loads(_session_get(url, timeout=15))
    except Exception as exc:
        return [], f"SearX: {type(exc).__name__}: {exc}"[:400]
    raw_results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_results, list):
        return [], "SearX: ответ не содержит массива results"
    offers: list[MarketOffer] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = _clean_text(str(item.get("title") or ""))[:220]
        snippet = _clean_text(str(item.get("content") or item.get("snippet") or ""))[:700]
        href = str(item.get("url") or "").strip()
        if not href.startswith(("http://", "https://")):
            continue
        price = _parse_price(f"{title} {snippet}") or 0
        engines = item.get("engines")
        engine_label = ",".join(str(value) for value in engines) if isinstance(engines, list) else ""
        offers.append(
            MarketOffer(
                source="Интернет",
                title=title or "Источник",
                price=float(price),
                url=href,
                snippet=snippet,
                discovery_engine=f"SearX{f'/{engine_label}' if engine_label else ''}",
            )
        )
        if len(offers) >= max_results:
            break
    return _dedupe_and_sort(offers, max_results=max_results), ""


def _tag_search_engine(offers: list[MarketOffer], engine: str) -> list[MarketOffer]:
    for offer in offers:
        if not offer.discovery_engine:
            offer.discovery_engine = engine
    return offers


def _web_query(query: str, region: str) -> str:
    raw = re.sub(r"\s+", " ", f"{query} {region}".strip())
    folded = raw.casefold()
    if not any(marker in folded for marker in ("цена", "стоимость", "прайс", "₽", "руб")):
        raw = f"{raw} цена прайс ₽"
    return raw


def search_avito_index(query: str, *, region: str = "", max_results: int = 3) -> list[MarketOffer]:
    """Read search-engine snippets for Avito without opening avito.ru.

    Indexed snippets are deliberately candidates, never verified prices. They
    remain useful as a low-risk orientation while the direct Avito guard is on.
    """

    cached = _load_search_cache("avito_index", query, region)
    if cached is not None:
        return _dedupe_and_sort(cached, max_results=max_results)
    clean_query = re.sub(r"\s+", " ", f"{query} {region}".strip())
    q = f"site:avito.ru {clean_query} цена ₽"
    raw_offers: list[MarketOffer] = []
    errors: list[str] = []

    bing_rss_url = "https://www.bing.com/search?" + urlencode(
        {"format": "rss", "q": q, "mkt": "ru-RU", "setlang": "ru", "cc": "RU"}
    )
    try:
        page = _session_get(bing_rss_url, timeout=18)
        raw_offers.extend(_tag_search_engine(_parse_bing_rss(page, max_results=max_results * 8), "Bing RSS"))
    except Exception as exc:
        errors.append(f"Bing RSS: {type(exc).__name__}: {exc}")

    yahoo_url = "https://search.yahoo.com/search?" + urlencode({"p": q})
    try:
        page = _session_get(yahoo_url, timeout=18)
        raw_offers.extend(_parse_yahoo_html(page, max_results=max_results * 8))
    except Exception as exc:
        errors.append(f"Yahoo: {type(exc).__name__}: {exc}")

    wanted_tokens = _search_tokens(clean_query)
    accepted: list[MarketOffer] = []
    seen: set[str] = set()
    for offer in raw_offers:
        parsed = urlparse(offer.url or "")
        host = parsed.netloc.casefold().split(":", 1)[0]
        if not (host == "avito.ru" or host.endswith(".avito.ru")):
            continue
        url = str(offer.url or "").split("#", 1)[0]
        if not url or url in seen or float(offer.price or 0) <= 0:
            continue
        candidate_tokens = _search_tokens(f"{offer.title} {offer.snippet}")
        overlap = len(wanted_tokens & candidate_tokens)
        if wanted_tokens and overlap < min(2, len(wanted_tokens)):
            continue
        seen.add(url)
        offer.source = "Авито · поисковый индекс"
        offer.url = url
        offer.verification = "candidate"
        offer.confidence = min(0.45, 0.18 + overlap * 0.06)
        offer.verification_reason = "Цена видна только в индексе поисковика; страница Авито не открывалась"
        offer.page_checked = False
        offer.page_error = "Прямой доступ к Авито не выполнялся"
        offer.discovery_reason = "site:avito.ru, публичный поисковый индекс"
        offer.rejection_code = "indexed_snippet_only"
        offer.rejection_stage = "discovery"
        accepted.append(offer)

    accepted = _dedupe_and_sort(accepted, max_results=max_results)
    _save_search_cache("avito_index", query, region, accepted)
    _append_avito_log(
        "index_search",
        query=clean_query,
        offers=len(accepted),
        engines=sorted({offer.discovery_engine for offer in accepted if offer.discovery_engine}),
        errors=errors[:4],
        direct_avito_request=False,
    )
    return accepted


def search_web(query: str, *, region: str = "", max_results: int = 3) -> list[MarketOffer]:
    global _DDG_BLOCKED_UNTIL
    q = _web_query(query, region)
    errors: list[str] = []
    raw_offers: list[MarketOffer] = []

    # Bing RSS остаётся быстрым первым источником, но больше не завершает поиск:
    # минимум один независимый движок дополняет его нишевыми поставщиками.
    bing_rss_url = "https://www.bing.com/search?" + urlencode(
        {"format": "rss", "q": q, "mkt": "ru-RU", "setlang": "ru", "cc": "RU"}
    )
    try:
        page = _session_get(bing_rss_url)
        raw_offers.extend(_tag_search_engine(_parse_bing_rss(page, max_results=max_results * 6), "Bing RSS"))
    except Exception as e:
        errors.append(f"Bing RSS: {type(e).__name__}: {e}")

    yahoo_url = "https://search.yahoo.com/search?" + urlencode({"p": q})
    yahoo_offers: list[MarketOffer] = []
    try:
        page = _session_get(yahoo_url, timeout=18)
        yahoo_offers = _parse_yahoo_html(page, max_results=max_results * 6)
        raw_offers.extend(yahoo_offers)
    except Exception as e:
        errors.append(f"Yahoo: {type(e).__name__}: {e}")

    preferred_enabled = (os.environ.get("MARKET_SEARCH_PREFERRED_DOMAINS", "1") or "1").strip().casefold() not in {
        "0", "false", "no", "off",
    }
    preferred_domains = _preferred_domains_for_query(q)[:4]
    already_has_preferred = any(
        _preferred_domain_bonus(urlparse(offer.url or "").netloc.casefold().split(":", 1)[0], q)[0] > 0
        for offer in raw_offers
    )
    if preferred_enabled and preferred_domains and not already_has_preferred:
        site_clause = " OR ".join(f"site:{domain}" for domain in preferred_domains)
        preferred_query = f"{q} ({site_clause})"
        preferred_url = "https://www.bing.com/search?" + urlencode(
            {"format": "rss", "q": preferred_query, "mkt": "ru-RU", "setlang": "ru", "cc": "RU"}
        )
        try:
            page = _session_get(preferred_url, timeout=18)
            preferred_offers = _parse_bing_rss(page, max_results=max_results * 8)
            raw_offers.extend(_tag_search_engine(preferred_offers, "Bing RSS/поставщики"))
        except Exception as e:
            errors.append(f"Bing поставщики: {type(e).__name__}: {e}")

    searx_offers, searx_err = _search_web_searx(q, max_results=max_results * 5)
    raw_offers.extend(searx_offers)
    if searx_err:
        errors.append(searx_err)

    before_ddgs = _relevant_search_offers(raw_offers, q, max_results=max_results, write_log=False)
    if not (yahoo_offers or searx_offers) or len(before_ddgs) < max_results:
        ddgs_offers, ddgs_err = _search_web_ddgs(q, max_results=max_results * 5)
        raw_offers.extend(ddgs_offers)
        if ddgs_err:
            errors.append(ddgs_err)

    provisional = _relevant_search_offers(raw_offers, q, max_results=max_results, write_log=False)
    ddg_url = "https://duckduckgo.com/html/?" + urlencode({"q": q})
    if len(provisional) < max_results and time.monotonic() >= _DDG_BLOCKED_UNTIL:
        try:
            page = _session_get(ddg_url)
            raw_offers.extend(
                _tag_search_engine(_parse_duckduckgo_html(page, max_results=max_results * 5), "DuckDuckGo HTML")
            )
        except Exception as e:
            errors.append(f"DDG: {type(e).__name__}: {e}")
            response = getattr(e, "response", None)
            if isinstance(e, requests.RequestException) or int(getattr(response, "status_code", 0) or 0) in (403, 429):
                _DDG_BLOCKED_UNTIL = time.monotonic() + 15 * 60

    provisional = _relevant_search_offers(raw_offers, q, max_results=max_results, write_log=False)
    if not provisional:
        bing_url = "https://www.bing.com/search?" + urlencode(
            {"q": q, "mkt": "ru-RU", "setlang": "ru", "cc": "RU"}
        )
        try:
            page = _session_get(bing_url)
            raw_offers.extend(_tag_search_engine(_parse_bing_html(page, max_results=max_results * 5), "Bing HTML"))
        except Exception as e:
            errors.append(f"Bing HTML: {type(e).__name__}: {e}")

    offers = _relevant_search_offers(raw_offers, q, max_results=max_results, write_log=True)
    for error in errors:
        _append_market_search_log("engine_error", query=q, reason=error[:500])
    if offers:
        return offers

    if errors:
        raise RuntimeError("; ".join(errors))
    return []


def search_market(
    query: str,
    *,
    region: str = "",
    sources: list[str],
    max_results: int,
    browser_fetcher: AvitoBrowserFetcher | None = None,
) -> tuple[list[MarketOffer], str]:
    offers: list[MarketOffer] = []
    errors: list[str] = []
    # Сначала ищем прямые страницы поставщиков и подрядчиков. Авито остаётся
    # резервом и не получает запрос, если обычный веб уже дал кандидатов.
    if "web" in sources:
        try:
            web_offers = _load_search_cache("web", query, region)
            if web_offers is None:
                web_offers = search_web(query, region=region, max_results=max(3, min(5, max_results)))
                _save_search_cache("web", query, region, web_offers)
            offers.extend(web_offers)
        except Exception as e:
            errors.append(f"Интернет: {type(e).__name__}: {e}")
    if "avito" in sources and not offers:
        avito_offers, avito_err = search_avito(
            query,
            region=region,
            max_results=max_results,
            browser_fetcher=browser_fetcher,
        )
        offers.extend(avito_offers)
        if avito_err:
            errors.append(avito_err)
    offers = _dedupe_and_sort(offers, max_results=max_results)
    return offers, "; ".join(errors)


def _offer_bundle(offers: list[MarketOffer]) -> list[dict[str, object]]:
    return [
        {
            "source": o.source,
            "title": o.title,
            "price": round(float(o.price), 2) if o.price and o.price > 0 else "",
            "url": o.url,
            "phone": "",
            "snippet": o.snippet[:500],
            "verification": o.verification,
            "confidence": round(float(o.confidence or 0), 2),
            "verification_reason": o.verification_reason,
            "matched_unit": o.matched_unit,
            "observed_at": o.observed_at,
            "position_type": o.position_type,
            "page_checked": bool(o.page_checked),
            "page_error": o.page_error,
            "adapter": o.adapter,
            "price_scope": o.price_scope,
            "evidence": o.evidence[:1600],
            "published_at": o.published_at,
            "location": o.location,
            "estimate_ratio": o.estimate_ratio,
            "plausibility": o.plausibility,
            "identity_verified": bool(o.identity_verified),
            "source_weight": round(float(o.source_weight or 0), 3),
            "index_hit": bool(o.index_hit),
            "index_match_score": round(float(o.index_match_score or 0), 4),
            "audit_record_path": o.audit_record_path,
            "snapshot_path": o.snapshot_path,
            "discovery_engine": o.discovery_engine,
            "discovery_score": round(float(o.discovery_score or 0), 3),
            "discovery_reason": o.discovery_reason,
            "rejection_code": o.rejection_code,
            "rejection_stage": o.rejection_stage,
            "extractor": o.extractor,
            "price_facts_found": int(o.price_facts_found or 0),
            "consensus_status": o.consensus_status,
            "consensus_median": o.consensus_median,
            "consensus_ratio": o.consensus_ratio,
            "agent_price": o.agent_price,
            "agent_unit": o.agent_unit,
            "agent_evidence": o.agent_evidence[:1600],
        }
        for o in offers
    ]


def _structured_price_from_page(page_html: str) -> float | None:
    prices: list[float] = []

    def add(value: object) -> None:
        if isinstance(value, (int, float)):
            number = float(value)
        else:
            raw = re.sub(r"[^0-9.,]", "", str(value or "")).replace(",", ".")
            try:
                number = float(raw)
            except ValueError:
                return
        if 10 <= number <= 500_000_000:
            prices.append(number)

    def visit(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        node_type = str(node.get("@type") or "").casefold()
        price_context = node_type in {"offer", "aggregateoffer", "unitpricespecification", "priceSpecification".casefold()} or "priceCurrency" in node
        if price_context:
            for key in ("price", "lowPrice"):
                if key in node:
                    add(node.get(key))
        for value in node.values():
            if isinstance(value, (dict, list)):
                visit(value)

    script_re = re.compile(r"<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
    for match in script_re.finditer(page_html or ""):
        try:
            visit(json.loads(match.group(1).strip()))
        except Exception:
            continue
    meta_patterns = (
        r"<meta\b(?=[^>]*(?:itemprop=[\"']price[\"']|property=[\"']product:price:amount[\"']))[^>]*content=[\"']([^\"']+)[\"'][^>]*>",
        r"<meta\b(?=[^>]*content=[\"']([^\"']+)[\"'])[^>]*(?:itemprop=[\"']price[\"']|property=[\"']product:price:amount[\"'])[^>]*>",
    )
    for pattern in meta_patterns:
        for match in re.finditer(pattern, page_html or "", flags=re.IGNORECASE):
            add(match.group(1))
    return min(prices) if prices else None


def _source_browser_enabled() -> bool:
    return (os.environ.get("MARKET_SOURCE_BROWSER_FALLBACK", "1") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _fetch_source_page(
    url: str,
    *,
    timeout: int,
    browser_fetcher: AvitoBrowserFetcher | None,
) -> tuple[str, str, str]:
    cached = _load_source_page_cache(url)
    if cached is not None:
        return cached
    http_error = ""
    try:
        page_html = _session_get(url, timeout=timeout)
        block = _generic_block_reason(page_html, url)
        if block:
            http_error = block
        elif page_html:
            _save_source_page_cache(url, page_html=page_html, method="http")
            return page_html, "", "http"
        else:
            http_error = "Источник вернул пустую страницу"
    except Exception as exc:
        http_error = f"{type(exc).__name__}: {exc}"[:400]

    if _source_browser_enabled() and browser_fetcher is not None and browser_fetcher.enabled:
        browser_html = browser_fetcher.fetch_source_page(url)
        if browser_html:
            _save_source_page_cache(url, page_html=browser_html, method="playwright")
            return browser_html, "", "playwright"
        browser_error = browser_fetcher.last_error or "браузер не вернул страницу"
        error = f"HTTP: {http_error}; Playwright: {browser_error}"
    else:
        error = f"HTTP: {http_error}"
    _save_source_page_cache(url, error=error, method="error")
    return "", error, "error"


def _agent_unit_multiplier(unit: object) -> float:
    """Return the explicit denominator from an agent unit such as ``100 м``."""

    text = re.sub(r"\s+", " ", str(unit or "").replace("\xa0", " ")).strip().casefold()
    match = re.match(r"^(10|100|1000)\s*(?=[a-zа-я²³])", text)
    return float(match.group(1)) if match else 1.0


def _page_confirms_agent_evidence(page_html: str, offer: MarketOffer) -> bool:
    """Confirm the price Hermes saw without replacing it by another page price.

    Supplier price lists often contain dozens of products.  The universal page
    extractor can legitimately select a different row, so the agent's exact
    title/evidence wins only when the freshly fetched page contains both that
    price and enough identifying words.
    """

    if not page_html or not offer.agent_evidence or not offer.agent_price:
        return False
    page_text = re.sub(r"\s+", " ", BeautifulSoup(page_html, "html.parser").get_text(" ", strip=True)).casefold()
    if not page_text:
        return False
    raw_price = float(offer.agent_price)
    if raw_price <= 0:
        return False
    integer, _, decimals = f"{raw_price:.2f}".partition(".")
    grouped = r"[\s\u00a0\u202f]*".join(re.escape(char) for char in integer)
    decimal_pattern = rf"(?:[,.]{re.escape(decimals.rstrip('0'))}\d*)?" if decimals.rstrip("0") else r"(?:[,.]0{1,2})?"
    if not re.search(rf"(?<!\d){grouped}{decimal_pattern}(?!\d)", page_text):
        return False
    stop = {
        "цена", "руб", "рублей", "рубля", "стоимость", "купить", "за", "для", "от",
        "метр", "метра", "штука", "тонна", "прайс", "товар", "услуга",
    }
    identity_text = f"{offer.title} {offer.agent_evidence}".casefold().replace("ё", "е")
    tokens = {
        token
        for token in re.findall(r"[0-9a-zа-я-]{4,}", identity_text)
        if token not in stop and not token.isdigit()
    }
    matched = [token for token in tokens if token in page_text]
    return len(matched) >= min(2, max(1, len(tokens)))


def _enrich_offer_from_page(
    offer: MarketOffer,
    src_row: pd.Series,
    plan: MarketSearchPlan,
    *,
    browser_fetcher: AvitoBrowserFetcher | None = None,
) -> MarketOffer:
    inspection_name = market_query_name(src_row.get(COL_NAME, ""))
    if offer.adapter == "hermes-browser-agent" and offer.title:
        inspection_name = str(src_row.get(COL_NAME, "") or offer.title).strip()
    if not offer.url:
        offer.page_error = "Нет прямой ссылки"
        offer.rejection_code = "invalid_url"
        offer.rejection_stage = "fetch"
        return offer
    is_avito = "avito.ru" in urlparse(offer.url).netloc.casefold()
    if is_avito:
        guard_error = _before_avito_request()
        if guard_error:
            offer.page_error = guard_error
            offer.rejection_code = _failure_code(guard_error)
            offer.rejection_stage = "fetch"
            return offer
        if browser_fetcher is None or not browser_fetcher.enabled:
            offer.page_error = "Страница Авито не проверена: Playwright отключён"
            offer.rejection_code = "browser_disabled"
            offer.rejection_stage = "fetch"
            return offer
        page_html = browser_fetcher.fetch(offer.url)
        if not page_html:
            offer.page_error = f"Страница Авито не открылась: {browser_fetcher.last_error or 'нет данных'}"
            offer.rejection_code = _failure_code(offer.page_error)
            offer.rejection_stage = "fetch"
            return offer
        block = _avito_block_reason(page_html)
        if block:
            _block_avito(block)
            offer.page_error = block
            offer.rejection_code = "blocked"
            offer.rejection_stage = "fetch"
            return offer
    else:
        try:
            source_timeout = max(5, min(25, int(os.environ.get("MARKET_SOURCE_TIMEOUT_SEC", "12"))))
        except ValueError:
            source_timeout = 12
        page_html, source_error, source_method = _fetch_source_page(
            offer.url,
            timeout=source_timeout,
            browser_fetcher=browser_fetcher,
        )
        if not page_html:
            offer.page_error = f"Страница не открылась: {source_error or 'нет данных'}"
            offer.rejection_code = _failure_code(offer.page_error)
            offer.rejection_stage = "fetch"
            return offer
    try:
        html_limit = max(500_000, min(5_000_000, int(os.environ.get("MARKET_SOURCE_HTML_MAX_BYTES", "2000000") or 2_000_000)))
    except ValueError:
        html_limit = 2_000_000
    if len(page_html) > html_limit:
        page_html = page_html[:html_limit]
    inspection = inspect_source_page(
        page_html,
        offer.url,
        name=inspection_name,
        target_unit=str(src_row.get("Ед. изм.", "") or ""),
        position_bucket=plan.position.bucket,
    )
    if (
        not is_avito
        and not inspection.accepted
        and source_method != "playwright"
        and _source_browser_enabled()
        and browser_fetcher is not None
        and browser_fetcher.enabled
    ):
        browser_html = browser_fetcher.fetch_source_page(offer.url)
        if browser_html:
            if len(browser_html) > html_limit:
                browser_html = browser_html[:html_limit]
            browser_inspection = inspect_source_page(
                browser_html,
                offer.url,
                name=inspection_name,
                target_unit=str(src_row.get("Ед. изм.", "") or ""),
                position_bucket=plan.position.bucket,
            )
            _save_source_page_cache(offer.url, page_html=browser_html, method="playwright")
            if browser_inspection.accepted or browser_inspection.evidence:
                inspection = browser_inspection
                page_html = browser_html
    if offer.adapter == "hermes-browser-agent" and _page_confirms_agent_evidence(page_html, offer):
        offer.evidence = offer.agent_evidence
        offer.snippet = offer.agent_evidence[:500]
        offer.matched_unit = normalize_unit(offer.agent_unit or offer.matched_unit)
        offer.page_checked = True
        offer.page_error = ""
        offer.rejection_code = ""
        offer.rejection_stage = ""
        offer.extractor = "hermes-evidence-confirmed"
        offer.price_facts_found = max(1, int(inspection.facts_found or 0))
        return offer
    offer.adapter = inspection.adapter
    offer.price_scope = inspection.price_scope
    offer.evidence = inspection.evidence
    offer.matched_unit = inspection.unit
    offer.extractor = inspection.extractor
    offer.price_facts_found = inspection.facts_found
    if inspection.evidence:
        offer.snippet = inspection.evidence
    if not inspection.accepted or inspection.price is None:
        offer.page_error = inspection.reason or "На странице не подтверждена цена"
        offer.rejection_code = inspection.status.replace("-", "_") or _failure_code(offer.page_error)
        offer.rejection_stage = "extraction"
        return offer
    offer.price = float(inspection.price)
    offer.title = inspection.title or offer.title
    offer.snippet = inspection.evidence or offer.snippet
    offer.page_checked = True
    offer.page_error = ""
    offer.rejection_code = ""
    offer.rejection_stage = ""
    return offer


def _independent_domain_prices(offers: list[MarketOffer]) -> list[float]:
    by_domain: dict[str, list[float]] = {}
    for offer in offers:
        if offer.verification != "verified" or not offer.price or offer.price <= 0:
            continue
        host = urlparse(offer.url or "").netloc.casefold().split(":", 1)[0]
        if not host:
            continue
        by_domain.setdefault(host, []).append(float(offer.price))
    return [float(statistics.median(values)) for values in by_domain.values() if values]


def _apply_market_consensus_guard(
    offers: list[MarketOffer],
    *,
    references: list[MarketOffer] | None = None,
) -> list[MarketOffer]:
    try:
        threshold = max(1.5, float(os.environ.get("MARKET_ANOMALY_RATIO", "3") or 3))
        minimum = max(3, int(os.environ.get("MARKET_ANOMALY_MIN_DOMAINS", "3") or 3))
    except ValueError:
        threshold = 3.0
        minimum = 3
    pool = _dedupe_and_sort([*(references or []), *offers], max_results=max(1, len(references or []) + len(offers)))
    reference_prices = _independent_domain_prices(pool)
    for offer in offers:
        if offer.verification != "verified":
            continue
        consensus = assess_market_median_anomaly(
            offer.price,
            reference_prices,
            threshold=threshold,
            min_sources=minimum,
        )
        offer.consensus_status = consensus.status
        offer.consensus_median = round(float(consensus.median), 2) if consensus.median else None
        offer.consensus_ratio = consensus.ratio
        if consensus.status == "review":
            offer.verification = "candidate"
            offer.confidence = min(float(offer.confidence or 0), 0.54)
            offer.verification_reason = consensus.reason
            offer.rejection_code = "market_outlier"
            offer.rejection_stage = "consensus"
    return offers


def _verify_offers(
    src_row: pd.Series,
    offers: list[MarketOffer],
    plan: MarketSearchPlan,
    *,
    browser_fetcher: AvitoBrowserFetcher | None = None,
    avito_collect_only: bool = False,
    reference_offers: list[MarketOffer] | None = None,
) -> list[MarketOffer]:
    checked: list[MarketOffer] = []
    for offer in offers:
        started_at = time.monotonic()
        is_avito = "avito.ru" in urlparse(offer.url or "").netloc.casefold()
        is_agent_offer = offer.adapter in {"hermes-browser-agent", "hermes-avito-agent"}
        is_agent_avito = offer.adapter == "hermes-avito-agent" and is_avito
        page_row = src_row
        if is_agent_offer and offer.title:
            # Agent-discovered pages can contain many prices. Its exact title
            # narrows page extraction, while identity is still checked against
            # the original estimate row below.
            page_row = src_row.copy()
            page_row[COL_NAME] = offer.title
        if is_agent_avito and offer.page_checked and offer.evidence:
            # Hermes has already opened the direct listing in the persistent Mac
            # browser session. Reopening it from the server would use another IP
            # and defeat the purpose of the dedicated Avito mode.
            offer.snippet = offer.evidence
            offer.page_error = ""
        elif avito_collect_only and is_avito:
            offer.page_checked = False
            offer.page_error = (
                "Карточка выдачи Авито сохранена без открытия объявления — "
                "бережный режим после ограничения IP"
            )
        else:
            offer = _enrich_offer_from_page(offer, page_row, plan, browser_fetcher=browser_fetcher)
        check = check_offer(
            name=(
                str(src_row.get(COL_NAME, "") or "").strip()
                if is_agent_offer
                else market_query_name(src_row.get(COL_NAME, ""))
            ),
            unit=src_row.get("Ед. изм.", ""),
            basis_code=src_row.get("basis_code", ""),
            section=src_row.get("Раздел", ""),
            title=offer.title,
            snippet=offer.snippet,
            url=offer.url,
            price=offer.price,
            page_checked=offer.page_checked,
            source_unit=offer.matched_unit,
        )
        offer.verification = check.status
        offer.identity_verified = check.status == "verified"
        offer.source_weight = source_quality(offer.url, offer.source)
        offer.confidence = check.confidence
        offer.verification_reason = check.reason
        if offer.page_error and check.status == "candidate":
            offer.verification_reason = offer.page_error
            offer.rejection_code = offer.rejection_code or _failure_code(offer.page_error)
            offer.rejection_stage = offer.rejection_stage or "verification"
        offer.matched_unit = check.matched_unit
        offer.observed_at = check.observed_at
        offer.position_type = plan.position.slug
        plausibility = assess_price_plausibility(
            estimate_price=src_row.get(COL_UNIT_PRICE, ""),
            market_price=offer.price,
            name=src_row.get(COL_NAME, ""),
            unit=src_row.get("Ед. изм.", ""),
            quantity=src_row.get(COL_QTY, ""),
            total=src_row.get(COL_SUM, ""),
        )
        offer.estimate_ratio = plausibility.ratio
        offer.plausibility = plausibility.status
        if check.status == "verified" and plausibility.status in {"review", "extreme"}:
            offer.verification = "candidate"
            offer.confidence = min(offer.confidence, 0.54 if plausibility.status == "review" else 0.32)
            offer.verification_reason = plausibility.reason
            offer.rejection_code = "estimate_scale"
            offer.rejection_stage = "plausibility"
        elif avito_collect_only and is_avito and plausibility.status in {"review", "extreme"}:
            offer.verification_reason = f"{offer.verification_reason}; {plausibility.reason}"
        if check.status != "rejected":
            checked.append(offer)
        else:
            offer.rejection_code = offer.rejection_code or _failure_code(check.reason)
            offer.rejection_stage = offer.rejection_stage or "identity"
        _append_market_search_log(
            "verification",
            query=plan.queries[0] if plan.queries else market_query_name(src_row.get(COL_NAME, "")),
            engine=offer.discovery_engine,
            url=offer.url,
            title=offer.title[:220],
            accepted=offer.verification == "verified",
            verification=offer.verification,
            reason_code=offer.rejection_code,
            stage=offer.rejection_stage,
            reason=offer.verification_reason,
            fetch_method=offer.adapter,
            matched_unit=offer.matched_unit,
            facts_found=offer.price_facts_found,
            elapsed_ms=int(round((time.monotonic() - started_at) * 1000)),
        )
    checked = _dedupe_and_sort(checked, max_results=max(1, len(checked)))
    return _apply_market_consensus_guard(checked, references=reference_offers)


def research_position_market(
    name: str,
    *,
    unit: str = "",
    basis_code: str = "",
    section: str = "",
    region: str = "",
    sources: list[str] | None = None,
    max_results: int = 5,
    browser_fetcher: AvitoBrowserFetcher | None = None,
) -> tuple[list[MarketOffer], MarketSearchPlan, str]:
    """Discover and verify offers for one position without writing a report."""
    plan = build_search_plan(name, unit, basis_code, section, region)
    queries = list(plan.queries) or [_compact_query(name)]
    row = pd.Series(
        {
            COL_NAME: name,
            "Ед. изм.": unit,
            "basis_code": basis_code,
            "Раздел": section,
        }
    )
    selected_sources = sources or ["web", "avito"]
    primary_sources = [source for source in selected_sources if source != "avito"] or selected_sources
    checked: list[MarketOffer] = []
    errors: list[str] = []
    for query in queries[:2]:
        found, found_error = search_market(
            query,
            region="" if plan.queries else region,
            sources=primary_sources,
            max_results=max_results,
            browser_fetcher=browser_fetcher,
        )
        verified_batch = _verify_offers(
            row,
            found,
            plan,
            browser_fetcher=browser_fetcher,
            reference_offers=checked,
        )
        checked = _apply_market_consensus_guard(
            _dedupe_and_sort(checked + verified_batch, max_results=max_results)
        )
        if found_error:
            errors.append(found_error)
        if sum(1 for offer in checked if offer.verification == "verified") >= min(3, max_results):
            break
    if "avito" in selected_sources and "web" in selected_sources and not any(
        offer.verification == "verified" for offer in checked
    ):
        found, found_error = search_market(
            queries[0],
            region="" if plan.queries else region,
            sources=["avito"],
            max_results=max_results,
            browser_fetcher=browser_fetcher,
        )
        avito_batch = _verify_offers(
            row,
            found,
            plan,
            browser_fetcher=browser_fetcher,
            reference_offers=checked,
        )
        checked = _apply_market_consensus_guard(
            _dedupe_and_sort(checked + avito_batch, max_results=max_results)
        )
        if found_error:
            errors.append(found_error)
    return checked, plan, "; ".join(dict.fromkeys(errors))


def _friendly_market_error(err: str) -> str:
    folded = str(err or "").casefold()
    if not folded:
        return ""
    if "429" in folded or "too many requests" in folded:
        return "часть источников ограничила частоту запросов"
    if any(marker in folded for marker in ("ddgs", "connecterror", "httperror", "timed out", "timeout")):
        return "часть интернет-источников временно недоступна"
    return "не все интернет-источники удалось проверить"


def _build_output_row(src_row: pd.Series, *, offers: list[MarketOffer], query: str, err: str, plan: MarketSearchPlan) -> dict:
    base = {str(k): src_row.get(k, "") for k in src_row.index if not str(k).startswith("_market")}
    verified = [o for o in offers if o.verification == "verified"]
    candidates = [o for o in offers if o.verification == "candidate"]
    prices = [float(o.price) for o in verified if o.price and o.price > 0]
    weighted_prices = [
        (float(o.price), max(0.01, float(o.source_weight or source_quality(o.url, o.source))) * max(0.05, float(o.confidence or 0.5)))
        for o in verified
        if o.price and o.price > 0
    ]
    prices_s = "; ".join(f"{p:,.0f}".replace(",", " ") for p in prices)
    bundle = _offer_bundle(offers)
    failure_counts: dict[str, int] = {}
    for offer in offers:
        if offer.rejection_code:
            failure_counts[offer.rejection_code] = failure_counts.get(offer.rejection_code, 0) + 1
    failure_summary = "; ".join(f"{code}: {count}" for code, count in sorted(failure_counts.items()))
    sources_s = "; ".join(o.url for o in offers if o.url)
    titles_s = "\n".join(f"{i}. {o.title}" for i, o in enumerate(offers, start=1))
    if prices:
        status = f"проверенных источников: {len(prices)}"
        raw_med = statistics.median(prices)
        med = weighted_median(weighted_prices) or raw_med
        mn = min(prices)
        mx = max(prices)
    elif candidates:
        status = f"есть кандидаты: {len(candidates)}, в расчёт не включены"
        med = raw_med = mn = mx = ""
    else:
        status = "обработано, подтверждённых цен не найдено"
        med = raw_med = mn = mx = ""
    if not plan.can_auto_price:
        status = plan.warning or "позицию нужно детализировать"
    if err:
        friendly_error = _friendly_market_error(err)
        status = (status + "; " if status else "") + friendly_error

    base.update(
        {
            "Рыночные источники": titles_s,
            "Рыночные источники (полный текст)": "\n".join(
                (
                    f"{i}. [{o.source}] {o.title}\nЦена: "
                    f"{f'{o.price:,.0f} ₽' if o.price and o.price > 0 else 'не распознана'}"
                    + (f"\nОпубликовано: {o.published_at}" if o.published_at else "")
                    + (f"\nЛокация: {o.location}" if o.location else "")
                    + f"\nПроверка: {'подтверждён' if o.verification == 'verified' else 'кандидат'} — "
                    f"{o.verification_reason}"
                    + f"\nВес источника: {float(o.source_weight or source_quality(o.url, o.source)):.2f}"
                    + (f"\nПоисковик: {o.discovery_engine}" if o.discovery_engine else "")
                    + (f"\nИзвлечение: {o.extractor}, найдено цен на странице: {o.price_facts_found}" if o.extractor else "")
                    + (f"\nКод проверки: {o.rejection_code} ({o.rejection_stage})" if o.rejection_code else "")
                    + (f"\nМедиана группы: {o.consensus_median:,.2f} ₽; отношение: ×{o.consensus_ratio:.2f}" if o.consensus_median and o.consensus_ratio else "")
                    + (f"\nАудит: {o.audit_record_path}" if o.audit_record_path else "")
                    + f"\n{o.url}"
                )
                for i, o in enumerate(offers, start=1)
            ),
            "Цены за ед. (рынок, руб)": prices_s,
            "Медиана цена за ед. (рынок)": med,
            "Обычная медиана цена за ед. (рынок)": raw_med,
            "Суммарный вес проверенных источников": round(sum(weight for _, weight in weighted_prices), 3),
            "Мин цена за ед. (рынок)": mn,
            "Макс цена за ед. (рынок)": mx,
            "Телефоны (строго)": "",
            "Ссылки (строго)": sources_s,
            "Цена-сайт-телефон (json)": json.dumps(bundle, ensure_ascii=False) if bundle else "",
            "Источники (ссылки/телефоны)": sources_s,
            "Ошибка / статус": status,
            "Техническая ошибка рынка": err[:2000],
            "Причины отклонения кандидатов": failure_summary,
            "Поисковый запрос рынка": query,
            "Тип позиции рынка": plan.position.label,
            "Код типа позиции рынка": plan.position.slug,
            "Группа рынка": plan.position.bucket_label,
            "Стратегия поиска рынка": plan.strategy_label,
            "Источники для поиска": plan.source_label,
            "Единица цены рынка": plan.normalized_unit,
            "Проверенных источников": len(verified),
            "Непроверенных кандидатов": len(candidates),
            "Источников из локального индекса": sum(1 for offer in offers if offer.index_hit),
            "Автоматический расчёт разрешён": "Да" if plan.can_auto_price else "Нет",
            "Предупреждение анализа": plan.warning,
        }
    )
    for i, offer in enumerate(offers[:5], start=1):
        base[f"Источник {i}"] = offer.source
        base[f"Название объявления {i}"] = offer.title
        base[f"Цена объявления {i}"] = offer.price
        base[f"Ссылка объявления {i}"] = offer.url
        base[f"Дата публикации {i}"] = offer.published_at
        base[f"Локация объявления {i}"] = offer.location
        base[f"Проверка источника {i}"] = "Подтверждён" if offer.verification == "verified" else "Кандидат"
        base[f"Причина проверки {i}"] = offer.verification_reason
        base[f"Вес источника {i}"] = round(float(offer.source_weight or source_quality(offer.url, offer.source)), 3)
        base[f"Аудиторская запись {i}"] = offer.audit_record_path
        base[f"Поисковик {i}"] = offer.discovery_engine
        base[f"Оценка кандидата {i}"] = round(float(offer.discovery_score or 0), 3)
        base[f"Способ извлечения {i}"] = offer.extractor
        base[f"Найдено цен на странице {i}"] = int(offer.price_facts_found or 0)
        base[f"Код отклонения {i}"] = offer.rejection_code
        base[f"Этап отклонения {i}"] = offer.rejection_stage
        base[f"Медиана группы {i}"] = offer.consensus_median
        base[f"Отклонение от медианы {i}"] = offer.consensus_ratio
    return base


def _eligible_rows(df: pd.DataFrame) -> list[tuple[int, pd.Series]]:
    out: list[tuple[int, pd.Series]] = []
    for idx, row in df.iterrows():
        if COL_DUP in df.columns and str(row.get(COL_DUP, "")).strip() == "Да":
            continue
        name = str(row.get(COL_NAME, "") or "").strip()
        if len(name) < 8:
            continue
        out.append((idx, row))
    return out


def _read_previous(path: Path) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    try:
        return pd.read_excel(path)
    except Exception:
        return pd.DataFrame()


def _revalidate_previous(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Apply current scale checks to saved evidence without another web request."""

    bundle_column = "Цена-сайт-телефон (json)"
    if frame.empty or bundle_column not in frame.columns:
        return frame, 0
    result = frame.copy()
    changed_rows = 0
    for index, row in result.iterrows():
        raw = row.get(bundle_column, "")
        if raw is None or (isinstance(raw, float) and pd.isna(raw)) or not str(raw).strip():
            continue
        try:
            bundle = json.loads(str(raw))
        except (TypeError, ValueError):
            continue
        if not isinstance(bundle, list):
            continue
        row_changed = False
        for item in bundle:
            if not isinstance(item, dict):
                continue
            plausibility = assess_price_plausibility(
                estimate_price=row.get(COL_UNIT_PRICE, ""),
                market_price=item.get("price", ""),
                name=row.get(COL_NAME, ""),
                unit=row.get("Ед. изм.", ""),
                quantity=row.get(COL_QTY, ""),
                total=row.get(COL_SUM, ""),
            )
            old_status = str(item.get("verification") or "candidate").casefold()
            new_status = old_status if old_status in {"verified", "candidate"} else "candidate"
            old_reason = str(item.get("verification_reason") or "")
            was_scale_downgrade = old_reason.startswith((
                "Цена отличается от сметы",
                "Аномальный масштаб цены относительно сметы",
            ))
            if new_status == "candidate" and (
                bool(item.get("identity_verified")) or was_scale_downgrade
            ) and plausibility.status in {"plausible", "unknown"}:
                identity_check = check_offer(
                    name=market_query_name(row.get(COL_NAME, "")),
                    unit=row.get("Ед. изм.", ""),
                    basis_code=row.get("basis_code", ""),
                    section=row.get("Раздел", ""),
                    title=item.get("title", ""),
                    snippet=item.get("snippet", ""),
                    url=item.get("url", ""),
                    price=item.get("price", ""),
                    page_checked=bool(item.get("page_checked")),
                    source_unit=item.get("matched_unit", ""),
                )
                if identity_check.status == "verified":
                    new_status = "verified"
                    item["identity_verified"] = True
                    item["verification_reason"] = identity_check.reason
                    item["confidence"] = identity_check.confidence
            if new_status == "verified" and plausibility.status in {"review", "extreme"}:
                new_status = "candidate"
                item["verification_reason"] = plausibility.reason
                try:
                    old_confidence = float(item.get("confidence") or 0)
                except (TypeError, ValueError):
                    old_confidence = 0.0
                item["confidence"] = min(old_confidence, 0.54 if plausibility.status == "review" else 0.32)
            if item.get("verification") != new_status:
                item["verification"] = new_status
                row_changed = True
            if item.get("estimate_ratio") != plausibility.ratio or item.get("plausibility") != plausibility.status:
                item["estimate_ratio"] = plausibility.ratio
                item["plausibility"] = plausibility.status
                row_changed = True
        if not row_changed:
            continue
        changed_rows += 1
        verified = [item for item in bundle if isinstance(item, dict) and item.get("verification") == "verified"]
        candidates = [item for item in bundle if isinstance(item, dict) and item.get("verification") == "candidate"]
        prices: list[float] = []
        weighted_prices: list[tuple[float, float]] = []
        for item in verified:
            try:
                price = float(item.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if price > 0:
                prices.append(price)
                try:
                    confidence = max(0.05, float(item.get("confidence") or 0.5))
                except (TypeError, ValueError):
                    confidence = 0.5
                try:
                    quality = float(item.get("source_weight") or 0)
                except (TypeError, ValueError):
                    quality = 0.0
                if quality <= 0:
                    quality = source_quality(item.get("url", ""), item.get("source", ""))
                    item["source_weight"] = quality
                weighted_prices.append((price, quality * confidence))
        result.at[index, bundle_column] = json.dumps(bundle, ensure_ascii=False)
        result.at[index, "Проверенных источников"] = len(verified)
        result.at[index, "Непроверенных кандидатов"] = len(candidates)
        result.at[index, "Цены за ед. (рынок, руб)"] = "; ".join(
            f"{price:,.0f}".replace(",", " ") for price in prices
        )
        raw_median = statistics.median(prices) if prices else float("nan")
        result.at[index, "Медиана цена за ед. (рынок)"] = weighted_median(weighted_prices) if prices else float("nan")
        result.at[index, "Обычная медиана цена за ед. (рынок)"] = raw_median
        result.at[index, "Суммарный вес проверенных источников"] = sum(weight for _, weight in weighted_prices)
        result.at[index, "Мин цена за ед. (рынок)"] = min(prices) if prices else float("nan")
        result.at[index, "Макс цена за ед. (рынок)"] = max(prices) if prices else float("nan")
        if prices:
            result.at[index, "Ошибка / статус"] = f"проверенных источников: {len(prices)}"
        elif candidates:
            result.at[index, "Ошибка / статус"] = f"есть кандидаты: {len(candidates)}, в расчёт не включены"
        else:
            result.at[index, "Ошибка / статус"] = "обработано, подтверждённых цен не найдено"
        for item_number, item in enumerate(bundle[:5], start=1):
            result.at[index, f"Проверка источника {item_number}"] = (
                "Подтверждён" if item.get("verification") == "verified" else "Кандидат"
            )
            result.at[index, f"Причина проверки {item_number}"] = str(item.get("verification_reason") or "")
            result.at[index, f"Вес источника {item_number}"] = float(
                item.get("source_weight") or source_quality(item.get("url", ""), item.get("source", ""))
            )
            result.at[index, f"Аудиторская запись {item_number}"] = str(item.get("audit_record_path") or "")
    return result, changed_rows


def _processed_keys(prev: pd.DataFrame) -> set[str]:
    if prev.empty or COL_NAME not in prev.columns:
        return set()
    # Отчёты прежней версии брали цену из поискового сниппета и не содержат
    # результата проверки самой страницы. Их нельзя считать завершёнными.
    if "Код типа позиции рынка" not in prev.columns or "Проверенных источников" not in prev.columns:
        return set()
    return {_norm_key(str(x)) for x in prev[COL_NAME].fillna("").astype(str) if str(x).strip()}


def _verified_market_keys(prev: pd.DataFrame) -> set[str]:
    if prev.empty or COL_NAME not in prev.columns:
        return set()
    keys: set[str] = set()
    for _, row in prev.iterrows():
        try:
            verified_count = int(float(row.get("Проверенных источников") or 0))
        except (TypeError, ValueError):
            verified_count = 0
        if verified_count > 0:
            key = _norm_key(str(row.get(COL_NAME, "") or ""))
            if key:
                keys.add(key)
    return keys


def _saved_offers_for_key(prev: pd.DataFrame, key: str) -> list[MarketOffer]:
    """Restore saved evidence so an Avito-only pass cannot erase other sites."""
    if prev.empty or COL_NAME not in prev.columns or not key:
        return []
    matches = prev[prev[COL_NAME].fillna("").astype(str).map(_norm_key) == key]
    if matches.empty:
        return []
    raw = matches.iloc[0].get("Цена-сайт-телефон (json)", "")
    if raw is None or (isinstance(raw, float) and pd.isna(raw)) or not str(raw).strip():
        return []
    try:
        bundle = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    if not isinstance(bundle, list):
        return []
    fields = set(MarketOffer.__dataclass_fields__)
    offers: list[MarketOffer] = []
    for item in bundle:
        if not isinstance(item, dict):
            continue
        payload = {field: item.get(field) for field in fields if field in item}
        try:
            payload["price"] = float(payload.get("price") or 0)
            offers.append(MarketOffer(**payload))
        except (TypeError, ValueError):
            continue
    return offers


def _avito_safe_error_is_fatal(err: str) -> bool:
    folded = str(err or "").casefold()
    if not folded:
        return False
    if any(marker in folded for marker in ("на паузе", "ограничил доступ", "очередь авито", "playwright отключён")):
        return True
    return "авито playwright:" in folded and "страница получена" not in folded


def _merge_rows(prev: pd.DataFrame, rows: list[dict]) -> pd.DataFrame:
    cur = pd.DataFrame(rows)
    if prev.empty:
        return cur
    if cur.empty:
        return prev
    if COL_NAME not in prev.columns or COL_NAME not in cur.columns:
        return pd.concat([prev, cur], ignore_index=True)
    prev = prev.copy()
    cur = cur.copy()
    prev["__key"] = prev[COL_NAME].map(_norm_key)
    cur["__key"] = cur[COL_NAME].map(_norm_key)
    prev = prev[~prev["__key"].isin(set(cur["__key"]))]
    out = pd.concat([prev, cur], ignore_index=True).drop(columns=["__key"], errors="ignore")
    return out


def _offers_from_local_index(src_row: pd.Series, *, max_results: int) -> list[MarketOffer]:
    rows = lookup_verified_offers(
        name=src_row.get(COL_NAME, ""),
        unit=src_row.get("Ед. изм.", ""),
        basis_code=src_row.get("basis_code", ""),
        section=src_row.get("Раздел", ""),
        limit=max_results,
    )
    offers: list[MarketOffer] = []
    for item in rows:
        try:
            observed_at = datetime.fromtimestamp(float(item.get("observed_at") or 0), tz=timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError, OSError):
            observed_at = ""
        offer = MarketOffer(
            source=str(item.get("source") or "Локальный индекс"),
            title=str(item.get("title") or "Проверенный источник"),
            price=float(item.get("price") or 0),
            url=str(item.get("url") or ""),
            snippet="Переиспользовано из локального индекса проверенных цен",
            verification="verified",
            confidence=max(0.05, min(1.0, float(item.get("confidence") or 0.5) * float(item.get("match_score") or 1))),
            verification_reason="Проверенный источник из локального индекса; TTL не истёк",
            matched_unit=str(item.get("unit") or ""),
            observed_at=observed_at,
            position_type=str(item.get("position_type") or ""),
            page_checked=True,
            evidence=f"Аудиторская запись: {item.get('audit_record_path') or 'метаданные индекса'}",
            source_weight=float(item.get("source_weight") or source_quality(item.get("url"), item.get("source"))),
            index_hit=True,
            index_match_score=float(item.get("match_score") or 0),
            audit_record_path=str(item.get("audit_record_path") or ""),
            snapshot_path=str(item.get("snapshot_path") or ""),
            identity_verified=True,
        )
        plausibility = assess_price_plausibility(
            estimate_price=src_row.get(COL_UNIT_PRICE, ""),
            market_price=offer.price,
            name=src_row.get(COL_NAME, ""),
            unit=src_row.get("Ед. изм.", ""),
            quantity=src_row.get(COL_QTY, ""),
            total=src_row.get(COL_SUM, ""),
        )
        offer.estimate_ratio = plausibility.ratio
        offer.plausibility = plausibility.status
        if plausibility.status in {"review", "extreme"}:
            offer.verification = "candidate"
            offer.verification_reason = plausibility.reason
        offers.append(offer)
    return _dedupe_and_sort(offers, max_results=max_results)


def _store_verified_offers_in_index(tender_id: str, src_row: pd.Series, offers: list[MarketOffer]) -> int:
    fresh = [offer for offer in offers if offer.verification == "verified" and not offer.index_hit]
    if not fresh:
        return 0
    payloads: list[dict] = []
    for offer in fresh:
        page_html = ""
        cached = _load_source_page_cache(offer.url)
        if cached is not None:
            page_html = cached[0]
        payload = dict(vars(offer))
        payload["page_html"] = page_html
        payloads.append(payload)
    stored = record_verified_offers(
        tender_id=tender_id,
        name=src_row.get(COL_NAME, ""),
        unit=src_row.get("Ед. изм.", ""),
        basis_code=src_row.get("basis_code", ""),
        section=src_row.get("Раздел", ""),
        offers=payloads,
    )
    if stored:
        indexed = lookup_verified_offers(
            name=src_row.get(COL_NAME, ""),
            unit=src_row.get("Ед. изм.", ""),
            basis_code=src_row.get("basis_code", ""),
            section=src_row.get("Раздел", ""),
            limit=max(20, len(offers) * 3),
        )
        by_url = {_canonical_offer_url(str(item.get("url") or "")): item for item in indexed}
        for offer in fresh:
            item = by_url.get(_canonical_offer_url(offer.url))
            if not item:
                continue
            offer.source_weight = float(item.get("source_weight") or offer.source_weight)
            offer.audit_record_path = str(item.get("audit_record_path") or "")
            offer.snapshot_path = str(item.get("snapshot_path") or "")
    return stored


def _backfill_price_index_from_report(tender_id: str, frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if frame.empty or COL_NAME not in frame.columns:
        return frame, 0
    result = frame.copy()
    fields = set(MarketOffer.__dataclass_fields__)
    stored_total = 0
    changed_rows = 0
    for index, row in result.iterrows():
        raw = row.get("Цена-сайт-телефон (json)", "")
        if raw is None or (isinstance(raw, float) and pd.isna(raw)) or not str(raw).strip():
            continue
        try:
            bundle = json.loads(str(raw))
        except (TypeError, ValueError):
            continue
        if not isinstance(bundle, list):
            continue
        offers: list[MarketOffer] = []
        for item in bundle:
            if not isinstance(item, dict):
                continue
            payload = {field: item.get(field) for field in fields if field in item}
            try:
                payload["price"] = float(payload.get("price") or 0)
                offers.append(MarketOffer(**payload))
            except (TypeError, ValueError):
                continue
        stored = _store_verified_offers_in_index(tender_id, row, offers)
        if not stored:
            continue
        stored_total += stored
        changed_rows += 1
        result.at[index, "Цена-сайт-телефон (json)"] = json.dumps(_offer_bundle(offers), ensure_ascii=False)
        result.at[index, "Источников из локального индекса"] = sum(1 for offer in offers if offer.index_hit)
        for item_number, offer in enumerate(offers[:5], start=1):
            result.at[index, f"Вес источника {item_number}"] = round(float(offer.source_weight or 0), 3)
            result.at[index, f"Аудиторская запись {item_number}"] = offer.audit_record_path
    return result, stored_total


def probe_agent_market_start_urls(
    tender_id: str,
    position_payload: dict[str, object],
    *,
    max_sources: int = 1,
) -> dict[str, object]:
    """Strictly extract a price from a preselected direct supplier page.

    This is a bounded fallback for a browser-agent failure, not web discovery:
    URLs must already be present in the trusted job payload and the normal page
    adapter must confirm identity, price and unit.
    """

    tid = str(tender_id or "").strip()
    name = str(position_payload.get("name") or "").strip()
    urls = [
        str(url or "").strip()
        for url in list(position_payload.get("start_urls") or [])
        if is_direct_source_url(url)
    ][: max(1, min(3, int(max_sources or 1)))]
    if not tid or not name or not urls:
        return {"schema_version": 2, "position_key": str(position_payload.get("position_key") or ""), "offers": [], "notes": "Нет прямых источников для строгой проверки"}
    estimate_path = estimate_path_for_tender(tid)
    if not estimate_path.is_file():
        return {"schema_version": 2, "position_key": str(position_payload.get("position_key") or ""), "offers": [], "notes": "Нет сметы для проверки прямых источников"}
    estimate = pd.read_excel(estimate_path)
    matches = estimate[estimate[COL_NAME].fillna("").astype(str).map(_norm_key) == _norm_key(name)]
    if matches.empty:
        return {"schema_version": 2, "position_key": str(position_payload.get("position_key") or ""), "offers": [], "notes": "Позиция больше не найдена в смете"}
    source_row = matches.iloc[0]
    plan = build_search_plan(
        source_row.get(COL_NAME, ""),
        source_row.get("Ед. изм.", ""),
        source_row.get("basis_code", ""),
        source_row.get("Раздел", ""),
        str(position_payload.get("region") or ""),
    )
    inspection_name = market_query_name(source_row.get(COL_NAME, ""), plan.position.slug)
    offers: list[dict[str, object]] = []
    failures: list[str] = []
    for url in urls:
        page_html, source_error, _ = _fetch_source_page(url, timeout=10, browser_fetcher=None)
        if not page_html:
            failures.append(f"{urlparse(url).netloc}: {source_error or 'страница не открылась'}")
            continue
        inspection = inspect_source_page(
            page_html,
            url,
            name=inspection_name,
            target_unit=str(source_row.get("Ед. изм.", "") or ""),
            position_bucket=plan.position.bucket,
        )
        if not inspection.accepted or not inspection.price:
            failures.append(f"{urlparse(url).netloc}: {inspection.reason or 'цена не подтверждена'}")
            continue
        offers.append(
            {
                "title": inspection.title or inspection_name,
                "price": float(inspection.price),
                "currency": "RUB",
                "unit": inspection.unit,
                "url": url,
                "evidence": inspection.evidence,
                "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "confidence": 0.82,
                "price_scope": inspection.price_scope,
            }
        )
        break
    return {
        "schema_version": 2,
        "position_key": str(position_payload.get("position_key") or ""),
        "offers": offers,
        "notes": "AutoBot проверил заранее выбранный прямой источник после неудачи Hermes"
        + (f"; {'; '.join(failures[:3])}" if failures else ""),
        "_autobot_direct_probe": True,
    }


def import_agent_market_result(
    tender_id: str,
    position_payload: dict[str, object],
    result: dict[str, object],
) -> dict[str, object]:
    """Import agent evidence and independently verify every direct page on AutoBot."""

    tid = str(tender_id or "").strip()
    name = str(position_payload.get("name") or "").strip()
    if not tid or not name:
        raise ValueError("Не указан тендер или позиция")
    estimate_path = estimate_path_for_tender(tid)
    if not estimate_path.is_file():
        raise FileNotFoundError(f"Нет {estimate_path.name}")
    estimate = pd.read_excel(estimate_path)
    if COL_NAME not in estimate.columns:
        raise ValueError(f"В смете нет колонки {COL_NAME!r}")
    key = _norm_key(name)
    matches = estimate[estimate[COL_NAME].fillna("").astype(str).map(_norm_key) == key]
    if matches.empty:
        raise ValueError("Позиция агента больше не найдена в смете")
    source_row = matches.iloc[0]
    metadata = load_tender_metadata().get(tid, {})
    plan = build_search_plan(
        source_row.get(COL_NAME, ""),
        source_row.get("Ед. изм.", ""),
        source_row.get("basis_code", ""),
        source_row.get("Раздел", ""),
        str(metadata.get("region") or ""),
    )
    search_mode = str(position_payload.get("search_mode") or "").strip().casefold()
    avito_agent_mode = search_mode == "avito_agent"
    direct_probe_mode = bool(result.get("_autobot_direct_probe"))
    imported: list[MarketOffer] = []
    for item in list(result.get("offers") or [])[:10]:
        if not isinstance(item, dict):
            continue
        raw_price = float(item.get("price") or 0)
        raw_unit = str(item.get("unit") or "").strip()
        unit_multiplier = _agent_unit_multiplier(raw_unit)
        price = raw_price / unit_multiplier
        url = str(item.get("url") or "").strip()
        if price <= 0 or not url:
            continue
        host = urlparse(url).netloc.casefold().removeprefix("www.")
        parsed_url = urlparse(url)
        evidence = str(item.get("evidence") or item.get("snippet") or "").strip()[:1600]
        if avito_agent_mode:
            is_avito_host = host == "avito.ru" or host.endswith(".avito.ru")
            is_direct_listing = bool(re.search(r"_\d{6,}(?:/)?$", parsed_url.path or ""))
            if not is_avito_host or not is_direct_listing or len(evidence) < 12:
                continue
        elif host == "avito.ru" or host.endswith(".avito.ru"):
            # Ordinary Hermes jobs must not accidentally smuggle Avito results
            # into the dedicated browser-session workflow.
            continue
        observed_at = str(item.get("observed_at") or result.get("observed_at") or "").strip()
        if not observed_at:
            observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        plausibility = assess_price_plausibility(
            estimate_price=source_row.get(COL_UNIT_PRICE, ""),
            market_price=price,
            name=source_row.get(COL_NAME, ""),
            unit=source_row.get("Ед. изм.", ""),
            quantity=source_row.get(COL_QTY, ""),
            total=source_row.get(COL_SUM, ""),
        )
        reason = (
            "Прямое объявление открыто Hermes в браузерной сессии Mac mini; AutoBot проверяет соответствие позиции и единицы"
            if avito_agent_mode
            else "AutoBot открыл заранее выбранный прямой источник и извлёк цену со страницы"
            if direct_probe_mode
            else "Получено браузерным агентом; AutoBot ещё не подтвердил страницу и соответствие позиции"
        )
        if plausibility.status in {"review", "extreme"}:
            reason += f"; {plausibility.reason}"
        imported.append(
            MarketOffer(
                source=("Hermes · Авито" if avito_agent_mode else f"AutoBot · {host or 'прямой источник'}" if direct_probe_mode else f"Hermes Agent · {host or 'веб'}"),
                title=str(item.get("title") or name).strip()[:500],
                price=price,
                url=url,
                snippet=evidence[:500],
                verification="candidate",
                confidence=max(0.05, min(0.58, float(item.get("confidence") or 0.45))),
                verification_reason=reason,
                matched_unit=str(item.get("unit") or "").strip(),
                observed_at=observed_at,
                position_type=plan.position.slug,
                page_checked=avito_agent_mode,
                adapter="hermes-avito-agent" if avito_agent_mode else "autobot-direct-source" if direct_probe_mode else "hermes-browser-agent",
                price_scope=str(item.get("price_scope") or "").strip(),
                evidence=evidence,
                published_at=str(item.get("published_at") or "").strip(),
                location=str(item.get("location") or "").strip(),
                estimate_ratio=plausibility.ratio,
                plausibility=plausibility.status,
                identity_verified=False,
                source_weight=source_quality(url, host),
                discovery_engine="Hermes Avito browser" if avito_agent_mode else "AutoBot direct source" if direct_probe_mode else "Hermes browser",
                discovery_score=max(0.0, min(1.0, float(item.get("confidence") or 0.45))),
                discovery_reason=(
                    "Прямое объявление открыто постоянной браузерной сессией Mac mini"
                    if avito_agent_mode
                    else "Результат фонового браузерного агента"
                ),
                rejection_code="" if avito_agent_mode else "agent_unverified",
                rejection_stage="agent_import",
                extractor="browser-agent",
                agent_price=raw_price,
                agent_unit=raw_unit,
                agent_evidence=evidence,
            )
        )
    if not imported:
        return {"imported": 0, "message": "Агент не вернул пригодных цен"}

    output_path = output_path_for_tender(tid)
    previous = _read_previous(output_path)
    saved = _saved_offers_for_key(previous, key)
    imported = _verify_offers(
        source_row,
        imported,
        plan,
        reference_offers=saved,
    )
    stored_verified = _store_verified_offers_in_index(tid, source_row, imported)
    offers = _dedupe_and_sort(saved + imported, max_results=12)
    query = str(position_payload.get("query") or "").strip()
    if not query:
        queries = position_payload.get("queries") or plan.queries
        query = str(next(iter(queries), "") if queries else market_query_name(name))
    row = _build_output_row(source_row, offers=offers, query=query, err="", plan=plan)
    merged = _merge_rows(previous, [row])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.stem}.{uuid.uuid4().hex}.tmp.xlsx")
    try:
        merged.to_excel(temp_path, index=False)
        temp_path.replace(output_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    offer_outcomes = [
        {
            "title": offer.title,
            "price": round(float(offer.price), 2),
            "raw_price": round(float(offer.agent_price or offer.price), 2),
            "matched_unit": offer.matched_unit,
            "raw_unit": offer.agent_unit,
            "url": offer.url,
            "verification": offer.verification,
            "verification_reason": offer.verification_reason,
            "page_checked": bool(offer.page_checked),
            "plausibility": offer.plausibility,
        }
        for offer in imported
    ]
    return {
        "imported": len(imported),
        "verified": sum(1 for offer in imported if offer.verification == "verified"),
        "total_candidates": sum(1 for offer in offers if offer.verification == "candidate"),
        "preserved_verified": sum(1 for offer in offers if offer.verification == "verified"),
        "indexed": stored_verified,
        "report": str(output_path),
        "offer_outcomes": offer_outcomes,
    }


def run_tender(
    tender_id: str,
    *,
    max_rows: int | None = None,
    max_results: int = 5,
    pause: float = 4.0,
    sources: list[str] | None = None,
    no_resume: bool = False,
    rerun_selected: bool = False,
    only_without_verified: bool = False,
    avito_collect_only: bool = False,
    dry_run: bool = False,
) -> Path | None:
    tid = str(tender_id or "").strip()
    if not tid:
        return None
    est_path = estimate_path_for_tender(tid)
    if not est_path.is_file():
        raise FileNotFoundError(f"Нет {est_path.name}")
    out_path = output_path_for_estimate(est_path)
    est = pd.read_excel(est_path)
    if COL_NAME not in est.columns:
        raise ValueError(f"В смете нет колонки {COL_NAME!r}")

    md = load_tender_metadata().get(tid, {})
    region = str(md.get("region") or "").strip()
    sources = sources or ["web", "avito"]
    prev = pd.DataFrame() if no_resume else _read_previous(out_path)
    prev, revalidated_rows = _revalidate_previous(prev)
    if revalidated_rows:
        prev.to_excel(out_path, index=False)
        print(f"Перепроверен масштаб сохранённых цен: {revalidated_rows} строк", flush=True)
    index_backfill_enabled = (os.environ.get("MARKET_INDEX_BACKFILL", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
    if index_backfill_enabled and not dry_run and not prev.empty:
        prev, backfilled_offers = _backfill_price_index_from_report(tid, prev)
        if backfilled_offers:
            prev.to_excel(out_path, index=False)
            print(f"Локальный индекс: перенесено проверенных источников из отчёта — {backfilled_offers}", flush=True)
    eligible = _eligible_rows(est)
    if only_without_verified:
        verified_keys = _verified_market_keys(prev)
        filtered: list[tuple[int, pd.Series]] = []
        seen_keys: set[str] = set()
        for row_index, row in eligible:
            key = _norm_key(str(row.get(COL_NAME, "") or ""))
            if not key or key in verified_keys or key in seen_keys:
                continue
            plan = build_search_plan(
                row.get(COL_NAME, ""),
                row.get("Ед. изм.", ""),
                row.get("basis_code", ""),
                row.get("Раздел", ""),
                region,
            )
            if not plan.can_auto_price:
                continue
            seen_keys.add(key)
            filtered.append((row_index, row))
        eligible = filtered
    if max_rows and max_rows > 0:
        eligible = eligible[:max_rows]
    total = len(eligible)
    done_keys = set() if no_resume or rerun_selected else _processed_keys(prev)
    new_rows: list[dict] = []
    run_started = time.monotonic()
    health = {"processed": 0, "with_offers": 0, "verified": 0, "candidates": 0, "errors": 0}
    indexed_reused = 0
    indexed_stored = 0

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Рынок: tender={tid}, строк={total}, источники={','.join(sources)}, регион={region or '-'}", flush=True)
    use_browser = (os.environ.get("MARKET_AVITO_BROWSER", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
    browser_headless = (os.environ.get("MARKET_AVITO_HEADLESS", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
    with AvitoBrowserFetcher(enabled=use_browser and not dry_run, headless=browser_headless) as browser:
        for seq, (_, row) in enumerate(eligible, start=1):
            work_name = str(row.get(COL_NAME, "") or "").strip()
            key = _norm_key(work_name)
            if key in done_keys:
                continue
            plan = build_search_plan(
                work_name,
                row.get("Ед. изм.", ""),
                row.get("basis_code", ""),
                row.get("Раздел", ""),
                region,
            )
            query = " | ".join(plan.queries)
            append_market_web_event(tid, "begin", seq, total, work_name=work_name)
            if dry_run:
                offers = []
                err = "dry-run"
            elif not plan.can_auto_price:
                offers = []
                err = ""
            else:
                offers = [] if avito_collect_only else _offers_from_local_index(row, max_results=max_results)
                indexed_reused += sum(1 for offer in offers if offer.index_hit)
                errors: list[str] = []
                primary_sources = [source for source in sources if source != "avito"] or sources
                query_limit = 1 if avito_collect_only and sources == ["avito"] else 2
                verified_from_index = sum(1 for offer in offers if offer.verification == "verified")
                try:
                    minimum_index_sources = max(1, int(os.environ.get("MARKET_INDEX_MIN_SOURCES", "3") or 3))
                except ValueError:
                    minimum_index_sources = 3
                index_target = min(max_results, minimum_index_sources)
                queries_to_run = plan.queries[:query_limit] if verified_from_index < index_target else ()
                for planned_query in queries_to_run:
                    found, found_err = search_market(
                        planned_query,
                        region="",
                        sources=primary_sources,
                        max_results=max_results,
                        browser_fetcher=browser,
                    )
                    checked = _verify_offers(
                        row,
                        found,
                        plan,
                        browser_fetcher=browser,
                        avito_collect_only=avito_collect_only,
                        reference_offers=offers,
                    )
                    offers = _apply_market_consensus_guard(
                        _dedupe_and_sort(offers + checked, max_results=max_results)
                    )
                    if found_err:
                        errors.append(found_err)
                    if sum(1 for offer in offers if offer.verification == "verified") >= max_results:
                        break
                if "avito" in sources and "web" in sources and not any(
                    offer.verification == "verified" for offer in offers
                ) and plan.queries:
                    found, found_err = search_market(
                        plan.queries[0],
                        region="",
                        sources=["avito"],
                        max_results=max_results,
                        browser_fetcher=browser,
                    )
                    offers = _dedupe_and_sort(
                        offers + _verify_offers(
                            row,
                            found,
                            plan,
                            browser_fetcher=browser,
                            avito_collect_only=avito_collect_only,
                            reference_offers=offers,
                        ),
                        max_results=max_results,
                    )
                    offers = _apply_market_consensus_guard(offers)
                    if found_err:
                        errors.append(found_err)
                offers = _apply_market_consensus_guard(
                    _dedupe_and_sort(offers, max_results=max_results)
                )
                err = "; ".join(dict.fromkeys(errors))
            if avito_collect_only and not offers and _avito_safe_error_is_fatal(err):
                detail = f"бережный сбор остановлен без изменения этой позиции: {_friendly_market_error(err)}"
                append_market_web_event(tid, "done", seq, total, work_name=work_name, detail=detail)
                print(f"{seq}/{total}: {work_name[:90]} -> {detail}", flush=True)
                break
            if avito_collect_only:
                previous_non_avito = [
                    offer
                    for offer in _saved_offers_for_key(prev, key)
                    if "avito.ru" not in urlparse(offer.url or "").netloc.casefold()
                    and str(offer.source or "").strip().casefold() != "авито"
                ]
                offers = _dedupe_and_sort(
                    previous_non_avito + offers,
                    max_results=min(10, max_results + len(previous_non_avito)),
                )
            if not dry_run:
                indexed_stored += _store_verified_offers_in_index(tid, row, offers)
            new_rows.append(_build_output_row(row, offers=offers, query=query, err=err, plan=plan))
            merged = _merge_rows(prev, new_rows)
            merged.to_excel(out_path, index=False)
            verified_count = sum(1 for offer in offers if offer.verification == "verified")
            candidate_count = sum(1 for offer in offers if offer.verification == "candidate")
            health["processed"] += 1
            health["with_offers"] += int(bool(offers))
            health["verified"] += int(verified_count > 0)
            health["candidates"] += int(candidate_count > 0 and verified_count == 0)
            health["errors"] += int(bool(err))
            if not plan.can_auto_price:
                detail = plan.warning or "нужна детализация"
            elif verified_count:
                detail = f"{verified_count} подтверждено, {candidate_count} кандид."
            elif candidate_count:
                detail = f"{candidate_count} кандид., цена не принята"
            else:
                detail = "ничего не найдено"
            if err and not offers:
                detail = f"{detail} ({_friendly_market_error(err)})"
            append_market_web_event(tid, "done", seq, total, work_name=work_name, detail=detail)
            print(f"{seq}/{total}: {work_name[:90]} -> {detail}", flush=True)
            if pause > 0 and seq < total and not dry_run:
                time.sleep(pause)

    parser_health = None
    if not dry_run and health["processed"]:
        parser_health = record_parser_run(
            tender_id=tid,
            sources=sources,
            total_rows=total,
            processed_rows=health["processed"],
            rows_with_offers=health["with_offers"],
            verified_rows=health["verified"],
            candidate_rows=health["candidates"],
            error_rows=health["errors"],
            duration_sec=time.monotonic() - run_started,
        )
        index_detail = f"Локальный индекс: переиспользовано {indexed_reused}, сохранено/обновлено {indexed_stored}."
        print(index_detail, flush=True)
        append_market_web_event(tid, "index", total, total, detail=index_detail)
        if parser_health.get("degraded"):
            current_pct = round(float(parser_health.get("offer_rate") or 0) * 100)
            baseline_pct = round(float(parser_health.get("baseline_rate") or 0) * 100)
            warning = f"Внимание: успешность источников упала с {baseline_pct}% до {current_pct}%. Возможно, изменилась вёрстка или действует ограничение."
            print(warning, flush=True)
            append_market_web_event(tid, "warning", total, total, detail=warning)

    if not new_rows and not prev.empty:
        return out_path
    if new_rows:
        _merge_rows(prev, new_rows).to_excel(out_path, index=False)
    elif not out_path.is_file():
        pd.DataFrame().to_excel(out_path, index=False)
    return out_path


def _export_probe_offers(query: str, offers: list[MarketOffer]) -> tuple[Path, Path]:
    """Сохранить диагностический срез без изменения основного отчёта тендера."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"AVITO_ПОИСК_{_safe_name(query) or 'query'}_{stamp}"
    csv_path = REPORTS_DIR / f"{stem}.csv"
    xlsx_path = REPORTS_DIR / f"{stem}.xlsx"
    columns = ["Название", "Цена, руб", "Ссылка", "Дата публикации", "Локация", "Текст карточки"]
    rows = [
        {
            "Название": offer.title,
            "Цена, руб": float(offer.price),
            "Ссылка": offer.url,
            "Дата публикации": offer.published_at,
            "Локация": offer.location,
            "Текст карточки": offer.snippet,
        }
        for offer in offers
    ]
    frame = pd.DataFrame(rows, columns=columns)
    frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
    frame.to_excel(xlsx_path, index=False)
    _append_avito_log(
        "export",
        query=query,
        offers=len(offers),
        csv=str(csv_path),
        xlsx=str(xlsx_path),
    )
    return csv_path, xlsx_path


def probe_market(
    query: str,
    *,
    sources: list[str],
    max_results: int = 5,
    region: str = "",
) -> int:
    """Диагностика доступа без чтения сметы; результаты Авито пишутся в CSV/XLSX."""
    use_browser = (os.environ.get("MARKET_AVITO_BROWSER", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
    browser_headless = (os.environ.get("MARKET_AVITO_HEADLESS", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
    print(
        "Проверка рынка: "
        f"query={query!r}, sources={','.join(sources)}, "
        f"browser={'on' if use_browser else 'off'}, "
        f"headless={'yes' if browser_headless else 'no'}, "
        f"proxy={'yes' if (os.environ.get('MARKET_PROXY') or '').strip() else 'no'}, "
        f"profile={os.environ.get('MARKET_AVITO_USER_DATA_DIR') or str(REPO_ROOT / 'data' / 'avito_profile')}",
        flush=True,
    )
    with AvitoBrowserFetcher(enabled=use_browser, headless=browser_headless) as browser:
        offers, err = search_market(
            query,
            region=region,
            sources=sources,
            max_results=max_results,
            browser_fetcher=browser,
        )
        scroll_counts = list(browser.last_scroll_counts)
        skipped_cards = int(browser.last_skipped_cards or 0)
    if err:
        print(f"Статус/ошибки: {err}", flush=True)
    if scroll_counts:
        print(f"Карточек по шагам: {' → '.join(str(value) for value in scroll_counts)}", flush=True)
    if skipped_cards:
        print(f"Пропущено карточек без цены/ссылки: {skipped_cards}", flush=True)
    print(f"Найдено: {len(offers)}", flush=True)
    for i, offer in enumerate(offers, start=1):
        details = " · ".join(value for value in (offer.published_at, offer.location) if value)
        suffix = f" · {details}" if details else ""
        print(f"{i}. [{offer.source}] {offer.price:,.0f} ₽ · {offer.title}{suffix} · {offer.url}".replace(",", " "), flush=True)
    if "avito" in sources:
        avito_offers = [offer for offer in offers if str(offer.source or "").startswith("Авито")]
        csv_path, xlsx_path = _export_probe_offers(query, avito_offers)
        print(f"CSV: {csv_path}", flush=True)
        print(f"Excel: {xlsx_path}", flush=True)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Поиск реальных рыночных источников: Авито/интернет")
    ap.add_argument("--tender-id", help="Номер тендера")
    ap.add_argument("--probe", help="Только проверить поиск по запросу, без записи Excel")
    ap.add_argument("--max-rows", type=int, default=0, help="Ограничить число строк сметы")
    ap.add_argument("--max-results-per-row", type=int, default=int(os.environ.get("MARKET_MAX_RESULTS", "5") or "5"))
    ap.add_argument("--pause", type=float, default=float(os.environ.get("MARKET_PAUSE_SEC", "4") or "4"))
    ap.add_argument(
        "--sources",
        default=os.environ.get("MARKET_SOURCES", "web,avito"),
        help="Источники через запятую: web,avito. По умолчанию web,avito.",
    )
    ap.add_argument("--no-resume", action="store_true", help="Игнорировать сохранённый РЫНОК_ИСТОЧНИКИ_*.xlsx")
    ap.add_argument(
        "--rerun-selected",
        action="store_true",
        help="Перепроверить выбранный --max-rows с сохранением остальных строк отчёта",
    )
    ap.add_argument(
        "--only-without-verified",
        action="store_true",
        help="Брать только уникальные позиции без подтверждённой цены",
    )
    ap.add_argument(
        "--avito-collect-only",
        action="store_true",
        help="Бережно собрать карточки выдачи Авито без открытия каждого объявления",
    )
    ap.add_argument(
        "--avito-safe-interval-sec",
        type=float,
        default=0,
        help="Минимальный интервал между страницами Авито; в бережном режиме не менее 90 секунд",
    )
    ap.add_argument("--dry-run", action="store_true", help="Без сетевых запросов: проверить чтение/запись и прогресс")
    ap.add_argument("--headed", action="store_true", help="Открыть видимый браузер Авито для ручной проверки")
    ap.add_argument("--headless", action="store_true", help="Принудительно скрытый браузер Авито")
    ap.add_argument("--manual-wait-sec", type=int, default=None, help="Сколько ждать ручную проверку Авито в headed-режиме")
    ap.add_argument("--proxy", default="", help="Прокси только для рынка/Авито, например http://user:pass@host:port")
    ap.add_argument("--user-data-dir", default="", help="Папка профиля браузера Авито с cookies")
    ap.add_argument(
        "--avito-manually-unblocked",
        action="store_true",
        help="Снять локальную паузу AutoBot после того, как пользователь вручную прошёл проверку Авито",
    )
    args = ap.parse_args()

    if args.proxy.strip():
        os.environ["MARKET_PROXY"] = args.proxy.strip()
    if args.user_data_dir.strip():
        os.environ["MARKET_AVITO_USER_DATA_DIR"] = args.user_data_dir.strip()
    if args.headed and args.headless:
        raise SystemExit("Нельзя одновременно --headed и --headless")
    if args.headed:
        os.environ["MARKET_AVITO_HEADLESS"] = "0"
        if args.manual_wait_sec is None:
            os.environ["MARKET_AVITO_MANUAL_WAIT_SEC"] = "180"
    elif args.headless:
        os.environ["MARKET_AVITO_HEADLESS"] = "1"
    if args.manual_wait_sec is not None:
        os.environ["MARKET_AVITO_MANUAL_WAIT_SEC"] = str(max(0, int(args.manual_wait_sec)))
    if args.avito_manually_unblocked:
        _clear_avito_guard_after_manual_check()

    sources = [x.strip().lower() for x in str(args.sources or "").split(",") if x.strip()]
    if not sources:
        sources = ["web", "avito"]
    bad = [x for x in sources if x not in ("avito", "web")]
    if bad:
        raise SystemExit(f"Неизвестные источники: {', '.join(bad)}")
    if args.avito_collect_only:
        if sources != ["avito"]:
            raise SystemExit("--avito-collect-only требует --sources avito")
        safe_interval = max(90.0, float(args.avito_safe_interval_sec or 90.0))
        os.environ["MARKET_AVITO_MIN_INTERVAL_SEC"] = str(safe_interval)
        os.environ["MARKET_AVITO_FORCE_REFRESH"] = "1"
        os.environ["MARKET_AVITO_MAX_SCROLLS"] = "2"
        os.environ["MARKET_AVITO_SCROLL_TARGET_CARDS"] = "25"
        os.environ["MARKET_AVITO_SCROLL_DELAY_MIN_SEC"] = "5"
        os.environ["MARKET_AVITO_SCROLL_DELAY_MAX_SEC"] = "9"
        requested_rows = max(1, min(2, int(args.max_rows or 2)))
        os.environ["MARKET_AVITO_MAX_REQUESTS_PER_RUN"] = str(requested_rows)
        print(
            f"Авито · бережный сбор: до {requested_rows} страниц выдачи, "
            f"интервал не менее {safe_interval:g} сек, объявления не открываем",
            flush=True,
        )

    if args.probe:
        raise SystemExit(
            probe_market(
                args.probe,
                sources=sources,
                max_results=max(1, min(10, int(args.max_results_per_row or 5))),
            )
        )
    if not args.tender_id:
        raise SystemExit("Нужен --tender-id или --probe")

    p = run_tender(
        args.tender_id,
        max_rows=args.max_rows if args.max_rows > 0 else None,
        max_results=max(1, min(10, int(args.max_results_per_row or 5))),
        pause=max(0.0, float(args.pause or 0)),
        sources=sources,
        no_resume=bool(args.no_resume),
        rerun_selected=bool(args.rerun_selected),
        only_without_verified=bool(args.only_without_verified),
        avito_collect_only=bool(args.avito_collect_only),
        dry_run=bool(args.dry_run),
    )
    if not p:
        raise SystemExit("Не удалось собрать рыночные источники")
    print(p)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Остановлено пользователем", file=sys.stderr)
        raise SystemExit(130)
