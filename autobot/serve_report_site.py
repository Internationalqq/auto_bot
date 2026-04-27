#!/usr/bin/env python3
"""
Опционально: отдельная раздача только папки reports_site (порт 8799).

Обычно достаточно py -3 web_ui.py — там же маршрут /merge-report/<id>/
на эти же файлы. Этот скрипт — запасной вариант без Flask.
"""

from __future__ import annotations

import functools
import http.server
import os
import socketserver
from pathlib import Path

from autobot.paths import REPO_ROOT

ROOT = REPO_ROOT / "data" / "reports_site"


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("REPORT_SITE_PORT", "8799"))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
    with socketserver.TCPServer(("0.0.0.0", port), handler) as httpd:
        print(f"Отчёты: {ROOT}")
        print(f"Пример: http://127.0.0.1:{port}/0171200001926001291/index.html")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
