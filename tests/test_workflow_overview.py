import json

from autobot.workflow_overview import build_tender_workflow_overview, build_workflow_payload


def test_workflow_overview_picks_next_action(tmp_path):
    data_dir = tmp_path
    reports = data_dir / "reports"
    reports_site = data_dir / "reports_site"
    reports.mkdir()
    reports_site.mkdir()
    (data_dir / "downloads" / "1").mkdir(parents=True)
    (data_dir / "downloads" / "1" / "doc.zip").write_bytes(b"x")
    (reports / "ОТЧЕТ_ПО_СМЕТАМ_1.xlsx").write_bytes(b"x")
    (reports / "ОТЧЕТ_ПО_СМЕТАМ_1.html").write_bytes(b"x")
    (reports / "РЫНОК_ИСТОЧНИКИ_ОТЧЕТ_ПО_СМЕТАМ_1.xlsx").write_bytes(b"x")
    (reports / "СВОДКА_РЫНОК_1.xlsx").write_bytes(b"x")
    (reports_site / "1").mkdir()
    (reports_site / "1" / "index.html").write_text("ok", encoding="utf-8")
    (data_dir / "tenders.json").write_text(
        json.dumps(
            [
                {"tender_id": "1", "title": "Ready", "region": "R"},
                {"tender_id": "2", "title": "Missing docs", "region": "R"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    items = build_tender_workflow_overview(
        data_dir=data_dir,
        reports_dir=reports,
        reports_site_dir=reports_site,
    )

    by_id = {item.tender_id: item for item in items}
    assert by_id["1"].next_action == "review"
    assert by_id["1"].is_ready is True
    assert by_id["2"].next_action == "download_documents"
    assert by_id["2"].next_action_label == "Скачать документы"


def test_workflow_payload_includes_counts_and_storage(tmp_path):
    (tmp_path / "tenders.json").write_text(
        json.dumps([{"tender_id": "1"}, {"tender_id": "2"}]),
        encoding="utf-8",
    )
    (tmp_path / "downloads" / "1").mkdir(parents=True)
    (tmp_path / "downloads" / "1" / "doc.zip").write_bytes(b"123")

    payload = build_workflow_payload(data_dir=tmp_path)

    assert payload["counts"]["extract_estimate"] == 1
    assert payload["counts"]["download_documents"] == 1
    assert any(item["name"] == "downloads" and item["files"] == 1 for item in payload["storage"])

