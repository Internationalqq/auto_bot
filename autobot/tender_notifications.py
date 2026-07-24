"""
Сборка уведомлений о новом тендере: текст карточки, вложения (Excel, промпт для внешней аналитики).
Отправка — через telegram_notify (только транспорт).

Аналитика с веб-поиском не делается здесь: файл perplexity_prompt_*.txt + Perplexity и т.п.
Опциональный черновик LLM — analytics_openai.py.
"""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from autobot.analytics_openai import try_short_tender_draft
from autobot.report_prompt import build_perplexity_prompt_for_tender
from autobot.site_public_url import get_report_site_public_base
from autobot.telegram_notify import send_document_bytes, send_message

# Согласовано с main.py (подсказка НДС в карточке)
NDS_MULT = 1.22


def _report_site_base() -> str:
    return get_report_site_public_base()


def _telegram_send_estimate_excel() -> bool:
    """Включить вложение Excel сметы в TG: TELEGRAM_SEND_ESTIMATE_EXCEL=1 (по умолчанию выкл.)."""
    v = (os.environ.get("TELEGRAM_SEND_ESTIMATE_EXCEL") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _fmt_rub(v: float | None) -> str:
    if v is None:
        return "—"
    s = f"{float(v):,.2f}"
    return s.replace(",", " ").replace(".", ",")


def _title_redundant_with_id(title: str, tender_id: str) -> bool:
    t = (title or "").strip().lower().replace("№", "").replace(" ", "")
    tid = (tender_id or "").strip()
    return bool(tid) and t == tid


def _web_ui_links_block(tender_id: str) -> list[str]:
    base = _report_site_base()
    if not base:
        return [
            "<i>Веб-ссылки: в .env задайте один из вариантов — "
            "<code>REPORT_SITE_PUBLIC_BASE_URL</code>, "
            "<code>WEB_UI_PUBLIC_BASE_URL</code> или пару "
            "<code>WEB_UI_PUBLIC_HOST</code> + <code>WEB_UI_PORT</code> "
            "(например хост <code>192.168.1.10</code>, порт <code>8765</code>). "
            "Должен быть запущен <code>web_ui.py</code>.</i>",
        ]
    est_name = f"ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.html"
    est_href = f"{base}/reports/{quote(est_name, safe='')}"
    merge_href = f"{base}/merge-report/{tender_id}/"
    return [
        "🔗 <a href=\""
        + html.escape(est_href, quote=True)
        + "\">Отчёт по сметам на сайте</a>",
        "🔗 <a href=\""
        + html.escape(merge_href, quote=True)
        + "\">Сводка сравнение + рынок</a> <i>(после сборки пайплайна)</i>",
    ]


def _build_new_tender_card_message(
    tender_id: str,
    title: str,
    region: str,
    url: str,
    price_rub: float | None,
    positions_count: int,
    sum_positions: float,
) -> str:
    nmcc = price_rub
    sum_vat_hint = sum_positions * NDS_MULT if sum_positions else 0.0
    lines = [
        "🆕 <b>Новый тендер</b> (по твоим фильтрам)",
        f"№ <code>{html.escape(tender_id)}</code>",
    ]
    if (title or "").strip() and not _title_redundant_with_id(title, tender_id):
        lines.append(html.escape(title.strip()))
    lines.extend(
        [
            f"Регион: {html.escape(region)}",
            f"НМЦК: {_fmt_rub(nmcc)} руб." if nmcc is not None else "НМЦК: —",
            f"Ссылка ЕИС: {html.escape(url, quote=True)}" if url else "",
            "",
            f"Позиций в отчёте: {positions_count}",
            f"Сумма по позициям: {_fmt_rub(sum_positions) if sum_positions else '—'} руб.",
            f"Ориентир ×{NDS_MULT:g} (если ЛСР без НДС): {_fmt_rub(sum_vat_hint) if sum_positions else '—'} руб.",
        ]
    )
    lines.extend(_web_ui_links_block(tender_id))
    return "\n".join(x for x in lines if x is not None)


def notify_new_tender_processed(
    token: str,
    chat_id: str,
    tender_id: str,
    title: str,
    region: str,
    url: str,
    price_rub: float | None,
    xlsx_path: Path | None,
    positions_count: int,
    sum_positions: float,
    top_work_lines: list[str],
    perplexity_max_rows: int = 300,
) -> None:
    send_message(
        token,
        chat_id,
        _build_new_tender_card_message(
            tender_id, title, region, url, price_rub, positions_count, sum_positions
        ),
    )

    ai_note = try_short_tender_draft(
        tender_id, region, price_rub, positions_count, sum_positions, top_work_lines
    )
    cap_parts: list[str] = []
    if ai_note:
        cap_parts.append("Черновик (OpenAI, без веб-поиска):\n" + ai_note)
    skip_pp = (os.environ.get("SKIP_PERPLEXITY_TXT") or "").strip().lower() in ("1", "true", "yes", "on")
    if not skip_pp:
        cap_parts.append(
            "Полный анализ с Авито/сайтами: следующий файл .txt — в Perplexity с веб-поиском."
        )
    caption = "\n\n".join(cap_parts)[:1024]

    if _telegram_send_estimate_excel() and xlsx_path and xlsx_path.is_file():
        try:
            xlsx_bytes = xlsx_path.read_bytes()
            if len(xlsx_bytes) <= 48 * 1024 * 1024:
                send_document_bytes(token, chat_id, xlsx_path.name, xlsx_bytes, caption=caption)
            else:
                send_message(
                    token,
                    chat_id,
                    "Excel отчёт слишком большой для Telegram — смотри на диске:\n" + str(xlsx_path),
                )
                if caption.strip():
                    send_message(token, chat_id, caption, parse_mode=None)
        except Exception:
            send_message(token, chat_id, "Не удалось отправить Excel во вложении.", parse_mode=None)
    elif caption.strip():
        send_message(token, chat_id, caption, parse_mode=None)

    if not skip_pp:
        prompt, _info = build_perplexity_prompt_for_tender(tender_id, perplexity_max_rows)
        fname = f"perplexity_prompt_{tender_id}.txt"
        send_document_bytes(
            token,
            chat_id,
            fname,
            prompt.encode("utf-8"),
            caption="Промпт для внешней аналитики (Perplexity + веб-поиск).",
        )


def safe_notify_new_tender(**kwargs: Any) -> None:
    try:
        notify_new_tender_processed(**kwargs)
    except Exception as e:
        print(f"Уведомление в Telegram не отправлено ({e})")
