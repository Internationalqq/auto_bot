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
    assert response.get_json()["ok"] is False
    assert response.get_json()["error"] == "cross_site_request_blocked"
    assert "Обновите страницу" in response.get_json()["message"]


def test_server_to_server_mutation_without_browser_origin_is_not_blocked() -> None:
    response = web_ui.app.test_client().post("/__cross_site_probe__")

    assert response.status_code == 404


def test_public_proxy_origin_is_allowed_without_trusting_arbitrary_headers(monkeypatch) -> None:
    monkeypatch.setenv("WEB_UI_PUBLIC_BASE_URL", "https://public.example/autobot")

    response = web_ui.app.test_client().post(
        "/__cross_site_probe__",
        headers={"Origin": "https://public.example", "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 404


def test_parent_crm_origin_is_allowed_and_idna_normalized(monkeypatch) -> None:
    unicode_host = "стройка.рф"
    ascii_host = unicode_host.encode("idna").decode("ascii")
    monkeypatch.setenv("PMBI_CRM_PARENT_ORIGIN", f"https://{unicode_host}")

    response = web_ui.app.test_client().post(
        "/__cross_site_probe__",
        headers={"Origin": f"https://{ascii_host}", "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 404


def test_report_site_public_origin_is_allowed_when_it_is_the_only_config(monkeypatch) -> None:
    for name in (
        "WEB_UI_PUBLIC_BASE_URL",
        "REPORT_SITE_PUBLIC_BASE_URL",
        "PMBI_PUBLIC_BASE_URL",
        "PMBI_CRM_PUBLIC_URL",
        "PMBI_CRM_PARENT_ORIGIN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REPORT_SITE_PUBLIC_BASE_URL", "https://reports.example/autobot")

    response = web_ui.app.test_client().post(
        "/__cross_site_probe__",
        headers={"Origin": "https://reports.example", "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 404


def test_configured_origins_take_precedence_over_request_host(monkeypatch) -> None:
    monkeypatch.setenv("WEB_UI_PUBLIC_BASE_URL", "https://public.example")

    response = web_ui.app.test_client().post(
        "/__cross_site_probe__",
        base_url="http://spoofed.internal",
        headers={"Origin": "http://spoofed.internal", "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "cross_site_request_blocked"


def test_request_host_is_used_for_local_development_without_configured_origin(monkeypatch) -> None:
    for name in (
        "WEB_UI_PUBLIC_BASE_URL",
        "REPORT_SITE_PUBLIC_BASE_URL",
        "PMBI_PUBLIC_BASE_URL",
        "PMBI_CRM_PUBLIC_URL",
        "PMBI_CRM_PARENT_ORIGIN",
    ):
        monkeypatch.delenv(name, raising=False)

    response = web_ui.app.test_client().post(
        "/__cross_site_probe__",
        base_url="http://localhost:5000",
        headers={"Origin": "http://localhost:5000", "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 404


def test_unconfigured_origin_is_rejected_even_without_cross_site_fetch_header(monkeypatch) -> None:
    monkeypatch.setenv("WEB_UI_PUBLIC_BASE_URL", "https://public.example")

    response = web_ui.app.test_client().post(
        "/__cross_site_probe__",
        headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "cross_site_request_blocked"
