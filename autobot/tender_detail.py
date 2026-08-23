"""View model for the always-available tender detail page."""

from __future__ import annotations

import json
import hashlib
import math
import re
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from autobot.market_analytics import COL_NAME, COL_QTY, COL_SUM, COL_UNIT, COL_UNIT_PRICE
from autobot.market_strategy import (
    assess_price_plausibility,
    build_search_plan,
    estimate_unit_multiplier,
    is_direct_source_url,
    normalize_unit,
)
from autobot.market_price_index import latest_parser_health, source_quality, weighted_median
from autobot.merge_estimate_market import OUT_PREFIX
from autobot.report_prompt import REPORTS_DIR


def _clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return re.sub(r"\s+", " ", str(value)).strip()


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(value: Any) -> int:
    number = _number(value)
    return max(0, int(number or 0))


def _fmt_number(value: Any, digits: int = 2) -> str:
    number = _number(value)
    if number is None:
        return "—"
    if abs(number - round(number)) < 1e-8:
        return f"{int(round(number)):,}".replace(",", " ")
    return f"{number:,.{digits}f}".replace(",", " ").replace(".", ",")


def _fmt_money(value: Any) -> str:
    number = _number(value)
    return f"{_fmt_number(number)} ₽" if number is not None else "—"


def _key(value: Any) -> str:
    return re.sub(r"\s+", " ", _clean(value).casefold().replace("ё", "е"))


def _read_excel(path: Path) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    try:
        return pd.read_excel(path)
    except Exception:
        return pd.DataFrame()


def _market_path(tender_id: str) -> Path:
    expected = REPORTS_DIR / f"РЫНОК_ИСТОЧНИКИ_ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.xlsx"
    if expected.is_file():
        return expected
    matches = sorted(REPORTS_DIR.glob(f"РЫНОК_ИСТОЧНИКИ_*{tender_id}*.xlsx"))
    return matches[0] if matches else expected


def _file_updated(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%d.%m.%Y %H:%M")
    except OSError:
        return ""


def _estimate_parse_manifest(tender_id: str) -> dict[str, Any]:
    path = REPORTS_DIR / f"ESTIMATE_PARSE_{tender_id}.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _natural_tokens(value: Any) -> tuple[tuple[int, Any], ...]:
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", _clean(value).casefold())
    )


def _position_sort_key(row: pd.Series, original_index: int) -> tuple[Any, ...]:
    source_file = _clean(row.get("Файл ЛСР", ""))
    source_name = Path(source_file).name
    page_match = re.search(r"стр\.\s*(\d+)", _clean(row.get("Лист", "")), re.IGNORECASE)
    page_number = int(page_match.group(1)) if page_match else 1_000_000
    return (
        _natural_tokens(source_name),
        page_number,
        original_index,
    )


def _file_title(source_file: str, file_number: int) -> str:
    stem = Path(source_file).stem if source_file else ""
    stem = re.sub(r"\s*-\s*лср\s*$", "", stem, flags=re.IGNORECASE).strip(" -")
    return f"Файл {file_number}" + (f" · {stem}" if stem else "")


def _section_title(section: str) -> str:
    title = _clean(section)
    if title and title.casefold() != "распознано из лср pdf":
        match = re.match(r"^(Раздел\s+\d+)(?:[.\s]+(.*))?$", title, flags=re.IGNORECASE)
        if match and match.group(2) and not re.search(r"[a-zа-яё]", match.group(2), flags=re.IGNORECASE):
            return match.group(1)
        return title
    return "Позиции без указанного раздела"


def _display_unit(
    name: str,
    unit: str,
    *,
    estimate_price: float | None = None,
    quantity: float | None = None,
    total: float | None = None,
) -> str:
    multiplier = estimate_unit_multiplier(
        name,
        unit,
        estimate_price=estimate_price,
        quantity=quantity,
        total=total,
    )
    if multiplier <= 1:
        return unit or "—"
    base = normalize_unit(unit) or unit
    base = base.replace("м2", "м²").replace("м3", "м³")
    return f"{multiplier:g} {base}"


def _parse_bundle(
    raw: Any,
    *,
    estimate_price: float | None = None,
    name: str = "",
    unit: str = "",
    quantity: float | None = None,
    total: float | None = None,
) -> list[dict[str, Any]]:
    text = _clean(raw)
    if not text:
        return []
    try:
        payload = json.loads(text)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    result: list[dict[str, Any]] = []
    for item in payload[:12]:
        if not isinstance(item, dict):
            continue
        url = _clean(item.get("url"))
        if not is_direct_source_url(url):
            continue
        verification = _clean(item.get("verification")).casefold()
        if verification not in {"verified", "candidate"}:
            verification = "candidate"
            reason = "Результат старого формата — требуется повторная проверка"
        else:
            reason = _clean(item.get("verification_reason"))
        price = _number(item.get("price"))
        plausibility = assess_price_plausibility(
            estimate_price=estimate_price,
            market_price=price,
            name=name,
            unit=unit,
            quantity=quantity,
            total=total,
        )
        if verification == "verified" and plausibility.status in {"review", "extreme"}:
            verification = "candidate"
            reason = plausibility.reason
        multiplier = plausibility.multiplier
        comparison_price = price * multiplier if price is not None else None
        base_unit = normalize_unit(unit).replace("м2", "м²").replace("м3", "м³")
        result.append(
            {
                "source": _clean(item.get("source")) or url.split("/", 3)[2],
                "title": _clean(item.get("title")) or "Источник цены",
                "price": price,
                "price_fmt": _fmt_money(price),
                "url": url,
                "verification": verification,
                "verified": verification == "verified",
                "reason": reason or ("Источник прошёл проверку" if verification == "verified" else "Нужна ручная проверка"),
                "confidence": max(0, min(100, round((_number(item.get("confidence")) or 0) * 100))),
                "source_weight": max(0, min(100, round((_number(item.get("source_weight")) or source_quality(url, item.get("source"))) * 100))),
                "index_hit": bool(item.get("index_hit")),
                "index_match_score": _number(item.get("index_match_score")),
                "audit_record_path": _clean(item.get("audit_record_path")),
                "snapshot_path": _clean(item.get("snapshot_path")),
                "matched_unit": _clean(item.get("matched_unit")),
                "observed_at": _clean(item.get("observed_at")),
                "evidence": _clean(item.get("evidence") or item.get("snippet")),
                "published_at": _clean(item.get("published_at")),
                "location": _clean(item.get("location")),
                "plausibility": plausibility.status,
                "estimate_ratio": plausibility.ratio,
                "estimate_ratio_fmt": f"×{plausibility.ratio:.2f}" if plausibility.ratio is not None else "—",
                "comparison_price": comparison_price,
                "comparison_price_fmt": _fmt_money(comparison_price),
                "price_basis": f"за 1 {base_unit}" if base_unit else "за единицу источника",
            }
        )
    return result


def _market_rows_by_name(df: pd.DataFrame) -> dict[str, pd.Series]:
    if df.empty or COL_NAME not in df.columns:
        return {}
    rows: dict[str, pd.Series] = {}
    for _, row in df.iterrows():
        name_key = _key(row.get(COL_NAME, ""))
        if name_key:
            rows[name_key] = row
    return rows


def _verdict(estimate_unit: float | None, market_unit: float | None) -> tuple[str, str]:
    if market_unit is None:
        return "Нет подтверждённой цены", "empty"
    if estimate_unit is None or estimate_unit <= 0:
        return "Цена есть, сравнивать не с чем", "warn"
    ratio = estimate_unit / market_unit if market_unit > 0 else 0
    if 0.8 <= ratio <= 1.25:
        return "Сопоставимо со сметой", "warn"
    if ratio > 1.25:
        return "Ниже сметы", "good"
    if ratio < 0.8:
        return "Выше сметы", "bad"
    return "Сопоставимо со сметой", "warn"


def build_tender_detail(tender_id: str, metadata: dict[str, Any], workflow: dict[str, Any]) -> dict[str, Any]:
    estimate_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.xlsx"
    market_path = _market_path(tender_id)
    comparison_path = REPORTS_DIR / f"{OUT_PREFIX}{tender_id}.xlsx"
    estimate = _read_excel(estimate_path)
    market = _read_excel(market_path)
    parse_manifest = _estimate_parse_manifest(tender_id)
    market_by_name = _market_rows_by_name(market)

    positions: list[dict[str, Any]] = []
    file_ids: dict[str, str] = {}
    section_ids: dict[tuple[str, str], str] = {}
    section_position_indexes: dict[str, int] = {}
    counts = {
        "works": 0,
        "materials": 0,
        "other": 0,
        "processed": 0,
        "verified": 0,
        "candidates": 0,
        "attention": 0,
    }
    estimate_total = 0.0
    estimate_rows_with_order = list(enumerate(estimate.iterrows()))
    estimate_rows_with_order.sort(key=lambda pair: _position_sort_key(pair[1][1], pair[0]))
    estimate_rows = [pair for _, pair in estimate_rows_with_order]
    for index, (_, row) in enumerate(estimate_rows, start=1):
        name = _clean(row.get(COL_NAME, ""))
        if not name:
            continue
        unit = _clean(row.get(COL_UNIT, ""))
        basis_code = _clean(row.get("basis_code", ""))
        section = _clean(row.get("Раздел", ""))
        source_file = _clean(row.get("Файл ЛСР", ""))
        file_key = source_file or "other"
        if file_key not in file_ids:
            file_ids[file_key] = f"estimate-file-{len(file_ids) + 1}"
        file_group = file_ids[file_key]
        section_key = (file_key, section or "other")
        if section_key not in section_ids:
            section_ids[section_key] = f"estimate-section-{len(section_ids) + 1}"
        section_group = section_ids[section_key]
        section_position_indexes[section_group] = section_position_indexes.get(section_group, 0) + 1
        section_position_index = section_position_indexes[section_group]
        plan = build_search_plan(name, unit, basis_code, section, metadata.get("region", ""))
        bucket = plan.position.bucket if plan.position.bucket in counts else "other"
        counts[bucket] += 1
        if plan.position.needs_decomposition:
            counts["attention"] += 1

        estimate_unit = _number(row.get(COL_UNIT_PRICE))
        row_total = _number(row.get(COL_SUM))
        if row_total is not None:
            estimate_total += row_total
        market_row = market_by_name.get(_key(name))
        market_processed = market_row is not None
        if market_processed:
            counts["processed"] += 1
        sources = _parse_bundle(
            market_row.get("Цена-сайт-телефон (json)", ""),
            estimate_price=estimate_unit,
            name=name,
            unit=unit,
            quantity=_number(row.get(COL_QTY)),
            total=row_total,
        ) if market_row is not None else []
        verified_sources = [source for source in sources if source["verified"]]
        candidate_sources = [source for source in sources if not source["verified"]]
        verified_count = len(verified_sources)
        candidate_count = len(candidate_sources)
        verified_prices = [source["price"] for source in verified_sources if source["price"] is not None]
        verified_weighted_prices = [
            (
                source["price"],
                max(0.01, float(source.get("source_weight") or 1) / 100)
                * max(0.05, float(source.get("confidence") or 1) / 100),
            )
            for source in verified_sources
            if source["price"] is not None
        ]
        market_median_base = weighted_median(verified_weighted_prices) if verified_prices else None
        if not verified_prices:
            verified_count = 0
        unit_multiplier = estimate_unit_multiplier(
            name,
            unit,
            estimate_price=estimate_unit,
            quantity=_number(row.get(COL_QTY)),
            total=row_total,
        )
        market_median = market_median_base * unit_multiplier if market_median_base is not None else None
        market_ratio = market_median / estimate_unit if market_median is not None and estimate_unit and estimate_unit > 0 else None
        if verified_count:
            counts["verified"] += 1
        elif candidate_count:
            counts["candidates"] += 1
        verdict, verdict_class = _verdict(estimate_unit, market_median)
        status_text = _clean(market_row.get("Ошибка / статус", "")) if market_row is not None else ""

        positions.append(
            {
                "position_key": hashlib.sha256(
                    "\x1f".join((name.casefold(), unit.casefold(), section.casefold(), source_file.casefold())).encode("utf-8")
                ).hexdigest()[:24],
                "index": index,
                "item_no": _clean(row.get("№ п/п", "")) or str(section_position_index),
                "name": name,
                "section": section,
                "section_note": "",
                "section_title": _section_title(section),
                "section_group": section_group,
                "source_file": source_file,
                "file_title": _file_title(source_file, len(file_ids)),
                "file_group": file_group,
                "basis_code": basis_code,
                "type_slug": plan.position.slug,
                "type_label": plan.position.label,
                "bucket": bucket,
                "bucket_label": plan.position.bucket_label,
                "classification_reason": plan.position.reason,
                "unit": _display_unit(
                    name,
                    unit,
                    estimate_price=estimate_unit,
                    quantity=_number(row.get(COL_QTY)),
                    total=row_total,
                ),
                "quantity": _number(row.get(COL_QTY)),
                "quantity_fmt": _fmt_number(row.get(COL_QTY)),
                "estimate_unit": estimate_unit,
                "estimate_unit_fmt": _fmt_money(estimate_unit),
                "estimate_total": row_total,
                "estimate_total_fmt": _fmt_money(row_total),
                "market_unit": market_median,
                "market_unit_fmt": _fmt_money(market_median),
                "market_base_unit": market_median_base,
                "market_base_unit_fmt": _fmt_money(market_median_base),
                "market_ratio": market_ratio,
                "market_ratio_fmt": f"×{market_ratio:.2f} к смете" if market_ratio is not None else "",
                "market_basis_note": (
                    f"{_fmt_money(market_median_base)} за 1 {normalize_unit(unit).replace('м2', 'м²').replace('м3', 'м³')}"
                    if market_median_base is not None and unit_multiplier > 1 else ""
                ),
                "market_processed": market_processed,
                "verified_count": verified_count,
                "candidate_count": candidate_count,
                "sources": sources,
                "verdict": verdict,
                "verdict_class": verdict_class,
                "strategy": plan.strategy_label,
                "source_strategy": plan.source_label,
                "can_auto_price": plan.can_auto_price,
                "warning": plan.warning,
                "market_status": status_text,
                "queries": list(plan.queries),
            }
        )

    file_summaries: dict[str, dict[str, Any]] = {}
    section_summaries: dict[str, dict[str, Any]] = {}
    for position in positions:
        file_summary = file_summaries.setdefault(
            position["file_group"],
            {"count": 0, "total": 0.0},
        )
        file_summary["count"] += 1
        file_summary["total"] += position["estimate_total"] or 0.0
        summary = section_summaries.setdefault(
            position["section_group"],
            {"count": 0, "total": 0.0},
        )
        summary["count"] += 1
        summary["total"] += position["estimate_total"] or 0.0
    previous_file_group = ""
    previous_section_group = ""
    for position in positions:
        file_summary = file_summaries[position["file_group"]]
        summary = section_summaries[position["section_group"]]
        position["file_start"] = position["file_group"] != previous_file_group
        position["file_count"] = file_summary["count"]
        position["file_total_fmt"] = _fmt_money(file_summary["total"])
        position["section_start"] = position["section_group"] != previous_section_group
        position["section_count"] = summary["count"]
        position["section_total_fmt"] = _fmt_money(summary["total"])
        previous_file_group = position["file_group"]
        previous_section_group = position["section_group"]

    total_positions = len(positions)
    coverage = round(counts["verified"] * 100 / total_positions) if total_positions else 0
    initial_price = _number(metadata.get("price_rub"))
    estimate_files_count = 0
    if not estimate.empty and "Файл ЛСР" in estimate.columns:
        estimate_files_count = len({_clean(value) for value in estimate["Файл ЛСР"] if _clean(value)})
    estimate_files_selected = _int(parse_manifest.get("selected_pdf_count")) or estimate_files_count
    estimate_files_parsed = _int(parse_manifest.get("parsed_pdf_count")) or estimate_files_count
    estimate_empty_files = [
        _clean(value)
        for value in parse_manifest.get("empty_pdf_files", [])
        if _clean(value)
    ] if isinstance(parse_manifest.get("empty_pdf_files"), list) else []
    estimate_official_total = _number(parse_manifest.get("official_total_rub"))
    estimate_official_files_count = _int(parse_manifest.get("official_total_files_count"))
    estimate_official_missing_files = [
        _clean(value)
        for value in parse_manifest.get("official_total_missing_files", [])
        if _clean(value)
    ] if isinstance(parse_manifest.get("official_total_missing_files"), list) else []
    estimate_total_with_vat = estimate_official_total * 1.22 if estimate_official_total else None
    comparison_total = estimate_official_total or (estimate_total if estimate_total > 0 else None)
    comparison_basis = "official" if estimate_official_total else "positions"
    if initial_price and initial_price > 0 and estimate_official_total and estimate_total_with_vat:
        direct_diff = abs(initial_price - estimate_official_total)
        vat_diff = abs(initial_price - estimate_total_with_vat)
        if vat_diff < direct_diff:
            comparison_total = estimate_total_with_vat
            comparison_basis = "official_with_vat"
    estimate_ratio = comparison_total / initial_price if comparison_total and initial_price and initial_price > 0 else None
    estimate_match_pct = round(estimate_ratio * 100, 1) if estimate_ratio is not None else None
    estimate_gap = initial_price - comparison_total if initial_price is not None and comparison_total else None
    estimate_check_class = "neutral"
    estimate_check_title = "Недостаточно данных для сверки"
    estimate_check_detail = "Нужны начальная цена и распознанные позиции смет."
    if estimate_ratio is not None:
        direct_diff = abs(1 - estimate_ratio)
        if direct_diff <= 0.06:
            estimate_check_class = "good"
            if comparison_basis == "official_with_vat":
                estimate_check_title = "Итоги ЛСР близки к НМЦК с учётом НДС"
                estimate_check_detail = "Сравнение использует строку «ВСЕГО по смете» каждого ЛСР и ориентир с НДС 22%. Расхождение не превышает 6%."
            else:
                estimate_check_title = "Итоги ЛСР близки к начальной цене"
                estimate_check_detail = "Расхождение официальных итогов ЛСР с НМЦК не превышает 6%."
        elif estimate_ratio < 0.75:
            estimate_check_class = "bad"
            estimate_check_title = "Найдена только часть смет"
            estimate_check_detail = "Сумма распознанных позиций покрывает меньше 75% начальной цены — нужно искать пропущенные ЛСР или разделы."
        elif estimate_ratio < 0.94:
            estimate_check_class = "warn"
            estimate_check_title = "Есть заметное расхождение"
            estimate_check_detail = "Возможно, не учтены НДС, коэффициенты, оборудование или отдельные локальные сметы."
        elif estimate_ratio > 1.06:
            estimate_check_class = "warn"
            estimate_check_title = "Сумма позиций выше начальной цены"
            estimate_check_detail = "Возможны дубли позиций или смешение нескольких версий смет."
    if estimate_files_selected > estimate_files_parsed:
        if estimate_check_class == "good":
            estimate_check_class = "warn"
            estimate_check_title = "Не все ЛСР распознаны"
        missing_label = ", ".join(estimate_empty_files[:3]) or "неизвестный файл"
        estimate_check_detail = f"Без позиций: {missing_label}. {estimate_check_detail}"
    if estimate_files_selected and estimate_official_files_count < estimate_files_selected:
        if estimate_check_class == "good":
            estimate_check_class = "warn"
            estimate_check_title = "Не все итоги ЛСР распознаны"
        missing_label = ", ".join(estimate_official_missing_files[:3]) or "неизвестный файл"
        estimate_check_detail = f"Не найдено «ВСЕГО по смете»: {missing_label}. {estimate_check_detail}"
    if comparison_basis == "positions":
        estimate_check_detail = f"Официальные итоги ЛСР не найдены; контроль рассчитан по сумме распознанных позиций. {estimate_check_detail}"
    steps = (
        {"key": "documents", "label": "Документы", "done": bool(workflow.get("has_downloads"))},
        {"key": "estimate", "label": "Смета", "done": estimate_path.is_file()},
        {"key": "market", "label": "Проверка цен", "done": market_path.is_file() and counts["verified"] > 0},
        {"key": "comparison", "label": "Сравнение", "done": comparison_path.is_file() and counts["verified"] > 0},
    )
    market_health = latest_parser_health(tender_id)
    if market_health:
        market_health["offer_rate_pct"] = round(float(market_health.get("offer_rate") or 0) * 100)
        baseline = market_health.get("baseline_rate")
        market_health["baseline_rate_pct"] = round(float(baseline) * 100) if baseline is not None else None
        market_health["degraded"] = bool(market_health.get("degraded"))
    return {
        "tender_id": tender_id,
        "title": _clean(metadata.get("title")) or f"Закупка № {tender_id}",
        "customer_name": _clean(metadata.get("customer_name")),
        "region": _clean(metadata.get("region")),
        "law": _clean(workflow.get("law") or metadata.get("law")),
        "purchase_method": _clean(workflow.get("purchase_method") or metadata.get("purchase_method")),
        "law_method_label": _clean(workflow.get("law_method_label")),
        "stage": _clean(workflow.get("eis_stage") or metadata.get("stage")),
        "price": initial_price,
        "price_fmt": _fmt_money(initial_price),
        "publish_date": _clean(metadata.get("publish_date")),
        "updated_date": _clean(metadata.get("updated_date")),
        "eis_url": _clean(workflow.get("eis_url") or metadata.get("url")),
        "next_action": _clean(workflow.get("next_action")),
        "status_label": _clean(workflow.get("status_label")),
        "status_detail": _clean(workflow.get("status_detail")),
        "steps": steps,
        "market_health": market_health,
        "positions": positions,
        "counts": counts,
        "total_positions": total_positions,
        "coverage": coverage,
        "estimate_total": estimate_total if estimate_total else None,
        "estimate_total_fmt": _fmt_money(estimate_total) if estimate_total else "—",
        "estimate_files_count": estimate_files_count,
        "estimate_files_selected": estimate_files_selected,
        "estimate_files_parsed": estimate_files_parsed,
        "estimate_empty_files": estimate_empty_files,
        "estimate_official_total": estimate_official_total,
        "estimate_official_total_fmt": _fmt_money(estimate_official_total),
        "estimate_official_files_count": estimate_official_files_count,
        "estimate_official_missing_files": estimate_official_missing_files,
        "estimate_total_with_vat": estimate_total_with_vat,
        "estimate_total_with_vat_fmt": _fmt_money(estimate_total_with_vat),
        "estimate_comparison_basis": comparison_basis,
        "estimate_match_pct": estimate_match_pct,
        "estimate_match_pct_fmt": _fmt_number(estimate_match_pct, digits=1),
        "estimate_match_bar": min(100, max(0, estimate_match_pct or 0)),
        "estimate_gap": estimate_gap,
        "estimate_gap_fmt": _fmt_money(abs(estimate_gap)) if estimate_gap is not None else "—",
        "estimate_gap_direction": "не хватает" if estimate_gap is not None and estimate_gap >= 0 else "выше НМЦК на",
        "estimate_check_class": estimate_check_class,
        "estimate_check_title": estimate_check_title,
        "estimate_check_detail": estimate_check_detail,
        "has_estimate": estimate_path.is_file(),
        "has_market": market_path.is_file(),
        "has_comparison": comparison_path.is_file(),
        "estimate_updated": _file_updated(estimate_path),
        "market_updated": _file_updated(market_path),
        "comparison_updated": _file_updated(comparison_path),
    }
