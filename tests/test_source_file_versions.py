from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import os
import shutil

from autobot.source_file_versions import cleanup_existing_source_duplicates, store_downloaded_source_file


TENDER_ID = "0171200001926000664"


def _incoming(root: Path, payload: bytes) -> Path:
    folder = root / "downloads" / TENDER_ID
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ".autobot-incoming-test.bin"
    path.write_bytes(payload)
    return path


def test_identical_download_reuses_existing_file(tmp_path: Path) -> None:
    folder = tmp_path / "downloads" / TENDER_ID
    folder.mkdir(parents=True)
    existing = folder / "Документы.zip"
    existing.write_bytes(b"same")

    result = store_downloaded_source_file(
        _incoming(tmp_path, b"same"),
        tender_id=TENDER_ID,
        preferred_name="Документы.zip",
        data_dir=tmp_path,
    )

    assert result["action"] == "unchanged"
    assert result["path"] == existing
    assert [path.name for path in folder.iterdir()] == ["Документы.zip"]
    assert not (tmp_path / "trash").exists()


def test_changed_download_replaces_active_file_and_trashes_cache(tmp_path: Path) -> None:
    folder = tmp_path / "downloads" / TENDER_ID
    folder.mkdir(parents=True)
    old = folder / "Документы.zip"
    old.write_bytes(b"old")
    cache = tmp_path / "extracted" / TENDER_ID / "Документы"
    cache.mkdir(parents=True)
    (cache / "old.pdf").write_bytes(b"derived")

    result = store_downloaded_source_file(
        _incoming(tmp_path, b"new"),
        tender_id=TENDER_ID,
        preferred_name="Документы.zip",
        source_url="https://zakupki.gov.ru/file/1",
        data_dir=tmp_path,
        previous_log=[{"url": "https://zakupki.gov.ru/file/1", "saved_name": old.name}],
    )

    assert result["action"] == "replaced"
    assert (folder / "Документы.zip").read_bytes() == b"new"
    trash = tmp_path / result["trash_path"]
    assert (trash / "downloads" / "Документы.zip").read_bytes() == b"old"
    assert (trash / "extracted" / "Документы" / "old.pdf").read_bytes() == b"derived"
    assert (trash / "version_manifest.json").is_file()


def test_cleanup_moves_exact_duplicates_and_keeps_selection_copy(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads" / TENDER_ID
    downloads.mkdir(parents=True)
    (downloads / "doc.zip").write_bytes(b"archive")
    (downloads / "doc (2).zip").write_bytes(b"archive")
    selected = tmp_path / "extracted" / TENDER_ID / "ПСД ВЫБОРКА ДЛЯ ТОРГОВ" / "ЛСР.pdf"
    selected.parent.mkdir(parents=True)
    selected.write_bytes(b"pdf")
    repeated = tmp_path / "extracted" / TENDER_ID / "Полная ПСД" / "ЛСР.pdf"
    repeated.parent.mkdir(parents=True)
    repeated.write_bytes(b"pdf")

    preview = cleanup_existing_source_duplicates(TENDER_ID, data_dir=tmp_path, dry_run=True)
    assert preview["files_moved"] == 2
    assert repeated.exists()

    result = cleanup_existing_source_duplicates(TENDER_ID, data_dir=tmp_path)
    assert result["files_moved"] == 2
    assert selected.exists()
    assert not repeated.exists()
    assert len(list(downloads.glob("*.zip"))) == 1
    assert all((tmp_path / path / "cleanup_manifest.json").is_file() for path in result["trash_batches"])


def test_cleanup_rolls_back_one_tender_when_a_move_fails(tmp_path: Path) -> None:
    folder = tmp_path / "downloads" / TENDER_ID
    folder.mkdir(parents=True)
    for name in ("doc.zip", "doc (2).zip", "doc (3).zip"):
        (folder / name).write_bytes(b"same")
    real_move = shutil.move
    calls = 0

    def flaky_move(source: str, destination: str):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("test failure")
        return real_move(source, destination)

    with patch("autobot.source_file_versions.shutil.move", side_effect=flaky_move):
        result = cleanup_existing_source_duplicates(TENDER_ID, data_dir=tmp_path, include_extracted=False)

    assert result["files_moved"] == 0
    assert result["errors"]
    assert sorted(path.name for path in folder.glob("*.zip")) == ["doc (2).zip", "doc (3).zip", "doc.zip"]


def test_cleanup_keeps_latest_changed_named_version_and_moves_old_cache(tmp_path: Path) -> None:
    folder = tmp_path / "downloads" / TENDER_ID
    folder.mkdir(parents=True)
    old = folder / "estimate (3).zip"
    current = folder / "estimate (4).zip"
    old.write_bytes(b"old archive")
    current.write_bytes(b"new archive")
    os.utime(old, (100, 100))
    os.utime(current, (200, 200))
    old_cache = tmp_path / "extracted" / TENDER_ID / old.stem
    old_cache.mkdir(parents=True)
    (old_cache / "old.xlsx").write_bytes(b"old extracted")

    result = cleanup_existing_source_duplicates(TENDER_ID, data_dir=tmp_path)

    assert result["files_moved"] == 2
    assert current.is_file()
    assert not old.exists()
    assert not old_cache.exists()
    assert {row["reason"] for row in result["details"]} == {
        "older_named_version",
        "cache_of_older_named_version",
    }
