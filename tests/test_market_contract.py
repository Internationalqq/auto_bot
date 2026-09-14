from __future__ import annotations

import json
import pandas as pd
import pytest

from autobot.market_analytics import COL_NAME, COL_UNIT, COL_QTY, COL_SUM, COL_UNIT_PRICE
from autobot.market_contract import BUNDLE_COLUMN, confirmed_prices, match_market_rows, merge_market_frames
from autobot.tender_viability import compute_viability_stats, _verdict_label


def position(name="Щебень гранитный 5-20", unit="м3", price=3000, qty=1, **extra):
    return {COL_NAME: name, COL_UNIT: unit, COL_QTY: qty, COL_UNIT_PRICE: price, COL_SUM: price * qty, **extra}


def offer(row, price=2500, verification="verified", **extra):
    return {**row, BUNDLE_COLUMN: json.dumps([{"price": price, "verification": verification,
        "url": "https://supplier.example/item", "matched_unit": row[COL_UNIT], **extra}])}


def test_same_name_different_units_cannot_share_price():
    cubic, tonne = position(), position(unit="т", price=2000)
    merged = merge_market_frames(pd.DataFrame([cubic, tonne]), pd.DataFrame([offer(cubic)]))
    assert merged["Рынок цены за ед. (итог)"].tolist() == ["2500", ""]
    assert merged[COL_UNIT].tolist() == ["м3", "т"]


def test_title_ruble_numbers_and_candidate_bundle_do_not_become_market_prices():
    row = position()
    market = offer(row, price=10, verification="candidate")
    market.update({"Рыночные источники": "Щебень — 10 руб.", "Цены за ед. (рынок, руб)": "10"})
    result = merge_market_frames(pd.DataFrame([row]), pd.DataFrame([market]))
    assert result.iloc[0]["Рынок цены за ед. (итог)"] == ""
    assert "10 руб." in result.iloc[0]["Рыночные источники"]
    assert compute_viability_stats(result).comparable == 0


@pytest.mark.parametrize("bundle", ["broken json", "{}", "[]", "", None])
def test_missing_or_corrupt_evidence_does_not_fall_back_to_old_numeric_columns(bundle):
    row = position(**{BUNDLE_COLUMN: bundle, "Рынок цены за ед. (итог)": "2500"})
    assert confirmed_prices(row) == []
    assert compute_viability_stats(pd.DataFrame([row])).comparable == 0


def test_same_name_unit_in_different_files_matches_its_own_source():
    a, b = position(**{"Файл ЛСР": "a.xlsx"}), position(**{"Файл ЛСР": "b.xlsx"})
    merged = merge_market_frames(pd.DataFrame([a, b]), pd.DataFrame([offer(b, 2600), offer(a, 2500)]))
    assert merged["Рынок цены за ед. (итог)"].tolist() == ["2500", "2600"]


def test_missing_context_does_not_guess_between_identical_rows():
    a, b = position(**{"Файл ЛСР": "a.xlsx"}), position(**{"Файл ЛСР": "b.xlsx"})
    assert match_market_rows(pd.DataFrame([a, b]), pd.DataFrame([offer(position())])) == [None, None]


def test_mismatched_version_and_unit_block_do_not_match():
    row = position(unit="100 м2", estimate_version="v2")
    market = offer(position(unit="м2", estimate_version="v1"))
    assert match_market_rows(pd.DataFrame([row]), pd.DataFrame([market])) == [None]


def test_cheap_rows_do_not_prove_expensive_unresearched_tender_is_profitable():
    rows = [offer(position(name=f"Материал номер {i}", unit="шт", price=100), price=80) for i in range(3)]
    rows.append(position(name="Основная дорогостоящая работа", unit="шт", price=1_000_000))
    stats = compute_viability_stats(pd.DataFrame(rows))
    assert stats.comparable == 3
    assert stats.coverage_cost_percent == pytest.approx(300 / 1_000_300 * 100)
    assert stats.uncovered_estimate_total == 1_000_000
    assert _verdict_label(stats)[1] == "viability--warn"
    assert "выгодным" not in _verdict_label(stats)[0]


def test_block_price_uses_actual_row_total_once():
    row = position(name="Покрытие площадки", unit="100 м2", price=10_000, qty=2)
    market = offer(row, price=80, matched_unit="м2")
    stats = compute_viability_stats(pd.DataFrame([market]))
    assert stats.comparable_estimate_total == 20_000
    assert stats.comparable_market_total == 16_000


def test_unknown_row_amount_keeps_coverage_unknown():
    a = offer(position(name="Материал первый"))
    b = position(name="Материал второй")
    b[COL_SUM] = b[COL_QTY] = None
    stats = compute_viability_stats(pd.DataFrame([a, b]))
    assert stats.rows_without_amount == 1
    assert stats.coverage_cost_percent is None
    assert _verdict_label(stats)[1] == "viability--warn"
