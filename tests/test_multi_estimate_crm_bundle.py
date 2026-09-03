from __future__ import annotations

import re

import pytest

from autobot import web_ui


def _sample_meta(estimate_id: str, title: str, filename: str) -> dict:
    return {
        "id": estimate_id,
        "title": title,
        "original_filename": filename,
        "created_at": "03.09.2026 10:00",
    }


def _sample_row(name: str) -> dict:
    return {
        "name": name,
        "qty": 2,
        "unit": "шт",
        "unit_price": 150,
        "total": 300,
        "type": "material",
        "type_label": "Материал",
        "excel_row": 7,
    }


def test_estimate_payload_repeats_stable_source_identity_on_every_row(monkeypatch) -> None:
    monkeypatch.setattr(
        web_ui,
        "_load_estimate_meta",
        lambda estimate_id: _sample_meta(estimate_id, "Фасад", "facade.xlsx"),
    )
    monkeypatch.setattr(web_ui, "_load_estimate_rows", lambda _estimate_id: [_sample_row("Панель")])

    payload = web_ui._build_estimate_crm_import_payload("abc123")

    assert payload["source"]["sourceKey"] == "abc123"
    assert payload["replace_source"] is True
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["estimate_source_type"] == "estimate"
    assert item["estimate_source_key"] == "abc123"
    assert item["estimate_title"] == "Фасад"
    assert item["estimate_file_name"] == "facade.xlsx"
    assert item["source_external_id"] == "abc123"
    assert item["source_reference"] == "/estimates/abc123"


def test_estimate_source_item_keys_are_stable_and_unique_across_sheets(monkeypatch) -> None:
    first = _sample_row("Панель")
    first["sheet"] = "Фасад"
    second = _sample_row("Панель")
    second["sheet"] = "Кровля"
    monkeypatch.setattr(
        web_ui,
        "_load_estimate_meta",
        lambda estimate_id: _sample_meta(estimate_id, "Корпус", "building.xlsx"),
    )
    monkeypatch.setattr(web_ui, "_load_estimate_rows", lambda _estimate_id: [first, second])

    first_payload = web_ui._build_estimate_crm_import_payload("abc123")
    second_payload = web_ui._build_estimate_crm_import_payload("abc123")
    first_keys = [item["source_item_key"] for item in first_payload["items"]]
    second_keys = [item["source_item_key"] for item in second_payload["items"]]

    assert len(set(first_keys)) == 2
    assert first_keys == second_keys
    assert first_keys[0].startswith("Фасад:7:")
    assert first_keys[1].startswith("Кровля:7:")
    moved_key = web_ui._crm_estimate_source_item_key(
        source_scope="abc123",
        sheet="Фасад",
        excel_row=7,
        item_no="",
        row_index=99,
        basis_code="",
        title="Панель с уточнённым названием",
    )
    assert moved_key == first_keys[0]
    bounded_key = web_ui._crm_estimate_source_item_key(
        source_scope="abc123",
        sheet="Лист",
        excel_row="",
        item_no="X" * 1000,
        row_index=1,
        basis_code="Код",
        title="Позиция",
    )
    assert len(bounded_key) < 150


def test_estimate_payload_refuses_to_silently_truncate(monkeypatch) -> None:
    monkeypatch.setenv("PMBI_CRM_MAX_MATERIALS", "1")
    monkeypatch.setattr(
        web_ui,
        "_load_estimate_meta",
        lambda estimate_id: _sample_meta(estimate_id, "Фасад", "facade.xlsx"),
    )
    monkeypatch.setattr(
        web_ui,
        "_load_estimate_rows",
        lambda _estimate_id: [_sample_row("Панель"), _sample_row("Мембрана")],
    )

    with pytest.raises(web_ui.EstimateImportTooLargeError, match="Импорт остановлен без изменений"):
        web_ui._build_estimate_crm_import_payload("abc123")


def test_estimates_page_exposes_secure_multi_select_bundle_flow(monkeypatch) -> None:
    estimates = [
        _sample_meta("aaa111", "Фасад", "facade.xlsx"),
        _sample_meta("bbb222", "Кровля", "roof.xlsx"),
    ]
    rows = {
        "aaa111": [_sample_row("Панель")],
        "bbb222": [_sample_row("Мембрана")],
    }
    monkeypatch.setattr(web_ui, "_read_estimates_index", lambda: estimates)
    monkeypatch.setattr(web_ui, "_load_estimate_rows", lambda estimate_id: rows[estimate_id])
    monkeypatch.setattr(web_ui, "_estimate_market_merged_path", lambda _estimate_id: web_ui.REPO_ROOT / "missing-merged")
    monkeypatch.setattr(web_ui, "_estimate_market_raw_path", lambda _estimate_id: web_ui.REPO_ROOT / "missing-raw")
    monkeypatch.setattr(web_ui, "_estimate_market_progress_for_card", lambda _estimate_id, _rows: (0, 1))
    monkeypatch.setattr(web_ui, "_configured_crm_parent_origin", lambda: "https://crm.example")

    response = web_ui.app.test_client().get("/estimates")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert '<meta name="autobot-parent-origin" content="https://crm.example"' in html
    assert 'src="/static/embed_bridge.js?v=20260903-bundle-1"' in html
    assert html.count("data-estimate-select") >= 2
    assert "Добавить выбранные в объект" in html
    assert "Добавить пакет смет в объект" in html
    assert "estimates: prepared.estimates" in html
    assert "replace_source: true" in html
    assert "maxBundleEstimates = 10" in html
    assert "maxBundleItems = 12000" in html
    assert "bundleBridge.importEstimates" in html
    assert "Не удалось подтвердить пакет" in html
    assert "PMBI_CRM_PASSWORD" not in html
    capabilities = re.findall(r'data-estimate-capability="([^"]+)"', html)
    assert len(capabilities) == 2
    assert all(web_ui._verify_estimate_import_capability(estimate_id, token) for estimate_id, token in zip(("aaa111", "bbb222"), capabilities))


def test_embed_bridge_accepts_bundle_but_keeps_exact_parent_boundary() -> None:
    bridge = (web_ui.REPO_ROOT / "autobot" / "static" / "embed_bridge.js").read_text(encoding="utf-8")

    assert "payload.estimates" in bridge
    assert "importEstimates: importEstimatePayload" in bridge
    assert "event.source !== window.parent" in bridge
    assert "event.origin !== trustedParentOrigin" in bridge
    assert 'window.parent.postMessage(message, trustedParentOrigin)' in bridge
    assert "PMBI_CRM_PASSWORD" not in bridge
