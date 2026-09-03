from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from autobot import estimate_excel_analysis, main, web_ui
from autobot.estimate_excel_analysis import EstimateRow
from autobot.market_analytics import COL_ITEM, COL_NAME, COL_QTY, COL_SUM, COL_UNIT, COL_UNIT_PRICE


def _actionable_row() -> EstimateRow:
    return EstimateRow(
        idx=1,
        name="Поставка бетонной смеси",
        unit="м3",
        qty=2,
        unit_price=500,
        total=1000,
        item_no="1",
        basis_code="ФСБЦ-01",
        sheet="Лист 1",
        excel_row=12,
        source="project-parser",
    )


def _reconciliation() -> dict:
    return {
        "schema_version": 1,
        "actionable_row_count": 1,
        "positive_position_total": 1000.0,
        "excluded_adjustment_count": 1,
        "excluded_adjustment_total": -100.0,
        "signed_position_total": 900.0,
        "declared_total": 1150.0,
        "unallocated_total": 250.0,
    }


def test_project_parser_reconciles_declared_total_and_negative_corrections(tmp_path, monkeypatch) -> None:
    source = tmp_path / "estimate.xlsx"
    pd.DataFrame([["Смета на сумму:", "1 150,00 руб."]]).to_excel(source, header=False, index=False)
    raw_rows = [
        {
            "work_name": "Поставка бетонной смеси",
            "price_from_estimate_rub": 1000.0,
        },
        {
            "work_name": "Корректировка бетонной смеси",
            "price_from_estimate_rub": -100.0,
        },
    ]
    clean = pd.DataFrame(
        [
            {
                COL_NAME: "Поставка бетонной смеси",
                COL_UNIT: "м3",
                COL_QTY: 2.0,
                COL_UNIT_PRICE: 500.0,
                COL_SUM: 1000.0,
                COL_ITEM: "1",
                "basis_code": "ФСБЦ-01",
                "Лист": "Лист 1",
                "Строка Excel": 12,
                "Раздел": "Материалы",
            }
        ]
    )
    monkeypatch.setattr(main, "extract_rows_from_excel", lambda _path, _tender: raw_rows)
    monkeypatch.setattr(main, "_build_tender_clean_df", lambda _rows: clean)

    rows, diagnostics = estimate_excel_analysis._read_via_project_parser_result(source)

    assert len(rows) == 1
    assert diagnostics == _reconciliation()


def test_upload_worker_persists_reconciliation_in_meta_and_status(tmp_path, monkeypatch) -> None:
    estimate_id = "b" * 16
    job_id = "a" * 16
    root = tmp_path
    estimates_dir = root / "data" / "user_estimates"
    source_dir = estimates_dir / estimate_id
    source_dir.mkdir(parents=True)
    source = source_dir / "source.xlsx"
    source.touch()

    monkeypatch.setattr(web_ui, "REPO_ROOT", root)
    monkeypatch.setattr(web_ui, "USER_ESTIMATES_DIR", estimates_dir)
    monkeypatch.setattr(web_ui, "USER_ESTIMATES_INDEX", estimates_dir / "index.json")
    monkeypatch.setattr(web_ui, "ESTIMATE_UPLOAD_JOBS_DIR", estimates_dir / ".upload_jobs")
    monkeypatch.setattr(
        estimate_excel_analysis,
        "load_estimate_session",
        lambda _path, progress_cb=None: SimpleNamespace(rows=[_actionable_row()], diagnostics=_reconciliation()),
    )

    try:
        with web_ui.estimate_upload_lock:
            web_ui.estimate_upload_jobs[job_id] = {
                "job_id": job_id,
                "running": True,
                "progress": 20,
                "log_lines": [],
            }

        web_ui._run_estimate_upload_worker(
            job_id,
            estimate_id=estimate_id,
            title_raw="Тестовая смета",
            original_name="estimate.xlsx",
            src_path=source,
        )

        meta = json.loads((source_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["reconciliation"] == _reconciliation()
        with web_ui.estimate_upload_lock:
            job = dict(web_ui.estimate_upload_jobs[job_id])
        assert "корректировки: 1 (-100,00 ₽)" in job["detail"]
        assert "разница итога: 250,00 ₽" in job["detail"]
    finally:
        with web_ui.estimate_upload_lock:
            web_ui.estimate_upload_jobs.pop(job_id, None)
            web_ui.estimate_upload_workers.discard(job_id)


def test_crm_payload_persists_reconciliation_as_source_metadata(monkeypatch) -> None:
    estimate_id = "abc123"
    monkeypatch.setattr(
        web_ui,
        "_load_estimate_meta",
        lambda _id: {
            "id": estimate_id,
            "title": "Тестовая смета",
            "original_filename": "estimate.xlsx",
            "created_at": "03.09.2026 12:00",
            "reconciliation": _reconciliation(),
        },
    )
    monkeypatch.setattr(web_ui, "_load_estimate_rows", lambda _id: [web_ui._estimate_row_to_dict(_actionable_row())])

    payload = web_ui._build_estimate_crm_import_payload(estimate_id)

    assert payload["source"]["metadata"] == {"reconciliation": _reconciliation()}
    assert web_ui._estimate_crm_prefill(estimate_id)["project"]["budget"] == 1150.0


def test_estimate_detail_shows_concise_reconciliation_notice(monkeypatch, tmp_path) -> None:
    estimate_id = "abc123def456"
    meta = {
        "id": estimate_id,
        "title": "Тестовая смета",
        "original_filename": "estimate.xlsx",
        "created_at": "03.09.2026 12:00",
        "row_count": 1,
        "reconciliation": _reconciliation(),
    }
    rows = [web_ui._estimate_row_to_dict(_actionable_row())]
    monkeypatch.setattr(web_ui, "_load_estimate_meta", lambda _id: meta if _id == estimate_id else None)
    monkeypatch.setattr(web_ui, "_load_estimate_rows", lambda _id: rows if _id == estimate_id else [])
    monkeypatch.setattr(web_ui, "_estimate_market_merged_path", lambda _id: tmp_path / "missing-compare.xlsx")
    monkeypatch.setattr(web_ui, "_estimate_market_raw_path", lambda _id: tmp_path / "missing-sources.xlsx")
    monkeypatch.setattr(web_ui, "_estimate_market_sections", lambda *args, **kwargs: [])
    monkeypatch.setattr(web_ui, "_estimate_market_links", lambda *args, **kwargs: [])

    response = web_ui.app.test_client().get(f"/estimates/{estimate_id}")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Сверка исходной сметы" in html
    assert "Итог файла: <b>1 150,00 ₽</b>" in html
    assert "Позиции с корректировками: <b>900,00 ₽</b>" in html
    assert "Отрицательные корректировки: 1 на -100,00 ₽" in html
    assert "Разница итога не добавлена отдельной закупкой" in html
