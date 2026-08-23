"""Recoverable deletion of a tender and its generated artifacts."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from autobot.paths import DATA_DIR


def _validate_tender_id(value: object) -> str:
    tender_id = str(value or "").strip()
    if not re.fullmatch(r"\d{8,25}", tender_id):
        raise ValueError("Некорректный номер тендера")
    return tender_id


def _read_json(path: Path, fallback: Any) -> Any:
    if not path.is_file():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _tree_stats(path: Path) -> tuple[int, int]:
    if path.is_file():
        try:
            return 1, int(path.stat().st_size)
        except OSError:
            return 1, 0
    files = 0
    size = 0
    if path.is_dir():
        for child in path.rglob("*"):
            if not child.is_file() or child.is_symlink():
                continue
            files += 1
            try:
                size += int(child.stat().st_size)
            except OSError:
                pass
    return files, size


def _artifact_targets(data_dir: Path, tender_id: str) -> list[tuple[str, Path]]:
    targets: list[tuple[str, Path]] = []
    for category in ("downloads", "extracted", "reports_site"):
        path = data_dir / category / tender_id
        if path.exists() and not path.is_symlink():
            targets.append((category, path))
    for category in ("reports", "logs"):
        folder = data_dir / category
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if tender_id in path.name and not path.is_symlink():
                targets.append((category, path))
    return targets


def _checkpoint_without_tender(payload: Any, tender_id: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    result = dict(payload)
    for key in ("completed_ids", "new_ids"):
        value = result.get(key)
        if isinstance(value, list):
            result[key] = [item for item in value if str(item or "").strip() != tender_id]
    filtered = result.get("filtered_tenders")
    if isinstance(filtered, list):
        result["filtered_tenders"] = [
            item for item in filtered
            if not isinstance(item, dict) or str(item.get("tender_id") or "").strip() != tender_id
        ]
    return result


def delete_tender_data(tender_id: object, *, data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Move tender artifacts to local trash and remove it from discovery state."""

    tid = _validate_tender_id(tender_id)
    root = Path(data_dir).resolve()
    tenders_path = root / "tenders.json"
    checkpoint_path = root / "search_resume_checkpoint.json"
    last_ids_path = root / "last_new_tender_ids.txt"

    tenders_payload = _read_json(tenders_path, [])
    if not isinstance(tenders_payload, list):
        raise RuntimeError("Файл tenders.json повреждён: ожидался список")
    matching_rows = [
        row for row in tenders_payload
        if isinstance(row, dict) and str(row.get("tender_id") or "").strip() == tid
    ]
    remaining_rows = [
        row for row in tenders_payload
        if not isinstance(row, dict) or str(row.get("tender_id") or "").strip() != tid
    ]
    targets = _artifact_targets(root, tid)
    if not matching_rows and not targets:
        raise FileNotFoundError(f"Тендер {tid} не найден")

    checkpoint_original = _read_json(checkpoint_path, {})
    checkpoint_updated = _checkpoint_without_tender(checkpoint_original, tid)
    last_ids_original = last_ids_path.read_text(encoding="utf-8", errors="ignore") if last_ids_path.is_file() else ""
    last_ids_updated = "\n".join(
        line for line in last_ids_original.splitlines()
        if line.strip() != tid
    )
    if last_ids_original.endswith("\n") and last_ids_updated:
        last_ids_updated += "\n"

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trash_dir = root / "trash" / "tenders" / tid / f"{stamp}_{uuid.uuid4().hex[:8]}"
    trash_dir.mkdir(parents=True, exist_ok=False)
    snapshot = {
        "tender_id": tid,
        "deleted_at": datetime.now().isoformat(timespec="seconds"),
        "metadata": matching_rows,
        "artifacts": [str(path.relative_to(root)) for _, path in targets],
    }
    (trash_dir / "deletion_manifest.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    moved: list[tuple[Path, Path]] = []
    total_files = 0
    total_bytes = 0
    try:
        for category, source in targets:
            source_resolved = source.resolve()
            if root not in source_resolved.parents:
                raise RuntimeError("Артефакт находится вне каталога данных")
            file_count, byte_count = _tree_stats(source)
            destination_dir = trash_dir / category
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / source.name
            shutil.move(str(source), str(destination))
            moved.append((source, destination))
            total_files += file_count
            total_bytes += byte_count

        _atomic_json_write(tenders_path, remaining_rows)
        if checkpoint_path.is_file():
            _atomic_json_write(checkpoint_path, checkpoint_updated)
        if last_ids_path.is_file():
            last_ids_path.write_text(last_ids_updated, encoding="utf-8")
    except Exception:
        for source, destination in reversed(moved):
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() and not source.exists():
                    shutil.move(str(destination), str(source))
            except Exception:
                pass
        try:
            _atomic_json_write(tenders_path, tenders_payload)
            if checkpoint_path.is_file():
                _atomic_json_write(checkpoint_path, checkpoint_original)
            if last_ids_path.is_file():
                last_ids_path.write_text(last_ids_original, encoding="utf-8")
        except Exception:
            pass
        raise

    return {
        "tender_id": tid,
        "deleted": True,
        "metadata_rows": len(matching_rows),
        "artifact_groups": len(moved),
        "files_moved": total_files,
        "bytes_moved": total_bytes,
        "trash_path": str(trash_dir.relative_to(root)),
    }

