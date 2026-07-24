"""
Реальный поиск рыночных источников по строкам сметы.

В отличие от старого real_market_scraper.py этот модуль не просит модель
придумать/свести цены. Он сохраняет только то, что смог вытащить из страниц:
цена, название объявления/страницы и ссылка.

Основной источник сейчас — Авито. Результат пишется построчно в:
  data/reports/РЫНОК_ИСТОЧНИКИ_ОТЧЕТ_ПО_СМЕТАМ_<tender_id>.xlsx

Формат совместим со старой сводкой через колонку «Цена-сайт-телефон (json)».
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urljoin, urlparse

import pandas as pd
import requests

from autobot.market_analytics import COL_DUP, COL_NAME
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
    "Chrome/126.0.0.0 Safari/537.36"
)


@dataclass
class MarketOffer:
    source: str
    title: str
    price: float
    url: str
    snippet: str = ""


class AvitoBrowserFetcher:
    """Ленивая Playwright-сессия: один браузер на весь прогон."""

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

    def __enter__(self) -> "AvitoBrowserFetcher":
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

    def fetch(self, url: str) -> str:
        if not self.enabled:
            return ""
        try:
            if self._page is None:
                from playwright.sync_api import sync_playwright

                self._playwright = sync_playwright().start()
                launch_kwargs = {"headless": self.headless}
                if self.proxy:
                    launch_kwargs["proxy"] = {"server": self.proxy}
                context_kwargs = {
                    "user_agent": os.environ.get("MARKET_USER_AGENT", DEFAULT_USER_AGENT),
                    "locale": "ru-RU",
                    "viewport": {"width": 1366, "height": 900},
                }
                if self.user_data_dir:
                    self._context = self._playwright.chromium.launch_persistent_context(
                        self.user_data_dir,
                        **launch_kwargs,
                        **context_kwargs,
                    )
                    self._page = self._context.new_page()
                else:
                    self._browser = self._playwright.chromium.launch(**launch_kwargs)
                    self._context = self._browser.new_context(**context_kwargs)
                    self._page = self._context.new_page()
            self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            self._page.wait_for_timeout(2200)
            try:
                self._page.mouse.wheel(0, 900)
                self._page.wait_for_timeout(600)
            except Exception:
                pass
            content = self._page.content()
            if (not self.headless) and self.manual_wait_sec > 0 and _avito_block_reason(content):
                print(
                    "Авито просит проверку/ограничил доступ. Открыл видимый браузер: "
                    f"пройдите проверку вручную, жду до {self.manual_wait_sec} сек…",
                    flush=True,
                )
                deadline = time.time() + self.manual_wait_sec
                while time.time() < deadline:
                    self._page.wait_for_timeout(5000)
                    content = self._page.content()
                    if not _avito_block_reason(content):
                        print("Проверка Авито пройдена, cookies сохранены в профиль.", flush=True)
                        break
                return content
            return content
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return ""


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
    return r.text


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
        href = html.unescape(m.group("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        if "avito.ru" not in href and not href.startswith("/"):
            continue
        url = urljoin(base_url, href.split("?")[0])
        parsed = urlparse(url)
        if "avito.ru" not in parsed.netloc:
            continue
        # Объявления обычно заканчиваются числовым id. Служебные ссылки пропускаем.
        if not re.search(r"_\d{6,}(?:$|/)", parsed.path):
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


def _dedupe_and_sort(offers: list[MarketOffer], *, max_results: int) -> list[MarketOffer]:
    by_url: dict[str, MarketOffer] = {}
    for offer in offers:
        if not offer.url:
            continue
        prev = by_url.get(offer.url)
        if prev is None:
            by_url[offer.url] = offer
            continue
        if prev.price <= 0 < offer.price:
            by_url[offer.url] = offer
        elif offer.price > 0 and prev.price > 0 and offer.price < prev.price:
            by_url[offer.url] = offer
    return sorted(
        by_url.values(),
        key=lambda x: (0 if x.price > 0 else 1, x.price if x.price > 0 else 10**18, 0 if x.source == "Авито" else 1, x.title),
    )[:max_results]


def search_avito(
    query: str,
    *,
    region: str = "",
    max_results: int = 5,
    browser_fetcher: AvitoBrowserFetcher | None = None,
) -> tuple[list[MarketOffer], str]:
    q = query
    if region:
        q = f"{query} {region}"
    url = "https://www.avito.ru/all?" + urlencode({"q": q})
    err = ""
    try:
        page = _session_get(url)
        block = _avito_block_reason(page)
        if block:
            err = block
            raise RuntimeError(block)
        offers = _parse_avito_html(page, url, max_results=max_results)
        if offers:
            return offers, ""
        err = "Авито: страница получена, объявлений с ценой не найдено"
    except Exception as e:
        err = f"Авито requests: {type(e).__name__}: {e}"
    if browser_fetcher is not None and browser_fetcher.enabled:
        page = browser_fetcher.fetch(url)
        if page:
            block = _avito_block_reason(page)
            if block:
                err = block
                return [], err
            offers = _parse_avito_html(page, url, max_results=max_results)
            if offers:
                return offers, ""
            err = "Авито browser: объявлений с ценой не найдено"
        elif browser_fetcher.last_error:
            err = f"{err}; Авито browser: {browser_fetcher.last_error}"
    return [], err


def search_web(query: str, *, region: str = "", max_results: int = 3) -> list[MarketOffer]:
    q = f"{query} {region} цена руб"
    errors: list[str] = []
    ddg_url = "https://duckduckgo.com/html/?" + urlencode({"q": q})
    try:
        page = _session_get(ddg_url)
        offers = _parse_duckduckgo_html(page, max_results=max_results)
        if offers:
            return offers
    except Exception as e:
        errors.append(f"DDG: {type(e).__name__}: {e}")

    bing_url = "https://www.bing.com/search?" + urlencode({"q": q})
    try:
        page = _session_get(bing_url)
        offers = _parse_bing_html(page, max_results=max_results)
        if offers:
            return offers
    except Exception as e:
        errors.append(f"Bing: {type(e).__name__}: {e}")
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
    if "avito" in sources:
        avito_offers, avito_err = search_avito(
            query,
            region=region,
            max_results=max_results,
            browser_fetcher=browser_fetcher,
        )
        offers.extend(avito_offers)
        if avito_err:
            errors.append(avito_err)
    # Обычный веб используем как подстраховку, но не забиваем им таблицу:
    # самые выгодные реальные Авито-объявления всё равно сортировкой окажутся наверху.
    if "web" in sources:
        try:
            offers.extend(search_web(query, region=region, max_results=max(2, min(3, max_results))))
        except Exception as e:
            errors.append(f"Интернет: {type(e).__name__}: {e}")
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
        }
        for o in offers
    ]


def _build_output_row(src_row: pd.Series, *, offers: list[MarketOffer], query: str, err: str) -> dict:
    base = {str(k): src_row.get(k, "") for k in src_row.index if not str(k).startswith("_market")}
    prices = [float(o.price) for o in offers if o.price and o.price > 0]
    prices_s = "; ".join(f"{p:,.0f}".replace(",", " ") for p in prices)
    bundle = _offer_bundle(offers)
    sources_s = "; ".join(o.url for o in offers if o.url)
    titles_s = "\n".join(f"{i}. {o.title}" for i, o in enumerate(offers, start=1))
    if prices:
        status = "найдено объявлений: " + str(len(prices))
        med = statistics.median(prices)
        mn = min(prices)
        mx = max(prices)
    else:
        status = "обработано, объявлений не найдено"
        med = mn = mx = ""
    if err:
        status = (status + "; " if status else "") + err

    base.update(
        {
            "Рыночные источники": titles_s,
            "Рыночные источники (полный текст)": "\n".join(
                f"{i}. [{o.source}] {o.title}\nЦена: {f'{o.price:,.0f} ₽' if o.price and o.price > 0 else 'не распознана'}\n{o.url}"
                for i, o in enumerate(offers, start=1)
            ),
            "Цены за ед. (рынок, руб)": prices_s,
            "Медиана цена за ед. (рынок)": med,
            "Мин цена за ед. (рынок)": mn,
            "Макс цена за ед. (рынок)": mx,
            "Телефоны (строго)": "",
            "Ссылки (строго)": sources_s,
            "Цена-сайт-телефон (json)": json.dumps(bundle, ensure_ascii=False) if bundle else "",
            "Источники (ссылки/телефоны)": sources_s,
            "Ошибка / статус": status,
            "Поисковый запрос рынка": query,
        }
    )
    for i, offer in enumerate(offers[:5], start=1):
        base[f"Источник {i}"] = offer.source
        base[f"Название объявления {i}"] = offer.title
        base[f"Цена объявления {i}"] = offer.price
        base[f"Ссылка объявления {i}"] = offer.url
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


def _processed_keys(prev: pd.DataFrame) -> set[str]:
    if prev.empty or COL_NAME not in prev.columns:
        return set()
    return {_norm_key(str(x)) for x in prev[COL_NAME].fillna("").astype(str) if str(x).strip()}


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


def run_tender(
    tender_id: str,
    *,
    max_rows: int | None = None,
    max_results: int = 5,
    pause: float = 4.0,
    sources: list[str] | None = None,
    no_resume: bool = False,
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
    sources = sources or ["avito"]
    eligible = _eligible_rows(est)
    if max_rows and max_rows > 0:
        eligible = eligible[:max_rows]
    total = len(eligible)
    prev = pd.DataFrame() if no_resume else _read_previous(out_path)
    done_keys = set() if no_resume else _processed_keys(prev)
    new_rows: list[dict] = []

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
            query = _compact_query(work_name)
            append_market_web_event(tid, "begin", seq, total, work_name=work_name)
            if dry_run:
                offers = []
                err = "dry-run"
            else:
                offers, err = search_market(
                    query,
                    region=region,
                    sources=sources,
                    max_results=max_results,
                    browser_fetcher=browser,
                )
            new_rows.append(_build_output_row(row, offers=offers, query=query, err=err))
            merged = _merge_rows(prev, new_rows)
            merged.to_excel(out_path, index=False)
            detail = f"{len(offers)} объявл." if offers else "ничего не найдено"
            if err and not offers:
                detail = f"{detail} ({err[:160]})"
            append_market_web_event(tid, "done", seq, total, work_name=work_name, detail=detail)
            print(f"{seq}/{total}: {work_name[:90]} -> {detail}", flush=True)
            if pause > 0 and seq < total and not dry_run:
                time.sleep(pause)

    if not new_rows and not prev.empty:
        return out_path
    if new_rows:
        _merge_rows(prev, new_rows).to_excel(out_path, index=False)
    elif not out_path.is_file():
        pd.DataFrame().to_excel(out_path, index=False)
    return out_path


def probe_market(
    query: str,
    *,
    sources: list[str],
    max_results: int = 5,
    region: str = "",
) -> int:
    """Быстрая диагностика доступа: Авито/веб без чтения сметы и без записи Excel."""
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
    if err:
        print(f"Статус/ошибки: {err}", flush=True)
    print(f"Найдено: {len(offers)}", flush=True)
    for i, offer in enumerate(offers, start=1):
        print(f"{i}. [{offer.source}] {offer.price:,.0f} ₽ · {offer.title} · {offer.url}".replace(",", " "), flush=True)
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
        default=os.environ.get("MARKET_SOURCES", "avito,web"),
        help="Источники через запятую: avito,web. По умолчанию avito,web.",
    )
    ap.add_argument("--no-resume", action="store_true", help="Игнорировать сохранённый РЫНОК_ИСТОЧНИКИ_*.xlsx")
    ap.add_argument("--dry-run", action="store_true", help="Без сетевых запросов: проверить чтение/запись и прогресс")
    ap.add_argument("--headed", action="store_true", help="Открыть видимый браузер Авито для ручной проверки")
    ap.add_argument("--headless", action="store_true", help="Принудительно скрытый браузер Авито")
    ap.add_argument("--manual-wait-sec", type=int, default=None, help="Сколько ждать ручную проверку Авито в headed-режиме")
    ap.add_argument("--proxy", default="", help="Прокси только для рынка/Авито, например http://user:pass@host:port")
    ap.add_argument("--user-data-dir", default="", help="Папка профиля браузера Авито с cookies")
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

    sources = [x.strip().lower() for x in str(args.sources or "").split(",") if x.strip()]
    if not sources:
        sources = ["avito"]
    bad = [x for x in sources if x not in ("avito", "web")]
    if bad:
        raise SystemExit(f"Неизвестные источники: {', '.join(bad)}")

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
