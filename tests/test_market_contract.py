from __future__ import annotations

import json
from datetime import datetime, timezone
import pandas as pd
import pytest

from autobot.market_analytics import COL_NAME, COL_UNIT, COL_QTY, COL_SUM, COL_UNIT_PRICE
from autobot.market_contract import BUNDLE_COLUMN, confirmed_prices, match_market_rows, merge_market_frames
from autobot.tender_viability import compute_viability_stats, _verdict_label


def position(name="Щебень гранитный 5-20", unit="м3", price=3000, qty=1, **extra):
    return {COL_NAME: name, COL_UNIT: unit, COL_QTY: qty, COL_UNIT_PRICE: price, COL_SUM: price * qty, **extra}


def offer(row, price=2500, verification="verified", **extra):
    return {**row, BUNDLE_COLUMN: json.dumps([{"price": price, "verification": verification,
        "url": "https://supplier.example/item", "matched_unit": row[COL_UNIT],
        "observed_at": datetime.now(timezone.utc).isoformat(), **extra}])}


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


@pytest.mark.parametrize('details', [
    {'extractor':'metadata', 'evidence':'Щебень гранитный 5-20 2500 руб/м3'},
    {'extractor':'price-block', 'evidence':'Похожие товары. Щебень гранитный 5-20 2500 руб/м3'},
])
def test_old_metadata_and_recommendation_prices_are_rechecked(details):
    assert not confirmed_prices(offer(position(), **details))


@pytest.mark.parametrize("bundle", ["broken json", "{}", "[]", "", None])
def test_missing_or_corrupt_evidence_does_not_fall_back_to_old_numeric_columns(bundle):
    row = position(**{BUNDLE_COLUMN: bundle, "Рынок цены за ед. (итог)": "2500"})
    assert confirmed_prices(row) == []
    assert compute_viability_stats(pd.DataFrame([row])).comparable == 0


def test_same_name_unit_in_different_files_matches_its_own_source():
    a, b = position(**{"Файл ЛСР": "a.xlsx"}), position(**{"Файл ЛСР": "b.xlsx"})
    merged = merge_market_frames(pd.DataFrame([a, b]), pd.DataFrame([offer(b, 2600), offer(a, 2500)]))
    assert merged["Рынок цены за ед. (итог)"].tolist() == ["2500", "2600"]


def test_xlsx_mixed_primary_and_resource_numbers_keep_market_identity(tmp_path):
    from autobot.market_contract import position_identity
    rows=[position(**{'№ п/п':'42', 'position_id':'pdf:12:42'}),
          position(**{'№ п/п':'42.1', 'position_id':'pdf:12:42.1'})]
    path=tmp_path/'market.xlsx'
    pd.DataFrame([offer(row) for row in rows]).to_excel(path,index=False)
    saved=pd.read_excel(path)
    assert [position_identity(row) for row in rows] == [position_identity(row) for _,row in saved.iterrows()]
    merged=merge_market_frames(pd.DataFrame(rows),saved)
    assert merged['Рынок цены за ед. (итог)'].tolist()==['2500','2500']
    assert not match_market_rows(pd.DataFrame([rows[0]]), saved.iloc[1:])[0]


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


def test_saved_price_without_observation_date_requires_new_search():
    data = offer(position())
    bundle = json.loads(data[BUNDLE_COLUMN])
    bundle[0].pop('observed_at')
    data[BUNDLE_COLUMN] = json.dumps(bundle)
    assert confirmed_prices(data) == []
    assert compute_viability_stats(pd.DataFrame([data])).comparable == 0


def test_market_row_cannot_overwrite_current_search_region():
    estimate = position(**{'Регион поиска': 'Ярославль'})
    market = offer(position(**{'Регион поиска': 'Москва'}), search_region='Москва', region_evidence='Бетон в Москве')
    merged = merge_market_frames(pd.DataFrame([estimate]), pd.DataFrame([market]))
    assert merged.iloc[0]['Регион поиска'] == 'Ярославль'
    assert confirmed_prices(merged.iloc[0]) == []
    assert json.loads(merged.iloc[0][BUNDLE_COLUMN])[0]['verification'] == 'candidate'


def test_common_calculation_uses_one_quote_per_source():
    data = position(price=200)
    bundle = []
    for n in range(10):
        bundle.append({'price': 100, 'verification': 'verified', 'url': f'https://same.example/item/{n}',
                       'matched_unit': 'м3', 'observed_at': datetime.now(timezone.utc).isoformat()})
    bundle.append(dict(bundle[0], price=200, url='https://other.example/item'))
    data[BUNDLE_COLUMN] = json.dumps(bundle)
    assert sorted(confirmed_prices(data)) == [100, 200]
    assert compute_viability_stats(pd.DataFrame([data])).comparable_market_total == 150


def test_base_unit_price_is_not_rounded_before_extending_quantity():
    from autobot.real_market_scraper import MarketOffer, _build_output_row
    from autobot.market_strategy import build_search_plan
    data = position(name='Кабель контрольный', unit='м', price=0.006, qty=1000)
    quote = MarketOffer(source='Поставщик', title=data[COL_NAME], price=0.005, url='https://supplier.example/item',
                        verification='verified', matched_unit='м', observed_at=datetime.now(timezone.utc).isoformat())
    output = _build_output_row(pd.Series(data), offers=[quote], query='кабель', err='',
                              plan=build_search_plan(data[COL_NAME], data[COL_UNIT]))
    assert confirmed_prices(output) == [0.005]
    assert compute_viability_stats(pd.DataFrame([output])).comparable_market_total == 5
