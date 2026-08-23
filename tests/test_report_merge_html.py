from __future__ import annotations

import pandas as pd

from autobot.market_analytics import COL_NAME, COL_QTY, COL_UNIT
from autobot.report_merge_html import (
    _display_name_from_row,
    _display_unit_from_row,
    _quantity_multiplier_from_row,
    _row_compare_market_value,
    _row_market_total,
)


def test_trailing_thousand_is_restored_as_estimate_unit_block() -> None:
    row = pd.Series(
        {
            COL_NAME: "Разработка грунта с погрузкой в траншеях 1000",
            COL_UNIT: "м3",
            COL_QTY: 2.08074,
        }
    )

    assert _quantity_multiplier_from_row(row) == (1000.0, "м³")
    assert _display_name_from_row(row) == "Разработка грунта с погрузкой в траншеях"
    assert _display_unit_from_row(row) == "1000 м³"
    assert _row_compare_market_value(row, 180.0) == 180_000.0
    assert _row_market_total(row, 180.0) == 374_533.2


def test_distance_up_to_ten_is_not_treated_as_unit_block() -> None:
    row = pd.Series(
        {
            COL_NAME: "Разработка грунта с перемещением до 10",
            COL_UNIT: "м",
            COL_QTY: 0.04808,
        }
    )

    assert _quantity_multiplier_from_row(row) == (1.0, "")
    assert _display_unit_from_row(row) == "м"


def test_ten_pieces_at_end_is_restored() -> None:
    row = pd.Series(
        {
            COL_NAME: "Заземлитель вертикальный диаметром 16 мм 10",
            COL_UNIT: "шт",
            COL_QTY: 0.6,
        }
    )

    assert _quantity_multiplier_from_row(row) == (10.0, "шт")
    assert _display_unit_from_row(row) == "10 шт"
