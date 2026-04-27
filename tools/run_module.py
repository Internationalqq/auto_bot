#!/usr/bin/env python3
"""
Запуск autobot.<подмодуль> с аргументами (как python -m), без зависимости от PYTHONPATH
и при «изолированном» py-launcher: в sys.path добавляется корень репозитория.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: run_module.py <module> [args...]", file=sys.stderr)
        print("example: run_module.py autobot.main --help", file=sys.stderr)
        raise SystemExit(2)
    mod = sys.argv[1]
    sys.path.insert(0, str(ROOT))
    sys.argv = [mod] + sys.argv[2:]
    runpy.run_module(mod, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
