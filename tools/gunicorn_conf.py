"""Production Gunicorn defaults for the AutoBot web UI container."""
from __future__ import annotations

import os


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = (os.environ.get(name) or str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


_host = (os.environ.get("WEB_UI_HOST") or "0.0.0.0").strip() or "0.0.0.0"
_port = _env_int("WEB_UI_PORT", 8765, minimum=1, maximum=65535)

bind = f"{_host}:{_port}"

# AutoBot keeps job progress and executor state in-process. One process plus a
# thread pool preserves that state while still allowing health/status requests
# to be served during longer operations. More workers are opt-in only.
worker_class = "gthread"
workers = _env_int("WEB_UI_WORKERS", 1, minimum=1, maximum=4)
threads = _env_int("WEB_UI_THREADS", 8, minimum=2, maximum=32)

timeout = _env_int("WEB_UI_WORKER_TIMEOUT_SEC", 600, minimum=30, maximum=86400)
graceful_timeout = _env_int("WEB_UI_GRACEFUL_TIMEOUT_SEC", 30, minimum=5, maximum=300)
keepalive = _env_int("WEB_UI_KEEPALIVE_SEC", 5, minimum=1, maximum=75)

accesslog = "-"
errorlog = "-"
capture_output = True
preload_app = False


def post_worker_init(worker):
    from autobot.estimate_publication_recovery import recover_pending_publications
    from autobot.report_prompt import REPORTS_DIR
    recover_pending_publications(REPORTS_DIR)
    from autobot.agent_market_delivery import start_delivery_recovery
    start_delivery_recovery()
    from autobot.market_web_worker import start_web_worker
    start_web_worker()
