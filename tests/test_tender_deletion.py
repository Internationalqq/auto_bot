from __future__ import annotations

import json

import pytest

from autobot import web_ui
from autobot.tender_deletion import delete_tender_data


def test_delete_tender_moves_artifacts_and_clears_resume_state(tmp_path):
    tender_id = "12345678"
    other_id = "87654321"
    (tmp_path / "downloads" / tender_id).mkdir(parents=True)
    (tmp_path / "downloads" / tender_id / "document.zip").write_bytes(b"source")
    (tmp_path / "extracted" / tender_id).mkdir(parents=True)
    (tmp_path / "extracted" / tender_id / "estimate.xlsx").write_bytes(b"estimate")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / f"ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.xlsx").write_bytes(b"report")
    (tmp_path / "reports" / f"ОТЧЕТ_ПО_СМЕТАМ_{other_id}.xlsx").write_bytes(b"keep")
    (tmp_path / "tenders.json").write_text(
        json.dumps([{"tender_id": tender_id}, {"tender_id": other_id}]), encoding="utf-8"
    )
    (tmp_path / "search_resume_checkpoint.json").write_text(
        json.dumps(
            {
                "completed_ids": [tender_id, other_id],
                "new_ids": [tender_id],
                "filtered_tenders": [{"tender_id": tender_id}, {"tender_id": other_id}],
            }
        ),
        encoding="utf-8",
    )

    result = delete_tender_data(tender_id, data_dir=tmp_path)

    assert result["deleted"] is True
    assert not (tmp_path / "downloads" / tender_id).exists()
    assert not (tmp_path / "extracted" / tender_id).exists()
    assert not (tmp_path / "reports" / f"ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.xlsx").exists()
    assert (tmp_path / "reports" / f"ОТЧЕТ_ПО_СМЕТАМ_{other_id}.xlsx").is_file()
    assert json.loads((tmp_path / "tenders.json").read_text(encoding="utf-8")) == [{"tender_id": other_id}]
    checkpoint = json.loads((tmp_path / "search_resume_checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["completed_ids"] == [other_id]
    assert checkpoint["new_ids"] == []
    assert checkpoint["filtered_tenders"] == [{"tender_id": other_id}]
    trash_root = tmp_path / result["trash_path"]
    assert (trash_root / "downloads" / tender_id / "document.zip").is_file()
    assert (trash_root / "reports" / f"ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.xlsx").is_file()


def test_delete_tender_rejects_bad_id(tmp_path):
    with pytest.raises(ValueError):
        delete_tender_data("../../etc", data_dir=tmp_path)


def test_delete_api_requires_exact_confirmation(monkeypatch):
    tender_id = "12345678"
    calls = []
    monkeypatch.setattr(web_ui, "delete_tender_data", lambda value: calls.append(value) or {"deleted": True})
    web_ui.parse_state["running"] = False
    web_ui.merge_site_state["running"] = False
    client = web_ui.app.test_client()

    missing = client.post(f"/api/tenders/{tender_id}/delete", json={})
    deleted = client.post(
        f"/api/tenders/{tender_id}/delete",
        json={"confirm_tender_id": tender_id},
    )

    assert missing.status_code == 400
    assert deleted.status_code == 200
    assert calls == [tender_id]
