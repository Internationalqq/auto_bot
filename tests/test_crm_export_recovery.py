from __future__ import annotations

import unittest
from unittest.mock import patch

from autobot import web_ui


class _Response:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class _Session:
    def __init__(self, project: dict):
        self.project = project
        self.calls: list[tuple[str, str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _Response(payload={"projects": [self.project]})

    def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/api/auth/login"):
            return _Response(payload={"ok": True})
        if url.endswith("/estimate-import"):
            return _Response(
                201,
                {"imported": 1, "estimateSources": 1, "items": [{"id": 1}]},
            )
        if url.endswith("/bootstrap"):
            return _Response(payload={"summary": {"tasks": 1, "stages": 0}})
        raise AssertionError(f"Unexpected POST {url}")


class CrmExportRecoveryTests(unittest.TestCase):
    def test_tender_retry_repairs_missing_starter_task_with_stable_key(self) -> None:
        tender_id = "0123456789012345678"
        session = _Session({"id": 42, "title": "Школа", "contract_no": tender_id})
        with (
            patch("requests.Session", return_value=session),
            patch.object(web_ui, "_crm_credentials", return_value=("director", "secret")),
            patch.object(web_ui, "_crm_base_url", return_value="http://crm"),
            patch.object(web_ui, "load_tender_metadata", return_value={tender_id: {"title": "Ремонт школы"}}),
            patch.object(
                web_ui,
                "_build_crm_project_payload",
                return_value=(
                    {"title": "Ремонт школы", "contract_no": tender_id},
                    [{"title": "Работа", "planned_qty": 1}],
                ),
            ),
        ):
            result = web_ui.export_tender_to_crm(tender_id)

        self.assertTrue(result["already_exists"])
        self.assertEqual(result["summary"]["tasks"], 1)
        self.assertFalse(any(method == "POST" and url == "http://crm/api/projects" for method, url, _ in session.calls))
        bootstrap = next(call for call in session.calls if call[1].endswith("/bootstrap"))
        self.assertFalse(bootstrap[2]["json"]["replace_existing"])
        self.assertEqual(
            bootstrap[2]["json"]["tasks"][0]["client_request_id"],
            f"autobot:tender:{tender_id}:starter",
        )

    def test_estimate_retry_repairs_missing_starter_task_with_stable_key(self) -> None:
        estimate_id = "abc123def456"
        contract_no = f"СМЕТА-{estimate_id}"
        session = _Session({"id": 77, "title": "Сметный объект", "contract_no": contract_no})
        with (
            patch("requests.Session", return_value=session),
            patch.object(web_ui, "_crm_credentials", return_value=("director", "secret")),
            patch.object(web_ui, "_crm_base_url", return_value="http://crm"),
            patch.object(
                web_ui,
                "_build_estimate_crm_project_payload",
                return_value=(
                    {"title": "Сметный объект", "contract_no": contract_no},
                    [{"title": "Материал", "planned_qty": 1}],
                    {"estimate_title": "Смета школы", "original_filename": "school.xlsx"},
                ),
            ),
            patch.object(
                web_ui,
                "_build_estimate_crm_import_payload",
                return_value={
                    "items": [{"title": "Материал", "planned_qty": 1}],
                    "source": {
                        "sourceType": "estimate",
                        "sourceKey": estimate_id,
                        "title": "Смета школы",
                    },
                    "label": "Смета школы",
                    "reference": f"/estimates/{estimate_id}",
                    "replace_source": True,
                },
            ),
        ):
            result = web_ui.export_estimate_to_crm(estimate_id)

        self.assertTrue(result["already_exists"])
        self.assertEqual(result["summary"]["tasks"], 1)
        bootstrap = next(call for call in session.calls if call[1].endswith("/bootstrap"))
        self.assertEqual(
            bootstrap[2]["json"]["tasks"][0]["client_request_id"],
            f"autobot:estimate:{estimate_id}:starter",
        )


if __name__ == "__main__":
    unittest.main()
