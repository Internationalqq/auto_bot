"""
Интерактивный анализ Excel-сметы для Telegram-бота.

Задача:
1) принять Excel;
2) показать найденные позиции;
3) по выбранной позиции найти похожие строки;
4) перед подсчётом показать, что будет объединено;
5) после подтверждения посчитать количество, объём, сумму, среднюю цену,
   строки/разделы сметы и возможные дубли.
"""

from __future__ import annotations

import html
import math
import re
import statistics
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pandas as pd

from autobot.market_analytics import COL_ITEM, COL_NAME, COL_QTY, COL_SUM, COL_UNIT, COL_UNIT_PRICE


ProgressCallback = Any


@dataclass
class EstimateRow:
    idx: int
    name: str
    unit: str = ""
    qty: float | None = None
    unit_price: float | None = None
    total: float | None = None
    item_no: str = ""
    basis_code: str = ""
    sheet: str = ""
    excel_row: int | None = None
    section: str = ""
    source: str = ""


@dataclass
class EstimateSession:
    file_path: Path
    rows: list[EstimateRow]
    catalogue: list[str]
    selected_query: str = ""
    candidates: list[EstimateRow] = field(default_factory=list)
    removed_candidate_ids: set[int] = field(default_factory=set)


STOP_WORDS = {
    "и",
    "или",
    "для",
    "при",
    "по",
    "на",
    "в",
    "во",
    "из",
    "с",
    "со",
    "до",
    "от",
    "без",
    "работ",
    "работы",
    "услуг",
    "услуги",
    "материал",
    "материалы",
    "устройство",
    "установка",
    "монтаж",
    "демонтаж",
}


def _clean_text(v: Any) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).replace("\xa0", " ").replace("\u202f", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return "" if s.casefold() == "nan" else s


def _num(v: Any) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (int, float)) and math.isfinite(float(v)):
        return float(v)
    s = _clean_text(v)
    if not s:
        return None
    s = re.sub(r"[^\d,.\-]", "", s).replace(",", ".")
    if not s or s in ("-", ".", "-."):
        return None
    try:
        out = float(s)
    except ValueError:
        return None
    return out if math.isfinite(out) else None


def _money(v: float | None) -> str:
    if v is None or not math.isfinite(float(v)):
        return "—"
    return f"{float(v):,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _amount(v: float | None) -> str:
    if v is None or not math.isfinite(float(v)):
        return "—"
    if abs(float(v) - round(float(v))) < 1e-9:
        return f"{float(v):,.0f}".replace(",", " ")
    return f"{float(v):,.4f}".replace(",", " ").rstrip("0").rstrip(".")


def _norm_name(s: str) -> str:
    t = _clean_text(s).casefold().replace("ё", "е")
    t = re.sub(r"[^\wа-яА-ЯёЁ]+", " ", t)
    t = re.sub(r"\b\d+(?:[,.]\d+)?\b", " ", t)
    toks = [x for x in t.split() if len(x) >= 3 and x not in STOP_WORDS]
    return " ".join(toks)


def _tokens(s: str) -> set[str]:
    return set(_norm_name(s).split())


def similarity(a: str, b: str) -> float:
    na = _norm_name(a)
    nb = _norm_name(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta = set(na.split())
    tb = set(nb.split())
    jacc = len(ta & tb) / max(1, len(ta | tb))
    containment = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return max(seq, 0.72 * jacc + 0.28 * containment, containment * 0.92)


def _same_unit_bonus(unit_a: str, unit_b: str) -> float:
    ua = _norm_unit(unit_a)
    ub = _norm_unit(unit_b)
    if not ua or not ub:
        return 0.0
    return 0.06 if ua == ub else -0.04


def _norm_unit(unit: str) -> str:
    u = _clean_text(unit).casefold().replace("²", "2").replace("³", "3")
    u = re.sub(r"\s+", "", u)
    replacements = {
        "штука": "шт",
        "штуки": "шт",
        "штук": "шт",
        "шт.": "шт",
        "м.кв.": "м2",
        "кв.м": "м2",
        "мкуб": "м3",
        "куб.м": "м3",
    }
    for old, new in replacements.items():
        u = u.replace(old, new)
    return u


def _read_standard_report(path: Path) -> list[EstimateRow]:
    try:
        df = pd.read_excel(path)
    except Exception:
        return []
    if COL_NAME not in df.columns:
        return []
    out: list[EstimateRow] = []
    for i, row in df.iterrows():
        name = _clean_text(row.get(COL_NAME))
        if len(name) < 4:
            continue
        total = _num(row.get(COL_SUM))
        qty = _num(row.get(COL_QTY))
        unit_price = _num(row.get(COL_UNIT_PRICE))
        out.append(
            EstimateRow(
                idx=len(out) + 1,
                name=name,
                unit=_clean_text(row.get(COL_UNIT)),
                qty=qty,
                unit_price=unit_price,
                total=total,
                item_no=_clean_text(row.get(COL_ITEM)),
                basis_code=_clean_text(row.get("basis_code") or row.get("Код") or row.get("Шифр") or row.get("Обоснование") or row.get("Норматив")),
                sheet="Отчёт",
                excel_row=int(i) + 2,
                section="",
                source="standard",
            )
        )
    return _dedupe_rows(out)


def _read_via_project_parser(path: Path) -> list[EstimateRow]:
    try:
        from autobot.main import Tender, _build_tender_clean_df, extract_rows_from_excel
    except Exception:
        return []
    tender = Tender(
        tender_id="uploaded",
        title="Загруженная Excel-смета",
        url="",
        region="",
        stage="",
        price_rub=None,
        publish_date="",
    )
    raw_rows = extract_rows_from_excel(path, tender)
    if not raw_rows:
        return []
    try:
        clean = _build_tender_clean_df(raw_rows)
    except Exception:
        return []
    out: list[EstimateRow] = []
    for _, row in clean.iterrows():
        name = _clean_text(row.get(COL_NAME))
        if len(name) < 4:
            continue
        item_no = _clean_text(row.get(COL_ITEM))
        out.append(
            EstimateRow(
                idx=len(out) + 1,
                name=name,
                unit=_clean_text(row.get(COL_UNIT)),
                qty=_num(row.get(COL_QTY)),
                unit_price=_num(row.get(COL_UNIT_PRICE)),
                total=_num(row.get(COL_SUM)),
                item_no=item_no,
                basis_code=_clean_text(row.get("basis_code") or row.get("Код") or row.get("Шифр") or row.get("Обоснование") or row.get("Норматив")),
                sheet=_clean_text(row.get("Лист")),
                excel_row=int(_num(row.get("Строка Excel")) or 0) or None,
                section=_clean_text(row.get("Раздел")),
                source="project-parser",
            )
        )
    return _dedupe_rows(out)


def _raw_sheets(path: Path) -> dict[str, pd.DataFrame]:
    try:
        return pd.read_excel(path, sheet_name=None, header=None)
    except Exception:
        return {}


def _read_generic_tables(path: Path) -> list[EstimateRow]:
    sheets = _raw_sheets(path)
    rows: list[EstimateRow] = []
    for sheet_name, df in sheets.items():
        if df.empty:
            continue
        header_idx = _guess_header_idx(df)
        if header_idx is None:
            continue
        header = [_clean_text(x).casefold() for x in df.iloc[header_idx].tolist()]
        name_col = _find_col(header, ("наименование", "работ", "услуг", "материал", "позиция"))
        unit_col = _find_col(header, ("ед", "изм"))
        qty_col = _find_col(header, ("кол", "объем", "объём"))
        total_col = _find_col(header, ("сумма", "стоимость", "всего"))
        price_col = _find_col(header, ("цена", "ед"))
        item_col = _find_col(header, ("№", "п/п", "номер"))
        if name_col is None:
            continue
        section = ""
        for ridx in range(header_idx + 1, len(df)):
            vals = df.iloc[ridx].tolist()
            row_text = " ".join(_clean_text(x) for x in vals if _clean_text(x))
            low = row_text.casefold()
            if "раздел" in low and 4 <= len(row_text) <= 220:
                section = row_text[:220]
                continue
            name = _clean_text(vals[name_col] if name_col < len(vals) else "")
            if len(name) < 4 or any(x in name.casefold() for x in ("итого", "всего", "ндс")):
                continue
            total = _num(vals[total_col]) if total_col is not None and total_col < len(vals) else None
            qty = _num(vals[qty_col]) if qty_col is not None and qty_col < len(vals) else None
            unit = _clean_text(vals[unit_col]) if unit_col is not None and unit_col < len(vals) else ""
            unit_price = _num(vals[price_col]) if price_col is not None and price_col < len(vals) else None
            if total is None and unit_price is not None and qty is not None:
                total = unit_price * qty
            if total is None and qty is None:
                continue
            rows.append(
                EstimateRow(
                    idx=len(rows) + 1,
                    name=name,
                    unit=unit,
                    qty=qty,
                    unit_price=unit_price,
                    total=total,
                    item_no=_clean_text(vals[item_col]) if item_col is not None and item_col < len(vals) else "",
                    sheet=str(sheet_name),
                    excel_row=ridx + 1,
                    section=section,
                    source="generic",
                )
            )
    return _dedupe_rows(rows)


def _guess_header_idx(df: pd.DataFrame) -> int | None:
    max_rows = min(len(df), 40)
    for ridx in range(max_rows):
        vals = [_clean_text(x).casefold() for x in df.iloc[ridx].tolist()]
        joined = " ".join(vals)
        if ("наименование" in joined or "работ" in joined or "услуг" in joined) and (
            "сумма" in joined or "стоимость" in joined or "кол" in joined
        ):
            return ridx
    return None


def _find_col(header: list[str], hints: tuple[str, ...]) -> int | None:
    best: tuple[int, int] | None = None
    for i, h in enumerate(header):
        if not h:
            continue
        score = sum(1 for x in hints if x in h)
        if score <= 0:
            continue
        if best is None or score > best[0]:
            best = (score, i)
    return best[1] if best else None


def _dedupe_rows(rows: list[EstimateRow]) -> list[EstimateRow]:
    seen: set[tuple] = set()
    out: list[EstimateRow] = []
    for row in rows:
        key = (
            _norm_name(row.name),
            _norm_unit(row.unit),
            round(float(row.qty or 0), 6),
            round(float(row.total or 0), 2),
            row.sheet,
            row.excel_row,
        )
        if key in seen:
            continue
        seen.add(key)
        row.idx = len(out) + 1
        out.append(row)
    return out


def _progress(cb: ProgressCallback | None, percent: int, stage: str, detail: str = "") -> None:
    if cb is None:
        return
    cb(int(percent), str(stage or "").strip(), str(detail or "").strip())


def load_estimate_session(path: Path, *, progress_cb: ProgressCallback | None = None) -> EstimateSession:
    _progress(progress_cb, 8, "Открываю Excel", f"Файл: {Path(path).name}")
    rows = _read_standard_report(path)
    if rows:
        _progress(progress_cb, 38, "Прочитан готовый отчёт", f"Найдено строк: {len(rows)}")
    if not rows:
        _progress(progress_cb, 28, "Пробую проектный парсер", "Ищу строки сметы в структуре ЛСР")
        rows = _read_via_project_parser(path)
        if rows:
            _progress(progress_cb, 64, "Проектный парсер завершён", f"Найдено строк: {len(rows)}")
    if not rows:
        _progress(progress_cb, 52, "Пробую универсальный разбор", "Ищу таблицы по заголовкам и суммам")
        rows = _read_generic_tables(path)
        if rows:
            _progress(progress_cb, 78, "Универсальный разбор завершён", f"Найдено строк: {len(rows)}")
    if not rows:
        _progress(progress_cb, 100, "Ошибка разбора", "Подходящих строк сметы не найдено")
        raise ValueError("Не удалось найти строки сметы в Excel. Нужны колонки/структура с названием, количеством и суммой.")
    _progress(progress_cb, 88, "Формирую каталог позиций", "Собираю список работ, услуг, товаров и материалов")
    catalogue = build_catalogue(rows)
    _progress(progress_cb, 96, "Подготовка завершена", f"Строк: {len(rows)} · позиций: {len(catalogue)}")
    return EstimateSession(file_path=Path(path), rows=rows, catalogue=catalogue)


def build_catalogue(rows: list[EstimateRow], *, limit: int = 10000) -> list[str]:
    best: dict[str, tuple[str, int]] = {}
    for row in rows:
        key = _norm_name(row.name)
        if not key:
            continue
        cur = best.get(key)
        if cur is None or len(row.name) < len(cur[0]):
            best[key] = (row.name, 1)
        else:
            best[key] = (cur[0], cur[1] + 1)
    vals = sorted(best.values(), key=lambda x: (-x[1], x[0].casefold()))
    return [v[0] for v in vals[:limit]]


def find_similar_rows(rows: list[EstimateRow], query: str, *, threshold: float = 0.58, max_rows: int = 80) -> list[EstimateRow]:
    q = _clean_text(query)
    if not q:
        return []
    # Если выбрали номер из исходных строк — берём название этой строки как запрос.
    if q.isdigit():
        n = int(q)
        for row in rows:
            if row.idx == n:
                q = row.name
                break
    scored: list[tuple[float, EstimateRow]] = []
    q_unit = ""
    for row in rows:
        score = similarity(q, row.name) + _same_unit_bonus(q_unit, row.unit)
        # точное вхождение тоже учитываем, особенно для коротких материалов
        nq = _norm_name(q)
        nr = _norm_name(row.name)
        if nq and (nq in nr or nr in nq):
            score = max(score, 0.86)
        if score >= threshold:
            scored.append((score, row))
    scored.sort(key=lambda x: (-x[0], x[1].idx))
    return [r for _, r in scored[:max_rows]]


def select_candidates(session: EstimateSession, query_or_number: str) -> list[EstimateRow]:
    q = _clean_text(query_or_number)
    if q.isdigit():
        n = int(q)
        if 1 <= n <= len(session.catalogue):
            q = session.catalogue[n - 1]
    session.selected_query = q
    session.candidates = find_similar_rows(session.rows, q)
    session.removed_candidate_ids = set()
    return session.candidates


def current_candidates(session: EstimateSession) -> list[EstimateRow]:
    return [r for r in session.candidates if r.idx not in session.removed_candidate_ids]


def remove_candidates(session: EstimateSession, numbers: list[int]) -> None:
    for n in numbers:
        if 1 <= n <= len(session.candidates):
            session.removed_candidate_ids.add(session.candidates[n - 1].idx)


def format_catalogue_html(session: EstimateSession, *, page: int = 1, page_size: int = 40) -> str:
    total = len(session.catalogue)
    page = max(1, page)
    start = (page - 1) * page_size
    end = min(total, start + page_size)
    lines = [
        "📊 <b>Excel-смета загружена.</b>",
        f"Найдено строк сметы: <b>{len(session.rows)}</b>",
        f"Уникальных позиций для выбора: <b>{total}</b>",
        "",
        f"<b>Позиции {start + 1}–{end} из {total}</b>",
    ]
    for i, name in enumerate(session.catalogue[start:end], start=start + 1):
        lines.append(f"{i}. {html.escape(name[:140])}")
    lines.extend(
        [
            "",
            "Напишите номер или текст позиции, например:",
            "<code>12</code>",
            "<code>бетон м300</code>",
            "",
            "Если список длинный: <code>список 2</code>",
        ]
    )
    return "\n".join(lines)


def format_candidates_preview_html(session: EstimateSession, *, page: int = 1, page_size: int = 20) -> str:
    cand = current_candidates(session)
    if not session.candidates:
        return (
            f"По запросу <b>{html.escape(session.selected_query)}</b> похожих строк не найдено.\n"
            "Попробуйте написать позицию иначе."
        )
    total_suggested = len(session.candidates)
    page = max(1, page)
    start = (page - 1) * page_size
    end = min(total_suggested, start + page_size)
    lines = [
        f"🔎 <b>Будут объединены строки по:</b> {html.escape(session.selected_query)}",
        f"Найдено похожих строк: <b>{len(cand)}</b> из {len(session.candidates)} предложенных.",
        "",
        f"<b>Проверьте перед подсчётом — строки {start + 1}–{end} из {total_suggested}:</b>",
    ]
    for i, row in enumerate(session.candidates[start:end], start=start + 1):
        removed = row.idx in session.removed_candidate_ids
        mark = "❌" if removed else "✅"
        where = _where(row)
        lines.append(
            f"{mark} {i}. {html.escape(row.name[:130])}\n"
            f"   ед.: {html.escape(row.unit or '—')} · кол-во: {_amount(row.qty)} · сумма: {_money(row.total)}"
            + (f"\n   {html.escape(where)}" if where else "")
        )
    if end < total_suggested:
        lines.append(f"Чтобы увидеть дальше: <code>строки {page + 1}</code>.")
    if page > 1:
        lines.append(f"Назад: <code>строки {page - 1}</code>.")
    lines.extend(
        [
            "",
            "Если всё верно — напишите <code>да</code> или <code>подтвердить</code>.",
            "Чтобы убрать лишние строки: <code>убрать 2,4,7</code>.",
        ]
    )
    return "\n".join(lines)


def _where(row: EstimateRow) -> str:
    parts: list[str] = []
    if row.sheet:
        parts.append(f"лист: {row.sheet}")
    if row.excel_row:
        parts.append(f"строка Excel: {row.excel_row}")
    if row.item_no:
        parts.append(f"№ п/п: {row.item_no}")
    if row.section:
        parts.append(f"раздел: {row.section}")
    return " · ".join(parts)


def format_summary_html(session: EstimateSession) -> str:
    rows = current_candidates(session)
    if not rows:
        return "После исключений не осталось строк для подсчёта."
    totals = [float(r.total) for r in rows if r.total is not None and math.isfinite(float(r.total))]
    qty_by_unit: dict[str, float] = {}
    unit_price_values: list[float] = []
    duplicate_hints: list[str] = []
    seen_dup: dict[tuple[str, str, float, float], int] = {}
    for row in rows:
        unit = row.unit or "без ед."
        if row.qty is not None and math.isfinite(float(row.qty)):
            qty_by_unit[unit] = qty_by_unit.get(unit, 0.0) + float(row.qty)
        if row.unit_price is not None and math.isfinite(float(row.unit_price)) and row.unit_price > 0:
            unit_price_values.append(float(row.unit_price))
        key = (_norm_name(row.name), _norm_unit(row.unit), round(float(row.qty or 0), 6), round(float(row.total or 0), 2))
        seen_dup[key] = seen_dup.get(key, 0) + 1
    for key, cnt in seen_dup.items():
        if cnt > 1:
            duplicate_hints.append(f"возможный дубль ×{cnt}: {key[0][:80]}")

    total_sum = sum(totals) if totals else None
    if unit_price_values:
        avg_price = statistics.mean(unit_price_values)
    elif total_sum is not None and len(qty_by_unit) == 1:
        qty_total = next(iter(qty_by_unit.values()))
        avg_price = total_sum / qty_total if qty_total else None
    else:
        avg_price = None

    qty_lines = [f"• {html.escape(unit)}: <b>{_amount(qty)}</b>" for unit, qty in sorted(qty_by_unit.items())]
    if not qty_lines:
        qty_lines = ["• количество/объём не распознан."]

    where_lines = []
    for row in rows:
        where = _where(row)
        where_lines.append(f"• {html.escape(where or row.name[:120])}")

    lines = [
        f"✅ <b>Сводка по позиции:</b> {html.escape(session.selected_query)}",
        "",
        "<b>Все найденные совпадения</b>",
    ]
    for i, row in enumerate(rows, start=1):
        lines.append(f"{i}. {html.escape(row.name[:150])}")
    lines.extend(
        [
            "",
            f"<b>Количество строк:</b> {len(rows)}",
            "<b>Общее количество / объём:</b>",
            "\n".join(qty_lines),
            f"<b>Общая стоимость:</b> {_money(total_sum)}",
            f"<b>Средняя цена:</b> {_money(avg_price)}",
            "",
            "<b>Где встречается</b>",
            "\n".join(where_lines[:40]),
        ]
    )
    if len(where_lines) > 40:
        lines.append(f"…и ещё {len(where_lines) - 40} строк.")
    lines.append("")
    if duplicate_hints:
        lines.append("<b>Возможные дубли</b>")
        lines.extend("• " + html.escape(x) for x in duplicate_hints[:10])
    else:
        lines.append("<b>Возможные дубли:</b> явных дублей среди объединённых строк не найдено.")
    return "\n".join(lines)


def parse_remove_numbers(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", text or "") if int(x) > 0]
