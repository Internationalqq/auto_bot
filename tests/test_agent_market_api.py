from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autobot.agent_market_queue as queue
from autobot.web_ui import app


class AgentMarketApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = queue.DEFAULT_DB_PATH
        queue.DEFAULT_DB_PATH = Path(self.tempdir.name) / "jobs.sqlite3"
        self.original_token = os.environ.get("MARKET_AGENT_TOKEN")
        os.environ["MARKET_AGENT_TOKEN"] = "unit-test-secret"
        self.client = app.test_client()
        queue.enqueue_jobs(
            "12345678",
            [{"position_key": "pos-1", "name": "Щебень 20-40", "unit": "м3"}],
        )

    def tearDown(self) -> None:
        queue.DEFAULT_DB_PATH = self.original_path
        if self.original_token is None:
            os.environ.pop("MARKET_AGENT_TOKEN", None)
        else:
            os.environ["MARKET_AGENT_TOKEN"] = self.original_token
        self.tempdir.cleanup()

    def auth(self) -> dict[str, str]:
        return {"Authorization": "Bearer unit-test-secret"}

    def test_claim_requires_token(self) -> None:
        response = self.client.post("/api/agent-market/v1/claim", json={"worker_id": "mac-mini"})
        self.assertEqual(response.status_code, 401)

    @patch("autobot.real_market_scraper.import_agent_market_result")
    def test_claim_and_complete_normalizes_agent_result(self, import_result) -> None:
        import_result.return_value = {"imported": 1}
        claimed = self.client.post(
            "/api/agent-market/v1/claim",
            headers=self.auth(),
            json={"worker_id": "mac-mini", "lease_seconds": 600},
        )
        self.assertEqual(claimed.status_code, 200)
        job = claimed.get_json()["job"]
        response = self.client.post(
            f"/api/agent-market/v1/jobs/{job['id']}/complete",
            headers=self.auth(),
            json={
                "worker_id": "mac-mini",
                "result": {
                    "position_key": "pos-1",
                    "offers": [
                        {
                            "title": "Щебень 20-40",
                            "price": "1 890 руб.",
                            "currency": "RUB",
                            "unit": "м3",
                            "url": "[источник](https://example.ru/scheben/20-40/)",
                        }
                    ],
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        imported_result = import_result.call_args.args[2]
        self.assertEqual(imported_result["offers"][0]["price"], 1890.0)
        self.assertEqual(imported_result["offers"][0]["url"], "https://example.ru/scheben/20-40/")

    def test_complete_rejects_result_for_other_position(self) -> None:
        job = self.client.post(
            "/api/agent-market/v1/claim", headers=self.auth(), json={"worker_id": "mac-mini"}
        ).get_json()["job"]
        response = self.client.post(
            f"/api/agent-market/v1/jobs/{job['id']}/complete",
            headers=self.auth(),
            json={"worker_id": "mac-mini", "result": {"position_key": "other", "offers": []}},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
