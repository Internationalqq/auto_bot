from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from autobot.paths import DATA_DIR, REPORTS_DIR, REPORTS_SITE_DIR


OPEN_STAGE = "Подача заявок"


@dataclass(frozen=True)
class DirectoryUsage:
    name: str
    files: int
    bytes: int

    @property
    def mb(self) -> float:
        return round(self.bytes / (1024 * 1024), 2)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["mb"] = self.mb
        return data


@dataclass(frozen=True)
class TenderWorkflowStatus:
    tender_id: str
    title: str
    region: str
    stage: str
    publish_date: str
    price_rub: float | None
    has_downloads: bool
    has_estimate: bool
    has_estimate_html: bool
    has_market_sources: bool
    has_comparison: bool
    has_report_site: bool
    next_action: str
    next_action_label: str
    is_ready: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _has_any_file(path: Path) -> bool:
    if not path.exists():
        return False
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if child.is_file():
                    return True
                if child.is_dir():
                    stack.append(child)
            except OSError:
                continue
    return False


def _file_exists(directory: Path, name: str) -> bool:
    try:
        return (directory / name).is_file()
    except OSError:
        return False


def _next_action(
    *,
    has_downloads: bool,
    has_estimate: bool,
    has_market_sources: bool,
    has_comparison: bool,
    has_report_site: bool,
) -> tuple[str, str]:
    if not has_downloads:
        return ("download_documents", "Скачать документы")
    if not has_estimate:
        return ("extract_estimate", "Извлечь смету")
    if not has_market_sources:
        return ("find_market_prices", "Найти цены")
    if not has_comparison or not has_report_site:
        return ("build_comparison", "Собрать сравнение")
    return ("review", "Открыть результат")


def build_tender_workflow_overview(
    *,
    data_dir: Path = DATA_DIR,
    reports_dir: Path = REPORTS_DIR,
    reports_site_dir: Path = REPORTS_SITE_DIR,
    limit: int = 300,
) -> list[TenderWorkflowStatus]:
    tenders_path = data_dir / "tenders.json"
    rows = _read_json_list(tenders_path)
    items: list[TenderWorkflowStatus] = []
    for row in rows[: max(0, limit)]:
        tid = str(row.get("tender_id") or "").strip()
        if not tid:
            continue

        has_downloads = _has_any_file(data_dir / "downloads" / tid)
        has_estimate = _file_exists(reports_dir, f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx")
        has_estimate_html = _file_exists(reports_dir, f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.html")
        has_market_sources = _file_exists(reports_dir, f"РЫНОК_ИСТОЧНИКИ_ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx")
        has_comparison = _file_exists(reports_dir, f"СВОДКА_РЫНОК_{tid}.xlsx")
        has_report_site = _file_exists(reports_site_dir / tid, "index.html")
        next_action, next_action_label = _next_action(
            has_downloads=has_downloads,
            has_estimate=has_estimate,
            has_market_sources=has_market_sources,
            has_comparison=has_comparison,
            has_report_site=has_report_site,
        )

        items.append(
            TenderWorkflowStatus(
                tender_id=tid,
                title=str(row.get("title") or "").strip(),
                region=str(row.get("region") or "").strip(),
                stage=str(row.get("stage") or "").strip(),
                publish_date=str(row.get("publish_date") or "").strip(),
                price_rub=row.get("price_rub"),
                has_downloads=has_downloads,
                has_estimate=has_estimate,
                has_estimate_html=has_estimate_html,
                has_market_sources=has_market_sources,
                has_comparison=has_comparison,
                has_report_site=has_report_site,
                next_action=next_action,
                next_action_label=next_action_label,
                is_ready=next_action == "review",
            )
        )
    return items


def directory_usage(path: Path, *, name: str | None = None) -> DirectoryUsage:
    files = 0
    total = 0
    if path.exists():
        stack = [path]
        while stack:
            current = stack.pop()
            try:
                children = list(current.iterdir())
            except OSError:
                continue
            for item in children:
                try:
                    if item.is_file():
                        files += 1
                        total += item.stat().st_size
                    elif item.is_dir():
                        stack.append(item)
                except OSError:
                    continue
    return DirectoryUsage(name=name or path.name, files=files, bytes=total)


def build_storage_overview(*, data_dir: Path = DATA_DIR) -> list[DirectoryUsage]:
    names = [
        "downloads",
        "extracted",
        "reports",
        "reports_site",
        "user_estimates",
        "uploads",
        "logs",
        "alice_playwright_profile",
        "avito_profile",
    ]
    return [directory_usage(data_dir / name, name=name) for name in names]


def build_workflow_payload(*, data_dir: Path = DATA_DIR, include_storage: bool = True) -> dict[str, Any]:
    reports_dir = data_dir / "reports"
    reports_site_dir = data_dir / "reports_site"
    tenders = build_tender_workflow_overview(
        data_dir=data_dir,
        reports_dir=reports_dir,
        reports_site_dir=reports_site_dir,
    )
    counts: dict[str, int] = {}
    for item in tenders:
        counts[item.next_action] = counts.get(item.next_action, 0) + 1
    payload: dict[str, Any] = {
        "tenders": [item.to_dict() for item in tenders],
        "counts": counts,
    }
    if include_storage:
        payload["storage"] = [item.to_dict() for item in build_storage_overview(data_dir=data_dir)]
    return payload
