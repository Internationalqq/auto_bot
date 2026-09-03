from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from autobot.agent_market_queue import (
    claim_job,
    complete_job,
    enqueue_jobs,
    fail_job,
    heartbeat_job,
    get_or_create_worker_token,
    job_progress,
    job_summary,
    list_jobs,
    patch_queued_job_payloads,
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

    def test_web_and_avito_jobs_can_coexist_for_same_position(self) -> None:
        web = enqueue_jobs(
            "12345678",
            [{**self.position, "search_mode": "fast_web"}],
            path=self.db_path,
        )
        avito = enqueue_jobs(
            "12345678",
            [{**self.position, "search_mode": "avito_agent"}],
            path=self.db_path,
        )

        self.assertEqual(len(web["created"]), 1)
        self.assertEqual(len(avito["created"]), 1)
        self.assertEqual(len(list_jobs("12345678", path=self.db_path, mode="web")), 1)
        self.assertEqual(len(list_jobs("12345678", path=self.db_path, mode="avito")), 1)
        self.assertEqual(job_progress("12345678", path=self.db_path, mode="avito")["total"], 1)

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

    def test_progress_uses_latest_attempt_per_position(self) -> None:
        second = {**self.position, "position_key": "second", "name": "Second position"}
        enqueue_jobs("12345678", [self.position, second], path=self.db_path)
        first_job = claim_job("mac-mini", path=self.db_path)
        self.assertTrue(fail_job(first_job["id"], "mac-mini", "temporary", path=self.db_path))
        enqueue_jobs("12345678", [self.position], path=self.db_path)

        second_job = claim_job("mac-mini", path=self.db_path)
        complete_job(second_job["id"], "mac-mini", {"offers": [{"price": 100}]}, path=self.db_path)

        progress = job_progress("12345678", path=self.db_path)
        self.assertEqual(progress["total"], 2)
        self.assertEqual(progress["processed"], 1)
        self.assertEqual(progress["completed"], 1)
        self.assertEqual(progress["failed"], 0)
        self.assertEqual(progress["queued"], 1)
        self.assertEqual(progress["current_index"], 2)
        self.assertEqual(progress["offers_found"], 1)
        self.assertEqual(progress["current"]["position_key"], "abc123")

    def test_worker_token_is_generated_once_and_persisted(self) -> None:
        token_path = Path(self.tempdir.name) / "worker.token"
        first = get_or_create_worker_token(token_path)
        second = get_or_create_worker_token(token_path)
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual(first, second)
        self.assertEqual(token_path.read_text(encoding="utf-8").strip(), first)

    def test_patch_queued_payloads_does_not_change_leased_job(self) -> None:
        second = {**self.position, "position_key": "second", "name": "Second position"}
        enqueue_jobs("12345678", [self.position, second], path=self.db_path)
        leased = claim_job("mac-mini", path=self.db_path)

        changed = patch_queued_job_payloads(
            "12345678",
            {"search_mode": "fast_web", "max_sources": 3},
            path=self.db_path,
        )

        jobs = {job["position_key"]: job for job in list_jobs("12345678", path=self.db_path)}
        self.assertEqual(changed, 1)
        self.assertNotIn("search_mode", leased["payload"])
        self.assertNotIn("search_mode", jobs[leased["position_key"]]["payload"])
        self.assertEqual(jobs["second"]["payload"]["search_mode"], "fast_web")
        self.assertEqual(jobs["second"]["payload"]["max_sources"], 3)

    def test_deterministic_empty_result_is_not_retried(self) -> None:
        enqueue_jobs(
            "12345678",
            [{**self.position, "max_attempts": 2, "retry_policy": "network_only"}],
            path=self.db_path,
        )
        job = claim_job("mac-mini", path=self.db_path)

        self.assertTrue(
            fail_job(
                job["id"],
                "mac-mini",
                "result has no acceptable offers with direct URLs",
                path=self.db_path,
                retry=True,
            )
        )
        self.assertEqual(list_jobs("12345678", path=self.db_path)[0]["status"], "failed")

    def test_transient_browser_failure_gets_one_retry(self) -> None:
        enqueue_jobs(
            "12345678",
            [{**self.position, "max_attempts": 2, "retry_policy": "network_only"}],
            path=self.db_path,
        )
        first = claim_job("mac-mini", path=self.db_path)
        self.assertTrue(fail_job(first["id"], "mac-mini", "browser crash: DevToolsActivePort", path=self.db_path, retry=True))
        self.assertEqual(list_jobs("12345678", path=self.db_path)[0]["status"], "queued")

        second = claim_job("mac-mini", path=self.db_path)
        self.assertTrue(fail_job(second["id"], "mac-mini", "browser crash: DevToolsActivePort", path=self.db_path, retry=True))
        self.assertEqual(list_jobs("12345678", path=self.db_path)[0]["status"], "failed")

    def test_expired_lease_stops_at_payload_retry_limit(self) -> None:
        enqueue_jobs(
            "12345678",
            [{**self.position, "max_attempts": 2}],
            path=self.db_path,
        )
        first = claim_job("worker-1", path=self.db_path)
        self.assertEqual(first["attempts"], 1)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE agent_market_jobs SET lease_until = 0 WHERE id = ?",
                (first["id"],),
            )
            connection.commit()

        second = claim_job("worker-2", path=self.db_path)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["attempts"], 2)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE agent_market_jobs SET lease_until = 0 WHERE id = ?",
                (first["id"],),
            )
            connection.commit()

        self.assertIsNone(claim_job("worker-3", path=self.db_path))
        exhausted = list_jobs("12345678", path=self.db_path)[0]
        self.assertEqual(exhausted["status"], "failed")
        self.assertEqual(exhausted["attempts"], 2)
        self.assertEqual(exhausted["worker_id"], "")
        self.assertIsNone(exhausted["lease_until"])
        self.assertIsNotNone(exhausted["completed_at"])
        self.assertIn("лимит повторов исчерпан", exhausted["error"])


if __name__ == "__main__":
    unittest.main()
