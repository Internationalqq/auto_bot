"""
Склейка отчёта по смете (ОТЧЕТ_ПО_СМЕТАМ_*.xlsx) с выгрузкой Алисы (АЛИСА_РЫНОК_*.xlsx).

Итоговый файл: СВОДКА_РЫНОК_<tender_id>.xlsx — работа, колонки сметы, ответ Алисы,
цены рынка за единицу (итог) и авто-разбор текста при необходимости.
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

ALICE_PREFIX = "АЛИСА_РЫНОК_"
OUT_PREFIX = "СВОДКА_РЫНОК_"


def refresh_svodka_if_estimate_newer(tender_id: str) -> Path | None:
    """
    Если ОТЧЕТ_ПО_СМЕТАМ обновлён позже СВОДКА_РЫНОК — пересобрать сводку (нужен файл АЛИСА_РЫНОК).
    Иначе веб продолжает показывать старые кол-ва/цены из старого merge.
    """
    tid = (tender_id or "").strip()
    if not tid:
        return None
    est_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    out_path = REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx"
    stem = est_path.stem
    alice_path = REPORTS_DIR / f"{ALICE_PREFIX}{stem}.xlsx"
    if not est_path.is_file():
        return out_path if out_path.is_file() else None
    if not alice_path.is_file():
        return out_path if out_path.is_file() else None
    try:
        est_mtime = est_path.stat().st_mtime
        alice_mtime = alice_path.stat().st_mtime
    except OSError:
        return out_path if out_path.is_file() else None
    sv_mtime = 0.0
    if out_path.is_file():
        try:
            sv_mtime = out_path.stat().st_mtime
        except OSError:
            sv_mtime = 0.0
    if max(est_mtime, alice_mtime) <= sv_mtime + 1.0:
        return out_path if out_path.is_file() else None
    return merge_estimate_and_alice(tid)


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


def merge_estimate_and_alice(tender_id: str) -> Path | None:
    tid = (tender_id or "").strip()
    if not tid:
        return None

    est_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    stem = est_path.stem
    alice_path = REPORTS_DIR / f"{ALICE_PREFIX}{stem}.xlsx"

    if not est_path.is_file():
        return None
    if not alice_path.is_file():
        return None

    est = pd.read_excel(est_path)
    est = recalc_estimate_qty_price_from_unit(est)
    ali = pd.read_excel(alice_path)

    if COL_NAME not in est.columns or COL_NAME not in ali.columns:
        return None

    # Старые выгрузки Алисы → новые имена колонок (за единицу, не «сумма»).
    _rename_alice = {}
    if "Цены за ед. (рынок, руб)" not in ali.columns and "Цены (строго, руб)" in ali.columns:
        _rename_alice["Цены (строго, руб)"] = "Цены за ед. (рынок, руб)"
    if "Медиана цена за ед. (рынок)" not in ali.columns and "Медиана цена (строго, руб)" in ali.columns:
        _rename_alice["Медиана цена (строго, руб)"] = "Медиана цена за ед. (рынок)"
    if "Мин цена за ед. (рынок)" not in ali.columns and "Мин цена (строго, руб)" in ali.columns:
        _rename_alice["Мин цена (строго, руб)"] = "Мин цена за ед. (рынок)"
    if "Макс цена за ед. (рынок)" not in ali.columns and "Макс цена (строго, руб)" in ali.columns:
        _rename_alice["Макс цена (строго, руб)"] = "Макс цена за ед. (рынок)"
    if _rename_alice:
        ali = ali.rename(columns=_rename_alice)

    est = est.copy()
    est["__merge_key"] = est[COL_NAME].map(_norm_key)

    ali_cols = [
        COL_NAME,
        "Ответ Алисы",
        "Ответ Алисы (полный)",
        "Цены за ед. (рынок, руб)",
        "Медиана цена за ед. (рынок)",
        "Мин цена за ед. (рынок)",
        "Макс цена за ед. (рынок)",
        "Телефоны (строго)",
        "Ссылки (строго)",
        "Цена-сайт-телефон (json)",
        "Источники (ссылки/телефоны)",
        "Ошибка / статус",
        "Запрос Алисы",
    ]
    take = [c for c in ali_cols if c in ali.columns]
    if COL_NAME not in take:
        return None

    ali_small = ali[take].copy()
    ali_small["__merge_key"] = ali_small[COL_NAME].map(_norm_key)
    drop_name = [c for c in ali_small.columns if c != "__merge_key" and c in est.columns]
    for c in drop_name:
        if c in ali_small.columns:
            ali_small = ali_small.drop(columns=[c], errors="ignore")
    ali_small = ali_small.drop(columns=[COL_NAME], errors="ignore")

    # В отчёте Алисы могут быть одинаковые названия работ (повторные запросы).
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

    def _rub_line(txt) -> str:
        if txt is None or (isinstance(txt, float) and pd.isna(txt)):
            return ""
        amounts = extract_ruble_amounts(str(txt))
        if not amounts:
            return ""
        uniq = sorted({round(x, 2) for x in amounts})[:12]
        return "; ".join(f"{v:,.0f}".replace(",", " ") for v in uniq)

    if "Ответ Алисы" in merged.columns:
        merged["Суммы из текста ответа (авто)"] = merged["Ответ Алисы"].map(_rub_line)

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

    ap = argparse.ArgumentParser(description="Склейка ОТЧЕТ_ПО_СМЕТАМ + АЛИСА_РЫНОК")
    ap.add_argument("--tender-id", required=True, help="Номер тендера")
    args = ap.parse_args()
    p = merge_estimate_and_alice(args.tender_id.strip())
    if not p:
        raise SystemExit("Нет файлов ОТЧЕТ или АЛИСА_РЫНОК для этого id (сначала main.py и alice_market_scraper.py).")
    print(p)


if __name__ == "__main__":
    main()
