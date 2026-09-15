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

from autobot.market_analytics import COL_NAME
from autobot.report_prompt import REPORTS_DIR, load_tender_metadata

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
    from autobot.market_contract import CONTRACT_VERSION
    contract_current = False
    if out_path.is_file():
        try:
            sample = pd.read_excel(out_path, nrows=1)
            contract_current = (not sample.empty and sample.iloc[0].get("Версия проверки рынка") == CONTRACT_VERSION)
            if contract_current:
                from autobot.market_evidence_policy import region_key
                expected_region = region_key((load_tender_metadata().get(tid) or {}).get('region'))
                contract_current = region_key(sample.iloc[0].get('Регион поиска')) == expected_region
        except (OSError, ValueError):
            pass
    if contract_current and max(est_mtime, market_mtime) <= sv_mtime:
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
    from autobot.market_contract import merge_market_frames
    from autobot.atomic_output import write_excel

    tid = (tender_id or "").strip()
    if not tid or "/" in tid or "\\" in tid or ".." in tid:
        return None
    est_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    market_path = _market_or_market_path(est_path.stem)
    if not est_path.is_file() or not market_path.is_file():
        return None
    est = pd.read_excel(est_path)
    region = (load_tender_metadata().get(tid) or {}).get('region')
    if region:
        est['Регион поиска'] = str(region)
    market = _normalize_market_columns(pd.read_excel(market_path))
    if COL_NAME not in est.columns or COL_NAME not in market.columns:
        return None
    merged = merge_market_frames(est, market)
    out_path = REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx"
    write_excel(merged, out_path)
    return out_path


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
