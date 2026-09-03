from __future__ import annotations

import gzip

from flask import Response

from autobot import web_ui


def test_large_text_response_is_gzipped_and_hardened() -> None:
    with web_ui.app.test_request_context(headers={"Accept-Encoding": "gzip"}):
        response = web_ui.app.process_response(
            Response("AutoBot " * 1000, content_type="text/html; charset=utf-8")
        )

    assert response.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in response.headers.get("Vary", "")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert gzip.decompress(response.get_data()).decode("utf-8").startswith("AutoBot")


def test_gzip_q_zero_is_respected() -> None:
    with web_ui.app.test_request_context(headers={"Accept-Encoding": "gzip;q=0"}):
        response = web_ui.app.process_response(
            Response("AutoBot " * 1000, content_type="text/html; charset=utf-8")
        )

    assert "Content-Encoding" not in response.headers


def test_cross_site_browser_mutation_is_rejected() -> None:
    response = web_ui.app.test_client().post(
        "/__cross_site_probe__",
        headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "ok": False,
        "error": "cross_site_request_blocked",
    }


def test_server_to_server_mutation_without_browser_origin_is_not_blocked() -> None:
    response = web_ui.app.test_client().post("/__cross_site_probe__")

    assert response.status_code == 404
