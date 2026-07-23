#!/usr/bin/env python3
"""Установка зависимостей в локальную папку .runtime для portable Python."""
from __future__ import annotations

import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))

from runtime_paths import configure_runtime_paths


ROOT = TOOLS_DIR.parent


def main() -> int:
    configure_runtime_paths(ROOT)
    try:
        from pip._internal.cli.main import main as pip_main
    except ImportError:
        print(
            "pip не найден ни в portable Python, ни в .runtime. "
            "Установите обычный Python 3.11+ с опцией pip.",
            file=sys.stderr,
        )
        return 1

    target = ROOT / ".runtime"
    target.mkdir(parents=True, exist_ok=True)
    return int(
        pip_main(
            [
                "install",
                "--target",
                str(target),
                "--requirement",
                str(ROOT / "requirements.txt"),
            ]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
