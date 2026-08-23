import json
from pathlib import Path

import pandas as pd

from autobot.main import (
    Tender,
    _archive_output_dir,
    _build_tender_clean_df,
    _candidate_notice_urls,
    _extract_reg_number_from_url,
    _sanitize_filename_for_windows,
    extract_lsr_rows,
    extract_rows_from_pdf,
    get_tender_id,
    parse_stage_from_card_text,
    parse_tender_card_metadata,
    select_estimate_pdf_files,
    write_estimate_parse_manifest,
)


def test_archive_output_dir_keeps_nested_archive_in_tender_tree(tmp_path):
    extracted = tmp_path / "extracted"
    nested_archive = extracted / "0171200001926000664" / "doc_6" / "estimate.rar"
    downloaded_archive = tmp_path / "downloads" / "0171200001926000664" / "documents.zip"

    assert _archive_output_dir(nested_archive, extracted) == (
        extracted / "0171200001926000664" / "doc_6" / "estimate"
    )
    assert _archive_output_dir(downloaded_archive, extracted) == (
        extracted / "0171200001926000664" / "documents"
    )


def test_select_estimate_pdf_files_prefers_trade_selection(tmp_path):
    selection = tmp_path / "\u0432\u044b\u0431\u043e\u0440\u043a\u0430 \u0434\u043b\u044f \u0442\u043e\u0440\u0433\u043e\u0432" / "1 - \u041b\u0421\u0420.pdf"
    selection_six = selection.parent / "6 - \u041b\u0421\u0420.pdf"
    selection_eleven = selection.parent / "11 - \u041b\u0421\u0420.pdf"
    full = tmp_path / "\u043f\u043e\u043b\u043d\u0430\u044f \u041f\u0421\u0414" / "2 - \u041b\u0421\u0420.pdf"
    duplicate = tmp_path / "full-copy" / "2 - \u041b\u0421\u0420.pdf"
    unrelated = tmp_path / "contract.pdf"
    for path, payload in (
        (selection, b"selection"),
        (selection_six, b"selection-six"),
        (selection_eleven, b"selection-eleven"),
        (full, b"full"),
        (duplicate, b"full"),
        (unrelated, b"contract"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    selected = select_estimate_pdf_files([selection_eleven, unrelated, full, selection_six, duplicate, selection])

    assert selected == [selection, selection_six, selection_eleven]


def test_select_estimate_pdf_files_falls_back_to_all_unique_lsr(tmp_path):
    first = tmp_path / "full" / "1 - \u041b\u0421\u0420.pdf"
    second = tmp_path / "full" / "2 - \u041b\u0421\u0420.pdf"
    duplicate = tmp_path / "copy" / "2 - \u041b\u0421\u0420.pdf"
    for path, payload in ((first, b"first"), (second, b"second"), (duplicate, b"second")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    assert select_estimate_pdf_files([second, duplicate, first]) == [first, second]


def test_write_estimate_parse_manifest_reports_empty_lsr(tmp_path):
    first = tmp_path / "1 - \u041b\u0421\u0420.pdf"
    empty = tmp_path / "2 - \u041b\u0421\u0420.pdf"
    manifest = write_estimate_parse_manifest(
        "123",
        [first, empty],
        [{"source_file": str(first)}],
        {"reports": tmp_path / "reports"},
        {first.name: 1234.56},
    )

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["selected_pdf_count"] == 2
    assert payload["parsed_pdf_count"] == 1
    assert payload["empty_pdf_files"] == [empty.name]
    assert payload["official_total_files_count"] == 1
    assert payload["official_total_missing_files"] == [empty.name]
    assert payload["official_total_rub"] == 1234.56


def test_build_tender_clean_df_sorts_files_and_pdf_pages_naturally():
    rows = [
        {"source_file": "11 - video - LSR.pdf", "sheet_name": "PDF, стр. 2", "work_name": "section eleven", "price_from_estimate_rub": 11},
        {"source_file": "6 - light - LSR.pdf", "sheet_name": "PDF, стр. 10", "work_name": "page ten item", "price_from_estimate_rub": 610},
        {"source_file": "6 - light - LSR.pdf", "sheet_name": "PDF, стр. 2", "work_name": "page two item", "price_from_estimate_rub": 602},
    ]

    clean = _build_tender_clean_df(rows)

    assert clean["Название работы/услуги"].tolist() == ["page two item", "page ten item", "section eleven"]


def test_build_tender_clean_df_keeps_same_position_from_different_estimate_files():
    rows = [
        {
            "source_file": "1 - heating - LSR.xlsx",
            "sheet_name": "Лист 1",
            "item_no": "1",
            "work_name": "Монтаж трубопровода отопления",
            "qty": 10,
            "price_from_estimate_rub": 125000.0,
        },
        {
            "source_file": "2 - water - LSR.xlsx",
            "sheet_name": "Лист 1",
            "item_no": "1",
            "work_name": "Монтаж трубопровода отопления",
            "qty": 10,
            "price_from_estimate_rub": 125000.0,
        },
    ]

    clean = _build_tender_clean_df(rows)

    assert len(clean) == 2
    assert clean["Файл ЛСР"].tolist() == ["1 - heating - LSR.xlsx", "2 - water - LSR.xlsx"]


def test_extract_reg_number_from_url():
    url = "https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=0869200000226003734"
    assert _extract_reg_number_from_url(url) == "0869200000226003734"
    assert _extract_reg_number_from_url("https://zakupki.gov.ru/no-query") == ""


def test_extract_223_purchase_number_from_text_and_documents_url():
    assert get_tender_id("223-ФЗ Иной способ № 32616311877") == "32616311877"
    url = (
        "https://zakupki.gov.ru/epz/order/notice/notice223/documents.html"
        "?purchaseNoticeNumber=32616311877&noticeGuid=test"
    )
    assert _extract_reg_number_from_url(url) == "32616311877"


def test_parse_eis_223_card_metadata():
    text = """223-ФЗ Иной способ
№ 32616311877
Работа комиссии
Объект закупки
оказание услуг по охране комплекса административных зданий
Заказчик
ОБЛАСТНОЕ ГОСУДАРСТВЕННОЕ АВТОНОМНОЕ УЧРЕЖДЕНИЕ
Начальная цена
1 346 880,00 ₽
Размещено
21.08.2026
Обновлено
21.08.2026
Документы"""

    meta = parse_tender_card_metadata(
        text,
        tender_id="32616311877",
        url="https://zakupki.gov.ru/223/purchase/public/purchase/info/common-info.html?regNumber=32616311877",
    )

    assert meta == {
        "object_name": "оказание услуг по охране комплекса административных зданий",
        "customer_name": "ОБЛАСТНОЕ ГОСУДАРСТВЕННОЕ АВТОНОМНОЕ УЧРЕЖДЕНИЕ",
        "publish_date": "21.08.2026",
        "updated_date": "21.08.2026",
        "law": "223-ФЗ",
        "purchase_method": "Иной способ",
    }
    assert parse_stage_from_card_text(text) == "Работа комиссии"


def test_parse_eis_44_placing_organization_as_customer():
    text = """44-ФЗ Запрос котировок в электронной форме
№ 0112200000826003685
Определение поставщика завершено
Объект закупки
Продукты питания для нужд школы
Организация, осуществляющая размещение
МИНИСТЕРСТВО ПО РЕГУЛИРОВАНИЮ КОНТРАКТНОЙ СИСТЕМЫ
Начальная цена
84 150,00 ₽
Размещено
13.08.2026
Обновлено
21.08.2026"""
    meta = parse_tender_card_metadata(text, tender_id="0112200000826003685")
    assert meta["object_name"] == "Продукты питания для нужд школы"
    assert meta["customer_name"] == "МИНИСТЕРСТВО ПО РЕГУЛИРОВАНИЮ КОНТРАКТНОЙ СИСТЕМЫ"


def test_candidate_notice_urls_has_multiple_routes():
    tender = Tender(
        tender_id="0869200000226003734",
        title="x",
        url="https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=0869200000226003734",
        region="r",
        stage="Подача заявок",
        price_rub=None,
        publish_date=None,
    )
    urls = _candidate_notice_urls(tender)
    assert any("/notice/ea20/view/common-info.html" in u for u in urls)
    assert any("/notice/zk20/view/documents.html" in u for u in urls)
    assert all("regNumber=0869200000226003734" in u for u in urls if "regNumber=" in u)


def test_candidate_notice_urls_supports_223_documents():
    tender = Tender(
        tender_id="32616311877",
        title="Охрана зданий",
        url="https://zakupki.gov.ru/223/purchase/public/purchase/info/common-info.html?regNumber=32616311877",
        region="Сахалинская область",
        stage="Работа комиссии",
        price_rub=1_346_880,
        publish_date="21.08.2026",
    )
    urls = _candidate_notice_urls(tender)
    assert any("purchaseNoticeNumber=32616311877" in u for u in urls)


def test_sanitize_filename_for_windows_strips_invalid_chars():
    raw = '  Смета: 01/02*?.xlsx  '
    cleaned = _sanitize_filename_for_windows(raw)
    assert "/" not in cleaned
    assert "*" not in cleaned
    assert "?" not in cleaned
    assert cleaned.endswith(".xlsx")


def test_sanitize_filename_for_windows_reserved_name():
    assert _sanitize_filename_for_windows("con.txt").startswith("_")


def test_extract_rows_from_pdf_fallback(monkeypatch):
    tender = Tender(
        tender_id="1",
        title="t",
        url="https://example.com",
        region="r",
        stage="Подача заявок",
        price_rub=None,
        publish_date=None,
    )
    lines = [
        "Устройство основания из щебня 125 000 руб",
        "Итого по разделу 999 999 руб",
        "Монтаж бордюров 45 500",
    ]
    monkeypatch.setattr("autobot.main._iter_pdf_lines", lambda _path: lines)
    rows = extract_rows_from_pdf(Path("dummy.pdf"), tender)
    assert len(rows) == 2
    names = [r["work_name"] for r in rows]
    assert any("основания" in n for n in names)
    assert any("бордюров" in n for n in names)
    assert all(r["extract_source"] == "PDF fallback" for r in rows)


def test_build_tender_clean_df_accepts_fallback_rows_without_lsr_columns():
    rows = [
        {
            "source_file": "estimate.xlsx",
            "sheet_name": "Sheet1",
            "excel_row": 12,
            "section": "",
            "extract_source": "Excel fallback",
            "work_name": "Montazh ograzhdeniya",
            "price_from_estimate_rub": 125000.0,
        }
    ]

    clean = _build_tender_clean_df(rows)

    assert len(clean) == 1
    assert clean.iloc[0]["Название работы/услуги"] == "Montazh ograzhdeniya"
    assert clean.iloc[0]["Сумма, руб"] == 125000.0
    assert clean.iloc[0]["Ед. изм."] == ""


def test_extract_lsr_rows_uses_explicit_current_unit_price_column():
    rows = [[None] * 16 for _ in range(5)]
    rows[0][0] = "Раздел 1. Окна"
    rows[1][0] = 1
    rows[1][1] = "ГЭСН15-01-050-04"
    rows[1][2] = "Демонтаж облицовки оконных откосов"
    rows[1][7] = "100 м2"
    rows[1][8] = 0.0316
    rows[2][2] = "Всего по позиции"
    rows[2][13] = 136210.13
    rows[2][15] = 4304.24
    rows[3][0] = 2
    rows[3][1] = "ФСБЦ-11.3.02.04-0027"
    rows[3][2] = "Блок оконный из ПВХ-профилей"
    rows[3][7] = "м2"
    rows[3][8] = 17.1
    rows[3][13] = 5062.25
    rows[3][15] = 86564.48
    rows[4][2] = "Всего по позиции"
    rows[4][15] = 86564.48
    tender = Tender(
        tender_id="uploaded",
        title="Смета",
        url="",
        region="",
        stage="",
        price_rub=None,
        publish_date="",
    )

    result = extract_lsr_rows(pd.DataFrame(rows), tender, Path("estimate.xlsx"))

    assert len(result) == 2
    assert result[0]["qty"] == 0.0316
    assert result[0]["unit_price_rub"] == 136210.13
    assert result[0]["price_from_estimate_rub"] == 4304.24
    assert result[1]["qty"] == 17.1
    assert result[1]["unit_price_rub"] == 5062.25
    assert result[1]["price_from_estimate_rub"] == 86564.48
