"""Подключение зависимостей для обычного и portable Python."""
from __future__ import annotations

import sys
from pathlib import Path


def configure_runtime_paths(repo_root: Path) -> None:
    """Добавляет локальные и portable site-packages раньше системных путей."""
    candidates = (
        repo_root / ".runtime",
        Path(sys.executable).resolve().parent / "Lib" / "site-packages",
    )
    for candidate in reversed(candidates):
        if candidate.is_dir():
            value = str(candidate)
            if value not in sys.path:
                sys.path.insert(0, value)
