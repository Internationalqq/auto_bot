from __future__ import annotations

import io

from autobot import web_ui


def test_upload_limit_defaults_to_100_mb(monkeypatch) -> None:
    monkeypatch.delenv("WEB_UI_MAX_UPLOAD_MB", raising=False)

    assert web_ui._configured_max_upload_mb() == 100


def test_upload_limit_can_be_configured(monkeypatch) -> None:
    monkeypatch.setenv("WEB_UI_MAX_UPLOAD_MB", "150")

    assert web_ui._configured_max_upload_mb() == 150


def test_invalid_upload_limit_uses_default(monkeypatch) -> None:
    monkeypatch.setenv("WEB_UI_MAX_UPLOAD_MB", "not-a-number")

    assert web_ui._configured_max_upload_mb() == 100


def test_oversized_upload_returns_json_error(monkeypatch) -> None:
    monkeypatch.setitem(web_ui.app.config, "MAX_CONTENT_LENGTH", 64)

    response = web_ui.app.test_client().post(
        "/api/estimates/upload",
        data={"file": (io.BytesIO(b"x" * 1024), "estimate.xlsx")},
    )

    assert response.status_code == 413
    assert response.get_json() == {
        "ok": False,
        "message": "Файл слишком большой. Максимальный размер загрузки — 100 МБ.",
    }
