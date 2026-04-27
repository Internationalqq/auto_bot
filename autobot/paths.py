"""Корень репозитория и каталоги данных — не зависят от того, из какого подпакета импортируют код."""

from __future__ import annotations

from pathlib import Path

# auto_bot/autobot/paths.py → родитель autobot/ — корень репозитория
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = REPO_ROOT / "data"
REPORTS_DIR: Path = DATA_DIR / "reports"
REPORTS_SITE_DIR: Path = DATA_DIR / "reports_site"
