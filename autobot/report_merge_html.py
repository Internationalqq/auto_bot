"""
Веб-отчёт по сводке смета + рынок: три карточки (сравнение, источники, смета).

Пишет data/reports_site/<tender_id>/index.html.
Открытие: тот же сервер, что web_ui.py → http://127.0.0.1:8765/merge-report/<id>/
В .env REPORT_SITE_PUBLIC_BASE_URL для ссылки в Telegram (см. scheduled_pipeline).
"""

from __future__ import annotations

from autobot.paths import REPO_ROOT

import html as html_mod
import json
import math
import re
import statistics
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

import pandas as pd

from autobot.market_analytics import (
    COL_NAME,
    COL_ITEM,
    COL_QTY,
    COL_SUM,
    COL_UNIT,
    COL_UNIT_PRICE,
    estimate_block_qty_from_unit,
    recalc_estimate_qty_price_from_unit,
    unit_has_area_or_volume_marker,
)
from autobot.merge_estimate_market import OUT_PREFIX, refresh_svodka_if_market_newer
from autobot.report_prompt import DATA_DIR, REPORTS_DIR, load_tender_metadata
from autobot.text_contacts import collect_phones, collect_urls

REPORTS_SITE_DIR = DATA_DIR / "reports_site"


def _cell(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "—"
    s = str(val).strip()
    return html_mod.escape(s) if s else "—"


def _cell_estimate_numeric(val) -> str:
    """Кол-во / цена / сумма в таблице сметы — компактно, без хвоста float."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "—"
    if isinstance(val, (int, float)) and not (isinstance(val, float) and pd.isna(val)):
        fv = float(val)
        if math.isfinite(fv):
            if abs(fv - round(fv)) < 1e-6 and abs(fv) < 1e12:
                s = str(int(round(fv)))
            else:
                s = f"{fv:.2f}".replace(".", ",")
            return html_mod.escape(s)
    return _cell(val)


def _split_semicolon_values(text: str) -> list[str]:
    if not text:
        return []
    raw = [x.strip() for x in str(text).split(";")]
    return [x for x in raw if x]


def _contacts_from_text(text: str) -> str:
    if not text or not str(text).strip():
        return "—"
    t = str(text)
    urls = collect_urls(t)[:6]
    phones = collect_phones(t)[:6]
    parts: list[str] = []
    if phones:
        parts.append("📞 " + html_mod.escape(", ".join(phones[:4])))
    if urls:
        safe_urls = []
        for u in urls[:4]:
            safe_urls.append(f'<a href="{html_mod.escape(u, quote=True)}" target="_blank" rel="noopener">{html_mod.escape(u[:64])}{"…" if len(u) > 64 else ""}</a>')
        parts.append("🌐 " + "<br>".join(safe_urls))
    return "<br>".join(parts) if parts else "—"


# «100 м²» в ед. изм. — рыночные ответы чаще за 1 м²; для сравнения с позицией сметы масштабируем.
_UNIT_LEADING_MV = re.compile(
    r"^\s*([\d\s\u00a0\u202f,]+)\s*(м\s*[2²]|м\s*[3³]|п\.?\s*м\.?)\s*$",
    re.IGNORECASE,
)


def _quantity_multiplier_from_unit(unit_raw: str) -> tuple[float, str]:
    """
    Возвращает (N, подпись_единицы) из строки вида «100 м2» / «10 м3».
    Если формата нет — (1.0, '').
    """
    u = (unit_raw or "").strip().replace("\xa0", " ").replace("\u202f", " ")
    if not u:
        return 1.0, ""
    m = _UNIT_LEADING_MV.match(u)
    if not m:
        return 1.0, ""
    num_s = re.sub(r"[\s\u00a0\u202f]+", "", m.group(1)).replace(",", ".")
    try:
        v = float(num_s)
    except ValueError:
        return 1.0, ""
    if v <= 1.0:
        return 1.0, ""
    raw_tag = (m.group(2) or "").strip().lower()
    raw_tag = re.sub(r"\s+", "", raw_tag)
    if "м" in raw_tag and ("2" in raw_tag or "²" in raw_tag):
        label = "м²"
    elif "м" in raw_tag and ("3" in raw_tag or "³" in raw_tag):
        label = "м³"
    elif "п" in raw_tag:
        label = "п.м."
    else:
        label = raw_tag
    return v, label


def _safe_float(x) -> float | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v <= 0:  # noqa: PLR0124
        return None
    return v


def _smeta_unit_display(row: pd.Series) -> str:
    """
    Цена за ед. для сравнения: из колонки сметы, но если она явно бредовая относительно Сумма/Кол-во — берём Сумма÷Кол-во.

    Для «100 м²» и т.п.: если в колонке «Кол-во» ошибочно попало число норматива (100), то Сумма/Кол-во
    даёт цену за 1 м² (~415), а «Цена за ед.» в отчёте — тариф за 100 м² (~11 865). Раньше подмена
    по диапазону относительно derived как раз затирала тариф; в этом случае колонку не подменяем.
    """
    unit_raw = row.get(COL_UNIT)
    unit = "" if pd.isna(unit_raw) else str(unit_raw).strip()
    norm_block: float | None = None
    if unit_has_area_or_volume_marker(unit):
        norm_block = estimate_block_qty_from_unit(unit)

    up = _safe_float(row.get(COL_UNIT_PRICE))
    sm = _safe_float(row.get(COL_SUM))
    q = _safe_float(row.get(COL_QTY))
    derived = (sm / q) if (sm is not None and q is not None and q > 0) else None
    use = up
    # Для позиций с нормативом в ед. изм. (напр. "100 м²") в сводку часто попадает
    # цена за 1 м² (Сумма/100). В карточке "Сравнение" рынок показан "за позицию",
    # поэтому здесь берём сумму позиции, чтобы сравнение было в одной шкале.
    if (
        sm is not None
        and sm > 0
        and unit_has_area_or_volume_marker(unit)
        and norm_block is not None
        and norm_block >= 10.0
        and q is not None
        and abs(q - norm_block) / norm_block <= 0.05
    ):
        use = sm
    if derived is not None and derived > 0:
        if use is None:
            use = derived
        elif use is not None and use > 0:
            qty_is_normative_block = (
                norm_block is not None
                and norm_block >= 10.0
                and q is not None
                and abs(q - norm_block) / norm_block <= 0.05
            )
            if not qty_is_normative_block:
                lo, hi = derived * 0.15, derived * 25.0
                if use < lo or use > hi:
                    use = derived
    if use is None or use <= 0:
        return "—"
    s = f"{use:.2f}".replace(".", ",")
    return html_mod.escape(s)


def _parse_semicolon_numbers(text: str) -> list[float]:
    if not text or not str(text).strip():
        return []
    out: list[float] = []
    for part in re.split(r"\s*;\s*", str(text).strip()):
        t = re.sub(r"[\s\u00a0\u202f]+", "", part.replace(",", "."))
        if not t:
            continue
        try:
            v = float(t)
        except ValueError:
            continue
        if v > 0 and v < 1e10:
            out.append(v)
    return out


def _median_unit_float(median_raw: object) -> float | None:
    """Число из колонки «Медиана цена за ед. (рынок)» — цена за единицу, без умножения на объём."""
    try:
        if median_raw is None or (isinstance(median_raw, float) and pd.isna(median_raw)):
            return None
        s = str(median_raw).strip()
        if not s or s in ("—", "-"):
            return None
        v = float(s.replace(" ", "").replace(",", "."))
        if v > 0 and v < 1e10 and math.isfinite(v):
            return v
    except (TypeError, ValueError):
        pass
    return None


def _market_prices_human(
    text: str,
    *,
    qty_scale: float = 1.0,
    unit_label: str = "",
) -> str:
    """
    Несколько чисел из Алисы — медиана и диапазон.
    Если qty_scale>1 (напр. 100 м² в ед. изм.), цены с сайтов считаем за 1 ед. и умножаем на объём для сравнения с позицией сметы.
    """
    if not text or not str(text).strip() or str(text).strip() in ("—", "-"):
        return "—"
    raw = str(text).strip()
    nums = _parse_semicolon_numbers(raw)
    if not nums:
        return html_mod.escape(raw)
    if qty_scale and qty_scale > 1.0:
        nums = [round(n * qty_scale, 2) for n in nums]
    nums.sort()
    med = statistics.median(nums)
    lo, hi = nums[0], nums[-1]
    med_s = f"{med:,.0f}".replace(",", " ")
    tip = "Медиана между найденными ценами."
    if qty_scale > 1.0 and unit_label:
        tip = f"На сайтах за 1 {unit_label}; здесь ×{qty_scale:g} под объём в смете."
    elif qty_scale > 1.0:
        tip = f"Цены с сайтов умножены на {qty_scale:g} под объём в смете."
    if len(nums) == 1:
        suf = ""
        if qty_scale > 1.0 and unit_label:
            suf = f' <span class="muted">(×{qty_scale:g} {html_mod.escape(unit_label)})</span>'
        elif qty_scale > 1.0:
            suf = f' <span class="muted">(×{qty_scale:g})</span>'
        return (
            f"<span title=\"{html_mod.escape(tip)}\">"
            f"≈ {html_mod.escape(med_s)} ₽{suf}</span>"
        )
    lo_s = f"{lo:,.0f}".replace(",", " ")
    hi_s = f"{hi:,.0f}".replace(",", " ")
    suf = ""
    if qty_scale > 1.0 and unit_label:
        suf = f' <span class="muted">(×{qty_scale:g} {html_mod.escape(unit_label)})</span>'
    elif qty_scale > 1.0:
        suf = f' <span class="muted">(×{qty_scale:g})</span>'
    return (
        f"<span title=\"{html_mod.escape(tip)}\">"
        f"≈ {html_mod.escape(med_s)} ₽"
        f'<span class="muted"> ({html_mod.escape(lo_s)}…{html_mod.escape(hi_s)})</span>{suf}</span>'
    )


def _market_prices_list_human(
    text: str,
    *,
    qty_scale: float = 1.0,
    unit_label: str = "",
) -> str:
    """Показывает все найденные цены (в одной шкале сравнения), без свёртки в медиану."""
    if not text or not str(text).strip() or str(text).strip() in ("—", "-"):
        return "—"
    nums = _parse_semicolon_numbers(str(text).strip())
    if not nums:
        return html_mod.escape(str(text).strip())
    if qty_scale and qty_scale > 1.0:
        nums = [round(n * qty_scale, 2) for n in nums]
    nums = sorted({float(x) for x in nums if x > 0})
    if not nums:
        return "—"
    parts = [f"{v:,.0f}".replace(",", " ") for v in nums[:12]]
    tail = ""
    if len(nums) > 12:
        tail = f' <span class="muted">(+{len(nums)-12})</span>'
    suf = ""
    if qty_scale > 1.0 and unit_label:
        suf = f' <span class="muted">(×{qty_scale:g} {html_mod.escape(unit_label)})</span>'
    elif qty_scale > 1.0:
        suf = f' <span class="muted">(×{qty_scale:g})</span>'
    return html_mod.escape("; ".join(parts)) + tail + suf


def _market_median_human(
    *,
    raw_prices_text: str,
    median_from_col,
    qty_scale: float = 1.0,
    unit_label: str = "",
) -> str:
    """
    Отдельная колонка медианы:
    - приоритет: «Медиана цена за ед. (рынок)» из файла Алисы;
    - fallback: медиана по «Рынок цены за ед. (итог)».
    """
    med_val: float | None = None
    try:
        if median_from_col is not None and not (isinstance(median_from_col, float) and pd.isna(median_from_col)):
            med_val = float(str(median_from_col).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        med_val = None
    if med_val is None:
        nums = _parse_semicolon_numbers(raw_prices_text or "")
        if nums:
            med_val = float(statistics.median(nums))
    if med_val is None or med_val <= 0:
        return "—"
    if qty_scale and qty_scale > 1.0:
        med_val *= qty_scale
    med_s = f"{med_val:,.0f}".replace(",", " ")
    suf = ""
    if qty_scale > 1.0 and unit_label:
        suf = f' <span class="muted">(×{qty_scale:g} {html_mod.escape(unit_label)})</span>'
    elif qty_scale > 1.0:
        suf = f' <span class="muted">(×{qty_scale:g})</span>'
    return f"≈ {html_mod.escape(med_s)} ₽{suf}"


def _contacts_from_structured(phones_text: str, urls_text: str) -> str:
    phones = _split_semicolon_values(phones_text)[:6]
    urls = _split_semicolon_values(urls_text)[:6]
    parts: list[str] = []
    if phones:
        parts.append("📞 " + html_mod.escape(", ".join(phones[:4])))
    if urls:
        safe_urls = []
        for u in urls[:4]:
            safe_urls.append(
                f'<a href="{html_mod.escape(u, quote=True)}" target="_blank" rel="noopener">{html_mod.escape(u[:64])}{"…" if len(u) > 64 else ""}</a>'
            )
        parts.append("🌐 " + "<br>".join(safe_urls))
    return "<br>".join(parts) if parts else "—"


def _rows_from_bundle_or_fallback(
    *,
    bundle_json: str,
    qty_scale: float,
    fallback_prices_text: str,
    fallback_phones_text: str,
    fallback_urls_text: str,
    market_full_text: str = "",
    median_unit_raw: object = None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    try:
        data = json.loads(bundle_json) if bundle_json and str(bundle_json).strip() else []
        if isinstance(data, list):
            for it in data:
                if not isinstance(it, dict):
                    continue
                p_raw = str(it.get("price", "") or "").strip()
                u_raw = str(it.get("url", "") or "").strip()
                ph_raw = str(it.get("phone", "") or "").strip()
                title_raw = str(it.get("title", "") or "").strip()
                source_raw = str(it.get("source", "") or "").strip()
                price_txt = ""
                if p_raw:
                    try:
                        pv = float(p_raw.replace(" ", "").replace(",", "."))
                        if qty_scale and qty_scale > 1.0:
                            pv *= qty_scale
                        price_txt = f"{pv:,.0f}".replace(",", " ") + " ₽"
                    except (TypeError, ValueError):
                        price_txt = p_raw
                rows.append({"price": price_txt, "phone": ph_raw, "url": u_raw, "title": title_raw, "source": source_raw})
    except (TypeError, ValueError, json.JSONDecodeError):
        rows = []

    if rows:
        pool_u = collect_urls(market_full_text)
        pool_p = collect_phones(market_full_text)
        nums_fb = _parse_semicolon_numbers(fallback_prices_text or "")
        if not nums_fb:
            mu = _median_unit_float(median_unit_raw)
            if mu is not None:
                nums_fb = [mu]
        if qty_scale and qty_scale > 1.0:
            nums_fb = [round(x * qty_scale, 2) for x in nums_fb]
        price_pool = [f"{v:,.0f}".replace(",", " ") + " ₽" for v in nums_fb[:12]]
        iu = ip = ipr = 0
        for r in rows:
            if not str(r.get("url", "") or "").strip() and iu < len(pool_u):
                r["url"] = pool_u[iu]
                iu += 1
            if not str(r.get("phone", "") or "").strip() and ip < len(pool_p):
                r["phone"] = pool_p[ip]
                ip += 1
            if not str(r.get("price", "") or "").strip():
                if ipr < len(price_pool):
                    r["price"] = price_pool[ipr]
                    ipr += 1
                elif price_pool:
                    r["price"] = price_pool[-1]
        return rows[:12]

    # fallback: подхватываем отдельные списки и выравниваем по индексу
    prices = _parse_semicolon_numbers(fallback_prices_text or "")
    if qty_scale and qty_scale > 1.0:
        prices = [round(x * qty_scale, 2) for x in prices]
    prices_txt = [f"{v:,.0f}".replace(",", " ") + " ₽" for v in prices[:12]]
    phones = _split_semicolon_values(fallback_phones_text or "")[:12]
    urls = _split_semicolon_values(fallback_urls_text or "")[:12]
    if (market_full_text or "").strip():
        for u in collect_urls(market_full_text):
            if u not in urls:
                urls.append(u)
        for p in collect_phones(market_full_text):
            if p not in phones:
                phones.append(p)
        phones = phones[:24]
        urls = urls[:24]
    n = max(len(prices_txt), len(phones), len(urls))
    out: list[dict[str, str]] = []
    for i in range(n):
        out.append(
            {
                "price": prices_txt[i] if i < len(prices_txt) else "",
                "phone": phones[i] if i < len(phones) else "",
                "url": urls[i] if i < len(urls) else "",
                "title": "",
                "source": "",
            }
        )
    return out


def _bundle_col_prices_html(rows: list[dict[str, str]], *, qty_scale: float = 1.0, unit_label: str = "") -> str:
    if not rows:
        return "—"
    lines: list[str] = []
    for i, it in enumerate(rows[:12], start=1):
        p = str(it.get("price", "") or "").strip() or "—"
        lines.append(f'<div class="psp-row"><span class="psp-i">{i}.</span> <span class="psp-price">{html_mod.escape(p)}</span></div>')
    if qty_scale > 1.0 and unit_label:
        lines.append(f'<div class="muted">×{qty_scale:g} {html_mod.escape(unit_label)} к смете</div>')
    return "".join(lines)


def _bundle_col_phones_html(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "—"
    lines: list[str] = []
    for i, it in enumerate(rows[:12], start=1):
        ph = str(it.get("phone", "") or "").strip() or "—"
        lines.append(f'<div class="psp-row"><span class="psp-i">{i}.</span> <span class="psp-phone">{html_mod.escape(ph)}</span></div>')
    return "".join(lines)


def _bundle_focus_html(rows: list[dict[str, str]], *, qty_scale: float = 1.0, unit_label: str = "") -> str:
    """Основной визуальный блок: цена + телефон + сайт в одной строке источника."""
    if not rows:
        return "—"
    parts: list[str] = []
    for i, it in enumerate(rows[:12], start=1):
        p = str(it.get("price", "") or "").strip() or "—"
        ph = str(it.get("phone", "") or "").strip() or "—"
        u = str(it.get("url", "") or "").strip()
        title = str(it.get("title", "") or "").strip()
        source = str(it.get("source", "") or "").strip()
        label = title or u
        if u:
            u_cell = (
                f'<a href="{html_mod.escape(u, quote=True)}" target="_blank" rel="noopener">'
                f"{html_mod.escape(label[:82])}{'…' if len(label) > 82 else ''}</a>"
            )
        else:
            u_cell = html_mod.escape(title) if title else "—"
        if source:
            u_cell = f'<span class="src-source">{html_mod.escape(source)}</span> {u_cell}'
        parts.append(
            "<div class='src-row'>"
            f"<span class='src-idx'>{i}.</span>"
            f"<span class='src-chip src-chip--price'>💰 {html_mod.escape(p)}</span>"
            f"<span class='src-chip src-chip--phone'>📞 {html_mod.escape(ph)}</span>"
            f"<span class='src-chip src-chip--site'>🌐 {u_cell}</span>"
            "</div>"
        )
    if qty_scale > 1.0 and unit_label:
        parts.append(
            f'<div class="muted" style="margin-top:.35rem;">×{qty_scale:g} {html_mod.escape(unit_label)} к смете</div>'
        )
    return "".join(parts)


def _bundle_col_urls_html(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "—"
    lines: list[str] = []
    for i, it in enumerate(rows[:12], start=1):
        u = str(it.get("url", "") or "").strip()
        title = str(it.get("title", "") or "").strip()
        source = str(it.get("source", "") or "").strip()
        label = title or u
        if u:
            u_cell = (
                f'<a href="{html_mod.escape(u, quote=True)}" target="_blank" rel="noopener">'
                f"{html_mod.escape(label[:72])}{'…' if len(label) > 72 else ''}</a>"
            )
        else:
            u_cell = html_mod.escape(title) if title else "—"
        if source:
            u_cell = f'<span class="src-source">{html_mod.escape(source)}</span> {u_cell}'
        lines.append(f'<div class="psp-row"><span class="psp-i">{i}.</span> <span class="psp-link">{u_cell}</span></div>')
    return "".join(lines)


def _render_html(
    tender_id: str,
    tender_title: str,
    df: pd.DataFrame,
    publish_date: str = "",
    *,
    viability_html: str = "",
) -> str:
    market_col = "Рыночные источники" if "Рыночные источники" in df.columns else ""
    market_full_col = "Рыночные источники (полный текст)" if "Рыночные источники (полный текст)" in df.columns else market_col
    if "Рынок цены за ед. (итог)" in df.columns:
        rub_col = "Рынок цены за ед. (итог)"
    elif "Суммы из ответа (итог)" in df.columns:
        rub_col = "Суммы из ответа (итог)"
    else:
        rub_col = "Суммы из текста ответа (авто)"
    err_col = "Ошибка / статус"

    def _clean_legacy_status(val: object) -> str:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return ""
        s = str(val).strip()
        if not s:
            return ""
        up = s.upper()
        old_engine_token = "".join(["ALI", "CE_"])
        if old_engine_token in up or "DEFAULT_INPUT_SELECTORS" in up:
            return ""
        return s

    def _has_text_value(val: object) -> bool:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return False
        s = str(val).strip()
        return bool(s and s.casefold() not in ("nan", "none", "—", "-", "н/д", "нет"))

    def _row_has_partial_market(row: pd.Series) -> bool:
        for c in [
            rub_col,
            "Цены за ед. (рынок, руб)",
            "Медиана цена за ед. (рынок)",
            "Цена-сайт-телефон (json)",
            "Ссылки (строго)",
            "Телефоны (строго)",
            "Рынок обработано",
            "Рыночные источники",
            "Рыночные источники (полный текст)",
        ]:
            if c in df.columns and _has_text_value(row.get(c, "")):
                return True
        return False

    df = df.copy()
    df["__has_partial_market"] = df.apply(_row_has_partial_market, axis=1)
    df["__orig_order"] = range(len(df))
    ready_count = int(df["__has_partial_market"].sum())
    total_count = int(len(df))
    df = df.sort_values(["__has_partial_market", "__orig_order"], ascending=[False, True])

    rows_compare: list[str] = []
    rows_market: list[str] = []
    rows_est: list[str] = []

    est_cols = [c for c in [COL_ITEM, COL_NAME, COL_UNIT, COL_QTY, COL_UNIT_PRICE, COL_SUM] if c in df.columns]

    for _, row in df.iterrows():
        row_cls = " class='row-ready'" if bool(row.get("__has_partial_market", False)) else ""
        item_no = _cell(row.get(COL_ITEM, "")) if COL_ITEM in df.columns else "—"
        name = _cell(row.get(COL_NAME, ""))
        sum_smeta = _smeta_unit_display(row)
        raw_rub = row.get(rub_col, "") if rub_col in df.columns else ""
        q_scale, u_lbl = _quantity_multiplier_from_unit(str(row.get(COL_UNIT, "") or ""))
        rub_med = _market_median_human(
            raw_prices_text="" if pd.isna(raw_rub) else str(raw_rub),
            median_from_col=row.get("Медиана цена за ед. (рынок)", ""),
            qty_scale=q_scale,
            unit_label=u_lbl,
        )
        market_raw = row.get(market_col, "") if market_col else ""
        market_full_raw = row.get(market_full_col, "") if market_full_col else ""
        phones_raw = "" if pd.isna(row.get("Телефоны (строго)", "")) else str(row.get("Телефоны (строго)", ""))
        urls_raw = "" if pd.isna(row.get("Ссылки (строго)", "")) else str(row.get("Ссылки (строго)", ""))
        phones_struct = _cell(phones_raw) if "Телефоны (строго)" in df.columns else "—"
        urls_struct = _cell(urls_raw) if "Ссылки (строго)" in df.columns else "—"
        if phones_struct == "—" and urls_struct == "—":
            # старые выгрузки без структурных колонок: пробуем взять из текста ответа
            txt = "" if pd.isna(market_raw) else str(market_raw)
            phones_raw = "; ".join(collect_phones(txt))
            urls_raw = "; ".join(collect_urls(txt))
        market_full_txt = _clean_legacy_status(market_full_raw)
        market_preview_txt = market_full_txt.strip() if market_full_txt.strip() else ("" if pd.isna(market_raw) else str(market_raw))
        market_preview_txt = _clean_legacy_status(market_preview_txt)
        market_preview_short = market_preview_txt[:420] + ("…" if len(market_preview_txt) > 420 else "")
        market_preview_html = html_mod.escape(market_preview_short) if market_preview_short else "—"
        market_full_html = html_mod.escape(market_preview_txt) if market_preview_txt else "—"
        bundle_rows = _rows_from_bundle_or_fallback(
            bundle_json="" if pd.isna(row.get("Цена-сайт-телефон (json)", "")) else str(row.get("Цена-сайт-телефон (json)", "")),
            qty_scale=q_scale,
            fallback_prices_text="" if pd.isna(raw_rub) else str(raw_rub),
            fallback_phones_text=phones_raw,
            fallback_urls_text=urls_raw,
            market_full_text=market_preview_txt,
            median_unit_raw=row.get("Медиана цена за ед. (рынок)", ""),
        )
        source_focus_col = _bundle_focus_html(bundle_rows, qty_scale=q_scale, unit_label=u_lbl)

        rows_compare.append(
            f"<tr{row_cls}><td>{item_no}</td><td>{name}</td><td>{sum_smeta}</td><td class='sources-col'>{source_focus_col}</td><td>{rub_med}</td></tr>"
        )

        err = _cell(_clean_legacy_status(row.get(err_col, ""))) if err_col in df.columns else ""
        rub_raw_cell = _cell(row.get(rub_col, "")) if rub_col in df.columns else "—"
        rows_market.append(
            f"<tr{row_cls}><td>{item_no}</td><td>{name}</td><td class='market-text'><details class='market-details'><summary>{market_preview_html}</summary>"
            f"<div class='market-scroll'>{market_full_html}</div></details></td>"
            f"<td>{rub_raw_cell}</td><td>{err}</td></tr>"
        )

        tds = []
        for c in est_cols:
            raw = row.get(c, "")
            if c in (COL_QTY, COL_UNIT_PRICE, COL_SUM):
                tds.append(f"<td>{_cell_estimate_numeric(raw)}</td>")
            else:
                tds.append(f"<td>{_cell(raw)}</td>")
        rows_est.append(f"<tr{row_cls}>" + "".join(tds) + "</tr>")

    th_est = "".join(f"<th>{html_mod.escape(c)}</th>" for c in est_cols)

    title_esc = html_mod.escape(tender_title)
    tid_esc = html_mod.escape(tender_id)
    tid_js = json.dumps(tender_id, ensure_ascii=False)
    pub_raw = (publish_date or "").strip()
    pub_esc = html_mod.escape(pub_raw) if pub_raw else "—"
    partial_note = (
        f"Найдено/обработано строк: <b>{ready_count}</b> из <b>{total_count}</b>. "
        "Сохранённые строки рынка подняты наверх; если объявления/сайты найдены, они показаны сразу."
    )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Смета и рынок · {tid_esc}</title>
  <style>
    :root {{
      --bg: #0f1419;
      --card: #1a222d;
      --card2: #232d3b;
      --text: #e8edf4;
      --muted: #8b9cb3;
      --teal: #2dd4bf;
      --violet: #a78bfa;
      --amber: #fbbf24;
      --border: rgba(255,255,255,.08);
      --radius: 18px;
      --shadow: 0 12px 40px rgba(0,0,0,.45);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Segoe UI", system-ui, sans-serif;
      background: radial-gradient(1200px 600px at 10% -10%, #1e293b 0%, transparent 50%),
                  radial-gradient(900px 500px at 90% 0%, #312e81 0%, transparent 45%),
                  var(--bg);
      color: var(--text);
      line-height: 1.45;
    }}
    .wrap {{ max-width: 1400px; margin: 0 auto; padding: 1.5rem 1rem 3rem; }}
    header {{
      text-align: center;
      margin-bottom: 2rem;
      animation: fadeIn .8s ease both;
    }}
    header h1 {{ font-size: 1.35rem; font-weight: 600; margin: 0 0 .35rem; }}
    header p {{ margin: 0; color: var(--muted); font-size: .95rem; }}
    .deck {{
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 1.25rem;
      align-items: start;
      margin-bottom: 1rem;
    }}
    @media (max-width: 1100px) {{
      .deck {{ grid-template-columns: 1fr; }}
    }}
    .card {{
      background: linear-gradient(160deg, var(--card) 0%, var(--card2) 100%);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
      cursor: pointer;
      transition: transform .35s cubic-bezier(.34,1.56,.64,1), box-shadow .35s ease, border-color .35s ease, background .35s ease;
      animation: rise .7s ease both;
    }}
    .card:nth-child(1) {{ animation-delay: .05s; }}
    .card:nth-child(2) {{ animation-delay: .12s; }}
    .card:nth-child(3) {{ animation-delay: .19s; }}
    .card:hover {{
      transform: translateY(-8px) scale(1.01);
      box-shadow: 0 20px 50px rgba(0,0,0,.55);
      border-color: rgba(255,255,255,.18);
    }}
    .card.active {{
      border-color: rgba(255,255,255,.3);
      box-shadow: 0 20px 50px rgba(0,0,0,.6);
      transform: translateY(-4px);
    }}
    .card__head {{
      padding: 1rem 1.1rem;
      font-weight: 700;
      font-size: .8rem;
      letter-spacing: .06em;
      text-transform: uppercase;
      border-bottom: 1px solid var(--border);
    }}
    .card--compare .card__head {{ color: var(--teal); background: rgba(45,212,191,.08); }}
    .card--market .card__head {{ color: var(--violet); background: rgba(167,139,250,.1); }}
    .card--estimate .card__head {{ color: var(--amber); background: rgba(251,191,36,.1); }}
    .card__body {{ padding: .8rem 1rem 1rem; color: var(--muted); font-size: .88rem; }}
    .viewer {{
      background: linear-gradient(160deg, var(--card) 0%, var(--card2) 100%);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
      animation: fadeIn .35s ease both;
    }}
    .viewer__head {{
      padding: .9rem 1rem;
      border-bottom: 1px solid var(--border);
      color: var(--muted);
      font-size: .85rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }}
    .viewer__hint {{ font-size: .78rem; opacity: .9; }}
    .live-note {{
      margin: 0 0 1rem;
      padding: .75rem .9rem;
      border: 1px solid rgba(45,212,191,.22);
      border-radius: 14px;
      background: rgba(45,212,191,.07);
      color: #cfe7e2;
      font-size: .92rem;
      line-height: 1.45;
    }}
    .report-actions {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: .55rem;
      margin: 0 0 1rem;
    }}
    .report-btn {{
      border: 1px solid rgba(45,212,191,.32);
      background: rgba(45,212,191,.09);
      color: #d7fff3;
      border-radius: 10px;
      padding: .55rem .75rem;
      cursor: pointer;
      font-size: .86rem;
      font-weight: 650;
    }}
    .report-btn:hover {{ background: rgba(45,212,191,.15); }}
    .report-btn.secondary {{
      border-color: rgba(255,255,255,.13);
      background: rgba(255,255,255,.05);
      color: #dbe7f7;
    }}
    .report-status {{
      color: var(--muted);
      font-size: .84rem;
      line-height: 1.35;
    }}
    .site-log-fab {{
      position: fixed; right: 18px; bottom: 18px; z-index: 50;
      width: 54px; height: 54px; border-radius: 999px;
      border: 1px solid rgba(45,212,191,.42);
      background: linear-gradient(180deg, #1f766f, #155e59);
      color: #fff; cursor: pointer; box-shadow: 0 14px 32px rgba(0,0,0,.42);
      display: flex; align-items: center; justify-content: center; font-size: 23px;
    }}
    .site-log-fab.has-new::after {{
      content: ""; position: absolute; right: 7px; top: 7px;
      width: 10px; height: 10px; border-radius: 999px; background: #5eead4;
      box-shadow: 0 0 0 3px rgba(94,234,212,.18);
    }}
    .site-log-panel {{
      position: fixed; right: 18px; bottom: 84px; z-index: 49;
      width: min(390px, calc(100vw - 28px)); max-height: min(560px, calc(100vh - 120px));
      border: 1px solid rgba(45,212,191,.28);
      border-radius: 15px; overflow: hidden;
      background: linear-gradient(180deg, #121a30, #0e1528);
      box-shadow: 0 18px 46px rgba(0,0,0,.5);
    }}
    .site-log-panel[hidden] {{ display: none !important; }}
    .site-log-head {{
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      padding: 11px 12px; border-bottom: 1px solid var(--border);
      color: #e4edff; font-size: 13px; font-weight: 750;
    }}
    .site-log-close {{
      border: 1px solid rgba(255,255,255,.13); background: rgba(15,19,36,.85);
      color: #c8d8f8; border-radius: 8px; cursor: pointer; padding: 4px 8px;
    }}
    .site-log-feed {{
      max-height: 430px; overflow: auto; padding: 10px;
      display: flex; flex-direction: column; gap: 8px;
    }}
    .site-log-empty {{ color: var(--muted); font-size: 12px; line-height: 1.45; padding: 4px 2px 8px; }}
    .site-log-msg {{
      border: 1px solid rgba(45,212,191,.13); border-radius: 11px;
      background: rgba(8, 12, 24, 0.48); padding: 8px 9px;
    }}
    .site-log-meta {{ color: var(--muted); font-size: 10px; margin-bottom: 4px; font-variant-numeric: tabular-nums; }}
    .site-log-text {{ color: #edf3ff; font-size: 12px; line-height: 1.4; white-space: pre-wrap; word-break: break-word; }}
    .viewer__table {{ width: 100%; overflow: auto; }}
    .panel {{ display: none; }}
    .panel.active {{ display: block; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: .82rem;
    }}
    th, td {{
      padding: .55rem .65rem;
      text-align: left;
      vertical-align: top;
      border-bottom: 1px solid var(--border);
    }}
    th {{ color: var(--muted); font-weight: 600; font-size: .72rem; text-transform: uppercase; letter-spacing: .04em; }}
    tr:last-child td {{ border-bottom: none; }}
    tr.row-ready td {{
      background: rgba(34, 197, 94, .055);
      border-bottom-color: rgba(34, 197, 94, .18);
    }}
    tr.row-ready:hover td {{
      background: rgba(34, 197, 94, .095);
    }}
    .contacts {{ font-size: .78rem; line-height: 1.35; }}
    .contacts a {{ color: var(--teal); word-break: break-all; }}
    .sources-col {{ min-width: 560px; }}
    .src-row {{
      display: flex;
      align-items: center;
      gap: .38rem;
      margin: 0 0 .34rem 0;
      flex-wrap: wrap;
      padding: .18rem .05rem;
    }}
    .src-idx {{ color: var(--muted); min-width: 1.4rem; font-size: .8rem; }}
    .src-chip {{
      display: inline-flex;
      align-items: center;
      gap: .2rem;
      padding: .17rem .45rem;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,.12);
      background: rgba(15,20,28,.35);
      font-size: .81rem;
      line-height: 1.25;
      max-width: 100%;
      word-break: break-all;
    }}
    .src-chip--price {{ color: #d7fff3; border-color: rgba(45,212,191,.45); background: rgba(45,212,191,.1); font-weight: 700; }}
    .src-chip--phone {{ color: #e6ddff; border-color: rgba(167,139,250,.45); background: rgba(167,139,250,.12); }}
    .src-chip--site {{ color: #b8f6ec; border-color: rgba(45,212,191,.35); }}
    .src-chip--site a {{ color: var(--teal); }}
    .src-source {{
      display: inline-block;
      margin-right: .35rem;
      padding: .08rem .36rem;
      border-radius: 999px;
      background: rgba(45,212,191,.14);
      color: #d7fff3;
      font-size: .75rem;
      font-weight: 700;
    }}
    .psp-row {{ margin: 0 0 .2rem 0; }}
    .psp-i {{ color: var(--muted); margin-right: .2rem; }}
    .psp-price {{ color: #d4f9ef; font-weight: 600; }}
    .psp-link a {{ color: var(--teal); }}
    .psp-phone {{ color: #d5c8ff; }}
    .market-text {{ max-width: 36vw; }}
    @media (max-width: 1100px) {{ .market-text {{ max-width: none; }} }}
    .market-scroll {{
      max-height: 9rem;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      padding: .25rem 0;
    }}
    .market-details summary {{
      cursor: pointer;
      white-space: pre-wrap;
      word-break: break-word;
      color: var(--text);
      list-style: none;
    }}
    .market-details summary::-webkit-details-marker {{ display: none; }}
    .market-details[open] summary {{ color: var(--violet); margin-bottom: .3rem; }}
    .market-scroll::-webkit-scrollbar {{ width: 6px; }}
    .market-scroll::-webkit-scrollbar-thumb {{ background: rgba(167,139,250,.4); border-radius: 3px; }}
    .muted {{ color: var(--muted); font-size: .88em; }}
    .table-cap {{
      caption-side: top;
      text-align: left;
      padding: 0 0 .55rem 0;
      margin: 0;
      color: var(--muted);
      font-size: .8rem;
      line-height: 1.35;
      max-width: 70rem;
    }}
    .viability {{
      margin: 0 0 1.25rem 0;
      padding: 1rem 1.15rem 1.05rem;
      border-radius: var(--radius);
      border: 1px solid var(--border);
      background: linear-gradient(135deg, rgba(45,212,191,.08), rgba(167,139,250,.07));
    }}
    .viability--tight {{ border-color: rgba(251,191,36,.42); background: linear-gradient(135deg, rgba(251,191,36,.09), rgba(45,212,191,.04)); }}
    .viability--room {{ border-color: rgba(45,212,191,.5); }}
    .viability--warn {{ border-color: rgba(248,113,113,.45); }}
    .viability__head {{ font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin-bottom: .55rem; }}
    .viability__verdict {{ margin: 0 0 .6rem 0; font-size: .95rem; line-height: 1.45; color: var(--text); }}
    .viability__facts {{ margin: 0 0 .75rem 1rem; padding: 0; color: var(--text); font-size: .84rem; line-height: 1.45; }}
    .viability__pros, .viability__cons {{ font-size: .82rem; color: var(--muted); margin: .5rem 0 .35rem; }}
    .viability__pros ul, .viability__cons ul {{ margin: .25rem 0 .35rem 1.1rem; padding: 0; line-height: 1.4; }}
    .viability__narrative {{
      margin-top: .75rem;
      padding: .75rem .85rem;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: rgba(15,20,28,.55);
      font-size: .84rem;
      line-height: 1.45;
      color: var(--text);
      white-space: pre-wrap;
    }}
    .viability__muted {{ margin: .65rem 0 0 0; font-size: .78rem; color: var(--muted); line-height: 1.35; }}
    @keyframes rise {{
      from {{ opacity: 0; transform: translateY(24px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    @keyframes fadeIn {{
      from {{ opacity: 0; }}
      to {{ opacity: 1; }}
    }}
  </style>
</head>
<body>
  <!-- merge-report-build: {html_mod.escape(datetime.now(timezone.utc).isoformat())} -->
  <div class="wrap">
    <header>
      <p style="margin:0 0 .75rem 0;font-size:.9rem;"><a href="/" style="color:var(--teal);text-decoration:none;">← К списку тендеров</a></p>
      <h1>{title_esc}</h1>
      <p><code style="background:#232d3b;padding:.15rem .4rem;border-radius:6px;">{tid_esc}</code> · опубликован: <b>{pub_esc}</b></p>
    </header>
    {viability_html}
    <div class="live-note">{partial_note}</div>
    <div class="report-actions">
      <button type="button" class="report-btn" onclick="startTenderCompare(false)">Продолжить поиск цен</button>
      <button type="button" class="report-btn secondary" onclick="startTenderCompare(true)">Начать поиск заново</button>
      <button type="button" class="report-btn secondary" onclick="location.reload()">Обновить страницу</button>
      <span class="report-status" id="liveStatus">Если поиск рынка сейчас работает, таблица будет периодически обновляться.</span>
    </div>
    <div class="deck" id="cardsDeck">
      <article class="card card--compare active" data-panel="panel-compare">
        <div class="card__head">Сравнение</div>
        <div class="card__body">Смета рядом с ценами с сайтов и одной сводной цифрой по строке.</div>
      </article>
      <article class="card card--market" data-panel="panel-market">
        <div class="card__head">Источники</div>
        <div class="card__body">Реальные объявления/страницы, откуда взяты цены.</div>
      </article>
      <article class="card card--estimate" data-panel="panel-estimate">
        <div class="card__head">Позиции сметы</div>
        <div class="card__body">Таблица сметы как в исходном отчёте.</div>
      </article>
    </div>
    <section class="viewer">
      <div class="viewer__head">
        <span id="panelTitle">Сравнение</span>
        <span class="viewer__hint">Сверху — три вкладки</span>
      </div>
      <div class="viewer__table">
        <div id="panel-compare" class="panel active">
          <table>
            <caption class="table-cap"><b>Рынок:</b> в одной строке цена, телефон и сайт (номер 1·2·3 — разные источники). В конце строки — <b>медиана</b> по всем ценам. «—» значит данных нет.</caption>
            <thead><tr>
              <th>№</th>
              <th>Работа</th>
              <th title="Из сметы, для сравнения с рынком">Смета</th>
              <th title="Цена, телефон и сайт по каждому источнику">Рынок</th>
              <th title="Одна сводная цифра по строке">Медиана</th>
            </tr></thead>
            <tbody>{"".join(rows_compare)}</tbody>
          </table>
        </div>
        <div id="panel-market" class="panel">
          <table>
            <thead><tr>
              <th>№ п/п</th>
              <th>Работа</th>
              <th>Ответ</th>
              <th>Цены (итог)</th>
              <th>Статус</th>
            </tr></thead>
            <tbody>{"".join(rows_market)}</tbody>
          </table>
        </div>
        <div id="panel-estimate" class="panel">
          <table>
            <thead><tr>{th_est}</tr></thead>
            <tbody>{"".join(rows_est)}</tbody>
          </table>
        </div>
      </div>
    </section>
  </div>
  <button type="button" class="site-log-fab" id="siteLogFab" onclick="toggleSiteLog()" title="Логи и сообщения выполнения">🧾</button>
  <aside class="site-log-panel" id="siteLogPanel" hidden>
    <div class="site-log-head">
      <span>🧾 Логи выполнения</span>
      <button type="button" class="site-log-close" onclick="toggleSiteLog(false)">закрыть</button>
    </div>
    <div class="site-log-feed" id="siteLogFeed">
      <div class="site-log-empty">Пока событий нет. Когда поиск рынка работает по этому тендеру, сообщения появятся здесь.</div>
    </div>
  </aside>
  <script>
    const TENDER_ID = {tid_js};
    let siteLogOpen = false;
    let siteLogLastKey = "";

    function toggleSiteLog(force) {{
      const panel = document.getElementById("siteLogPanel");
      const fab = document.getElementById("siteLogFab");
      if (!panel) return;
      siteLogOpen = typeof force === "boolean" ? force : panel.hidden;
      panel.hidden = !siteLogOpen;
      if (siteLogOpen && fab) fab.classList.remove("has-new");
      const feed = document.getElementById("siteLogFeed");
      if (siteLogOpen && feed) feed.scrollTop = feed.scrollHeight;
    }}

    function renderSiteLog(events) {{
      const feed = document.getElementById("siteLogFeed");
      const fab = document.getElementById("siteLogFab");
      if (!feed) return;
      const list = Array.isArray(events) ? events.filter((ev) => !ev.tender_id || ev.tender_id === TENDER_ID).slice(-90) : [];
      const last = list.length ? JSON.stringify(list[list.length - 1]) : "";
      if (last && last !== siteLogLastKey && !siteLogOpen && fab) fab.classList.add("has-new");
      siteLogLastKey = last;
      feed.replaceChildren();
      if (!list.length) {{
        const empty = document.createElement("div");
        empty.className = "site-log-empty";
        empty.textContent = "Пока событий нет. Когда поиск рынка работает по этому тендеру, сообщения появятся здесь.";
        feed.appendChild(empty);
        return;
      }}
      for (const ev of list) {{
        const kind = String(ev.kind || "");
        const msg = document.createElement("div");
        msg.className = "site-log-msg" + (kind ? " is-" + kind : "");
        const meta = document.createElement("div");
        meta.className = "site-log-meta";
        meta.textContent = String(ev.ts || "сейчас").replace("T", " ");
        const text = document.createElement("div");
        text.className = "site-log-text";
        const icon = kind === "done" ? "✅" : (kind === "error" || kind === "warn") ? "⚠️" : kind === "begin" ? "🔎" : "🧾";
        const rawText = String(ev.text || "");
        text.textContent = rawText.startsWith(icon) ? rawText : (icon + " " + rawText);
        msg.appendChild(meta);
        msg.appendChild(text);
        feed.appendChild(msg);
      }}
      if (siteLogOpen) feed.scrollTop = feed.scrollHeight;
    }}

    async function startTenderCompare(resetMarket) {{
      const ok = confirm(
        resetMarket
          ? "Начать поиск цен заново для этого тендера?\\n\\nСохранённый прогресс рынка будет отброшен."
          : "Продолжить поиск цен для этого тендера?\\n\\nУже сохранённые строки рынка будут использованы."
      );
      if (!ok) return;
      const status = document.getElementById("liveStatus");
      if (status) status.textContent = "Отправляю задачу на сервер…";
      try {{
        const r = await fetch(resetMarket ? "/api/generate-merge-site-one-rerun-market" : "/api/generate-merge-site-one", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json", "Accept": "application/json" }},
          body: JSON.stringify({{ tender_id: TENDER_ID }}),
        }});
        const data = await r.json().catch(() => ({{}}));
        if (!r.ok || !data.ok) {{
          alert(data.message || ("Не удалось запустить задачу, HTTP " + r.status));
          if (status) status.textContent = data.message || "Запуск не выполнен.";
          return;
        }}
        if (status) status.textContent = "Задача запущена. Таблица будет обновляться по мере сохранения строк.";
      }} catch (e) {{
        alert("Ошибка запроса: " + e);
        if (status) status.textContent = "Ошибка запроса к серверу.";
      }}
    }}

    (function autoRefreshWhileMarketRuns() {{
      let lastDone = null;
      let lastReloadAt = Date.now();
      async function tick() {{
        const status = document.getElementById("liveStatus");
        try {{
          const r = await fetch("/api/merge-site-status", {{ cache: "no-store" }});
          if (!r.ok) return;
          const st = await r.json();
          renderSiteLog(st.chat_events || []);
          if (!st.running || st.current_tid !== TENDER_ID) return;
          const done = Number(st.market_done || 0);
          const total = Number(st.market_total || 0);
          if (status && total > 0) {{
            status.textContent = "Поиск рынка работает: " + done + "/" + total + ". Страница автообновляется, чтобы показать новые строки.";
          }}
          if (lastDone === null) {{
            lastDone = done;
            return;
          }}
          if (done > lastDone && Date.now() - lastReloadAt > 12000) {{
            lastReloadAt = Date.now();
            location.reload();
          }}
          lastDone = Math.max(lastDone, done);
        }} catch (e) {{}}
      }}
      setInterval(tick, 3000);
      tick();
    }})();

    (function() {{
      const cards = Array.from(document.querySelectorAll(".deck .card"));
      const panels = Array.from(document.querySelectorAll(".panel"));
      const titleEl = document.getElementById("panelTitle");
      const titleMap = {{
        "panel-compare": "Сравнение",
        "panel-market": "Источники рынка",
        "panel-estimate": "Смета",
      }};
      function setActive(panelId) {{
        cards.forEach((c) => c.classList.toggle("active", c.dataset.panel === panelId));
        panels.forEach((p) => p.classList.toggle("active", p.id === panelId));
        if (titleEl) titleEl.textContent = titleMap[panelId] || "Таблица";
      }}
      cards.forEach((card) => {{
        card.addEventListener("click", () => setActive(card.dataset.panel));
      }});
      setActive("panel-compare");
    }})();
  </script>
</body>
</html>
"""


def write_tender_report_site(tender_id: str) -> Path | None:
    tid = (tender_id or "").strip()
    if not tid:
        return None
    try:
        refresh_svodka_if_market_newer(tid)
    except Exception:
        print(f"[report_merge_html] refresh_svodka_if_market_newer failed tid={tid}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
    summary = REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx"
    est_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    market_path = REPORTS_DIR / f"РЫНОК_ИСТОЧНИКИ_{est_path.stem}.xlsx"
    use_summary = summary.is_file() and market_path.is_file()
    if use_summary:
        try:
            df = pd.read_excel(summary)
        except Exception:
            return None
    else:
        if not est_path.is_file():
            return None
        try:
            df = pd.read_excel(est_path)
        except Exception:
            return None
    if COL_NAME not in df.columns:
        return None
    df = recalc_estimate_qty_price_from_unit(df)

    meta = load_tender_metadata().get(tid, {})
    title = str(meta.get("title") or f"Тендер {tid}")

    viability_html = ""
    try:
        from autobot.tender_viability import (
            build_viability_narrative_openai,
            build_viability_section_html,
            compute_viability_stats,
        )

        vst = compute_viability_stats(df)
        narr = build_viability_narrative_openai(tid, vst)
        viability_html = build_viability_section_html(vst, tid, narrative=narr)
    except Exception:
        print(f"[report_merge_html] viability block skipped tid={tid}", file=sys.stderr, flush=True)

    REPORTS_SITE_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = REPORTS_SITE_DIR / tid
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    publish_date = str(meta.get("publish_date") or "").strip()
    path.write_text(
        _render_html(tid, title, df, publish_date=publish_date, viability_html=viability_html),
        encoding="utf-8",
    )
    return path


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="HTML-отчёт по СВОДКА_РЫНОК")
    ap.add_argument("--tender-id", required=True)
    args = ap.parse_args()
    p = write_tender_report_site(args.tender_id.strip())
    if not p:
        raise SystemExit("Нет СВОДКА_РЫНОК_<id>.xlsx — сначала merge_estimate_market.")
    print(p)


if __name__ == "__main__":
    main()
