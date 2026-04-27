#!/usr/bin/env python3
"""CLI Алисы: py -3 tools/run_alice.py --tender-id …"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    runpy.run_module("autobot.alice_market_scraper", run_name="__main__", alter_sys=True)
