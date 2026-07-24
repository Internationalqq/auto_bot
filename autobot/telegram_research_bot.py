"""
Мини-обработчик Telegram-команд для справки по услуге/товару/материалу.

Команды:
  /info бетон м300
  /research демонтаж кровли
  /help

Без внешних Telegram-фреймворков: используется тот же requests-слой и .env,
что уже есть в проекте.
"""

from __future__ import annotations

import argparse
import html
import os
import time
from pathlib import Path
from typing import Any

import requests

from autobot.estimate_excel_analysis import (
    EstimateSession,
    format_candidates_preview_html,
    format_catalogue_html,
    format_summary_html,
    load_estimate_session,
    parse_remove_numbers,
    remove_candidates,
    select_candidates,
)
from autobot.item_research import format_research_html, parse_sources, research_item
from autobot.paths import REPO_ROOT
from autobot.telegram_notify import TELEGRAM_API, _telegram_request_kwargs, send_message, telegram_config

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass


HELP_TEXT = """\
Я умею:
1) собрать короткую сводку по услуге, товару или материалу;
2) разобрать Excel-смету и посчитать выбранную позицию.

Команды:
<code>/info бетон м300</code>
<code>/research демонтаж кровли</code>
или загрузите Excel-файл сметы.

После Excel:
• выберите позицию номером или текстом;
• проверьте строки, которые бот предложит объединить;
• напишите <code>да</code> для расчёта или <code>убрать 2,4</code>, чтобы исключить лишние.
"""


ESTIMATE_SESSIONS: dict[str, EstimateSession] = {}


def _tg_get(token: str, method: str, **kwargs: Any) -> dict[str, Any]:
    url = TELEGRAM_API.format(token=token, method=method)
    r = requests.get(url, params={k: v for k, v in kwargs.items() if v is not None}, **_telegram_request_kwargs(60))
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data


def _download_telegram_file(token: str, file_id: str, dest: Path) -> Path:
    data = _tg_get(token, "getFile", file_id=file_id)
    file_path = str((data.get("result") or {}).get("file_path") or "")
    if not file_path:
        raise RuntimeError("Telegram не вернул file_path для файла")
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    r = requests.get(url, **_telegram_request_kwargs(180))
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return dest


def _send_chat_action(token: str, chat_id: str, action: str = "typing") -> None:
    try:
        url = TELEGRAM_API.format(token=token, method="sendChatAction")
        requests.post(
            url,
            json={"chat_id": str(chat_id), "action": action},
            **_telegram_request_kwargs(20),
        )
    except Exception:
        pass


def _allowed_chats(default_chat_id: str) -> set[str]:
    raw = (os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS") or default_chat_id or "").strip()
    return {x.strip() for x in raw.split(",") if x.strip()}


def _extract_query(text: str, *, chat_type: str) -> tuple[str, str]:
    raw = (text or "").strip()
    low = raw.casefold()
    for cmd in ("/info", "/research", "/item", "/товар", "/справка"):
        if low == cmd or low.startswith(cmd + " ") or low.startswith(cmd + "@"):
            # /info@BotName бетон -> бетон
            parts = raw.split(maxsplit=1)
            return ("query", parts[1].strip() if len(parts) > 1 else "")
    if low in ("/start", "/help", "help", "помощь"):
        return ("help", "")
    if low in ("/estimate", "/smeta", "/смета", "смета"):
        return ("estimate_help", "")
    if low in ("/cancel", "отмена", "сброс"):
        return ("cancel", "")

    accept_plain = (os.environ.get("RESEARCH_BOT_ACCEPT_PLAIN") or "1").strip().lower() not in ("0", "false", "no", "off")
    # В личке можно писать просто "бетон м300"; в группе — только командами, чтобы бот не отвечал на всё подряд.
    if accept_plain and chat_type == "private" and raw and not raw.startswith("/"):
        return ("query", raw)
    return ("ignore", "")


def _safe_upload_name(chat_id: str, document: dict[str, Any]) -> str:
    raw = str(document.get("file_name") or "estimate.xlsx")
    suffix = Path(raw).suffix.lower()
    if suffix not in (".xlsx", ".xls", ".xlsm"):
        suffix = ".xlsx"
    unique = str(document.get("file_unique_id") or document.get("file_id") or int(time.time()))
    safe_chat = "".join(ch for ch in chat_id if ch.isdigit() or ch == "-")[:40] or "chat"
    safe_unique = "".join(ch for ch in unique if ch.isalnum() or ch in ("_", "-"))[:80] or str(int(time.time()))
    return f"{safe_chat}_{safe_unique}{suffix}"


def _handle_estimate_document(token: str, chat_id: str, document: dict[str, Any]) -> bool:
    filename = str(document.get("file_name") or "")
    ext = Path(filename).suffix.lower()
    if ext not in (".xlsx", ".xls", ".xlsm"):
        return False
    file_id = str(document.get("file_id") or "")
    if not file_id:
        raise RuntimeError("У документа нет file_id")
    _send_chat_action(token, chat_id, "typing")
    dest = REPO_ROOT / "data" / "uploads" / "estimate_analysis" / _safe_upload_name(chat_id, document)
    _download_telegram_file(token, file_id, dest)
    session = load_estimate_session(dest)
    ESTIMATE_SESSIONS[chat_id] = session
    send_message(token, chat_id, format_catalogue_html(session), parse_mode="HTML", disable_web_page_preview=True)
    return True


def _handle_estimate_text(token: str, chat_id: str, text: str) -> bool:
    session = ESTIMATE_SESSIONS.get(chat_id)
    if not session:
        return False
    raw = (text or "").strip()
    low = raw.casefold()
    if not raw:
        return True
    if low in ("/start", "/help", "help", "помощь"):
        send_message(token, chat_id, HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)
        return True
    if low in ("/estimate", "/smeta", "/смета", "смета"):
        send_message(
            token,
            chat_id,
            "Excel-смета уже загружена. Напишите номер/название позиции или <code>список 2</code>. Для сброса: <code>отмена</code>.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    if low in ("/cancel", "отмена", "сброс"):
        ESTIMATE_SESSIONS.pop(chat_id, None)
        send_message(token, chat_id, "Ок, анализ Excel-сметы сброшен.", parse_mode="HTML", disable_web_page_preview=True)
        return True
    if low.startswith("список") or low.startswith("/list"):
        nums = parse_remove_numbers(raw)
        page = nums[0] if nums else 1
        send_message(token, chat_id, format_catalogue_html(session, page=page), parse_mode="HTML", disable_web_page_preview=True)
        return True
    if session.candidates and (low.startswith("строки") or low.startswith("кандидаты")):
        nums = parse_remove_numbers(raw)
        page = nums[0] if nums else 1
        send_message(token, chat_id, format_candidates_preview_html(session, page=page), parse_mode="HTML", disable_web_page_preview=True)
        return True
    if session.candidates and low in ("да", "ок", "подтвердить", "считать", "посчитать", "итог"):
        send_message(token, chat_id, format_summary_html(session), parse_mode="HTML", disable_web_page_preview=True)
        return True
    if session.candidates and (low.startswith("убрать") or low.startswith("исключить") or low.startswith("удалить")):
        nums = parse_remove_numbers(raw)
        if not nums:
            send_message(token, chat_id, "Напишите номера строк из превью, например: <code>убрать 2,4</code>", parse_mode="HTML")
            return True
        remove_candidates(session, nums)
        send_message(token, chat_id, format_candidates_preview_html(session), parse_mode="HTML", disable_web_page_preview=True)
        return True
    # Если пользователь явно запускает справку по рынку, не перехватываем.
    if low.startswith(("/info", "/research", "/item", "/товар", "/справка")):
        return False
    select_candidates(session, raw)
    send_message(token, chat_id, format_candidates_preview_html(session), parse_mode="HTML", disable_web_page_preview=True)
    return True


def handle_update(token: str, update: dict[str, Any], *, allowed_chat_ids: set[str], sources: list[str], max_results: int, region: str) -> int | None:
    message = update.get("message") or update.get("edited_message") or {}
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    chat_type = str(chat.get("type") or "")
    text = str(message.get("text") or "").strip()
    document = message.get("document") or {}
    if not chat_id:
        return None
    if allowed_chat_ids and chat_id not in allowed_chat_ids:
        return None

    if isinstance(document, dict) and document:
        try:
            handled = _handle_estimate_document(token, chat_id, document)
            if handled:
                return int(update.get("update_id") or 0)
        except Exception as e:
            send_message(
                token,
                chat_id,
                "Не смог разобрать Excel-смету.\n"
                f"Причина: <code>{html.escape(str(e)[:900])}</code>",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return int(update.get("update_id") or 0)

    if not text:
        return None
    if _handle_estimate_text(token, chat_id, text):
        return int(update.get("update_id") or 0)

    kind, query = _extract_query(text, chat_type=chat_type)
    if kind == "ignore":
        return None
    if kind == "help":
        send_message(token, chat_id, HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)
        return int(update.get("update_id") or 0)
    if kind == "estimate_help":
        send_message(
            token,
            chat_id,
            "Загрузите Excel-файл сметы (.xlsx/.xls/.xlsm). Я покажу найденные позиции, потом дам выбрать и подтвердить объединяемые строки.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return int(update.get("update_id") or 0)
    if kind == "cancel":
        ESTIMATE_SESSIONS.pop(chat_id, None)
        send_message(token, chat_id, "Ок, текущий сценарий сброшен.", parse_mode="HTML", disable_web_page_preview=True)
        return int(update.get("update_id") or 0)
    if not query:
        send_message(
            token,
            chat_id,
            "Напишите запрос после команды, например:\n<code>/info бетон м300</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return int(update.get("update_id") or 0)

    _send_chat_action(token, chat_id, "typing")
    try:
        result = research_item(query, region=region, sources=sources, max_results=max_results)
        send_message(token, chat_id, format_research_html(result), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        send_message(
            token,
            chat_id,
            "Не смог собрать сводку по запросу.\n"
            f"Причина: <code>{str(e)[:900]}</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    return int(update.get("update_id") or 0)


def run_polling(*, poll_timeout: int = 30, once: bool = False) -> None:
    cfg = telegram_config()
    if not cfg:
        raise RuntimeError("Нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID в .env")
    token, default_chat_id = cfg
    allowed = _allowed_chats(default_chat_id)
    sources = parse_sources(os.environ.get("MARKET_SUMMARY_SOURCES") or os.environ.get("MARKET_SOURCES") or "avito,web")
    max_results = max(1, min(10, int(os.environ.get("MARKET_SUMMARY_MAX_RESULTS", "5") or "5")))
    region = os.environ.get("MARKET_SUMMARY_REGION", "").strip()
    offset: int | None = None

    print(
        "Telegram research bot запущен: "
        f"allowed_chats={','.join(sorted(allowed)) or 'all'}, "
        f"sources={','.join(sources)}, max_results={max_results}, region={region or '-'}",
        flush=True,
    )
    while True:
        try:
            data = _tg_get(token, "getUpdates", offset=offset, timeout=poll_timeout, allowed_updates='["message","edited_message"]')
            updates = data.get("result") or []
            for upd in updates:
                uid = int(upd.get("update_id") or 0)
                handled_uid = handle_update(
                    token,
                    upd,
                    allowed_chat_ids=allowed,
                    sources=sources,
                    max_results=max_results,
                    region=region,
                )
                offset = max(offset or 0, uid + 1)
                if handled_uid is not None:
                    offset = max(offset or 0, handled_uid + 1)
            if once:
                return
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"Telegram research bot: {type(e).__name__}: {e}", flush=True)
            if once:
                raise
            time.sleep(5)


def main() -> None:
    ap = argparse.ArgumentParser(description="Telegram-команды /info и /research")
    ap.add_argument("--once", action="store_true", help="Один getUpdates-цикл и выход")
    ap.add_argument("--poll-timeout", type=int, default=30)
    args = ap.parse_args()
    run_polling(poll_timeout=max(1, min(60, int(args.poll_timeout or 30))), once=bool(args.once))


if __name__ == "__main__":
    main()
