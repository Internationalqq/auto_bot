from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from autobot.paths import REPO_ROOT


DEFAULT_DB_PATH = REPO_ROOT / "data" / "agent_market_jobs.sqlite3"
DEFAULT_TOKEN_PATH = REPO_ROOT / "data" / "agent_market_worker.token"
ACTIVE_STATUSES = ("queued", "leased")
FINAL_STATUSES = ("completed", "failed", "canceled")
_INIT_LOCK = threading.Lock()


def get_or_create_worker_token(path: Path | str | None = None) -> str:
    configured = str(os.environ.get("MARKET_AGENT_TOKEN") or "").strip()
    if configured:
        return configured
    token_path = Path(path or DEFAULT_TOKEN_PATH)
    with _INIT_LOCK:
        try:
            current = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            current = ""
        if len(current) >= 32:
            return current
        token = secrets.token_urlsafe(36)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = token_path.with_suffix(token_path.suffix + ".tmp")
        temp_path.write_text(token + "\n", encoding="utf-8")
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        temp_path.replace(token_path)
        return token


def _now() -> float:
    return time.time()


def _connect(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path or DEFAULT_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=20, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 20000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_db(path: Path | str | None = None) -> Path:
    db_path = Path(path or DEFAULT_DB_PATH)
    with _INIT_LOCK, closing(_connect(db_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_market_jobs (
                id TEXT PRIMARY KEY,
                tender_id TEXT NOT NULL,
                position_key TEXT NOT NULL,
                position_name TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                priority INTEGER NOT NULL DEFAULT 100,
                attempts INTEGER NOT NULL DEFAULT 0,
                worker_id TEXT NOT NULL DEFAULT '',
                lease_until REAL,
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_market_claim
                ON agent_market_jobs(status, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_market_tender
                ON agent_market_jobs(tender_id, created_at DESC);
            """
        )
    return db_path


def _row_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for field in ("payload_json", "result_json"):
        raw = item.pop(field, "")
        try:
            item[field.removesuffix("_json")] = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            item[field.removesuffix("_json")] = None
    return item


def enqueue_jobs(
    tender_id: str,
    positions: list[dict[str, Any]],
    *,
    path: Path | str | None = None,
    priority: int = 100,
) -> dict[str, Any]:
    init_db(path)
    created: list[dict[str, Any]] = []
    skipped: list[str] = []
    now = _now()
    with closing(_connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for payload in positions:
                key = str(payload.get("position_key") or "").strip()
                name = str(payload.get("name") or "").strip()
                if not key or not name:
                    continue
                active = connection.execute(
                    """SELECT id FROM agent_market_jobs
                       WHERE tender_id = ? AND position_key = ? AND status IN ('queued', 'leased')
                       LIMIT 1""",
                    (tender_id, key),
                ).fetchone()
                if active:
                    skipped.append(key)
                    continue
                job_id = uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO agent_market_jobs
                       (id, tender_id, position_key, position_name, payload_json, status,
                        priority, attempts, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'queued', ?, 0, ?, ?)""",
                    (
                        job_id,
                        tender_id,
                        key,
                        name,
                        json.dumps(payload, ensure_ascii=False),
                        int(priority),
                        now,
                        now,
                    ),
                )
                created.append({"id": job_id, "position_key": key, "position_name": name})
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    return {"created": created, "skipped_active": skipped}


def claim_job(
    worker_id: str,
    *,
    path: Path | str | None = None,
    lease_seconds: int = 300,
) -> dict[str, Any] | None:
    init_db(path)
    worker = str(worker_id or "").strip()[:120]
    if not worker:
        raise ValueError("worker_id is required")
    now = _now()
    lease = now + max(60, min(int(lease_seconds or 300), 1800))
    with closing(_connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """UPDATE agent_market_jobs
                   SET status = 'queued', worker_id = '', lease_until = NULL,
                       error = 'Предыдущий агент не продлил аренду; задание возвращено в очередь',
                       updated_at = ?
                   WHERE status = 'leased' AND lease_until IS NOT NULL AND lease_until <= ?""",
                (now, now),
            )
            row = connection.execute(
                """SELECT * FROM agent_market_jobs
                   WHERE status = 'queued'
                   ORDER BY priority ASC, created_at ASC
                   LIMIT 1"""
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            connection.execute(
                """UPDATE agent_market_jobs
                   SET status = 'leased', worker_id = ?, lease_until = ?, attempts = attempts + 1,
                       error = '', updated_at = ?
                   WHERE id = ? AND status = 'queued'""",
                (worker, lease, now, row["id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM agent_market_jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    return _row_payload(claimed)


def heartbeat_job(
    job_id: str,
    worker_id: str,
    *,
    path: Path | str | None = None,
    lease_seconds: int = 300,
) -> bool:
    now = _now()
    lease = now + max(60, min(int(lease_seconds or 300), 1800))
    with closing(_connect(path)) as connection:
        cursor = connection.execute(
            """UPDATE agent_market_jobs SET lease_until = ?, updated_at = ?
               WHERE id = ? AND status = 'leased' AND worker_id = ?""",
            (lease, now, job_id, worker_id),
        )
    return cursor.rowcount == 1


def complete_job(
    job_id: str,
    worker_id: str,
    result: dict[str, Any],
    *,
    path: Path | str | None = None,
) -> dict[str, Any] | None:
    now = _now()
    with closing(_connect(path)) as connection:
        cursor = connection.execute(
            """UPDATE agent_market_jobs
               SET status = 'completed', result_json = ?, error = '', lease_until = NULL,
                   updated_at = ?, completed_at = ?
               WHERE id = ? AND status = 'leased' AND worker_id = ?""",
            (json.dumps(result, ensure_ascii=False), now, now, job_id, worker_id),
        )
        if cursor.rowcount != 1:
            return None
        row = connection.execute("SELECT * FROM agent_market_jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_payload(row)


def fail_job(
    job_id: str,
    worker_id: str,
    error: str,
    *,
    path: Path | str | None = None,
    retry: bool = False,
) -> bool:
    status = "queued" if retry else "failed"
    now = _now()
    with closing(_connect(path)) as connection:
        cursor = connection.execute(
            """UPDATE agent_market_jobs
               SET status = ?, error = ?, worker_id = '', lease_until = NULL, updated_at = ?
               WHERE id = ? AND status = 'leased' AND worker_id = ?""",
            (status, str(error or "Ошибка агента")[:2000], now, job_id, worker_id),
        )
    return cursor.rowcount == 1


def cancel_job(job_id: str, tender_id: str, *, path: Path | str | None = None) -> bool:
    now = _now()
    with closing(_connect(path)) as connection:
        cursor = connection.execute(
            """UPDATE agent_market_jobs
               SET status = 'canceled', lease_until = NULL, updated_at = ?
               WHERE id = ? AND tender_id = ? AND status IN ('queued', 'leased')""",
            (now, job_id, tender_id),
        )
    return cursor.rowcount == 1


def list_jobs(
    tender_id: str,
    *,
    path: Path | str | None = None,
    limit: int = 250,
) -> list[dict[str, Any]]:
    init_db(path)
    with closing(_connect(path)) as connection:
        rows = connection.execute(
            """SELECT * FROM agent_market_jobs WHERE tender_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (tender_id, max(1, min(int(limit), 1000))),
        ).fetchall()
    return [_row_payload(row) or {} for row in rows]


def job_summary(tender_id: str, *, path: Path | str | None = None) -> dict[str, int]:
    counts = {status: 0 for status in (*ACTIVE_STATUSES, *FINAL_STATUSES)}
    counts["total"] = 0
    for job in list_jobs(tender_id, path=path):
        status = str(job.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
        counts["total"] += 1
    return counts


def job_progress(tender_id: str, *, path: Path | str | None = None) -> dict[str, Any]:
    """Return progress for the latest attempt of every unique position.

    A tender can contain retries and old canceled jobs. Counting raw database
    rows makes the UI jump backwards and inflates the total, so the progress
    view intentionally keeps only the newest job for each position key.
    """
    latest_by_position: dict[str, dict[str, Any]] = {}
    for job in list_jobs(tender_id, path=path, limit=1000):
        key = str(job.get("position_key") or "").strip()
        if key and key not in latest_by_position:
            latest_by_position[key] = job

    jobs = list(latest_by_position.values())
    counts = {status: 0 for status in (*ACTIVE_STATUSES, *FINAL_STATUSES)}
    offers_found = 0
    for job in jobs:
        status = str(job.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
        result = job.get("result") or {}
        offers_found += len(result.get("offers") or [])

    total = len(jobs)
    processed = sum(counts.get(status, 0) for status in FINAL_STATUSES)
    percent = int(round((processed / total) * 100)) if total else 0
    active = sorted(
        (job for job in jobs if str(job.get("status") or "") in ACTIVE_STATUSES),
        key=lambda item: (
            0 if item.get("status") == "leased" else 1,
            int(item.get("priority") or 100),
            float(item.get("created_at") or 0),
        ),
    )
    current = active[0] if active else None
    recent = sorted(
        (job for job in jobs if str(job.get("status") or "") in FINAL_STATUSES),
        key=lambda item: float(item.get("updated_at") or 0),
        reverse=True,
    )[:6]

    def public_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
        if not job:
            return None
        result = job.get("result") or {}
        return {
            "id": job.get("id"),
            "position_key": job.get("position_key"),
            "position_name": job.get("position_name"),
            "status": job.get("status"),
            "attempts": int(job.get("attempts") or 0),
            "worker_id": job.get("worker_id") or "",
            "error": job.get("error") or "",
            "offers_found": len(result.get("offers") or []),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "completed_at": job.get("completed_at"),
        }

    return {
        "total": total,
        "processed": processed,
        "remaining": max(0, total - processed),
        "percent": max(0, min(100, percent)),
        "current_index": min(total, processed + 1) if current else processed,
        "queued": counts.get("queued", 0),
        "leased": counts.get("leased", 0),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "canceled": counts.get("canceled", 0),
        "offers_found": offers_found,
        "running": bool(active),
        "current": public_job(current),
        "recent": [public_job(job) for job in recent],
        "updated_at": max((float(job.get("updated_at") or 0) for job in jobs), default=0),
    }


def get_job(job_id: str, *, path: Path | str | None = None) -> dict[str, Any] | None:
    init_db(path)
    with closing(_connect(path)) as connection:
        row = connection.execute("SELECT * FROM agent_market_jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_payload(row)
