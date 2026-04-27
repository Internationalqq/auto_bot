"""
Минимальная аналитика: по строкам Excel-отчёта (название + цены сметы) — веб-поиск
и извлечение сумм в рублях из сниппетов, сравнение с ценой за единицу из сметы.

Зависимость: pip install ddgs   (раньше: duckduckgo-search — даёт предупреждение о переименовании)

Ограничения: выдача поиска часто не про «ту же» работу и ту же единицу измерения;
цифры из сниппетов — ориентир, не договор. При блокировках DDG — пауза и меньше строк (--max-rows).

Надёжный поиск: переменная окружения SERPAPI_KEY (serpapi.com) — иначе бесплатный DDG,
который часто отвечает пусто («No results found»).

Запуск:
  py market_analytics.py --tender-id 0121200004726000378 --max-rows 25
  py market_analytics.py --queries-only --tender-id ...   # только колонка «Поисковый запрос», без сети
"""

from __future__ import annotations

import argparse
import math
import os
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

import pandas as pd

from autobot.report_prompt import REPORTS_DIR, load_tender_metadata

# Колонки как в write_tender_estimate_report / _build_tender_clean_df
COL_NAME = "Название работы/услуги"
COL_UNIT = "Ед. изм."
COL_QTY = "Кол-во"
COL_UNIT_PRICE = "Цена за ед., руб"
COL_SUM = "Сумма, руб"
COL_ITEM = "№ п/п"
COL_DUP = "Явный дубликат"


def estimate_block_qty_from_unit(unit_text: str) -> float | None:
    """
    Множитель блока из «Ед. изм.» (ГЭСН): 100 м2, 100м2, 100 м², m2, 100 шт, или только «100» в ячейке.
    Используется при разборе ЛСР и при пересчёте строк сводки для веба.
    """
    raw = (unit_text or "").strip()
    if not raw:
        return None
    t = raw.lstrip("\ufeff")
    for ch in ("\xa0", "\u202f", "\u2009", "\u2007"):
        t = t.replace(ch, " ")
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return None

    def _parse_num_chunk(chunk: str) -> float | None:
        num_s = re.sub(r"\s+", "", chunk).replace(",", ".")
        if not num_s:
            return None
        try:
            v = float(num_s)
        except ValueError:
            return None
        if 0 < v <= 1e9:
            return v
        return None

    patterns = (
        r"^\s*([\d\s]+(?:[.,]\d+)?)\s*(?:шт\.?|штук|штуки|ед\.?)(?:\s|$|[^\d])",
        r"^\s*([\d\s]+(?:[.,]\d+)?)\s*(?:м|m)\s*[2²](?:\s|$|[^\d])",
        r"^\s*([\d\s]+(?:[.,]\d+)?)\s*(?:м|m)\s*[3³](?:\s|$|[^\d])",
        r"^\s*([\d\s]+(?:[.,]\d+)?)\s*(?:м2|м²|m2|m\s*2)(?:\s|$|[^\d])",
        r"^\s*([\d\s]+(?:[.,]\d+)?)\s*(?:м3|м³|m3|m\s*3)(?:\s|$|[^\d])",
    )
    for pat in patterns:
        m = re.match(pat, t, re.IGNORECASE)
        if not m:
            continue
        v = _parse_num_chunk(m.group(1))
        if v is not None:
            return v

    m2 = re.search(
        r"^\s*([\d\s]+(?:[.,]\d+)?).{0,6}?(?:м\s*[2²]|м2|м²|m\s*2|m2)",
        t,
        re.IGNORECASE,
    )
    if m2:
        v = _parse_num_chunk(m2.group(1))
        if v is not None:
            return v

    m3 = re.search(
        r"^\s*([\d\s]+(?:[.,]\d+)?).{0,6}?(?:м\s*[3³]|м3|м³|m\s*3|m3)",
        t,
        re.IGNORECASE,
    )
    if m3:
        v = _parse_num_chunk(m3.group(1))
        if v is not None:
            return v

    msht = re.search(
        r"^\s*([\d\s]+(?:[.,]\d+)?).{0,6}?(?:шт\.?|штук)",
        t,
        re.IGNORECASE,
    )
    if msht:
        v = _parse_num_chunk(msht.group(1))
        if v is not None:
            return v

    if re.fullmatch(r"[\d\s,]+(?:[.,]\d+)?", t):
        v = _parse_num_chunk(t)
        if v is not None and v >= 1.0:
            return v

    return None


def unit_has_area_or_volume_marker(unit: str) -> bool:
    """В «Ед. изм.» указана площадь/объём (м², м³), а не только шт."""
    u = (unit or "").strip()
    if not u:
        return False
    return bool(
        re.search(
            r"(?:м|m)\s*[2²]|(?:м|m)\s*[3³]|м2|м²|м3|м³|m2|m3",
            u,
            re.IGNORECASE,
        )
    )


def recalc_estimate_qty_price_from_unit(df: pd.DataFrame) -> pd.DataFrame:
    """
    Для веб-сводки: правим кол-во и цену по «Ед. изм.» и сумме.

    «100 м2» / «10 м3» — не пересчитываем: число в ед. изм. — норматив ГЭСН, а не объём;
    деление суммы на него давало «цену за 1 м²» и визуальный сдвиг в сравнении с рынком.

    «100 шт» — N в подписи это размер норматива (тариф за N штук), а не объём.
    Если в строке ошибочно взяли Кол-во = N и Цену = Сумма/N (копейки за шт),
    а по смыслу сметы цена = тариф за блок N шт (крупное число), восстанавливаем:
    Цена за ед. = цена_из_ячейки × N, Кол-во = Сумма / эта_цена, но только если
    цена явно меньше «средней за штуку в блоке» Сумма/N (иначе это нормальная цена за шт).
    """
    out = df.copy()
    out.columns = [str(c).replace("\xa0", " ").strip() for c in out.columns]
    need = {COL_UNIT, COL_QTY, COL_SUM}
    if not need.issubset(out.columns):
        return df
    for _col in (COL_QTY, COL_UNIT_PRICE):
        if _col in out.columns:
            out[_col] = pd.to_numeric(out[_col], errors="coerce").astype("float64")
    for idx in out.index:
        u = out.loc[idx, COL_UNIT]
        unit = "" if pd.isna(u) else str(u).strip()
        sm_raw = out.loc[idx, COL_SUM]
        if pd.isna(sm_raw):
            continue
        try:
            sm_f = float(sm_raw)
        except (TypeError, ValueError):
            continue
        if sm_f <= 0:
            continue

        m_sht = re.match(r"^\s*(\d{2,})\s*шт", unit, re.IGNORECASE)
        if m_sht and COL_UNIT_PRICE in out.columns:
            n_block = int(m_sht.group(1))
            try:
                p_cur = float(out.loc[idx, COL_UNIT_PRICE])
                q_cur = float(out.loc[idx, COL_QTY])
            except (TypeError, ValueError):
                p_cur = q_cur = None
            if p_cur is not None and q_cur is not None and p_cur > 0 and q_cur > 0:
                # Ошибка: Кол-во = N из «N шт», Цена = Сумма/N — тогда p_cur*N << Сумма (не хватает до суммы
                # целым числом блоков). Нормальный случай «N шт по N р/шт = Сумма» даёт p_cur*N ≈ Сумма — не трогаем.
                tight = max(1.0, abs(sm_f) * 1e-6)
                near_block_tariff = abs(p_cur * n_block - sm_f) <= tight
                qty_matches_n = abs(q_cur - float(n_block)) <= max(0.02 * n_block, 0.05)
                tariff_gap = p_cur * n_block < sm_f * 0.998 - 1e-9
                product_ok = abs(p_cur * q_cur - sm_f) / sm_f < 0.04
                # 1) Строка согласована (цена×кол≈сумма), но p×N << суммы — делили сумму на N.
                # 2) Как в п.1 по колонке «100», но цена×кол не бьётся с суммой (старая ошибка парсера):
                #    при N≥100 и «мелкой» цене — всё равно восстанавливаем тариф за блок N шт.
                if n_block >= 10 and qty_matches_n and (
                    (product_ok and tariff_gap)
                    or (n_block >= 100 and tariff_gap and p_cur < 45.0)
                    or (n_block >= 100 and near_block_tariff and p_cur < 45.0)
                ):
                    p_block = p_cur * n_block
                    if p_block > 0 and math.isfinite(p_block):
                        q_new = sm_f / p_block
                        if q_new > 0 and q_new < 1e8 and abs(q_new * p_block - sm_f) / sm_f < 0.02:
                            out.loc[idx, COL_UNIT_PRICE] = p_block
                            out.loc[idx, COL_QTY] = q_new
                continue

        # Площадь/объём в «Ед. изм.» — не делим сумму на норматив (100 м² и т.п.): в сводке уже
        # кол-во и цена из отчёта; иначе «Цена сметы» становится в ~100 раз меньше ожидаемой.
        if unit_has_area_or_volume_marker(unit):
            continue

        qb = estimate_block_qty_from_unit(unit)
        if qb is None:
            continue
        up = sm_f / qb
        if not math.isfinite(up) or up <= 0 or up > sm_f:
            continue
        out.loc[idx, COL_QTY] = qb
        if COL_UNIT_PRICE in out.columns:
            out.loc[idx, COL_UNIT_PRICE] = up
    return out


RUBLE_NUM_RE = re.compile(
    r"(?P<n>\d{1,3}(?:\s\d{3})+(?:[,.]\d{1,2})?|\d+(?:[,.]\d{1,2})?)\s*(?:руб\.?|₽|р\.\s*)",
    re.IGNORECASE,
)
# «от 15 000» без слова «руб» сразу после числа
RUBLE_FROM_RE = re.compile(
    r"(?:от|до|цена)\s*(?P<n>\d{1,3}(?:\s\d{3})+|\d{4,})\s*(?:руб|₽|р\.?)?",
    re.IGNORECASE,
)


def _parse_ru_number(raw: str) -> float | None:
    s = raw.strip().replace("\xa0", " ").replace("\u202f", " ")
    s = s.replace(" ", "").replace(",", ".")
    if not s:
        return None
    try:
        v = float(s)
        return v if v == v and v > 0 else None  # noqa: PLR0124
    except ValueError:
        return None


def extract_ruble_amounts(text: str) -> list[float]:
    if not text:
        return []
    t = text.replace("\xa0", " ")
    out: list[float] = []
    for m in RUBLE_NUM_RE.finditer(t):
        v = _parse_ru_number(m.group("n"))
        if v is not None and 10 <= v <= 500_000_000:
            out.append(v)
    for m in RUBLE_FROM_RE.finditer(t):
        v = _parse_ru_number(m.group("n"))
        if v is not None and 100 <= v <= 500_000_000:
            out.append(v)
    return out


def _ddgs_client():
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        pass
    try:
        from duckduckgo_search import DDGS
        return DDGS
    except ImportError:
        raise SystemExit("Установи: pip install ddgs") from None


def _search_snippets_ddg_once(
    query: str,
    max_results: int,
    *,
    timeout_sec: float,
    region: str,
) -> tuple[list[str], str | None]:
    DDGS = _ddgs_client()
    chunks: list[str] = []
    try:
        with DDGS(timeout=timeout_sec) as ddgs:
            for r in ddgs.text(query, region=region, max_results=max_results):
                title = (r.get("title") or "").strip()
                body = (r.get("body") or "").strip()
                chunks.append(f"{title}. {body}")
    except Exception as e:
        return [], str(e)[:300]
    if not chunks:
        return [], "пустая выдача"
    return chunks, None


def _search_snippets_ddg(
    query: str,
    max_results: int = 5,
    *,
    timeout_sec: float = 30.0,
    attempts: int = 3,
) -> tuple[list[str], str | None]:
    """
    Несколько попыток и смена региона выдачи: DDG часто отдаёт пусто при лимитах/блоках.
    """
    regions = ("ru-ru", "wt-wt")
    last_err: str | None = None
    for i in range(max(1, attempts)):
        reg = regions[i % len(regions)]
        chunks, err = _search_snippets_ddg_once(query, max_results, timeout_sec=timeout_sec, region=reg)
        if chunks:
            return chunks, None
        last_err = err or "нет результатов"
        if i + 1 < attempts:
            time.sleep(1.2 + i * 0.5)
    # короткий запрос — последний шанс
    short = " ".join(query.split()[:12]) + " цена руб"
    chunks, err = _search_snippets_ddg_once(short, max_results, timeout_sec=timeout_sec, region="ru-ru")
    if chunks:
        return chunks, None
    return [], last_err or err


def _search_serpapi(
    query: str,
    max_results: int,
    *,
    timeout_sec: float,
) -> tuple[list[str], str | None]:
    """Если задан SERPAPI_KEY — Google через SerpAPI (стабильнее, чем бесплатный DDG)."""
    key = (os.environ.get("SERPAPI_KEY") or "").strip()
    if not key:
        return [], None
    try:
        r = requests.get(
            "https://serpapi.com/search.json",
            params={
                "q": query,
                "api_key": key,
                "engine": "google",
                "hl": "ru",
                "gl": "ru",
                "num": min(max(max_results, 5), 10),
            },
            timeout=timeout_sec,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return [], f"SerpAPI: {e}"[:300]
    chunks: list[str] = []
    for item in (data.get("organic_results") or [])[:max_results]:
        t = (item.get("title") or "").strip()
        s = (item.get("snippet") or "").strip()
        if t or s:
            chunks.append(f"{t}. {s}")
    if not chunks:
        err = (data.get("error") or "нет organic_results")
        return [], f"SerpAPI: {err}"[:300]
    return chunks, None


def search_snippets_web(
    query: str,
    max_results: int = 5,
    *,
    timeout_sec: float = 30.0,
) -> tuple[list[str], str | None]:
    """Сначала SerpAPI (если есть ключ), иначе DuckDuckGo."""
    chunks, err = _search_serpapi(query, max_results, timeout_sec=timeout_sec)
    if chunks:
        return chunks, None
    return _search_snippets_ddg(query, max_results, timeout_sec=timeout_sec)


def _reference_price(unit_price: float | None, sum_price: float | None, qty: float | None) -> float | None:
    if unit_price is not None and unit_price > 0:
        return float(unit_price)
    if sum_price is not None and qty is not None and qty > 1e-9 and sum_price > 0:
        return float(sum_price) / float(qty)
    if sum_price is not None and sum_price > 0:
        return float(sum_price)
    return None


def _pick_market_median(amounts: list[float], ref: float | None) -> tuple[float | None, str]:
    if not amounts:
        return None, "нет сумм в выдаче"
    if ref is None or ref <= 0:
        return float(statistics.median(amounts)), "эталон сметы не задан — медиана по всем найденным суммам"

    lo, hi = ref * 0.03, ref * 120.0
    tight = [a for a in amounts if lo <= a <= hi]
    if len(tight) >= 1:
        return float(statistics.median(tight)), "медиана в разумном коридоре от цены сметы"
    loose = [a for a in amounts if ref * 0.005 <= a <= ref * 500]
    if len(loose) >= 1:
        return float(statistics.median(loose)), "медиана по расширенному коридору (низкая уверенность)"
    return float(statistics.median(amounts)), "очень широкий разброс — только ориентир"


def build_search_query(work_name: str, region: str, unit: str) -> str:
    w = (work_name or "").strip()[:180]
    u = (unit or "").strip()[:40]
    reg = (region or "").strip()[:80]
    parts = [w, "стоимость работ", reg]
    if u:
        parts.append(u)
    return " ".join(p for p in parts if p)


def run_market_table(
    df: pd.DataFrame,
    region: str,
    *,
    max_rows: int,
    pause_sec: float,
    skip_duplicates: bool,
    timeout_sec: float = 30.0,
    verbose: bool = True,
    queries_only: bool = False,
) -> pd.DataFrame:
    rows_out: list[dict] = []
    n = 0
    for _, row in df.iterrows():
        if n >= max_rows:
            break
        if skip_duplicates and str(row.get(COL_DUP, "")).strip() == "Да":
            continue
        name = str(row.get(COL_NAME, "") or "").strip()
        if len(name) < 8:
            continue

        if verbose:
            short = (name[:72] + "…") if len(name) > 72 else name
            print(f"[{n + 1}/{max_rows}] поиск: {short}", flush=True)

        unit = str(row.get(COL_UNIT, "") or "")
        qty = row.get(COL_QTY)
        qty_f = float(qty) if qty is not None and pd.notna(qty) else None
        up = row.get(COL_UNIT_PRICE)
        up_f = float(up) if up is not None and pd.notna(up) else None
        sm = row.get(COL_SUM)
        sm_f = float(sm) if sm is not None and pd.notna(sm) else None

        ref = _reference_price(up_f, sm_f, qty_f)
        query = build_search_query(name, region, unit)
        if queries_only:
            snippets, search_err = [], "без поиска (--queries-only)"
        else:
            snippets, search_err = search_snippets_web(query, max_results=5, timeout_sec=timeout_sec)
        blob = " \n".join(snippets)
        amounts = extract_ruble_amounts(blob)
        if verbose:
            extra = f" ({search_err})" if search_err else ""
            print(f"    -> сниппетов: {len(snippets)}, сумм в тексте: {len(amounts)}{extra}", flush=True)
        market_med, note = _pick_market_median(amounts, ref)

        dev_pct = None
        if ref and ref > 0 and market_med is not None:
            dev_pct = round((market_med - ref) / ref * 100.0, 1)

        rows_out.append(
            {
                COL_ITEM: row.get(COL_ITEM, ""),
                COL_NAME: name,
                COL_UNIT: unit,
                COL_QTY: qty_f if qty_f is not None else row.get(COL_QTY),
                "Цена за ед. смета": up_f,
                "Сумма смета": sm_f,
                "Поисковый запрос": query,
                "Сниппеты (фрагмент)": (blob[:1200] + "…") if len(blob) > 1200 else blob,
                "Найденные суммы, руб": "; ".join(f"{a:,.0f}".replace(",", " ") for a in sorted(set(amounts))[:30]),
                "Рыночный ориентир, руб": market_med,
                "Отклонение от цены сметы, %": dev_pct,
                "Примечание": note,
                "Статус поиска": "ок" if snippets else (search_err or "пусто"),
            }
        )
        n += 1
        if pause_sec > 0 and n < max_rows and not queries_only:
            if verbose:
                print(f"    пауза {pause_sec} с…", flush=True)
            time.sleep(pause_sec)
    return pd.DataFrame(rows_out)


def _load_report_xlsx(tender_id: str | None, xlsx_path: str | None) -> Path:
    if xlsx_path:
        p = Path(xlsx_path)
        if not p.is_file():
            raise SystemExit(f"Нет файла: {p}")
        return p
    if not tender_id:
        raise SystemExit("Укажи --tender-id или --xlsx")
    p = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tender_id.strip()}.xlsx"
    if not p.is_file():
        raise SystemExit(f"Нет отчёта: {p}")
    return p


def main() -> None:
    ap = argparse.ArgumentParser(description="Минимальная рыночная аналитика по Excel-отчёту ЛСР")
    ap.add_argument("--tender-id", default="", help="ID тендера (файл data/reports/ОТЧЕТ_ПО_СМЕТАМ_<id>.xlsx)")
    ap.add_argument("--xlsx", default="", help="Путь к .xlsx вместо tender-id")
    ap.add_argument("--region", default="", help="Регион для запроса (иначе из tenders.json)")
    ap.add_argument("--max-rows", type=int, default=30, help="Сколько позиций обработать (остальное пропуск)")
    ap.add_argument("--pause", type=float, default=1.2, help="Пауза между запросами, сек")
    ap.add_argument(
        "--timeout",
        type=float,
        default=35.0,
        help="Таймаут одного запроса к поиску, сек (чтобы не «висеть» бесконечно)",
    )
    ap.add_argument("--quiet", action="store_true", help="Без прогресса в консоли")
    ap.add_argument(
        "--queries-only",
        action="store_true",
        help="Не ходить в интернет: только сформировать поисковые запросы в Excel",
    )
    ap.add_argument("--include-duplicates", action="store_true", help="Не пропускать строки «Явный дубликат»")
    args = ap.parse_args()

    path = _load_report_xlsx(args.tender_id or None, args.xlsx or None)
    df = pd.read_excel(path)
    need = {COL_NAME, COL_SUM}
    if not need.issubset(df.columns):
        raise SystemExit(f"В файле нет колонок {need}. Есть: {list(df.columns)}")

    region = (args.region or "").strip()
    if not region and args.tender_id:
        meta = load_tender_metadata().get(args.tender_id.strip(), {})
        region = (meta.get("region") or "").strip()
    if not region:
        region = "Россия"

    lim = max(1, min(args.max_rows, 500))
    pause = max(0.0, args.pause)
    timeout_sec = max(5.0, args.timeout)
    if not args.quiet and not args.queries_only:
        rough_min = (lim * (pause + min(timeout_sec, 45.0) * 0.25 + 2.0)) / 60.0
        print(
            f"До {lim} запросов к поиску, пауза {pause} с, таймаут {timeout_sec} с — ориентир ~{rough_min:.1f} мин. "
            "Строки в консоли идут по одной позиции; Ctrl+C — прервать.",
            flush=True,
        )
    if not args.quiet and args.queries_only:
        print("Режим --queries-only: сеть не используется.", flush=True)
    if not args.quiet and not args.queries_only and not (os.environ.get("SERPAPI_KEY") or "").strip():
        print(
            "Подсказка: DDG часто пустой; для стабильной выдачи задай SERPAPI_KEY (https://serpapi.com).",
            flush=True,
        )

    skip_dup = not args.include_duplicates
    out_df = run_market_table(
        df,
        region,
        max_rows=lim,
        pause_sec=pause,
        skip_duplicates=skip_dup,
        timeout_sec=timeout_sec,
        verbose=not args.quiet,
        queries_only=args.queries_only,
    )
    if len(out_df) == 0 and skip_dup and not args.quiet:
        print(
            "Ни одной строки: все отфильтрованы как «Явный дубликат». "
            "Запусти с флагом --include-duplicates, если нужны и они.",
            flush=True,
        )

    stem = path.stem
    out_path = REPORTS_DIR / f"АНАЛИТИКА_РЫНОК_{stem}.xlsx"
    try:
        out_df.to_excel(out_path, index=False)
    except PermissionError:
        out_path = REPORTS_DIR / f"АНАЛИТИКА_РЫНОК_{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        out_df.to_excel(out_path, index=False)
        print(
            "Файл с прежним именем занят (часто открыт в Excel) — записана копия с датой во имени.",
            flush=True,
        )
    print(f"Готово: {out_path} (строк: {len(out_df)})", flush=True)


if __name__ == "__main__":
    main()
