"""
Оценка «выгодности» тендера по сводке смета + рынок (СВОДКА_РЫНОК_*.xlsx).
Логика сопоставления смета ↔ рынок совпадает с карточкой «Сравнение» в report_merge_html.py
(та же шкала: медиана по «Рынок цены за ед. (итог)» с масштабом из «Ед. изм.» и та же цена сметы для сравнения).
"""

from __future__ import annotations

import html as html_std
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from autobot.market_analytics import (
    COL_DUP,
    COL_ITEM,
    COL_NAME,
    COL_QTY,
    COL_SUM,
    COL_UNIT,
    COL_UNIT_PRICE,
    estimate_block_qty_from_unit,
    recalc_estimate_qty_price_from_unit,
    unit_has_area_or_volume_marker,
)


_UNIT_LEADING_MV = re.compile(
    r"^\s*([\d\s\u00a0\u202f,]+)\s*(м\s*[2²]|м\s*[3³]|п\.?\s*м\.?)\s*$",
    re.IGNORECASE,
)


def _safe_float(x: Any) -> float | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v <= 0:  # noqa: PLR0124
        return None
    return v


def _quantity_multiplier_from_unit(unit_raw: str) -> tuple[float, str]:
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
        if 0 < v < 1e10:
            out.append(v)
    return out


def _estimate_numeric_for_compare(row: pd.Series) -> float | None:
    """Число в колонке «смета» для сравнения с рынком — как _smeta_unit_display в report_merge_html."""
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
        return None
    return float(use)


def _market_median_for_row(row: pd.Series, rub_col: str) -> float | None:
    raw = row.get(rub_col, "")
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip()
    if not s or s in ("—", "-", "nan", "None"):
        return None
    nums = _parse_semicolon_numbers(s)
    if not nums:
        return None
    q_scale, _ = _quantity_multiplier_from_unit(str(row.get(COL_UNIT, "") or ""))
    if q_scale and q_scale > 1.0:
        nums = [round(n * q_scale, 2) for n in nums]
    return float(statistics.median(nums))


def _rub_col(df: pd.DataFrame) -> str | None:
    if "Рынок цены за ед. (итог)" in df.columns:
        return "Рынок цены за ед. (итог)"
    if "Суммы из ответа (итог)" in df.columns:
        return "Суммы из ответа (итог)"
    if "Суммы из текста ответа (авто)" in df.columns:
        return "Суммы из текста ответа (авто)"
    return None


@dataclass
class ViabilityStats:
    rows_total: int
    rows_considered: int
    comparable: int
    no_market: int
    smeta_below_market: int
    smeta_near_market: int
    smeta_above_market: int
    median_ratio: float | None
    share_sum_smeta_where_cheaper_than_median: float | None


def compute_viability_stats(df: pd.DataFrame) -> ViabilityStats:
    df = recalc_estimate_qty_price_from_unit(df.copy())
    rc = _rub_col(df)
    rows_total = len(df)
    considered = 0
    comparable = 0
    no_market = 0
    below = near = above = 0
    ratios: list[float] = []
    sum_smeta_cheap = 0.0
    sum_smeta_all = 0.0

    for _, row in df.iterrows():
        if str(row.get(COL_DUP, "")).strip() == "Да":
            continue
        name = str(row.get(COL_NAME, "") or "").strip()
        if len(name) < 4:
            continue
        considered += 1
        if rc is None:
            no_market += 1
            continue
        est = _estimate_numeric_for_compare(row)
        mkt = _market_median_for_row(row, rc) if rc else None
        sm = _safe_float(row.get(COL_SUM))
        if est is None or mkt is None or mkt <= 0:
            no_market += 1
            continue
        comparable += 1
        r = est / mkt
        ratios.append(r)
        if sm is not None and sm > 0:
            sum_smeta_all += sm
            if r < 0.97:
                sum_smeta_cheap += sm
        if r < 0.92:
            below += 1
        elif r > 1.08:
            above += 1
        else:
            near += 1

    med_r = float(statistics.median(ratios)) if ratios else None
    share = (sum_smeta_cheap / sum_smeta_all) if sum_smeta_all > 0 else None

    return ViabilityStats(
        rows_total=rows_total,
        rows_considered=considered,
        comparable=comparable,
        no_market=no_market,
        smeta_below_market=below,
        smeta_near_market=near,
        smeta_above_market=above,
        median_ratio=med_r,
        share_sum_smeta_where_cheaper_than_median=share,
    )


def _verdict_label(st: ViabilityStats) -> tuple[str, str]:
    """(краткая оценка, css-класс)."""
    if st.comparable < 3:
        return "Мало строк с рынком — вывод осторожный.", "viability--warn"
    if st.median_ratio is None:
        return "Нет пар «смета / рынок» для сравнения.", "viability--warn"
    if st.median_ratio < 0.92:
        return (
            "Лимит сметы в среднем ниже рынка — маржа может быть узкой.",
            "viability--tight",
        )
    if st.median_ratio > 1.08:
        return (
            "Лимит сметы в среднем выше рынка — больше запас по цене.",
            "viability--room",
        )
    return (
        "Смета и рынок в среднем близки — маржу смотрите по строкам.",
        "viability--neutral",
    )


def build_viability_section_html(
    st: ViabilityStats,
    tender_id: str,
    *,
    narrative: str | None = None,
) -> str:
    title, cls = _verdict_label(st)
    med_s = f"{st.median_ratio:.2f}".replace(".", ",") if st.median_ratio is not None else "—"
    share_s = (
        f"{100.0 * st.share_sum_smeta_where_cheaper_than_median:.0f}%".replace(".", ",")
        if st.share_sum_smeta_where_cheaper_than_median is not None
        else "—"
    )
    narr_block = ""
    if narrative and narrative.strip():
        narr_block = (
            f'<div class="viability__narrative">{html_std.escape(narrative.strip()[:3500])}</div>'
        )

    tid_esc = html_std.escape((tender_id or "").strip())
    title_esc = html_std.escape(title)

    dyn_pro = ""
    if st.smeta_above_market > st.smeta_below_market:
        dyn_pro = f"<li>Смета выше рынка (&gt;8%): <b>{st.smeta_above_market}</b> — больше, чем «ниже рынка»: <b>{st.smeta_below_market}</b>.</li>"
    elif st.smeta_above_market > 0:
        dyn_pro = f"<li>Есть <b>{st.smeta_above_market}</b> поз. с запасом к рынку.</li>"

    dyn_con = ""
    if st.smeta_below_market > st.smeta_above_market:
        dyn_con = f"<li>Смета ниже рынка чаще, чем с запасом: <b>{st.smeta_below_market}</b> vs <b>{st.smeta_above_market}</b>.</li>"
    elif st.no_market > st.comparable and st.rows_considered > 0:
        dyn_con = f"<li>Много строк без рынка (<b>{st.no_market}</b>) — картина неполная.</li>"

    return f"""<section class="viability {cls}" aria-label="Смета и рынок">
  <div class="viability__head">Быстрый разбор · <code>{tid_esc}</code></div>
  <p class="viability__verdict"><strong>{title_esc}</strong></p>
  <ul class="viability__facts">
    <li>Строк в смете: <b>{st.rows_considered}</b> · с рынком: <b>{st.comparable}</b></li>
    <li>Смета дешевле рынка (&lt;92%): <b>{st.smeta_below_market}</b> · рядом: <b>{st.smeta_near_market}</b> · дороже (&gt;108%): <b>{st.smeta_above_market}</b></li>
    <li>Медиана «цена в таблице / медиана рынка»: <b>{med_s}</b></li>
    <li>Доля суммы, где отношение &lt;0,97: <b>{share_s}</b></li>
  </ul>
  <div class="viability__pros">
    <strong>Плюсы</strong>
    <ul>
      {dyn_pro}
      <li>Если смета чаще выше рынка — проще уложиться в лимит.</li>
      <li>Телефоны и ссылки — можно перепроверить спорные строки.</li>
    </ul>
  </div>
  <div class="viability__cons">
    <strong>Риски</strong>
    <ul>
      {dyn_con}
      <li>Много строк «смета ниже рынка» — потолок жёсткий.</li>
      <li>Нет рынка по части строк — картина неполная; догоните Алису.</li>
    </ul>
  </div>
  {narr_block}
  <p class="viability__muted">Цены с сайтов — ориентир, не НМЦК.</p>
</section>"""


def build_viability_narrative_openai(tender_id: str, st: ViabilityStats) -> str | None:
    """Опционально: короткий связный текст через OpenAI по уже посчитанным метрикам."""
    try:
        from autobot.analytics_openai import try_merge_viability_narrative

        return try_merge_viability_narrative(tender_id, st)
    except Exception:
        return None


def format_viability_for_telegram(tender_id: str) -> str | None:
    """
    HTML для Telegram (parse_mode=HTML) по сводке СВОДКА_РЫНОК после merge.
    Возвращает None, если файла нет или не прочитался.
    """
    from autobot.merge_estimate_market import OUT_PREFIX
    from autobot.report_prompt import REPORTS_DIR

    tid = (tender_id or "").strip()
    if not tid:
        return None
    path = REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx"
    if not path.is_file():
        return None
    try:
        df = pd.read_excel(path)
    except (OSError, ValueError):
        return None
    if COL_NAME not in df.columns:
        return None
    st = compute_viability_stats(df)
    title, _ = _verdict_label(st)
    med_s = f"{st.median_ratio:.2f}".replace(".", ",") if st.median_ratio is not None else "—"
    share_s = (
        f"{100.0 * st.share_sum_smeta_where_cheaper_than_median:.0f}%".replace(".", ",")
        if st.share_sum_smeta_where_cheaper_than_median is not None
        else "—"
    )
    nar = build_viability_narrative_openai(tid, st)

    lines: list[str] = [
        "📊 <b>Смета vs рынок</b> · <code>" + html_std.escape(tid) + "</code>",
        "",
        "<i>" + html_std.escape(title) + "</i>",
        "",
        f"• Строк: <b>{st.rows_considered}</b> · с рынком: <b>{st.comparable}</b> · без рынка: <b>{st.no_market}</b>",
        f"• Дешевле рынка (&lt;92%): <b>{st.smeta_below_market}</b> · рядом: <b>{st.smeta_near_market}</b> · дороже (&gt;108%): <b>{st.smeta_above_market}</b>",
        f"• Медиана «смета/рынок»: <b>{med_s}</b> · сумма при &lt;0,97: <b>{share_s}</b>",
        "",
        "<i>Цены с сайтов — ориентир, не НМЦК.</i>",
    ]
    if nar and nar.strip():
        lines.extend(["", "<b>AI</b>", html_std.escape(nar.strip()[:2200])])
    body = "\n".join(lines)
    if len(body) > 4000:
        body = body[:3990] + "…"
    return body


if __name__ == "__main__":
    import argparse

    from autobot.merge_estimate_market import OUT_PREFIX
    from autobot.report_prompt import REPORTS_DIR

    ap = argparse.ArgumentParser(description="Viability metrics from merged estimate+market XLSX")
    ap.add_argument("--tender-id", required=True)
    args = ap.parse_args()
    tid = args.tender_id.strip()
    p = REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx"
    if not p.is_file():
        raise SystemExit(f"Нет файла {p}")
    dfx = pd.read_excel(p)
    stx = compute_viability_stats(dfx)
    print(stx)
    nar = build_viability_narrative_openai(tid, stx)
    if nar:
        print("\n--- OpenAI ---\n", nar)
