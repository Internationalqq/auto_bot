"""Convert text PDFs and scanned estimate tables to pandas-compatible rows."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any, Iterable, Sequence


class PdfEstimateAdapterError(RuntimeError):
    pass


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _rectangular(rows: Iterable[Sequence[Any]]) -> list[list[str]]:
    result = [[_clean(cell) for cell in row] for row in rows]
    result = [row for row in result if any(row)]
    if not result:
        return []
    width = max(len(row) for row in result)
    return [row + [""] * (width - len(row)) for row in result]


def _score(rows: Sequence[Sequence[Any]]) -> tuple[int, int, int]:
    rows = _rectangular(rows)
    return (
        sum(bool(cell) for row in rows for cell in row),
        len(rows),
        max((len(row) for row in rows), default=0),
    )


def _centers(indexes: Iterable[int], *, max_gap: int = 4) -> list[int]:
    indexes = sorted(set(map(int, indexes)))
    if not indexes:
        return []
    groups = [[indexes[0]]]
    for index in indexes[1:]:
        if index - groups[-1][-1] <= max_gap:
            groups[-1].append(index)
        else:
            groups.append([index])
    return [round(sum(group) / len(group)) for group in groups]


def _is_main_estimate_code(value: str) -> bool:
    text = _clean(value).replace(" ", "")
    lowered = text.casefold()
    if len(text) < 8 or text.count("-") < 2 or not any(char.isdigit() for char in text):
        return False
    return not lowered.startswith(("пр/", "1-100", "2-100", "4-100", "91.", "01.", "08.", "11."))


def _is_unit(value: str) -> bool:
    text = _clean(value).casefold().replace(" ", "").replace("²", "2").replace("³", "3")
    return text in {
        "м", "м2", "м3", "m", "m2", "m3", "т", "кг", "kg", "шт", "pcs", "компл", "чел.-ч", "чел-ч", "маш.-ч", "маш-ч",
        "100м", "100м2", "100м3", "100шт",
    }


def _to_number(value: str) -> float | None:
    value = re.sub(r"[^0-9,.-]", "", _clean(value)).replace(",", ".")
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _number_in_column(words: Sequence[dict[str, object]], left: float, right: float) -> float | None:
    """Return one OCR number from a known table column.

    Tesseract may split a value such as ``5 537,22`` into two tokens, and can
    read the leading digit ``5`` as ``§``.  Joining only the tokens located in
    one physical column is safe and avoids mixing a price with a quantity.
    """
    tokens = [
        str(item["text"]).replace("§", "5")
        for item in sorted(words, key=lambda item: float(item["left"]))
        if left <= float(item["left"]) < right
        and (str(item["text"]).replace("§", "5").strip() or "").strip()
    ]
    return _to_number(" ".join(tokens)) if tokens else None


def _word_lines(words: Sequence[dict[str, object]]) -> list[tuple[float, str]]:
    """Reconstruct readable OCR lines for headings such as ``Раздел 2``."""
    if not words:
        return []
    heights = sorted(max(1.0, float(word.get("height") or 1)) for word in words)
    tolerance = max(12.0, heights[len(heights) // 2] * 0.72)
    grouped: list[list[dict[str, object]]] = []
    for word in sorted(words, key=lambda item: (float(item["top"]), float(item["left"]))):
        center = float(word["top"]) + float(word.get("height") or 0) / 2
        if grouped:
            previous_center = sum(
                float(item["top"]) + float(item.get("height") or 0) / 2
                for item in grouped[-1]
            ) / len(grouped[-1])
            if abs(center - previous_center) <= tolerance:
                grouped[-1].append(word)
                continue
        grouped.append([word])
    lines: list[tuple[float, str]] = []
    for group in grouped:
        group.sort(key=lambda item: float(item["left"]))
        center = sum(float(item["top"]) + float(item.get("height") or 0) / 2 for item in group) / len(group)
        text = _clean(" ".join(str(item["text"]) for item in group))
        if text:
            lines.append((center, text))
    return lines


def _clean_section_name(value: str, *, strip_total: bool = False) -> str:
    text = _clean(value).strip(" .:-–—")
    if strip_total:
        text = re.sub(r"\s+\d[\d\s]*[,.]\d{2}(?:\D.*)?$", "", text).strip(" .:-–—")
    if text and not re.search(r"[a-zа-яё]", text, flags=re.IGNORECASE):
        return ""
    return text[:240]


def _section_markers_from_words(
    words: Sequence[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    starts: list[dict[str, object]] = []
    ends: list[dict[str, object]] = []
    for center, text in _word_lines(words):
        end_match = re.search(
            r"\bвсего\s+по\s+разделу\s*[№n]?\s*(\d+)\s*(.*)$",
            text,
            flags=re.IGNORECASE,
        )
        if end_match:
            ends.append(
                {
                    "number": int(end_match.group(1)),
                    "title": _clean_section_name(end_match.group(2), strip_total=True),
                    "y": center,
                }
            )
            continue
        start_match = re.search(
            r"(?:^|\s)раздел\s*[№n]?\s*(\d+)\s*[.\-:–—]?\s*(.*)$",
            text,
            flags=re.IGNORECASE,
        )
        if start_match:
            starts.append(
                {
                    "number": int(start_match.group(1)),
                    "title": _clean_section_name(start_match.group(2)),
                    "y": center,
                }
            )
    return starts, ends


def _section_for_position(
    position_y: float,
    starts: Sequence[dict[str, object]],
    ends: Sequence[dict[str, object]],
) -> str:
    latest_start = max(
        (marker for marker in starts if float(marker["y"]) <= position_y),
        key=lambda marker: float(marker["y"]),
        default=None,
    )
    if latest_start is not None:
        number = int(latest_start["number"])
        matching_end = min(
            (
                marker for marker in ends
                if int(marker["number"]) == number and float(marker["y"]) >= float(latest_start["y"])
            ),
            key=lambda marker: float(marker["y"]),
            default=None,
        )
        if matching_end is None or float(matching_end["y"]) >= position_y:
            title = str(latest_start.get("title") or (matching_end or {}).get("title") or "").strip()
            return f"Раздел {number}" + (f". {title}" if title else "")
    future_end = min(
        (marker for marker in ends if float(marker["y"]) >= position_y),
        key=lambda marker: float(marker["y"]),
        default=None,
    )
    if future_end is None:
        return ""
    number = int(future_end["number"])
    title = str(future_end.get("title") or "").strip()
    return f"Раздел {number}" + (f". {title}" if title else "")


class PdfEstimateAdapter:
    """Extract a rectangular table from a PDF estimate.

    A PDF with a text layer is handled by pdfplumber.  A scan is rendered at
    300 DPI, deskewed, split into OpenCV-detected cells and OCRed by Tesseract.
    If grid lines are missing, the fallback reconstructs rows from OCR word
    coordinates.
    """

    def __init__(self, *, languages: str = "rus+eng", dpi: int = 300) -> None:
        self.languages = languages
        self.dpi = dpi

    def to_rows(self, source: Path | str | bytes) -> list[list[str]]:
        raw = source if isinstance(source, bytes) else Path(source).read_bytes()
        native = self._native(raw)
        if self._usable(native):
            return native
        rows = self._ocr(raw)
        if not self._usable(rows):
            raise PdfEstimateAdapterError(
                "Не удалось распознать таблицу в PDF. Проверьте качество скана и Tesseract OCR."
            )
        return rows

    def to_position_records(self, source: Path | str | bytes) -> list[dict[str, object]]:
        """Read primary estimate rows from a scan by OCR word coordinates.

        This deliberately does not rely on horizontal table borders: LSR PDFs
        often have page breaks and multi-line cells that merge when a grid is
        reconstructed. Primary position codes occupy the second column, while
        resource rows begin farther to the right, which makes them separable.
        """
        raw = source if isinstance(source, bytes) else Path(source).read_bytes()
        try:
            import cv2
            import fitz
            import numpy as np
            import pytesseract
        except ImportError as error:
            raise PdfEstimateAdapterError("Не установлены зависимости OCR для PDF") from error
        document = fitz.open(stream=raw, filetype="pdf")
        records: list[dict[str, object]] = []
        all_words: list[dict[str, float | str | int]] = []
        section_words: list[dict[str, float | str | int]] = []
        page_y_offset = 0.0
        try:
            for page_number, page in enumerate(document, start=1):
                zoom = self.dpi / 72
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                data = pytesseract.image_to_data(
                    image, lang=self.languages, config="--psm 11", output_type=pytesseract.Output.DICT
                )
                for index, raw_text in enumerate(data["text"]):
                    text = _clean(raw_text)
                    if not text:
                        continue
                    try:
                        confidence = float(data["conf"][index])
                    except (TypeError, ValueError):
                        confidence = -1
                    # Labels such as "Всего по позиции" are often faint in
                    # scans.  Their coordinates are validated later, so keep
                    # low-confidence words instead of discarding the total.
                    if confidence < 0:
                        continue
                    all_words.append(
                        {
                            "text": text,
                            "left": float(data["left"][index]),
                            # Treat the PDF as one vertical sheet.  A position
                            # may start before a page break and have its
                            # "Всего по позиции" line on the following page.
                            "top": page_y_offset + float(data["top"][index]),
                            "width": float(data["width"][index]),
                            "height": float(data["height"][index]),
                            "page": page_number,
                        }
                    )
                # Sparse PSM 11 is best at estimate rows, but it often loses
                # full-width headings.  A direct 150 DPI PSM 3 render is used
                # only for section boundaries and mapped back to the primary
                # OCR coordinate system.  Rendering directly matters here:
                # shrinking the 300 DPI raster made light headings disappear.
                section_pix = page.get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72), alpha=False)
                section_image = np.frombuffer(section_pix.samples, dtype=np.uint8).reshape(
                    section_pix.height, section_pix.width, section_pix.n
                )
                section_image = cv2.cvtColor(section_image, cv2.COLOR_RGB2BGR)
                section_data = pytesseract.image_to_data(
                    section_image, lang=self.languages, config="--psm 3", output_type=pytesseract.Output.DICT
                )
                section_scale = float(image.shape[1]) / max(1.0, float(section_image.shape[1]))
                for index, raw_text in enumerate(section_data["text"]):
                    text = _clean(raw_text)
                    if not text:
                        continue
                    try:
                        confidence = float(section_data["conf"][index])
                    except (TypeError, ValueError):
                        confidence = -1
                    if confidence < 0:
                        continue
                    section_words.append(
                        {
                            "text": text,
                            "left": float(section_data["left"][index]) * section_scale,
                            "top": page_y_offset + float(section_data["top"][index]) * section_scale,
                            "width": float(section_data["width"][index]) * section_scale,
                            "height": float(section_data["height"][index]) * section_scale,
                            "page": page_number,
                        }
                    )
                page_y_offset += float(image.shape[0]) + 120.0
            records = self._position_records_from_words(all_words, image.shape[1], section_words=section_words)
        except Exception as error:
            raise PdfEstimateAdapterError("Не удалось выделить позиции сметы из OCR PDF") from error
        finally:
            document.close()
        unique: dict[tuple[str, str], dict[str, object]] = {}
        for record in records:
            key = (str(record["code"]), str(record["name"]).casefold())
            unique.setdefault(key, record)
        return list(unique.values())

    @staticmethod
    def _position_records_from_words(
        words,
        page_width: int,
        *,
        section_words=None,
    ) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        section_starts, section_ends = _section_markers_from_words(section_words or words)
        code_column_limit = page_width * 0.18
        anchors = sorted(
            (
                item
                for item in words
                if float(item["left"]) <= code_column_limit and _is_main_estimate_code(str(item["text"]))
            ),
            key=lambda item: float(item["top"]) + float(item["height"]) / 2,
        )
        for word in words:
            code = str(word["text"])
            left = float(word["left"])
            if left > code_column_limit or not _is_main_estimate_code(code):
                continue
            center_y = float(word["top"]) + float(word["height"]) / 2
            same_line = sorted(
                (
                    other
                    for other in words
                    if abs((float(other["top"]) + float(other["height"]) / 2) - center_y) <= 35
                ),
                key=lambda item: float(item["left"]),
            )
            unit_word = next((item for item in same_line if float(item["left"]) > left and _is_unit(str(item["text"]))), None)
            code_index = same_line.index(word)
            value_column_left = page_width * 0.45
            quantity_column_right = page_width * 0.63
            unit_index = same_line.index(unit_word) if unit_word is not None else None
            title_end = unit_index if unit_index is not None else next(
                (index for index, item in enumerate(same_line) if index > code_index and float(item["left"]) >= value_column_left),
                len(same_line),
            )
            title_items = same_line[code_index + 1:title_end]
            if title_items and str(title_items[-1]["text"]) == "100":
                title_items = title_items[:-1]
            title = _clean(" ".join(str(item["text"]) for item in title_items))
            if len(title) < 4:
                continue
            unit = str(unit_word["text"]) if unit_word is not None else ""
            if unit_index is not None and unit_index > 0 and str(same_line[unit_index - 1]["text"]) == "100":
                unit = f"100 {unit}"
            # In the standard ЛСР layout the source quantity is column 7
            # ("всего с учетом коэффициентов"), not column 5.  The latter
            # often looks identical, but diverges whenever a coefficient is
            # present.  Its fixed x-range survives skew much better than a
            # search for the first number after the unit.
            qty = _number_in_column(same_line, page_width * 0.55, quantity_column_right)
            if qty is None or qty <= 0:
                qty = next(
                    (
                        _to_number(str(item["text"]))
                        for item in same_line[(unit_index + 1) if unit_index is not None else (code_index + 1):]
                        if float(item["left"]) >= value_column_left
                        and float(item["left"]) < quantity_column_right
                        and _to_number(str(item["text"])) is not None
                        and float(_to_number(str(item["text"])) or 0) > 0
                    ),
                    None,
                )
            if qty is None:
                # A quantity cell may be blank in a scan for a single item;
                # never let the following price column masquerade as quantity.
                qty = 1.0 if unit_index is not None else None
            if qty is None:
                continue
            # The last column holds the current total.  It is the most stable
            # price signal in a scan; the unit price is calculated from it.
            # When the total is unreadable, retain a directly OCRed price.
            total = _number_in_column(same_line, page_width * 0.90, page_width * 1.01)
            if total is None:
                # Complex work positions occupy several printed rows.  Their
                # total is on a separate "Всего по позиции" line before the
                # following position code, rather than next to the title.
                next_anchor_y = next(
                    (
                        float(item["top"]) + float(item["height"]) / 2
                        for item in anchors
                        if float(item["top"]) + float(item["height"]) / 2 > center_y + 35
                    ),
                    float("inf"),
                )
                total_lines = []
                for item in words:
                    item_y = float(item["top"]) + float(item["height"]) / 2
                    item_text = _clean(str(item["text"])).casefold()
                    if center_y + 35 < item_y < next_anchor_y - 8 and item_text.startswith(("всего", "итого")):
                        total_lines.append(item_y)
                for total_y in total_lines:
                    # In a scanned LSR the numeric total is usually printed
                    # just *above* the "Всего по позиции" text.  Do not use a
                    # wide horizontal band here: it also captures the total
                    # of the next position and makes two values look like one.
                    total_value_y = max(
                        (
                            float(item["top"]) + float(item["height"]) / 2
                            for item in words
                            if page_width * 0.90 <= float(item["left"]) < page_width * 1.01
                            and total_y - 80 <= float(item["top"]) + float(item["height"]) / 2 <= total_y + 6
                            and _to_number(str(item["text"]).replace("§", "5")) is not None
                        ),
                        default=None,
                    )
                    if total_value_y is None:
                        continue
                    total_line = [
                        item
                        for item in words
                        if abs((float(item["top"]) + float(item["height"]) / 2) - total_value_y) <= 12
                    ]
                    detected = _number_in_column(total_line, page_width * 0.90, page_width * 1.01)
                    if detected is not None:
                        total = detected
            unit_price = (total / qty) if total is not None and qty > 0 else _number_in_column(
                same_line, page_width * 0.63, page_width * 0.90
            )
            position = next(
                (
                    str(item["text"])
                    for item in same_line[:code_index]
                    if re.fullmatch(r"\d{1,3}(?:\.\d+)?", str(item["text"]))
                ),
                "",
            )
            records.append(
                {
                    "page": int(word.get("page") or 0),
                    "position": position,
                    "code": code,
                    "name": title,
                    "unit": unit,
                    "qty": qty,
                    "unit_price": unit_price,
                    "total": total,
                    "section": _section_for_position(center_y, section_starts, section_ends),
                }
            )
        return records

    @staticmethod
    def _usable(rows: Sequence[Sequence[str]]) -> bool:
        return len(rows) >= 2 and max((len(row) for row in rows), default=0) >= 2 and _score(rows)[0] >= 3

    def _native(self, raw: bytes) -> list[list[str]]:
        try:
            import pdfplumber
        except ImportError as error:
            raise PdfEstimateAdapterError("Не установлена библиотека pdfplumber") from error
        settings = {
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
            "snap_tolerance": 5,
            "join_tolerance": 5,
            "intersection_tolerance": 8,
        }
        rows: list[list[str]] = []
        try:
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                for page in pdf.pages:
                    table = max(page.extract_tables(table_settings=settings) or [], key=_score, default=[])
                    rows.extend(_rectangular(table))
        except Exception:
            return []
        return _rectangular(rows)

    def _ocr(self, raw: bytes) -> list[list[str]]:
        try:
            import cv2
            import fitz
            import numpy as np
            import pytesseract
        except ImportError as error:
            raise PdfEstimateAdapterError("Не установлены зависимости OCR для PDF") from error
        try:
            document = fitz.open(stream=raw, filetype="pdf")
        except Exception as error:
            raise PdfEstimateAdapterError("PDF поврежден или не читается") from error
        result: list[list[str]] = []
        try:
            for page in document:
                zoom = self.dpi / 72
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                image = self._deskew(image, cv2)
                rows = self._grid_rows(image, cv2, np, pytesseract)
                if not self._usable(rows):
                    rows = self._layout_rows(image, pytesseract)
                result.extend(rows)
        except PdfEstimateAdapterError:
            raise
        except Exception as error:
            raise PdfEstimateAdapterError(
                "Tesseract OCR не запущен. Установите tesseract-ocr и языки rus, eng."
            ) from error
        finally:
            document.close()
        return _rectangular(result)

    @staticmethod
    def _deskew(image, cv2):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        points = cv2.findNonZero(mask)
        if points is None or len(points) < 50:
            return image
        angle = cv2.minAreaRect(points)[-1]
        angle = -(90 + angle) if angle < -45 else -angle
        if abs(angle) < 0.15 or abs(angle) > 12:
            return image
        height, width = image.shape[:2]
        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1)
        return cv2.warpAffine(image, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

    def _grid_rows(self, image, cv2, np, pytesseract) -> list[list[str]]:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 11)
        height, width = binary.shape
        horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, width // 35), 1)))
        vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, height // 35))))
        h_lines = _centers(np.where(np.sum(horizontal > 0, axis=1) >= max(40, width * 0.12))[0])
        v_lines = _centers(np.where(np.sum(vertical > 0, axis=0) >= max(40, height * 0.12))[0])
        if len(h_lines) < 3 or len(v_lines) < 3:
            return []
        rows: list[list[str]] = []
        for top, bottom in zip(h_lines, h_lines[1:]):
            if bottom - top < 8:
                continue
            row: list[str] = []
            for left, right in zip(v_lines, v_lines[1:]):
                if right - left < 8:
                    row.append("")
                    continue
                margin_x, margin_y = max(3, (right - left) // 20), max(3, (bottom - top) // 12)
                crop = gray[top + margin_y:bottom - margin_y, left + margin_x:right - margin_x]
                text = pytesseract.image_to_string(crop, lang=self.languages, config="--psm 6") if crop.size else ""
                row.append(_clean(text))
            rows.append(row)
        return _rectangular(rows)

    def _layout_rows(self, image, pytesseract) -> list[list[str]]:
        data = pytesseract.image_to_data(
            image, lang=self.languages, config="--psm 6", output_type=pytesseract.Output.DICT
        )
        words = []
        for i, raw in enumerate(data["text"]):
            text = _clean(raw)
            if not text:
                continue
            try:
                confidence = float(data["conf"][i])
            except (TypeError, ValueError):
                confidence = -1
            if confidence >= 10:
                words.append((float(data["top"][i]), float(data["left"][i]), float(data["height"][i]), text))
        if not words:
            return []
        median_height = sorted(word[2] for word in words)[len(words) // 2]
        tolerance = max(10.0, median_height * 0.75)
        lines: list[list[tuple[float, float, float, str]]] = []
        for word in sorted(words):
            center = word[0] + word[2] / 2
            if lines and abs(center - sum(item[0] + item[2] / 2 for item in lines[-1]) / len(lines[-1])) <= tolerance:
                lines[-1].append(word)
            else:
                lines.append([word])
        rows = []
        split_gap = max(18.0, median_height * 1.7)
        for line in lines:
            line.sort(key=lambda item: item[1])
            cells, current, previous = [], [], line[0][1]
            for word in line:
                if current and word[1] - previous > split_gap:
                    cells.append(" ".join(current))
                    current = []
                current.append(word[3])
                previous = word[1] + max(1, len(word[3])) * median_height * 0.45
            if current:
                cells.append(" ".join(current))
            rows.append(cells)
        return _rectangular(rows)


def pdf_to_dataframe(path: Path | str):
    import pandas as pd

    return pd.DataFrame(PdfEstimateAdapter().to_rows(path))


def pdf_to_position_records(path: Path | str) -> list[dict[str, object]]:
    return PdfEstimateAdapter().to_position_records(path)
