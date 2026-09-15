"""Delivery of accepted agent evidence, including recovery after process restart."""
from __future__ import annotations

import logging
import sqlite3
import threading

from autobot import agent_market_queue as queue
from autobot.real_market_scraper import prepare_agent_market_result, publish_agent_market_result

_start_lock = threading.Lock()
_started = False


class DeliveryConflict(ValueError):
    pass


def complete_agent_result(job_id, worker_id, result, *, lease_token=None):
    received = queue.received_job_result(job_id, worker_id, result, lease_token=lease_token)
    if not received:
        if not queue.owns_current_lease(job_id, worker_id, lease_token=lease_token):
            raise DeliveryConflict('Попытка задания истекла, отменена или принадлежит другому исполнителю')
        job = queue.get_job(job_id)
        prepared = prepare_agent_market_result(job['tender_id'], job.get('payload') or {}, result)
        received = queue.accept_job_result(job_id, worker_id, result, prepared, lease_token=lease_token)
        if not received:
            raise DeliveryConflict('Попытка задания изменилась во время проверки; отчёт не обновлён')
    if received.get('status') == 'completed':
        return received
    try:
        completed = queue.apply_accepted_result(job_id, publish_agent_market_result)
    except (OSError, sqlite3.OperationalError):
        # Acknowledging durable acceptance lets the worker stop safely. The
        # recovery loop publishes without another paid/browser search.
        latest = queue.get_job(job_id)
        if latest and (latest.get('delivery_pending') or latest.get('status') == 'completed'):
            return latest
        raise DeliveryConflict('Задание больше не ожидает публикации')
    if completed is None:
        raise DeliveryConflict('Задание отменено до публикации')
    return completed


def recover_accepted_results(*, limit=10):
    outcomes = {'completed': 0, 'pending': 0, 'failed': 0}
    for job_id in queue.pending_deliveries(limit=limit):
        try:
            job = queue.apply_accepted_result(job_id, publish_agent_market_result)
            if job and job.get('status') == 'completed':
                outcomes['completed'] += 1
        except (ValueError, TypeError):
            outcomes['failed'] += 1
        except (OSError, sqlite3.OperationalError):
            outcomes['pending'] += 1
    return outcomes


def start_delivery_recovery():
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    def recover():
        timer = threading.Event()
        while True:
            try:
                recover_accepted_results()
            except Exception:
                logging.getLogger(__name__).exception('Agent result recovery failed')
            timer.wait(30)

    threading.Thread(target=recover, name='autobot-result-recovery', daemon=True).start()
