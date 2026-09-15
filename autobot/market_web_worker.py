"""Run queued web searches on the server using the existing durable delivery."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid

from autobot import agent_market_queue as queue
from autobot.atomic_output import output_lock
from autobot.paths import REPO_ROOT
from autobot.real_market_scraper import prepare_builtin_market_result, publish_agent_market_result

_log = logging.getLogger(__name__)
_start_lock = threading.Lock()
_thread = None
_stop = threading.Event()
LEADER_PATH = REPO_ROOT / 'data' / 'market_web_worker'
HEARTBEAT_SECONDS = 20
LEASE_SECONDS = 300


def web_worker_enabled():
    return os.environ.get('MARKET_WEB_WORKER', '1').strip().lower() not in {'0', 'false', 'no', 'off'}


def _pause_seconds():
    try:
        return max(1, min(300, int(os.environ.get('MARKET_PAUSE_SEC', '18'))))
    except (ValueError, TypeError):
        return 18


def run_once(worker_id, *, stopping=None):
    """Claim at most one web job. No jobs are created by this executor."""
    job = queue.claim_job(worker_id, mode='web', lease_seconds=LEASE_SECONDS, include_uploaded=True)
    if job is None:
        return None
    job_id, token = job['id'], job['lease_token']
    finished = threading.Event()
    lost_lease = threading.Event()

    def cancelled():
        return lost_lease.is_set() or (stopping is not None and stopping.is_set())

    def heartbeat():
        while not finished.wait(HEARTBEAT_SECONDS):
            try:
                if queue.heartbeat_job(job_id, worker_id, lease_seconds=LEASE_SECONDS, lease_token=token):
                    continue
            except Exception:
                _log.exception('Web search lease could not be renewed: %s', job_id)
            lost_lease.set()
            return

    pulse = threading.Thread(target=heartbeat, name='autobot-web-lease', daemon=True)
    pulse.start()
    accepted = False
    try:
        result, prepared = prepare_builtin_market_result(job['tender_id'], job['payload'], cancelled=cancelled)
        if cancelled() or not queue.owns_current_lease(job_id, worker_id, lease_token=token):
            return queue.get_job(job_id)
        accepted = bool(queue.accept_job_result(job_id, worker_id, result, prepared, lease_token=token))
        if not accepted:
            return queue.get_job(job_id)
        finished.set()
        try:
            return queue.apply_accepted_result(job_id, publish_agent_market_result)
        except (OSError, sqlite3.OperationalError):
            # The accepted package is durable; the recovery loop retries it
            # without repeating supplier requests.
            return queue.get_job(job_id)
    except Exception as error:
        if not accepted:
            queue.fail_job(job_id, worker_id, str(error), retry=True, lease_token=token)
        _log.exception('Server web search failed: %s', job_id)
        return queue.get_job(job_id)
    finally:
        finished.set()
        pulse.join(timeout=1)


def _work_as_leader(stop):
    worker_id = f'server-web:{os.getpid()}:{uuid.uuid4().hex[:8]}'
    while not stop.is_set():
        try:
            job = run_once(worker_id, stopping=stop)
        except Exception:
            _log.exception('Server web queue unavailable')
            stop.wait(10)
            continue
        stop.wait(_pause_seconds() if job is not None else 3)


def _run(stop):
    while not stop.is_set():
        try:
            # The OS releases this lock if a Gunicorn process dies. Other
            # processes wait here, avoiding parallel server searches.
            with output_lock(LEADER_PATH, timeout=0.1):
                _work_as_leader(stop)
        except TimeoutError:
            pass
        except Exception:
            _log.exception('Server web worker unavailable')
        stop.wait(10)


def start_web_worker():
    """Explicit startup hook; imports and tests never start background work."""
    global _thread
    if not web_worker_enabled():
        return None
    with _start_lock:
        if _thread is None or not _thread.is_alive():
            _stop.clear()
            _thread = threading.Thread(target=_run, args=(_stop,), name='autobot-web-worker', daemon=True)
            _thread.start()
        return _thread
