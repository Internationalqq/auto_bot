import json
from pathlib import Path

import pandas as pd

import autobot.tender_detail as tender_detail
from autobot.market_analytics import COL_NAME, COL_QTY, COL_SUM, COL_UNIT, COL_UNIT_PRICE


def test_detail_separates_processed_rows_from_verified_prices(tmp_path, monkeypatch):
    tender_id = "0171200001926000664"
    monkeypatch.setattr(tender_detail, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(tender_detail, "latest_parser_health", lambda _tender_id: {})

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


def test_position_table_omits_search_strategy_and_sources_stay_in_their_column():
    package_dir = Path(tender_detail.__file__).parent
    template = (package_dir / "templates" / "tender_detail.html").read_text(encoding="utf-8")
    styles = (package_dir / "static" / "tender_detail.css").read_text(encoding="utf-8")

    assert 'class="strategy"' not in template
    assert "Как ищем цену" not in template
    assert 'class="source-details"' in template
    assert ".source-list { width: 100%; min-width: 0;" in styles
    assert "width: 360px" not in styles


def test_tender_header_uses_a_back_arrow_to_return_to_the_board():
    package_dir = Path(tender_detail.__file__).parent
    template = (package_dir / "templates" / "tender_detail.html").read_text(encoding="utf-8")
    styles = (package_dir / "static" / "tender_detail.css").read_text(encoding="utf-8")

    assert 'href="/tenders" aria-label="Назад к списку тендеров"' in template
    assert 'class="brand-back"' in template
    assert 'class="brand-mark"' not in template
    assert ".brand-back i" in styles


def test_position_table_fits_without_horizontal_scrolling():
    package_dir = Path(tender_detail.__file__).parent
    template = (package_dir / "templates" / "tender_detail.html").read_text(encoding="utf-8")
    styles = (package_dir / "static" / "tender_detail.css").read_text(encoding="utf-8")

    assert ".table-wrap { width: 100%; max-width: 100%; overflow-x: hidden; }" in styles
    assert "table { width: 100%; min-width: 0;" in styles
    assert "min-width: 1280px" not in styles
    assert '@media (max-width: 760px)' in styles
    assert 'data-label="Источники"' in template
    assert 'data-label="Результат"' not in template


def test_market_price_replaces_the_redundant_result_column():
    package_dir = Path(tender_detail.__file__).parent
    template = (package_dir / "templates" / "tender_detail.html").read_text(encoding="utf-8")
    styles = (package_dir / "static" / "tender_detail.css").read_text(encoding="utf-8")

    assert "<th>Результат</th>" not in template
    assert 'class="verdict ' not in template
    assert 'market-price-{{ p.verdict_class }}' in template
    assert 'title="{{ p.verdict }}"' in template
    assert 'colspan="7"' in template
    assert 'colspan="8"' not in template
    assert ".market-price-good > b" in styles
    assert ".market-price-bad > b" in styles


def test_selection_number_and_position_columns_are_compact():
    package_dir = Path(tender_detail.__file__).parent
    template = (package_dir / "templates" / "tender_detail.html").read_text(encoding="utf-8")
    styles = (package_dir / "static" / "tender_detail.css").read_text(encoding="utf-8")

    assert "th:nth-child(1) { width: 28px; }" in styles
    assert "th:nth-child(2) { width: 34px; }" in styles
    assert ".select-col { padding-left: 2px; padding-right: 0;" in styles
    assert ".row-no { padding-left: 2px; padding-right: 3px;" in styles
    assert 'title="Выбрать позицию для поиска цены агентом"' in template


def test_feature_modals_expand_crm_workspace_and_avito_uses_brand_mark():
    package_dir = Path(tender_detail.__file__).parent
    template = (package_dir / "templates" / "tender_detail.html").read_text(encoding="utf-8")
    styles = (package_dir / "static" / "tender_detail.css").read_text(encoding="utf-8")

    assert 'class="command-tool-icon is-avito"' in template
    assert 'class="command-tool-icon is-hermes"' in template
    assert 'data-feature-open="rulesModal"' not in template
    assert 'id="rulesModal"' not in template
    assert "Карточка откроет окно с подробностями" in template
    assert "Смета против НМЦК" in template
    assert "Статус обработки" in template
    assert "Повторно найти цены" in template
    assert "Найти цены на Авито" in template
    assert 'type: "autobot:feature-modal", open: true' in template
    assert 'type: "autobot:feature-modal", open: false' in template
    assert ".command-tool-icon.is-avito i:nth-child(4)" in styles
    assert ".command-tool-icon.is-hermes" in styles
    assert "max-height: calc(100dvh - 20px)" in styles
    assert 'JSON.stringify({ mode: "web"' in template
    assert 'JSON.stringify({ mode: "avito"' in template
    assert "Дожать 20 без цены" in template


def test_avito_modal_shows_one_minimal_latest_run_summary():
    package_dir = Path(tender_detail.__file__).parent
    template = (package_dir / "templates" / "tender_detail.html").read_text(encoding="utf-8")
    styles = (package_dir / "static" / "tender_detail.css").read_text(encoding="utf-8")

    assert 'id="avitoRunTitle"' in template
    assert 'id="avitoRunProgressFill"' in template
    assert 'id="avitoRunPositionList"' in template
    assert "const run = data.latest_run || {};" in template
    assert "formatAgentDuration(run.elapsed_seconds)" in template
    assert 'class="agent-run-journal"' in template
    assert ".agent-run-position-list" in styles
    assert ".agent-run-offers" in styles
