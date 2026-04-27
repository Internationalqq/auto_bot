"""
Общая генерация промпта для рыночного анализа (Perplexity и др.).
Используется web_ui и telegram_notify без зависимости от Flask.
"""

from __future__ import annotations

import json
import math

import pandas as pd

from autobot.paths import DATA_DIR, REPO_ROOT

# Совместимость: раньше BASE_DIR был каталогом репозитория.
BASE_DIR = REPO_ROOT
REPORTS_DIR = DATA_DIR / "reports"
TENDERS_JSON = DATA_DIR / "tenders.json"


def load_tender_metadata() -> dict[str, dict]:
    if not TENDERS_JSON.exists():
        return {}
    try:
        data = json.loads(TENDERS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    meta: dict[str, dict] = {}
    for row in data:
        tid = str(row.get("tender_id", "")).strip()
        if not tid:
            continue
        meta[tid] = {
            "title": row.get("title") or f"Тендер {tid}",
            "region": row.get("region") or "Без региона",
            "url": (row.get("url") or "").strip(),
            "price_rub": row.get("price_rub"),
            "publish_date": (row.get("publish_date") or "").strip(),
            "stage": (row.get("stage") or "").strip(),
        }
    return meta


def _perplexity_positions_caption(has_data: bool, truncated: bool) -> str:
    if not has_data:
        return ""
    if truncated:
        return "(ниже выборка — самые крупные по сумме в отчёте)"
    return "(ниже все позиции из отчёта)"


def _fmt_rub_amount(v) -> str:
    if v is None:
        return "—"
    try:
        if isinstance(v, float) and math.isnan(v):
            return "—"
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    s = f"{x:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", " ")


def build_perplexity_prompt_for_tender(tender_id: str, max_rows: int) -> tuple[str, dict]:
    """
    Текст промпта для Perplexity и метаданные (строки/усечение).
    """
    meta = load_tender_metadata().get(tender_id, {})
    title = meta.get("title") or f"Тендер {tender_id}"
    region = meta.get("region") or "Без региона"
    url = meta.get("url") or ""
    price_rub = meta.get("price_rub")
    publish_date = meta.get("publish_date") or ""
    stage = meta.get("stage") or ""

    xlsx_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.xlsx"
    df = pd.DataFrame()
    if xlsx_path.exists():
        try:
            df = pd.read_excel(xlsx_path)
        except Exception:
            df = pd.DataFrame()

    expected_cols = {
        "Файл ЛСР",
        "№ п/п",
        "Название работы/услуги",
        "Ед. изм.",
        "Кол-во",
        "Объем",
        "Цена за ед., руб",
        "Сумма, руб",
    }
    has_data = not df.empty and expected_cols.issubset(set(df.columns))

    total_rows = int(len(df)) if has_data else 0
    truncated = False
    included_rows = 0
    file_lines: list[str] = []
    position_block = ""

    if has_data:
        sum_col = "Сумма, руб"
        df = df.dropna(subset=[sum_col])
        df[sum_col] = pd.to_numeric(df[sum_col], errors="coerce")
        df = df[df[sum_col] > 0]
        total_rows = len(df)
        total_sum = float(df[sum_col].sum())

        grp = df.groupby("Файл ЛСР", dropna=False)[sum_col].agg(["sum", "count"])
        grp = grp.sort_values("sum", ascending=False)
        for fname, row in grp.iterrows():
            file_lines.append(
                f"- {fname}: позиций {int(row['count'])}, сумма {_fmt_rub_amount(row['sum'])} руб."
            )

        cap = max(0, min(max_rows, 5000))
        work = df.sort_values(sum_col, ascending=False).head(cap) if cap else df.iloc[0:0]
        included_rows = len(work)
        truncated = total_rows > included_rows

        lines: list[str] = []
        for _, r in work.iterrows():
            name = str(r.get("Название работы/услуги", "")).strip().replace("\n", " ")
            if len(name) > 420:
                name = name[:417] + "..."
            unit = str(r.get("Ед. изм.", "") or "").strip()
            qty = r.get("Кол-во", "")
            if pd.notna(qty):
                try:
                    qty_s = f"{float(qty):g}".replace(".", ",")
                except (TypeError, ValueError):
                    qty_s = str(qty)
            else:
                qty_s = "—"
            up = r.get("Цена за ед., руб", "")
            up_s = _fmt_rub_amount(up) if pd.notna(up) else "—"
            sm = _fmt_rub_amount(r.get(sum_col))
            no = r.get("№ п/п", "")
            lines.append(
                f"{len(lines) + 1}. [п/п {no}] {name}\n"
                f"   Ед.: {unit}, кол-во: {qty_s}, цена за ед.: {up_s} руб., сумма по смете: {sm} руб."
            )

        position_block = "\n".join(lines) if lines else "(нет строк с положительной суммой)"
    else:
        total_sum = 0.0
        position_block = (
            "(Excel-отчёт не найден или пустой — сметные позиции не подставлены. "
            f"Ожидаемый файл: {xlsx_path.name}. Сгенерируй отчёт через main.py и обнови страницу.)"
        )

    nmcc = _fmt_rub_amount(price_rub)
    sum_report = _fmt_rub_amount(total_sum) if has_data and total_rows else "—"

    file_section = "\n".join(file_lines) if file_lines else "—"

    prompt = f"""КРИТИЧНО — прочитай перед данными:
• Отвечай ТОЛЬКО по-русски (без англоязычного пересказа «what the file contains»).
• НЕ описывай формат файла, «что это за документ», структуру таблицы — ниже уже готовые извлечённые данные, не нужно их рефлексировать.
• Нужна АНАЛИТИКА тендера: реальные/рыночные ориентиры цен (через веб-поиск по РФ и региону «{region}»: Авито, магазины, объявления подрядчиков), сравнение со сметой, риски, вывод для подрядчика.
• Если веб-поиск недоступен — одной строкой в начале: «Веб-поиск недоступен», затем только общие рекомендации без выдуманных цифр с «рынка».

Ты аналитик строительного и ремонтного рынка в России. Ниже — контекст госзакупки и строки локальных смет (ЛСР).

ЗАДАЧА
1) По возможности найди ориентиры рыночных цен в РФ на дату анализа (или ближайший период): Авито, объявления подрядчиков, интернет-магазины стройматериалов, агрегаторы, типовые калькуляторы — что уместно по каждой категории работ/материалов.
2) Сопоставь рынок с ценами из сметы: за единицу и суммы по строкам; отметь где смета заметно выше/ниже рынка и почему (объём, регион «{region}», единицы измерения 100 м² и т.д.).
3) Оцени экономическую привлекательность и риски контракта для подрядчика (сроки, дефекты объёма в ЛСР, дубли позиций, заложенные материалы vs факт).
4) Сформулируй краткий итог: целесообразно ли участвовать, что обязательно уточнить на объекте и в документации.

ОГОВОРКИ
Цены в открытых объявлениях — ориентир, не договор. Указывай источники и диапазоны. Регион закупки: {region}.

ДАННЫЕ ЗАКУПКИ (ЕИС)
- Реестровый номер / ID: {tender_id}
- Регион: {region}
- Наименование / номер: {title}
- Стадия: {stage or "—"}
- Дата публикации (если есть): {publish_date or "—"}
- НМЦК (из карточки парсера): {nmcc} руб.
- Ссылка на карточку: {url or "—"}

СВОДКА ПО ФАЙЛАМ ЛСР (как в отчёте)
{file_section}

ИТОГИ ПО РАСПОЗНАННЫМ ПОЗИЦИЯМ
- Число позиций в отчёте: {total_rows if has_data else 0}
- Сумма по позициям в отчёте: {sum_report} руб.
- Важно: сумма по ЛСР часто не совпадает с НМЦК: НМЦК обычно с НДС 22%, в ЛСР часто цены без НДС; плюс добавочные коэффициенты сметы (зимние, индексные, районные и т.д.) и неполный охват файлов (объектные сметы, НЗ).

ПОЗИЦИИ СМЕТЫ ДЛЯ РЫНОЧНОГО СОПОСТАВЛЕНИЯ
{_perplexity_positions_caption(has_data, truncated)}
{position_block}

ФОРМАТ ОТВЕТА
Структурируй ответ заголовками: «Рынок и цены», «Смета vs рынок», «Риски», «Итог». Пиши по-русски."""

    info = {
        "total_rows": total_rows,
        "included_rows": included_rows,
        "truncated": truncated,
    }
    return prompt.strip(), info
