from __future__ import annotations

from autobot.paths import REPO_ROOT
import argparse
import atexit
import html
import hashlib
import io
import json
import os
import math
import re
import shutil
import subprocess
import warnings
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
import sys
from typing import Iterable
from urllib.parse import parse_qs, urlencode, urlparse

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

import pandas as pd
import rarfile
import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from autobot.market_analytics import estimate_block_qty_from_unit, unit_has_area_or_volume_marker
from autobot.telegram_notify import send_message, telegram_config
from autobot.tender_notifications import safe_notify_new_tender


BASE_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)

REGIONS = [
    "Ставропольский край",
    "Челябинская область",
    "Ярославская область",
]
KEYWORDS = [
    "строительство",
    "благоустройство",
]

PRICE_MIN = 20_000_000
PRICE_MAX = 100_000_000
NEEDED_STAGE = "Подача заявок"

ARCHIVE_KEYWORDS = [
    "смет",
    "лср",
    "докум",
]
DOC_EXTENSIONS = (".zip", ".rar", ".7z", ".xlsx", ".xls", ".xlsm", ".pdf", ".doc", ".docx", ".rtf")
NOTICE_ROUTE_TYPES = (
    "ea20",
    "ea44",
    "zk20",
    "ok20",
    "po615",
    "priz",
    "ep44",
)

# Параметры этапов из URL расширенного поиска.
# Оставляем включенными, а итоговый этап дополнительно фильтруем ниже по карточке.
STAGE_QUERY_FLAGS = {
    "ca": "on",
    "pc": "on",
    "pa": "on",
}

WORK_COLUMN_HINTS = ("наименование", "работ", "услуг", "позиция")
PRICE_COLUMN_HINTS = ("цена", "стоимость", "сумма", "всего")
ESTIMATE_FILE_HINTS = ("лср",)
OBJECT_SMETA_NAME_HINTS = ("объектн", "объектная", "objectnaya")
PDF_LINE_PRICE_RE = re.compile(r"^(?P<name>.+?)\s+(?P<price>\d[\d\s\xa0]{2,}(?:[.,]\d{1,2})?)\s*(?:руб(?:\.|лей|ля|ль)?|₽)?$", re.IGNORECASE)


def should_skip_object_estimate_file(path: Path) -> bool:
    """Объектные сметы — другая структура и итоги; для сравнения с позициями ЛСР не смешиваем."""
    low = path.as_posix().lower()
    return any(h in low for h in OBJECT_SMETA_NAME_HINTS)


SKIP_ROW_HINTS = (
    "раздел",
    "итого",
    "всего",
    "в т.ч",
    "ндс",
    "локальная смета",
    "объектная смета",
    "составлен",
    "наименование объекта",
)
USE_FALLBACK_EXTRACTION = False

# НМЦК в закупке часто «с НДС», строки ЛСР в файлах — часто без НДС; плюс в смете бывают добавочные коэффициенты (зимние, индексные, районные и т.д.).
NDS_RATE = 0.22
NDS_MULTIPLIER = 1.0 + NDS_RATE
# Считать контроль «зелёным», если сумма отчёта × (1+НДС) близка к НМЦК (остаток — коэффициенты, неполный охват ЛСР).
NMCK_NEAR_VAT_MAX_REL_DIFF = 0.06


@dataclass
class Tender:
    tender_id: str
    title: str
    url: str
    region: str
    stage: str
    price_rub: float | None
    publish_date: str | None


def _truthy_env(name: str, default: str = "0") -> bool:
    v = (os.environ.get(name, default) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _eis_ignore_https_errors() -> bool:
    """
    Обход для случаев, когда VPN/антивирус подменяет TLS-сертификат ЕИС.
    Можно выключить через EIS_IGNORE_HTTPS_ERRORS=0.
    """
    return _truthy_env("EIS_IGNORE_HTTPS_ERRORS", "1")


def _new_eis_page(browser):
    context = browser.new_context(
        user_agent=USER_AGENT,
        ignore_https_errors=_eis_ignore_https_errors(),
    )
    return context.new_page()


def configure_rar_backend() -> bool:
    """
    Настраивает backend для распаковки RAR.
    Возвращает True, если backend доступен.
    """
    possible_tools = [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
        r"C:\Program Files\WinRAR\UnRAR.exe",
        r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
        "unrar",
        "7z",
    ]
    for tool in possible_tools:
        rarfile.UNRAR_TOOL = tool
        try:
            rarfile.tool_setup()
            return True
        except Exception:
            continue
    return False


def get_7z_path() -> str | None:
    """Путь к 7-Zip: сначала типичные каталоги Windows, затем 7z в PATH."""
    for tool in (
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
        Path.home() / r"AppData\Local\Programs\7-Zip\7z.exe",
    ):
        p = Path(tool)
        if p.is_file():
            return str(p)
    for name in ("7z", "7z.exe"):
        wh = shutil.which(name)
        if wh:
            return wh
    for tool in ("7z",):
        try:
            result = subprocess.run(
                [tool, "-h"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode in (0, 1):
                return tool
        except Exception:
            continue
    return None


def ensure_dirs() -> dict[str, Path]:
    root = Path("data")
    paths = {
        "root": root,
        "downloads": root / "downloads",
        "extracted": root / "extracted",
        "reports": root / "reports",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _resume_enabled() -> bool:
    return _truthy_env("SEARCH_RESUME", "1")


def _checkpoint_signature(args: argparse.Namespace) -> str:
    payload = {
        "max_pages": int(args.max_pages),
        "max_tenders": int(args.max_tenders),
        "days_back": int(args.days_back),
        "regions": REGIONS,
        "keywords": KEYWORDS,
        "price_min": PRICE_MIN,
        "price_max": PRICE_MAX,
        "needed_stage": NEEDED_STAGE,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _search_checkpoint_path(out_paths: dict[str, Path]) -> Path:
    return out_paths["root"] / "search_resume_checkpoint.json"


def _load_search_checkpoint(out_paths: dict[str, Path], args: argparse.Namespace) -> dict | None:
    if not _resume_enabled():
        return None
    path = _search_checkpoint_path(out_paths)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("signature") != _checkpoint_signature(args):
        return None
    if data.get("completed"):
        return None
    tenders = data.get("filtered_tenders")
    if not isinstance(tenders, list) or not tenders:
        return None
    return data


def _save_search_checkpoint(
    out_paths: dict[str, Path],
    args: argparse.Namespace,
    *,
    filtered: list[Tender],
    completed_ids: set[str],
    new_ids: set[str],
    search_total: int,
    completed: bool = False,
) -> None:
    if not _resume_enabled():
        return
    payload = {
        "signature": _checkpoint_signature(args),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "completed": bool(completed),
        "search_total": int(search_total),
        "filtered_tenders": [asdict(t) for t in filtered],
        "completed_ids": sorted({str(x).strip() for x in completed_ids if str(x).strip()}),
        "new_ids": sorted({str(x).strip() for x in new_ids if str(x).strip()}),
    }
    _search_checkpoint_path(out_paths).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _clear_search_checkpoint(out_paths: dict[str, Path]) -> None:
    path = _search_checkpoint_path(out_paths)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def get_tender_price_from_cache(tender_id: str, out_paths: dict[str, Path]) -> float | None:
    tenders_path = out_paths["root"] / "tenders.json"
    if not tenders_path.exists():
        return None
    try:
        data = json.loads(tenders_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    for row in data:
        if str(row.get("tender_id", "")).strip() == tender_id:
            return to_float(row.get("price_rub"))
    return None


def load_cached_tenders(out_paths: dict[str, Path]) -> list[dict]:
    tenders_path = out_paths["root"] / "tenders.json"
    if not tenders_path.exists():
        return []
    try:
        data = json.loads(tenders_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def merge_and_save_tenders(out_paths: dict[str, Path], tenders: list[Tender]) -> tuple[int, int]:
    cached = load_cached_tenders(out_paths)
    merged: dict[str, dict] = {}
    for row in cached:
        tid = str(row.get("tender_id", "")).strip()
        if tid:
            merged[tid] = row
    existed_before = set(merged.keys())

    for tender in tenders:
        incoming = asdict(tender)
        existing = merged.get(tender.tender_id)
        # Не откатываем уже уточнённый "закрывающий" этап обратно в "Подача заявок":
        # иначе в каждом прогоне будет повторное "изменение статуса".
        if existing:
            existing_stage = str(existing.get("stage") or "").strip()
            incoming_stage = str(incoming.get("stage") or "").strip()
            if (
                existing_stage
                and existing_stage != NEEDED_STAGE
                and (not incoming_stage or incoming_stage == NEEDED_STAGE)
            ):
                incoming["stage"] = existing_stage
        merged[tender.tender_id] = incoming

    total = len(merged)
    new_added = len([t for t in tenders if t.tender_id not in existed_before])
    (out_paths["root"] / "tenders.json").write_text(
        json.dumps(list(merged.values()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return new_added, total


def parse_price(text: str) -> float | None:
    if not text:
        return None
    normalized = text.replace("\xa0", " ").lower()
    values: list[float] = []

    # Ищем суммы, рядом с которыми явно есть "руб"
    for match in re.finditer(r"(\d[\d\s]{4,}(?:[,.]\d+)?)\s*(?:руб|₽)", normalized):
        raw = match.group(1).replace(" ", "").replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            continue
        # Отбрасываем явно маленькие/нереалистичные значения
        if value >= 1_000:
            values.append(value)

    if not values:
        return None
    # Обычно в карточке есть несколько сумм, берем самую крупную как НМЦК.
    return max(values)


def get_tender_id(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"\d{19}", text)
    if m:
        return m.group(0)
    m = re.search(r"\d{8,}", text)
    return m.group(0) if m else ""


def normalize_href(href: str) -> str:
    if href.startswith("http"):
        return href
    return f"https://zakupki.gov.ru{href}"


def _extract_reg_number_from_url(url: str) -> str:
    try:
        p = urlparse(url)
        q = parse_qs(p.query)
    except Exception:
        return ""
    reg = (q.get("regNumber", [""])[0] or "").strip()
    return reg if reg.isdigit() else ""


def _candidate_notice_urls(tender: Tender) -> list[str]:
    """
    Генерирует расширенный набор URL карточки/документов для разных типов закупок.
    Это повышает шанс найти файлы, если исходный тип пути в ссылке неправильный или устарел.
    """
    base = (tender.url or "").strip()
    out: list[str] = []
    if base:
        out.append(base)
        if "common-info.html" in base:
            out.append(base.replace("common-info.html", "documents.html"))
        if "documents.html" in base:
            out.append(base.replace("documents.html", "common-info.html"))

    reg = _extract_reg_number_from_url(base) or (tender.tender_id.strip() if tender.tender_id.isdigit() else "")
    if reg:
        for route in NOTICE_ROUTE_TYPES:
            root = f"https://zakupki.gov.ru/epz/order/notice/{route}/view"
            out.append(f"{root}/common-info.html?regNumber={reg}")
            out.append(f"{root}/documents.html?regNumber={reg}")

    uniq: list[str] = []
    seen: set[str] = set()
    for u in out:
        if not u or u in seen:
            continue
        seen.add(u)
        uniq.append(u)
    return uniq


def _goto_with_retries(page, url: str, *, timeout_ms: int = 60_000, retries: int = 3) -> tuple[bool, list[str]]:
    """Пробует открыть страницу несколько раз, включая fallback wait_until='commit'."""
    errs: list[str] = []
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1800)
            return True, errs
        except (PlaywrightTimeoutError, PlaywrightError) as e:
            errs.append(f"{url} [attempt {attempt}/{attempts}, domcontentloaded] -> {type(e).__name__}: {e}")
        try:
            page.goto(url, wait_until="commit", timeout=timeout_ms)
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
            page.wait_for_timeout(1800)
            return True, errs
        except (PlaywrightTimeoutError, PlaywrightError) as e:
            errs.append(f"{url} [attempt {attempt}/{attempts}, commit] -> {type(e).__name__}: {e}")
        if attempt < attempts:
            page.wait_for_timeout(1000 * attempt)
    return False, errs


def parse_publish_date(text: str) -> str | None:
    m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text or "")
    return m.group(1) if m else None


def is_recent(date_str: str | None, days_back: int) -> bool:
    if not date_str:
        return True
    try:
        date_obj = datetime.strptime(date_str, "%d.%m.%Y")
    except ValueError:
        return True
    return date_obj >= datetime.now() - timedelta(days=days_back)


def search_tenders(region: str, keyword: str, max_pages: int = 3) -> list[Tender]:
    results: list[Tender] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = _new_eis_page(browser)

        for page_no in range(1, max_pages + 1):
            params = {
                "searchString": f"{region} {keyword}",
                "morphology": "on",
                "search-filter": "Дате размещения",
                "pageNumber": page_no,
                "sortDirection": "false",
                "recordsPerPage": "_10",
                "showLotsInfoHidden": "false",
                "sortBy": "UPDATE_DATE",
                "fz44": "on",
                "fz223": "on",
                "af": "on",
                "priceFromGeneral": str(PRICE_MIN),
                "priceToGeneral": str(PRICE_MAX),
                "currencyIdGeneral": "-1",
            }
            params.update(STAGE_QUERY_FLAGS)
            url = f"{BASE_URL}?{urlencode(params)}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(1500)
            except (PlaywrightTimeoutError, PlaywrightError) as e:
                print(
                    f"Поиск: {region} / {keyword} стр.{page_no}: не открылась страница zakupki "
                    f"({type(e).__name__}: {str(e)[:200]}). Частая причина — VPN/блокировка ЕИС."
                )
                continue

            cards = page.locator("div.search-registry-entry-block, div.registry-entry__form")
            count = cards.count()
            if count == 0:
                print(
                    f"Поиск: {region} / {keyword} стр.{page_no}: страница открылась, но карточек тендеров на странице нет "
                    f"(селектор пустой — возможна смена вёрстки или пустая выдача)."
                )
                continue

            for i in range(count):
                card = cards.nth(i)
                text = card.inner_text(timeout=3000)

                links = card.locator("a")
                title = ""
                href = ""
                for j in range(min(links.count(), 5)):
                    t = links.nth(j).inner_text(timeout=2000).strip()
                    h = links.nth(j).get_attribute("href")
                    if t and h and "/epz/order/notice" in h:
                        title = t
                        href = h
                        break
                if not title and links.count() > 0:
                    title = links.first.inner_text(timeout=2000).strip()
                    href = links.first.get_attribute("href") or ""

                stage = parse_stage_from_card_text(text)

                price = parse_price(text)
                publish_date = parse_publish_date(text)
                tender_id = get_tender_id(text + " " + href)

                if not tender_id or not href:
                    continue

                results.append(
                    Tender(
                        tender_id=tender_id,
                        title=title or f"Тендер {tender_id}",
                        url=normalize_href(href),
                        region=region,
                        stage=stage,
                        price_rub=price,
                        publish_date=publish_date,
                    )
                )
        browser.close()
    return dedupe_tenders(results)


def dedupe_tenders(tenders: Iterable[Tender]) -> list[Tender]:
    uniq: dict[str, Tender] = {}
    for tender in tenders:
        if tender.tender_id not in uniq:
            uniq[tender.tender_id] = tender
    return list(uniq.values())


def parse_stage_from_card_text(text: str) -> str:
    """Этап закупки по тексту карточки в выдаче или на странице извещения (как в search_tenders)."""
    stage = ""
    stage_match = re.search(r"(?i)этап\s*[:\s]\s*([^\n\r]+)", text or "")
    if stage_match:
        stage = stage_match.group(1).strip()
    elif NEEDED_STAGE.lower() in (text or "").lower():
        stage = NEEDED_STAGE
    stage = stage.strip(" .:;,-").strip()
    low = stage.lower()
    # В карточках встречаются общие подписи типа "Этап закупки"/"закупки",
    # это не фактический статус и такие значения не нужно сохранять/рассылать.
    meaningless = {
        "",
        "закупки",
        "этап закупки",
        "этап",
        "статус",
        "статус закупки",
    }
    if low in meaningless:
        return ""
    return stage


def refresh_cached_open_tender_stages(out_paths: dict[str, Path]) -> list[tuple[str, str, str]]:
    """
    Для записей в tenders.json со статусом «Подача заявок» подтягивает актуальный этап с карточки ЕИС.
    Обновляет файл и возвращает список (tender_id, старый_этап, новый_этап) только при реальном изменении.
    """
    rows = load_cached_tenders(out_paths)
    if not rows:
        return []
    try:
        max_n = int((os.environ.get("REFRESH_CACHED_STAGES_MAX") or "40").strip() or "40")
    except ValueError:
        max_n = 40
    max_n = max(1, min(max_n, 80))

    candidates: list[tuple[str, str]] = []
    for row in rows:
        tid = str(row.get("tender_id", "") or "").strip()
        st = (str(row.get("stage") or "")).strip()
        if not tid or st != NEEDED_STAGE:
            continue
        url = (str(row.get("url") or "")).strip()
        if url and not url.startswith("http"):
            url = normalize_href(url)
        if not url.startswith("http"):
            continue
        candidates.append((tid, url))
    candidates = candidates[:max_n]
    if not candidates:
        return []

    changes: list[tuple[str, str, str]] = []
    merged: dict[str, dict] = {}
    for row in rows:
        tid = str(row.get("tender_id", "") or "").strip()
        if tid:
            merged[tid] = dict(row)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = _new_eis_page(browser)
        for tid, url in candidates:
            old_stage = (str(merged.get(tid, {}).get("stage") or "")).strip() or NEEDED_STAGE
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(1200)
                body = page.inner_text("body", timeout=15_000)
            except (PlaywrightTimeoutError, PlaywrightError) as e:
                print(f"Этап {tid}: не открылась карточка ({type(e).__name__}: {str(e)[:160]})")
                continue
            new_stage = parse_stage_from_card_text(body)
            if not new_stage or new_stage == old_stage:
                continue
            row = merged.get(tid)
            if not row:
                continue
            row["stage"] = new_stage[:500]
            changes.append((tid, old_stage, new_stage[:500]))
            print(f"Этап обновлён {tid}: «{old_stage}» → «{new_stage[:120]}»")

        browser.close()

    if changes:
        (out_paths["root"] / "tenders.json").write_text(
            json.dumps(list(merged.values()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return changes


def tender_matches_filters(tender: Tender, days_back: int) -> bool:
    # Раньше при пустом stage условие «if tender.stage and …» пропускало проверку этапа —
    # в выдачу попадали тендеры с любым этапом. Нужен непустой этап и явное «Подача заявок».
    st = (tender.stage or "").strip()
    if not st or NEEDED_STAGE.lower() not in st.lower():
        return False
    if tender.price_rub is not None and not (PRICE_MIN <= tender.price_rub <= PRICE_MAX):
        return False
    if not is_recent(tender.publish_date, days_back):
        return False
    return True


def collect_doc_links(page) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    links = page.locator("a")
    total = links.count()
    for idx in range(total):
        el = links.nth(idx)
        href = el.get_attribute("href") or ""
        try:
            text = (el.text_content(timeout=1500) or "").strip().lower()
        except PlaywrightError:
            text = ""
        if not href:
            continue
        full = normalize_href(href)
        low_url = full.lower()
        is_direct_download = "/download/" in low_url and "file.html" in low_url
        is_doc_ext = low_url.endswith(DOC_EXTENSIONS)
        is_doc_text = any(k in text for k in ARCHIVE_KEYWORDS) or any(x in text for x in ("pdf", "xlsx", "xls", "doc"))
        if is_doc_ext or is_doc_text or is_direct_download:
            candidates.append((text, full))
    # Иногда ссылка на архив есть только в сыром HTML.
    html = page.content().lower()
    ext_re = "|".join(x.strip(".") for x in DOC_EXTENSIONS)
    for match in re.finditer(rf'https?://[^"\'\s>]+?\.(?:{ext_re})(?:\?[^"\'\s>]*)?', html):
        candidates.append(("html-direct", match.group(0)))
    # Keep unique urls
    uniq = {}
    for text, url in candidates:
        uniq[url] = text
    return [(t, u) for u, t in uniq.items()]


def _extension_from_headers(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None
    decoded = requests.utils.unquote(content_disposition)
    match = re.search(r"filename\*?=(?:UTF-8''|\"?)([^\";]+)", decoded, flags=re.IGNORECASE)
    if not match:
        return None
    filename = match.group(1).strip().strip('"')
    ext = Path(filename).suffix.lower()
    return ext if ext else None


def _filename_from_headers(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None
    decoded = requests.utils.unquote(content_disposition)
    match = re.search(r"filename\*?=(?:UTF-8''|\"?)([^\";]+)", decoded, flags=re.IGNORECASE)
    if not match:
        return None
    name = Path(match.group(1).strip().strip('"')).name
    return name or None


def _guess_original_name(url: str, content_disposition: str | None, saved_path: Path) -> str:
    from_header = _filename_from_headers(content_disposition)
    if from_header:
        return from_header
    url_name = Path(urlparse(url).path).name
    if url_name:
        return requests.utils.unquote(url_name)
    return saved_path.name


def _sanitize_filename_for_windows(name: str, fallback: str = "document.bin") -> str:
    """Безопасное имя файла для Windows/NTFS."""
    cleaned = (name or "").strip().replace("\x00", "")
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    cleaned = re.sub(r'[<>:"|?*]+', "_", cleaned)
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        cleaned = fallback
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
    stem = Path(cleaned).stem or cleaned
    if stem.upper() in reserved:
        ext = Path(cleaned).suffix
        cleaned = f"_{stem}{ext}"
    return cleaned[:220]


def _choose_unique_path(target_dir: Path, desired_name: str) -> Path:
    """Выбирает уникальный путь, добавляя суффикс (2), (3), ..."""
    candidate = target_dir / desired_name
    if not candidate.exists():
        return candidate
    stem = Path(desired_name).stem
    ext = Path(desired_name).suffix
    for i in range(2, 10_000):
        alt = target_dir / f"{stem} ({i}){ext}"
        if not alt.exists():
            return alt
    return target_dir / f"{stem}_{int(datetime.now().timestamp())}{ext}"


def _doc_type_label(path_or_name: str) -> str:
    ext = Path(path_or_name).suffix.lower()
    if ext == ".pdf":
        return "PDF"
    if ext in (".doc", ".docx", ".rtf", ".odt"):
        return "Word"
    if ext in (".xls", ".xlsx", ".xlsm", ".ods"):
        return "Excel"
    if ext in (".zip", ".rar", ".7z"):
        return "Архивы"
    return "Прочее"


def _is_relevant_doc_name(name: str) -> bool:
    low = name.strip().lower()
    if not low:
        return False
    if re.match(r"^doc_\d+\.bin$", low):
        return False
    if low in {"download_log.json", "desktop.ini"}:
        return False
    ext = Path(low).suffix
    if ext in {".json", ".log", ".txt", ".tmp", ".part", ".crdownload", ".html", ".htm"}:
        return False
    return True


def _build_no_estimate_files_summary_lines(tender_dir: Path) -> dict[str, list[str]]:
    entries: list[tuple[str, str]] = []
    log_path = tender_dir / "download_log.json"
    if log_path.exists():
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for row in data:
                    if not isinstance(row, dict) or row.get("status") != "ok":
                        continue
                    original_name = str(row.get("original_name") or "").strip()
                    saved_path_raw = str(row.get("saved_path") or "").strip()
                    fallback = Path(saved_path_raw).name if saved_path_raw else ""
                    name = original_name or fallback
                    if name and _is_relevant_doc_name(name):
                        entries.append((_doc_type_label(name), name))
        except Exception:
            pass

    if not entries:
        for p in sorted([x for x in tender_dir.glob("*") if x.is_file() and x.name != "download_log.json"]):
            if _is_relevant_doc_name(p.name):
                entries.append((_doc_type_label(p.name), p.name))

    if not entries:
        return {}

    seen: set[tuple[str, str]] = set()
    grouped: dict[str, list[str]] = {"PDF": [], "Word": [], "Excel": [], "Архивы": [], "Прочее": []}
    for label, name in entries:
        key = (label, name)
        if key in seen:
            continue
        seen.add(key)
        grouped.setdefault(label, []).append(name)
    return grouped


def _print_no_estimate_files_summary(tender_id: str, tender_dir: Path) -> None:
    grouped = _build_no_estimate_files_summary_lines(tender_dir)
    if not grouped:
        return
    print(f"  -> {tender_id}: Сметы не найдены, вот какие файлы есть:")
    for label in ("PDF", "Word", "Excel", "Архивы", "Прочее"):
        names = grouped.get(label, [])
        if not names:
            continue
        print(f"     {label}:")
        for name in names:
            print(f"       - {name}")


def _send_no_estimate_files_summary_to_tg(
    token: str,
    chat_id: str,
    tender_id: str,
    tender_title: str,
    tender_dir: Path,
) -> None:
    grouped = _build_no_estimate_files_summary_lines(tender_dir)
    if not grouped:
        return
    emoji_by_label = {
        "PDF": "📕",
        "Word": "📝",
        "Excel": "📗",
        "Архивы": "🗜️",
        "Прочее": "📎",
    }
    lines = [
        f"⚠️ <b>Сметы не найдены</b>: <code>{html.escape(tender_id)}</code>",
        f"<i>{html.escape((tender_title or '').strip() or f'Тендер {tender_id}')}</i>",
        "",
        "📂 <b>Вот какие файлы есть:</b>",
    ]
    for label in ("PDF", "Word", "Excel", "Архивы", "Прочее"):
        names = grouped.get(label, [])
        if not names:
            continue
        icon = emoji_by_label.get(label, "📄")
        lines.append(f"{icon} <b>{label}</b>:")
        for name in names[:20]:
            lines.append(f"• <code>{html.escape(name)}</code>")
        if len(names) > 20:
            lines.append(f"• … и ещё {len(names) - 20}")
        lines.append("")
    send_message(token, chat_id, "\n".join(lines).strip(), parse_mode="HTML", disable_web_page_preview=True)


def download_file(url: str, target_path: Path, cookies: dict[str, str] | None = None) -> tuple[Path, str] | None:
    headers = {"User-Agent": USER_AGENT}
    verify_tls = not _eis_ignore_https_errors()
    if not verify_tls:
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    try:
        resp = requests.get(
            url,
            headers=headers,
            cookies=cookies or {},
            timeout=60,
            verify=verify_tls,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    final_path = target_path
    if target_path.suffix.lower() == ".bin":
        ext = _extension_from_headers(resp.headers.get("content-disposition"))
        if not ext:
            ctype = (resp.headers.get("content-type") or "").lower()
            if "zip" in ctype:
                ext = ".zip"
            elif "rar" in ctype:
                ext = ".rar"
            elif "spreadsheet" in ctype:
                ext = ".xlsx"
            elif "excel" in ctype:
                ext = ".xls"
        if ext:
            final_path = target_path.with_suffix(ext)

    final_path.write_bytes(resp.content)
    original_name = _guess_original_name(url, resp.headers.get("content-disposition"), final_path)
    return final_path, original_name


def open_tender_and_download_archives(tender: Tender, downloads_dir: Path) -> list[Path]:
    tender_dir = downloads_dir / tender.tender_id
    tender_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    download_log: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = _new_eis_page(browser)
        links: list[tuple[str, str]] = []
        candidate_urls = _candidate_notice_urls(tender)

        cookie_jar: dict[str, str] = {}
        nav_errors: list[str] = []
        nav_timeout_ms = max(20_000, int(os.environ.get("EIS_NAV_TIMEOUT_MS", "90000") or "90000"))
        nav_retries = max(1, min(5, int(os.environ.get("EIS_NAV_RETRIES", "3") or "3")))
        for candidate_url in candidate_urls:
            ok, errs = _goto_with_retries(
                page,
                candidate_url,
                timeout_ms=nav_timeout_ms,
                retries=nav_retries,
            )
            nav_errors.extend(errs)
            if not ok:
                continue
            try:
                links.extend(collect_doc_links(page))
                for item in page.context.cookies():
                    name = item.get("name")
                    value = item.get("value")
                    if name and value:
                        cookie_jar[name] = value
            except (PlaywrightTimeoutError, PlaywrightError) as e:
                nav_errors.append(f"{candidate_url} [collect] -> {type(e).__name__}: {e}")
                continue
        browser.close()

    # Убираем дубликаты между страницами common-info/documents.
    uniq_links = []
    seen = set()
    for text, url in links:
        if url in seen:
            continue
        seen.add(url)
        uniq_links.append((text, url))

    if not uniq_links:
        print(
            f"[single] {tender.tender_id}: ссылки на документы не найдены "
            f"(страниц проверено: {len(candidate_urls)}, ошибок навигации: {len(nav_errors)})."
        )
        if nav_errors:
            for err in nav_errors[-2:]:
                print(f"[single] nav-error: {err[:380]}")
        else:
            print(
                "[single] Похоже, портал не отдал ссылки на документы (возможны капча/блокировка/недоступность сети)."
            )

    for idx, (_, url) in enumerate(uniq_links, start=1):
        ext = ".zip" if ".zip" in url.lower() else ".rar" if ".rar" in url.lower() else ".bin"
        file_path = tender_dir / f"doc_{idx}{ext}"
        downloaded = download_file(url, file_path, cookies=cookie_jar)
        if downloaded:
            saved_path, original_name = downloaded
            preferred_name = _sanitize_filename_for_windows(
                original_name,
                fallback=saved_path.name,
            )
            final_path = _choose_unique_path(tender_dir, preferred_name)
            if final_path != saved_path:
                try:
                    if final_path.exists():
                        final_path.unlink()
                    saved_path.rename(final_path)
                    saved_path = final_path
                except OSError:
                    # Если переименование не удалось, оставляем техническое имя doc_N.
                    pass
            saved.append(saved_path)
            download_log.append(
                {
                    "tender_id": tender.tender_id,
                    "url": url,
                    "status": "ok",
                    "saved_path": str(saved_path),
                    "original_name": original_name,
                    "saved_name": saved_path.name,
                    "size_bytes": saved_path.stat().st_size if saved_path.exists() else 0,
                }
            )
        else:
            download_log.append(
                {
                    "tender_id": tender.tender_id,
                    "url": url,
                    "status": "failed",
                    "saved_path": "",
                    "original_name": "",
                    "size_bytes": 0,
                }
            )

    if download_log:
        (tender_dir / "download_log.json").write_text(
            json.dumps(download_log, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return saved


def extract_archives(archives: list[Path], extracted_base: Path) -> list[Path]:
    extracted_files: list[Path] = []
    seven_zip = get_7z_path()
    for archive in archives:
        out_dir = extracted_base / archive.parent.name / archive.stem
        out_dir.mkdir(parents=True, exist_ok=True)

        if archive.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(archive, "r") as zf:
                    zf.extractall(out_dir)
                    extracted_files.extend([out_dir / n for n in zf.namelist()])
            except zipfile.BadZipFile:
                continue
        elif archive.suffix.lower() == ".rar":
            extracted = False
            if seven_zip:
                try:
                    proc = subprocess.run(
                        [seven_zip, "x", str(archive), f"-o{out_dir}", "-y"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                    if proc.returncode in (0, 1):
                        extracted = True
                        extracted_files.extend([p for p in out_dir.rglob("*") if p.is_file()])
                except Exception:
                    extracted = False

            if not extracted:
                try:
                    with rarfile.RarFile(archive) as rf:
                        rf.extractall(out_dir)
                        extracted_files.extend([out_dir / n for n in rf.namelist()])
                except Exception:
                    continue
    return extracted_files


def archive_seeds_for_tender(downloads_dir: Path | None, extracted_tender_root: Path | None) -> list[Path]:
    """Все zip/rar в папке скачивания и уже распакованного дерева (вложенные сметы)."""
    paths: list[Path] = []
    for root in (downloads_dir, extracted_tender_root):
        if root is None or not root.exists():
            continue
        paths.extend(
            p
            for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in (".zip", ".rar")
        )
    return unique_paths_preserve_order(paths)


def extract_archives_nested(archives: list[Path], extracted_base: Path, max_rounds: int = 32) -> list[Path]:
    """
    Распаковывает цепочку вложенных архивов (типично: doc_5.zip → «сметная документация.zip» → *.xlsx).
    """
    all_out: list[Path] = []
    pending = [p for p in archives if p.is_file() and p.suffix.lower() in (".zip", ".rar")]
    seen_keys: set[str] = set()
    for _ in range(max_rounds):
        if not pending:
            break
        batch: list[Path] = []
        for p in pending:
            try:
                key = str(p.resolve())
            except OSError:
                key = str(p)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            batch.append(p)
        if not batch:
            break
        out = extract_archives(batch, extracted_base)
        all_out.extend(out)
        pending = [p for p in out if p.is_file() and p.suffix.lower() in (".zip", ".rar")]
    return all_out


def unique_paths_preserve_order(paths: list[Path]) -> list[Path]:
    """Один физический файл — один раз (иначе позиции в отчёте/HTML дублируются)."""
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        try:
            key = str(p.resolve().absolute())
        except OSError:
            key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def sort_excel_for_lsr_priority(paths: list[Path]) -> list[Path]:
    """Сначала крупные .xlsx — в них чаще полноценная ЛСР; мелкие часто формы/шаблоны."""
    uniq = unique_paths_preserve_order(paths)

    def key(p: Path) -> tuple[int, str]:
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        return (-sz, str(p))

    return sorted(uniq, key=key)


def is_excel_file(path: Path) -> bool:
    return path.suffix.lower() in (".xlsx", ".xls")


def is_pdf_file(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"


def is_estimate_excel(path: Path) -> bool:
    if not is_excel_file(path):
        return False
    # Не фильтруем строго по имени файла: в архивах имена могут быть в "битой" кодировке.
    # Отбор ЛСР делаем по структуре листа в extract_lsr_rows().
    return True


def _is_number_like(value) -> bool:
    return to_float(value) is not None


def _cell_text(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def to_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        if math.isnan(num) or math.isinf(num):
            return None
        return num
    raw = str(value).strip()
    if not raw:
        return None
    raw = raw.replace("\xa0", " ").replace(" ", "").replace(",", ".")
    m = re.match(r"^-?\d+(\.\d+)?$", raw)
    if not m:
        return None
    try:
        num = float(raw)
        if math.isnan(num) or math.isinf(num):
            return None
        return num
    except ValueError:
        return None


def is_mostly_numeric(text: str) -> bool:
    cleaned = re.sub(r"[\d\.,\-\s]+", "", text)
    return len(cleaned.strip()) <= 1


def find_header_row(df: pd.DataFrame) -> tuple[int | None, int | None, int | None]:
    max_rows = min(len(df), 30)
    for r in range(max_rows):
        row_values = [str(v).lower().strip() for v in df.iloc[r].tolist()]
        work_col = None
        price_col = None
        for c, val in enumerate(row_values):
            if work_col is None and any(h in val for h in WORK_COLUMN_HINTS):
                work_col = c
            if price_col is None and any(h in val for h in PRICE_COLUMN_HINTS):
                price_col = c
        if work_col is not None and price_col is not None:
            return r, work_col, price_col
    return None, None, None


def detect_lsr_layout(df: pd.DataFrame) -> int | None:
    """
    Структурный детектор ЛСР (без привязки к языку/кодировке заголовка):
    - номер позиции в A
    - наименование в C-G
    (сумма может быть не в P — см. pick_lsr_position_total)
    """
    if df.shape[1] < 9:
        return None

    start = 0
    end = min(len(df), 300)
    good_rows = 0
    first_row = None

    for r in range(start, end):
        row = df.iloc[r].tolist()
        # Только колонка A — начало позиции ЛСР. В B бывают коды ресурсов и подномера (1, 2, …).
        item_ok = _is_number_like(row[0] if len(row) > 0 else None)
        if not item_ok:
            continue
        work_text = " ".join(_cell_text(row[c]) for c in range(2, min(7, len(row)))).strip()
        if len(work_text) < 6:
            continue
        if is_mostly_numeric(work_text):
            continue
        # Строка шапки позиции почти всегда без суммы в P (итог внизу блока) — сумму здесь не проверяем.
        good_rows += 1
        if first_row is None:
            first_row = r

    if good_rows >= 1:
        return first_row
    return None


# Подписи итога по позиции в ЛСР (снизу блока). Сначала длинные фразы — короткие вроде «итог по поз.»
# входят в «итог по позиции» как подстрока, порядок важен только для одной строки.
LSR_POSITION_TOTAL_HINTS = (
    "всего затрат по позиции",
    "итог по позиции",
    "всего по позиции",
    "итог по поз.",
    "всего по поз.",
    "стоимость по позиции",
    "затраты по позиции",
    "всего затрат",
)


def _best_money_in_row(df: pd.DataFrame, rr: int, col_start: int = 8) -> float | None:
    """Fallback: наибольшее разумное число в строке справа от блока названия."""
    best = None
    for c in range(col_start, df.shape[1]):
        num = to_float(df.iat[rr, c])
        if num is None or num <= 0:
            continue
        if num > 5_000_000_000:
            continue
        if best is None or num > best:
            best = num
    return best


def _rightmost_money_in_row(df: pd.DataFrame, rr: int, col_start: int = 8) -> float | None:
    """Для строки «Всего по позиции» / итога — самое правое число в строке (как в ЛСР)."""
    for c in range(df.shape[1] - 1, col_start - 1, -1):
        num = to_float(df.iat[rr, c])
        if num is None or num <= 0:
            continue
        if num > 5_000_000_000:
            continue
        return num
    return None


def _lsr_total_column_probe_order(ncols: int, total_col: int) -> list[int]:
    """
    Порядок колонок для поиска суммы по позиции.
    Раньше бралась самая правая колонка листа с числом — там часто не «Всего», а тариф/Тр/др. поле,
    из-за чего в «сумму позиции» попадали миллионы при нормальной работе (напр. разборка слуховых окон).
    Сначала смотрим колонку «как правило всего» (P≈15) и соседей, затем остальные справа налево.
    """
    seen: set[int] = set()
    order: list[int] = []
    for delta in (0, -1, 1, -2, 2, -3, 3, -4, 4):
        c = total_col + delta
        if 8 <= c < ncols and c not in seen:
            seen.add(c)
            order.append(c)
    for c in range(ncols - 1, 7, -1):
        if c not in seen:
            seen.add(c)
            order.append(c)
    return order


def pick_lsr_position_total(df: pd.DataFrame, row_idx: int, next_row_idx: int, total_col: int) -> float | None:
    """
    Сумма по позиции:
    1) Строка внизу блока с текстом «Всего по позиции» / «итог по позиции» и т.п. — берём самое правое число в этой строке.
    2) Иначе — «нижнее» значение по колонкам (типичная колонка «Всего» и соседи).
    3) В крайнем случае — max по строкам ресурсов блока.
    """
    text_cols_end = min(16, df.shape[1])
    for rr in range(next_row_idx - 1, row_idx - 1, -1):
        if rr < 0 or rr >= len(df):
            continue
        name_blob = " ".join(_cell_text(df.iat[rr, c]) for c in range(2, text_cols_end)).lower()
        for hint in LSR_POSITION_TOTAL_HINTS:
            if hint in name_blob:
                val = _rightmost_money_in_row(df, rr, 8)
                if val is not None:
                    return val
                break

    for c in _lsr_total_column_probe_order(df.shape[1], total_col):
        last_total = None
        for rr in range(row_idx, next_row_idx):
            num = to_float(df.iat[rr, c])
            if num is not None and num > 0:
                last_total = num
        if last_total is not None and last_total >= 100:
            return last_total

    if total_col < df.shape[1]:
        last_total = None
        for rr in range(row_idx, next_row_idx):
            num = to_float(df.iat[rr, total_col])
            if num is not None and num > 0:
                last_total = num
        if last_total is not None:
            return last_total

    block_best = None
    for rr in range(row_idx + 1, next_row_idx):
        val = _best_money_in_row(df, rr, 8)
        if val is None or val < 100:
            continue
        if block_best is None or val > block_best:
            block_best = val
    return block_best


def _lsr_header_numeric_cells(header_vals: list, c_lo: int = 8, c_hi: int = 24) -> list[tuple[int, float]]:
    """Числа в строке шапки позиции (кол-во, тарифы, цена за ед. и т.д.)."""
    out: list[tuple[int, float]] = []
    hi = min(c_hi, len(header_vals))
    for c in range(c_lo, hi):
        v = to_float(header_vals[c])
        if v is None or v <= 0:
            continue
        if v > 1e12:
            continue
        out.append((c, v))
    return out


def _lsr_degenerate_qty1_times_total(qty: float, up: float, last_total: float) -> bool:
    """Вырожденная пара «1 × почти вся сумма» — часто ложная, если в строке есть нормальный тариф×объём."""
    if last_total <= 0:
        return False
    if abs(qty - 1.0) > 1e-6 and abs(up - 1.0) > 1e-6:
        return False
    big = up if abs(qty - 1.0) < 1e-6 else qty
    return abs(big - last_total) / last_total < 0.02


def _lsr_spurious_tiny_times_huge(qty: float, up: float, last_total: float) -> bool:
    """0.03 × огромный «тариф» даёт сумму — в ЛСР малый множитель часто коэффициент, не объём."""
    if last_total <= 0:
        return False
    lo = min(qty, up)
    hi = max(qty, up)
    if lo >= 0.12:
        return False
    return hi >= max(last_total * 0.35, 25_000.0)


def _lsr_best_qty_unit_pair(
    header_vals: list,
    last_total: float,
    *,
    tol: float = 0.055,
) -> tuple[float, float] | None:
    """
    Подбирает кол-во и цену за ед. по числам в строке позиции так, чтобы qty×unit ≈ сумма по позиции.
    Раньше всегда брали last_total / qty из фиксированной колонки — при сдвиге колонок или «чужом» числе
    получались неверные тарифы (напр. 19 298 вместо ~3 046 при верном произведении).
    """
    cells = _lsr_header_numeric_cells(header_vals)
    if len(cells) < 2 or last_total < 100:
        return None

    scored: list[tuple[float, float, float, float, float]] = []
    n = len(cells)
    for i in range(n):
        ci, vi = cells[i]
        for j in range(n):
            if i == j:
                continue
            cj, vj = cells[j]
            for qty, up, cq, cup in ((vi, vj, ci, cj), (vj, vi, cj, ci)):
                prod = qty * up
                if prod <= 0:
                    continue
                err = abs(prod - last_total) / max(last_total, 1.0)
                if err > tol:
                    continue
                if _lsr_spurious_tiny_times_huge(qty, up, last_total):
                    continue
                scored.append((err, qty, up, float(cq), float(cup)))

    if not scored:
        return None

    non_deg = [t for t in scored if not _lsr_degenerate_qty1_times_total(t[1], t[2], last_total)]
    pool = non_deg if non_deg else scored

    def sort_key(t: tuple[float, float, float, float, float]) -> tuple:
        err, qty, up, cq, cup = t
        # Типичный порядок в ЛСР: объём левее тарифа/цены за ед.
        order_pen = 0.0 if cq < cup else 0.001
        return (err, order_pen)

    pool.sort(key=sort_key)
    _, qty, up, _, _ = pool[0]
    if qty <= 0 or up <= 0:
        return None
    return (qty, up)


def extract_lsr_rows(df: pd.DataFrame, tender: Tender, source_file: Path, *, sheet_name: str = "") -> list[dict]:
    items: list[dict] = []
    header_row = detect_lsr_layout(df)
    if header_row is None:
        return items

    # Типичные колонки ЛСР: A №, C-G название, H ед.изм, I количество; сумма — в pick_lsr_position_total (часто не P).
    work_cols = list(range(2, 7))
    unit_col = 7
    qty_col = 8
    total_col = min(15, df.shape[1] - 1)
    price_per_unit_candidates = [c for c in (9, 10, 11, 12, 13, 14) if c < df.shape[1]]

    position_rows: list[tuple[int, int, str, str, float | None, str]] = []
    current_section = ""
    # header_row — уже строка первой позиции (шапка с № в A), не пропускаем её.
    for r in range(header_row, len(df)):
        row_values = df.iloc[r].tolist()
        row_text = " ".join(_cell_text(x) for x in row_values if _cell_text(x)).strip()
        row_low = row_text.lower()
        if "раздел" in row_low and 4 <= len(row_text) <= 220:
            current_section = row_text[:220]
        if len(row_values) < 9:
            continue

        # Номер позиции только в A. Строки ресурсов: A пустая, в B — коды/подномера — не считаем позицией.
        item_no_a = row_values[0] if len(row_values) > 0 else None
        if not _is_number_like(item_no_a):
            continue
        item_no = to_float(item_no_a)
        if item_no is None:
            continue
        # Оставляем только "позиции", отсекаем вложенные подстроки ресурсов.
        if abs(item_no - round(item_no)) > 1e-9:
            continue

        work_parts = []
        for c in work_cols:
            if c < len(row_values):
                t = _cell_text(row_values[c])
                if t:
                    work_parts.append(t)
        work_text = " ".join(work_parts).strip()
        if not work_text:
            continue
        if is_mostly_numeric(work_text):
            continue
        if work_text.startswith("в т.ч") or work_text.startswith("в том числе"):
            continue
        low = work_text.lower()
        if any(h in low for h in SKIP_ROW_HINTS):
            continue
        if len(work_text) < 8:
            continue

        unit_text = _cell_text(row_values[unit_col]) if unit_col < len(row_values) else ""
        qty_val = row_values[qty_col] if qty_col < len(row_values) else None
        qty = to_float(qty_val)

        position_rows.append((r, int(round(item_no)), work_text, unit_text, qty, current_section))

    if not position_rows:
        return items

    # Берем сумму позиции как последнее числовое значение в колонке P
    # между текущей позицией и началом следующей.
    for idx, (row_idx, item_no, work_text, unit_text, qty, section_text) in enumerate(position_rows):
        next_row_idx = position_rows[idx + 1][0] if idx + 1 < len(position_rows) else len(df)
        last_total = pick_lsr_position_total(df, row_idx, next_row_idx, total_col)
        if last_total is None:
            continue

        header_row_vals = df.iloc[row_idx].tolist()
        qty_from_unit = estimate_block_qty_from_unit(unit_text)
        pair = _lsr_best_qty_unit_pair(header_row_vals, last_total, tol=0.055)

        # Сначала пара «кол-во × цена» из ячеек строки позиции — она даёт реальный объём и тариф.
        # Раньше для «100 м2» срабатывал fallback qty_from_unit: кол-во=100 и цена=сумма/100 (~руб/м²),
        # из-за чего _lsr_best_qty_unit_pair даже не вызывался и в ОТЧЁТ уходили 100 и 415 вместо 3,5 и 11 865.
        unit_price = None
        if pair is not None:
            pq, pup = pair
            if not _lsr_spurious_tiny_times_huge(pq, pup, last_total):
                qty, unit_price = pq, pup
        if unit_price is None and qty_from_unit is not None and qty_from_unit > 0:
            # Для м²/м³ число в «Ед. изм.» — норматив ГЭСН, не объём; сумма/норматив — не «цена за ед.» в смете.
            if not unit_has_area_or_volume_marker(unit_text):
                up_guess = last_total / qty_from_unit
                if math.isfinite(up_guess) and up_guess > 0 and up_guess <= min(last_total, 100_000_000.0):
                    qty = qty_from_unit
                    unit_price = up_guess
        if unit_price is None or unit_price <= 0:
            if qty is not None and qty > 1e-9 and qty >= 0.12:
                unit_price = last_total / qty
            else:
                unit_price = None
            if unit_price is None or unit_price <= 0:
                for c in price_per_unit_candidates:
                    if c >= len(header_row_vals):
                        continue
                    maybe = to_float(header_row_vals[c])
                    if maybe is not None and maybe >= 10:
                        unit_price = maybe
                        break

        items.append(
            {
                "tender_id": tender.tender_id,
                "region": tender.region,
                "tender_title": tender.title,
                "tender_url": tender.url,
                "source_file": str(source_file),
                "sheet_name": str(sheet_name or ""),
                "excel_row": int(row_idx + 1),
                "section": section_text,
                "extract_source": "LSR",
                "item_no": item_no,
                "work_name": work_text,
                "unit": unit_text,
                "qty": qty,
                "qty_with_unit": f"{qty:g} {unit_text}".strip() if qty is not None else unit_text,
                "unit_price_rub": unit_price,
                "price_from_estimate_rub": last_total,
            }
        )
    return items


def extract_work_rows(df: pd.DataFrame, tender: Tender, source_file: Path, *, sheet_name: str = "") -> list[dict]:
    items: list[dict] = []
    header_row, work_col, price_col = find_header_row(df)
    if header_row is not None:
        current_section = ""
        for r in range(header_row + 1, len(df)):
            row_vals = df.iloc[r].tolist()
            row_text = " ".join(_cell_text(x) for x in row_vals if _cell_text(x)).strip()
            row_low = row_text.lower()
            if "раздел" in row_low and 4 <= len(row_text) <= 220:
                current_section = row_text[:220]
                continue
            work_val = df.iat[r, work_col] if work_col < df.shape[1] else None
            price_val = df.iat[r, price_col] if price_col < df.shape[1] else None
            work_text = str(work_val).strip() if work_val is not None else ""
            if not work_text or work_text.lower() == "nan":
                continue
            low = work_text.lower()
            if any(h in low for h in SKIP_ROW_HINTS):
                continue
            if len(work_text) < 6:
                continue
            price = to_float(price_val)
            if price is None or price <= 100:
                continue
            items.append(
                {
                    "tender_id": tender.tender_id,
                    "region": tender.region,
                    "tender_title": tender.title,
                    "tender_url": tender.url,
                    "source_file": str(source_file),
                    "sheet_name": str(sheet_name or ""),
                    "excel_row": int(r + 1),
                    "section": current_section,
                    "extract_source": "Excel fallback",
                    "work_name": work_text,
                    "price_from_estimate_rub": price,
                }
            )
    return items


def extract_work_rows_fallback(df: pd.DataFrame, tender: Tender, source_file: Path, *, sheet_name: str = "") -> list[dict]:
    items: list[dict] = []
    current_section = ""
    for r in range(len(df)):
        row = df.iloc[r].tolist()
        row_text_full = " ".join(_cell_text(x) for x in row if _cell_text(x)).strip()
        row_low_full = row_text_full.lower()
        if "раздел" in row_low_full and 4 <= len(row_text_full) <= 220:
            current_section = row_text_full[:220]
        text_candidates: list[str] = []
        numeric_candidates: list[tuple[int, float]] = []

        for c, cell in enumerate(row):
            if cell is None:
                continue
            text = str(cell).strip()
            if not text or text.lower() == "nan":
                continue
            num = to_float(cell)
            if num is not None:
                if num > 0:
                    numeric_candidates.append((c, num))
                continue
            if c <= 8 and len(text) >= 4 and not is_mostly_numeric(text):
                text_candidates.append(text)

        if not text_candidates or not numeric_candidates:
            continue
        if len(numeric_candidates) < 2 or len(numeric_candidates) > 8:
            continue

        work_name = max(text_candidates, key=len).strip()
        low = work_name.lower()
        if any(h in low for h in SKIP_ROW_HINTS):
            continue
        if len(work_name) < 6 or len(work_name) > 220:
            continue

        # Для смет обычно итог ближе к правой части строки.
        rightmost_price = max(numeric_candidates, key=lambda x: x[0])[1]
        if rightmost_price < 100 or rightmost_price > 5_000_000_000:
            continue

        items.append(
            {
                "tender_id": tender.tender_id,
                "region": tender.region,
                "tender_title": tender.title,
                "tender_url": tender.url,
                "source_file": str(source_file),
                "sheet_name": str(sheet_name or ""),
                "excel_row": int(r + 1),
                "section": current_section,
                "extract_source": "Excel deep fallback",
                "work_name": work_name,
                "price_from_estimate_rub": rightmost_price,
            }
        )
    return items


def extract_rows_from_excel(path: Path, tender: Tender) -> list[dict]:
    rows: list[dict] = []
    if should_skip_object_estimate_file(path):
        return rows
    try:
        data = pd.read_excel(path, sheet_name=None, header=None)
    except Exception:
        return rows

    fallback_enabled = _truthy_env("ESTIMATE_EXCEL_FALLBACK", "1")
    deep_fallback_enabled = _truthy_env("ESTIMATE_EXCEL_DEEP_FALLBACK", "0")
    for sheet_name, df in data.items():
        if df.empty:
            continue
        part = extract_lsr_rows(df, tender, path, sheet_name=str(sheet_name))
        if not part and fallback_enabled:
            part = extract_work_rows(df, tender, path, sheet_name=str(sheet_name))
        if not part and deep_fallback_enabled:
            part = extract_work_rows_fallback(df, tender, path, sheet_name=str(sheet_name))
        rows.extend(part)
    # Убираем дубли строк в рамках одного файла.
    uniq = {}
    for row in rows:
        key = (row["source_file"], row["work_name"], round(float(row["price_from_estimate_rub"]), 2))
        uniq[key] = row
    return list(uniq.values())


def _iter_pdf_lines(path: Path) -> Iterable[str]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return []
    try:
        reader = PdfReader(str(path))
    except Exception:
        return []
    out: list[str] = []
    for pg in reader.pages:
        try:
            txt = pg.extract_text() or ""
        except Exception:
            txt = ""
        if not txt:
            continue
        for ln in txt.splitlines():
            s = re.sub(r"\s+", " ", ln).strip()
            if s:
                out.append(s)
    return out


def extract_rows_from_pdf(path: Path, tender: Tender) -> list[dict]:
    """
    Универсальный fallback по PDF: ищем строки вида «наименование ... сумма».
    Работает не для всех PDF (сканы/OCR), но даёт шанс вытащить позиции там, где Excel нет.
    """
    rows: list[dict] = []
    for line in _iter_pdf_lines(path):
        m = PDF_LINE_PRICE_RE.match(line)
        if not m:
            continue
        work_name = str(m.group("name") or "").strip()
        if len(work_name) < 8 or len(work_name) > 260:
            continue
        low = work_name.lower()
        if any(h in low for h in SKIP_ROW_HINTS):
            continue
        price = to_float(m.group("price"))
        if price is None or price < 100 or price > 5_000_000_000:
            continue
        rows.append(
            {
                "tender_id": tender.tender_id,
                "region": tender.region,
                "tender_title": tender.title,
                "tender_url": tender.url,
                "source_file": str(path),
                "extract_source": "PDF fallback",
                "item_no": None,
                "work_name": work_name,
                "unit": "",
                "qty": None,
                "qty_with_unit": "",
                "unit_price_rub": None,
                "price_from_estimate_rub": price,
            }
        )
    uniq: dict[tuple[str, str, float], dict] = {}
    for row in rows:
        key = (row["source_file"], row["work_name"], round(float(row["price_from_estimate_rub"]), 2))
        uniq[key] = row
    return list(uniq.values())


def write_outputs(tenders: list[Tender], rows: list[dict], out_paths: dict[str, Path]) -> Path:
    (out_paths["root"] / "tenders.json").write_text(
        json.dumps([asdict(t) for t in tenders], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report_path = out_paths["reports"] / f"works_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(
            columns=[
                "tender_id",
                "region",
                "tender_title",
                "tender_url",
                "source_file",
                "work_name",
                "price_from_estimate_rub",
            ]
        )
    df.to_excel(report_path, index=False)
    return report_path


def write_tender_estimate_report(tender: Tender, rows: list[dict], out_paths: dict[str, Path]) -> tuple[Path, pd.DataFrame]:
    safe_name = f"ОТЧЕТ_ПО_СМЕТАМ_{tender.tender_id}.xlsx"
    path = out_paths["reports"] / safe_name
    clean_df = _build_tender_clean_df(rows)
    clean_df.to_excel(path, index=False)
    return path, clean_df


def _build_tender_clean_df(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(
            columns=[
                "Файл ЛСР",
                "Источник извлечения",
                "№ п/п",
                "Лист",
                "Строка Excel",
                "Раздел",
                "Название работы/услуги",
                "Ед. изм.",
                "Кол-во",
                "Объем",
                "Цена за ед., руб",
                "Сумма, руб",
            ]
        )
    df = pd.DataFrame(rows)
    clean_df = df[
        [
            "source_file",
            "extract_source",
            "item_no",
            "sheet_name",
            "excel_row",
            "section",
            "work_name",
            "unit",
            "qty",
            "qty_with_unit",
            "unit_price_rub",
            "price_from_estimate_rub",
        ]
    ].rename(
        columns={
            "source_file": "Файл ЛСР",
            "extract_source": "Источник извлечения",
            "item_no": "№ п/п",
            "sheet_name": "Лист",
            "excel_row": "Строка Excel",
            "section": "Раздел",
            "work_name": "Название работы/услуги",
            "unit": "Ед. изм.",
            "qty": "Кол-во",
            "qty_with_unit": "Объем",
            "unit_price_rub": "Цена за ед., руб",
            "price_from_estimate_rub": "Сумма, руб",
        }
    )
    clean_df = clean_df.dropna(subset=["Название работы/услуги", "Сумма, руб"])
    clean_df["Название работы/услуги"] = clean_df["Название работы/услуги"].astype(str).str.strip()
    clean_df = clean_df[clean_df["Название работы/услуги"].str.len() >= 6]
    clean_df = clean_df[~clean_df["Название работы/услуги"].str.lower().str.contains("|".join(SKIP_ROW_HINTS), regex=True)]
    clean_df = clean_df[clean_df["Сумма, руб"] > 0]
    # Точные дубли (×2 в отчёте): тот же файл, название, сумма и кол-во — часто после двойного чтения одного LSR.
    clean_df = clean_df.drop_duplicates(
        subset=["Файл ЛСР", "Название работы/услуги", "Сумма, руб", "Кол-во"],
        keep="first",
    )
    # Повтор с тем же названием и суммой, если «Кол-во» отличалось из-за NaN/округления.
    clean_df = clean_df.drop_duplicates(
        subset=["Файл ЛСР", "Название работы/услуги", "Сумма, руб"],
        keep="first",
    )
    # Одна и та же позиция из разных файлов / повторный разбор — в отчёте одна строка (название + сумма).
    clean_df = clean_df.drop_duplicates(
        subset=["Название работы/услуги", "Сумма, руб"],
        keep="first",
    )
    clean_df = clean_df.sort_values(by=["Файл ЛСР", "Лист", "№ п/п", "Строка Excel"], ascending=[True, True, True, True])
    return clean_df


def write_tender_estimate_html(tender: Tender, clean_df: pd.DataFrame, out_paths: dict[str, Path]) -> Path:
    html_path = out_paths["reports"] / f"ОТЧЕТ_ПО_СМЕТАМ_{tender.tender_id}.html"

    def fmt_num(v):
        if pd.isna(v):
            return ""
        try:
            return f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
        except Exception:
            return str(v)

    def source_badge(raw) -> str:
        src = str(raw or "").strip()
        key = src.lower()
        cls = "src-lsr"
        if "pdf" in key:
            cls = "src-pdf"
        elif "deep" in key:
            cls = "src-deep"
        elif "fallback" in key:
            cls = "src-fallback"
        label = src or "LSR"
        return f"<span class='src-badge {cls}'>{html.escape(label)}</span>"

    total_sum_val = float(clean_df["Сумма, руб"].sum()) if not clean_df.empty else 0.0
    total_sum = fmt_num(total_sum_val)
    tender_price = to_float(tender.price_rub)
    diff_val = (total_sum_val - tender_price) if tender_price is not None else None
    diff_text = fmt_num(diff_val) if diff_val is not None else "—"
    tolerance = max(5000.0, abs(tender_price or 0) * 0.02)
    sum_with_vat_hint = total_sum_val * NDS_MULTIPLIER
    sum_with_vat_text = fmt_num(sum_with_vat_hint)
    near_nmck_via_vat = False
    if tender_price is not None and tender_price > 0 and total_sum_val > 0:
        near_nmck_via_vat = abs(sum_with_vat_hint - tender_price) / tender_price <= NMCK_NEAR_VAT_MAX_REL_DIFF
    indicator_class = (
        "ok"
        if (diff_val is not None and abs(diff_val) <= tolerance)
        or near_nmck_via_vat
        else "warn"
    )

    groups_html = []
    if not clean_df.empty:
        grouped = clean_df.groupby("Файл ЛСР", dropna=False, sort=True)
        for file_name, grp in grouped:
            grp = grp.sort_values(by=["№ п/п"], ascending=True)
            file_sum = float(grp["Сумма, руб"].sum())
            rows_html = []
            for _, row in grp.iterrows():
                rows_html.append(
                    "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{}</td>"
                    "<td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{}</td></tr>".format(
                        row.get("№ п/п", ""),
                        source_badge(row.get("Источник извлечения", "")),
                        row.get("Название работы/услуги", ""),
                        row.get("Ед. изм.", ""),
                        fmt_num(row.get("Кол-во", "")),
                        row.get("Объем", ""),
                        fmt_num(row.get("Цена за ед., руб", "")),
                        fmt_num(row.get("Сумма, руб", "")),
                    )
                )
            groups_html.append(
                f"""
<details class="group" open>
  <summary>
    <span class="fname">{file_name}</span>
    <span class="fmeta">Позиции: {len(grp)} | Сумма: {fmt_num(file_sum)} руб.</span>
  </summary>
  <table>
    <thead>
      <tr>
        <th>№ п/п</th><th>Источник</th><th>Название работы/услуги</th><th>Ед. изм.</th><th>Кол-во</th>
        <th>Объем</th><th>Цена за ед., руб</th><th>Сумма, руб</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>
</details>
"""
            )
    html_doc = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Отчет по сметам {tender.tender_id}</title>
  <style>
    html {{ box-sizing: border-box; }}
    *, *::before, *::after {{ box-sizing: inherit; }}
    body {{ font-family: Segoe UI, Arial, sans-serif; background:#0f1220; color:#e8ecf1; margin:0; padding:16px 20px 56px 20px; }}
    .card {{ background:#171b2e; border:1px solid #2a3155; border-radius:14px; padding:16px; margin-bottom:16px; }}
    .report-header.card {{
      position: sticky;
      top: 0;
      z-index: 20;
      margin-bottom: 20px;
      box-shadow: 0 8px 24px rgba(0,0,0,.45);
      transition: padding 0.28s ease, margin-bottom 0.28s ease, box-shadow 0.28s ease;
    }}
    .report-header.scrolled {{
      padding: 8px 12px;
      margin-bottom: 12px;
      box-shadow: 0 4px 18px rgba(0,0,0,.55);
    }}
    .report-tables.card {{ margin-top: 4px; }}
    .report-header h1 {{ margin:0 0 8px 0; font-size:22px; line-height:1.2; transition: font-size 0.26s ease, margin 0.26s ease; }}
    .report-header.scrolled h1 {{ font-size: 14px; margin-bottom: 4px; }}
    .meta {{ color:#9fb0d6; font-size:14px; }}
    .report-header .report-pos-line {{ transition: font-size 0.26s ease; }}
    .report-header.scrolled .report-pos-line {{ font-size: 11px; }}
    .sum {{ font-size:18px; font-weight:700; color:#9df0b8; margin-top:8px; transition: font-size 0.26s ease, margin 0.26s ease; }}
    .report-header.scrolled .sum {{ font-size: 13px; margin-top: 2px; }}
    .check {{ margin-top:8px; font-size:14px; padding:8px 10px; border-radius:10px; transition: margin 0.26s ease, padding 0.26s ease, font-size 0.26s ease; }}
    .report-header.scrolled .check {{ margin-top: 6px; padding: 6px 8px; font-size: 11px; line-height: 1.35; }}
    .check.ok {{ background:#203a2f; color:#a8f4c8; border:1px solid #3d8a67; }}
    .check.warn {{ background:#3a2c20; color:#ffd7a8; border:1px solid #8a6340; }}
    .group {{ margin-bottom:14px; border:1px solid #273055; border-radius:12px; overflow:visible; background:#13182b; }}
    .group > summary {{ cursor:pointer; list-style:none; padding:10px 12px; background:#1e2644; display:flex; justify-content:space-between; gap:12px; border-radius:12px 12px 0 0; }}
    .group > summary::-webkit-details-marker {{ display:none; }}
    .group table {{ margin-top: 0; }}
    .fname {{ font-weight:600; }}
    .fmeta {{ color:#b8c7ea; font-size:13px; }}
    table {{ width:100%; border-collapse:collapse; background:#13182b; }}
    th, td {{ border:1px solid #273055; padding:8px 10px; font-size:13px; vertical-align:top; }}
    th {{ background:#1e2644; }}
    tr:nth-child(even) {{ background:#12172a; }}
    .wrap {{ white-space:normal; }}
    .src-badge {{
      display:inline-flex;
      align-items:center;
      font-weight:700;
      font-size:11px;
      line-height:1.2;
      padding:3px 8px;
      border-radius:999px;
      border:1px solid #3c4f83;
      background:#1d2742;
      color:#d8e4ff;
      white-space:nowrap;
    }}
    .src-badge.src-lsr {{ background:#1d3a2b; border-color:#3d8a67; color:#b8f5d6; }}
    .src-badge.src-fallback {{ background:#3a2f1f; border-color:#8a6340; color:#ffd8ae; }}
    .src-badge.src-deep {{ background:#3a2323; border-color:#8b4a4a; color:#ffcdcd; }}
    .src-badge.src-pdf {{ background:#2e2440; border-color:#7a5ab3; color:#dbc7ff; }}
    .report-header-extra {{
      overflow: hidden;
      max-height: 320px;
      opacity: 1;
      margin-top: 8px;
      transition: max-height 0.35s ease, opacity 0.25s ease, margin 0.28s ease;
    }}
    .report-header.scrolled .report-header-extra {{
      max-height: 0;
      opacity: 0;
      margin-top: 0;
      pointer-events: none;
    }}
    .report-header-extra .meta + .meta {{ margin-top: 8px; }}
  </style>
</head>
<body>
  <div class="card report-header" id="reportHeaderRoot">
    <h1>ОТЧЕТ ПО СМЕТАМ: {tender.tender_id}</h1>
    <div class="meta report-pos-line">Позиции: {len(clean_df)}</div>
    <div class="sum">Общая сумма по позициям: {total_sum} руб.</div>
    <div class="check {indicator_class}">
      Контроль: сумма по строкам отчёта {total_sum} руб.
      | НМЦК тендера: {fmt_num(tender_price) if tender_price is not None else "—"} руб.
      | Разница: {diff_text} руб.
    </div>
    <div class="report-header-extra">
      <div class="meta">
        Ориентир, если в ЛСР суммы без НДС, а НМЦК с НДС {int(NDS_RATE * 100)}%: {total_sum} × {NDS_MULTIPLIER:g} ≈ {sum_with_vat_text} руб.
        (остаток до НМЦК часто закрывают добавочные коэффициенты в смете — зимние, индексные, районные и др. — и то, что в отчёт не попали все разделы.)
      </div>
      <div class="meta">
        Сумма в таблице — только позиции из локальных ЛСР (файлы в отчёте). НМЦК закупки обычно шире: все разделы сметы,
        объектные сметы, непредвиденные, НДС и коэффициенты, поэтому полное совпадение с «голой» суммой позиций редко.
        Номер позиции в парсере только из колонки A (не из B).
      </div>
    </div>
  </div>
  <div class="card report-tables">
    {''.join(groups_html) if groups_html else '<div class="meta">Нет данных для отображения.</div>'}
  </div>
  <script>
(function () {{
  var root = document.getElementById("reportHeaderRoot");
  if (!root) return;
  var threshold = 36;
  function tick() {{
    var y = window.scrollY || document.documentElement.scrollTop || 0;
    root.classList.toggle("scrolled", y > threshold);
  }}
  window.addEventListener("scroll", tick, {{ passive: true }});
  tick();
}})();
  </script>
</body>
</html>"""
    html_path.write_text(html_doc, encoding="utf-8")
    return html_path


def _top_work_snippets_for_tg(tender_rows: list[dict], limit: int = 22) -> list[str]:
    rows = sorted(
        tender_rows,
        key=lambda x: float(x.get("price_from_estimate_rub") or 0),
        reverse=True,
    )[:limit]
    out: list[str] = []
    for r in rows:
        name = str(r.get("work_name", "")).replace("\n", " ").strip()
        if len(name) > 280:
            name = name[:277] + "..."
        pr = r.get("price_from_estimate_rub")
        if pr is not None:
            try:
                ps = f"{float(pr):,.0f}".replace(",", " ")
            except (TypeError, ValueError):
                ps = str(pr)
            out.append(f"- {name} — {ps} руб.")
        else:
            out.append(f"- {name}")
    return out


def _top_work_snippets_from_clean_df(clean_df: pd.DataFrame, limit: int = 22) -> list[str]:
    """Топ позиций после дедупликации (как в Excel/HTML отчёте)."""
    if clean_df.empty or "Название работы/услуги" not in clean_df.columns or "Сумма, руб" not in clean_df.columns:
        return []
    sub = clean_df[["Название работы/услуги", "Сумма, руб"]].copy()
    sub["Сумма, руб"] = pd.to_numeric(sub["Сумма, руб"], errors="coerce")
    sub = sub.dropna(subset=["Сумма, руб"])
    sub = sub.sort_values(by="Сумма, руб", ascending=False).head(limit)
    out: list[str] = []
    for _, row in sub.iterrows():
        name = str(row.get("Название работы/услуги", "")).replace("\n", " ").strip()
        if len(name) > 280:
            name = name[:277] + "..."
        try:
            ps = f"{float(row['Сумма, руб']):,.0f}".replace(",", " ")
        except (TypeError, ValueError):
            ps = str(row.get("Сумма, руб", ""))
        out.append(f"- {name} — {ps} руб.")
    return out


def parse_args():
    parser = argparse.ArgumentParser(description="Tender parser MVP")
    parser.add_argument("--max-pages", type=int, default=2, help="Сколько страниц поиска смотреть на комбинацию регион/ключ")
    parser.add_argument("--max-tenders", type=int, default=15, help="Лимит тендеров на обработку")
    parser.add_argument("--days-back", type=int, default=30, help="Брать тендеры не старше N дней")
    parser.add_argument("--from-downloaded-tender-id", type=str, default="", help="Собрать отчет только из уже скачанных файлов тендера")
    parser.add_argument("--from-tender-id", type=str, default="", help="Собрать отчет по конкретному tender_id (вместе с --from-tender-url)")
    parser.add_argument("--from-tender-url", type=str, default="", help="URL карточки тендера для точечного скачивания документов")
    parser.add_argument(
        "--emit-new-ids-to",
        type=str,
        default="",
        help="Путь к файлу: записать новые tender_id по одному на строку (для pipeline после прогона)",
    )
    return parser.parse_args()


def _stdio_utf8_on_windows() -> None:
    """Чтобы кириллица в путях не превращалась в РћС‚С‡РµС‚ в консоли Windows."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError):
            pass


def main():
    _stdio_utf8_on_windows()
    warnings.filterwarnings(
        "ignore",
        message=r"Workbook contains no default style",
        category=UserWarning,
    )
    args = parse_args()
    out_paths = ensure_dirs()
    rar_enabled = configure_rar_backend()
    if not rar_enabled:
        print("Внимание: не найден backend для RAR (unrar/7z). RAR-архивы будут пропущены.")
    all_found: list[Tender] = []

    if args.from_tender_id and args.from_tender_url:
        tid = args.from_tender_id.strip()
        turl = args.from_tender_url.strip()
        # Достаём мета из локального кэша tenders.json, если есть.
        cached = load_cached_tenders(out_paths)
        m = next((x for x in cached if str(x.get("tender_id", "")).strip() == tid), {})
        tender = Tender(
            tender_id=tid,
            title=(m.get("title") or f"Тендер {tid}"),
            url=turl,
            region=(m.get("region") or ""),
            stage=(m.get("stage") or ""),
            price_rub=m.get("price_rub"),
            publish_date=(m.get("publish_date") or None),
        )
        print(f"[single] {tid}: скачивание документов по ссылке")
        downloaded_files = open_tender_and_download_archives(tender, out_paths["downloads"])
        if not downloaded_files:
            print("Документы не скачались по заданной ссылке.")
            raise SystemExit(2)
        tender_dl = out_paths["downloads"] / tender.tender_id
        ext_root = out_paths["extracted"] / tender.tender_id
        seeds = archive_seeds_for_tender(tender_dl, ext_root)
        extracted = extract_archives_nested(seeds, out_paths["extracted"]) if seeds else []
        direct_excel = [p for p in downloaded_files if p.exists() and is_excel_file(p)]
        extracted_excel = [p for p in extracted if p.exists() and is_excel_file(p)]
        excel_files = unique_paths_preserve_order(extracted_excel + direct_excel)
        direct_pdf = [p for p in downloaded_files if p.exists() and is_pdf_file(p)]
        extracted_pdf = [p for p in extracted if p.exists() and is_pdf_file(p)]
        pdf_files = unique_paths_preserve_order(extracted_pdf + direct_pdf)
        estimate_excel_files = [p for p in excel_files if is_estimate_excel(p)]
        if not estimate_excel_files:
            estimate_excel_files = excel_files
        if not estimate_excel_files:
            if not pdf_files:
                print("Не найдено ни одного Excel/PDF-файла (ЛСР/смета) после скачивания/распаковки.")
                raise SystemExit(3)
        estimate_excel_files = sort_excel_for_lsr_priority(estimate_excel_files)
        tender_rows: list[dict] = []
        for excel_path in estimate_excel_files:
            rows = extract_rows_from_excel(excel_path, tender)
            tender_rows.extend(rows)
        for pdf_path in pdf_files:
            tender_rows.extend(extract_rows_from_pdf(pdf_path, tender))
        if not tender_rows:
            rar_cnt = sum(1 for p in tender_dl.rglob("*") if p.is_file() and p.suffix.lower() == ".rar")
            extracted_xlsx = [p for p in extracted if p.exists() and is_excel_file(p)] if extracted else []
            if rar_cnt and not extracted_xlsx:
                print(
                    f"[single] {tid}: найдено RAR: {rar_cnt}, но из архивов не извлечено ни одного .xlsx/.xls. "
                    "Установите 7-Zip (в т.ч. в «C:\\Program Files (x86)\\7-Zip\\») или добавьте 7z.exe в PATH."
                )
            elif rar_cnt:
                print(
                    f"[single] {tid}: RAR распакованы, но в {len(estimate_excel_files)} Excel не найдено строк ЛСР "
                    "(возможен нестандартный формат сметы)."
                )
            else:
                print(
                    f"[single] {tid}: в скачанных Excel не найдено строк ЛСР "
                    f"(просмотрено файлов: {len(estimate_excel_files)})."
                )
            _print_no_estimate_files_summary(tid, tender_dl)
            if tg_cfg:
                tok, chat = tg_cfg
                try:
                    _send_no_estimate_files_summary_to_tg(tok, chat, tid, tender.title, tender_dl)
                except Exception as e:
                    print(f"Telegram: не удалось отправить список файлов по {tid}: {e}")
        tender_report, clean_df = write_tender_estimate_report(tender, tender_rows, out_paths)
        tender_html = write_tender_estimate_html(tender, clean_df, out_paths)
        print(f"Отчет по сметам (Excel): {tender_report} (позиций: {len(clean_df)}, исходно: {len(tender_rows)})")
        print(f"Отчет по сметам (HTML): {tender_html}")
        return

    if args.from_downloaded_tender_id:
        tid = args.from_downloaded_tender_id.strip()
        cached_price = get_tender_price_from_cache(tid, out_paths)
        tender = Tender(
            tender_id=tid,
            title=f"Тендер {tid}",
            url="",
            region="",
            stage="",
            price_rub=cached_price,
            publish_date=None,
        )
        base = out_paths["downloads"] / tid
        downloaded_files = [p for p in base.rglob("*") if p.is_file()] if base.exists() else []
        existing_extracted_root = out_paths["extracted"] / tid
        seeds = archive_seeds_for_tender(base, existing_extracted_root)
        extracted = extract_archives_nested(seeds, out_paths["extracted"]) if seeds else []
        direct_excel = [p for p in downloaded_files if p.exists() and is_excel_file(p)]
        extracted_excel = [p for p in extracted if p.exists() and is_excel_file(p)]
        direct_pdf = [p for p in downloaded_files if p.exists() and is_pdf_file(p)]
        extracted_pdf = [p for p in extracted if p.exists() and is_pdf_file(p)]
        if existing_extracted_root.exists():
            extracted_excel.extend([p for p in existing_extracted_root.rglob("*") if p.is_file() and is_excel_file(p)])
            extracted_pdf.extend([p for p in existing_extracted_root.rglob("*") if p.is_file() and is_pdf_file(p)])
        pdf_files = unique_paths_preserve_order(extracted_pdf + direct_pdf)
        estimate_excel_files = unique_paths_preserve_order(extracted_excel + direct_excel)
        if not estimate_excel_files and not pdf_files:
            print(f"Для тендера {tid} в downloads/extracted не найдено Excel/PDF-файлов.")
            raise SystemExit(4)
        estimate_excel_files = sort_excel_for_lsr_priority(estimate_excel_files)
        tender_rows: list[dict] = []
        for excel_path in estimate_excel_files:
            tender_rows.extend(extract_rows_from_excel(excel_path, tender))
        for pdf_path in pdf_files:
            tender_rows.extend(extract_rows_from_pdf(pdf_path, tender))
        if not tender_rows:
            rar_cnt = sum(1 for p in base.rglob("*") if p.is_file() and p.suffix.lower() == ".rar")
            extracted_xlsx = [p for p in extracted if p.exists() and is_excel_file(p)] if extracted else []
            if rar_cnt and not extracted_xlsx:
                print(
                    f"Для {tid}: RAR найдены ({rar_cnt}), но распаковка не дала Excel — нужен 7-Zip/UnRAR."
                )
            elif rar_cnt:
                print(f"Для {tid}: Excel извлечён из RAR, но строк ЛСР не найдено.")
            else:
                print(f"Для {tid}: в Excel не найдено строк ЛСР.")
            _print_no_estimate_files_summary(tid, base)
            if tg_cfg:
                tok, chat = tg_cfg
                try:
                    _send_no_estimate_files_summary_to_tg(tok, chat, tid, tender.title, base)
                except Exception as e:
                    print(f"Telegram: не удалось отправить список файлов по {tid}: {e}")
        tender_report, clean_df = write_tender_estimate_report(tender, tender_rows, out_paths)
        tender_html = write_tender_estimate_html(tender, clean_df, out_paths)
        print(f"Отчет по сметам (Excel): {tender_report} (позиций в отчёте: {len(clean_df)}, исходно строк: {len(tender_rows)})")
        print(f"Отчет по сметам (HTML): {tender_html}")
        print(f"Done (exit 0). Tender {tid}: {len(clean_df)} positions -> XLSX + HTML.")
        return

    checkpoint = _load_search_checkpoint(out_paths, args)
    resumed_from_checkpoint = False
    completed_ids: set[str] = set()
    search_total = 0
    unique: list[Tender] = []
    filtered: list[Tender] = []
    new_ids: set[str] = set()
    already_in_system = 0
    new_in_run = 0
    if checkpoint:
        for row in checkpoint.get("filtered_tenders") or []:
            try:
                filtered.append(Tender(**row))
            except TypeError:
                continue
        unique = list(filtered)
        completed_ids = {str(x).strip() for x in checkpoint.get("completed_ids") or [] if str(x).strip()}
        new_ids = {str(x).strip() for x in checkpoint.get("new_ids") or [] if str(x).strip()}
        search_total = int(checkpoint.get("search_total") or 0)
        resumed_from_checkpoint = bool(filtered)
        cached = load_cached_tenders(out_paths)
        cached_ids = {str(x.get("tender_id", "")).strip() for x in cached if str(x.get("tender_id", "")).strip()}
        current_ids = {t.tender_id for t in filtered}
        already_in_system = len([tid for tid in current_ids if tid in cached_ids])
        new_in_run = len(current_ids) - already_in_system
        if resumed_from_checkpoint:
            print(
                f"Продолжаем прошлый прогон: найдено {len(filtered)}, "
                f"уже обработано {len(completed_ids)}, осталось {max(0, len(filtered) - len(completed_ids))}."
            )
            print(f"Итого после фильтров: {len(filtered)}")
            print(f"Новые в этом запуске: {new_in_run}")
            print(f"Уже были в системе: {already_in_system}")
    else:
        print("Поиск тендеров...")
        for region in REGIONS:
            for kw in KEYWORDS:
                found = search_tenders(region, kw, max_pages=args.max_pages)
                all_found.extend(found)
                print(f"- {region} / {kw}: {len(found)} найдено")
        search_total = len(all_found)
        unique = dedupe_tenders(all_found)
        filtered = [t for t in unique if tender_matches_filters(t, days_back=args.days_back)]
        filtered = filtered[: args.max_tenders]
        cached = load_cached_tenders(out_paths)
        cached_ids = {str(x.get("tender_id", "")).strip() for x in cached if str(x.get("tender_id", "")).strip()}
        new_ids = {t.tender_id for t in filtered if t.tender_id not in cached_ids}
        current_ids = {t.tender_id for t in filtered}
        already_in_system = len([tid for tid in current_ids if tid in cached_ids])
        new_in_run = len(current_ids) - already_in_system
        print(f"Итого после фильтров: {len(filtered)}")
        print(f"Новые в этом запуске: {new_in_run}")
        print(f"Уже были в системе: {already_in_system}")
        _save_search_checkpoint(
            out_paths,
            args,
            filtered=filtered,
            completed_ids=completed_ids,
            new_ids=new_ids,
            search_total=search_total,
            completed=False,
        )

    stage_changes: list[tuple[str, str, str]] = []
    if os.environ.get("REFRESH_CACHED_STAGES", "1").strip().lower() not in ("0", "false", "no", "off"):
        try:
            stage_changes = refresh_cached_open_tender_stages(out_paths)
        except Exception as e:
            print(f"Предупреждение: обновление этапов в tenders.json: {e}")

    added_count, total_in_system = merge_and_save_tenders(out_paths, filtered)
    print(f"Добавлено в систему: {added_count}")
    print(f"Всего тендеров в системе: {total_in_system}")

    tg_cfg = telegram_config()
    if new_ids and not tg_cfg:
        print("Telegram: есть новые тендеры, но не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — уведомления пропущены.")

    if tg_cfg:
        tok, chat = tg_cfg
        try:
            if stage_changes:
                lines = [
                    "📌 <b>Обновились этапы в ЕИС</b>",
                    "<i>Проверил ранее сохранённые закупки со статусом «Подача заявок» и нашёл изменения:</i>",
                    "",
                ]
                for tid, old_s, new_s in stage_changes[:35]:
                    lines.append(
                        f"• № <code>{html.escape(tid)}</code>\n"
                        f"  раньше: <i>{html.escape(old_s)}</i>\n"
                        f"  сейчас: <b>{html.escape(new_s)}</b>"
                    )
                if len(stage_changes) > 35:
                    lines.append(f"\n… и ещё {len(stage_changes) - 35}.")
                send_message(tok, chat, "\n".join(lines))

            if new_ids:
                head = [
                    "🔄 <b>Плановый прогон</b>",
                    f"Новых тендеров: <b>{len(new_ids)}</b>. Скачиваю документы и собираю Excel/HTML…",
                    "",
                ]
                for tid in sorted(new_ids)[:30]:
                    head.append(f"• № <code>{html.escape(tid)}</code>")
                if len(new_ids) > 30:
                    head.append(f"… и ещё {len(new_ids) - 30}")
                send_message(tok, chat, "\n".join(head))
            elif len(unique) == 0:
                send_message(
                    tok,
                    chat,
                    "⚠️ <b>Плановый прогон</b>\n"
                    "Поиск на zakupki.gov.ru не вернул ни одной карточки тендера (0 в выдаче).\n"
                    "Частая причина — <b>VPN</b> или нестабильный доступ к ЕИС.\n"
                    "Рекомендация: запускать парсинг <b>без VPN</b>, а для Telegram задать в .env "
                    "<code>TELEGRAM_HTTPS_PROXY</code> (прокси только для api.telegram.org).",
                )
            elif len(filtered) == 0:
                send_message(
                    tok,
                    chat,
                    "📋 <b>Плановый прогон</b>\n"
                    f"С поиска получено уникальных записей: <b>{len(unique)}</b>, "
                    "но ни одна не прошла фильтры (этап «Подача заявок», цена, дата).\n"
                    "<i>Это не «ничего не найдено на ЕИС», а отсев по правилам.</i>",
                )
            else:
                send_message(
                    tok,
                    chat,
                    "📋 <b>Плановый прогон</b>\n"
                    f"В текущей выборке после фильтров: <b>{len(filtered)}</b> тендер(ов). "
                    f"Новых (ещё нет в <code>tenders.json</code>): <b>0</b> — все уже в системе.\n"
                    "<i>Поиск отработал; ниже в этом прогоне при необходимости обновятся сметы по этим номерам. "
                    "Сравнение цен по расписанию пойдёт только если в этом запуске появятся новые id.</i>",
                )
        except Exception as e:
            print(f"Telegram: не удалось отправить сводку в начале прогона ({e})")
            print(
                "Подсказка: если ЕИС без VPN, а Telegram только с VPN — задайте TELEGRAM_HTTPS_PROXY в .env "
                "и оставьте системный трафик к zakupki.gov.ru без VPN."
            )

    emit_new = (args.emit_new_ids_to or "").strip()
    if emit_new:
        emit_path = Path(emit_new)
        emit_path.parent.mkdir(parents=True, exist_ok=True)
        emit_path.write_text("\n".join(sorted(new_ids)) + ("\n" if new_ids else ""), encoding="utf-8")
        print(f"Список новых id записан: {emit_path} ({len(new_ids)} шт.)")

    remaining = [t for t in filtered if t.tender_id not in completed_ids]
    if resumed_from_checkpoint and completed_ids:
        print(f"Возобновляем обработку документов: осталось {len(remaining)} из {len(filtered)}.")
    if resumed_from_checkpoint and not remaining:
        print("По чекпоинту новых шагов не осталось — очищаем сохранённый прогресс.")
        _clear_search_checkpoint(out_paths)
        return

    all_rows: list[dict] = []
    for idx, tender in enumerate(remaining, start=1):
        absolute_idx = len(completed_ids) + idx
        print(f"[{absolute_idx}/{len(filtered)}] {tender.tender_id}: скачивание документов")
        downloaded_files = open_tender_and_download_archives(tender, out_paths["downloads"])
        if not downloaded_files:
            if tg_cfg and tender.tender_id in new_ids:
                tok, chat = tg_cfg
                safe_notify_new_tender(
                    token=tok,
                    chat_id=chat,
                    tender_id=tender.tender_id,
                    title=tender.title,
                    region=tender.region,
                    url=tender.url,
                    price_rub=tender.price_rub,
                    xlsx_path=None,
                    positions_count=0,
                    sum_positions=0.0,
                    top_work_lines=["(документы не скачались — проверь доступ к ЕИС)"],
                )
            completed_ids.add(tender.tender_id)
            _save_search_checkpoint(
                out_paths,
                args,
                filtered=filtered,
                completed_ids=completed_ids,
                new_ids=new_ids,
                search_total=search_total,
                completed=False,
            )
            continue
        tender_dl = out_paths["downloads"] / tender.tender_id
        ext_root = out_paths["extracted"] / tender.tender_id
        seeds = archive_seeds_for_tender(tender_dl, ext_root)
        extracted = extract_archives_nested(seeds, out_paths["extracted"]) if seeds else []
        direct_excel = [p for p in downloaded_files if p.exists() and is_excel_file(p)]
        extracted_excel = [p for p in extracted if p.exists() and is_excel_file(p)]
        direct_pdf = [p for p in downloaded_files if p.exists() and is_pdf_file(p)]
        extracted_pdf = [p for p in extracted if p.exists() and is_pdf_file(p)]
        excel_files = unique_paths_preserve_order(extracted_excel + direct_excel)
        pdf_files = unique_paths_preserve_order(extracted_pdf + direct_pdf)

        # В отчет по сметам берем только файлы, похожие на сметные.
        estimate_excel_files = [p for p in excel_files if is_estimate_excel(p)]
        # Fallback: если не нашли по имени, берем все Excel, чтобы не потерять данные.
        if not estimate_excel_files:
            estimate_excel_files = excel_files

        estimate_excel_files = sort_excel_for_lsr_priority(estimate_excel_files)
        tender_rows: list[dict] = []
        for excel_path in estimate_excel_files:
            rows = extract_rows_from_excel(excel_path, tender)
            tender_rows.extend(rows)
        for pdf_path in pdf_files:
            tender_rows.extend(extract_rows_from_pdf(pdf_path, tender))
        if not tender_rows:
            rar_cnt = sum(1 for p in tender_dl.rglob("*") if p.is_file() and p.suffix.lower() == ".rar")
            extracted_xlsx = [p for p in extracted if p.exists() and is_excel_file(p)] if extracted else []
            if rar_cnt and not extracted_xlsx:
                print(
                    f"  -> {tender.tender_id}: RAR ({rar_cnt} шт.) не распакованы в Excel — проверьте 7-Zip в PATH."
                )
            elif rar_cnt:
                print(f"  -> {tender.tender_id}: RAR распакованы, но строк ЛСР в Excel нет.")
            _print_no_estimate_files_summary(tender.tender_id, tender_dl)
            if tg_cfg:
                tok, chat = tg_cfg
                try:
                    _send_no_estimate_files_summary_to_tg(tok, chat, tender.tender_id, tender.title, tender_dl)
                except Exception as e:
                    print(f"Telegram: не удалось отправить список файлов по {tender.tender_id}: {e}")

        all_rows.extend(tender_rows)
        tender_report, clean_df = write_tender_estimate_report(tender, tender_rows, out_paths)
        tender_html = write_tender_estimate_html(tender, clean_df, out_paths)
        print(f"  -> Отчет по сметам (Excel): {tender_report} (позиций в отчёте: {len(clean_df)}, исходно строк: {len(tender_rows)})")
        print(f"  -> Отчет по сметам (HTML): {tender_html}")

        if tg_cfg and tender.tender_id in new_ids:
            tok, chat = tg_cfg
            sum_pos = float(clean_df["Сумма, руб"].sum()) if not clean_df.empty else 0.0
            xlsx_p = out_paths["reports"] / f"ОТЧЕТ_ПО_СМЕТАМ_{tender.tender_id}.xlsx"
            safe_notify_new_tender(
                token=tok,
                chat_id=chat,
                tender_id=tender.tender_id,
                title=tender.title,
                region=tender.region,
                url=tender.url,
                price_rub=tender.price_rub,
                xlsx_path=xlsx_p if xlsx_p.is_file() else None,
                positions_count=len(clean_df),
                sum_positions=sum_pos,
                top_work_lines=_top_work_snippets_from_clean_df(clean_df),
            )
        completed_ids.add(tender.tender_id)
        _save_search_checkpoint(
            out_paths,
            args,
            filtered=filtered,
            completed_ids=completed_ids,
            new_ids=new_ids,
            search_total=search_total,
            completed=False,
        )

    # Общий файл по всем сметам (без отдельного отчета по тендерам).
    combined_path = out_paths["reports"] / f"ОТЧЕТ_ПО_СМЕТАМ_ОБЩИЙ_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    combined_df = pd.DataFrame(all_rows) if all_rows else pd.DataFrame(
        columns=[
            "tender_id",
            "region",
            "tender_title",
            "tender_url",
            "source_file",
            "work_name",
            "price_from_estimate_rub",
        ]
    )
    combined_df.to_excel(combined_path, index=False)
    print(f"Готово. Общий отчет по сметам: {combined_path}")
    print(f"Всего позиций из смет: {len(all_rows)}")
    _clear_search_checkpoint(out_paths)


if __name__ == "__main__":
    main()
