"""
Только доставка в Telegram: сообщения и файлы в чат / беседу.
Никакой аналитики и смет — см. tender_notifications.py, report_prompt.py, analytics_openai.py.

Переменные окружения:
  TELEGRAM_BOT_TOKEN — токен бота от @BotFather
  TELEGRAM_CHAT_ID — chat_id пользователя или группы (для беседы — id группы, бот должен быть в группе)
  TELEGRAM_HTTPS_PROXY / TELEGRAM_HTTP_PROXY — только для запросов к api.telegram.org (раздельный маршрут с ЕИС)
  TELEGRAM_SPOOL_ON_FAIL — при сбое сети складывать текст в data/logs/telegram_outbox.jsonl (по умолчанию 1)
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import requests

from autobot.paths import REPO_ROOT

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _telegram_request_kwargs(timeout_sec: int) -> dict[str, Any]:
    """
    Опциональный прокси только для Telegram:
      TELEGRAM_HTTPS_PROXY=https://user:pass@host:port
      TELEGRAM_HTTP_PROXY=http://host:port
    """
    kwargs: dict[str, Any] = {"timeout": timeout_sec}
    http_proxy = (os.environ.get("TELEGRAM_HTTP_PROXY") or "").strip()
    https_proxy = (os.environ.get("TELEGRAM_HTTPS_PROXY") or "").strip()
    proxies: dict[str, str] = {}
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    if proxies:
        kwargs["proxies"] = proxies
    return kwargs


def _spool_enabled() -> bool:
    v = (os.environ.get("TELEGRAM_SPOOL_ON_FAIL") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _spool_path() -> Path:
    p = REPO_ROOT / "data" / "logs" / "telegram_outbox.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _spool_message(chat_id: str, text: str, parse_mode: str | None, disable_web_page_preview: bool) -> None:
    if not _spool_enabled():
        return
    row = {
        "chat_id": str(chat_id),
        "text": str(text),
        "parse_mode": parse_mode,
        "disable_web_page_preview": bool(disable_web_page_preview),
    }
    with _spool_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def flush_spooled_messages(token: str, *, max_items: int = 30) -> int:
    """
    Пытается отправить накопившиеся сообщения из outbox.
    Возвращает число успешно отправленных сообщений.
    """
    p = _spool_path()
    if not p.is_file():
        return 0
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return 0
    sent = 0
    keep: list[str] = []
    for i, ln in enumerate(lines):
        if sent >= max_items:
            keep.extend(lines[i:])
            break
        try:
            item = json.loads(ln)
            _tg_post_json(
                token,
                "sendMessage",
                chat_id=item.get("chat_id"),
                text=item.get("text"),
                parse_mode=item.get("parse_mode"),
                disable_web_page_preview=item.get("disable_web_page_preview", True),
            )
            sent += 1
        except Exception:
            keep.extend(lines[i:])
            break
    if keep:
        p.write_text("\n".join(keep) + "\n", encoding="utf-8")
    elif p.exists():
        p.unlink(missing_ok=True)
    return sent


def telegram_config() -> tuple[str, str] | None:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if token and chat:
        return token, chat
    return None


def _tg_post_json(token: str, method: str, **kwargs) -> dict[str, Any]:
    url = TELEGRAM_API.format(token=token, method=method)
    r = requests.post(
        url,
        json={k: v for k, v in kwargs.items() if v is not None},
        **_telegram_request_kwargs(120),
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data


def delete_message(token: str, chat_id: str, message_id: int) -> bool:
    """
    Удалить сообщение бота из чата.
    В супергруппах нужны права; сообщения старше 48 ч иногда не удаляются — тогда False.
    """
    try:
        _tg_post_json(
            token,
            "deleteMessage",
            chat_id=str(chat_id),
            message_id=int(message_id),
        )
        return True
    except Exception:
        return False


def send_message_first_chunk_message_id(
    token: str,
    chat_id: str,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    disable_web_page_preview: bool = True,
) -> int | None:
    """
    Как send_message для первого чанка: возвращает message_id первого отправленного сообщения.
    Для коротких текстов — одно сообщение. При ошибке — в spool как send_message, возвращает None.
    """
    try:
        flush_spooled_messages(token)
    except Exception:
        pass
    limit = 4096
    first_id: int | None = None
    for i in range(0, len(text), limit):
        chunk = text[i : i + limit]
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            data = _tg_post_json(token, "sendMessage", **payload)
            if first_id is None:
                res = data.get("result")
                if isinstance(res, dict):
                    mid = res.get("message_id")
                    if mid is not None:
                        first_id = int(mid)
        except Exception:
            _spool_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            return None
    return first_id


def send_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    disable_web_page_preview: bool = True,
) -> None:
    """Текст до 4096 символов за запрос; длиннее — режется на несколько сообщений."""
    # Перед отправкой новых пытаемся догнать накопившиеся (если есть сеть до Telegram).
    try:
        flush_spooled_messages(token)
    except Exception:
        pass
    limit = 4096
    for i in range(0, len(text), limit):
        chunk = text[i : i + limit]
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            _tg_post_json(token, "sendMessage", **payload)
        except Exception:
            _spool_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            raise


def send_document_bytes(
    token: str,
    chat_id: str,
    filename: str,
    data: bytes,
    caption: str = "",
    *,
    caption_parse_mode: str | None = None,
) -> None:
    cap = (caption or "")[:1024]
    url = TELEGRAM_API.format(token=token, method="sendDocument")
    data_form: dict[str, Any] = {"chat_id": chat_id}
    if cap:
        data_form["caption"] = cap
    if caption_parse_mode:
        data_form["caption_parse_mode"] = caption_parse_mode
    files = {"document": (filename, io.BytesIO(data), "application/octet-stream")}
    r = requests.post(url, data=data_form, files=files, **_telegram_request_kwargs(300))
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram sendDocument: {body}")


def send_document_file(
    token: str,
    chat_id: str,
    file_path: Path,
    caption: str = "",
    *,
    caption_parse_mode: str | None = None,
) -> None:
    """Отправить файл с диска."""
    p = Path(file_path)
    send_document_bytes(
        token,
        chat_id,
        p.name,
        p.read_bytes(),
        caption=caption,
        caption_parse_mode=caption_parse_mode,
    )
