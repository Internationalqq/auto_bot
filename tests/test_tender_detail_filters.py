import json
from pathlib import Path

import pandas as pd

import autobot.tender_detail as tender_detail
from autobot.market_analytics import COL_NAME, COL_QTY, COL_SUM, COL_UNIT, COL_UNIT_PRICE


def test_detail_separates_processed_rows_from_verified_prices(tmp_path, monkeypatch):
    tender_id = "0171200001926000664"
    monkeypatch.setattr(tender_detail, "REPORTS_DIR", tmp_path)

    estimate = pd.DataFrame(
        [
            {COL_NAME: "Работа с ценой", COL_UNIT: "м3", COL_QTY: 2, COL_UNIT_PRICE: 100, COL_SUM: 200},
            {COL_NAME: "Работа без цены", COL_UNIT: "м2", COL_QTY: 3, COL_UNIT_PRICE: 50, COL_SUM: 150},
        ]
    )
    estimate.to_excel(tmp_path / f"ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.xlsx", index=False)

    verified_offer = json.dumps(
        [
            {
                "source": "Поставщик",
                "title": "Работа с ценой",
                "price": 90,
                "url": "https://supplier.example/price",
                "verification": "verified",
                "verification_reason": "Цена подтверждена",
            }
        ],
        ensure_ascii=False,
    )
    market = pd.DataFrame(
        [
            {
                COL_NAME: "Работа с ценой",
                "Цена-сайт-телефон (json)": verified_offer,
                "Проверенных источников": 1,
                "Медиана цена за ед. (рынок)": 90,
            },
            {
                COL_NAME: "Работа без цены",
                "Проверенных источников": 0,
                "Ошибка / статус": "обработано, подтверждённых цен не найдено",
            },
        ]
    )
    market.to_excel(
        tmp_path / f"РЫНОК_ИСТОЧНИКИ_ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.xlsx",
        index=False,
    )

    detail = tender_detail.build_tender_detail(tender_id, {}, {})

    assert detail["counts"]["processed"] == 2
    assert detail["counts"]["verified"] == 1
    assert [position["market_processed"] for position in detail["positions"]] == [True, True]
    assert [position["verified_count"] for position in detail["positions"]] == [1, 0]


def test_verified_price_metric_is_an_actual_filter_button():
    template_path = Path(tender_detail.__file__).parent / "templates" / "tender_detail.html"
    template = template_path.read_text(encoding="utf-8")

    assert 'data-bucket-filter="processed"' in template
    assert 'data-bucket-filter="verified"' in template
    assert 'data-market-verified="{{ \'1\' if p.verified_count else \'0\' }}"' in template
    assert 'activeBucket === "verified" && row.dataset.marketVerified === "1"' in template


def test_feature_modals_expand_crm_workspace_and_avito_uses_brand_mark():
    package_dir = Path(tender_detail.__file__).parent
    template = (package_dir / "templates" / "tender_detail.html").read_text(encoding="utf-8")
    styles = (package_dir / "static" / "tender_detail.css").read_text(encoding="utf-8")

    assert 'class="command-tool-icon is-avito"' in template
    assert 'type: "autobot:feature-modal", open: true' in template
    assert 'type: "autobot:feature-modal", open: false' in template
    assert ".command-tool-icon.is-avito i:nth-child(4)" in styles
    assert "max-height: calc(100dvh - 20px)" in styles
