from pathlib import Path

from autobot.main import (
    Tender,
    _candidate_notice_urls,
    _extract_reg_number_from_url,
    _sanitize_filename_for_windows,
    extract_rows_from_pdf,
)


def test_extract_reg_number_from_url():
    url = "https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=0869200000226003734"
    assert _extract_reg_number_from_url(url) == "0869200000226003734"
    assert _extract_reg_number_from_url("https://zakupki.gov.ru/no-query") == ""


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
