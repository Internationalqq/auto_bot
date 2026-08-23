"""Safe listing and previews for tender files downloaded from EIS."""

from __future__ import annotations

import base64
import io
import json
import re
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

import pandas as pd

from autobot.paths import DATA_DIR


DOWNLOADS_DIR = DATA_DIR / "downloads"
_HIDDEN_FILES = {"download_log.json", "desktop.ini"}
_TEXT_EXTENSIONS = {".txt", ".log", ".json", ".xml", ".csv", ".html", ".htm", ".rtf"}
_EXCEL_EXTENSIONS = {".xlsx", ".xls", ".xlsm"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_TEXT_EXTENSIONS.add(".svg")
_ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}
_MAX_ARCHIVE_DEPTH = 3
_MAX_ARCHIVE_MEMBER_BYTES = 256 * 1024 * 1024


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _mojibake_score(value: str) -> int:
    suspicious = ("Ð", "Ñ", "�", "Р°", "Р±", "РІ", "Рґ", "Рµ", "РЅ", "Рѕ", "С‚", "СЂ", "СЏ")
    return sum(value.count(marker) for marker in suspicious)


def _filename_quality(value: str) -> int:
    """Prefer readable Cyrillic and reject control/box-drawing mojibake."""

    cyrillic = sum("\u0400" <= char <= "\u04ff" for char in value)
    controls = sum(unicodedata.category(char) in {"Cc", "Cs", "Co"} for char in value)
    odd = sum(char in "ƒΓ∞∩╨╤╬╦╚╔╠╩╟╒╓╘╙╜╛" for char in value)
    return cyrillic * 5 - controls * 40 - odd * 6 - _mojibake_score(value) * 15 - value.count("�") * 50


def repair_filename(value: Any) -> str:
    """Repair UTF-8/header mojibake and old ZIP CP866 names used by EIS."""

    # Do not normalize whitespace before decoding: byte 0xA0 may be part of
    # a UTF-8 sequence that was incorrectly decoded as a single-byte charset.
    text = str(value or "").strip()
    if not text:
        return "Документ"

    candidates = {text}
    frontier = {text}
    transforms = (
        ("latin1", "utf-8"),
        ("cp1251", "utf-8"),
        ("cp1252", "utf-8"),
        # ZIP without the UTF-8 flag is decoded by Python as CP437. Russian
        # EIS archives often contain those exact bytes in DOS CP866.
        ("cp437", "cp866"),
    )
    for _ in range(2):
        next_frontier: set[str] = set()
        for current in frontier:
            for source_encoding, target_encoding in transforms:
                try:
                    candidate = current.encode(source_encoding).decode(target_encoding)
                except (UnicodeEncodeError, UnicodeDecodeError):
                    continue
                if candidate not in candidates:
                    candidates.add(candidate)
                    next_frontier.add(candidate)
        frontier = next_frontier
        if not frontier:
            break

    best = max(candidates, key=lambda candidate: (_filename_quality(candidate), -len(candidate)))
    return _clean(best)


def format_file_size(size: int) -> str:
    value = max(0, int(size or 0))
    units = ("Б", "КБ", "МБ", "ГБ")
    amount = float(value)
    for index, unit in enumerate(units):
        if amount < 1024 or index == len(units) - 1:
            if index == 0:
                return f"{int(amount)} {unit}"
            digits = 0 if amount >= 100 else 1
            return f"{amount:.{digits}f}".replace(".", ",") + f" {unit}"
        amount /= 1024
    return f"{value} Б"


def make_file_token(filename: str) -> str:
    return base64.urlsafe_b64encode(filename.encode("utf-8")).decode("ascii").rstrip("=")


def make_archive_member_token(member_chain: list[str]) -> str:
    payload = json.dumps(member_chain, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_file_token(token: str) -> str:
    raw = str(token or "")
    if not raw or len(raw) > 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        raise ValueError("Некорректный идентификатор файла")
    padding = "=" * (-len(raw) % 4)
    try:
        name = base64.urlsafe_b64decode(raw + padding).decode("utf-8")
    except Exception as exc:
        raise ValueError("Некорректный идентификатор файла") from exc
    if not name or name in {".", ".."} or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("Некорректное имя файла")
    return name


def _decode_archive_member_token(token: str) -> list[str]:
    raw = str(token or "")
    if not raw or len(raw) > 16_384 or not re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        raise ValueError("Некорректный идентификатор файла в архиве")
    padding = "=" * (-len(raw) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + padding).decode("utf-8"))
    except Exception as exc:
        raise ValueError("Некорректный идентификатор файла в архиве") from exc
    if not isinstance(payload, list) or not 1 <= len(payload) <= _MAX_ARCHIVE_DEPTH:
        raise ValueError("Некорректный путь файла в архиве")
    result: list[str] = []
    for raw_name in payload:
        name = str(raw_name or "")
        normalized = name.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        if (
            not name
            or len(name) > 4096
            or "\x00" in name
            or normalized.startswith("/")
            or ".." in parts
        ):
            raise ValueError("Некорректный путь файла в архиве")
        result.append(name)
    return result


def _tender_dir(tender_id: str) -> Path:
    tid = str(tender_id or "").strip()
    if not re.fullmatch(r"\d{8,25}", tid):
        raise ValueError("Некорректный номер тендера")
    return DOWNLOADS_DIR / tid


def resolve_tender_source_file(tender_id: str, token: str) -> Path:
    root = _tender_dir(tender_id).resolve()
    name = _decode_file_token(token)
    if name.casefold() in _HIDDEN_FILES:
        raise FileNotFoundError(name)
    candidate = (root / name).resolve()
    if candidate.parent != root or not candidate.is_file() or candidate.is_symlink():
        raise FileNotFoundError(name)
    return candidate


def _read_download_log(folder: Path) -> dict[str, dict[str, Any]]:
    path = folder / "download_log.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    result: dict[str, dict[str, Any]] = {}
    if isinstance(payload, list):
        for row in payload:
            if not isinstance(row, dict) or str(row.get("status") or "").casefold() != "ok":
                continue
            saved_name = Path(str(row.get("saved_name") or row.get("saved_path") or "")).name
            if saved_name:
                result[saved_name] = row
    return result


def _strip_copy_suffix(filename: str) -> str:
    return re.sub(r"\s+\(\d+\)(?=\.[^.]+$)", "", filename).strip()


def _file_kind(filename: str) -> tuple[str, str, bool]:
    extension = Path(filename).suffix.casefold()
    if extension == ".pdf":
        return "pdf", "PDF", True
    if extension in _EXCEL_EXTENSIONS:
        return "excel", "Excel", True
    if extension == ".docx":
        return "word", "Word", True
    if extension in {".doc", ".odt"}:
        return "word", "Word", False
    if extension in _IMAGE_EXTENSIONS:
        return "image", "Изображение", True
    if extension in _TEXT_EXTENSIONS:
        label = "Страница ЕИС" if extension in {".html", ".htm"} else "Текст / данные"
        return "text", label, True
    if extension in _ARCHIVE_EXTENSIONS:
        return "archive", "Архив", extension in {".zip", ".rar"}
    return "other", extension.lstrip(".").upper() or "Файл", False


def list_tender_source_files(tender_id: str) -> dict[str, Any]:
    folder = _tender_dir(tender_id)
    if not folder.is_dir():
        return {"files": [], "count": 0, "physical_count": 0, "total_size": 0, "total_size_fmt": "0 Б", "updated": ""}
    log_by_name = _read_download_log(folder)
    grouped: dict[str, dict[str, Any]] = {}
    physical_count = 0
    physical_size = 0
    latest_timestamp = 0.0
    for path in folder.iterdir():
        if not path.is_file() or path.is_symlink() or path.name.casefold() in _HIDDEN_FILES:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        physical_count += 1
        physical_size += stat.st_size
        latest_timestamp = max(latest_timestamp, stat.st_mtime)
        log_row = log_by_name.get(path.name, {})
        raw_display = str(log_row.get("original_name") or path.name)
        display_name = repair_filename(_strip_copy_suffix(raw_display))
        kind, type_label, can_preview = _file_kind(display_name)
        key = display_name.casefold()
        item = {
            "token": make_file_token(path.name),
            "saved_name": path.name,
            "name": display_name,
            "extension": Path(display_name).suffix.lstrip(".").upper() or "ФАЙЛ",
            "kind": kind,
            "type_label": type_label,
            "can_preview": can_preview,
            "size": int(stat.st_size),
            "size_fmt": format_file_size(stat.st_size),
            "updated": datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y %H:%M"),
            "timestamp": stat.st_mtime,
            "copies": 1,
            "source_url": str(log_row.get("url") or "").strip(),
        }
        previous = grouped.get(key)
        if previous is not None:
            largest = max(int(previous.get("size") or 0), int(stat.st_size), 1)
            # ZIP timestamps can change an archive by a couple of bytes. Treat
            # that as another download of the same EIS document, while keeping
            # genuinely different files with the same title as separate rows.
            if abs(int(previous.get("size") or 0) - int(stat.st_size)) / largest > 0.01:
                key = f"{key}\0{int(stat.st_size)}"
                previous = grouped.get(key)
        if previous is None:
            grouped[key] = item
        else:
            copies = int(previous.get("copies") or 1) + 1
            if item["timestamp"] > previous["timestamp"]:
                item["copies"] = copies
                grouped[key] = item
            else:
                previous["copies"] = copies
    files = sorted(grouped.values(), key=lambda row: (row["kind"] == "text", -row["timestamp"], row["name"].casefold()))
    updated = datetime.fromtimestamp(latest_timestamp).strftime("%d.%m.%Y %H:%M") if latest_timestamp else ""
    return {
        "files": files,
        "count": len(files),
        "physical_count": physical_count,
        "total_size": physical_size,
        "total_size_fmt": format_file_size(physical_size),
        "updated": updated,
        "preview_count": sum(1 for item in files if item["can_preview"]),
    }


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _excel_preview(source: Any) -> dict[str, Any]:
    sheets: list[dict[str, Any]] = []
    workbook = pd.ExcelFile(source)
    for sheet_name in workbook.sheet_names[:5]:
        frame = pd.read_excel(workbook, sheet_name=sheet_name, nrows=100)
        frame = frame.iloc[:, :30].fillna("")
        columns = [_clean(column) or f"Столбец {index + 1}" for index, column in enumerate(frame.columns)]
        rows = [[_clean(value) for value in row] for row in frame.astype(object).values.tolist()]
        sheets.append({"name": _clean(sheet_name), "columns": columns, "rows": rows})
    return {"kind": "excel", "sheets": sheets, "truncated": len(workbook.sheet_names) > 5}


def _docx_preview(source: Any) -> dict[str, Any]:
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(source) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    paragraphs: list[str] = []
    for paragraph in root.iter(namespace + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(namespace + "t")).strip()
        if text:
            paragraphs.append(text)
        if len(paragraphs) >= 1200:
            break
    return {"kind": "document", "paragraphs": paragraphs, "truncated": len(paragraphs) >= 1200}


def _is_estimate_filename(filename: str) -> bool:
    name = filename.casefold().replace("ё", "е")
    extension = Path(name).suffix
    if extension in {".sig", ".p7s"} or Path(name).name.startswith("~$"):
        return False
    keywords = (
        "лср",
        "сср",
        "смет",
        "локальн",
        "обоснование нмцк",
        "ведомость объем",
        "ведомость объём",
    )
    return any(keyword in name for keyword in keywords)


def _archive_member_model(raw_name: str, size: int, directory: bool, chain: list[str]) -> dict[str, Any]:
    display_path = repair_filename(raw_name).replace("\\", "/")
    short_name = PurePosixPath(display_path.rstrip("/")).name or display_path
    kind, type_label, can_preview = _file_kind(short_name)
    extension = Path(short_name).suffix.lstrip(".").upper() or ("ПАПКА" if directory else "ФАЙЛ")
    service = Path(short_name).suffix.casefold() in {".sig", ".p7s"} or short_name.startswith("~$")
    return {
        "raw_name": raw_name,
        "name": display_path,
        "short_name": short_name,
        "size": int(size or 0),
        "size_fmt": format_file_size(size),
        "directory": bool(directory),
        "kind": "folder" if directory else kind,
        "type_label": "Папка" if directory else type_label,
        "extension": extension,
        "can_preview": bool(can_preview and not directory),
        "token": "" if directory else make_archive_member_token(chain),
        "likely_estimate": bool(not directory and _is_estimate_filename(display_path)),
        "service": service,
        "children": [],
        "nested_error": "",
    }


def _collect_archive_entries(source: Any, source_name: str, chain_prefix: list[str], depth: int) -> tuple[list[dict[str, Any]], bool]:
    extension = Path(source_name).suffix.casefold()
    if hasattr(source, "seek"):
        source.seek(0)
    if extension == ".zip":
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            return _models_from_archive_infos(
                infos,
                lambda info: archive.read(info),
                lambda info: info.is_dir(),
                chain_prefix,
                depth,
            )
    if extension == ".rar":
        import rarfile

        with rarfile.RarFile(source) as archive:
            infos = archive.infolist()
            return _models_from_archive_infos(
                infos,
                lambda info: archive.read(info),
                lambda info: info.isdir(),
                chain_prefix,
                depth,
            )
    raise ValueError("Неподдерживаемый формат вложенного архива")


def _models_from_archive_infos(
    infos: list[Any],
    read_member: Any,
    is_directory: Any,
    chain_prefix: list[str],
    depth: int,
) -> tuple[list[dict[str, Any]], bool]:
    entries: list[dict[str, Any]] = []
    truncated = len(infos) > 500
    for info in infos[:500]:
        raw_name = str(info.filename)
        directory = bool(is_directory(info))
        size = int(getattr(info, "file_size", 0) or 0)
        chain = [*chain_prefix, raw_name]
        entry = _archive_member_model(raw_name, size, directory, chain)
        if (
            not directory
            and entry["kind"] == "archive"
            and depth < _MAX_ARCHIVE_DEPTH - 1
            and size <= _MAX_ARCHIVE_MEMBER_BYTES
        ):
            try:
                nested_data = read_member(info)
                children, child_truncated = _collect_archive_entries(
                    io.BytesIO(nested_data),
                    entry["short_name"],
                    chain,
                    depth + 1,
                )
                entry["children"] = children
                truncated = truncated or child_truncated
            except Exception as exc:
                entry["nested_error"] = f"Не удалось раскрыть вложенный архив ({type(exc).__name__})"
        entries.append(entry)
    return entries, truncated


def _flatten_archive_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for entry in entries:
        result.append(entry)
        result.extend(_flatten_archive_entries(entry.get("children") or []))
    return result


def _archive_preview(source: Any, source_name: str, chain_prefix: list[str] | None = None) -> dict[str, Any]:
    entries, truncated = _collect_archive_entries(source, source_name, list(chain_prefix or []), 0)
    flat = _flatten_archive_entries(entries)
    files = [entry for entry in flat if not entry["directory"]]
    estimates = [entry for entry in files if entry["likely_estimate"]]
    return {
        "kind": "archive",
        "entries": entries,
        "total": len(files),
        "top_total": sum(1 for entry in entries if not entry["directory"]),
        "estimate_entries": estimates,
        "estimate_count": len(estimates),
        "nested_count": sum(1 for entry in files if entry["kind"] == "archive" and entry["children"]),
        "truncated": truncated,
    }


def _read_one_archive_member(source: Any, source_name: str, raw_name: str) -> bytes:
    extension = Path(source_name).suffix.casefold()
    if hasattr(source, "seek"):
        source.seek(0)
    if extension == ".zip":
        with zipfile.ZipFile(source) as archive:
            info = next((item for item in archive.infolist() if item.filename == raw_name), None)
            if info is None or info.is_dir():
                raise FileNotFoundError(raw_name)
            if int(info.file_size or 0) > _MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError("Файл внутри архива слишком большой для просмотра")
            return archive.read(info)
    if extension == ".rar":
        import rarfile

        with rarfile.RarFile(source) as archive:
            info = next((item for item in archive.infolist() if item.filename == raw_name), None)
            if info is None or info.isdir():
                raise FileNotFoundError(raw_name)
            if int(info.file_size or 0) > _MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError("Файл внутри архива слишком большой для просмотра")
            return archive.read(info)
    raise ValueError("Неподдерживаемый формат вложенного архива")


def read_archive_member(path: Path, member_token: str) -> dict[str, Any]:
    chain = _decode_archive_member_token(member_token)
    source: Any = path
    source_name = path.name
    data = b""
    for raw_name in chain:
        data = _read_one_archive_member(source, source_name, raw_name)
        source = io.BytesIO(data)
        source_name = raw_name
    display_path = repair_filename(chain[-1]).replace("\\", "/")
    name = PurePosixPath(display_path).name or display_path
    kind, type_label, can_preview = _file_kind(name)
    return {
        "data": data,
        "chain": chain,
        "token": member_token,
        "name": name,
        "full_name": display_path,
        "extension": Path(name).suffix.lstrip(".").upper() or "ФАЙЛ",
        "kind": kind,
        "type_label": type_label,
        "can_preview": can_preview,
        "size": len(data),
        "size_fmt": format_file_size(len(data)),
        "updated": "из архива",
    }


def build_source_bytes_preview(data: bytes, filename: str, chain_prefix: list[str] | None = None) -> dict[str, Any]:
    extension = Path(filename).suffix.casefold()
    if extension in _EXCEL_EXTENSIONS:
        return _excel_preview(io.BytesIO(data))
    if extension == ".docx":
        return _docx_preview(io.BytesIO(data))
    if extension in _TEXT_EXTENSIONS:
        limit = 2 * 1024 * 1024
        return {"kind": "text", "text": _decode_text(data[:limit]), "truncated": len(data) > limit}
    if extension in {".zip", ".rar"}:
        return _archive_preview(io.BytesIO(data), filename, chain_prefix)
    return {"kind": "unavailable", "message": "Этот формат браузер не показывает. Файл можно скачать и открыть на компьютере."}


def build_source_file_preview(path: Path) -> dict[str, Any]:
    extension = path.suffix.casefold()
    if extension in _EXCEL_EXTENSIONS:
        return _excel_preview(path)
    if extension == ".docx":
        return _docx_preview(path)
    if extension in _TEXT_EXTENSIONS:
        limit = 2 * 1024 * 1024
        data = path.read_bytes()[:limit]
        return {"kind": "text", "text": _decode_text(data), "truncated": path.stat().st_size > limit}
    if extension in {".zip", ".rar"}:
        return _archive_preview(path, path.name)
    return {"kind": "unavailable", "message": "Этот формат браузер не показывает. Файл можно скачать и открыть на компьютере."}
