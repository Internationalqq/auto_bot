from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autobot.agent_market_queue import (
    claim_job,
    complete_job,
    enqueue_jobs,
    fail_job,
    heartbeat_job,
    get_or_create_worker_token,
    job_summary,
    list_jobs,
)


class AgentMarketQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "jobs.sqlite3"
        self.position = {
            "position_key": "abc123",
            "name": "Щебень гравийный фракции 20-40",
            "unit": "м3",
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_enqueue_is_idempotent_while_job_is_active(self) -> None:
        first = enqueue_jobs("12345678", [self.position], path=self.db_path)
        second = enqueue_jobs("12345678", [self.position], path=self.db_path)
        self.assertEqual(len(first["created"]), 1)
        self.assertEqual(second["created"], [])
        self.assertEqual(second["skipped_active"], ["abc123"])

    def test_claim_heartbeat_and_complete(self) -> None:
        enqueue_jobs("12345678", [self.position], path=self.db_path)
        job = claim_job("mac-mini", path=self.db_path, lease_seconds=120)
        self.assertIsNotNone(job)
        self.assertEqual(job["payload"]["name"], self.position["name"])
        self.assertTrue(heartbeat_job(job["id"], "mac-mini", path=self.db_path))
        completed = complete_job(
            job["id"],
            "mac-mini",
            {"offers": [{"price": 1890, "url": "https://example.ru/item"}]},
            path=self.db_path,
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(job_summary("12345678", path=self.db_path)["completed"], 1)

    def test_wrong_worker_cannot_finish_job(self) -> None:
        enqueue_jobs("12345678", [self.position], path=self.db_path)
        job = claim_job("mac-mini", path=self.db_path)
        self.assertIsNone(complete_job(job["id"], "other", {"offers": []}, path=self.db_path))
        self.assertFalse(fail_job(job["id"], "other", "no", path=self.db_path))
        self.assertEqual(list_jobs("12345678", path=self.db_path)[0]["status"], "leased")

    def test_worker_token_is_generated_once_and_persisted(self) -> None:
        token_path = Path(self.tempdir.name) / "worker.token"
        first = get_or_create_worker_token(token_path)
        second = get_or_create_worker_token(token_path)
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual(first, second)
        self.assertEqual(token_path.read_text(encoding="utf-8").strip(), first)


if __name__ == "__main__":
    unittest.main()
