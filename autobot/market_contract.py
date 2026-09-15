"""Shared rules for joining estimate rows and using market evidence.

Spreadsheet text and old summary columns are presentation, not proof of a price.
Keep rejected evidence visible, but derive calculation fields from the bundle.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
from typing import Any, Mapping

import pandas as pd

from autobot.market_analytics import COL_NAME, COL_UNIT, COL_QTY, COL_SUM, COL_UNIT_PRICE
from autobot.market_strategy import (
    assess_price_plausibility, is_direct_source_url, normalize_unit, units_compatible, classify_position,
)
from autobot.market_evidence_policy import freshness_reason, specification_reason, select_independent_offers, region_key, price_terms_reason

BUNDLE_COLUMN = "Цена-сайт-телефон (json)"
CONTRACT_VERSION = 2


def clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (ValueError, TypeError):
        pass
    return re.sub(r"\s+", " ", str(value)).strip()


def key(value: Any) -> str:
    return clean(value).casefold().replace("ё", "е")


def decimal_number(value: Any) -> Decimal | None:
    try:
        result = Decimal(clean(value).replace(" ", "").replace(",", "."))
        return result if result.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def kopecks(value: Any) -> int | None:
    number = decimal_number(value)
    return int((number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if number is not None else None


def relative_market_total_kopecks(estimate_total: Any, estimate_unit: Any, market_unit: Any) -> int | None:
    amount, estimate, market = map(decimal_number, (estimate_total, estimate_unit, market_unit))
    if amount is None or estimate is None or market is None or estimate <= 0 or market <= 0 or amount < 0:
        return None
    return int((Decimal(kopecks(amount)) * market / estimate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _number(value: Any) -> float | None:
    number = decimal_number(value)
    return float(number) if number is not None else None


def offers_for_row(row: Mapping[str, Any]) -> list[dict]:
    """Validate stored evidence at every read boundary, including old reports."""
    try:
        bundle = json.loads(clean(row.get(BUNDLE_COLUMN)))
    except (ValueError, TypeError):
        return []
    if not isinstance(bundle, list):
        return []
    result = []
    seen = set()
    position = classify_position(row.get(COL_NAME), row.get(COL_UNIT), row.get('basis_code'), row.get('Раздел'))
    for item in bundle:
        if not isinstance(item, dict):
            continue
        offer = dict(item)
        price = _number(item.get("price"))
        reason = ""
        if key(item.get("verification")) != "verified":
            reason = clean(item.get("verification_reason")) or "Цена ещё не проверена"
        elif price is None or not 0 < price < 1e10:
            reason = "Нет корректной цены"
        elif not is_direct_source_url(clean(item.get("url"))):
            reason = "Нет прямой ссылки на предложение"
        elif COL_UNIT in row and clean(row.get(COL_UNIT)) in {"", "—", "-"}:
            reason = "Не определена единица позиции сметы"
        elif not clean(item.get("matched_unit")):
            reason = "Не подтверждена единица предложения"
        elif clean(item.get("matched_unit")) and clean(row.get(COL_UNIT)) and not units_compatible(
            normalize_unit(clean(row.get(COL_UNIT))), normalize_unit(clean(item.get("matched_unit")))
        ):
            reason = "Единица предложения не совпадает с позицией сметы"
        else:
            reason = freshness_reason(item, position.bucket) or price_terms_reason(item) or specification_reason(
                row.get(COL_NAME), clean(item.get('evidence')) or clean(item.get('snippet')) or clean(item.get('title')),
            )
            expected_region = region_key(row.get('Регион поиска')) or region_key(item.get('search_region'))
            if not reason and expected_region:
                if region_key(item.get('search_region')) != expected_region:
                    reason = 'Регион сохранённой цены не совпадает с текущим поиском'
                elif not clean(item.get('region_evidence')):
                    reason = 'Источник не подтверждает работу или доставку в выбранный регион'
            assessment = assess_price_plausibility(
                estimate_price=_number(row.get(COL_UNIT_PRICE)), market_price=price,
                name=clean(row.get(COL_NAME)), unit=clean(row.get(COL_UNIT)),
                quantity=_number(row.get(COL_QTY)), total=_number(row.get(COL_SUM)),
            )
            if not reason and assessment.status in {"review", "extreme"}:
                reason = assessment.reason
        offer["price"] = price
        offer["verification"] = "candidate" if reason else "verified"
        if reason:
            offer["verification_reason"] = reason
        identity = (clean(offer.get("url")), price, offer["verification"])
        if identity in seen:
            continue
        seen.add(identity)
        result.append(offer)
    return result


def confirmed_prices(row: Mapping[str, Any]) -> list[float]:
    return [offer["price"] for offer in select_independent_offers(
        [offer for offer in offers_for_row(row) if offer["verification"] == "verified"])]


def sanitize_market_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Recompute trusted numeric columns without destroying original source text."""
    import statistics

    result = frame.copy()
    bundles, prices, medians, lows, highs, counts, candidates, statuses = [], [], [], [], [], [], [], []
    for _, row in result.iterrows():
        offers = offers_for_row(row)
        values = [offer["price"] for offer in select_independent_offers(
            [offer for offer in offers if offer["verification"] == "verified"])]
        bundles.append(json.dumps(offers, ensure_ascii=False, allow_nan=False))
        prices.append("; ".join(format(Decimal(str(v)).normalize(), "f") for v in values))
        medians.append(statistics.median(values) if values else None)
        lows.append(min(values) if values else None)
        highs.append(max(values) if values else None)
        counts.append(len(values))
        candidate_count = sum(offer['verification'] != 'verified' for offer in offers)
        candidates.append(candidate_count)
        status = clean(row.get('Ошибка / статус'))
        if offers:
            status = f"В расчёте источников: {len(values)}; кандидатов: {candidate_count}"
            if not values and candidate_count:
                reasons = list(dict.fromkeys(clean(offer.get('verification_reason')) for offer in offers))
                status += '. ' + '; '.join(reason for reason in reasons if reason)
        statuses.append(status)
    result[BUNDLE_COLUMN] = bundles
    for column in ("Цены за ед. (рынок, руб)", "Рынок цены за ед. (итог)", "Суммы из ответа (итог)"):
        result[column] = prices
    result["Медиана цена за ед. (рынок)"] = medians
    result["Мин цена за ед. (рынок)"] = lows
    result["Макс цена за ед. (рынок)"] = highs
    result["Подтверждённых источников"] = counts
    result["Проверенных источников"] = counts
    result["Непроверенных кандидатов"] = candidates
    result["Ошибка / статус"] = statuses
    result["Версия проверки рынка"] = CONTRACT_VERSION
    return result


# These are coordinates/identity, not money fields. Missing legacy coordinates
# can match only when both sides have exactly one possible owner.
_CONTEXT = ("position_id", "estimate_version", "Файл ЛСР", "Лист", "Строка Excel", "№ п/п", "basis_code", "Раздел")


def position_identity(row: Mapping[str, Any]) -> str:
    fields = (COL_NAME, COL_UNIT, *_CONTEXT, COL_QTY, COL_UNIT_PRICE, COL_SUM)
    values = []
    for field in fields:
        number = decimal_number(row.get(field)) if field in (COL_QTY, COL_UNIT_PRICE, COL_SUM) else None
        values.append(format(number.normalize(), "f") if number is not None else key(row.get(field)))
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()[:32]


def _compatible(left: Mapping, right: Mapping) -> bool:
    lv, rv = clean(left.get('estimate_version')), clean(right.get('estimate_version'))
    if (lv.startswith('correction:') or rv.startswith('correction:')) and lv != rv:
        return False
    if key(left.get(COL_NAME)) != key(right.get(COL_NAME)):
        return False
    lu, ru = clean(left.get(COL_UNIT)), clean(right.get(COL_UNIT))
    if lu and ru and normalize_unit(lu) != normalize_unit(ru):
        return False
    # A block of 100 m² and 1 m² have the same base unit but different prices.
    if lu and ru:
        from autobot.market_strategy import estimate_unit_multiplier
        if estimate_unit_multiplier("", lu) != estimate_unit_multiplier("", ru):
            return False
    if any(key(left.get(c)) and key(right.get(c)) and key(left.get(c)) != key(right.get(c)) for c in _CONTEXT):
        return False
    for column in (COL_QTY, COL_UNIT_PRICE, COL_SUM):
        a, b = decimal_number(left.get(column)), decimal_number(right.get(column))
        if a is not None and b is not None and abs(a - b) > Decimal("0.005"):
            return False
    return True


def match_market_rows(estimate: pd.DataFrame, market: pd.DataFrame) -> list[dict | None]:
    """Return a unique one-to-one match in estimate order; never guess a duplicate."""
    groups = defaultdict(list)
    market_rows = market.to_dict("records")
    for index, row in enumerate(market_rows):
        if key(row.get(COL_NAME)):
            groups[key(row.get(COL_NAME))].append(index)
    candidates = []
    owners = defaultdict(list)
    for index, row in enumerate(estimate.to_dict("records")):
        choices = [i for i in groups.get(key(row.get(COL_NAME)), []) if _compatible(row, market_rows[i])]
        candidates.append(choices)
        for i in choices:
            owners[i].append(index)
    return [dict(market_rows[choices[0]]) if len(choices) == 1 and len(owners[choices[0]]) == 1 else None
            for choices in candidates]


def merge_market_frames(estimate: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    """Preserve estimate coordinates, values and order; enrich only unambiguous rows."""
    protected = set(estimate.columns) | set(_CONTEXT) | {COL_NAME, COL_UNIT, COL_QTY, COL_SUM, COL_UNIT_PRICE}
    matches = match_market_rows(estimate, market)
    rows = []
    for original, matched in zip(estimate.to_dict("records"), matches):
        row = dict(original)
        # Existing derived fields must not survive a missing/new ambiguous match.
        for column in market.columns:
            if column not in protected:
                row[column] = None
        row[BUNDLE_COLUMN] = "[]"
        row["Рынок обработано"] = "Да" if matched is not None else "Нет"
        row["Сопоставление рынка"] = "Позиция сопоставлена" if matched is not None else "Нет однозначного соответствия"
        if matched is not None:
            for column, value in matched.items():
                if column not in {COL_NAME, COL_UNIT, COL_QTY, COL_SUM, COL_UNIT_PRICE, *_CONTEXT} and not (
                    column == 'Регион поиска' and clean(original.get('Регион поиска'))
                ):
                    row[column] = value
            if clean(matched.get(COL_UNIT)) in {"", "—", "-"}:
                # Keep legacy sources inspectable without guessing their unit.
                candidates = offers_for_row(matched)
                for offer in candidates:
                    offer["verification"] = "candidate"
                    offer["verification_reason"] = "Не определена единица сохранённого результата"
                row[BUNDLE_COLUMN] = json.dumps(candidates, ensure_ascii=False, allow_nan=False)
        rows.append(row)
    return sanitize_market_frame(pd.DataFrame(rows, columns=list(dict.fromkeys([*estimate.columns, *market.columns, "Рынок обработано", "Сопоставление рынка", BUNDLE_COLUMN]))))
