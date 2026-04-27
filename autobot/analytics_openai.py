"""
Опциональная аналитика через OpenAI API (черновик без веб-поиска).
Telegram этим не занимается — только вызов из tender_notifications / других оркестраторов.

Переменные окружения:
  OPENAI_API_KEY
  OPENAI_MODEL — по умолчанию gpt-4o-mini
"""

from __future__ import annotations

import os
from typing import Any

import requests


def _fmt_rub(v: float | None) -> str:
    if v is None:
        return "—"
    s = f"{float(v):,.2f}"
    return s.replace(",", " ").replace(".", ",")


def try_short_tender_draft(
    tender_id: str,
    region: str,
    nmcc: float | None,
    positions: int,
    sum_positions: float,
    top_snippets: list[str],
) -> str | None:
    """Короткий текст для подрядчика; без цен с «рынка»."""
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        return None
    model = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
    user_block = "\n".join(top_snippets[:25])
    nmcc_s = _fmt_rub(nmcc) if nmcc is not None else "не указана"
    prompt = f"""Регион закупки: {region}
НМЦК (карточка): {nmcc_s} руб.
Позиций в распознанной смете: {positions}
Сумма по позициям в отчёте: {_fmt_rub(sum_positions)} руб.
ID тендера: {tender_id}

Крупнейшие позиции (фрагмент):
{user_block}

Дай краткий черновик (до 900 символов) для подрядчика: общие риски/выгода, на что смотреть.
Не выдумывай конкретные цены с Авито — их нет в данных. Пиши по-русски."""

    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Ты аналитик строительных тендеров в РФ. Ответы краткие, без выдуманных цифр рынка.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 500,
                "temperature": 0.3,
            },
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return None


def try_merge_viability_narrative(tender_id: str, st: Any) -> str | None:
    """
    Короткий текст плюсы/минусы по уже посчитанным метрикам сводки смета+рынок.
    Нужен OPENAI_API_KEY; иначе None.
    """
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        return None
    model = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
    med = getattr(st, "median_ratio", None)
    med_s = f"{float(med):.3f}".replace(".", ",") if med is not None else "—"
    user_block = f"""Тендер (regNumber): {tender_id}
Строк сметы без «Явный дубликат»: {getattr(st, "rows_considered", 0)}
Строк, где есть и смета и рынок в одной шкале: {getattr(st, "comparable", 0)}
Строк без ориентира рынка: {getattr(st, "no_market", 0)}
Смета заметно ниже рынка (отношение < 0,92): {getattr(st, "smeta_below_market", 0)}
Рядом с рынком (0,92–1,08): {getattr(st, "smeta_near_market", 0)}
Смета заметно выше рынка (> 1,08): {getattr(st, "smeta_above_market", 0)}
Медиана отношений «цена в сравнении / медиана рынка» по строкам: {med_s}
"""
    prompt = f"""{user_block}
Напиши по-русски для подрядчика, который смотрит на закупку:
1) Одно предложение — насколько по этим цифрам тендер «жёсткий» по цене или наоборот «с запасом».
2) Маркированный список «Плюсы» (3–5 пунктов) — только из этих метрик, без новых чисел.
3) Маркированный список «Минусы / риски» (3–5 пунктов).
Объём до 1100 символов. Не придумывай НМЦК и не ссылайся на внешние источники."""

    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Ты аналитик строительных тендеров РФ. Пишешь кратко, без выдуманных цифр.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 600,
                "temperature": 0.25,
            },
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return None
