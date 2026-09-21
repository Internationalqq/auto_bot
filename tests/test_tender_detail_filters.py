import json
import time
from pathlib import Path

import pandas as pd

import autobot.tender_detail as tender_detail
from autobot.market_analytics import COL_NAME, COL_QTY, COL_SUM, COL_UNIT, COL_UNIT_PRICE


def test_archive_failure_prevents_complete_estimate_status(tmp_path, monkeypatch):
    tid = '0171200001926000664'
    monkeypatch.setattr(tender_detail, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(tender_detail, 'latest_parser_health', lambda _: {})
    pd.DataFrame([{COL_NAME: 'Поставка материала', COL_UNIT: 'м3', COL_QTY: 1,
                   COL_UNIT_PRICE: 100, COL_SUM: 100}]).to_excel(tmp_path / f'ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx', index=False)
    (tmp_path / f'ARCHIVES_{tid}.json').write_text(json.dumps({'failed_count': 1, 'archives': [
        {'archive': 'docs.zip', 'status': 'failed', 'message': 'Архив ZIP повреждён'}]}), encoding='utf-8')
    detail = tender_detail.build_tender_detail(tid, {'price_rub': 100}, {})
    assert detail['estimate_check_class'] == 'warn'
    assert 'docs.zip' in detail['estimate_check_detail']
    assert 'не изменён' in detail['estimate_check_detail']
    assert detail['archive_extraction']['failed_count'] == 1
    assert not next(step for step in detail['steps'] if step['key'] == 'estimate')['done']


def test_unconfirmed_tax_is_never_guessed_to_match_initial_price(tmp_path, monkeypatch):
    tid = '0171200001926000664'
    monkeypatch.setattr(tender_detail, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(tender_detail, 'latest_parser_health', lambda _: {})
    (tmp_path / f'ESTIMATE_PARSE_{tid}.json').write_text(json.dumps({
        'selected_pdf_count': 1, 'parsed_pdf_count': 1,
        'official_total_rub': 10000, 'official_total_files_count': 1}), encoding='utf-8')
    for price in (10000, 12200):
        detail = tender_detail.build_tender_detail(tid, {'price_rub': price}, {})
        assert detail['estimate_check_class'] != 'good'
        assert detail['estimate_total_with_vat'] is None
        assert detail['estimate_comparison_basis'] == 'official'
        assert detail['estimate_gap'] == price - 10000
        assert 'не доказывает полноту' in detail['estimate_check_detail']


def test_zero_parsed_pdfs_is_not_replaced_by_old_report_file_count(tmp_path, monkeypatch):
    tid = '0171200001926000664'
    monkeypatch.setattr(tender_detail, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(tender_detail, 'latest_parser_health', lambda _: {})
    pd.DataFrame([{'Файл ЛСР': 'old.xlsx', COL_NAME: 'Поставка материала', COL_UNIT: 'м3', COL_QTY: 1,
                   COL_UNIT_PRICE: 100, COL_SUM: 100}]).to_excel(tmp_path / f'ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx', index=False)
    (tmp_path / f'ESTIMATE_PARSE_{tid}.json').write_text(json.dumps({
        'selected_pdf_count': 1, 'parsed_pdf_count': 0, 'empty_pdf_files': ['new.pdf']}), encoding='utf-8')
    detail = tender_detail.build_tender_detail(tid, {}, {})
    assert detail['estimate_files_parsed'] == 0
    assert detail['estimate_check_class'] == 'warn'
    assert detail['estimate_check_title'] == 'Не все ЛСР распознаны'


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
                "matched_unit": "м3",
                "observed_at": time.time(),
            }
        ],
        ensure_ascii=False,
    )
    market = pd.DataFrame(
        [
            {
                COL_NAME: "Работа с ценой",
                COL_UNIT: "м3",
                "Цена-сайт-телефон (json)": verified_offer,
                "Проверенных источников": 1,
                "Медиана цена за ед. (рынок)": 90,
            },
            {
                COL_NAME: "Работа без цены",
                COL_UNIT: "м2",
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


def test_found_price_filter_requires_a_positive_offer_on_a_priceable_row(tmp_path, monkeypatch):
    from bs4 import BeautifulSoup
    from autobot import web_ui
    from autobot.market_contract import BUNDLE_COLUMN

    tid = '991234567890'
    monkeypatch.setattr(tender_detail, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(tender_detail, 'latest_parser_health', lambda _: {})
    rows = []
    market = []
    for index, (price, quantity) in enumerate([(90, 1), (None, 1), (0, 1), (90, -1)]):
        name = f'Кабель ВВГнг 3х{index + 1}'
        rows.append({COL_NAME: name, COL_UNIT: 'м', COL_QTY: quantity,
                     COL_UNIT_PRICE: 100, COL_SUM: 100 * quantity})
        market.append({COL_NAME: name, COL_UNIT: 'м', BUNDLE_COLUMN: json.dumps([{
            'source': 'Поставщик', 'title': name, 'url': 'https://supplier.example/cable',
            'price': price, 'verification': 'candidate', 'matched_unit': 'м',
            'verification_reason': 'Нужно проверить условия', 'observed_at': time.time(),
        }], ensure_ascii=False)})
    pd.DataFrame(rows).to_excel(tmp_path / f'ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx', index=False)
    pd.DataFrame(market).to_excel(tmp_path / f'РЫНОК_ИСТОЧНИКИ_ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx', index=False)
    detail = tender_detail.build_tender_detail(tid, {}, {})
    assert [p['has_found_price'] for p in detail['positions']] == [True, False, False, False]
    assert detail['counts']['found'] == 1
    assert detail['price_coverage']['verified'] == 0
    detail.update(active_tab='overview', documents={'count': 0, 'files': []})
    with web_ui.app.test_request_context():
        page = BeautifulSoup(web_ui.render_template('tender_detail.html', tender=detail), 'html.parser')
    assert page.select_one('[data-price-filter="found"] b').text == '1'
    assert page.select_one('[data-price-filter="verified"] b').text == '0'
    assert [r['data-market-found'] for r in page.select('[data-position-row]')] == ['1', '0', '0', '0']
    assert page.select_one('[data-bucket-filter="processed"]')


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


def test_price_columns_have_accessible_labels_and_preserve_position_identity(tmp_path, monkeypatch):
    from bs4 import BeautifulSoup
    from autobot import web_ui
    tid = '0171200001926000664'
    monkeypatch.setattr(tender_detail, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(tender_detail, 'latest_parser_health', lambda _: {})
    pd.DataFrame([{COL_NAME: 'Поставка материала', COL_UNIT: 'м3', COL_QTY: 2,
                   COL_UNIT_PRICE: 100, COL_SUM: 200}]).to_excel(tmp_path / f'ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx', index=False)
    detail = tender_detail.build_tender_detail(tid, {'price_rub': 200}, {})
    detail['active_tab'] = 'overview'
    detail['documents'] = {'count': 0, 'files': []}
    with web_ui.app.test_request_context():
        html = web_ui.render_template('tender_detail.html', tender=detail)
    page = BeautifulSoup(html, 'html.parser')
    table = page.select_one('#positions table')
    assert len(table.select('thead th')) == 8
    row = table.select_one('[data-position-row]')
    assert len(row.select(':scope > td')) == 8
    assert row.select_one('input[data-agent-position]')['value'] == detail['positions'][0]['position_key']
    assert '100 ₽' in row.select_one('.col-estimate').get_text()
    assert 'Нет сопоставимой цены' in row.select_one('.col-market').get_text()
    assert 'Ждём цену рынка' in row.select_one('.col-difference').get_text()
    assert row.select_one('a')['href'].startswith('/tenders/' + tid + '/review?position_id=')
    assert page.select_one('[data-open-workspace="search"]')
    assert page.select_one('#agentMarketCard').find_parent(attrs={'data-workspace-panel':'search'})
    assert page.select_one('[data-tender-economics]').find_parent(attrs={'data-workspace-panel':'economics'})


def test_evidence_dialogs_and_search_modes_preserve_existing_contract():
    template = (Path(tender_detail.__file__).parent / 'templates/tender_detail.html').read_text(encoding='utf-8')
    assert 'data-feature-open="estimateModal"' in template
    assert 'data-feature-open="readinessModal"' in template
    assert 'data-feature-open="avitoModal"' in template
    assert 'type: "autobot:feature-modal", open: true' in template
    assert 'type: "autobot:feature-modal", open: false' in template
    assert 'JSON.stringify({ mode: "web"' in template
    assert 'JSON.stringify({ mode: "avito"' in template
    assert 'id="queueAgentMarketBtn"' in template and 'id="stopAgentMarketBtn"' in template


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
