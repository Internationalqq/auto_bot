#!/usr/bin/env python3
"""Старт Flask web_ui из корня репозитория (без PYTHONPATH)."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))

from runtime_paths import configure_runtime_paths

ROOT = TOOLS_DIR.parent

if __name__ == "__main__":
    configure_runtime_paths(ROOT)
    sys.path.insert(0, str(ROOT))
    runpy.run_module("autobot.web_ui", run_name="__main__", alter_sys=True)
