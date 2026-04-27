"""
Базовый URL веб-UI для ссылок в Telegram (merge-report, /reports/…).

Приоритет переменных окружения:
  1) REPORT_SITE_PUBLIC_BASE_URL — полный базовый URL (рекомендуется)
  2) WEB_UI_PUBLIC_BASE_URL — то же, дубль для удобства
  3) WEB_UI_PUBLIC_HOST + WEB_UI_PORT — собрать http://HOST:PORT
"""

from __future__ import annotations

import os


def get_report_site_public_base() -> str:
    for key in ("REPORT_SITE_PUBLIC_BASE_URL", "WEB_UI_PUBLIC_BASE_URL"):
        v = (os.environ.get(key) or "").strip().rstrip("/")
        if v:
            return v
    host = (os.environ.get("WEB_UI_PUBLIC_HOST") or "").strip()
    if not host:
        return ""
    port = (os.environ.get("WEB_UI_PORT") or "8765").strip() or "8765"
    return f"http://{host}:{port}"
