"""
Склейка отчёта по смете (ОТЧЕТ_ПО_СМЕТАМ_*.xlsx) с реальными рыночными источниками.

Итоговый файл: СВОДКА_РЫНОК_<tender_id>.xlsx — работа, колонки сметы,
цены рынка за единицу, объявления/страницы и ссылки на источники.
"""

from __future__ import annotations

from autobot.paths import REPO_ROOT

import re
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

import pandas as pd

from autobot.market_analytics import COL_NAME, extract_ruble_amounts, recalc_estimate_qty_price_from_unit
from autobot.report_prompt import REPORTS_DIR

MARKET_PREFIX = "РЫНОК_ИСТОЧНИКИ_"
OUT_PREFIX = "СВОДКА_РЫНОК_"

_MOJIBAKE_MARKET_COLUMNS = {
    "Р¦РµРЅС‹ Р·Р° РµРґ. (СЂС‹РЅРѕРє, СЂСѓР±)": "Цены за ед. (рынок, руб)",
    "РњРµРґРёР°РЅР° С†РµРЅР° Р·Р° РµРґ. (СЂС‹РЅРѕРє)": "Медиана цена за ед. (рынок)",
    "РњРёРЅ С†РµРЅР° Р·Р° РµРґ. (СЂС‹РЅРѕРє)": "Мин цена за ед. (рынок)",
    "РњР°РєСЃ С†РµРЅР° Р·Р° РµРґ. (СЂС‹РЅРѕРє)": "Макс цена за ед. (рынок)",
    "РўРµР»РµС„РѕРЅС‹ (СЃС‚СЂРѕРіРѕ)": "Телефоны (строго)",
    "РЎСЃС‹Р»РєРё (СЃС‚СЂРѕРіРѕ)": "Ссылки (строго)",
    "Р¦РµРЅР°-СЃР°Р№С‚-С‚РµР»РµС„РѕРЅ (json)": "Цена-сайт-телефон (json)",
    "РСЃС‚РѕС‡РЅРёРєРё (СЃСЃС‹Р»РєРё/С‚РµР»РµС„РѕРЅС‹)": "Источники (ссылки/телефоны)",
    "РћС€РёР±РєР° / СЃС‚Р°С‚СѓСЃ": "Ошибка / статус",
}


def _normalize_market_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for old, new in _MOJIBAKE_MARKET_COLUMNS.items():
        if old not in out.columns:
            continue
        if new in out.columns:
            old_s = out[old].fillna("").astype(str)
            new_s = out[new].fillna("").astype(str)
            out[new] = out[new].where(new_s.str.strip() != "", out[old])
            out = out.drop(columns=[old], errors="ignore")
        else:
            out = out.rename(columns={old: new})
    return out


def _market_or_market_path(stem: str) -> Path:
    """Файл с реальными источниками рынка."""
    return REPORTS_DIR / f"{MARKET_PREFIX}{stem}.xlsx"


def refresh_svodka_if_market_newer(tender_id: str) -> Path | None:
    """
    Если ОТЧЕТ_ПО_СМЕТАМ обновлён позже СВОДКА_РЫНОК — пересобрать сводку.
    Иначе веб продолжает показывать старые кол-ва/цены из старого merge.
    """
    tid = (tender_id or "").strip()
    if not tid:
        return None
    est_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    out_path = REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx"
    stem = est_path.stem
    market_path = _market_or_market_path(stem)
    if not est_path.is_file():
        return out_path if out_path.is_file() else None
    if not market_path.is_file():
        return out_path if out_path.is_file() else None
    try:
        est_mtime = est_path.stat().st_mtime
        market_mtime = market_path.stat().st_mtime
    except OSError:
        return out_path if out_path.is_file() else None
    sv_mtime = 0.0
    if out_path.is_file():
        try:
            sv_mtime = out_path.stat().st_mtime
        except OSError:
            sv_mtime = 0.0
    if max(est_mtime, market_mtime) <= sv_mtime + 1.0:
        return out_path if out_path.is_file() else None
    return merge_estimate_and_market(tid)


def _norm_key(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _agg_text(series: pd.Series) -> str:
    vals: list[str] = []
    for v in series:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        s = str(v).strip()
        if not s:
            continue
        if s not in vals:
            vals.append(s)
    if not vals:
        return ""
    if len(vals) == 1:
        return vals[0]
    return "\n\n---\n\n".join(vals)


def merge_estimate_and_market(tender_id: str) -> Path | None:
    tid = (tender_id or "").strip()
    if not tid:
        return None

    est_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    stem = est_path.stem
    market_path = _market_or_market_path(stem)

    if not est_path.is_file():
        return None
    if not market_path.is_file():
        return None

    est = pd.read_excel(est_path)
    est = recalc_estimate_qty_price_from_unit(est)
    ali = pd.read_excel(market_path)
    ali = _normalize_market_columns(ali)

    if COL_NAME not in est.columns or COL_NAME not in ali.columns:
        return None

    _rename_market = {}
    if "Цены за ед. (рынок, руб)" not in ali.columns and "Цены (строго, руб)" in ali.columns:
        _rename_market["Цены (строго, руб)"] = "Цены за ед. (рынок, руб)"
    if "Медиана цена за ед. (рынок)" not in ali.columns and "Медиана цена (строго, руб)" in ali.columns:
        _rename_market["Медиана цена (строго, руб)"] = "Медиана цена за ед. (рынок)"
    if "Мин цена за ед. (рынок)" not in ali.columns and "Мин цена (строго, руб)" in ali.columns:
        _rename_market["Мин цена (строго, руб)"] = "Мин цена за ед. (рынок)"
    if "Макс цена за ед. (рынок)" not in ali.columns and "Макс цена (строго, руб)" in ali.columns:
        _rename_market["Макс цена (строго, руб)"] = "Макс цена за ед. (рынок)"
    if _rename_market:
        ali = ali.rename(columns=_rename_market)

    est = est.copy()
    est["__merge_key"] = est[COL_NAME].map(_norm_key)

    ali_cols = [
        COL_NAME,
        "Рыночные источники",
        "Рыночные источники (полный текст)",
        "Цены за ед. (рынок, руб)",
        "Медиана цена за ед. (рынок)",
        "Мин цена за ед. (рынок)",
        "Макс цена за ед. (рынок)",
        "Телефоны (строго)",
        "Ссылки (строго)",
        "Цена-сайт-телефон (json)",
        "Источники (ссылки/телефоны)",
        "Ошибка / статус",
        "Поисковый запрос рынка",
        "Источник 1",
        "Название объявления 1",
        "Цена объявления 1",
        "Ссылка объявления 1",
        "Источник 2",
        "Название объявления 2",
        "Цена объявления 2",
        "Ссылка объявления 2",
        "Источник 3",
        "Название объявления 3",
        "Цена объявления 3",
        "Ссылка объявления 3",
        "Источник 4",
        "Название объявления 4",
        "Цена объявления 4",
        "Ссылка объявления 4",
        "Источник 5",
        "Название объявления 5",
        "Цена объявления 5",
        "Ссылка объявления 5",
    ]
    take = [c for c in ali_cols if c in ali.columns]
    if COL_NAME not in take:
        return None

    ali_small = ali[take].copy()
    ali_small["Рынок обработано"] = "Да"
    ali_small["__merge_key"] = ali_small[COL_NAME].map(_norm_key)
    drop_name = [c for c in ali_small.columns if c != "__merge_key" and c in est.columns]
    for c in drop_name:
        if c in ali_small.columns:
            ali_small = ali_small.drop(columns=[c], errors="ignore")
    ali_small = ali_small.drop(columns=[COL_NAME], errors="ignore")

    # В отчёте рынка могут быть одинаковые названия работ (повторные запросы).
    # Перед merge схлопываем до одной строки на ключ, иначе получаем размножение строк в сводке.
    agg_cols = [c for c in ali_small.columns if c != "__merge_key"]
    if agg_cols:
        ali_small = (
            ali_small.groupby("__merge_key", as_index=False)
            .agg({c: _agg_text for c in agg_cols})
        )

    merged = est.merge(ali_small, on="__merge_key", how="left")
    merged = merged.drop(columns=["__merge_key"], errors="ignore")
    merged = recalc_estimate_qty_price_from_unit(merged)

    if "Рынок обработано" in merged.columns:
        processed = merged["Рынок обработано"].fillna("").astype(str).str.strip().ne("")
        if "Ошибка / статус" not in merged.columns:
            merged["Ошибка / статус"] = ""
        status = merged["Ошибка / статус"].fillna("").astype(str).str.strip()
        merged.loc[processed & status.eq(""), "Ошибка / статус"] = "обработано, данных нет"

    def _rub_line(txt) -> str:
        if txt is None or (isinstance(txt, float) and pd.isna(txt)):
            return ""
        amounts = extract_ruble_amounts(str(txt))
        if not amounts:
            return ""
        uniq = sorted({round(x, 2) for x in amounts})[:12]
        return "; ".join(f"{v:,.0f}".replace(",", " ") for v in uniq)

    if "Рыночные источники" in merged.columns:
        merged["Суммы из текста ответа (авто)"] = merged["Рыночные источники"].map(_rub_line)

    if "Цены за ед. (рынок, руб)" in merged.columns:
        strict_prices = merged["Цены за ед. (рынок, руб)"].fillna("").astype(str).str.strip()
        fallback = merged.get("Суммы из текста ответа (авто)", pd.Series([""] * len(merged), index=merged.index))
        merged["Рынок цены за ед. (итог)"] = strict_prices.where(strict_prices != "", fallback)
    elif "Суммы из текста ответа (авто)" in merged.columns:
        merged["Рынок цены за ед. (итог)"] = merged["Суммы из текста ответа (авто)"]
    else:
        merged["Рынок цены за ед. (итог)"] = pd.Series([""] * len(merged), index=merged.index, dtype=object)

    # Старое имя колонки — совместимость; по смыслу это цены за ед., не сумма по позиции.
    merged["Суммы из ответа (итог)"] = merged["Рынок цены за ед. (итог)"]

    out = REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    merged.to_excel(out, index=False)
    return out


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Склейка ОТЧЕТ_ПО_СМЕТАМ + РЫНОК_ИСТОЧНИКИ")
    ap.add_argument("--tender-id", required=True, help="Номер тендера")
    args = ap.parse_args()
    p = merge_estimate_and_market(args.tender_id.strip())
    if not p:
        raise SystemExit("Нет файлов ОТЧЕТ или РЫНОК_ИСТОЧНИКИ для этого id (сначала main.py и real_market_scraper.py).")
    print(p)


if __name__ == "__main__":
    main()
