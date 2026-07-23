#!/usr/bin/env python3
"""CLI Алисы: py -3 tools/run_alice.py --tender-id …"""
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
    runpy.run_module("autobot.alice_market_scraper", run_name="__main__", alter_sys=True)
