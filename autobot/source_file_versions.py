"""Content-addressed storage and recoverable cleanup for EIS source files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from autobot.paths import DATA_DIR


_HIDDEN_NAMES = {"download_log.json", "desktop.ini"}
_COPY_SUFFIX_RE = re.compile(r"\s+\(\d+\)(?=\.[^.]+$)")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity_name(value: object) -> str:
    name = Path(str(value or "")).name.strip()
    name = _COPY_SUFFIX_RE.sub("", name)
    return re.sub(r"\s+", " ", name).casefold()


def _read_log(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [dict(row) for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _active_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    out: list[Path] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return out
    for path in entries:
        try:
            if (
                path.is_file()
                and not path.is_symlink()
                and path.name.casefold() not in _HIDDEN_NAMES
                and not path.name.startswith(".autobot-incoming-")
            ):
                out.append(path)
        except OSError:
            continue
    return out


def _walk_files(folder: Path, skipped: list[str]) -> list[Path]:
    """Walk a mounted data tree without aborting on one damaged directory."""

    out: list[Path] = []

    def on_error(error: OSError) -> None:
        skipped.append(str(getattr(error, "filename", "") or error))

    for current, directories, names in os.walk(folder, topdown=True, onerror=on_error, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in directories:
            child = current_path / name
            try:
                if not child.is_symlink():
                    safe_directories.append(name)
            except OSError:
                skipped.append(str(child))
        directories[:] = safe_directories
        for name in names:
            path = current_path / name
            if path.name.casefold() in _HIDDEN_NAMES or path.name.startswith("."):
                continue
            try:
                if path.is_file() and not path.is_symlink():
                    out.append(path)
            except OSError:
                skipped.append(str(path))
    return out


def _trash_batch(data_dir: Path, tender_id: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return data_dir / "trash" / "source_file_versions" / tender_id / f"{stamp}_{uuid.uuid4().hex[:8]}"


def _assert_inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise RuntimeError(f"Путь вне каталога данных: {resolved}")
    return resolved


def _move_recoverably(source: Path, destination: Path, *, data_dir: Path) -> Path:
    _assert_inside(source, data_dir)
    _assert_inside(destination.parent, data_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination = destination.with_name(f"{destination.stem}_{uuid.uuid4().hex[:8]}{destination.suffix}")
    shutil.move(str(source), str(destination))
    return destination


def _matching_old_versions(
    tender_dir: Path,
    *,
    preferred_name: str,
    source_url: str,
    previous_log: Iterable[dict[str, Any]],
) -> list[Path]:
    identity = _identity_name(preferred_name)
    names: set[str] = set()
    for row in previous_log:
        saved_name = Path(str(row.get("saved_name") or row.get("saved_path") or "")).name
        same_url = bool(source_url and str(row.get("url") or "").strip() == source_url)
        same_name = _identity_name(row.get("original_name") or saved_name) == identity
        if saved_name and (same_url or same_name):
            names.add(saved_name)
    out: list[Path] = []
    for path in _active_files(tender_dir):
        if path.name in names or _identity_name(path.name) == identity:
            out.append(path)
    return out


def store_downloaded_source_file(
    incoming_path: Path,
    *,
    tender_id: str,
    preferred_name: str,
    source_url: str = "",
    data_dir: Path = DATA_DIR,
    previous_log: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Activate a downloaded file or reuse an identical active copy.

    Equal bytes are discarded by SHA-256. A changed document with the same
    URL/name replaces the active version; the previous file and its extracted
    cache are moved to a recoverable trash batch.
    """

    root = Path(data_dir).resolve()
    tid = str(tender_id or "").strip()
    if not re.fullmatch(r"\d{8,25}", tid):
        raise ValueError("Некорректный номер тендера")
    tender_dir = root / "downloads" / tid
    tender_dir.mkdir(parents=True, exist_ok=True)
    incoming = _assert_inside(Path(incoming_path), tender_dir)
    if not incoming.is_file() or incoming.is_symlink():
        raise FileNotFoundError(incoming)

    incoming_size = incoming.stat().st_size
    incoming_hash = sha256_file(incoming)
    same_content: list[Path] = []
    for existing in _active_files(tender_dir):
        try:
            if existing.stat().st_size == incoming_size and sha256_file(existing) == incoming_hash:
                same_content.append(existing)
        except OSError:
            continue
    if same_content:
        keeper = max(same_content, key=lambda path: path.stat().st_mtime)
        incoming.unlink()
        return {
            "path": keeper,
            "sha256": incoming_hash,
            "action": "unchanged",
            "old_versions_moved": 0,
            "trash_path": "",
        }

    final_path = tender_dir / Path(preferred_name).name
    old_versions = _matching_old_versions(
        tender_dir,
        preferred_name=preferred_name,
        source_url=source_url,
        previous_log=previous_log,
    )
    trash_dir = _trash_batch(root, tid) if old_versions else None
    moved: list[tuple[Path, Path]] = []
    manifest_rows: list[dict[str, Any]] = []
    try:
        for old in old_versions:
            old_hash = sha256_file(old)
            destination = trash_dir / "downloads" / old.name  # type: ignore[operator]
            destination = _move_recoverably(old, destination, data_dir=root)
            moved.append((old, destination))
            manifest_rows.append(
                {
                    "source": str(old.relative_to(root)),
                    "trash": str(destination.relative_to(root)),
                    "sha256": old_hash,
                    "reason": "replaced_by_changed_version",
                }
            )
            extracted_cache = root / "extracted" / tid / old.stem
            if extracted_cache.exists() and not extracted_cache.is_symlink():
                cache_destination = trash_dir / "extracted" / extracted_cache.name  # type: ignore[operator]
                cache_destination = _move_recoverably(extracted_cache, cache_destination, data_dir=root)
                moved.append((extracted_cache, cache_destination))
                manifest_rows.append(
                    {
                        "source": str(extracted_cache.relative_to(root)),
                        "trash": str(cache_destination.relative_to(root)),
                        "reason": "cache_of_replaced_version",
                    }
                )
        if final_path.exists():
            raise RuntimeError(f"Не удалось освободить имя новой версии: {final_path.name}")
        os.replace(incoming, final_path)
    except Exception:
        for source, destination in reversed(moved):
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() and not source.exists():
                    shutil.move(str(destination), str(source))
            except Exception:
                pass
        raise

    if trash_dir is not None:
        _atomic_json_write(
            trash_dir / "version_manifest.json",
            {
                "tender_id": tid,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "new_file": str(final_path.relative_to(root)),
                "new_sha256": incoming_hash,
                "moved": manifest_rows,
            },
        )
    return {
        "path": final_path,
        "sha256": incoming_hash,
        "action": "replaced" if old_versions else "created",
        "old_versions_moved": len(old_versions),
        "trash_path": str(trash_dir.relative_to(root)) if trash_dir else "",
    }


def _keeper_key(path: Path) -> tuple[int, float, int, str]:
    readable_selection = 1 if "выборк" in path.as_posix().casefold() else 0
    no_copy_suffix = 1 if not _COPY_SUFFIX_RE.search(path.name) else 0
    try:
        modified = path.stat().st_mtime
    except OSError:
        modified = 0.0
    return readable_selection, modified, no_copy_suffix, path.as_posix()


def _duplicate_groups(files: Iterable[Path]) -> list[list[Path]]:
    by_size: dict[int, list[Path]] = {}
    for path in files:
        try:
            by_size.setdefault(path.stat().st_size, []).append(path)
        except OSError:
            continue
    groups: list[list[Path]] = []
    for same_size in by_size.values():
        if len(same_size) < 2:
            continue
        by_hash: dict[str, list[Path]] = {}
        for path in same_size:
            try:
                by_hash.setdefault(sha256_file(path), []).append(path)
            except OSError:
                continue
        groups.extend(group for group in by_hash.values() if len(group) > 1)
    return groups


def _path_size(path: Path) -> int:
    if path.is_file():
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0
    total = 0
    skipped: list[str] = []
    for child in _walk_files(path, skipped):
        try:
            total += int(child.stat().st_size)
        except OSError:
            pass
    return total


def _is_inside_any(path: Path, roots: Iterable[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            root_resolved = root.resolve()
        except OSError:
            continue
        if resolved == root_resolved or root_resolved in resolved.parents:
            return True
    return False


def cleanup_existing_source_duplicates(
    tender_id: str | None = None,
    *,
    data_dir: Path = DATA_DIR,
    include_extracted: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Move exact duplicate files from active storage to recoverable trash."""

    root = Path(data_dir).resolve()
    downloads_root = root / "downloads"
    extracted_root = root / "extracted"
    if tender_id is not None and not re.fullmatch(r"\d{8,25}", str(tender_id).strip()):
        raise ValueError("Некорректный номер тендера")
    if tender_id:
        tender_ids = [str(tender_id).strip()]
    else:
        tender_ids = sorted(
            {
                path.name
                for base in (downloads_root, extracted_root)
                if base.is_dir()
                for path in base.iterdir()
                if path.is_dir() and re.fullmatch(r"\d{8,25}", path.name)
            }
        )

    summary: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "tenders_scanned": 0,
        "duplicate_groups": 0,
        "files_moved": 0,
        "bytes_moved": 0,
        "trash_batches": [],
        "details": [],
        "skipped_paths": [],
        "errors": [],
    }
    for tid in tender_ids:
        per_tender: list[tuple[str, Path, Path, str, str]] = []
        downloads_folder = root / "downloads" / tid
        extracted_folder = root / "extracted" / tid
        old_named_files: set[Path] = set()
        old_cache_dirs: set[Path] = set()
        by_identity: dict[str, list[Path]] = {}
        for path in _active_files(downloads_folder):
            by_identity.setdefault(_identity_name(path.name), []).append(path)
        for logical_versions in by_identity.values():
            if len(logical_versions) < 2:
                continue
            keeper = max(logical_versions, key=_keeper_key)
            summary["duplicate_groups"] += 1
            for old in logical_versions:
                if old == keeper:
                    continue
                old_named_files.add(old)
                try:
                    digest = sha256_file(old)
                except OSError:
                    digest = ""
                per_tender.append(("downloads", old, keeper, digest, "older_named_version"))
                cache = extracted_folder / old.stem
                if include_extracted and cache.exists() and not cache.is_symlink():
                    old_cache_dirs.add(cache)
                    per_tender.append(
                        (
                            "extracted",
                            cache,
                            extracted_folder / keeper.stem,
                            "",
                            "cache_of_older_named_version",
                        )
                    )
        categories = ["downloads"] + (["extracted"] if include_extracted else [])
        for category in categories:
            folder = root / category / tid
            if not folder.is_dir():
                continue
            files = _walk_files(folder, summary["skipped_paths"])
            if category == "downloads":
                files = [path for path in files if path not in old_named_files]
            elif category == "extracted" and old_cache_dirs:
                files = [path for path in files if not _is_inside_any(path, old_cache_dirs)]
            for group in _duplicate_groups(files):
                keeper = max(group, key=_keeper_key)
                digest = sha256_file(keeper)
                summary["duplicate_groups"] += 1
                for duplicate in group:
                    if duplicate == keeper:
                        continue
                    per_tender.append((category, duplicate, keeper, digest, "exact_duplicate"))
        summary["tenders_scanned"] += 1
        if not per_tender:
            continue
        trash_dir = _trash_batch(root, tid)
        planned_rows: list[dict[str, Any]] = []
        name_map: dict[str, str] = {}
        for category, duplicate, keeper, digest, reason in per_tender:
            size = _path_size(duplicate)
            destination = trash_dir / category / duplicate.relative_to(root / category / tid)
            row = {
                "source": str(duplicate.relative_to(root)),
                "trash": str(destination.relative_to(root)),
                "keeper": str(keeper.relative_to(root)),
                "sha256": digest,
                "size_bytes": size,
                "reason": reason,
            }
            planned_rows.append(row)
            if category == "downloads" and duplicate.parent == root / "downloads" / tid and keeper.parent == duplicate.parent:
                name_map[duplicate.name] = keeper.name
        if dry_run:
            summary["details"].extend(planned_rows)
            summary["files_moved"] += len(planned_rows)
            summary["bytes_moved"] += sum(int(row["size_bytes"]) for row in planned_rows)
            continue
        log_path = root / "downloads" / tid / "download_log.json"
        original_log = log_path.read_bytes() if log_path.is_file() else None
        moved: list[tuple[Path, Path]] = []
        manifest_rows: list[dict[str, Any]] = []
        try:
            for plan, (_, duplicate, _, _, _) in zip(planned_rows, per_tender):
                destination = _move_recoverably(duplicate, root / str(plan["trash"]), data_dir=root)
                moved.append((duplicate, destination))
                actual = dict(plan)
                actual["trash"] = str(destination.relative_to(root))
                manifest_rows.append(actual)
            if name_map and log_path.is_file():
                rows = _read_log(log_path)
                for row in rows:
                    saved_name = Path(str(row.get("saved_name") or row.get("saved_path") or "")).name
                    keeper_name = name_map.get(saved_name)
                    if not keeper_name:
                        continue
                    row["deduplicated_from"] = saved_name
                    row["saved_name"] = keeper_name
                    row["saved_path"] = str(root / "downloads" / tid / keeper_name)
                    row["sha256"] = sha256_file(root / "downloads" / tid / keeper_name)
                _atomic_json_write(log_path, rows)
            _atomic_json_write(
                trash_dir / "cleanup_manifest.json",
                {
                    "tender_id": tid,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "moved": manifest_rows,
                },
            )
        except Exception as error:
            for source, destination in reversed(moved):
                try:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists() and not source.exists():
                        shutil.move(str(destination), str(source))
                except Exception:
                    pass
            if original_log is not None:
                try:
                    log_path.write_bytes(original_log)
                except OSError:
                    pass
            summary["errors"].append({"tender_id": tid, "error": f"{type(error).__name__}: {error}"})
            continue
        summary["details"].extend(manifest_rows)
        summary["files_moved"] += len(manifest_rows)
        summary["bytes_moved"] += sum(int(row["size_bytes"]) for row in manifest_rows)
        summary["trash_batches"].append(str(trash_dir.relative_to(root)))
    return summary
