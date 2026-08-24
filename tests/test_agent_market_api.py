from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autobot.agent_market_queue as queue
import autobot.web_ui as web_ui
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

    def test_tender_jobs_exposes_unique_position_progress(self) -> None:
        response = self.client.get("/api/tenders/12345678/agent-market/jobs")
        self.assertEqual(response.status_code, 200)
        progress = response.get_json()["progress"]
        self.assertEqual(progress["total"], 1)
        self.assertEqual(progress["processed"], 0)
        self.assertEqual(progress["current_index"], 1)
        self.assertEqual(progress["current"]["position_key"], "pos-1")

    def test_tender_jobs_filters_avito_queue(self) -> None:
        queue.enqueue_jobs(
            "12345678",
            [{"position_key": "pos-1", "name": "Щебень 20-40", "search_mode": "avito_agent"}],
        )
        response = self.client.get("/api/tenders/12345678/agent-market/jobs?mode=avito")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["mode"], "avito")
        self.assertEqual(payload["progress"]["total"], 1)
        self.assertEqual(payload["progress"]["current"]["job_mode"], "avito")

    def test_post_avito_mode_builds_mac_browser_task(self) -> None:
        reports_dir = Path(self.tempdir.name) / "reports"
        reports_dir.mkdir()
        (reports_dir / "ОТЧЕТ_ПО_СМЕТАМ_12345678.xlsx").touch()
        tender = {
            "region": "Ярославская область",
            "positions": [{
                "position_key": "pos-1",
                "item_no": 1,
                "name": "Щебень 20-40",
                "unit": "м3",
                "verified_count": 0,
                "queries": ["щебень 20-40 цена за м3"],
            }],
        }
        with (
            patch.object(web_ui, "REPORTS_DIR", reports_dir),
            patch.object(web_ui, "load_tender_metadata", return_value={"12345678": {}}),
            patch.object(web_ui, "_tenders_items", return_value=([], {})),
            patch.object(web_ui, "build_tender_detail", return_value=tender),
        ):
            response = self.client.post(
                "/api/tenders/12345678/agent-market/jobs",
                json={"mode": "avito", "position_keys": ["pos-1"]},
            )

        self.assertEqual(response.status_code, 200)
        job = queue.list_jobs("12345678", mode="avito")[0]
        self.assertEqual(job["payload"]["search_mode"], "avito_agent")
        self.assertEqual(job["payload"]["allowed_domains"], ["avito.ru"])
        self.assertIn("https://www.avito.ru/yaroslavl?", job["payload"]["start_urls"][0])
        self.assertIn("%d1%89%d0%b5%d0%b1%d0%b5%d0%bd%d1%8c", job["payload"]["start_urls"][0].casefold())
        self.assertNotIn("%d1%86%d0%b5%d0%bd%d0%b0", job["payload"]["start_urls"][0].casefold())
        self.assertIn("не обходи captcha", job["payload"]["task"].casefold())

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

    @patch("autobot.real_market_scraper.import_agent_market_result")
    @patch("autobot.real_market_scraper.probe_agent_market_start_urls")
    def test_worker_failure_recovers_from_trusted_direct_source(self, probe_sources, import_result) -> None:
        queue.patch_queued_job_payloads(
            "12345678",
            {"start_urls": ["https://supplier.example/sand/"], "max_attempts": 1},
        )
        job = self.client.post(
            "/api/agent-market/v1/claim", headers=self.auth(), json={"worker_id": "mac-mini"}
        ).get_json()["job"]
        probe_sources.return_value = {
            "schema_version": 2,
            "position_key": "pos-1",
            "offers": [{
                "title": "Щебень 20-40",
                "price": 2400,
                "currency": "RUB",
                "unit": "м3",
                "url": "https://supplier.example/sand/",
                "evidence": "Щебень 20-40 — 2400 руб/м3",
            }],
            "_autobot_direct_probe": True,
        }
        import_result.return_value = {"imported": 1, "verified": 1, "offer_outcomes": []}

        response = self.client.post(
            f"/api/agent-market/v1/jobs/{job['id']}/fail",
            headers=self.auth(),
            json={"worker_id": "mac-mini", "error": "result has no acceptable offers with direct URLs", "retry": False},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["recovered"])
        self.assertEqual(queue.get_job(job["id"])["status"], "completed")
        validated = import_result.call_args.args[2]
        self.assertTrue(validated["_autobot_direct_probe"])

    def test_post_filters_ineligible_rows_and_prioritizes_materials(self) -> None:
        reports_dir = Path(self.tempdir.name) / "reports"
        reports_dir.mkdir(exist_ok=True)
        (reports_dir / "ОТЧЕТ_ПО_СМЕТАМ_12345678.xlsx").touch()
        tender = {
            "region": "Ярославская область",
            "positions": [
                {
                    "position_key": "aggregate-1", "name": "Итого по разделу", "unit": "—",
                    "type_slug": "aggregate", "can_auto_price": False, "queries": [],
                },
                {
                    "position_key": "work-1", "name": "Укладка плитки", "unit": "м2",
                    "type_slug": "work", "can_auto_price": True, "queries": ["укладка плитки цена за м2"],
                },
                {
                    "position_key": "material-1", "name": "Щебень 20-40", "unit": "м3",
                    "type_slug": "material", "can_auto_price": True, "queries": ["щебень 20-40 цена за м3"],
                },
            ],
        }
        with (
            patch.object(web_ui, "REPORTS_DIR", reports_dir),
            patch.object(web_ui, "load_tender_metadata", return_value={"12345678": {}}),
            patch.object(web_ui, "_tenders_items", return_value=([], {})),
            patch.object(web_ui, "build_tender_detail", return_value=tender),
        ):
            response = self.client.post("/api/tenders/12345678/agent-market/jobs", json={"mode": "web", "limit": 20})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["created"], 2)
        self.assertEqual(payload["skipped_ineligible"][0]["position_key"], "aggregate-1")
        claimed = queue.claim_job("mac-priority")
        self.assertEqual(claimed["position_key"], "material-1")
        self.assertEqual(claimed["payload"]["max_attempts"], 1)
        self.assertIn("scheben", " ".join(claimed["payload"]["start_urls"]))

    def test_regional_supplier_hints_cover_concrete_and_sand(self) -> None:
        concrete = web_ui._agent_market_start_urls(
            "Бетон В15 М200", ["бетон В15 цена м3"], "Ярославская область"
        )
        sand = web_ui._agent_market_start_urls(
            "Песок строительный мелкий", ["песок цена м3"], "Ярославль"
        )

        self.assertEqual(len(concrete), 3)
        self.assertEqual(len(sand), 4)
        self.assertIn("beton-yrs.ru", concrete[0])
        self.assertIn("pesok_karerniy", sand[0])

    def test_historical_agent_block_price_is_normalized_for_display(self) -> None:
        price, unit = web_ui._agent_market_offer_display_values(
            {"price": 65000, "unit": "100 м"},
            {"price": 65000, "verification": "candidate"},
        )
        self.assertEqual(price, 650)
        self.assertEqual(unit, "м")

        normalized_price, normalized_unit = web_ui._agent_market_offer_display_values(
            {"price": 65000, "unit": "100 м"},
            {"raw_price": 65000, "price": 650, "matched_unit": "м"},
        )
        self.assertEqual(normalized_price, 650)
        self.assertEqual(normalized_unit, "м")

    def test_get_exposes_verified_offer_outcome_for_live_results(self) -> None:
        job = queue.claim_job("mac-mini")
        queue.complete_job(
            job["id"],
            "mac-mini",
            {
                "offers": [{"title": "Щебень 20-40", "price": 2400, "unit": "м3", "url": "https://supplier.example/item/20-40", "evidence": "2400 руб/м3"}],
                "import": {
                    "verified": 1,
                    "offer_outcomes": [{"url": "https://supplier.example/item/20-40", "verification": "verified", "verification_reason": "Цена подтверждена", "matched_unit": "м3"}],
                },
            },
        )

        response = self.client.get("/api/tenders/12345678/agent-market/jobs")
        payload = response.get_json()
        self.assertEqual(payload["result_totals"]["verified"], 1)
        self.assertEqual(payload["results"][0]["verification"], "verified")
        self.assertEqual(payload["results"][0]["url"], "https://supplier.example/item/20-40")


if __name__ == "__main__":
    unittest.main()
