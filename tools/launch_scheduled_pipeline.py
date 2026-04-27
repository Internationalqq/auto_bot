#!/usr/bin/env python3
"""Старт планового пайплайна из корня репозитория (без PYTHONPATH)."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    runpy.run_module("autobot.scheduled_pipeline", run_name="__main__", alter_sys=True)
