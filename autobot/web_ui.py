from __future__ import annotations

from autobot.paths import DATA_DIR, REPO_ROOT
import io
import gzip
import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import uuid
import sys
import time
import traceback
import threading
import sqlite3
import html as html_mod
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

import pandas as pd

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

from flask import (
    Flask,
    abort,
    redirect,
    jsonify,
    make_response,
    render_template,
    render_template_string,
    request,
    send_file,
    send_from_directory,
    url_for,
)

from autobot.site_public_url import get_report_site_public_base
from autobot.source_documents import (
    build_source_bytes_preview,
    build_source_file_preview,
    format_file_size,
    list_tender_source_files,
    read_archive_member,
    repair_filename,
    resolve_tender_source_file,
)
from autobot.tender_detail import build_tender_detail
from autobot.document_preview_worker import PreviewRejected
from autobot.tender_deletion import delete_tender_data
from autobot.workflow_overview import build_storage_overview, build_workflow_payload

_AUTOBOT_MAIN_FILE = REPO_ROOT / "autobot" / "main.py"
_TOOLS_RUN_MODULE = REPO_ROOT / "tools" / "run_module.py"

try:
    from autobot.report_prompt import (
        BASE_DIR,
        REPORTS_DIR,
        TENDERS_JSON,
        load_tender_metadata,
    )
except ModuleNotFoundError as e:
    if getattr(e, "name", None) == "report_prompt":
        sys.stderr.write("Не удалось загрузить autobot.report_prompt (проверьте установку пакета autobot/).\n")
    raise

DEFAULT_MAX_UPLOAD_MB = 100


def _configured_max_upload_mb() -> int:
    raw_value = (os.environ.get("WEB_UI_MAX_UPLOAD_MB") or str(DEFAULT_MAX_UPLOAD_MB)).strip()
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_UPLOAD_MB


MAX_UPLOAD_MB = _configured_max_upload_mb()

app = Flask(__name__)
from autobot.uploaded_review import blueprint as uploaded_review_blueprint
app.register_blueprint(uploaded_review_blueprint)
from autobot.tender_review import blueprint as tender_review_blueprint
app.register_blueprint(tender_review_blueprint)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
_estimate_capability_secret_raw = str(os.environ.get("AUTOBOT_BRIDGE_SIGNING_SECRET") or "")
_ESTIMATE_IMPORT_CAPABILITY_SECRET = (
    hashlib.sha256(_estimate_capability_secret_raw.encode("utf-8")).digest()
    if _estimate_capability_secret_raw
    else os.urandom(32)
)
del _estimate_capability_secret_raw


def _http_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlparse(str(value or "").strip())
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").strip().rstrip(".").lower()
        if scheme not in {"http", "https"} or not hostname:
            return None
        hostname = hostname.encode("idna").decode("ascii")
        port = parsed.port or (443 if scheme == "https" else 80)
    except (UnicodeError, ValueError):
        return None
    return scheme, hostname, port


def _trusted_mutation_origins() -> set[tuple[str, str, int]]:
    """Return configured public origins, falling back to the request host locally."""

    configured_values = (
        os.environ.get("WEB_UI_PUBLIC_BASE_URL", ""),
        os.environ.get("REPORT_SITE_PUBLIC_BASE_URL", ""),
        os.environ.get("PMBI_PUBLIC_BASE_URL", ""),
        os.environ.get("PMBI_CRM_PUBLIC_URL", ""),
        os.environ.get("PMBI_CRM_PARENT_ORIGIN", ""),
    )
    trusted: set[tuple[str, str, int]] = set()
    for value in configured_values:
        origin = _http_origin(str(value or ""))
        if origin is not None:
            trusted.add(origin)
    if not any(str(value or "").strip() for value in configured_values):
        host_origin = _http_origin(request.host_url)
        if host_origin is not None:
            trusted.add(host_origin)
    return trusted


@app.before_request
def reject_cross_site_mutation():
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    if str(request.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
        return jsonify(
            {
                "ok": False,
                "error": "cross_site_request_blocked",
                "message": "Не удалось подтвердить источник запроса. Обновите страницу и повторите действие.",
            }
        ), 403
    origin = str(request.headers.get("Origin") or "").strip()
    if not origin:
        return None
    request_origin = _http_origin(origin)
    if request_origin is None or request_origin not in _trusted_mutation_origins():
        return jsonify(
            {
                "ok": False,
                "error": "cross_site_request_blocked",
                "message": "Не удалось подтвердить источник запроса. Обновите страницу и повторите действие.",
            }
        ), 403
    return None


@app.before_request
def protect_report_publication():
    from flask import g
    from autobot.estimate_publication_recovery import consistent_report, PublicationRecoveryRequired
    consumers = {'tender_detail_page', 'tender_economics_source',
                 'tender_estimate_download_xlsx', 'tender_market_sources_download_xlsx',
                 'tender_svodka_download_xlsx', 'merge_report_site'}
    tid = None
    if request.endpoint in consumers or (request.endpoint == 'api_tender_agent_market_jobs' and request.method == 'POST'):
        tid = (request.view_args or {}).get('tender_id')
    elif request.endpoint == 'api_export_to_crm':
        payload = request.get_json(silent=True)
        tid = payload.get('tender_id') if isinstance(payload, dict) else None
    elif request.endpoint == 'report_file':
        filename = (request.view_args or {}).get('filename', '')
        if any(part.startswith('.') or part == 'previous' for part in Path(filename).parts) or filename.startswith('PUBLICATION_'):
            abort(404)
        match = re.search(r'_([0-9]{8,25})\.(?:xlsx|html|json)$', filename)
        tid = match[1] if match else None
    if not isinstance(tid, str) or not re.fullmatch(r'[0-9]{8,25}', tid):
        return None
    context = consistent_report(REPORTS_DIR, tid)
    try:
        context.__enter__()
    except (PublicationRecoveryRequired, OSError, TimeoutError) as error:
        busy = isinstance(error, TimeoutError)
        message = ('Отчёт сейчас сохраняется. Обновите страницу через несколько секунд.' if busy
                   else str(error) if isinstance(error, PublicationRecoveryRequired)
                   else 'Не удалось проверить сохранность отчёта. Исходные файлы доступны ниже.')
        if request.path.startswith('/api/'):
            response = jsonify({'ok': False, 'error': 'report_busy' if busy else 'publication_recovery_required', 'message': message})
        else:
            response = make_response(render_template('publication_unavailable.html', tender_id=tid,
                busy=busy, message=message, documents=list_tender_source_files(tid)))
        response.status_code = 503 if busy else 409
        response.headers['Cache-Control'] = 'no-store'
        if busy:
            response.headers['Retry-After'] = '2'
        return response
    g.report_publication_context = context


@app.teardown_request
def release_report_publication(error):
    from flask import g
    context = g.pop('report_publication_context', None)
    if context is not None:
        context.__exit__(None, None, None)


def _request_accepts_gzip() -> bool:
    qualities: dict[str, float] = {}
    for item in str(request.headers.get("Accept-Encoding") or "").split(","):
        parts = [part.strip() for part in item.split(";")]
        encoding = parts[0].lower()
        if not encoding:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            name, separator, value = parameter.partition("=")
            if separator and name.strip().lower() == "q":
                try:
                    quality = max(0.0, min(1.0, float(value.strip())))
                except ValueError:
                    quality = 0.0
        qualities[encoding] = quality
    return qualities.get("gzip", qualities.get("*", 0.0)) > 0


@app.after_request
def secure_and_compress_response(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), geolocation=(), microphone=(self)",
    )
    parent_origin = _configured_crm_parent_origin()
    if parent_origin:
        frame_ancestors = f"frame-ancestors 'self' {parent_origin}"
        current_csp = str(response.headers.get("Content-Security-Policy") or "").strip()
        if "frame-ancestors" not in current_csp.lower():
            response.headers["Content-Security-Policy"] = (
                f"{current_csp.rstrip(';')}; {frame_ancestors}" if current_csp else frame_ancestors
            )

    content_type = str(response.content_type or "").split(";", 1)[0].lower()
    compressible = (
        content_type.startswith("text/")
        or content_type
        in {
            "application/javascript",
            "application/json",
            "application/manifest+json",
            "application/xml",
            "image/svg+xml",
        }
    )
    if (
        request.method == "HEAD"
        or response.status_code < 200
        or response.status_code in {204, 206, 304}
        or response.direct_passthrough
        or response.headers.get("Content-Encoding")
        or not compressible
        or not _request_accepts_gzip()
    ):
        return response

    body = response.get_data()
    if len(body) < 1024:
        return response
    compressed = gzip.compress(body, compresslevel=6, mtime=0)
    if len(compressed) >= len(body):
        return response
    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(compressed))
    response.vary.add("Accept-Encoding")
    return response


@app.errorhandler(413)
def request_entity_too_large(_error):
    return jsonify(
        {
            "ok": False,
            "message": f"Файл слишком большой. Максимальный размер загрузки — {MAX_UPLOAD_MB} МБ.",
        }
    ), 413

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs>
  <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="#5ea2ff"/>
    <stop offset="100%" stop-color="#5ecf8a"/>
  </linearGradient>
</defs>
<rect x="6" y="6" width="52" height="52" rx="14" fill="#0f1830"/>
<path d="M20 16h16l8 8v20a4 4 0 0 1-4 4H20a4 4 0 0 1-4-4V20a4 4 0 0 1 4-4z" fill="url(#g)"/>
<path d="M36 16v8h8" fill="none" stroke="#e8eefc" stroke-width="3" stroke-linejoin="round"/>
<path d="M24 30h12M24 36h10" stroke="#e8eefc" stroke-width="3" stroke-linecap="round"/>
<circle cx="42" cy="42" r="8" fill="#0f1830" stroke="#e8eefc" stroke-width="3"/>
<path d="M47.5 47.5L53 53" stroke="#e8eefc" stroke-width="3" stroke-linecap="round"/>
</svg>"""

# Совпадает с NEEDED_STAGE в main.py — единственная стадия, которую подсвечиваем зелёным.
STAGE_SUBMISSION = "Подача заявок"

parse_state = {
    "running": False,
    "task": "",
    "command": "",
    "started_at": None,
    "ended_at": None,
    "exit_code": None,
    "log_lines": [],
}
parse_lock = threading.Lock()

merge_site_state: dict = {
    "running": False,
    "total": 0,
    "done": 0,
    "current_tid": "",
    "market_done": 0,
    "market_total": 0,
    "last_market_chat_done": 0,
    "started_at": None,
    "ended_at": None,
    "error_ids": [],
    "log_lines": [],
    "chat_events": [],
    "last_ended_at": None,
    "last_summary": "",
    "last_reason_counts": {},
}
merge_site_lock = threading.Lock()

estimate_upload_jobs: dict[str, dict] = {}
estimate_upload_lock = threading.Lock()
estimate_upload_workers: set[str] = set()
tender_delete_lock = threading.Lock()


def _agent_market_token() -> str:
    from autobot.agent_market_queue import get_or_create_worker_token

    try:
        return get_or_create_worker_token()
    except OSError:
        return ""


def _agent_market_authorized() -> bool:
    expected = _agent_market_token()
    if not expected:
        return False
    authorization = str(request.headers.get("Authorization") or "").strip()
    supplied = authorization[7:].strip() if authorization.casefold().startswith("bearer ") else ""
    if not supplied:
        supplied = str(request.headers.get("X-AutoBot-Agent-Token") or "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _require_agent_market_token():
    if not _agent_market_token():
        return jsonify({"ok": False, "message": "MARKET_AGENT_TOKEN не настроен в AutoBot"}), 503
    if not _agent_market_authorized():
        return jsonify({"ok": False, "message": "Неверный токен агента"}), 401
    return None


def _agent_offer_url(value: object) -> str:
    raw = html_mod.unescape(str(value or "")).strip()
    match = re.search(r"https?://[^\s<>\]\[\)\}\"']+", raw, flags=re.IGNORECASE)
    url = (match.group(0) if match else raw).rstrip(".,;:")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return ""
    return url[:2000]


def _agent_offer_price(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        raw = str(value or "").replace("\xa0", " ").replace("\u202f", " ")
        match = re.search(r"\d[\d\s]*(?:[,.]\d{1,2})?", raw)
        if not match:
            return None
        normalized = match.group(0).replace(" ", "").replace(",", ".")
        try:
            number = float(normalized)
        except ValueError:
            return None
    return number if math.isfinite(number) and 1 <= number <= 500_000_000 else None


def _validate_agent_market_result(result: object, expected_position_key: str) -> dict:
    if not isinstance(result, dict):
        raise ValueError("Результат должен быть JSON-объектом")
    returned_key = str(result.get("position_key") or expected_position_key).strip()
    if returned_key != expected_position_key:
        raise ValueError("Агент вернул результат для другой позиции")
    offers: list[dict] = []
    seen_urls: set[str] = set()
    for raw_offer in list(result.get("offers") or [])[:20]:
        if not isinstance(raw_offer, dict):
            continue
        currency = str(raw_offer.get("currency") or "RUB").strip().upper()
        if currency not in {"RUB", "RUR", "₽", "РУБ", "РУБ."}:
            continue
        price = _agent_offer_price(raw_offer.get("price"))
        url = _agent_offer_url(raw_offer.get("url"))
        if price is None or not url or url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            confidence = float(raw_offer.get("confidence") or 0.45)
        except (TypeError, ValueError):
            confidence = 0.45
        offers.append(
            {
                "title": re.sub(r"\s+", " ", str(raw_offer.get("title") or "Источник цены")).strip()[:500],
                "price": price,
                "currency": "RUB",
                "unit": re.sub(r"\s+", " ", str(raw_offer.get("unit") or "")).strip()[:80],
                "url": url,
                "evidence": str(raw_offer.get("evidence") or raw_offer.get("snippet") or "").strip()[:1600],
                "observed_at": str(raw_offer.get("observed_at") or "").strip()[:80],
                "published_at": str(raw_offer.get("published_at") or "").strip()[:120],
                "location": re.sub(r"\s+", " ", str(raw_offer.get("location") or "")).strip()[:250],
                "price_scope": re.sub(r"\s+", " ", str(raw_offer.get("price_scope") or "")).strip()[:120],
                "confidence": max(0.0, min(1.0, confidence)),
            }
        )
        if len(offers) >= 10:
            break
    return {
        "schema_version": 1,
        "position_key": expected_position_key,
        "offers": offers,
        "notes": str(result.get("notes") or "").strip()[:2000],
        "observed_at": str(result.get("observed_at") or "").strip()[:80],
    }


def _telegram_cfg() -> tuple[str, str] | None:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if token and chat:
        return token, chat
    return None


def _tg_send(text: str) -> None:
    cfg = _telegram_cfg()
    if not cfg:
        return
    try:
        from autobot.telegram_notify import send_message

        send_message(cfg[0], cfg[1], text, parse_mode="HTML", disable_web_page_preview=False)
    except Exception:
        pass


def _tg_flush_spool() -> None:
    """Догнать outbox Telegram до отправки следующих сообщений (иначе порядок в чате ломается)."""
    cfg = _telegram_cfg()
    if not cfg:
        return
    try:
        from autobot.telegram_notify import flush_spooled_messages

        flush_spooled_messages(cfg[0])
    except Exception:
        pass


def _merge_chat_add(kind: str, text: str, *, tender_id: str = "", seq: int = 0, total: int = 0) -> None:
    event = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": "web",
        "kind": kind,
        "text": (text or "").strip()[:700],
        "tender_id": (tender_id or "").strip(),
        "seq": int(seq or 0),
        "total": int(total or 0),
    }
    if not event["text"]:
        return
    with merge_site_lock:
        events = list(merge_site_state.get("chat_events") or [])
        events.append(event)
        merge_site_state["chat_events"] = events[-120:]


def _market_web_events_path(tender_id: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", (tender_id or "unknown").strip())[:80] or "unknown"
    return REPO_ROOT / "data" / "logs" / f"market_web_events_{safe}.jsonl"


def _read_market_web_events(tender_id: str, *, limit: int = 80) -> list[dict]:
    paths = [_market_web_events_path(tender_id)]
    raw_lines: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            raw_lines.extend(path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:])
        except OSError:
            continue
    if not raw_lines:
        return []
    out: list[dict] = []
    for line in raw_lines[-limit * 2 :]:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        kind = str(ev.get("kind") or "")
        seq = int(ev.get("seq") or 0)
        total = int(ev.get("total") or 0)
        work = str(ev.get("work_name") or "").strip()
        detail = str(ev.get("detail") or "").strip()
        tid = str(ev.get("tender_id") or tender_id or "").strip()
        source = str(ev.get("source") or "").strip().lower()
        if source == "market" and str(ev.get("text") or "").strip():
            text = str(ev.get("text") or "").strip()
        elif kind == "begin":
            text = f"Работа {seq} из {total} началась" + (f": {work}" if work else "")
        elif kind == "done":
            text = f"✅ {seq}/{total} · готово."
        elif kind == "warn":
            text = f"⚠️ {seq}/{total} · пустой ответ" + (f": {work}" if work else "")
        elif kind == "error":
            text = f"⚠️ {seq}/{total} · ошибка" + (f": {detail}" if detail else "")
        else:
            text = str(ev.get("text") or "").strip()
        if not text:
            continue
        out.append(
            {
                "ts": str(ev.get("ts") or ""),
                "source": source or "market",
                "kind": kind,
                "text": text[:700],
                "tender_id": tid,
                "seq": seq,
                "total": total,
            }
        )
    return out


def eis_notice_url(tender_id: str, stored_url: str | None) -> str:
    """Ссылка на карточку закупки: из tenders.json или запасной URL по regNumber."""
    u = (stored_url or "").strip()
    if u.startswith("http://") or u.startswith("https://"):
        return u
    tid = (tender_id or "").strip()
    if not tid:
        return ""
    return (
        "https://zakupki.gov.ru/epz/order/notice/zk20/view/common-info.html"
        f"?regNumber={quote(tid, safe='')}"
    )


INDEX_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  {% if embed_mode %}<base target="_top" />{% endif %}
  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  <title>Помощник по госзакупкам</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect x='8' y='10' width='34' height='44' rx='8' fill='%23121a30' stroke='%236db7ff' stroke-width='3'/%3E%3Cpath d='M18 22h14M18 30h14M18 38h10' stroke='%239fd2ff' stroke-width='3' stroke-linecap='round'/%3E%3Ccircle cx='45' cy='42' r='10' fill='none' stroke='%235ecf8a' stroke-width='4'/%3E%3Cpath d='M52 49l6 6' stroke='%235ecf8a' stroke-width='4' stroke-linecap='round'/%3E%3C/svg%3E" />
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: #ffffff;
      --panel-soft: #f7fafe;
      --border: #d8e2ef;
      --border-soft: #e7edf5;
      --text: #172235;
      --muted: #61748c;
      --muted-soft: #75859a;
      --accent: #1f72dc;
      --accent-2: #195fba;
      --accent-bright: #4d9bff;
      --ok: #2e8b57;
      --danger: #cf5a5a;
      --shadow: 0 16px 42px rgba(28, 49, 84, 0.08);
    }
    html, body { min-height: 100%; margin: 0; box-sizing: border-box; }
    *, *::before, *::after { box-sizing: inherit; }
    body { font-family: "Segoe UI", Arial, sans-serif; background: linear-gradient(180deg, #ffffff 0%, var(--bg) 100%); color: var(--text); }
    .page { max-width: 1220px; margin: 0 auto; padding: 26px 18px 44px; display: flex; flex-direction: column; }
    .page > .hero-title, .page > h1, .page > .sub { order: 0; }
    .page > .action-hub { order: 1; }
    .page > #reportCoverageBanner { order: 2; }
    .page > .tenders-section { order: 3; }
    .page > .help-section { order: 4; }
    .page > .tool-section { order: 5; }
    .hero-title { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
    .hero-mark {
      width: 42px; height: 42px; flex: 0 0 42px;
      display: inline-flex; align-items: center; justify-content: center;
      border-radius: 12px;
      background: linear-gradient(180deg, rgba(57, 126, 209, 0.22), rgba(40, 93, 164, 0.3));
      border: 1px solid rgba(109, 183, 255, 0.35);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.06), 0 10px 24px rgba(0, 0, 0, 0.18);
    }
    .hero-mark svg { width: 26px; height: 26px; display: block; }
    h1 { margin: 0 0 6px 0; font-size: 1.45rem; font-weight: 700; letter-spacing: -0.02em; line-height: 1.15; }
    .section-title { font-size: 1.1rem; font-weight: 700; color: #e0eaff; margin: 0 0 7px 0; letter-spacing: -0.01em; }
    .section-lead { color: var(--muted); font-size: 13px; margin: 0 0 12px 0; line-height: 1.45; max-width: 72ch; }
    .sub { color: var(--muted); font-size: 13px; margin: 0 0 18px 0; line-height: 1.45; max-width: 62ch; }
    .meta { color: var(--muted); font-size: 12px; margin-bottom: 10px; line-height: 1.4; }
    .controls, .filters {
      border: 1px solid var(--border);
      border-radius: 12px;
      background: linear-gradient(180deg, var(--panel), var(--panel-soft));
      box-shadow: var(--shadow);
    }
    .controls { padding: 12px; margin-bottom: 14px; }
    .filters {
      padding: 8px 12px;
      margin-bottom: 10px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px 14px;
      font-size: 12px;
      color: #b8c7ea;
    }
    .filters label { display:flex; align-items:center; gap:8px; cursor:pointer; font-weight: 600; }
    .filters input[type="checkbox"] { width: 15px; height: 15px; accent-color: var(--ok); }
    .filters select {
      background:#0b1223;
      border:1px solid var(--border-soft);
      color:var(--text);
      border-radius:8px;
      padding:6px 8px;
      font-size:12px;
      outline:none;
    }
    .filters select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(75, 101, 187, 0.2);
    }
    .filters .muted { color: var(--muted-soft); }
    .filters a { color: #87bbff; font-size: 12px; text-decoration: none; }
    .filters a:hover { text-decoration: underline; color: #b8d4ff; }
    .btn {
      border:1px solid var(--accent);
      background: linear-gradient(180deg, #397ed1, #285da4);
      color:#ecf2ff;
      border-radius:8px;
      padding:7px 11px;
      cursor:pointer;
      font-size:12px;
      font-weight: 600;
      transition: transform .14s ease, filter .14s ease;
    }
    .btn:hover { transform: translateY(-1px); filter: brightness(1.08); }
    .btn.secondary { border-color:#4a567e; background: linear-gradient(180deg, #2d3853, #283247); }
    .btn:disabled { opacity:.58; cursor:not-allowed; transform:none; filter:none; }
    .btn-lg { padding: 10px 18px; font-size: 13px; border-radius: 10px; }
    .action-bar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 4px; }
    .controls-hint { font-size: 12px; color: var(--muted-soft); margin: 10px 0 0 0; line-height: 1.4; }
    details.advanced {
      margin-top: 14px;
      border: 1px solid var(--border-soft);
      border-radius: 11px;
      background: rgba(10, 14, 28, 0.55);
    }
    details.advanced > summary {
      list-style: none;
      cursor: pointer;
      padding: 11px 14px;
      font-size: 12px;
      color: #a8b8e6;
      user-select: none;
    }
    details.advanced > summary::-webkit-details-marker { display: none; }
    details.advanced[open] > summary {
      color: #d2defa;
      border-bottom: 1px solid var(--border-soft);
    }
    details.advanced .advanced-body { padding: 12px 14px 14px; }
    .link-refresh {
      font-size: 12px;
      color: var(--muted-soft);
      margin-left: 6px;
      text-decoration: none;
      align-self: center;
    }
    .link-refresh:hover { color: #c8d8f8; text-decoration: underline; }
    .stat-strip { line-height: 1.5; }
    .page-footer { margin-top: 22px; font-size: 11px; color: var(--muted-soft); text-align: center; }
    .btn-row { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; align-items:center; }
    .opts { display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap:8px; margin-top:8px; font-size:12px; color:#b8c7ea; }
    .opts label { display:flex; flex-direction:column; gap:4px; }
    .opts input, .link-row input, .rebuild-row select {
      background:#0b1223;
      border:1px solid var(--border-soft);
      color:var(--text);
      border-radius:8px;
      padding:7px 9px;
      font-size:12px;
      outline: none;
    }
    .opts input:focus, .link-row input:focus, .rebuild-row select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(75, 101, 187, 0.2);
    }
    .link-row, .rebuild-row { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-top:10px; font-size:12px; color:#b8c7ea; }
    .link-row input, .rebuild-row select { flex:1; min-width:220px; max-width:100%; }
    .status { margin-top:8px; font-size:12px; color:#b8c7ea; }
    .logs, .tender-grid {
      margin-top:8px;
      max-height:165px;
      overflow:auto;
      border:1px solid var(--border-soft);
      border-radius:10px;
      background:#0b1223;
      padding:8px;
      font-size:12px;
    }
    .logs { font-family: Consolas, monospace; white-space:pre-wrap; }
    .parse-bar-wrap { height: 10px; background: #0b1223; border-radius: 8px; border: 1px solid var(--border-soft); overflow: hidden; margin-top: 6px; }
    .parse-bar-fill { height: 100%; width: 0%; background: linear-gradient(90deg, #3d5290, #5ecf8a); transition: width .28s ease; }
    .parse-bar-fill.running { width: 65%; animation: parseIndeterminate 1.3s ease-in-out infinite; }
    @keyframes parseIndeterminate {
      0% { transform: translateX(-45%); width: 35%; }
      50% { transform: translateX(10%); width: 55%; }
      100% { transform: translateX(120%); width: 30%; }
    }
    .region-block {
      margin-bottom: 12px;
      padding: 10px 12px 12px;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: linear-gradient(180deg, #121a31, #10182c);
      box-shadow: var(--shadow);
    }
    .region-title { font-size: 13px; font-weight: 700; color: #d2defa; margin: 0 0 9px 0; padding-bottom: 6px; border-bottom: 1px solid #243356; }
    .tender-filter-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px 12px;
      margin: 4px 0 14px;
      padding: 10px 12px;
      border: 1px solid var(--border-soft);
      border-radius: 10px;
      background: rgba(8, 12, 24, 0.42);
      color: #b8c7ea;
      font-size: 12px;
    }
    .tender-filter-row label { display: flex; align-items: center; gap: 8px; font-weight: 700; }
    .tender-filter-row select {
      min-width: 230px;
      max-width: 100%;
      background: #0b1223;
      border: 1px solid var(--border-soft);
      color: var(--text);
      border-radius: 8px;
      padding: 7px 9px;
      font-size: 12px;
      outline: none;
    }
    .tender-filter-row select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(75, 101, 187, 0.2);
    }
    .tender-filter-row a { color: #87bbff; text-decoration: none; }
    .tender-filter-row a:hover { text-decoration: underline; color: #b8d4ff; }
    .tender-group { margin-top: 14px; }
    .tender-group:first-of-type { margin-top: 8px; }
    .tender-group-title {
      margin: 0 0 10px;
      padding: 0 0 7px;
      border-bottom: 1px solid #243356;
      color: #d2defa;
      font-size: 15px;
      font-weight: 800;
      letter-spacing: -0.01em;
      line-height: 1.3;
    }
    .tender-group-body { margin-top: 0; }
    .tender-grid-main { display: grid; grid-template-columns: repeat(auto-fill, minmax(285px, 1fr)); gap: 9px; }
    .tender-cell { display: flex; flex-direction: column; gap: 5px; position: relative; min-width: 0; }
    .tender-card {
      display: flex;
      flex-direction: column;
      gap: 2px;
      padding: 12px;
      border-radius: 14px;
      border: 1px solid #35508d;
      background:
        linear-gradient(180deg, rgba(109, 183, 255, 0.12), rgba(109, 183, 255, 0) 32%),
        linear-gradient(145deg, #1d294a, #141d34);
      color: var(--text);
      transition: transform .15s ease, border-color .15s, box-shadow .15s, background .15s ease;
      min-height: 0;
      min-width: 0;
      overflow: hidden;
      box-shadow: 0 10px 26px rgba(0,0,0,.22);
    }
    .tender-card:hover {
      transform: translateY(-2px);
      border-color: #81b8ff;
      box-shadow: 0 14px 30px rgba(5, 10, 25, 0.34);
      background:
        linear-gradient(180deg, rgba(109, 183, 255, 0.18), rgba(109, 183, 255, 0.02) 32%),
        linear-gradient(145deg, #212f56, #17213b);
    }
    .tender-card[data-href] { cursor: pointer; }
    .tender-card.no-data { border-left: 3px solid var(--danger); }
    .tender-card-link {
      display: block;
      min-width: 0;
      text-decoration: none;
      color: inherit;
      padding-right: 34px;
    }
    .tender-card-link--more { flex: 1 1 auto; margin-top: 2px; }
    .tender-card .title {
      font-size: 14px;
      font-weight: 750;
      line-height: 1.35;
      max-height: 4.05em;
      overflow: hidden;
      word-break: break-word;
      color: #f4f8ff;
      text-shadow: 0 1px 0 rgba(0,0,0,.18);
    }
    .tender-card-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-top: 6px;
      min-width: 0;
    }
    .tender-card-sub {
      flex: 1 1 auto;
      min-width: 0;
      text-decoration: none;
      color: inherit;
    }
    .tender-card-sub:hover .tid { color: #c8d8f8; }
    .tender-card .tid { font-size: 11px; color: var(--muted); margin-top: 0; word-break: break-word; line-height: 1.35; }
    .tender-card-meta {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: 10px;
    }
    .tender-meta-item {
      min-width: 0;
      padding: 9px 10px;
      border-radius: 10px;
      background: linear-gradient(180deg, rgba(17, 28, 53, 0.92), rgba(9, 16, 31, 0.86));
      border: 1px solid rgba(109, 183, 255, 0.2);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
    }
    .tender-meta-item--wide { grid-column: 1 / -1; }
    .tender-meta-label {
      display: block;
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      color: #92a7d6;
      margin-bottom: 5px;
    }
    .tender-meta-value {
      display: block;
      font-size: 13px;
      color: #edf3ff;
      line-height: 1.35;
      word-break: break-word;
      font-weight: 650;
    }
    .tender-meta-value--mono { font-variant-numeric: tabular-nums; color: #d9e7ff; }
    .tender-card-pub {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px 10px;
      margin-top: 11px;
      padding: 8px 10px;
      border-radius: 11px;
      background: rgba(10, 18, 34, 0.46);
      border: 1px solid rgba(109, 183, 255, 0.14);
    }
    .tender-card-pub-label {
      font-size: 9px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      color: #7d8fbb;
    }
    .tender-card-pub-date {
      display: inline-block;
      font-size: 12px;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      color: #f0f7ff;
      background: linear-gradient(180deg, rgba(88, 118, 210, 0.42), rgba(52, 72, 140, 0.55));
      border: 1px solid rgba(130, 160, 230, 0.45);
      border-radius: 999px;
      padding: 4px 11px;
      line-height: 1.2;
      box-shadow: 0 0 0 1px rgba(0,0,0,.12) inset;
    }
    .tender-status-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px 8px;
      margin-top: 10px;
      min-width: 0;
    }
    .tender-status-row .eis-in-card { margin-left: auto; }
    .tender-progress {
      margin-top: 8px;
      padding: 7px 8px;
      border-radius: 9px;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(140, 172, 220, 0.2);
    }
    .tender-progress-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 10px;
      color: #c7d6ef;
      margin-bottom: 5px;
    }
    .tender-progress-label { font-weight: 700; letter-spacing: 0.02em; }
    .tender-progress-value { color: #ecf2ff; font-weight: 700; font-variant-numeric: tabular-nums; }
    .tender-progress-track {
      height: 6px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.04);
    }
    .tender-progress-fill {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #4b7dff, #5ecf8a);
    }
    .tender-progress-note {
      margin-top: 5px;
      font-size: 10px;
      color: #9fb2d7;
      line-height: 1.35;
    }
    .eis-in-card {
      display: inline-flex;
      align-items: center;
      flex-shrink: 0;
      font-size: 11px;
      font-weight: 600;
      color: #b8d8ff;
      text-decoration: none;
      padding: 3px 8px;
      border-radius: 7px;
      background: rgba(20, 32, 64, 0.65);
      border: 1px solid rgba(100, 140, 220, 0.35);
      white-space: nowrap;
    }
    .eis-in-card:hover { background: rgba(75, 101, 187, 0.35); color: #fff; border-color: rgba(140, 175, 255, 0.55); }
    .tag { font-size: 11px; padding: 4px 8px; border-radius: 999px; font-weight: 700; letter-spacing: .1px; }
    .tag-ok { background: #1e4d35; color: #9df0b8; }
    .tag-nodata { background: #5a1a22; color: #ffc9cc; border: 1px solid #a04048; }
    .tag-stage-open { background: #1e4d35; color: #9df0b8; border: 1px solid #3d8a67; }
    .tag-stage-closed { background: #5a1a22; color: #ffc9cc; border: 1px solid #a04048; }
    .tender-menu-wrap { position: absolute; top: 7px; right: 7px; z-index: 3; }
    .tender-menu-btn {
      width: 30px;
      height: 30px;
      border-radius: 8px;
      border: 1px solid #d7e2ec;
      background: #ffffff;
      color: #1f334d;
      cursor: pointer;
      font-weight: 700;
      font-size: 16px;
      line-height: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 4px 12px rgba(15, 23, 42, 0.08);
    }
    .tender-menu-btn:hover { background: #f7fafc; border-color: #c3d4e6; color: #10263d; }
    .tender-menu-wrap > summary {
      list-style: none;
      display: block;
      cursor: pointer;
      user-select: none;
    }
    .tender-menu-wrap > summary::-webkit-details-marker { display: none; }
    .tender-menu {
      display: none;
      position: absolute;
      top: 34px;
      right: 0;
      min-width: 260px;
      background: #ffffff;
      border: 1px solid #d7e2ec;
      border-radius: 10px;
      padding: 6px;
      box-shadow: 0 14px 30px rgba(15, 23, 42, 0.14);
    }
    .tender-menu-wrap[open] .tender-menu,
    .tender-menu-wrap.menu-open .tender-menu { display: block; }
    .tender-menu button {
      width: 100%;
      text-align: left;
      background: transparent;
      color: #1f334d;
      border: none;
      padding: 8px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 12px;
    }
    .tender-menu button:hover { background: #f4f8fc; }
    .parse-progress-panel {
      margin-top: 18px; padding: 18px 20px; border-radius: 15px;
      background: linear-gradient(135deg, rgba(34, 57, 101, 0.96), rgba(18, 29, 53, 0.98));
      border: 1px solid rgba(109, 183, 255, 0.58);
      box-shadow: 0 14px 34px rgba(0, 0, 0, 0.28);
    }
    .parse-progress-panel[hidden] { display: none !important; }
    .parse-progress-head { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; font-size: 16px; color: #e4edff; }
    .parse-pulse { width: 12px; height: 12px; border-radius: 50%; background: #5ecf8a; flex-shrink: 0; animation: parsePulse 1.2s ease-in-out infinite; box-shadow: 0 0 12px #5ecf8a; }
    @keyframes parsePulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.55; transform: scale(0.92); } }
    .parse-progress-time { margin-top: 9px; font-size: 14px; color: #9df0b8; font-variant-numeric: tabular-nums; }
    .parse-progress-hint { font-size: 12px; color: #b4c4e5; margin-top: 8px; line-height: 1.45; }
    .parse-status-line { margin-top: 8px; font-size: 12px; color: #9aabd0; word-break: break-all; }
    .parse-summary {
      margin-top: 14px;
      padding: 14px 15px;
      border-radius: 12px;
      background: rgba(9, 16, 31, 0.42);
      border: 1px solid rgba(109, 183, 255, 0.2);
    }
    .parse-summary-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .parse-summary-item {
      padding: 11px 12px;
      border-radius: 10px;
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(255,255,255,0.05);
      min-width: 0;
    }
    .parse-summary-label {
      font-size: 11px;
      color: var(--muted-soft);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-bottom: 6px;
    }
    .parse-summary-value { font-size: 14px; color: #ecf2ff; line-height: 1.4; word-break: break-word; }
    .parse-summary-value.ok { color: #9df0b8; }
    .parse-summary-value.warn { color: #ffd7a8; }
    .parse-summary-value.bad { color: #ffc9cc; }
    details.compact-details {
      margin-top: 12px;
      border: 1px solid rgba(109, 183, 255, 0.18);
      border-radius: 10px;
      background: rgba(9, 16, 31, 0.24);
    }
    details.compact-details > summary {
      list-style: none;
      cursor: pointer;
      padding: 10px 12px;
      color: #b8c7ea;
      font-size: 13px;
      user-select: none;
    }
    details.compact-details > summary::-webkit-details-marker { display: none; }
    details.compact-details .logs { margin: 0 10px 10px; }
    details.compact-details .parse-status-line { margin: 0 10px 8px; }
    .merge-bar-wrap { height: 12px; background: #0f1324; border-radius: 8px; overflow: hidden; margin-top: 10px; border: 1px solid #2b365e; }
    .merge-bar-fill { height: 100%; background: linear-gradient(90deg, #3d5290, #5ecf8a); transition: width 0.35s ease; border-radius: 8px; }
    .merge-logs { margin-top: 8px; max-height: 140px; overflow: auto; border: 1px solid #2b365e; border-radius: 8px; background: #0f1324; padding: 8px; font-family: Consolas, monospace; font-size: 11px; white-space: pre-wrap; }
    .site-chat-fab {
      position: fixed; right: 18px; bottom: 18px; z-index: 50;
      width: 54px; height: 54px; border-radius: 999px;
      border: 1px solid rgba(109, 183, 255, 0.62);
      background: linear-gradient(180deg, #397ed1, #285da4);
      color: #fff; cursor: pointer; box-shadow: 0 14px 32px rgba(0,0,0,.38);
      display: flex; align-items: center; justify-content: center; font-size: 23px;
    }
    .site-chat-fab.has-new::after {
      content: ""; position: absolute; right: 7px; top: 7px;
      width: 10px; height: 10px; border-radius: 999px; background: #5ecf8a;
      box-shadow: 0 0 0 3px rgba(94,207,138,.22);
    }
    .site-chat-panel {
      position: fixed; right: 18px; bottom: 84px; z-index: 49;
      width: min(390px, calc(100vw - 28px)); max-height: min(560px, calc(100vh - 120px));
      border: 1px solid rgba(109, 183, 255, 0.42);
      border-radius: 15px; overflow: hidden;
      background: linear-gradient(180deg, #121a30, #0e1528);
      box-shadow: 0 18px 46px rgba(0,0,0,.46);
    }
    .site-chat-panel[hidden] { display: none !important; }
    .site-chat-head {
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      padding: 11px 12px; border-bottom: 1px solid var(--border-soft);
      color: #e4edff; font-size: 13px; font-weight: 750;
    }
    .site-chat-close {
      border: 1px solid #3a4677; background: rgba(15,19,36,.85);
      color: #c8d8f8; border-radius: 8px; cursor: pointer; padding: 4px 8px;
    }
    .site-chat-feed {
      max-height: 430px; overflow: auto; padding: 10px;
      display: flex; flex-direction: column; gap: 8px;
    }
    .site-chat-empty { color: var(--muted-soft); font-size: 12px; line-height: 1.45; padding: 4px 2px 8px; }
    .site-chat-msg {
      border: 1px solid rgba(109, 183, 255, 0.13); border-radius: 11px;
      background: rgba(8, 12, 24, 0.48); padding: 8px 9px;
    }
    .site-chat-msg.is-done { border-color: rgba(94,207,138,.32); }
    .site-chat-msg.is-error, .site-chat-msg.is-warn { border-color: rgba(255,201,204,.28); }
    .site-chat-meta { color: #7d8fbb; font-size: 10px; margin-bottom: 4px; font-variant-numeric: tabular-nums; }
    .site-chat-text { color: #edf3ff; font-size: 12px; line-height: 1.4; white-space: pre-wrap; word-break: break-word; }
    .cov-banner { padding: 13px 16px; border-radius: 12px; margin-bottom: 16px; font-size: 13px; line-height: 1.5; }
    .cov-warn { background: rgba(90, 26, 34, 0.45); border: 1px solid #a04048; color: #ffc9cc; }
    .cov-partial { background: rgba(77, 53, 30, 0.45); border: 1px solid #8a623d; color: #ffd7a8; }
    .cov-ok { background: rgba(30, 77, 53, 0.35); border: 1px solid #3d8a67; color: #9df0b8; }
    .workflow-strip {
      display: none; flex-wrap: wrap; align-items: center; gap: 8px 12px;
      margin-bottom: 20px; padding: 13px 15px; border-radius: 12px;
      background: rgba(10, 14, 28, 0.55); border: 1px solid var(--border-soft);
      font-size: 13px; color: #c4d2ef;
    }
    .wf-step { display: flex; align-items: center; gap: 8px; font-weight: 600; }
    .wf-num {
      display: inline-flex; align-items: center; justify-content: center;
      width: 28px; height: 28px; border-radius: 999px;
      background: linear-gradient(180deg, #397ed1, #285da4); color: #ecf2ff;
      font-size: 12px; font-weight: 800;
    }
    .wf-arrow { color: #607dce; font-weight: 700; }
    .action-hub, .tenders-section, .tool-section {
      margin-bottom: 14px;
      padding: 14px;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: linear-gradient(180deg, var(--panel), var(--panel-soft));
      box-shadow: var(--shadow);
    }
    .action-hub {
      position: relative;
      overflow: hidden;
      border-color: rgba(109, 183, 255, 0.48);
      background:
        linear-gradient(135deg, rgba(25, 42, 77, 0.98), rgba(13, 23, 44, 0.99));
    }
    .action-hub::before {
      content: ""; position: absolute; inset: 0 auto auto 0; width: 100%; height: 3px;
      background: linear-gradient(90deg, var(--accent-bright), var(--ok), transparent 78%);
    }
    .action-grid { display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: 10px; }
    .action-card {
      grid-column: span 6;
      padding: 12px;
      border-radius: 10px;
      border: 1px solid var(--border-soft);
      background: linear-gradient(145deg, rgba(18, 29, 53, 0.92), rgba(10, 17, 33, 0.9));
      box-shadow: 0 8px 20px rgba(0, 0, 0, 0.18);
    }
    .action-card:nth-child(1) { border-top: 3px solid var(--accent-bright); }
    .action-card:nth-child(2) { border-top: 3px solid var(--ok); }
    .action-card--wide { grid-column: span 8; }
    .action-card:last-child { grid-column: span 4; }
    .action-card-title { margin: 0 0 7px 0; font-size: 14px; font-weight: 700; color: #e2ebff; line-height: 1.35; }
    .action-card-desc { margin: 0 0 10px 0; font-size: 12px; color: #9fb0d6; line-height: 1.45; }
    .action-card .btn-row { margin-top: 0; }
    .action-card .opts { margin-top: 10px; }
    .action-card > .btn.btn-lg { width: 100%; }
    .action-card .btn-row .btn-lg { flex: 1 1 260px; }
    .tender-actions {
      display: flex; flex-direction: column; gap: 8px;
      margin-top: 12px; padding-top: 12px;
      border-top: 1px solid rgba(76, 108, 181, 0.45);
    }
    .tender-act {
      display: inline-flex; align-items: center; justify-content: center;
      border: 1px solid #3a4677; background: rgba(15, 19, 36, 0.85);
      color: #d5e4ff; border-radius: 11px; padding: 9px 11px;
      font-size: 12px; font-weight: 700; cursor: pointer; text-decoration: none;
      line-height: 1.25; text-align: center;
    }
    .tender-act:hover { background: rgba(75, 101, 187, 0.35); color: #fff; border-color: rgba(140, 175, 255, 0.55); }
    .tender-act:disabled { opacity: 0.45; cursor: not-allowed; transform: none; }
    .tender-act--primary { border-color: var(--accent); background: linear-gradient(180deg, #334b93, #2a3f82); color: #ecf2ff; }
    .tender-act--main { width: 100%; min-height: 43px; font-size: 13px; padding: 10px 12px; }
    .tender-act--crm {
      width: 100%;
      min-height: 46px;
      font-size: 14px;
      border-color: #ffb64d;
      background: linear-gradient(180deg, #ffb84f, #ea8e1f);
      color: #241300;
      box-shadow: 0 10px 22px rgba(234, 142, 31, 0.28);
    }
    .tender-act--crm:hover {
      background: linear-gradient(180deg, #ffc364, #f39b27);
      border-color: #ffd089;
      color: #1b0f00;
      box-shadow: 0 12px 24px rgba(243, 155, 39, 0.34);
    }
    .tender-more-actions {
      display: grid; grid-template-columns: 1fr; gap: 5px; padding: 7px;
    }
    .tender-more-actions .tender-act { width: 100%; justify-content: flex-start; text-align: left; }
    .tender-menu .tender-act,
    .tender-menu .tender-act--primary,
    .tender-menu .tender-act--crm,
    .tender-menu .tender-act--main,
    .tender-menu .tender-act-btn {
      width: 100%;
      min-height: 0;
      justify-content: flex-start;
      text-align: left;
      padding: 9px 10px;
      border-radius: 8px;
      border: 1px solid #d7e2ec;
      background: #ffffff;
      color: #1f334d;
      box-shadow: none;
      font-size: 12px;
      font-weight: 600;
    }
    .tender-menu .tender-act:hover,
    .tender-menu .tender-act--primary:hover,
    .tender-menu .tender-act--crm:hover,
    .tender-menu .tender-act--main:hover,
    .tender-menu .tender-act-btn:hover {
      background: #f4f8fc;
      border-color: #c3d4e6;
      color: #10263d;
    }
    .tender-menu .tender-act:focus,
    .tender-menu .tender-act-btn:focus,
    .tender-menu-btn:focus {
      outline: 2px solid #c9ddff;
      outline-offset: 1px;
    }
    .tender-next { display: none; }
    details.tender-more { display: none; }
    .tender-card-link--disabled { cursor: default; }
    .tag-merge { background: #2a3a6e; color: #b8d4ff; border: 1px solid #4a67b8; }
    .tag-nomerge { background: #3a3048; color: #d0c4e8; border: 1px solid #5a4a72; }
    .help-section { display: none; }
    .help-section .section-title { color: #e8f0ff; margin-bottom: 8px; }
    .help-steps { margin: 0 0 14px 0; padding-left: 22px; color: #c8d8f8; font-size: 14px; line-height: 1.6; }
    .help-steps li { margin-bottom: 6px; }
    .help-steps strong { color: #fff; }
    details.help-glossary {
      border: 1px solid var(--border-soft); border-radius: 10px;
      background: rgba(8, 12, 24, 0.5); font-size: 12px; color: #b8c7ea;
    }
    details.help-glossary > summary {
      cursor: pointer; padding: 10px 12px; font-weight: 600; color: #a8c4ff;
      list-style: none;
    }
    details.help-glossary > summary::-webkit-details-marker { display: none; }
    details.help-glossary[open] > summary { border-bottom: 1px solid var(--border-soft); }
    .glossary-grid { display: grid; grid-template-columns: 110px 1fr; gap: 6px 12px; padding: 10px 12px 12px; line-height: 1.45; }
    .glossary-term { font-weight: 700; color: #d2defa; }
    .btn-effect {
      margin: 8px 0 0 0; padding: 8px 10px; border-radius: 8px;
      background: rgba(15, 22, 44, 0.85); border: 1px dashed #3a4677;
      font-size: 11px; color: #9fb0d6; line-height: 1.45;
    }
    .btn-effect strong { color: #c8e0ff; font-weight: 600; }
    .card-legend {
      margin: 0 0 12px 0; padding: 10px 12px; border-radius: 10px;
      border: 1px solid var(--border-soft); background: rgba(8, 12, 24, 0.45);
      font-size: 11px; color: #9fb0d6; line-height: 1.5;
    }
    .card-legend strong { color: #d2defa; }
    .tool-section--optional { border-style: dashed; opacity: 0.95; }
    .optional-badge {
      display: inline-block; font-size: 10px; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.06em; color: #ffd7a8; background: rgba(77, 53, 30, 0.5);
      border: 1px solid #8a623d; border-radius: 999px; padding: 2px 8px; margin-left: 8px;
    }
    .main-tabs {
      display:flex; flex-wrap:wrap; gap:8px; margin: 14px 0 18px;
    }
    .main-tab {
      display:inline-flex; align-items:center; gap:7px;
      padding:9px 12px; border-radius:999px; text-decoration:none;
      color:#c8d8f8; background:rgba(15, 22, 44, .72);
      border:1px solid var(--border-soft); font-size:13px; font-weight:700;
    }
    .main-tab:hover { color:#fff; border-color:#6d8fe8; background:rgba(49, 78, 145, .45); }
    .main-tab.is-active { color:#fff; background:linear-gradient(180deg, #345095, #263d78); border-color:#6d8fe8; }
    details.action-options {
      margin-top: 13px;
      border: 1px solid var(--border-soft);
      border-radius: 9px;
      background: rgba(8, 12, 24, 0.42);
    }
    details.action-options > summary {
      list-style: none; cursor: pointer; padding: 11px 13px;
      font-size: 12px; font-weight: 600; color: #a8b8e6; user-select: none;
    }
    details.action-options > summary::-webkit-details-marker { display: none; }
    details.action-options > summary::before { content: "Показать: "; color: #7891cc; }
    details.action-options[open] > summary {
      border-bottom: 1px solid var(--border-soft); color: #d2defa;
    }
    details.action-options .opts,
    details.action-options .rebuild-row { margin: 0; padding: 10px; }
    .hero-mark {
      background: linear-gradient(180deg, #eef5ff, #f8fbff);
      border-color: #bfd4ef;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.9), 0 10px 24px rgba(35, 74, 135, 0.08);
    }
    .section-title,
    .help-section .section-title,
    .glossary-term,
    .region-title,
    .tender-card .title,
    .tender-meta-value,
    .parse-progress-head,
    .parse-summary-value,
    .site-chat-text,
    .action-card-title,
    .help-steps strong,
    .tender-meta-value--mono {
      color: #1b2a41;
    }
    .controls,
    .filters,
    .action-card,
    .parse-progress,
    .region-card,
    .tender-card,
    .tender-meta-item,
    .tender-card-pub,
    .tender-progress,
    .merge-status-card,
    .site-chat,
    .workflow-note,
    .help-note,
    .glossary-card,
    .btn-effect,
    .card-legend,
    details.advanced,
    details.action-options,
    .tender-menu,
    .merge-logs,
    .status,
    .logs {
      background: #ffffff;
      border-color: var(--border);
      box-shadow: var(--shadow);
      color: var(--text);
    }
    .filters,
    .meta,
    .sub,
    .section-lead,
    .controls-hint,
    .page-footer,
    .tender-next,
    .parse-progress-hint,
    .parse-status-line,
    .parse-summary-label,
    .tender-card .tid,
    .tender-card-pub-label,
    .tender-meta-label,
    .site-chat-meta,
    .help-steps,
    .card-legend,
    .btn-effect,
    .status-line,
    .market-links-note {
      color: var(--muted);
    }
    .filters select,
    .opts input,
    .link-row input,
    .rebuild-row select,
    .tender-filter-row select,
    .tender-filter-row input,
    .type-picker,
    textarea,
    input[type="text"],
    input[type="file"],
    select {
      background: #ffffff;
      border-color: #cfd9e8;
      color: var(--text);
    }
    .btn,
    .main-tab.is-active,
    .tender-act--primary,
    .market-link-chip,
    .chip.is-active {
      background: linear-gradient(180deg, #2e80e8, #1d6fdc);
      border-color: #2e80e8;
      color: #ffffff;
    }
    .btn.secondary,
    .main-tab,
    .tender-act,
    .chip,
    .eis-in-card,
    .offer-source,
    .workflow-pill,
    .upload-step,
    .tag,
    .tag-merge,
    .tag-nomerge {
      background: #f4f8fd;
      border-color: #cfd9e8;
      color: #35506f;
    }
    .main-tab:hover,
    .tender-act:hover,
    .market-link-chip:hover,
    .chip:hover,
    .eis-in-card:hover {
      background: #eaf2fd;
      border-color: #9ec0ef;
      color: #173a65;
    }
    .tender-card,
    .tender-meta-item,
    .tender-card-pub,
    .parse-summary-item,
    .action-card {
      background: linear-gradient(180deg, #ffffff, #f8fbff);
      border-color: #d9e4f1;
    }
    .tender-card:hover {
      background: linear-gradient(180deg, #ffffff, #f2f7fd);
      border-color: #9ec0ef;
      box-shadow: 0 18px 34px rgba(43, 78, 131, 0.12);
    }
    .tag-ok,
    .tag-stage-open,
    .cov-ok {
      background: #e9f8ef;
      color: #257347;
      border-color: #bfe5cc;
    }
    .tag-nodata,
    .tag-stage-closed,
    .cov-warn {
      background: #fff1f1;
      color: #a94444;
      border-color: #f0c5c5;
    }
    .cov-partial {
      background: #fff8e8;
      color: #91621c;
      border-color: #f0deb1;
    }
    .parse-bar-wrap,
    .merge-bar-wrap {
      background: #edf3fa;
      border-color: #d6e0ee;
    }
    .merge-logs,
    .logs,
    .site-chat-msg,
    .parse-summary-item,
    .status-box {
      background: #f8fbff;
      border-color: #dfe7f1;
      color: var(--text);
    }
    .opts,
    .link-row,
    .rebuild-row,
    .status {
      color: var(--muted);
    }
    details.advanced > summary,
    details.action-options > summary {
      color: #35506f;
    }
    details.action-options > summary::before,
    details.advanced[open] > summary,
    .wf-arrow,
    .where {
      color: #5f7ca5;
    }
    @media (max-width: 980px) {
      .action-grid { grid-template-columns: 1fr; }
      .action-card, .action-card--wide, .action-card:last-child { grid-column: 1 / -1; }
      .opts { grid-template-columns: 1fr 1fr; }
    }
    @media (max-width: 720px) {
      .page { padding: 22px 12px 32px; }
      .hero-title { gap: 10px; }
      .hero-mark { width: 46px; height: 46px; flex-basis: 46px; border-radius: 14px; }
      .hero-mark svg { width: 28px; height: 28px; }
      h1 { font-size: 2rem; }
      .action-hub, .tenders-section, .tool-section { padding: 15px; border-radius: 14px; }
      .action-card { padding: 15px; }
      .parse-summary-grid { grid-template-columns: 1fr; }
      .opts { grid-template-columns: 1fr; }
      .tender-grid-main { grid-template-columns: 1fr; }
      .btn-row .btn { width: 100%; }
      .link-row, .rebuild-row { align-items: stretch; flex-direction: column; }
      .link-row input, .rebuild-row select { width: 100%; min-width: 0; }
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="hero-title">
      <span class="hero-mark" aria-hidden="true">
        <svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
          <rect x="10" y="9" width="30" height="42" rx="8" fill="#121a30" stroke="#6db7ff" stroke-width="3"/>
          <path d="M19 22H31" stroke="#9fd2ff" stroke-width="3" stroke-linecap="round"/>
          <path d="M19 30H31" stroke="#9fd2ff" stroke-width="3" stroke-linecap="round"/>
          <path d="M19 38H27" stroke="#9fd2ff" stroke-width="3" stroke-linecap="round"/>
          <circle cx="45" cy="42" r="10" stroke="#5ecf8a" stroke-width="4"/>
          <path d="M52 49L58 55" stroke="#5ecf8a" stroke-width="4" stroke-linecap="round"/>
        </svg>
      </span>
      <h1>Помощник по госзакупкам</h1>
    </div>
    <p class="sub" style="max-width:none;">Программа ищет закупки на <strong>zakupki.gov.ru</strong>, вытаскивает из документов <strong>смету</strong> (список работ и цен), ищет <strong>рыночные источники</strong> в интернете и показывает, где заказчик завысил или занизил.</p>
    <nav class="main-tabs" aria-label="Разделы сайта">
      <a class="main-tab is-active" href="/tenders">📋 Тендеры</a>
      <a class="main-tab" href="/estimates">📊 Сметы</a>
      <a class="main-tab" href="/research">🔎 Поиск по позиции</a>
    </nav>

    <section class="help-section" aria-labelledby="helpTitle">
      <h2 class="section-title" id="helpTitle">Как пользоваться — три шага</h2>
      <ol class="help-steps">
        <li><strong>Шаг 1.</strong> Нажмите «Найти новые закупки» — программа скачает документы и извлечёт сметы.</li>
        <li><strong>Шаг 2.</strong> Нажмите «Подготовить недостающие сравнения» — программа найдёт рыночные цены и ссылки на источники. <strong>Это долгий этап</strong>, он может идти часами.</li>
        <li><strong>Шаг 3.</strong> В готовой карточке нажмите «Посмотреть сравнение цен».</li>
      </ol>
      <details class="help-glossary">
        <summary>Словарь: что значат непонятные слова</summary>
        <div class="glossary-grid">
          <div class="glossary-term">Тендер</div>
          <div>Государственная закупка: кто дешевле выполнит работы — тот выиграет контракт.</div>
          <div class="glossary-term">ЕИС</div>
          <div>Официальный портал <strong>zakupki.gov.ru</strong>. «Скачать с ЕИС» = скачать с этого сайта.</div>
          <div class="glossary-term">Смета</div>
          <div>Таблица из документов: какие работы, объёмы и цены заложил заказчик.</div>
          <div class="glossary-term">Рыночные источники</div>
          <div>Объявления и страницы в интернете, откуда берём примерные цены по позициям сметы.</div>
          <div class="glossary-term">Сравнение цен</div>
          <div>Готовая страница: цена заказчика рядом с найденными рыночными ценами. Это главный результат работы.</div>
          <div class="glossary-term">НМЦК</div>
          <div>Максимальная цена контракта — сколько заказчик готов заплатить. Блок внизу страницы — <strong>отдельный инструмент</strong>, к шагам 1–3 не относится.</div>
          <div class="glossary-term">Telegram</div>
          <div>Кнопка на карточке шлёт краткий вывод «выгодно / невыгодно» в ваш чат (если настроен бот).</div>
        </div>
      </details>
    </section>

    <div id="reportCoverageBanner" class="cov-banner stat-strip {% if coverage.tender_count == 0 %}cov-warn{% elif coverage.tenders_missing_merge_html > 0 %}{% if coverage.merge_html_among_tenders == 0 and coverage.svodka_xlsx_count == 0 %}cov-warn{% else %}cov-partial{% endif %}{% else %}cov-ok{% endif %}">
      {% if coverage.tender_count == 0 %}
      Закупок в базе пока нет. Нажмите «Найти новые закупки» в верхней панели.
      {% else %}
      {% if coverage.merge_html_among_tenders >= coverage.tender_count %}
      Все {{ coverage.tender_count }} закупок имеют готовую страницу сравнения «смета vs рынок».
      {% else %}
      В базе <strong>{{ coverage.tender_count }}</strong> закупок · готовых страниц сравнения: <strong>{{ coverage.merge_html_among_tenders }}</strong>
      {% if coverage.tenders_missing_merge_html > 0 %}
      · ещё <strong>{{ coverage.tenders_missing_merge_html }}</strong> ждут шага 2 («Подготовить недостающие сравнения»)
      {% if coverage.missing_no_svodka > 0 and coverage.missing_no_estimate == 0 %}
      — у {{ coverage.missing_no_svodka }} смета уже есть, но рыночные источники ещё не собирались
      {% endif %}
      {% endif %}
      {% endif %}
      {% endif %}
    </div>

    <section class="tenders-section" aria-labelledby="tendersTitle">
      <h2 class="section-title" id="tendersTitle">Список тендеров</h2>
      <p class="section-lead">В каждой карточке показан один рекомендуемый следующий шаг. Повторные и служебные операции находятся в «Дополнительных действиях».</p>
      <form class="tender-filter-row" method="get" action="/tenders">
        {% if show_all %}<input type="hidden" name="all" value="1">{% endif %}
        <input type="hidden" name="sort" value="{{ sort_mode }}">
        <label>
          Регион
          <select name="region" onchange="this.form.submit()">
            <option value="" {% if not selected_region %}selected{% endif %}>Все регионы</option>
            {% for region in region_options %}
            <option value="{{ region }}" {% if selected_region == region %}selected{% endif %}>{{ region }}</option>
            {% endfor %}
          </select>
        </label>
        {% if selected_region %}
        <a href="/tenders?sort={{ sort_mode }}{% if show_all %}&all=1{% endif %}">сбросить регион</a>
        {% endif %}
      </form>
      <p class="section-lead" style="margin-top:-4px;">
        Показано <strong>{{ visible_count }}</strong> из <strong>{{ tender_count }}</strong> тендеров
        {% if selected_region %}
        · регион: <strong>{{ selected_region }}</strong>
        {% endif %}
        {% if show_all %}
        · все этапы
        · <a href="/tenders?sort={{ sort_mode }}{% if selected_region %}&region={{ selected_region|urlencode }}{% endif %}" style="color:#87bbff;">показать только «Подача заявок»</a>
        {% else %}
        · только этап «Подача заявок»
        · <a href="/tenders?all=1&sort={{ sort_mode }}{% if selected_region %}&region={{ selected_region|urlencode }}{% endif %}" style="color:#87bbff;">показать все этапы</a>
        {% endif %}
      </p>

      <div class="tender-grid-main">
        {% for t in items %}
          <div class="tender-cell">
          <div class="tender-card{% if not t.has_estimate %} no-data{% endif %}" data-href="/merge-report/{{ t.tender_id }}/">
            <details class="tender-menu-wrap">
              <summary class="tender-menu-btn" title="Дополнительные действия">&#9776;</summary>
              <div class="tender-menu">
                <div class="tender-more-actions">
                  <button type="button" class="tender-act tender-act-btn" data-tid="{{ t.tender_id }}" onclick="runFullForTender('{{ t.tender_id }}')" title="Продолжить поиск недостающих рыночных цен и заново собрать страницу сравнения.">Продолжить или обновить поиск цен</button>
                  <button type="button" class="tender-act tender-act-btn" data-tid="{{ t.tender_id }}" onclick="exportTenderToCrm('{{ t.tender_id }}')" title="Создать объект в PM.bi CRM и перенести туда строки сметы как материалы.">Добавить в объекты</button>
                  <button type="button" class="tender-act tender-act-btn" data-tid="{{ t.tender_id }}" onclick="rerunMarketForTender('{{ t.tender_id }}')" title="Удалить прогресс поиска цен и опросить Алису по всем позициям заново.">Начать поиск цен заново</button>
                  <button type="button" class="tender-act tender-act-btn" data-tid="{{ t.tender_id }}" onclick="rebuildReportForTender('{{ t.tender_id }}')" title="Повторно прочитать уже скачанные документы. Поиск рыночных цен не запускается.">Повторно извлечь смету из файлов</button>
                  {% if t.has_estimate %}
                  <a class="tender-act" href="/tenders/{{ t.tender_id }}/estimate.xlsx">Скачать Excel сметы</a>
                  {% endif %}
                  {% if t.has_market_partial %}
                  <a class="tender-act" href="/tenders/{{ t.tender_id }}/market-sources.xlsx">Скачать источники рынка</a>
                  {% endif %}
                  {% if t.has_svodka %}
                  <a class="tender-act" href="/tenders/{{ t.tender_id }}/svodka.xlsx">Скачать Excel выгодности</a>
                  <button type="button" class="tender-act tender-act-btn" data-tid="{{ t.tender_id }}" onclick="runViabilityOnly('{{ t.tender_id }}')" title="Обновить вывод о выгодности и отправить его в настроенный Telegram-чат.">Отправить вывод в Telegram</button>
                  {% endif %}
                </div>
              </div>
            </details>
            {% if t.has_merge_report %}
            <a class="tender-card-link" href="/merge-report/{{ t.tender_id }}/">
              <div class="title">{{ t.display_title }}</div>
            </a>
            {% else %}
            <div class="tender-card-link tender-card-link--disabled">
              <div class="title">{{ t.display_title }}</div>
            </div>
            {% endif %}
            <div class="tid">{{ t.tender_id }}</div>

            <div class="tender-card-meta">
              <div class="tender-meta-item">
                <span class="tender-meta-label">&#1062;&#1077;&#1085;&#1072; &#1090;&#1077;&#1085;&#1076;&#1077;&#1088;&#1072;</span>
                <span class="tender-meta-value">{{ t.price_fmt }}</span>
              </div>
              <div class="tender-meta-item">
                <span class="tender-meta-label">&#1056;&#1077;&#1075;&#1080;&#1086;&#1085;</span>
                <span class="tender-meta-value">{{ t.region }}</span>
              </div>
              <div class="tender-meta-item">
                <span class="tender-meta-label">&#1069;&#1090;&#1072;&#1087;</span>
                <span class="tender-meta-value{% if t.stage_open %} tone-good{% elif not t.stage_display or t.stage_display == '—' %} muted{% endif %}">{{ t.stage_display }}</span>
              </div>
            </div>

            {% if t.market_progress_total > 0 %}
            <div class="tender-progress">
              <div class="tender-progress-head">
                <span class="tender-progress-label">&#1055;&#1088;&#1086;&#1072;&#1085;&#1072;&#1083;&#1080;&#1079;&#1080;&#1088;&#1086;&#1074;&#1072;&#1085;&#1086;</span>
                <span class="tender-progress-value">{{ t.market_progress_done }}/{{ t.market_progress_total }}</span>
              </div>
              <div class="tender-progress-track">
                <div class="tender-progress-fill" style="width: {{ t.market_progress_percent }}%;"></div>
              </div>
              <div class="tender-progress-note">
                {% if t.market_progress_done >= t.market_progress_total %}
                &#1042;&#1089;&#1077; &#1089;&#1090;&#1088;&#1086;&#1082;&#1080; &#1089;&#1084;&#1077;&#1090;&#1099; &#1086;&#1073;&#1088;&#1072;&#1073;&#1086;&#1090;&#1072;&#1085;&#1099;.
                {% elif t.has_market_partial %}
                &#1054;&#1073;&#1088;&#1072;&#1073;&#1086;&#1090;&#1072;&#1085;&#1086; {{ t.market_progress_done }} &#1080;&#1079; {{ t.market_progress_total }}, &#1086;&#1089;&#1090;&#1072;&#1083;&#1086;&#1089;&#1100; {{ t.market_progress_left }}.
                {% else %}
                &#1057;&#1084;&#1077;&#1090;&#1072; &#1075;&#1086;&#1090;&#1086;&#1074;&#1072;. &#1055;&#1086;&#1080;&#1089;&#1082; &#1094;&#1077;&#1085; &#1077;&#1097;&#1105; &#1085;&#1077; &#1079;&#1072;&#1087;&#1091;&#1089;&#1082;&#1072;&#1083;&#1089;&#1103;.
                {% endif %}
              </div>
            </div>
            {% endif %}

            <div class="tender-card-pub">
              <span class="tender-card-pub-label">&#1055;&#1091;&#1073;&#1083;&#1080;&#1082;&#1072;&#1094;&#1080;&#1103;</span>
              <span class="tender-card-pub-date">{{ t.publish_date or "&#1044;&#1072;&#1090;&#1072; &#1085;&#1077; &#1091;&#1082;&#1072;&#1079;&#1072;&#1085;&#1072;" }}</span>
              <span class="tender-card-pub-label">&#1054;&#1082;&#1086;&#1085;&#1095;&#1072;&#1085;&#1080;&#1077;</span>
              <span class="tender-card-pub-date">{{ t.deadline_date }}</span>
            </div>

            <div class="tender-status-row">
              {% if t.has_svodka %}
              <span class="tag tag-merge">&#1050;&#1072;&#1088;&#1090;&#1086;&#1095;&#1082;&#1072; &#1075;&#1086;&#1090;&#1086;&#1074;&#1072;</span>
              {% elif t.has_market_partial %}
              <span class="tag tag-merge">&#1045;&#1089;&#1090;&#1100; &#1095;&#1072;&#1089;&#1090;&#1080;&#1095;&#1085;&#1099;&#1077; &#1094;&#1077;&#1085;&#1099;</span>
              {% elif t.has_estimate %}
              <span class="tag tag-ok">&#1057;&#1084;&#1077;&#1090;&#1072; &#1075;&#1086;&#1090;&#1086;&#1074;&#1072;</span>
              {% else %}
              <span class="tag tag-nodata">&#1053;&#1077;&#1090; &#1089;&#1084;&#1077;&#1090;&#1099;</span>
              {% endif %}
              <span class="tag {% if t.stage_open %}tag-stage-open{% else %}tag-stage-closed{% endif %}">{{ t.stage_display }}</span>
              <a class="eis-in-card" href="{{ t.eis_url }}" target="_blank" rel="noopener noreferrer">&#1045;&#1048;&#1057;</a>
            </div>

            <div class="tender-actions">
              {% if t.has_svodka %}
              <a class="tender-act tender-act--primary tender-act--main" href="/merge-report/{{ t.tender_id }}/">Посмотреть сравнение цен</a>
              <button type="button" class="tender-act tender-act--crm tender-act-btn" data-tid="{{ t.tender_id }}" onclick="exportTenderToCrm('{{ t.tender_id }}')" title="Создать объект в PM.bi CRM и перенести туда строки сметы как материалы.">+ Добавить в объекты</button>
              <p class="tender-next">Готово или частично готово: сохранённые строки Алисы будут вверху таблицы.</p>
              {% elif t.has_market_partial %}
              <a class="tender-act tender-act--primary tender-act--main" href="/merge-report/{{ t.tender_id }}/">Посмотреть частичные цены</a>
              <button type="button" class="tender-act tender-act--crm tender-act-btn" data-tid="{{ t.tender_id }}" onclick="exportTenderToCrm('{{ t.tender_id }}')" title="Создать объект в PM.bi CRM и перенести туда строки сметы как материалы.">+ Добавить в объекты</button>
              <p class="tender-next">Есть сохранённый прогресс Алисы. Можно открыть карточку и продолжить поиск.</p>
              {% elif t.has_estimate %}
              <a class="tender-act tender-act--primary tender-act--main" href="/merge-report/{{ t.tender_id }}/">Открыть карточку тендера</a>
              <button type="button" class="tender-act tender-act--crm tender-act-btn" data-tid="{{ t.tender_id }}" onclick="exportTenderToCrm('{{ t.tender_id }}')" title="Создать объект в PM.bi CRM и перенести туда строки сметы как материалы.">+ Добавить в объекты</button>
              <p class="tender-next">Смета готова. В карточке можно запустить поиск цен и смотреть сохранённые строки.</p>
              {% else %}
              <button type="button" class="tender-act tender-act--primary tender-act--main tender-act-btn" data-tid="{{ t.tender_id }}" onclick="runFullForTender('{{ t.tender_id }}')">Скачать документы и подготовить сравнение</button>
              <p class="tender-next">Смета не извлечена. Программа попробует скачать документы повторно.</p>
              {% endif %}
              <details class="tender-more">
                <summary>Дополнительные действия</summary>
                <div class="tender-more-actions">
                  <button type="button" class="tender-act tender-act-btn" data-tid="{{ t.tender_id }}" onclick="runFullForTender('{{ t.tender_id }}')" title="Продолжить поиск недостающих рыночных цен и заново собрать страницу сравнения.">Продолжить или обновить поиск цен</button>
                  <button type="button" class="tender-act tender-act-btn" data-tid="{{ t.tender_id }}" onclick="exportTenderToCrm('{{ t.tender_id }}')" title="Создать объект в PM.bi CRM и перенести туда строки сметы как материалы.">Добавить в объекты</button>
                  <button type="button" class="tender-act tender-act-btn" data-tid="{{ t.tender_id }}" onclick="rerunMarketForTender('{{ t.tender_id }}')" title="Удалить прогресс поиска цен и опросить Алису по всем позициям заново.">Начать поиск цен заново</button>
                  <button type="button" class="tender-act tender-act-btn" data-tid="{{ t.tender_id }}" onclick="rebuildReportForTender('{{ t.tender_id }}')" title="Повторно прочитать уже скачанные документы. Поиск рыночных цен не запускается.">Повторно извлечь смету из файлов</button>
                  {% if t.has_estimate %}
                  <a class="tender-act" href="/tenders/{{ t.tender_id }}/estimate.xlsx">Скачать Excel сметы</a>
                  {% endif %}
                  {% if t.has_market_partial %}
                  <a class="tender-act" href="/tenders/{{ t.tender_id }}/market-sources.xlsx">Скачать источники рынка</a>
                  {% endif %}
                  {% if t.has_svodka %}
                  <a class="tender-act" href="/tenders/{{ t.tender_id }}/svodka.xlsx">Скачать Excel выгодности</a>
                  {% endif %}
                  {% if t.has_svodka %}
                  <button type="button" class="tender-act tender-act-btn" data-tid="{{ t.tender_id }}" onclick="runViabilityOnly('{{ t.tender_id }}')" title="Обновить вывод о выгодности и отправить его в настроенный Telegram-чат.">Отправить вывод в Telegram</button>
                  {% endif %}
                </div>
              </details>
            </div>
          </div>
        </div>
        {% endfor %}
      </div>
    {% if not items %}
    {% if tender_count == 0 %}
    <p class="sub" style="margin:0;">База пуста — нажмите «Найти новые закупки» в верхней панели.</p>
    {% elif not show_all %}
    <p class="sub" style="margin:0;">Нет закупок на этапе «Подача заявок». Включите «Показать все этапы» выше или скачайте новые тендеры.</p>
    {% else %}
    <p class="sub" style="margin:0;">Нет данных для отображения.</p>
    {% endif %}
    {% endif %}

    {% if tender_count %}
    <p class="meta" style="margin:12px 0 0;">В базе {{ tender_count }} тендеров · сметы с таблицей позиций: {{ display_report_count }} / {{ report_count }} · <span style="color:#d89090;">красная полоска слева</span> — в смете нет извлечённых работ.</p>
    {% endif %}
    </section>

    <section class="action-hub controls" aria-labelledby="actionHubTitle">
      <h2 class="section-title" id="actionHubTitle">Главное</h2>
      <p class="section-lead">Сначала нажмите «Найти новые закупки». Если поиск не сработает, ниже появится короткое объяснение причины и следующий шаг.</p>

      <div class="workflow-strip" aria-hidden="true">
        <div class="wf-step"><span class="wf-num">1</span> Найти закупки</div>
        <span class="wf-arrow">→</span>
        <div class="wf-step"><span class="wf-num">2</span> Сравнить цены</div>
        <span class="wf-arrow">→</span>
        <div class="wf-step"><span class="wf-num">3</span> Посмотреть результат</div>
      </div>

      <div class="action-grid">
        <article class="action-card">
          <h3 class="action-card-title">Шаг 1. Найти новые закупки</h3>
          <p class="action-card-desc">Ищет закупки по вашим регионам и ключевым словам, скачивает архивы документов, распаковывает их и извлекает смету в Excel. Результат попадает в список выше.</p>
          <button class="btn btn-lg" type="button" id="startBtn" onclick="startParsing()">Найти новые закупки</button>
          <details class="action-options">
            <summary>параметры поиска</summary>
            <div class="opts">
              <label title="Сколько страниц результатов просматривать на каждую пару регион × ключевое слово">Страниц результатов
                <input type="number" id="optMaxPages" min="1" max="20" value="2" />
              </label>
              <label title="Максимум новых тендеров за один запуск">Закупок за запуск
                <input type="number" id="optMaxTenders" min="1" max="50" value="15" />
              </label>
              <label title="Не брать закупки старше указанного числа дней">Опубликованы за последние, дней
                <input type="number" id="optDaysBack" min="1" max="365" value="60" />
              </label>
            </div>
          </details>
        </article>

        <article class="action-card">
          <h3 class="action-card-title">Шаг 2. Сравнить цены заказчика с рынком</h3>
          <p class="action-card-desc">Программа ищет рыночные цены и реальные ссылки для позиций сметы, а затем собирает готовую страницу сравнения. <strong>Долго</strong> — обработка нескольких закупок может занять часы.</p>
          <div class="btn-row">
            <button class="btn btn-lg" type="button" id="genMergeMissingBtn" onclick="generateMergeSiteMissing()">Подготовить недостающие сравнения</button>
            <button class="btn secondary" type="button" id="genMergeSiteBtn" onclick="generateMergeSiteAll()">Обновить сравнения для всех</button>
          </div>
          <p class="btn-effect"><strong>Рекомендуется первая кнопка:</strong> она пропускает уже готовые результаты. Вторая повторно обрабатывает все доступные сметы.</p>
        </article>

        <article class="action-card action-card--wide">
          <h3 class="action-card-title">Проверить одну закупку по ссылке</h3>
          <p class="action-card-desc">Вставьте ссылку с zakupki.gov.ru или номер закупки. Программа скачает документы, извлечёт смету, найдёт рыночные цены и подготовит сравнение.</p>
          <div class="link-row">
            <span>Ссылка или номер:</span>
            <input id="tenderLinkInput" type="text" placeholder="https://zakupki.gov.ru/... или 19-значный номер" />
            <button class="btn" type="button" id="runByLinkBtn" onclick="runByTenderLink()">Проверить эту закупку</button>
          </div>
          <div id="quickTenderCheck" class="meta" style="margin-top:6px;display:none;">
            Последний запуск: <a id="quickTenderReportLink" href="#" target="_blank" rel="noopener noreferrer">открыть сводку</a>
            · <a id="quickTenderEisLink" href="#" target="_blank" rel="noopener noreferrer">карточка на ЕИС</a>
          </div>
          <details class="action-options">
            <summary>повторное извлечение сметы из уже скачанных файлов</summary>
            <div class="rebuild-row">
              <span>Выберите закупку:</span>
              <select id="rebuildTenderSelect" {% if not rebuild_options %}disabled{% endif %}>
                {% for o in rebuild_options %}
                <option value="{{ o.tender_id }}">{{ o.tender_id }} — {{ o.display_title }}</option>
                {% endfor %}
                {% if not rebuild_options %}
                <option value="">— нет тендеров —</option>
                {% endif %}
              </select>
              <button class="btn secondary" type="button" id="rebuildBtn" onclick="rebuildReport()">Извлечь смету повторно</button>
              <button class="btn secondary" type="button" id="rebuildAllBtn" onclick="rebuildAllReports()" {% if tender_count < 1 %}disabled title="Нет тендеров в базе"{% endif %}>Повторить для всех</button>
            </div>
          </details>
        </article>

        <article class="action-card">
          <h3 class="action-card-title">Уведомления в браузере</h3>
          <p class="action-card-desc">Всплывающее окно, когда закончится поиск закупок или появится новое сравнение цен. Это не Telegram — уведомление работает только в этом браузере.</p>
          <button class="btn secondary" type="button" id="enablePushBtn" onclick="enableWebPush()">Включить уведомления</button>
          <a class="link-refresh" href="#" onclick="location.reload(); return false;" style="display:inline-block;margin-top:10px;">Обновить страницу (F5)</a>
        </article>
      </div>

      <div id="mergeSitePanel" class="parse-progress-panel" role="status" aria-live="polite" hidden>
        <div class="parse-progress-head">
          <span class="parse-pulse" aria-hidden="true"></span>
          <strong id="mergeSiteLabel">Подготавливаем сравнения цен</strong>
        </div>
        <div class="merge-bar-wrap"><div id="mergeBarFill" class="merge-bar-fill" style="width:0%"></div></div>
        <div class="parse-progress-time" id="mergePercentText">0%</div>
        <div class="parse-progress-hint" id="mergeSiteDetail"></div>
        <div class="merge-logs" id="mergeSiteLogs"></div>
      </div>
      <div id="mergeIdleSummary" class="meta" style="margin-top:4px;"></div>
      <div id="mergeMissingReason" class="meta" style="margin-top:4px;"></div>
      <div id="parseProgressPanel" class="parse-progress-panel" role="status" aria-live="polite" hidden>
        <div class="parse-progress-head">
          <span class="parse-pulse" aria-hidden="true"></span>
          <strong id="parseProgressLabel">Выполняется…</strong>
        </div>
        <div class="parse-bar-wrap"><div id="parseBarFill" class="parse-bar-fill"></div></div>
        <div class="parse-progress-time" id="parseProgressTime">Прошло: 0 с</div>
        <div class="status" id="parseStatus"></div>
        <div class="parse-summary">
          <div class="parse-summary-grid">
            <div class="parse-summary-item">
              <div class="parse-summary-label">Итог</div>
              <div class="parse-summary-value" id="parseResultMain">Ждём запуск</div>
            </div>
            <div class="parse-summary-item">
              <div class="parse-summary-label">Причина</div>
              <div class="parse-summary-value" id="parseResultIssue">Пока нет</div>
            </div>
            <div class="parse-summary-item">
              <div class="parse-summary-label">Что делать</div>
              <div class="parse-summary-value" id="parseResultNext">Нажать кнопку поиска</div>
            </div>
          </div>
        </div>
        <div id="parseProgressLogCount" class="parse-progress-hint" style="margin-top:8px;color:#b8c7ea;"></div>
        <details class="compact-details">
          <summary>Технические подробности</summary>
          <div class="parse-status-line" id="parseCommandLine"></div>
          <div class="logs" id="parseLogs"></div>
        </details>
      </div>
    </section>

    <section class="tool-section region-block" id="nmckParseBlock" aria-labelledby="nmckParseTitle">
      <h2 class="section-title" id="nmckParseTitle">Дополнительно: обоснование НМЦК (Приложение №2)</h2>
      <p class="action-card-desc">Загрузите Excel «Приложение №2 к извещению (Обоснование НМЦК)» — получите таблицу и JSON с позициями, количествами, коммерческими предложениями и НМЦК.</p>
      <div class="btn-row" style="margin-top:0;">
        <input type="file" id="nmckFileInput" accept=".xlsx,.xls,.xlsm,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" style="max-width:100%;font-size:12px;color:#b8c7ea;" />
        <button type="button" class="btn secondary" id="nmckParseBtn" onclick="parseNmckJustification()">Разобрать Excel в таблицу и JSON</button>
        <a class="btn secondary" id="nmckPreviewLink" href="#" target="_blank" rel="noopener noreferrer" hidden>Открыть таблицу</a>
        <button type="button" class="btn secondary" id="nmckCopyBtn" onclick="copyNmckJson()" disabled>Скопировать JSON</button>
        <button type="button" class="btn secondary" id="nmckDownloadBtn" onclick="downloadNmckJson()" disabled>Скачать JSON-файл</button>
      </div>
      <p class="status" id="nmckParseStatus" style="margin-top:8px;"></p>
      <textarea id="nmckJsonOut" readonly hidden style="width:100%;min-height:220px;margin-top:10px;font-family:Consolas,monospace;font-size:11px;line-height:1.35;background:#0b1223;border:1px solid var(--border-soft);color:var(--text);border-radius:8px;padding:10px;box-sizing:border-box;resize:vertical;"></textarea>
    </section>
  </div>
  <button type="button" class="site-chat-fab" id="siteChatFab" onclick="toggleSiteChat()" title="Логи и сообщения выполнения">🧾</button>
  <aside class="site-chat-panel" id="siteChatPanel" hidden>
    <div class="site-chat-head">
      <span>🧾 Логи выполнения</span>
      <button type="button" class="site-chat-close" onclick="toggleSiteChat(false)">закрыть</button>
    </div>
    <div class="site-chat-feed" id="siteChatFeed">
      <div class="site-chat-empty">Пока событий нет. Когда запустится сравнение цен, здесь появятся сообщения как в Telegram.</div>
    </div>
  </aside>
  <script>
    function switchMarketSection(key) {
      document.querySelectorAll("[data-market-tab]").forEach(el => el.classList.toggle("is-active", el.getAttribute("data-market-tab") === key));
      document.querySelectorAll("[data-market-pane]").forEach(el => el.classList.toggle("is-active", el.getAttribute("data-market-pane") === key));
    }

    document.addEventListener("click", function(e) {
      const card = e.target && e.target.closest ? e.target.closest(".tender-card[data-href]") : null;
      if (!card) return;
      if (e.target.closest("a, button, summary, details, input, select, textarea, label")) return;
      const href = card.getAttribute("data-href");
      if (href) window.location.href = href;
    });

    (function bindRebuildSelect() {
      const sel = document.getElementById("rebuildTenderSelect");
      if (!sel) return;
      sel.addEventListener("change", function() {
        applyToolbarDisabled(parseRunning, !!window.__mergeRunLive);
      });
    })();

    let lastNmckJson = "";
    async function parseNmckJustification() {
      const inp = document.getElementById("nmckFileInput");
      const st = document.getElementById("nmckParseStatus");
      const ta = document.getElementById("nmckJsonOut");
      const copyB = document.getElementById("nmckCopyBtn");
      const dlB = document.getElementById("nmckDownloadBtn");
      const prevA = document.getElementById("nmckPreviewLink");
      const f = inp && inp.files && inp.files[0];
      if (!f) { alert("Выберите файл Excel (.xlsx)"); return; }
      if (st) st.textContent = "Загрузка и разбор…";
      lastNmckJson = "";
      if (copyB) copyB.disabled = true;
      if (dlB) dlB.disabled = true;
      if (prevA) { prevA.hidden = true; prevA.href = "#"; }
      if (ta) { ta.hidden = true; ta.value = ""; }
      const fd = new FormData();
      fd.append("file", f);
      try {
        const r = await fetch("/api/parse-nmck-justification", { method: "POST", body: fd });
        let data = {};
        try { data = await r.json(); } catch (e) {}
        if (!r.ok || !data.ok) {
          if (st) st.textContent = (data && data.message) ? data.message : ("Ошибка " + r.status);
          return;
        }
        const pack = { columns: data.columns, rows: data.rows, meta: data.meta };
        lastNmckJson = JSON.stringify(pack, null, 2);
        const m = data.meta || {};
        if (st) {
          st.textContent = "Готово: " + (m.row_count != null ? m.row_count : "?") + " поз., колонок "
            + (m.column_count != null ? m.column_count : "?") + ", лист «" + (m.sheet || "") + "»";
        }
        if (data.preview_url && prevA) {
          prevA.href = data.preview_url;
          prevA.hidden = false;
        }
        if (ta) { ta.value = lastNmckJson; ta.hidden = false; }
        if (copyB) copyB.disabled = false;
        if (dlB) dlB.disabled = false;
      } catch (e) {
        if (st) st.textContent = "Запрос не выполнен (сеть или сервер).";
      }
    }
    function copyNmckJson() {
      if (!lastNmckJson) return;
      navigator.clipboard.writeText(lastNmckJson).then(function() {
        const st = document.getElementById("nmckParseStatus");
        if (st) st.textContent += " · JSON в буфере обмена";
      }).catch(function() { alert("Не удалось скопировать"); });
    }
    function downloadNmckJson() {
      if (!lastNmckJson) return;
      const blob = new Blob([lastNmckJson], { type: "application/json;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "nmck_prilozhenie_2.json";
      a.click();
      URL.revokeObjectURL(a.href);
    }

    function getRebuildTenderId() {
      const s = document.getElementById("rebuildTenderSelect");
      return s && s.value ? String(s.value).trim() : "";
    }
    function setQuickTenderLinks(tid) {
      const t = String(tid || "").trim();
      const box = document.getElementById("quickTenderCheck");
      const rep = document.getElementById("quickTenderReportLink");
      const eis = document.getElementById("quickTenderEisLink");
      if (!box || !rep || !eis) return;
      if (!t) {
        box.style.display = "none";
        return;
      }
      rep.href = "/merge-report/" + encodeURIComponent(t) + "/";
      eis.href = "https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=" + encodeURIComponent(t);
      rep.textContent = "сводка " + t;
      box.style.display = "";
      try { localStorage.setItem("lastTenderCheckId", t); } catch (e) {}
    }
    try {
      const lastTid = localStorage.getItem("lastTenderCheckId") || "";
      if (lastTid) setQuickTenderLinks(lastTid);
    } catch (e) {}
    const TENDER_COUNT = {{ tender_count }};

    let parseRunning = false;
    let parseStartMs = null;
    let parsePendingUntil = 0;
    let parseAutoReloadArmed = false;
    let notifyState = {
      enabled: localStorage.getItem("webPushEnabled") === "1",
      prev: null,
    };

    function formatElapsed(sec) {
      const s = Math.max(0, Math.floor(sec));
      const m = Math.floor(s / 60);
      const h = Math.floor(m / 60);
      if (h > 0) return `${h} ч ${m % 60} мин ${s % 60} с`;
      if (m > 0) return `${m} мин ${s % 60} с`;
      return `${s} с`;
    }

    function updateParseElapsed() {
      if (!parseRunning || parseStartMs == null) return;
      const sec = (Date.now() - parseStartMs) / 1000;
      const el = document.getElementById("parseProgressTime");
      if (el) el.textContent = "Прошло: " + formatElapsed(sec);
    }

    setInterval(updateParseElapsed, 1000);

    function showParseLaunchFeedback() {
      parseRunning = true;
      parseStartMs = Date.now();
      parsePendingUntil = Date.now() + 8000;
      parseAutoReloadArmed = true;
      const panel = document.getElementById("parseProgressPanel");
      const label = document.getElementById("parseProgressLabel");
      const bar = document.getElementById("parseBarFill");
      const time = document.getElementById("parseProgressTime");
      const status = document.getElementById("parseStatus");
      const logs = document.getElementById("parseLogs");
      const logCount = document.getElementById("parseProgressLogCount");
      const cmd = document.getElementById("parseCommandLine");
      if (panel) panel.hidden = false;
      if (label) label.textContent = "Запускаем поиск новых закупок…";
      if (bar) {
        bar.classList.add("running");
        bar.style.width = "65%";
      }
      if (time) time.textContent = "Прошло: 0 с";
      if (status) status.textContent = "Статус: передаём задачу серверу";
      if (logs) logs.textContent = "Ожидаем первые сообщения от программы…";
      if (logCount) logCount.textContent = "Поиск запускается.";
      if (cmd) cmd.textContent = "";
      applyToolbarDisabled(true, false);
      window.setTimeout(function() {
        if (panel) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }, 80);
    }

    function showParseLaunchError(message) {
      parseRunning = false;
      parseStartMs = null;
      parsePendingUntil = 0;
      parseAutoReloadArmed = false;
      const panel = document.getElementById("parseProgressPanel");
      const label = document.getElementById("parseProgressLabel");
      const bar = document.getElementById("parseBarFill");
      const time = document.getElementById("parseProgressTime");
      const status = document.getElementById("parseStatus");
      if (panel) panel.hidden = false;
      if (label) label.textContent = "Поиск не запустился";
      if (bar) {
        bar.classList.remove("running");
        bar.style.width = "100%";
      }
      if (time) time.textContent = "";
      if (status) status.textContent = message || "Сервер не смог запустить поиск.";
      applyToolbarDisabled(false, !!window.__mergeRunLive);
    }

    function applyToolbarDisabled(parseRun, mergeRun) {
      const busy = parseRun || mergeRun;
      const startBtn = document.getElementById("startBtn");
      const rebuildBtn = document.getElementById("rebuildBtn");
      const rebuildAllBtn = document.getElementById("rebuildAllBtn");
      const genBtn = document.getElementById("genMergeSiteBtn");
      const genMissingBtn = document.getElementById("genMergeMissingBtn");
      const runByLinkBtn = document.getElementById("runByLinkBtn");
      if (startBtn) {
        startBtn.disabled = busy;
        startBtn.textContent = parseRun ? "Ищем закупки…" : "Найти новые закупки";
      }
      if (rebuildBtn) rebuildBtn.disabled = busy || !getRebuildTenderId();
      if (rebuildAllBtn) rebuildAllBtn.disabled = busy || TENDER_COUNT < 1;
      if (genBtn) genBtn.disabled = busy;
      if (genMissingBtn) genMissingBtn.disabled = busy;
      if (runByLinkBtn) runByLinkBtn.disabled = busy;
      document.querySelectorAll(".tender-act-btn").forEach(function(btn) {
        btn.disabled = busy;
      });
    }

    function updatePushButtonUi() {
      const b = document.getElementById("enablePushBtn");
      if (!b) return;
      if (!("Notification" in window)) {
        b.textContent = "Браузер не поддерживает уведомления";
        b.disabled = true;
        return;
      }
      const perm = Notification.permission;
      if (notifyState.enabled && perm === "granted") {
        b.textContent = "Уведомления включены";
        b.disabled = true;
        return;
      }
      b.textContent = "Включить уведомления";
      b.disabled = false;
    }

    async function enableWebPush() {
      if (!("Notification" in window)) {
        alert("Браузер не поддерживает уведомления.");
        return;
      }
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        alert("Разрешение на уведомления не выдано.");
        updatePushButtonUi();
        return;
      }
      notifyState.enabled = true;
      localStorage.setItem("webPushEnabled", "1");
      updatePushButtonUi();
      new Notification("AutoBot", { body: "Уведомления в браузере включены." });
    }

    function safeNotify(title, body) {
      if (!notifyState.enabled) return;
      if (!("Notification" in window)) return;
      if (Notification.permission !== "granted") return;
      try {
        new Notification(title, { body });
      } catch (e) {}
    }

    function handlePushDiff(nextState) {
      const prev = notifyState.prev;
      notifyState.prev = nextState;
      if (!prev) return;

      if (prev.parse_running && !nextState.parse_running) {
        const ok = nextState.parse_exit_code === 0;
        safeNotify(
          ok ? "Поиск закупок завершён" : "Поиск закупок завершён с ошибкой",
          ok ? "Обновите страницу, чтобы увидеть результат." : "Откройте ход работы на странице."
        );
      }

      if (prev.merge_running && !nextState.merge_running) {
        safeNotify("Сравнения цен готовы", nextState.merge_last_summary || "Обработка завершена.");
      }

      if ((nextState.coverage_merge_html || 0) > (prev.coverage_merge_html || 0)) {
        const delta = (nextState.coverage_merge_html || 0) - (prev.coverage_merge_html || 0);
        safeNotify("Появились новые сравнения цен", "Готово новых страниц: " + delta);
      }
    }

    async function refreshPushState() {
      try {
        const r = await fetch("/api/push-state");
        if (!r.ok) return;
        const st = await r.json();
        handlePushDiff(st);
      } catch (e) {}
    }

    async function startParsing() {
      showParseLaunchFeedback();
      try {
        const body = {
          max_pages: parseInt(document.getElementById("optMaxPages").value, 10) || 2,
          max_tenders: parseInt(document.getElementById("optMaxTenders").value, 10) || 15,
          days_back: parseInt(document.getElementById("optDaysBack").value, 10) || 60,
        };
        const r = await fetch("/api/tender-search/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await r.json();
        if (!r.ok || !data.ok) {
          const message = data.message || "Не удалось запустить поиск закупок";
          showParseLaunchError(message);
          alert(message);
          return;
        }
        window.setTimeout(refreshStatus, 200);
      } catch (e) {
        const message = "Не удалось запустить поиск закупок. Проверьте, работает ли сервер.";
        showParseLaunchError(message);
        alert(message);
      }
    }

    async function rebuildReport() {
      const tid = getRebuildTenderId();
      if (!tid) { alert("Выберите закупку."); return; }
      if (!confirm("Повторно извлечь смету для закупки " + tid + " из уже скачанных документов?\\n\\nРыночные цены обновляться не будут.")) return;
      applyToolbarDisabled(true, false);
      try {
        const r = await fetch("/api/reports/rebuild", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tender_id: tid }),
        });
        const data = await r.json();
        if (!data.ok) {
          alert(data.message || "Не удалось запустить повторное извлечение сметы");
          refreshStatus();
        }
      } catch (e) {
        alert("Не удалось отправить запрос на повторное извлечение сметы.");
        refreshStatus();
      }
    }

    async function rebuildReportForTender(tid) {
      const t = String(tid || "").trim();
      if (!t) return;
      if (!confirm("Повторно извлечь смету для закупки " + t + " из уже скачанных документов?\\n\\nРыночные цены обновляться не будут.")) return;
      applyToolbarDisabled(true, false);
      try {
        const r = await fetch("/api/reports/rebuild", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tender_id: t }),
        });
        const data = await r.json();
        if (!data.ok) {
          alert(data.message || "Не удалось запустить повторное извлечение сметы");
          refreshStatus();
        }
      } catch (e) {
        alert("Не удалось отправить запрос на повторное извлечение сметы.");
        refreshStatus();
      }
    }

    async function rebuildAllReports() {
      if (TENDER_COUNT < 1) { alert("В списке пока нет закупок."); return; }
      if (!confirm(
        "Повторно извлечь сметы для всех " + TENDER_COUNT + " закупок?\\n\\n"
        + "Программа перечитает уже скачанные документы. Рыночные цены обновляться не будут."
      )) return;
      applyToolbarDisabled(true, false);
      try {
        const r = await fetch("/api/reports/rebuild-all", { method: "POST" });
        const data = await r.json();
        if (!data.ok) {
          alert(data.message || "Не удалось запустить повторное извлечение смет");
          refreshStatus();
        }
      } catch (e) {
        alert("Ошибка запроса");
        refreshStatus();
      }
    }

    async function generateMergeSiteAll() {
      if (!confirm("Обновить сравнения цен для всех закупок со сметой?\\n\\nПрограмма повторно проверит рыночные источники. Процесс может занять несколько часов.")) return;
      try {
        const r = await fetch("/api/generate-merge-site-all", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: "{}",
        });
        let data = {};
        try {
          data = await r.json();
        } catch (e) {
          alert("Сервер вернул не JSON (код " + r.status + "). Проверьте консоль web_ui.py.");
          refreshStatus();
          return;
        }
        if (!r.ok || !data.ok) {
          alert(data.message || ("Запрос отклонён (HTTP " + r.status + ")"));
        }
      } catch (e) {
        alert("Сеть или сервер недоступен: " + e);
      }
      refreshStatus();
      refreshCoverage();
    }

    async function generateMergeSiteMissing() {
      if (!confirm("Подготовить сравнения только там, где результата ещё нет или прошлая обработка завершилась с ошибкой?\\n\\nУже готовые страницы будут пропущены.")) return;
      try {
        const r = await fetch("/api/generate-merge-site-missing", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: "{}",
        });
        let data = {};
        try {
          data = await r.json();
        } catch (e) {
          alert("Сервер вернул не JSON (код " + r.status + "). Проверьте консоль web_ui.py.");
          refreshStatus();
          return;
        }
        if (!r.ok || !data.ok) {
          alert(data.message || ("Запрос отклонён (HTTP " + r.status + ")"));
        }
      } catch (e) {
        alert("Сеть или сервер недоступен: " + e);
      }
      refreshStatus();
      refreshCoverage();
    }

    async function runFullForTender(tid) {
      const t = String(tid || "").trim();
      if (!t) return;
      if (!confirm("Подготовить сравнение цен для закупки " + t + "?\\n\\nПрограмма проверит документы, найдёт рыночные цены и соберёт готовую страницу.")) return;
      setQuickTenderLinks(t);
      try {
        const r = await fetch("/api/generate-merge-site-one", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ tender_id: t }),
        });
        const data = await r.json();
        if (!r.ok || !data.ok) {
          alert(data.message || ("Запрос отклонён (HTTP " + r.status + ")"));
        }
      } catch (e) {
        alert("Сеть или сервер недоступен: " + e);
      }
      refreshStatus();
      refreshCoverage();
    }

    function navigateCrmProject(url) {
      if (!url) return;
      try {
        if (window.parent && window.parent !== window) {
          window.parent.postMessage({ type: "pmbi:navigate", href: url }, "*");
          return;
        }
      } catch (e) {}
      window.location.href = url;
    }

    async function exportTenderToCrm(tid) {
      const t = String(tid || "").trim();
      if (!t) return;
      if (!confirm("Добавить закупку " + t + " в CRM?\\n\\nБудет создан объект, а строки сметы уйдут в материалы объекта.")) return;
      try {
        const r = await fetch("/api/export-to-crm", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ tender_id: t }),
        });
        let data = {};
        try { data = await r.json(); } catch (e) {}
        if (!r.ok || !data.ok) {
          alert(data.message || ("CRM-экспорт не прошёл (HTTP " + r.status + ")"));
          return;
        }
        const summary = data.summary || {};
        if (data.already_exists) {
          alert("Эта закупка уже есть в CRM: объект #" + data.project_id + ".");
          if (data.project_url) navigateCrmProject(data.project_url);
          return;
        }
        alert(
          "Готово: объект #" + data.project_id + " создан в CRM.\\n"
          + "Материалов отправлено: " + (data.materials_sent || 0) + ".\\n"
          + "В CRM сейчас материалов: " + (summary.materials == null ? "?" : summary.materials) + "."
        );
        if (data.project_url) navigateCrmProject(data.project_url);
      } catch (e) {
        alert("Не удалось отправить закупку в CRM: " + e);
      }
    }

    async function rerunMarketForTender(tid) {
      const t = String(tid || "").trim();
      if (!t) return;
      if (!confirm("Начать поиск рыночных цен для закупки " + t + " заново?\\n\\nСохранённый прогресс Алисы будет отброшен.")) return;
      setQuickTenderLinks(t);
      try {
        const r = await fetch("/api/generate-merge-site-one-rerun-market", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ tender_id: t }),
        });
        const data = await r.json();
        if (!r.ok || !data.ok) {
          alert(data.message || ("Запрос отклонён (HTTP " + r.status + ")"));
        }
      } catch (e) {
        alert("Сеть или сервер недоступен: " + e);
      }
      refreshStatus();
      refreshCoverage();
    }

    async function runViabilityOnly(tid) {
      const t = String(tid || "").trim();
      if (!t) return;
      if (!confirm("Обновить вывод о выгодности закупки " + t + " и отправить его в Telegram?")) return;
      try {
        const r = await fetch("/api/tender-viability-refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ tender_id: t }),
        });
        const data = await r.json();
        if (!r.ok || !data.ok) {
          alert(data.message || ("Запрос отклонён (HTTP " + r.status + ")"));
          return;
        }
        let msg = data.message || "Готово.";
        if (data.report_url) {
          msg += " | Открыть: " + data.report_url;
        }
        if (data.telegram_sent) {
          msg += " | В Telegram отправлен анализ.";
        }
        alert(msg);
      } catch (e) {
        alert("Сеть или сервер недоступен: " + e);
      }
      refreshCoverage();
    }

    async function runByTenderLink() {
      const inp = document.getElementById("tenderLinkInput");
      const raw = inp && inp.value ? String(inp.value).trim() : "";
      if (!raw) { alert("Вставьте ссылку на закупку с zakupki.gov.ru или её номер."); return; }
      if (!confirm("Проверить эту закупку?\\n\\nПрограмма скачает документы, извлечёт смету и найдёт рыночные цены.")) return;
      try {
        const r = await fetch("/api/generate-merge-site-by-link", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ tender_link: raw }),
        });
        const data = await r.json();
        if (!r.ok || !data.ok) {
          alert(data.message || ("Запрос отклонён (HTTP " + r.status + ")"));
        } else if (inp) {
          inp.value = "";
          setQuickTenderLinks(data.tender_id || "");
        }
      } catch (e) {
        alert("Сеть или сервер недоступен: " + e);
      }
      refreshStatus();
      refreshCoverage();
    }

    async function refreshCoverage() {
      const el = document.getElementById("reportCoverageBanner");
      if (!el) return;
      try {
        const r = await fetch("/api/reports-coverage");
        if (!r.ok) return;
        const c = await r.json();
        const nt = c.tender_count ?? 0;
        const mh = c.merge_html_among_tenders ?? 0;
        const miss = c.tenders_missing_merge_html ?? 0;
        const sx = c.svodka_xlsx_count ?? 0;
        const rs_no_est = c.missing_no_estimate ?? 0;
        const rs_no_svodka = c.missing_no_svodka ?? 0;
        const rs_no_html = c.missing_no_html ?? 0;
        let cls = "cov-banner stat-strip cov-ok";
        if (nt === 0) {
          el.className = "cov-banner stat-strip cov-warn";
          el.innerHTML = "В списке пока нет закупок. Нажмите «Найти новые закупки».";
          return;
        }
        if (miss > 0) cls = mh === 0 && sx === 0 ? "cov-banner stat-strip cov-warn" : "cov-banner stat-strip cov-partial";
        el.className = cls;
        let html = "Готовых сравнений цен: <strong>" + mh + "</strong> из " + nt;
        if (miss > 0) {
          html += " · ждут обработки: <strong>" + miss + "</strong>";
          html += "<br/><span style=\\"opacity:.85;font-size:11px\\">Из них: без извлечённой сметы — " + rs_no_est + ", без найденных рыночных цен — " + rs_no_svodka + ", страница результата не собрана — " + rs_no_html + ".</span>";
        }
        el.innerHTML = html;
      } catch (e) {}
    }

    function parseProgressView(pr, parsePending) {
      const lines = Array.isArray(pr.log_tail) ? pr.log_tail : [];
      const isTenderSearch = String(pr.task || "").includes("поиск новых закупок")
        || lines.some(function(line) { return line === "Поиск тендеров..."; });
      if (parsePending) {
        return { title: "Запускаем поиск новых закупок…", detail: "Передаём задачу серверу", percent: 5, indeterminate: true };
      }
      if (!pr.running) {
        if (pr.exit_code === 0) {
          return {
            title: isTenderSearch ? "Поиск закупок завершён" : "Задание завершено",
            detail: isTenderSearch ? "Готово. Обновите страницу, чтобы увидеть новые закупки." : "Готово.",
            percent: 100,
            indeterminate: false,
          };
        }
        if (pr.exit_code !== null && pr.exit_code !== undefined) {
          return { title: isTenderSearch ? "Поиск завершён с ошибкой" : "Задание завершено с ошибкой", detail: "Подробности — в журнале ниже.", percent: 100, indeterminate: false };
        }
        return { title: "Ожидание", detail: "", percent: 0, indeterminate: false };
      }

      let searchChecks = 0;
      let tenderStep = null;
      let filtersDone = false;
      let finalReport = false;
      for (const line of lines) {
        if (line.startsWith("- ") && line.includes(" найдено")) searchChecks += 1;
        if (line.startsWith("Итого после фильтров:")) filtersDone = true;
        if (line.startsWith("Готово. Общий отчет по сметам:")) finalReport = true;
        const match = line.match(/^\\[([0-9]+)\\/([0-9]+)\\] ([0-9]+):/);
        if (match) tenderStep = { current: Number(match[1]), total: Number(match[2]), id: match[3] };
      }

      if (finalReport) {
        return { title: "Завершаем поиск", detail: "Сохраняем итоговый отчёт и список закупок", percent: 98, indeterminate: false };
      }
      if (tenderStep && tenderStep.total > 0) {
        const completedBefore = Math.max(0, tenderStep.current - 1);
        const percent = 48 + Math.round((completedBefore / tenderStep.total) * 47);
        return {
          title: "Скачиваем документы и извлекаем сметы",
          detail: "Закупка " + tenderStep.current + " из " + tenderStep.total + " · № " + tenderStep.id,
          percent: percent,
          indeterminate: false,
        };
      }
      if (filtersDone) {
        return { title: "Формируем список закупок", detail: "Поиск завершён, применяем фильтры и проверяем ранее найденные закупки", percent: 45, indeterminate: true };
      }
      if (searchChecks > 0 || lines.some(function(line) { return line === "Поиск тендеров..."; })) {
        return {
          title: "Ищем закупки на zakupki.gov.ru",
          detail: searchChecks > 0 ? "Проверено поисковых направлений: " + searchChecks : "Получаем первые результаты…",
          percent: Math.min(40, 10 + searchChecks * 5),
          indeterminate: true,
        };
      }
      return { title: pr.task ? "Сейчас: " + pr.task : "Подготавливаем поиск…", detail: "Процесс запущен, ожидаем первые сообщения", percent: 7, indeterminate: true };
    }

    function parseOutcomeSummary(pr, parsePending) {
      const lines = Array.isArray(pr.log_tail) ? pr.log_tail : [];
      const joined = lines.join("\\n");
      const foundMatch = joined.match(/Итого после фильтров:\\s*([0-9]+)/);
      const addedMatch = joined.match(/Добавлено в систему:\\s*([0-9]+)/);
      const resultEl = { text: "Поиск ещё не завершён", cls: "" };
      const issueEl = { text: "Идёт выполнение", cls: "" };
      const nextEl = { text: "Дождаться окончания", cls: "" };

      if (parsePending) {
        return {
          result: { text: "Запускаем поиск", cls: "" },
          issue: { text: "Сервер принимает задачу", cls: "" },
          next: { text: "Подождать несколько секунд", cls: "" },
        };
      }
      if (pr.running) {
        return {
          result: { text: "Идёт поиск закупок", cls: "" },
          issue: { text: "Программа проверяет ЕИС и документы", cls: "" },
          next: { text: "Можно просто оставить вкладку открытой", cls: "" },
        };
      }

      if (joined.includes("ERR_CERT_AUTHORITY_INVALID")) {
        resultEl.text = "Новых закупок не получено";
        resultEl.cls = "bad";
        issueEl.text = "Сайт zakupki.gov.ru отклонён из-за проблемы с сертификатом";
        issueEl.cls = "bad";
        nextEl.text = "Проверить сертификаты/антивирус/VPN и повторить поиск";
        nextEl.cls = "warn";
      } else if (joined.includes("ERR_NETWORK_ACCESS_DENIED")) {
        resultEl.text = "Новых закупок не получено";
        resultEl.cls = "bad";
        issueEl.text = "Нет доступа к zakupki.gov.ru из браузера Playwright";
        issueEl.cls = "bad";
        nextEl.text = "Проверить VPN, прокси, фаервол или блокировку сети";
        nextEl.cls = "warn";
      } else if (pr.exit_code === 0) {
        const found = foundMatch ? Number(foundMatch[1]) : null;
        const added = addedMatch ? Number(addedMatch[1]) : null;
        if (found === null) {
          resultEl.text = "Поиск завершён";
          resultEl.cls = "ok";
          issueEl.text = added !== null ? ("Добавлено в базу: " + added) : "Итог поиска сохранён";
          issueEl.cls = "ok";
          nextEl.text = "Проверить список закупок ниже";
        } else if (found === 0) {
          resultEl.text = "Подходящих закупок не найдено";
          resultEl.cls = "warn";
          issueEl.text = "По текущим регионам и ключевым словам результат пустой";
          issueEl.cls = "warn";
          nextEl.text = "Расширить параметры поиска или проверить доступ к ЕИС";
        } else {
          resultEl.text = "Поиск завершён";
          resultEl.cls = "ok";
          issueEl.text = "Найдено: " + found + (added !== null ? " · новых в базе: " + added : "");
          issueEl.cls = "ok";
          nextEl.text = "Проверить список закупок ниже";
        }
      } else if (pr.exit_code !== null && pr.exit_code !== undefined) {
        resultEl.text = "Поиск завершился с ошибкой";
        resultEl.cls = "bad";
        issueEl.text = "Подробности скрыты в технических деталях";
        issueEl.cls = "warn";
        nextEl.text = "Открыть детали и посмотреть последнюю ошибку";
      }

      return { result: resultEl, issue: issueEl, next: nextEl };
    }

    let siteChatOpen = false;
    let siteChatLastKey = "";

    function toggleSiteChat(force) {
      const panel = document.getElementById("siteChatPanel");
      const fab = document.getElementById("siteChatFab");
      if (!panel) return;
      siteChatOpen = typeof force === "boolean" ? force : panel.hidden;
      panel.hidden = !siteChatOpen;
      if (siteChatOpen && fab) fab.classList.remove("has-new");
      const feed = document.getElementById("siteChatFeed");
      if (siteChatOpen && feed) feed.scrollTop = feed.scrollHeight;
    }

    function renderSiteChat(events) {
      const feed = document.getElementById("siteChatFeed");
      const fab = document.getElementById("siteChatFab");
      if (!feed) return;
      const list = Array.isArray(events) ? events.slice(-90) : [];
      const last = list.length ? JSON.stringify(list[list.length - 1]) : "";
      if (last && last !== siteChatLastKey && !siteChatOpen && fab) fab.classList.add("has-new");
      siteChatLastKey = last;
      feed.replaceChildren();
      if (!list.length) {
        const empty = document.createElement("div");
        empty.className = "site-chat-empty";
        empty.textContent = "Пока событий нет. Когда запустится сравнение цен, здесь появятся сообщения как в Telegram.";
        feed.appendChild(empty);
        return;
      }
      for (const ev of list) {
        const msg = document.createElement("div");
        const kind = String(ev.kind || "");
        msg.className = "site-chat-msg" + (kind ? " is-" + kind : "");
        const meta = document.createElement("div");
        meta.className = "site-chat-meta";
        const ts = String(ev.ts || "").replace("T", " ");
        const tid = ev.tender_id ? " · " + ev.tender_id : "";
        meta.textContent = (ts || "сейчас") + tid;
        const text = document.createElement("div");
        text.className = "site-chat-text";
        const icon = kind === "done" ? "✅" : (kind === "error" || kind === "warn") ? "⚠️" : kind === "begin" ? "🔎" : "🧾";
        const rawText = String(ev.text || "");
        text.textContent = rawText.startsWith(icon) ? rawText : (icon + " " + rawText);
        msg.appendChild(meta);
        msg.appendChild(text);
        feed.appendChild(msg);
      }
      if (siteChatOpen) feed.scrollTop = feed.scrollHeight;
    }

    async function refreshStatus() {
      let pr = { running: false };
      let mr = { running: false };
      try {
        const rp = await fetch("/api/parse-status");
        if (rp.ok) pr = await rp.json();
      } catch (e) {}
      try {
        const rm = await fetch("/api/merge-site-status");
        if (rm.ok) mr = await rm.json();
      } catch (e) {}
      try {
        if (pr.running) parsePendingUntil = 0;
        const parsePending = !pr.running && Date.now() < parsePendingUntil;
        parseRunning = !!pr.running || parsePending;
        if (pr.running && pr.started_at) {
          const ms = Date.parse(pr.started_at);
          parseStartMs = Number.isNaN(ms) ? null : ms;
        } else if (!parsePending) {
          parseStartMs = null;
        }

        const hasParseHistory = !!(
          (pr.log_tail && pr.log_tail.length)
          || pr.command
          || pr.ended_at
          || pr.exit_code !== null && pr.exit_code !== undefined
        );
        const panel = document.getElementById("parseProgressPanel");
        if (panel) panel.hidden = !(parseRunning || hasParseHistory);

        const progressView = parseProgressView(pr, parsePending);
        const label = document.getElementById("parseProgressLabel");
        if (label) label.textContent = progressView.title;

        const lc = document.getElementById("parseProgressLogCount");
        if (lc && (parseRunning || hasParseHistory)) {
          const n = pr.log_lines_count ?? 0;
          lc.textContent = parsePending
            ? "Поиск запускается."
            : pr.running
            ? "Строк в логе: " + n + " (растёт, пока идёт вывод)."
            : "Строк в логе: " + n + ".";
        } else if (lc) lc.textContent = "";

        const bar = document.getElementById("parseBarFill");
        if (bar) {
          if (parseRunning && progressView.indeterminate) {
            bar.classList.add("running");
          } else {
            bar.classList.remove("running");
          }
          bar.style.width = Math.min(100, Math.max(0, progressView.percent)) + "%";
        }

        const status = document.getElementById("parseStatus");
        const logs = document.getElementById("parseLogs");
        const cmdLine = document.getElementById("parseCommandLine");
        const summary = parseOutcomeSummary(pr, parsePending);
        const resultMain = document.getElementById("parseResultMain");
        const resultIssue = document.getElementById("parseResultIssue");
        const resultNext = document.getElementById("parseResultNext");
        let st = progressView.detail || (parsePending ? "запускается" : pr.running ? "идёт выполнение" : "ожидание");
        if (!pr.running && pr.exit_code !== null && pr.exit_code !== undefined) {
          st += " · код выхода: " + pr.exit_code;
        }
        if (pr.ended_at && !pr.running) st += " · завершено: " + pr.ended_at;
        status.textContent = st;
        if (resultMain) {
          resultMain.textContent = summary.result.text;
          resultMain.className = "parse-summary-value" + (summary.result.cls ? " " + summary.result.cls : "");
        }
        if (resultIssue) {
          resultIssue.textContent = summary.issue.text;
          resultIssue.className = "parse-summary-value" + (summary.issue.cls ? " " + summary.issue.cls : "");
        }
        if (resultNext) {
          resultNext.textContent = summary.next.text;
          resultNext.className = "parse-summary-value" + (summary.next.cls ? " " + summary.next.cls : "");
        }
        if (cmdLine) {
          cmdLine.textContent = pr.running && pr.command ? "Команда: " + pr.command : "";
        }
        if (logs && (!parsePending || pr.log_tail && pr.log_tail.length)) {
          logs.textContent = (pr.log_tail && pr.log_tail.length ? pr.log_tail.join("\\n") : "");
          logs.scrollTop = logs.scrollHeight;
        }

        if (parseRunning) {
          updateParseElapsed();
        } else {
          const endLine = document.getElementById("parseProgressTime");
          if (endLine) {
            if (pr.started_at && pr.ended_at) {
              const ms1 = Date.parse(pr.started_at);
              const ms2 = Date.parse(pr.ended_at);
              if (!Number.isNaN(ms1) && !Number.isNaN(ms2) && ms2 >= ms1) {
                endLine.textContent = "Длительность: " + formatElapsed((ms2 - ms1) / 1000);
              } else {
                endLine.textContent = pr.ended_at ? ("Завершено: " + pr.ended_at) : "";
              }
            } else {
              endLine.textContent = pr.ended_at ? ("Завершено: " + pr.ended_at) : "";
            }
          }
        }

        const mergeRun = !!mr.running;
        const mp = document.getElementById("mergeSitePanel");
        if (mp) mp.hidden = !mergeRun;

        const pct = typeof mr.percent === "number" ? mr.percent : 0;
        const fill = document.getElementById("mergeBarFill");
        const ptext = document.getElementById("mergePercentText");
        const det = document.getElementById("mergeSiteDetail");
        const mlogs = document.getElementById("mergeSiteLogs");
        const marketDone = Number(mr.market_done || 0);
        const marketTotal = Number(mr.market_total || 0);
        const marketLeft = Number(mr.market_left || Math.max(0, marketTotal - marketDone));
        if (fill) fill.style.width = Math.min(100, Math.max(0, pct)) + "%";
        if (ptext) {
          let text = pct + "% · тендеры " + (mr.done ?? 0) + " / " + (mr.total ?? 0);
          if (marketTotal > 0) text += " · рынок " + marketDone + " / " + marketTotal;
          if (mr.current_tid) text += " · сейчас: " + mr.current_tid;
          ptext.textContent = text;
        }
        if (det) {
          if (mergeRun && marketTotal > 0 && marketDone < marketTotal) {
            det.textContent = "Поиск рынка идёт по строкам сметы: обработано " + marketDone + " из " + marketTotal + ", осталось " + marketLeft + ".";
          } else if (mergeRun && marketTotal > 0 && marketDone >= marketTotal) {
            det.textContent = "Рынок обработал строки сметы, собираем страницу сравнения…";
          } else {
            det.textContent = mergeRun ? "Ищем рыночные цены и собираем страницы сравнения…" : "";
          }
        }
        if (mlogs) {
          mlogs.textContent = (mr.log_tail && mr.log_tail.length ? mr.log_tail.join("\\n") : "");
          mlogs.scrollTop = mlogs.scrollHeight;
        }
        renderSiteChat(mr.chat_events || []);

        const mis = document.getElementById("mergeIdleSummary");
        const mreason = document.getElementById("mergeMissingReason");
        if (mis) {
          if (!mergeRun && mr.last_ended_at) {
            mis.textContent = "Последний прогон сводок: " + mr.last_ended_at + " — " + (mr.last_summary || "");
          } else if (mergeRun) {
            mis.textContent = "";
          }
        }
        if (mreason) {
          const reasons = mr.last_reason_counts || {};
          const txt = "Не удалось обработать: без сметы " + (reasons.no_estimate || 0)
            + ", ошибка поиска цен " + (reasons.market_failed || 0)
            + ", ошибка объединения данных " + (reasons.merge_failed || 0)
            + ", ошибка страницы результата " + (reasons.html_failed || 0);
          mreason.textContent = !mergeRun && mr.last_ended_at ? txt : "";
        }

        window.__mergeRunLive = mergeRun;
        applyToolbarDisabled(parseRunning, mergeRun);
        if (parseAutoReloadArmed && !pr.running && pr.exit_code === 0 && !mergeRun) {
          parseAutoReloadArmed = false;
          window.setTimeout(function() {
            location.reload();
          }, 900);
        }
        if (typeof window._wasMergeRun === "undefined") window._wasMergeRun = false;
        if (window._wasMergeRun && !mergeRun) refreshCoverage();
        window._wasMergeRun = mergeRun;
      } catch (e) {}
    }

    setInterval(refreshStatus, 2000);
    setInterval(refreshCoverage, 5000);
    setInterval(refreshPushState, 5000);
    refreshStatus();
    refreshCoverage();
    refreshPushState();
    updatePushButtonUi();
  </script>
</body>
</html>
"""


TENDERS_SHELL_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  <title>Тендеры</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: rgba(255,255,255,.94);
      --line: #dbe5f0;
      --text: #172235;
      --muted: #64748b;
      --accent: #2d6fd2;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", Arial, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(92, 149, 224, 0.12), transparent 34%),
        linear-gradient(180deg, #ffffff 0%, var(--bg) 100%);
      color: var(--text);
    }
    .page {
      width: 100%;
      max-width: none;
      margin: 0;
      padding: 18px 18px 0;
    }
    .tabs {
      display: inline-flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 18px;
      padding: 6px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.82);
      box-shadow: 0 16px 36px rgba(40, 70, 118, 0.08);
      backdrop-filter: blur(14px);
    }
    .tab {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      padding: 0 14px;
      border-radius: 12px;
      color: #35506f;
      text-decoration: none;
      font-size: 14px;
      font-weight: 700;
    }
    .tab.is-active {
      background: linear-gradient(180deg, #ffffff, #eef5ff);
      color: var(--accent);
      box-shadow: inset 0 0 0 1px #cfe0f7;
    }
    .shell {
      position: relative;
      min-height: calc(100vh - 86px);
      border-radius: 0;
      border: 0;
      background: transparent;
      box-shadow: none;
      overflow: visible;
    }
    .loader {
      position: absolute;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 28px;
      background: linear-gradient(180deg, rgba(255,255,255,.92), rgba(246,249,253,.96));
      transition: opacity .32s ease, visibility .32s ease;
      z-index: 3;
    }
    .loader.is-hidden {
      opacity: 0;
      visibility: hidden;
      pointer-events: none;
    }
    .loader-card {
      width: min(780px, 100%);
      padding: 26px;
      border-radius: 24px;
      border: 1px solid #e0e8f3;
      background: var(--panel);
      box-shadow: 0 24px 48px rgba(40, 69, 110, 0.10);
    }
    .loader-card h1 {
      margin: 0 0 8px;
      font-size: 28px;
      line-height: 1.15;
    }
    .loader-card p {
      margin: 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.55;
    }
    .bar {
      margin-top: 18px;
      height: 10px;
      border-radius: 999px;
      overflow: hidden;
      background: #e6eef8;
    }
    .bar-fill {
      width: 36%;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, #4d8be6, #8abbff);
      animation: loadbar 1.4s ease-in-out infinite;
      transform-origin: left center;
    }
    .skeleton-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-top: 18px;
    }
    .sk {
      border-radius: 16px;
      background: linear-gradient(90deg, #eef3fa 20%, #f8fbff 50%, #eef3fa 80%);
      background-size: 220% 100%;
      animation: shimmer 1.35s linear infinite;
    }
    .sk.big { height: 112px; }
    .sk.small { height: 68px; }
    .loader-note {
      margin-top: 14px;
      color: #50657f;
      font-size: 13px;
      line-height: 1.5;
    }
    .loader-note strong { color: #173050; }
    iframe {
      display: block;
      width: 100%;
      min-height: calc(100vh - 86px);
      border: 0;
      background: transparent;
      opacity: 0;
      transition: opacity .28s ease;
    }
    iframe.is-ready { opacity: 1; }
    @keyframes shimmer {
      0% { background-position: 200% 0; }
      100% { background-position: -200% 0; }
    }
    @keyframes loadbar {
      0% { transform: translateX(-105%) scaleX(.85); }
      55% { transform: translateX(150%) scaleX(1.08); }
      100% { transform: translateX(210%) scaleX(.9); }
    }
    @media (max-width: 760px) {
      .page { padding: 16px 12px 0; }
      .shell,
      iframe { min-height: calc(100vh - 78px); }
      .loader-card { padding: 18px; border-radius: 18px; }
      .loader-card h1 { font-size: 22px; }
      .skeleton-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="page">
    <nav class="tabs">
      <a class="tab is-active" href="/tenders">Тендеры</a>
      <a class="tab" href="/estimates">Сметы</a>
      <a class="tab" href="/research">Поиск по позиции</a>
    </nav>

    <section class="shell">
      <div class="loader" id="tendersLoader">
        <div class="loader-card">
          <h1>Загружаем тендеры</h1>
          <p>Страница тяжёлая: здесь много карточек, статусов, ссылок на Excel и прогресса по рынку. Сначала показываем оболочку, потом подтягиваем содержимое без белого экрана.</p>
          <div class="bar"><div class="bar-fill"></div></div>
          <div class="skeleton-grid" aria-hidden="true">
            <div class="sk big"></div>
            <div class="sk big"></div>
            <div class="sk small"></div>
            <div class="sk small"></div>
          </div>
          <div class="loader-note" id="tendersLoaderNote">Если карточек много, это может занять несколько секунд. <strong>Сметы открываются быстрее</strong> и доступны сразу по умолчанию.</div>
        </div>
      </div>
      <iframe id="tendersFrame" src="{{ iframe_src }}" title="Тендеры" loading="eager"></iframe>
    </section>
  </div>
  <script>
    (function() {
      const frame = document.getElementById("tendersFrame");
      const loader = document.getElementById("tendersLoader");
      const note = document.getElementById("tendersLoaderNote");
      let watchdog = setTimeout(function() {
        if (note) {
          note.innerHTML = 'Загрузка идет дольше обычного. Страница всё еще собирается, это не зависание. Можно подождать или открыть <strong>Сметы</strong>.';
        }
      }, 6000);

      frame.addEventListener("load", function() {
        window.clearTimeout(watchdog);
        frame.classList.add("is-ready");
        loader.classList.add("is-hidden");
      });
    })();
  </script>
</body>
</html>
"""


def _html_reports_by_tender_id() -> dict[str, str]:
    """Номер тендера → имя файла ОТЧЕТ_ПО_СМЕТАМ_<id>.html (без общих сводок)."""
    out: dict[str, str] = {}
    if not REPORTS_DIR.exists():
        return out
    prefix = "ОТЧЕТ_ПО_СМЕТАМ_"
    for p in REPORTS_DIR.iterdir():
        if not p.is_file() or not p.name.startswith(prefix) or not p.name.endswith(".html"):
            continue
        if "ОБЩИЙ" in p.name:
            continue
        tid = p.name[len(prefix) : -len(".html")]
        if tid:
            out[tid] = p.name
    return out


def _smet_report_html_has_position_groups(path: Path) -> bool:
    """True, если в отчёте main.py есть раскрытые блоки сметы (не только «Нет данных для отображения»)."""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            chunk = f.read(512_000)
    except OSError:
        return False
    return "<details class=\"group\"" in chunk


def _estimate_rows_by_tender_id() -> dict[str, int]:
    """Количество строк-работ в ОТЧЕТ_ПО_СМЕТАМ_<id>.xlsx."""
    out: dict[str, int] = {}
    prefix = "ОТЧЕТ_ПО_СМЕТАМ_"
    if not REPORTS_DIR.is_dir():
        return out
    for p in REPORTS_DIR.glob(f"{prefix}*.xlsx"):
        if "ОБЩИЙ" in p.name:
            continue
        tid = p.stem[len(prefix) :]
        if not tid:
            continue
        try:
            # Считаем строки в файле сметы; без тяжелых вычислений.
            df = pd.read_excel(p, usecols=[0])
            out[tid] = int(len(df))
        except Exception:
            out[tid] = 0
    return out


def _live_market_progress_by_tender() -> dict[str, tuple[int, int]]:
    """Только живой прогресс активного запуска, без чтения всех Excel при открытии списка тендеров."""
    with merge_site_lock:
        running = bool(merge_site_state.get("running"))
        current_tid = str(merge_site_state.get("current_tid") or "").strip()
        done = int(merge_site_state.get("market_done") or 0)
        total = int(merge_site_state.get("market_total") or 0)
    if not running or not current_tid or total <= 0:
        return {}
    return {current_tid: (max(0, done), max(0, total))}


def _market_progress_for_tender(tid: str) -> tuple[int, int]:
    """
    Прогресс Алисы по строкам сметы: (готово, всего) для тех же строк,
    которые реально идут в real_market_scraper.py
    (без явных дублей и без слишком коротких названий).
    """
    from autobot.market_analytics import COL_DUP, COL_NAME

    tid = (tid or "").strip()
    if not tid:
        return 0, 0
    est_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    if not est_path.is_file():
        return 0, 0
    try:
        est = pd.read_excel(est_path)
    except Exception:
        return 0, 0
    if COL_NAME not in est.columns:
        return 0, 0

    total = 0
    for _, row in est.iterrows():
        if COL_DUP in est.columns and str(row.get(COL_DUP, "")).strip() == "Да":
            continue
        name = str(row.get(COL_NAME, "") or "").strip()
        if len(name) < 8:
            continue
        total += 1
    if total <= 0:
        return 0, 0

    market_path = _price_output_path_for_tender(tid)
    if not market_path.is_file():
        return 0, total
    try:
        ali = pd.read_excel(market_path)
    except Exception:
        return 0, total
    if COL_NAME not in ali.columns:
        return 0, total

    ren: dict[str, str] = {}
    if "Цены за ед. (рынок, руб)" not in ali.columns and "Цены (строго, руб)" in ali.columns:
        ren["Цены (строго, руб)"] = "Цены за ед. (рынок, руб)"
    if ren:
        ali = ali.rename(columns=ren)

    done = 0
    for _, row in ali.iterrows():
        name = str(row.get(COL_NAME, "") or "").strip()
        if not name:
            continue
        # Частичный файл с рыночными источниками содержит только уже пройденные строки.
        # Для прогресса считаем строку обработанной даже если цена не найдена
        # или ответ был пустым/ошибочным — Telegram считает этот шаг так же.
        done += 1
    return min(done, total), total


def _tender_deadline_text(meta: dict) -> str:
    for key in (
        "deadline_date",
        "end_date",
        "close_date",
        "finish_date",
        "submission_end",
        "application_end",
        "bidding_end_date",
    ):
        txt = str(meta.get(key) or "").strip()
        if txt:
            return txt
    return ""

def _legacy_price_output_path_for_tender(tid: str) -> Path:
    est_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    return REPORTS_DIR / f"РЫНОК_ИСТОЧНИКИ_{est_path.stem}.xlsx"


def _market_output_path_for_tender(tid: str) -> Path:
    est_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    return REPORTS_DIR / f"РЫНОК_ИСТОЧНИКИ_{est_path.stem}.xlsx"


def _price_output_path_for_tender(tid: str) -> Path:
    market_path = _market_output_path_for_tender(tid)
    if market_path.is_file():
        return market_path
    return _legacy_price_output_path_for_tender(tid)


def _safe_download_stem(title: str, fallback: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё._ -]+", "_", str(title or fallback)).strip(" ._") or str(fallback or "report")


def _crm_base_url() -> str:
    return (
        os.environ.get("PMBI_CRM_URL")
        or os.environ.get("PMBI_CRM_BASE_URL")
        or "http://127.0.0.1:8080"
    ).strip().rstrip("/")


def _crm_public_base_url() -> str:
    return (
        os.environ.get("PMBI_CRM_PUBLIC_URL")
        or os.environ.get("PMBI_PUBLIC_BASE_URL")
        or os.environ.get("PMBI_CRM_URL")
        or "http://127.0.0.1:8080"
    ).strip().rstrip("/")


def _configured_crm_parent_origin() -> str:
    """Return only an explicitly configured browser origin for the PM.bi parent."""
    raw_value = (
        os.environ.get("PMBI_CRM_PARENT_ORIGIN")
        or os.environ.get("PMBI_CRM_PUBLIC_URL")
        or os.environ.get("PMBI_PUBLIC_BASE_URL")
        or ""
    ).strip()
    if not raw_value:
        return ""
    try:
        parsed = urlparse(raw_value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        hostname = parsed.hostname.lower()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        parsed_port = parsed.port
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        port = f":{parsed_port}" if parsed_port and parsed_port != default_port else ""
    except ValueError:
        return ""
    return f"{parsed.scheme.lower()}://{hostname}{port}"


def _legacy_browser_crm_export_allowed() -> bool:
    """Service-account CRM calls are opt-in and must not back an embedded browser flow."""
    return str(os.environ.get("PMBI_ALLOW_LEGACY_BROWSER_CRM_EXPORT") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _legacy_browser_crm_export_denied():
    return (
        jsonify(
            {
                "ok": False,
                "error": "legacy_browser_crm_export_disabled",
                "message": "Добавление в CRM доступно через AutoBot внутри PM.bi.",
            }
        ),
        403,
    )


def _issue_estimate_import_capability(estimate_id: str, *, ttl_seconds: int = 1200) -> str:
    clean_estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    expires_at = int(time.time()) + max(60, min(int(ttl_seconds), 3600))
    unsigned = f"{clean_estimate_id}.{expires_at}"
    signature = hmac.new(
        _ESTIMATE_IMPORT_CAPABILITY_SECRET,
        unsigned.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{unsigned}.{signature}"


def _verify_estimate_import_capability(estimate_id: str, token: str) -> bool:
    clean_estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    try:
        token_estimate_id, expires_raw, signature = str(token or "").rsplit(".", 2)
        expires_at = int(expires_raw)
    except (TypeError, ValueError):
        return False
    if token_estimate_id != clean_estimate_id or expires_at < int(time.time()):
        return False
    unsigned = f"{token_estimate_id}.{expires_at}"
    expected = hmac.new(
        _ESTIMATE_IMPORT_CAPABILITY_SECRET,
        unsigned.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def _crm_project_url(project_id: int, tab: str | None = None) -> str:
    query = f"openProject={int(project_id)}"
    if tab:
        query += f"&tab={quote(str(tab), safe='')}"
    return f"/app/projects?{query}"


def _crm_credentials() -> tuple[str, str] | None:
    login = (os.environ.get("PMBI_CRM_LOGIN") or "").strip()
    password = os.environ.get("PMBI_CRM_PASSWORD") or ""
    if login and password:
        return login, password
    return None


def _float_or_none(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


class EstimateImportTooLargeError(ValueError):
    """Raised instead of silently truncating an estimate sent to CRM."""


def _crm_estimate_source_item_key(
    *,
    source_scope: object,
    sheet: object,
    excel_row: object,
    item_no: object,
    row_index: int,
    basis_code: object,
    title: object,
) -> str:
    """Return a stable row identity that remains unique across workbook sheets."""

    normalized_sheet = re.sub(r"\s+", " ", str(sheet or "").strip()).casefold()
    normalized_excel_row = str(excel_row or "").strip()
    normalized_item_no = re.sub(r"\s+", " ", str(item_no or "").strip()).casefold()
    location = normalized_excel_row or normalized_item_no or str(row_index)
    if normalized_excel_row:
        row_identity = {"kind": "excel_row", "value": normalized_excel_row}
    elif normalized_item_no:
        row_identity = {
            "kind": "item_no",
            "value": normalized_item_no,
            "basis_code": re.sub(r"\s+", " ", str(basis_code or "").strip()).casefold(),
        }
    else:
        row_identity = {
            "kind": "sequence",
            "value": int(row_index),
            "basis_code": re.sub(r"\s+", " ", str(basis_code or "").strip()).casefold(),
            "title": re.sub(r"\s+", " ", str(title or "").strip()).casefold(),
        }
    identity = json.dumps(
        {
            "source": str(source_scope or "").strip().casefold(),
            "sheet": normalized_sheet,
            "row": row_identity,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    sheet_label = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "-", str(sheet or "").strip())[:48] or "sheet"
    location_label = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "-", location)[:64] or str(row_index)
    return f"{sheet_label}:{location_label}:{digest}"


def _tender_estimate_materials_for_crm(tender_id: str) -> list[dict]:
    from autobot.estimate_publication_recovery import consistent_report
    if not str(tender_id or '').strip():
        return []
    with consistent_report(REPORTS_DIR, tender_id):
        return _consistent_tender_estimate_materials_for_crm(tender_id)


def _consistent_tender_estimate_materials_for_crm(tender_id: str) -> list[dict]:
    from autobot.market_analytics import COL_DUP, COL_ITEM, COL_NAME, COL_QTY, COL_SUM, COL_UNIT, COL_UNIT_PRICE
    from autobot.market_strategy import build_search_plan

    tid = (tender_id or "").strip()
    if not tid:
        return []
    path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    if not path.is_file():
        return []
    try:
        df = pd.read_excel(path)
    except Exception:
        return []
    if COL_NAME not in df.columns:
        return []

    try:
        max_rows = int(os.environ.get("PMBI_CRM_MAX_MATERIALS", "10000") or "10000")
    except ValueError:
        max_rows = 10000
    max_rows = max(1, min(max_rows, 10000))

    meta = load_tender_metadata().get(tid, {}) or {}
    region = str(meta.get("region") or "").strip()
    materials: list[dict] = []
    originals = {}
    if 'estimate_version' in df and df['estimate_version'].fillna('').astype(str).str.startswith('correction:').any():
        from autobot import tender_corrections
        value = tender_corrections.snapshot_locked(REPORTS_DIR, tid)
        originals = {row['position_id']: row for row in value['original_rows']}
    for row_index, (_, row) in enumerate(df.iterrows(), start=1):
        if COL_DUP in df.columns and str(row.get(COL_DUP, "")).strip().casefold() in {"да", "yes", "true", "1"}:
            continue
        title = str(row.get(COL_NAME, "") or "").strip()
        corrected = str(row.get('estimate_version') or '').startswith('correction:')
        original_row = originals.get(str(row.get('position_id') or ''), {})
        if not title or (len(title) < 4 and not corrected):
            continue
        qty = _float_or_none(row.get(COL_QTY))
        unit_price = _float_or_none(row.get(COL_UNIT_PRICE))
        total = _float_or_none(row.get(COL_SUM))
        if corrected and (qty is None or qty <= 0 or unit_price is None or unit_price < 0
                          or total is None or total < 0 or str(row.get(COL_UNIT) or '').strip().casefold() in {'','nan','none'}):
            from autobot.uploaded_corrections import CorrectionError
            raise CorrectionError('Перед импортом уточните единицу, положительное количество, цену и сумму исправленной строки «' + title[:120] + '».', 422)
        if not corrected and (qty is None or qty <= 0):
            qty = 1.0
        if not corrected and (unit_price is None or (unit_price <= 0 and total is not None and total > 0)):
            unit_price = _float_or_none(total / qty) if total is not None and qty > 0 else 0.0
            unit_price = unit_price or 0.0
        source_file = str(row.get("Файл ЛСР", "") or "").strip()
        if source_file.casefold() in {"nan", "none"}:
            source_file = ""
        file_name = Path(source_file).name if source_file else f"Смета тендера {tid}.xlsx"
        estimate_title = Path(file_name).stem or f"Смета тендера {tid}"
        section = _normalize_section_title(str(row.get("Раздел", "") or ""))
        basis_code = str(
            row.get("basis_code", "")
            or row.get("Шифр расценки", "")
            or row.get("Код", "")
            or ""
        ).strip()
        plan = build_search_plan(title, row.get(COL_UNIT, ""), basis_code, section, region)
        item_kind = plan.position.slug
        if item_kind not in {"work", "material", "service", "product", "other"}:
            item_kind = "other"
        notes = [f"Тендер: {tid}"]
        item_no = str(row.get(COL_ITEM, "") or "").strip()
        if item_no:
            notes.append(f"Позиция: {item_no}")
        if total is not None and total > 0:
            notes.append(f"Сумма по смете: {total:.2f} руб.")
        if len(materials) >= max_rows:
            raise EstimateImportTooLargeError(
                f"В смете больше {max_rows} подходящих позиций. "
                "Импорт остановлен без изменений: увеличьте PMBI_CRM_MAX_MATERIALS "
                "или разделите смету."
            )
        sheet = str(row.get("Лист", "") or "").strip()
        excel_row = row.get("Строка Excel")
        materials.append(
            {
                "title": title[:500],
                "unit": str(row.get(COL_UNIT, "") or "").strip() or "шт",
                "planned_qty": float(qty) if corrected else max(0.000001, float(qty)),
                "planned_price": float(unit_price) if corrected else max(0.0, float(unit_price or 0)),
                "planned_total": float(total) if corrected else max(0.0, float(total or (qty * (unit_price or 0)))),
                "article": basis_code,
                "code": basis_code,
                "basis_code": basis_code,
                "item_kind": item_kind,
                "type": item_kind,
                "type_label": plan.position.label,
                "section_title": section or None,
                "section": section or "",
                "estimate_file_name": file_name,
                "estimate_title": estimate_title,
                "source_item_key": _crm_estimate_source_item_key(
                    source_scope=file_name,
                    sheet=original_row.get('sheet', sheet),
                    excel_row=original_row.get('excel_row', excel_row),
                    item_no=original_row.get('item_no', item_no),
                    row_index=row_index,
                    basis_code=original_row.get('basis_code', basis_code),
                    title=original_row.get('name', title),
                ),
                "notes": "; ".join(notes),
            }
        )
    return materials


def _estimate_materials_for_crm(estimate_id: str, *, document=None) -> list[dict]:
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    if not estimate_id:
        return []
    meta, rows = document if document is not None else _load_estimate_document(estimate_id)
    if not rows:
        return []

    try:
        max_rows = int(os.environ.get("PMBI_CRM_MAX_MATERIALS", "10000") or "10000")
    except ValueError:
        max_rows = 10000
    max_rows = max(1, min(max_rows, 10000))

    meta = meta or {}
    originals = {}
    if any(str(row.get('estimate_version') or '').startswith('correction:') for row in rows):
        from autobot.uploaded_estimates import original_document
        originals = {row['position_id']:row for row in original_document(USER_ESTIMATES_DIR, estimate_id)[1]}
    file_name = str(meta.get("original_filename") or f"Смета {estimate_id}.xlsx").strip()
    estimate_title = str(meta.get("title") or Path(file_name).stem or f"Смета {estimate_id}").strip()
    materials: list[dict] = []
    for row_index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        corrected = str(row.get('estimate_version') or '').startswith('correction:')
        original_row = originals.get(row.get('position_id'), row)
        title = str(row.get("name") or "").strip()
        if not title or (len(title) < 4 and not corrected):
            continue
        qty = _float_or_none(row.get("qty"))
        unit_price = _float_or_none(row.get("unit_price"))
        total = _float_or_none(row.get("total"))
        if corrected and (qty is None or qty <= 0 or unit_price is None or unit_price < 0
                          or total is None or total < 0 or not str(row.get('unit') or '').strip()):
            from autobot.uploaded_corrections import CorrectionError
            raise CorrectionError('Перед импортом уточните единицу, положительное количество, цену и сумму исправленной строки «' + title[:120] + '».', 422)
        if not corrected and (qty is None or qty <= 0):
            qty = 1.0
        if not corrected and (unit_price is None or (unit_price <= 0 and total is not None and total > 0)):
            unit_price = _float_or_none(total / qty) if total is not None and qty > 0 else 0.0
            unit_price = unit_price or 0.0
        notes = [f"Смета: {estimate_id}"]
        item_no = str(row.get("item_no") or "").strip()
        if item_no:
            notes.append(f"Позиция: {item_no}")
        section = _normalize_section_title(str(row.get("section") or ""))
        if section:
            notes.append(f"Раздел: {section}")
        sheet = str(row.get("sheet") or "").strip()
        if sheet:
            notes.append(f"Лист: {sheet}")
        excel_row = row.get("excel_row")
        if excel_row not in (None, ""):
            notes.append(f"Строка Excel: {excel_row}")
        basis_code = str(row.get("basis_code") or row.get("code") or row.get("article") or "").strip()
        if basis_code:
            notes.append(f"Код: {basis_code}")
        type_key = str(row.get("type") or "").strip().lower()
        type_label = str(row.get("type_label") or "").strip()
        code_type = _estimate_code_type(basis_code)
        if code_type and not corrected:
            type_key, type_label = code_type
        if type_label:
            notes.append(f"Тип: {type_label}")
        if total is not None and total > 0:
            notes.append(f"Сумма по смете: {total:.2f} руб.")
        item_kind = type_key if type_key in {"work", "material", "service", "product", "other"} else (type_label or "")
        planned_total = total if corrected else (_float_or_none(total if total is not None and total > 0 else qty * (unit_price or 0.0)) or 0.0)
        if len(materials) >= max_rows:
            raise EstimateImportTooLargeError(
                f"В смете «{estimate_title}» больше {max_rows} подходящих позиций. "
                "Импорт остановлен без изменений: разделите смету или увеличьте "
                "PMBI_CRM_MAX_MATERIALS."
            )
        materials.append(
            {
                "title": title[:500],
                "unit": str(row.get("unit") or "").strip() or "шт",
                "planned_qty": max(0.000001, float(qty)),
                "planned_price": max(0.0, float(unit_price or 0.0)),
                "planned_total": max(0.0, float(planned_total)),
                "article": basis_code,
                "code": basis_code,
                "basis_code": basis_code,
                "item_kind": item_kind,
                "type": type_key or item_kind,
                "type_label": type_label,
                "section_title": section or None,
                "section": section or "",
                "estimate_file_name": file_name,
                "estimate_title": estimate_title,
                "source_external_id": estimate_id,
                "source_item_key": _crm_estimate_source_item_key(
                    source_scope=estimate_id,
                    sheet=str(original_row.get('sheet') or ''),
                    excel_row=original_row.get('excel_row'),
                    item_no=str(original_row.get('item_no') or ''),
                    row_index=row_index,
                    basis_code=str(original_row.get('basis_code') or original_row.get('code') or original_row.get('article') or ''),
                    title=str(original_row.get('name') or ''),
                ),
                "notes": "; ".join(notes),
            }
        )
    return materials


def _estimate_crm_prefill(estimate_id: str, *, document=None) -> dict:
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    meta, rows = document if document is not None else _load_estimate_document(estimate_id)
    meta = meta or {}
    summary = _summarize_estimate_rows(rows)
    type_counts = summary.get("type_counts") or {}
    type_labels = {
        "material": "материалы",
        "work": "работы",
        "service": "услуги",
        "product": "товары",
        "other": "прочее",
    }
    type_bits = [f"{type_labels.get(key, key)}: {int(val)}" for key, val in type_counts.items() if int(val or 0) > 0]
    estimate_title = str(meta.get("title") or f"Смета {estimate_id}").strip()[:240]
    original_name = str(meta.get("original_filename") or "").strip()
    created_at = str(meta.get("created_at") or "").strip()
    reconciliation = meta.get("reconciliation") if isinstance(meta.get("reconciliation"), dict) else {}
    declared_total = _float_or_none(reconciliation.get("declared_total"))
    budget = declared_total if declared_total is not None and declared_total > 0 else (_float_or_none(summary.get("total_sum")) or 0.0)
    excluded_adjustment_count = int(_float_or_none(reconciliation.get("excluded_adjustment_count")) or 0)
    excluded_adjustment_total = _float_or_none(reconciliation.get("excluded_adjustment_total"))
    unallocated_total = _float_or_none(reconciliation.get("unallocated_total"))
    description_lines = [
        "Импортировано из auto_bot по отдельной смете.",
        f"Смета: {estimate_title}",
        f"Файл: {original_name}" if original_name else "",
        f"Дата загрузки: {created_at}" if created_at else "",
        f"Строк в смете: {int(summary.get('row_count') or 0)}",
        f"Состав: {', '.join(type_bits)}" if type_bits else "",
        f"Итог исходного файла: {declared_total:.2f} руб." if declared_total is not None else "",
        (
            f"Отрицательные корректировки: {excluded_adjustment_count} шт., "
            f"{excluded_adjustment_total:.2f} руб.; в закупки не добавлены."
            if excluded_adjustment_count and excluded_adjustment_total is not None
            else ""
        ),
        (
            f"Разница между итогом файла и распознанными позициями: {unallocated_total:.2f} руб.; "
            "отдельная закупочная строка не создавалась."
            if unallocated_total is not None and abs(unallocated_total) > 0.01
            else ""
        ),
    ]
    return {
        "estimate_id": estimate_id,
        "estimate_title": estimate_title,
        "original_filename": original_name,
        "created_at": created_at,
        "row_count": int(summary.get("row_count") or 0),
        "total_sum": budget if budget > 0 else None,
        "total_sum_fmt": _fmt_money(budget) if budget > 0 else "—",
        "reconciliation": reconciliation,
        "normalization": meta.get('normalization') or {},
        "project": {
            "title": estimate_title,
            "client_name": "Объект по смете",
            "address": "Адрес уточнить по смете",
            "region": "",
            "contract_no": f"ESTIMATE-{estimate_id}",
            "budget": budget,
            "description": "\n".join(x for x in description_lines if x),
        },
    }


def _build_estimate_crm_project_payload(estimate_id: str, overrides: dict | None = None) -> tuple[dict, list[dict], dict]:
    prefill = _estimate_crm_prefill(estimate_id)
    base_project = dict(prefill.get("project") or {})
    data = overrides if isinstance(overrides, dict) else {}

    def _txt(key: str, default: str, limit: int) -> str:
        raw = str(data.get(key) if key in data else default).strip()
        return raw[:limit]

    budget_raw = data.get("budget")
    if isinstance(budget_raw, str):
        budget_raw = budget_raw.replace(" ", "").replace("\xa0", "").replace(",", ".")
    budget = _float_or_none(budget_raw)
    if budget is None:
        budget = _float_or_none(base_project.get("budget")) or 0.0

    title = _txt("title", str(base_project.get("title") or f"Смета {estimate_id}"), 240) or f"Смета {estimate_id}"
    client_name = _txt("client_name", str(base_project.get("client_name") or "Объект по смете"), 240) or "Объект по смете"
    address = _txt("address", str(base_project.get("address") or "Адрес уточнить по смете"), 500) or "Адрес уточнить по смете"
    region = _txt("region", str(base_project.get("region") or ""), 160)
    contract_no = _txt("contract_no", str(base_project.get("contract_no") or f"ESTIMATE-{estimate_id}"), 120) or f"ESTIMATE-{estimate_id}"
    description = _txt("description", str(base_project.get("description") or ""), 5000)

    project = {
        "title": title,
        "client_name": client_name,
        "address": address,
        "region": region or None,
        "contract_no": contract_no,
        "budget": max(0.0, float(budget or 0.0)),
        "description": description,
    }
    materials = _estimate_materials_for_crm(estimate_id)
    return project, materials, prefill


def _build_estimate_crm_import_payload(estimate_id: str) -> dict:
    """Build the normalized, read-only payload that PM.bi imports as the signed-in user."""
    clean_estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    document = _load_estimate_document(clean_estimate_id)
    prefill = _estimate_crm_prefill(clean_estimate_id, document=document)
    items = _estimate_materials_for_crm(clean_estimate_id, document=document)
    label = str(prefill.get("estimate_title") or f"Смета {clean_estimate_id}").strip()
    reference = f"/estimates/{clean_estimate_id}"
    source = {
        "sourceType": "estimate",
        "sourceKey": clean_estimate_id,
        "externalId": clean_estimate_id,
        "title": label,
        "fileName": str(prefill.get("original_filename") or ""),
        "sourceReference": reference,
    }
    reconciliation = prefill.get("reconciliation")
    if isinstance(reconciliation, dict) and reconciliation:
        source["metadata"] = {"reconciliation": reconciliation}
    if prefill.get('normalization'):
        source.setdefault('metadata', {})['normalization'] = prefill['normalization']
    # Repeat the source identity on every row. A single CRM request can then
    # contain several estimates while preserving each one as an independently
    # replaceable source under the project.
    for item in items:
        item["estimate_source_type"] = "estimate"
        item["estimate_source_key"] = clean_estimate_id
        item["estimate_title"] = label
        item["estimate_file_name"] = source["fileName"]
        item["source_external_id"] = clean_estimate_id
        item["source_reference"] = reference
    return {
        "items": items,
        "source": source,
        "label": label,
        "reference": reference,
        # CRM's existing estimate-import field names are included so the parent
        # can forward this object without copying or reinterpreting estimate data.
        "sourceLabel": label,
        "sourceReference": reference,
        "replace_source": True,
    }


def _build_crm_project_payload(tender_id: str) -> tuple[dict, list[dict]]:
    meta = load_tender_metadata().get(tender_id, {}) or {}
    title = str(meta.get("title") or f"Тендер {tender_id}").strip()
    region = str(meta.get("region") or "").strip()
    eis_url = eis_notice_url(tender_id, meta.get("url"))
    stage = str(meta.get("stage") or "").strip()
    publish_date = str(meta.get("publish_date") or "").strip()
    price = _float_or_none(meta.get("price_rub")) or 0.0
    description_lines = [
        f"Импортировано из auto_bot по тендеру {tender_id}.",
        f"ЕИС: {eis_url}" if eis_url else "",
        f"Этап закупки: {stage}" if stage else "",
        f"Дата публикации: {publish_date}" if publish_date else "",
        f"Регион: {region}" if region else "",
    ]
    project = {
        "title": title[:240],
        "client_name": "Заказчик из ЕИС",
        "address": region or f"Адрес уточнить по тендеру {tender_id}",
        "region": region or None,
        "contract_no": tender_id,
        "budget": price,
        "description": "\n".join(x for x in description_lines if x),
    }
    materials = _tender_estimate_materials_for_crm(tender_id)
    return project, materials


def export_tender_to_crm(tender_id: str, project_id: int | None = None) -> dict:
    project_payload, materials = _build_crm_project_payload(tender_id)
    base = _crm_base_url()
    tid = str(tender_id or "").strip()
    meta = load_tender_metadata().get(tid, {}) or {}
    source_reference = eis_notice_url(tid, meta.get("url"))

    import requests

    with requests.Session() as session:
        _crm_login(session, base)
        projects = _crm_projects(session, base)
        requested_project_id = _requested_crm_project_id(project_id)
        target = next((row for row in projects if row["id"] == requested_project_id), None) if requested_project_id else None
        if requested_project_id and not target:
            raise RuntimeError("Выбранный объект не найден или недоступен в CRM.")
        if not target:
            target = next((row for row in projects if row["contract_no"] == tid), None)

        created_new = target is None
        ensure_starter_task = created_new or requested_project_id is None
        if created_new:
            create_resp = session.post(f"{base}/api/projects", json=project_payload, timeout=30)
            if create_resp.status_code >= 400:
                raise RuntimeError(f"CRM не создала объект: HTTP {create_resp.status_code} {create_resp.text[:300]}")
            project = create_resp.json().get("project") or {}
            target_project_id = int(project.get("id") or 0)
            if target_project_id <= 0:
                raise RuntimeError("CRM создала объект, но не вернула project.id.")
        else:
            target_project_id = int(target["id"])

        import_result = _import_crm_estimate(
            session,
            base,
            target_project_id,
            materials,
            {
                "sourceType": "tender",
                "sourceKey": f"tender:{tid}",
                "externalId": tid,
                "tenderId": tid,
                "title": str(project_payload.get("title") or f"Сметы тендера {tid}"),
                "sourceReference": source_reference,
            },
            source_label=f"Сметы тендера {tid}",
            source_reference=source_reference,
        )

        task_summary = {"tasks": 0, "stages": 0}
        if ensure_starter_task:
            boot_resp = session.post(
                f"{base}/api/projects/{target_project_id}/bootstrap",
                json={
                    "replace_existing": False,
                    "materials": [],
                    "tasks": [
                        {
                            "title": "Проверить тендер и решение об участии",
                            "description": f"Проверить условия закупки {tid}, сметы, сроки и риски перед дальнейшей работой.",
                            "priority": "high",
                            "client_request_id": f"autobot:tender:{tid}:starter",
                        }
                    ],
                },
                timeout=60,
            )
            if boot_resp.status_code >= 400:
                raise RuntimeError(f"Смета импортирована, но стартовая задача не создана: HTTP {boot_resp.status_code} {boot_resp.text[:300]}")
            task_summary = boot_resp.json().get("summary") or task_summary

        summary = {
            "materials": len(import_result.get("items") or []),
            "tasks": int(task_summary.get("tasks") or 0),
            "stages": int(task_summary.get("stages") or 0),
            "estimate_sources": int(import_result.get("estimateSources") or 0),
        }

    return {
        "project_id": target_project_id,
        "project_url": _crm_project_url(target_project_id, "schedule"),
        "materials_sent": int(import_result.get("imported") or len(materials)),
        "summary": summary,
        "already_exists": not created_new,
        "added_to_existing": not created_new,
    }


def export_estimate_to_crm(
    estimate_id: str,
    overrides: dict | None = None,
    project_id: int | None = None,
) -> dict:
    project_payload, _, prefill = _build_estimate_crm_project_payload(estimate_id, overrides=overrides)
    import_payload = _build_estimate_crm_import_payload(estimate_id)
    materials = import_payload["items"]
    base = _crm_base_url()
    source_reference = str(import_payload["reference"])

    import requests

    with requests.Session() as session:
        _crm_login(session, base)
        projects = _crm_projects(session, base)
        requested_project_id = _requested_crm_project_id(project_id)
        target = next((row for row in projects if row["id"] == requested_project_id), None) if requested_project_id else None
        if requested_project_id and not target:
            raise RuntimeError("Выбранный объект не найден или недоступен в CRM.")
        contract_no = str(project_payload.get("contract_no") or "").strip()
        if not target and contract_no:
            target = next((row for row in projects if row["contract_no"] == contract_no), None)

        created_new = target is None
        ensure_starter_task = created_new or requested_project_id is None
        if created_new:
            create_resp = session.post(f"{base}/api/projects", json=project_payload, timeout=30)
            if create_resp.status_code >= 400:
                raise RuntimeError(f"CRM не создала объект: HTTP {create_resp.status_code} {create_resp.text[:300]}")
            project = create_resp.json().get("project") or {}
            target_project_id = int(project.get("id") or 0)
            if target_project_id <= 0:
                raise RuntimeError("CRM создала объект, но не вернула project.id.")
        else:
            target_project_id = int(target["id"])

        import_result = _import_crm_estimate(
            session,
            base,
            target_project_id,
            materials,
            import_payload["source"],
            source_label=str(import_payload["label"]),
            source_reference=source_reference,
        )

        task_summary = {"tasks": 0, "stages": 0}
        if ensure_starter_task:
            boot_resp = session.post(
                f"{base}/api/projects/{target_project_id}/bootstrap",
                json={
                    "replace_existing": False,
                    "materials": [],
                    "tasks": [
                        {
                            "title": "Проверить смету и подготовить объект",
                            "description": f"Проверить импортированную смету «{prefill.get('estimate_title') or estimate_id}», уточнить материалы, объёмы и план работ.",
                            "priority": "high",
                            "client_request_id": f"autobot:estimate:{estimate_id}:starter",
                        }
                    ],
                },
                timeout=60,
            )
            if boot_resp.status_code >= 400:
                raise RuntimeError(f"Смета импортирована, но стартовая задача не создана: HTTP {boot_resp.status_code} {boot_resp.text[:300]}")
            task_summary = boot_resp.json().get("summary") or task_summary

        summary = {
            "materials": len(import_result.get("items") or []),
            "tasks": int(task_summary.get("tasks") or 0),
            "stages": int(task_summary.get("stages") or 0),
            "estimate_sources": int(import_result.get("estimateSources") or 0),
        }

    return {
        "project_id": target_project_id,
        "project_url": _crm_project_url(target_project_id, "schedule"),
        "materials_sent": int(import_result.get("imported") or len(materials)),
        "summary": summary,
        "already_exists": not created_new,
        "added_to_existing": not created_new,
    }


def delete_estimate(estimate_id: str) -> None:
    from autobot.uploaded_market import source_lock
    with source_lock(estimate_id, USER_ESTIMATES_DIR):
        _delete_estimate_locked(estimate_id)


def _delete_estimate_locked(estimate_id: str) -> None:
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    if not estimate_id:
        raise RuntimeError("Нужен estimate_id.")
    meta = _load_estimate_meta(estimate_id)
    if not meta:
        raise RuntimeError("Смета не найдена.")

    from autobot import uploaded_market
    if (_uploaded_market_call(uploaded_market.status, estimate_id) or {}).get('running'):
        raise RuntimeError("Нельзя удалить смету, пока по ней идёт поиск рынка.")

    est_dir = _estimate_dir_path(estimate_id)
    try:
        resolved_root = USER_ESTIMATES_DIR.resolve()
        resolved_dir = est_dir.resolve()
    except Exception:
        resolved_root = USER_ESTIMATES_DIR
        resolved_dir = est_dir
    if resolved_dir == resolved_root or resolved_root not in resolved_dir.parents:
        raise RuntimeError("Небезопасный путь удаления сметы.")

    from autobot import uploaded_estimates
    from autobot.atomic_output import output_lock
    with output_lock(USER_ESTIMATES_INDEX):
        _uploaded_store_call(uploaded_estimates.remove, estimate_id)
        index_items = _read_legacy_estimates_index()
        remaining = [x for x in index_items if str(x.get("id") or "") != estimate_id]
        if len(remaining) != len(index_items):
            _write_estimates_index(remaining)
    if est_dir.is_dir():
        shutil.rmtree(est_dir)


def collect_sidebar_tenders() -> tuple[list[dict], int, int, int]:
    """
    Все тендеры из tenders.json + признаки: есть файл отчёта и есть ли в нём блоки позиций
    (иначе внутри отчёта только «Нет данных для отображения»).
    """
    from autobot.merge_estimate_market import OUT_PREFIX

    meta = load_tender_metadata()
    reports_map = _html_reports_by_tender_id()
    estimate_ids = set(_estimate_xlsx_tender_ids())
    live_market_progress = _live_market_progress_by_tender()
    merge_root = REPO_ROOT / "data" / "reports_site"
    items: list[dict] = []
    for tid, tmeta in meta.items():
        report_file = reports_map.get(tid, "")
        has_report = bool(report_file) and (REPORTS_DIR / report_file).is_file()
        if not has_report:
            report_file = ""
        rp = REPORTS_DIR / report_file if report_file else None
        has_display_data = bool(rp) and _smet_report_html_has_position_groups(rp)
        has_estimate = tid in estimate_ids
        market_partial_exists = _price_output_path_for_tender(tid).is_file()
        merge_html_exists = (merge_root / tid / "index.html").is_file()
        svodka_exists = (REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx").is_file()
        saved_market_done, saved_market_total = _market_progress_for_tender(tid)
        live_market_done, live_market_total = live_market_progress.get(tid, (0, 0))
        if live_market_total > 0:
            market_done, market_total = live_market_done, live_market_total
        else:
            market_done, market_total = saved_market_done, saved_market_total
        market_left = max(0, market_total - market_done)
        market_pct = int(min(100, max(0, round(100.0 * market_done / market_total)))) if market_total > 0 else 0
        stage_raw = (tmeta.get("stage") or "").strip()
        stage_open = stage_raw == STAGE_SUBMISSION
        stage_display = stage_raw if stage_raw else "—"
        items.append(
            {
                "tender_id": tid,
                "display_title": tmeta.get("title") or f"Тендер {tid}",
                "region": tmeta.get("region") or "Без региона",
                "eis_url": eis_notice_url(tid, tmeta.get("url")),
                "has_report": has_report,
                "has_display_data": has_display_data,
                "has_estimate": has_estimate,
                "has_merge_report": merge_html_exists or svodka_exists or market_partial_exists or has_estimate,
                "has_svodka": svodka_exists,
                "has_market_partial": market_partial_exists,
                "report_file": report_file,
                "stage_open": stage_open,
                "stage_display": stage_display,
                "estimate_rows": None,
                "market_progress_done": market_done,
                "market_progress_total": market_total,
                "market_progress_left": market_left,
                "market_progress_percent": market_pct,
                "publish_date": (tmeta.get("publish_date") or "").strip(),
            }
        )
    items.sort(key=lambda x: (x["region"], x["display_title"], x["tender_id"]))
    n_reports = sum(1 for x in items if x["has_report"])
    n_with_data = sum(1 for x in items if x["has_display_data"])
    return items, len(items), n_reports, n_with_data

def _publish_date_sort_key(raw: str, *, newest_first: bool) -> tuple[int, float]:
    """
    Ключ сортировки для даты публикации:
    - сначала валидные даты, потом пустые/неразобранные;
    - поддержка ISO и привычного формата dd.mm.yyyy (с временем или без).
    """
    txt = (raw or "").strip()
    if not txt:
        return 1, 0.0
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(txt, fmt)
            ts = dt.timestamp()
            return 0, (-ts if newest_first else ts)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        ts = dt.timestamp()
        return 0, (-ts if newest_first else ts)
    except ValueError:
        return 1, 0.0


TENDERS_STATUS_LABELS = {
    "download_documents": "Нужны документы",
    "extract_estimate": "Нужна смета",
    "find_market_prices": "Нужны цены",
    "build_comparison": "Нужно сравнение",
    "review": "Есть сравнение",
}

TENDERS_STATUS_DETAILS = {
    "download_documents": "Документы еще не скачаны.",
    "extract_estimate": "Документы есть, смета еще не извлечена.",
    "find_market_prices": "Есть файл сметы. Нужны подтверждённые цены.",
    "build_comparison": "Есть результаты поиска. Соберите таблицу для проверки.",
    "review": "Проверьте источники, полноту сметы и условия участия.",
}

TENDERS_STATUS_CLASS = {
    "download_documents": "status-attention",
    "extract_estimate": "status-attention",
    "find_market_prices": "status-work",
    "build_comparison": "status-work",
    "review": "status-work",
}

TENDERS_STATUS_ORDER = {
    "download_documents": 10,
    "extract_estimate": 20,
    "find_market_prices": 30,
    "build_comparison": 40,
    "review": 90,
}

TENDERS_MAIN_ACTION_LABELS = {
    "download_documents": "Скачать документы",
    "extract_estimate": "Извлечь смету",
    "find_market_prices": "Найти цены",
    "build_comparison": "Собрать сравнение",
    "review": "Открыть результат",
}

TENDERS_RUN_TITLES = {
    "download_documents": "Скачиваем документы",
    "extract_estimate": "Извлекаем смету",
    "find_market_prices": "Ищем цены",
    "build_comparison": "Собираем сравнение",
    "review": "Открываем результат",
}

TENDERS_RUN_DETAILS = {
    "download_documents": "Загружаем комплект из ЕИС и извлекаем смету.",
    "extract_estimate": "Разбираем сохранённые документы. Прежний отчёт сохранится при ошибке.",
    "find_market_prices": "Система найдет рыночные цены и соберет сравнение.",
    "build_comparison": "Система обновит итоговую таблицу и страницу результата.",
    "review": "Сравнение уже готово.",
}


def _tender_object_name(raw_title: str, tender_id: str) -> str:
    title = re.sub(r"\s+", " ", str(raw_title or "")).strip()
    if not title:
        return ""
    remainder = title.replace(str(tender_id or ""), "")
    remainder = re.sub(r"[№#\s.:;,_—–-]+", "", remainder).casefold()
    if remainder in {"", "тендер", "закупка", "извещение", "документы"}:
        return ""
    return title


def _tender_law_and_method(tender_id: str, url: str, law: str, method: str) -> tuple[str, str]:
    tid = str(tender_id or "").strip()
    href = str(url or "").casefold()
    law_text = str(law or "").strip()
    method_text = str(method or "").strip()
    if not law_text:
        law_text = "223-ФЗ" if len(tid) == 11 or "notice223" in href or "/223/" in href else "44-ФЗ"
    if not method_text and law_text == "44-ФЗ":
        route_methods = {
            "/ea20/": "Электронный аукцион",
            "/ok20/": "Открытый конкурс",
            "/zk20/": "Запрос котировок",
        }
        method_text = next((label for marker, label in route_methods.items() if marker in href), "")
    return law_text, method_text


def _tenders_items() -> tuple[list[dict], dict[str, int]]:
    payload = build_workflow_payload(include_storage=False)
    meta_by_id = load_tender_metadata()
    items = list(payload.get("tenders") or [])
    for item in items:
        tid = str(item.get("tender_id") or "").strip()
        action = str(item.get("next_action") or "").strip() or "download_documents"
        meta_row = meta_by_id.get(tid, {}) or {}
        raw_title = str(meta_row.get("title") or item.get("title") or "").strip()
        object_name = _tender_object_name(raw_title, tid)
        title = object_name or f"Закупка № {tid}"
        price_rub = _float_or_none(item.get("price_rub") if item.get("price_rub") is not None else meta_row.get("price_rub"))
        eis_url = eis_notice_url(tid, meta_row.get("url"))
        law, purchase_method = _tender_law_and_method(
            tid,
            eis_url,
            str(meta_row.get("law") or ""),
            str(meta_row.get("purchase_method") or ""),
        )
        eis_stage = str(item.get("stage") or meta_row.get("stage") or "").strip()
        if eis_stage.casefold() in {"закупки", "этап", "этап закупки", "статус", "статус закупки"}:
            eis_stage = ""
        item["title"] = title
        item["object_name"] = object_name
        item["customer_name"] = str(meta_row.get("customer_name") or "").strip()
        item["updated_date"] = str(meta_row.get("updated_date") or "").strip()
        item["law"] = law
        item["purchase_method"] = purchase_method
        item["law_method_label"] = " · ".join(x for x in (law, purchase_method) if x)
        item["eis_stage"] = eis_stage
        item["status_label"] = TENDERS_STATUS_LABELS.get(action, item.get("next_action_label") or "Следующий шаг")
        item["status_detail"] = TENDERS_STATUS_DETAILS.get(action, "")
        item["status_class"] = TENDERS_STATUS_CLASS.get(action, "status-work")
        item["sort_weight"] = TENDERS_STATUS_ORDER.get(action, 50)
        item["price_fmt"] = _fmt_money(price_rub) if price_rub else "не указана"
        item["price_value"] = float(price_rub or 0)
        item["eis_url"] = eis_url
        item["result_url"] = f"/tenders/{tid}"
        item["main_button_label"] = TENDERS_MAIN_ACTION_LABELS.get(action, item.get("next_action_label") or "Продолжить")
        item["main_run_title"] = TENDERS_RUN_TITLES.get(action, "Продолжаем закупку")
        item["main_run_detail"] = TENDERS_RUN_DETAILS.get(action, "Система выполнит следующий недостающий шаг.")
        if item.get("document_download_blocked"):
            item["status_label"] = "Загрузка не завершена"
            item["status_detail"] = "Повторите скачивание комплекта из ЕИС. Прежний отчёт сохранён."
            item["main_button_label"] = "Повторить скачивание"
        elif item.get("document_parse_blocked") and action == "extract_estimate":
            item["status_label"] = "Разбор не завершён"
            item["status_detail"] = "Повторите разбор сохранённых документов. Подробности — в карточке."
            item["main_button_label"] = "Повторить разбор"
        item["can_export_crm"] = bool(item.get("has_estimate"))
    items.sort(
        key=lambda x: (
            int(x.get("sort_weight") or 50),
            _publish_date_sort_key(str(x.get("publish_date") or ""), newest_first=True),
            str(x.get("title") or ""),
            str(x.get("tender_id") or ""),
        )
    )
    counts = {str(k): int(v) for k, v in (payload.get("counts") or {}).items()}
    return items, counts


def _tenders_overview(items: list[dict], counts: dict[str, int]) -> dict:
    ready = int(counts.get("review", 0) or 0)
    needs_work = max(0, len(items) - ready)
    return {
        "total": len(items),
        "ready": ready,
        "needs_work": needs_work,
        "needs_prices": int(counts.get("find_market_prices", 0) or 0),
        "needs_docs": int(counts.get("download_documents", 0) or 0),
        "needs_estimate": int(counts.get("extract_estimate", 0) or 0),
        "needs_comparison": int(counts.get("build_comparison", 0) or 0),
    }


@app.route("/dashboard")
def dashboard_redirect():
    return redirect(url_for("estimates_page"))


def _render_tenders_board():
    items, counts = _tenders_items()
    selected_action = (request.args.get("action") or "").strip()
    if selected_action == "needs_work":
        visible_items = [x for x in items if str(x.get("next_action") or "") != "review"]
    elif selected_action and selected_action != "all":
        visible_items = [x for x in items if str(x.get("next_action") or "") == selected_action]
    else:
        selected_action = "all"
        visible_items = items
    filters = [
        {"key": "all", "label": "Все", "count": len(items)},
        {"key": "needs_work", "label": "В работе", "count": max(0, len(items) - int(counts.get("review", 0) or 0))},
        {"key": "download_documents", "label": "Документы", "count": counts.get("download_documents", 0)},
        {"key": "extract_estimate", "label": "Сметы", "count": counts.get("extract_estimate", 0)},
        {"key": "find_market_prices", "label": "Цены", "count": counts.get("find_market_prices", 0)},
        {"key": "build_comparison", "label": "Сравнения", "count": counts.get("build_comparison", 0)},
        {"key": "review", "label": "Есть сравнение", "count": counts.get("review", 0)},
    ]

    def _filter_counts(key: str, empty_label: str = "") -> list[dict]:
        values: dict[str, int] = {}
        for row in visible_items:
            raw = str(row.get(key) or "").strip()
            value = raw or "__empty__"
            values[value] = values.get(value, 0) + 1
        result = []
        for value, count in sorted(values.items(), key=lambda pair: (pair[0] == "__empty__", pair[0].casefold())):
            result.append({
                "value": value,
                "label": empty_label if value == "__empty__" else value,
                "count": count,
            })
        return result

    law_filters = _filter_counts("law", "Закон не указан")
    stage_filters = _filter_counts("eis_stage", "Статус не указан")
    method_filters = _filter_counts("purchase_method", "Способ не указан")
    region_filters = _filter_counts("region", "Регион не указан")
    return render_template(
        "tenders.html",
        items=visible_items,
        filters=filters,
        selected_action=selected_action,
        overview=_tenders_overview(items, counts),
        law_filters=law_filters,
        stage_filters=stage_filters,
        method_filters=method_filters,
        region_filters=region_filters,
    )


SIMPLE_INDEX_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  <title>Тендеры</title>
  <style>
    :root { color-scheme: light; }
    body { margin:0; font-family: Segoe UI, Arial, sans-serif; background:linear-gradient(180deg,#ffffff 0,#f4f7fb 100%); color:#172235; }
    .wrap { max-width: 1180px; margin: 0 auto; padding: 24px; }
    .top { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom: 18px; }
    .card { background:#ffffff; border:1px solid #d9e3ef; border-radius:16px; padding:18px; box-shadow: 0 10px 30px rgba(28,49,84,.08); }
    .muted { color:#62748b; }
    .stats { display:flex; flex-wrap:wrap; gap:12px; }
    .stat { min-width:140px; }
    .stat b { display:block; font-size:22px; margin-top:4px; }
    .group { margin-top: 18px; }
    .group h2 { margin:0 0 10px; font-size:18px; }
    .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap:14px; }
    .tender h3 { margin:0 0 10px; font-size:16px; line-height:1.35; }
    .meta { display:grid; gap:6px; margin-bottom:12px; font-size:14px; }
    .progress { margin: 12px 0; }
    .progress-row { display:flex; justify-content:space-between; gap:8px; font-size:13px; margin-bottom:6px; }
    .track { width:100%; height:10px; background:#edf3fa; border-radius:999px; overflow:hidden; border:1px solid #d6e0ee; }
    .fill { height:100%; background:linear-gradient(90deg, #4f8cff, #63d1ff); }
    .tags { display:flex; flex-wrap:wrap; gap:8px; margin:10px 0 14px; }
    .tag { font-size:12px; padding:5px 9px; border-radius:999px; background:#f4f8fd; border:1px solid #cfd9e8; color:#35506f; }
    .tag.ok { background:#e9f8ef; border-color:#bfe5cc; color:#257347; }
    .tag.warn { background:#fff8e8; border-color:#f0deb1; color:#a06b18; }
    .tag.bad { background:#fff1f1; border-color:#f0c5c5; color:#b04e4e; }
    .actions { display:flex; flex-wrap:wrap; gap:8px; }
    .btn { border:0; border-radius:10px; padding:10px 14px; cursor:pointer; font-size:14px; background:linear-gradient(180deg,#2e80e8,#1f72dc); color:#fff; }
    .btn.secondary { background:#f4f8fd; color:#35506f; border:1px solid #cfd9e8; }
    .btn[disabled] { opacity:.55; cursor:not-allowed; }
    a.btn { text-decoration:none; display:inline-block; }
    .empty { padding:18px; text-align:center; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <h1 style="margin:0 0 8px;">Тендеры</h1>
        <div class="muted">Список закупок, прогресс по поиску цен и быстрые действия.</div>
      </div>
      <div class="card stats">
        <div class="stat"><span class="muted">Всего тендеров</span><b>{{ tender_count }}</b></div>
        <div class="stat"><span class="muted">В показе</span><b>{{ visible_count }}</b></div>
        <div class="stat"><span class="muted">Карточек</span><b>{{ display_report_count }}/{{ report_count }}</b></div>
      </div>
    </div>

    {% if grouped %}
      {% for region, items in grouped %}
      <section class="group">
        <h2>{{ region }}</h2>
        <div class="grid">
          {% for t in items %}
          <article class="card tender">
            <h3>{{ t.display_title }}</h3>
            <div class="meta">
              <div><span class="muted">Тендер:</span> <code>{{ t.tender_id }}</code></div>
              <div><span class="muted">Публикация:</span> {{ t.publish_date or "не указана" }}</div>
              <div><span class="muted">Этап:</span> {{ t.stage_display }}</div>
              <div><span class="muted">Смета:</span> {% if t.has_estimate %}{{ t.estimate_rows }} строк{% else %}ещё не собрана{% endif %}</div>
            </div>

            {% if t.market_progress_total > 0 %}
            <div class="progress">
              <div class="progress-row">
                <span>Поиск цен</span>
                <span>{{ t.market_progress_done }}/{{ t.market_progress_total }}</span>
              </div>
              <div class="track"><div class="fill" style="width: {{ t.market_progress_percent }}%;"></div></div>
            </div>
            {% endif %}

            <div class="tags">
              {% if t.has_merge_report %}
              <span class="tag ok">Карточка готова</span>
              {% elif t.has_market_partial %}
              <span class="tag warn">Есть частичные цены</span>
              {% elif t.has_estimate %}
              <span class="tag ok">Смета готова</span>
              {% else %}
              <span class="tag bad">Нет сметы</span>
              {% endif %}
              <span class="tag">{{ t.stage_display }}</span>
            </div>

            <div class="actions">
              {% if t.has_merge_report %}
              <a class="btn" href="/merge-report/{{ t.tender_id }}/">Открыть карточку</a>
              {% endif %}
              {% if t.has_estimate %}
              <button class="btn secondary" type="button" onclick="runAction('/api/generate-merge-site-one', '{{ t.tender_id }}', 'Запускаю поиск цен…')">Запустить поиск цен</button>
              <button class="btn secondary" type="button" onclick="runAction('/api/generate-merge-site-one-rerun-market', '{{ t.tender_id }}', 'Перезапускаю поиск…')">Перезапустить</button>
              <button class="btn secondary" type="button" onclick="runAction('/api/reports/rebuild', '{{ t.tender_id }}', 'Пересобираю карточку…')">Пересобрать карточку</button>
              {% else %}
              <button class="btn secondary" type="button" disabled>Сначала нужна смета</button>
              {% endif %}
              <a class="btn secondary" href="{{ t.eis_url }}" target="_blank" rel="noopener noreferrer">ЕИС</a>
            </div>
          </article>
          {% endfor %}
        </div>
      </section>
      {% endfor %}
    {% else %}
      <div class="card empty">
        <div>Сейчас список пуст.</div>
        <div class="muted" style="margin-top:8px;">Либо ещё нет тендеров, либо включён фильтр, который всё скрывает.</div>
      </div>
    {% endif %}
  </div>

  <script>
    function primeTenderMarketProgress(tenderId, startMessage) {
      const panel = document.getElementById("mergeSitePanel");
      const fill = document.getElementById("mergeBarFill");
      const ptext = document.getElementById("mergePercentText");
      const det = document.getElementById("mergeSiteDetail");
      const logs = document.getElementById("mergeSiteLogs");
      if (panel) panel.hidden = false;
      if (fill) fill.style.width = "3%";
      if (ptext) ptext.textContent = "0% ? ??????? 0 / 1" + (tenderId ? (" ? ??????: " + tenderId) : "");
      if (det) det.textContent = startMessage || "???????? ????? ??? ? ??????? ???????? ??????????";
      if (logs) logs.textContent = (tenderId ? ("????? ?? ??????? " + tenderId) : "????? ???????");
    }

    async function runAction(url, tenderId, startMessage) {
      try {
        const body = tenderId ? { tender_id: tenderId } : {};
        const isMarketRun = String(url || "").includes("generate-merge-site-one");
        if (isMarketRun) {
          primeTenderMarketProgress(tenderId, startMessage);
          if (typeof refreshStatus === "function") refreshStatus();
        } else if (startMessage) {
          alert(startMessage);
        }
        const resp = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || data.message || ("HTTP " + resp.status));
        if (isMarketRun) {
          if (typeof refreshStatus === "function") refreshStatus();
          if (typeof refreshCoverage === "function") refreshCoverage();
          return;
        }
        alert(data.message || "??????? ??????????.");
        location.reload();
      } catch (err) {
        alert("??????: " + (err.message || err));
      }
    }
  </script>
/body>
</html>
"""


def _render_tenders_index(*, embed_mode: bool = False):
    sidebar_items, tender_count, report_count, display_report_count = collect_sidebar_tenders()
    meta_by_id = load_tender_metadata()
    show_all = (request.args.get("all", "") or "").strip().lower() in ("1", "true", "yes", "on")
    sort_mode = "publish_desc"
    region_options = sorted({str(x.get("region") or "Без региона") for x in sidebar_items})
    selected_region = (request.args.get("region", "") or "").strip()
    if selected_region not in region_options:
        selected_region = ""
    only_submission = not show_all  # True = только «Подача заявок» (режим по умолчанию)
    visible_items = [x for x in sidebar_items if (x.get("stage_open") if only_submission else True)]
    if selected_region:
        visible_items = [x for x in visible_items if str(x.get("region") or "Без региона") == selected_region]
    newest_first = sort_mode == "publish_desc"
    visible_items.sort(
        key=lambda x: (
            _publish_date_sort_key(str(x.get("publish_date") or ""), newest_first=newest_first),
            str(x.get("display_title") or ""),
            str(x.get("tender_id") or ""),
        )
    )
    for item in visible_items:
        meta_row = meta_by_id.get(str(item.get("tender_id") or ""), {}) or {}
        price_rub = _float_or_none(meta_row.get("price_rub"))
        item["price_fmt"] = _fmt_money(price_rub) if price_rub else "—"
        item["deadline_date"] = _tender_deadline_text(meta_row) or "не указано"
    visible_count = len(visible_items)
    rebuild_options = [
        {"tender_id": x["tender_id"], "display_title": x["display_title"]} for x in sidebar_items
    ]
    coverage = _compute_reports_coverage()
    return render_template_string(
        INDEX_TEMPLATE,
        items=visible_items,
        rebuild_options=rebuild_options,
        tender_count=tender_count,
        report_count=report_count,
        display_report_count=display_report_count,
        coverage=coverage,
        show_all=show_all,
        sort_mode=sort_mode,
        visible_count=visible_count,
        region_options=region_options,
        selected_region=selected_region,
        embed_mode=embed_mode,
    )


@app.route("/")
def root_index():
    return redirect(url_for("estimates_page"))


@app.route("/tenders")
def index():
    return _render_tenders_board()


@app.route("/tenders/<tender_id>")
def tender_detail_page(tender_id: str):
    tid = str(tender_id or "").strip()
    if not re.fullmatch(r"\d{8,25}", tid):
        abort(404)
    metadata = load_tender_metadata()
    meta = dict(metadata.get(tid) or {})
    estimate_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    if not meta and not estimate_path.is_file():
        abort(404)

    workflow_items, _ = _tenders_items()
    workflow = next((dict(item) for item in workflow_items if str(item.get("tender_id") or "") == tid), {})
    if not workflow:
        eis_url = eis_notice_url(tid, meta.get("url"))
        law, method = _tender_law_and_method(tid, eis_url, str(meta.get("law") or ""), str(meta.get("purchase_method") or ""))
        workflow = {
            "tender_id": tid,
            "law": law,
            "purchase_method": method,
            "law_method_label": " · ".join(value for value in (law, method) if value),
            "eis_url": eis_url,
            "eis_stage": str(meta.get("stage") or "").strip(),
            "status_label": "Проверить данные тендера",
            "status_detail": "Продолжите обработку, чтобы получить смету и проверенные цены.",
        }
    tender = build_tender_detail(tid, meta, workflow)
    active_tab = str(request.args.get("tab") or "overview").strip().casefold()
    tender["active_tab"] = "files" if active_tab == "files" else "overview"
    tender["documents"] = list_tender_source_files(tid)
    response = make_response(render_template("tender_detail.html", tender=tender))
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.get('/tenders/document-jobs.js')
def tender_document_jobs_client():
    return app.send_static_file('document_jobs.js')


@app.get('/api/tenders/<tender_id>/economics-source')
def tender_economics_source(tender_id: str):
    if not re.fullmatch(r'[0-9]{8,25}', tender_id):
        abort(404)
    metadata = dict(load_tender_metadata().get(tender_id) or {})
    if not metadata and not (REPORTS_DIR / f'ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.xlsx').is_file():
        abort(404)
    from autobot.tender_economics_source import build_source
    try:
        payload = build_source(tender_id, metadata, REPORTS_DIR, build_tender_detail)
    except (OSError, ValueError):
        return jsonify({'error': 'source_unavailable'}), 503
    response = jsonify(payload)
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route("/tenders/market-audit")
@app.route("/market-audit")
def market_audit_view():
    """Read-only viewer for immutable market evidence captured during verification."""
    record_value = str(request.args.get("record") or "").strip()
    records_root = (REPO_ROOT / "data" / "market_index" / "audit" / "records").resolve()
    try:
        record_path = (REPO_ROOT / record_value).resolve()
        if not record_path.is_relative_to(records_root) or record_path.suffix.casefold() != ".json":
            abort(404)
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        abort(404)
    if not isinstance(payload, dict):
        abort(404)

    snapshot_value = str(payload.get("snapshot_path") or "").strip()
    snapshot_html = ""
    if snapshot_value:
        blobs_root = (REPO_ROOT / "data" / "market_index" / "audit" / "blobs").resolve()
        try:
            snapshot_path = (REPO_ROOT / snapshot_value).resolve()
            if not snapshot_path.is_relative_to(blobs_root) or snapshot_path.suffix.casefold() != ".gz":
                raise ValueError("invalid audit snapshot path")
            with gzip.open(snapshot_path, "rt", encoding="utf-8", errors="replace") as stream:
                snapshot_html = stream.read(400_000)
        except (OSError, ValueError):
            snapshot_html = ""
    if str(request.args.get("download") or "") == "1" and snapshot_html:
        response = make_response(
            send_file(
                io.BytesIO(snapshot_html.encode("utf-8")),
                mimetype="text/html; charset=utf-8",
                as_attachment=True,
                download_name=f"market-source-{record_path.stem}.html",
                max_age=0,
            )
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    response = make_response(render_template_string(
        """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Аудиторский снимок · AutoBot</title><style>
        body{margin:0;background:#f4f7fb;color:#20334f;font:14px/1.5 Inter,Arial,sans-serif}.page{max-width:1180px;margin:auto;padding:28px}
        header,.card{background:#fff;border:1px solid #dfe7f1;border-radius:14px;padding:20px;box-shadow:0 10px 30px rgba(38,59,88,.06)}
        header{display:flex;justify-content:space-between;gap:20px;align-items:center}h1{margin:4px 0 0;font-size:22px}small{color:#71829a}
        .actions{display:flex;gap:9px}.btn{padding:10px 14px;border-radius:9px;text-decoration:none;color:#fff;background:#1769d2;font-weight:700}.btn.alt{color:#31506f;background:#eef4fb}
        .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:16px 0}.grid div{background:#fff;border:1px solid #dfe7f1;border-radius:10px;padding:13px}.grid small,.grid b{display:block}
        pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.45 ui-monospace,Consolas,monospace;color:#344861}.card{max-height:68vh;overflow:auto}
        @media(max-width:720px){header{align-items:flex-start;flex-direction:column}.grid{grid-template-columns:1fr}.page{padding:14px}}
        </style></head><body><main class="page"><header><div><small>AutoBot · доказательство цены</small><h1>{{ title }}</h1></div><div class="actions">{% if has_snapshot %}<a class="btn alt" href="?record={{ record|urlencode }}&download=1">Скачать HTML</a>{% endif %}<a class="btn" href="{{ url }}" target="_blank" rel="noopener noreferrer">Оригинал ↗</a></div></header>
        <section class="grid"><div><small>Цена</small><b>{{ price }} ₽</b></div><div><small>Зафиксировано</small><b>{{ captured }}</b></div><div><small>SHA-256 снимка</small><b>{{ sha or 'нет HTML' }}</b></div></section>
        <section class="card"><small>Сохранённый HTML-код страницы</small><pre>{{ snapshot if snapshot else 'HTML-снимок отсутствует; сохранены URL, время, цена и метаданные проверки.' }}</pre></section></main></body></html>""",
        title=str(payload.get("title") or "Источник цены"),
        price=payload.get("price") or "—",
        captured=str(payload.get("captured_at") or payload.get("observed_at") or "—"),
        sha=str(payload.get("snapshot_sha256") or ""),
        url=str(payload.get("url") or "#"),
        record=record_value,
        snapshot=snapshot_html,
        has_snapshot=bool(snapshot_html),
    ))
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"
    return response


def _source_file_view_model(tender_id: str, token: str, path: Path) -> dict:
    inventory = list_tender_source_files(tender_id)
    item = next((dict(row) for row in inventory.get("files", []) if row.get("token") == token), None)
    if item is not None:
        return item
    stat = path.stat()
    return {
        "token": token,
        "name": repair_filename(path.name),
        "extension": path.suffix.lstrip(".").upper() or "ФАЙЛ",
        "kind": "other",
        "type_label": "Файл",
        "size_fmt": format_file_size(stat.st_size),
        "updated": datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y %H:%M"),
    }


@app.route("/tenders/<tender_id>/source-files/<token>/download")
def tender_source_file_download(tender_id: str, token: str):
    try:
        path = resolve_tender_source_file(tender_id, token)
    except (ValueError, FileNotFoundError, OSError):
        abort(404)
    file_model = _source_file_view_model(tender_id, token, path)
    response = make_response(
        send_file(
            path,
            as_attachment=True,
            download_name=file_model["name"],
            conditional=True,
            max_age=0,
        )
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/tenders/<tender_id>/source-files/<token>/preview")
def tender_source_file_preview(tender_id: str, token: str):
    try:
        path = resolve_tender_source_file(tender_id, token)
    except (ValueError, FileNotFoundError, OSError):
        abort(404)
    file_model = _source_file_view_model(tender_id, token, path)
    extension = path.suffix.casefold()
    if extension == ".pdf" or extension in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        response = make_response(
            send_file(
                path,
                mimetype=mimetypes.guess_type(path.name)[0],
                as_attachment=False,
                download_name=file_model["name"],
                conditional=True,
                max_age=0,
            )
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self' data: blob:"
        return response
    try:
        preview = build_source_file_preview(path)
    except PreviewRejected as exc:
        preview = {'kind': 'unavailable', 'message': str(exc)}
    except Exception as exc:
        preview = {
            "kind": "unavailable",
            "message": f"Не удалось открыть предпросмотр ({type(exc).__name__}). Файл можно скачать без изменений.",
        }
    response = make_response(
        render_template(
            "source_file_preview.html",
            tender_id=str(tender_id),
            file=file_model,
            preview=preview,
            back_url=f"/tenders/{tender_id}?tab=files",
            download_url=f"/tenders/{tender_id}/source-files/{token}/download",
            archive_source_token=token,
        )
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'self'"
    return response


@app.route("/tenders/<tender_id>/source-files/<token>/members/<member_token>/download")
def tender_archive_member_download(tender_id: str, token: str, member_token: str):
    try:
        path = resolve_tender_source_file(tender_id, token)
        member = read_archive_member(path, member_token)
    except PreviewRejected as exc:
        return _archive_preview_limit_response(tender_id, token, path, str(exc))
    except (ValueError, FileNotFoundError, OSError):
        abort(404)
    response = make_response(
        send_file(
            io.BytesIO(member["data"]),
            mimetype=mimetypes.guess_type(member["name"])[0],
            as_attachment=True,
            download_name=member["name"],
            conditional=True,
            max_age=0,
        )
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.route("/tenders/<tender_id>/source-files/<token>/members/<member_token>/preview")
def tender_archive_member_preview(tender_id: str, token: str, member_token: str):
    try:
        path = resolve_tender_source_file(tender_id, token)
        member = read_archive_member(path, member_token)
    except PreviewRejected as exc:
        return _archive_preview_limit_response(tender_id, token, path, str(exc))
    except (ValueError, FileNotFoundError, OSError):
        abort(404)

    extension = Path(member["name"]).suffix.casefold()
    download_url = f"/tenders/{tender_id}/source-files/{token}/members/{member_token}/download"
    if extension == ".pdf" or extension in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        response = make_response(
            send_file(
                io.BytesIO(member["data"]),
                mimetype=mimetypes.guess_type(member["name"])[0],
                as_attachment=False,
                download_name=member["name"],
                conditional=True,
                max_age=0,
            )
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self' data: blob:"
        return response

    try:
        preview = build_source_bytes_preview(member["data"], member["name"], member["chain"])
    except PreviewRejected as exc:
        preview = {'kind': 'unavailable', 'message': str(exc)}
    except Exception as exc:
        preview = {
            "kind": "unavailable",
            "message": f"Не удалось открыть предпросмотр ({type(exc).__name__}). Файл можно скачать без изменений.",
        }
    response = make_response(
        render_template(
            "source_file_preview.html",
            tender_id=str(tender_id),
            file=member,
            preview=preview,
            back_url=f"/tenders/{tender_id}/source-files/{token}/preview",
            download_url=download_url,
            archive_source_token=token,
        )
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'self'"
    return response


def _archive_preview_limit_response(tender_id, token, path, message):
    file_model = {'name': repair_filename(path.name), 'token': token, 'kind': 'archive',
                  'extension': path.suffix.lstrip('.').upper(), 'type_label': 'Исходный архив',
                  'size_fmt': format_file_size(path.stat().st_size), 'updated': '—'}
    response = make_response(render_template('source_file_preview.html', tender_id=tender_id,
        file=file_model, preview={'kind': 'unavailable', 'message': message},
        back_url=f'/tenders/{tender_id}?tab=files',
        download_url=f'/tenders/{tender_id}/source-files/{token}/download', archive_source_token=token), 422)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'self'"
    return response


@app.route("/tenders/old")
def tenders_old():
    query = request.query_string.decode("utf-8", errors="ignore").strip()
    iframe_src = "/tenders/content"
    if query:
        iframe_src = f"{iframe_src}?{query}"
    return render_template_string(TENDERS_SHELL_TEMPLATE, iframe_src=iframe_src)


@app.route("/tenders/content")
def tenders_content():
    return _render_tenders_index(embed_mode=True)


@app.route("/favicon.svg")
def favicon_svg():
    resp = make_response(FAVICON_SVG)
    resp.headers["Content-Type"] = "image/svg+xml"
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "autobot"})


USER_ESTIMATES_DIR = REPO_ROOT / "data" / "user_estimates"
USER_ESTIMATES_INDEX = USER_ESTIMATES_DIR / "index.json"
ESTIMATE_UPLOAD_JOBS_DIR = USER_ESTIMATES_DIR / ".upload_jobs"


def _estimate_upload_allowed(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in (".xlsx", ".xls", ".xlsm", ".pdf")


def _safe_upload_filename(filename: str) -> str:
    raw = Path(filename or "estimate.xlsx").name
    stem = Path(raw).stem
    suffix = Path(raw).suffix.lower()
    stem = re.sub(r"[^0-9A-Za-zА-Яа-я_. -]+", "_", stem).strip(" ._")[:80] or "estimate"
    if suffix not in (".xlsx", ".xls", ".xlsm", ".pdf"):
        suffix = ".xlsx"
    return f"{stem}{suffix}"


def _uploaded_store_call(operation, *args):
    from autobot.uploaded_estimates import StoreError
    from autobot.uploaded_corrections import CorrectionError
    try:
        return operation(USER_ESTIMATES_DIR, *args)
    except CorrectionError as error:
        abort(make_response(jsonify({'ok': False, 'message': str(error)}), error.status))
    except StoreError:
        abort(503, description="Хранилище смет временно недоступно. Повторите открытие позже.")


def _uploaded_market_call(operation, *args, **kwargs):
    from autobot.uploaded_market import MarketError
    from autobot.uploaded_corrections import CorrectionError
    from autobot.upload_admission import AdmissionError
    from autobot.uploaded_estimates import StoreError
    try:
        return operation(*args, **kwargs)
    except (MarketError, AdmissionError, CorrectionError) as error:
        if request.path.startswith('/api/'):
            abort(make_response(jsonify({'ok':False,'message':str(error)}), error.status))
        abort(error.status, description=str(error))
    except (sqlite3.Error, OSError, TimeoutError, StoreError):
        message = 'Очередь поиска временно недоступна. Повторите запрос позже.'
        if request.path.startswith('/api/'):
            abort(make_response(jsonify({'ok':False,'message':message}),503))
        abort(503, description=message)


def _read_legacy_estimates_index() -> list[dict]:
    if not USER_ESTIMATES_INDEX.is_file():
        return []
    try:
        data = json.loads(USER_ESTIMATES_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        abort(503, description="Не удалось прочитать список существующих смет.")
    if not isinstance(data, list):
        abort(503, description="Повреждён список существующих смет.")
    return data


def _read_estimates_index() -> list[dict]:
    from autobot import uploaded_estimates
    current = _uploaded_store_call(uploaded_estimates.catalogue)
    ids = {row['id'] for row in current}
    combined = current + [row for row in _read_legacy_estimates_index() if str(row.get('id') or '') not in ids]
    if not (USER_ESTIMATES_DIR / '.corrections.sqlite3').exists():
        return combined
    return [_uploaded_store_call(uploaded_estimates.load_meta, row['id']) or row for row in combined]


def _write_estimates_index(items: list[dict]) -> None:
    from autobot.uploaded_estimates import write_json
    write_json(USER_ESTIMATES_INDEX, items)


def _estimate_meta_path(estimate_id: str) -> Path:
    return USER_ESTIMATES_DIR / estimate_id / "meta.json"


def _estimate_rows_path(estimate_id: str) -> Path:
    return USER_ESTIMATES_DIR / estimate_id / "rows.json"


def _estimate_market_raw_path(estimate_id: str) -> Path:
    return USER_ESTIMATES_DIR / estimate_id / "market_sources.xlsx"


def _estimate_market_merged_path(estimate_id: str) -> Path:
    return USER_ESTIMATES_DIR / estimate_id / "market_compare.xlsx"


def _estimate_market_revision(estimate_id: str) -> str:
    """Fingerprint saved reports without exposing paths or rereading whole workbooks."""
    versions = []
    for path in (_estimate_market_raw_path(estimate_id), _estimate_market_merged_path(estimate_id)):
        try:
            stat = path.stat()
            versions.append([stat.st_mtime_ns, stat.st_size] if path.is_file() else None)
        except OSError:
            versions.append(None)
    return hashlib.sha256(json.dumps(versions).encode('ascii')).hexdigest()[:24]


def _estimate_original_path(estimate_id: str, meta: dict) -> Path | None:
    """An original belongs to this estimate directory, never an arbitrary metadata path."""
    if not re.fullmatch(r'[0-9a-fA-F-]{1,40}', estimate_id or ''):
        return None
    source = str(meta.get('source_path') or '').strip()
    if not source:
        return None
    path = Path(source)
    if not path.is_absolute():
        path = REPO_ROOT / path
    folder = USER_ESTIMATES_DIR / estimate_id
    try:
        if (folder.is_symlink() or path.is_symlink() or not path.is_file()
                or folder.resolve().parent != USER_ESTIMATES_DIR.resolve()
                or path.resolve().parent != folder.resolve() or not _estimate_upload_allowed(path.name)):
            return None
        return path.resolve()
    except (OSError, ValueError):
        return None


def _estimate_dir_path(estimate_id: str) -> Path:
    return USER_ESTIMATES_DIR / estimate_id


def _estimate_market_progress_for_card(estimate_id: str, rows: list[dict] | None = None) -> tuple[int, int]:
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    rows = list(rows or [])
    total = len(rows)
    if total <= 0:
        return 0, 0

    saved_done = 0
    market_path = _estimate_market_raw_path(estimate_id)
    if not market_path.is_file():
        market_path = _estimate_market_merged_path(estimate_id)
    if market_path.is_file():
        try:
            from autobot.market_contract import confirmed_prices

            df = _estimate_market_df_for_rows(market_path, rows)
            saved_done = sum(bool(confirmed_prices(row)) for _, row in df.iterrows())
        except Exception:
            saved_done = 0
    return max(0, min(saved_done, total)), total


def _load_estimate_meta(estimate_id: str) -> dict | None:
    from autobot import uploaded_estimates, uploaded_market
    value = _uploaded_store_call(uploaded_estimates.load_meta, estimate_id)
    return dict(value, **_uploaded_market_call(uploaded_market.settings, estimate_id)) if value else value


def _load_estimate_original_meta(estimate_id: str):
    from autobot.uploaded_estimates import load_original_meta
    return _uploaded_store_call(load_original_meta, estimate_id)


def _load_estimate_rows(estimate_id: str) -> list[dict]:
    from autobot import uploaded_estimates
    return _uploaded_store_call(uploaded_estimates.load_rows, estimate_id)


def _load_estimate_document(estimate_id: str):
    from autobot.uploaded_market import source_lock
    if not re.fullmatch(r'[0-9a-fA-F-]{1,40}', estimate_id or ''):
        return None, []
    with source_lock(estimate_id, USER_ESTIMATES_DIR):
        return _load_estimate_meta(estimate_id), _load_estimate_rows(estimate_id)


def _json_num(v) -> float | None:
    try:
        if v is None or pd.isna(v):
            return None
    except Exception:
        if v is None:
            return None
    try:
        f = float(v)
    except Exception:
        return None
    return f if f == f else None


def _estimate_code_type(value: str) -> tuple[str, str] | None:
    text = str(value or "")
    if re.search(r"(?<![\w\u0400-\u04ff])(?:ФСБЦ|FSBC)\s*[-\d]", text, flags=re.IGNORECASE):
        return "material", "Материал"
    if re.search(r"(?<![\w\u0400-\u04ff])(?:ГЭСН|GESN)\s*[A-ZА-Я]?\s*\d", text, flags=re.IGNORECASE):
        return "work", "Работа"
    return None


def _position_type(name: str, unit: str = "", basis_code: str = "") -> tuple[str, str]:
    code_type = _estimate_code_type(f"{basis_code} {name} {unit}")
    if code_type:
        return code_type
    text = f"{name} {unit}".casefold().replace("ё", "е")
    forced_material_keys = (
        "видеокамер",
        "камера ip",
        "камеры видеонаблюден",
        "trassir",
    )
    forced_work_keys = (
        "погруз",
        "перевозк",
        "автосамосвал",
        "комплекс работ",
        "обращен",
        "строительных отход",
        "строительными отход",
    )
    if any(k in text for k in forced_material_keys):
        return "material", "Материал"
    if any(k in text for k in forced_work_keys):
        return "work", "Работа"
    material_keys = (
        "бетон", "раствор", "смесь", "цемент", "песок", "щебень", "грунт", "краска", "эмаль",
        "плитк", "кирпич", "труба", "кабель", "провод", "арматур", "битум", "мастик", "лист",
        "профил", "доска", "брус", "изоляц", "линолеум", "ламинат", "керамзит",
    )
    product_keys = (
        "насос", "шкаф", "щит", "светильник", "радиатор", "кран", "задвижк", "клапан", "вентил",
        "люк", "двер", "окно", "блок", "прибор", "оборудован", "издели", "унитаз", "раковин",
        "смесител", "тройник", "угольник", "муфт", "фланец",
    )
    service_keys = ("аренда", "перевозка", "доставка", "вывоз", "погруз", "разгруз", "обслуживание", "испытание", "пусконалад")
    work_keys = (
        "устройство", "установка", "монтаж", "демонтаж", "разборка", "снятие", "прокладка", "окраска",
        "ремонт", "очистка", "расчистка", "штукатур", "облицов", "сверление", "засыпка", "разработка",
        "укладка", "изоляция", "испытание",
    )
    if any(k in text for k in service_keys):
        return "work", "Работа"
    if any(k in text for k in work_keys):
        return "work", "Работа"
    if any(k in text for k in material_keys):
        return "material", "Материал"
    if any(k in text for k in product_keys):
        return "material", "Материал"
    return "other", "Другое"


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{float(v):,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _estimate_reconciliation_view(meta: dict) -> dict:
    diagnostics = meta.get("reconciliation") if isinstance(meta, dict) else None
    if not isinstance(diagnostics, dict) or not diagnostics:
        return {"available": False}

    declared_total = _float_or_none(diagnostics.get("declared_total"))
    signed_total = _float_or_none(diagnostics.get("signed_position_total"))
    difference = _float_or_none(diagnostics.get("unallocated_total"))
    adjustment_count = int(_float_or_none(diagnostics.get("excluded_adjustment_count")) or 0)
    missing_total_count = int(_float_or_none(diagnostics.get('missing_total_count')) or 0)
    adjustment_total = _float_or_none(diagnostics.get("excluded_adjustment_total"))
    available = declared_total is not None or signed_total is not None or adjustment_count > 0
    has_difference = difference is not None and abs(difference) > 0.01
    return {
        "available": available,
        "needs_attention": bool(adjustment_count or has_difference or missing_total_count),
        "missing_total_count": missing_total_count,
        "declared_total_fmt": _fmt_money(declared_total),
        "signed_total_fmt": _fmt_money(signed_total),
        "difference_fmt": _fmt_money(difference),
        "has_difference": has_difference,
        "adjustment_count": adjustment_count,
        "adjustment_total_fmt": _fmt_money(adjustment_total),
    }


def _fmt_qty(v: float | None) -> str:
    if v is None:
        return "—"
    s = f"{float(v):,.4f}".replace(",", " ").rstrip("0").rstrip(".")
    return s if s else "0"


def _normalize_section_title(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    parts = text.split(" ")
    if len(parts) >= 4 and len(parts) % 2 == 0:
        half = len(parts) // 2
        left = " ".join(parts[:half]).strip()
        right = " ".join(parts[half:]).strip()
        if left == right:
            return left
    return text


def _summarize_estimate_rows(rows: list[dict]) -> dict:
    total_sum = 0.0
    has_sum = False
    qty_by_unit: dict[str, float] = {}
    prices: list[float] = []
    type_counts: dict[str, int] = {}
    for r in rows:
        tkey = str(r.get("type") or "other")
        type_counts[tkey] = type_counts.get(tkey, 0) + 1
        sm = _json_num(r.get("total"))
        if sm is not None:
            total_sum += sm
            has_sum = True
        qty = _json_num(r.get("qty"))
        unit = str(r.get("unit") or "без ед.").strip() or "без ед."
        if qty is not None:
            qty_by_unit[unit] = qty_by_unit.get(unit, 0.0) + qty
        up = _json_num(r.get("unit_price"))
        if up is not None and up > 0:
            prices.append(up)
    qty_parts = [f"{_fmt_qty(v)} {u}" for u, v in sorted(qty_by_unit.items(), key=lambda x: x[0])]
    return {
        "row_count": len(rows),
        "total_sum": total_sum if has_sum else None,
        "qty_by_unit": qty_by_unit,
        "qty_text": "; ".join(qty_parts) if qty_parts else "—",
        "avg_price": (sum(prices) / len(prices)) if prices else None,
        "type_counts": type_counts,
    }


def _normalize_selected_estimate_types(raw_values: list[str] | tuple[str, ...] | None) -> list[str]:
    allowed = {"work", "service", "product", "material", "other"}
    out: list[str] = []
    for value in raw_values or []:
        for part in str(value or "").split(","):
            key = part.strip()
            if key and key in allowed and key not in out:
                out.append(key)
    return out


def _filter_estimate_rows(rows_all: list[dict], *, q: str = "", selected_types: list[str] | None = None) -> list[dict]:
    rows = list(rows_all)
    if q:
        q_low = str(q).casefold()
        rows = [r for r in rows if q_low in str(r.get("name") or "").casefold()]
    selected_types = _normalize_selected_estimate_types(selected_types)
    if selected_types:
        allowed = set(selected_types)
        rows = [r for r in rows if str(r.get("type") or "") in allowed]
    return rows


def _estimate_row_to_dict(row) -> dict:
    basis_code = str(getattr(row, "basis_code", "") or "").strip()
    type_key, type_label = _position_type(row.name, row.unit, basis_code)
    return {
        "idx": int(row.idx),
        "name": row.name,
        "unit": row.unit,
        "qty": _json_num(row.qty),
        "unit_price": _json_num(row.unit_price),
        "total": _json_num(row.total),
        "item_no": row.item_no,
        "basis_code": basis_code,
        "code": basis_code,
        "sheet": row.sheet,
        "excel_row": row.excel_row,
        "section": row.section,
        "source": row.source,
        "position_id": str(getattr(row, "position_id", "") or ""),
        "type": type_key,
        "type_label": type_label,
    }


def _estimate_rows_to_report_df(rows: list[dict]) -> pd.DataFrame:
    from autobot.uploaded_estimates import report_frame
    return report_frame(rows)


def _merge_uploaded_estimate_market_df(est_df: pd.DataFrame, market_df: pd.DataFrame) -> pd.DataFrame:
    from autobot.market_contract import merge_market_frames
    from autobot.merge_estimate_market import _normalize_market_columns

    return merge_market_frames(est_df, _normalize_market_columns(market_df))


def _estimate_market_sections(estimate_id: str, rows_filtered: list[dict], selected_types: list[str] | None = None) -> list[dict]:
    from autobot.market_contract import offers_for_row, clean
    from autobot.tender_viability import _market_median_for_row

    frame = _estimate_market_df_for_rows(_estimate_market_merged_path(estimate_id), rows_filtered)
    if frame.empty:
        return []
    labels = {"work": "Работы", "service": "Услуги", "product": "Товары/изделия", "material": "Материалы", "other": "Другое"}
    groups = {}
    for src, (_, row) in zip(rows_filtered, frame.iterrows()):
        verified, candidates = [], []
        for item in offers_for_row(row):
            price = _json_num(item.get("price"))
            offer = {"source": clean(item.get("source")) or "Интернет", "title": clean(item.get("title")) or "Источник",
                "price": price, "price_fmt": _fmt_money(price) if price is not None else "—",
                "url": clean(item.get("url")), "snippet": clean(item.get("evidence") or item.get("snippet"))[:320],
                "verification": item["verification"], "reason": clean(item.get("verification_reason"))}
            (verified if item["verification"] == "verified" else candidates).append(offer)
        median = _market_median_for_row(row)
        status = clean(row.get("Ошибка / статус"))
        if not verified:
            status = status or ("Есть кандидаты, цена требует проверки" if candidates else "Нет подтверждённой цены")
        type_key = str(src.get("type") or "other")
        groups.setdefault(type_key, []).append({
            "position_index": src.get("idx"), "name": str(src.get("name") or ""),
            "type": type_key, "type_label": src.get("type_label") or labels[type_key],
            "unit": str(src.get("unit") or ""), "qty_fmt": _fmt_qty(_json_num(src.get("qty"))),
            "estimate_price_fmt": _fmt_money(_json_num(src.get("unit_price"))),
            "estimate_total_fmt": _fmt_money(_json_num(src.get("total"))),
            "market_prices": _fmt_money(median) if median is not None else "—",
            "status": status, "offers": verified[:12], "candidates": candidates[:12],
        })
    order = selected_types or ["work", "service", "product", "material", "other"]
    return [{"key": k, "label": labels.get(k, k), "count": len(groups[k]), "items": groups[k]} for k in order if groups.get(k)]


def _estimate_market_links(estimate_id: str, market_sections: list[dict], *, q: str = "", selected_types: list[str] | None = None) -> list[dict]:
    selected_types = _normalize_selected_estimate_types(selected_types)
    out: list[dict] = []
    for sec in market_sections:
        params: list[tuple[str, str]] = []
        if q:
            params.append(("q", q))
        for t in selected_types:
            params.append(("types", t))
        params.append(("market_type", str(sec.get("key") or "")))
        out.append(
            {
                "key": str(sec.get("key") or ""),
                "label": str(sec.get("label") or ""),
                "count": int(sec.get("count") or 0),
                "href": f"/estimates/{estimate_id}/market-view?{urlencode(params, doseq=True)}",
            }
        )
    return out


def _estimate_market_df_for_rows(path: Path, rows_filtered: list[dict], *, preserve_candidates: bool = False) -> pd.DataFrame:
    from autobot.market_contract import merge_market_frames
    from autobot.merge_estimate_market import _normalize_market_columns

    # Raw evidence is canonical after queued publication. Never consume a
    # separately saved derived workbook while the source workbook is present.
    if path.name == 'market_compare.xlsx' and path.parent.parent.resolve() == USER_ESTIMATES_DIR.resolve():
        raw = path.with_name('market_sources.xlsx')
        if raw.is_file():
            path = raw
        else:
            from autobot import uploaded_market
            if _uploaded_market_call(uploaded_market.latest, path.parent.name) is not None:
                return pd.DataFrame()
    if not path.is_file():
        return pd.DataFrame()
    try:
        market = _normalize_market_columns(pd.read_excel(path))
    except (OSError, ValueError):
        return pd.DataFrame()
    if preserve_candidates:
        # The sources table is an audit of offers, including rejected/unverified ones.
        # Matching to selected estimate rows still happens in _estimate_source_rows.
        return market
    # Include all requested estimate rows in the denominator, even when the
    # persisted market file contains only a successful subset.
    estimate = _estimate_rows_to_report_df(rows_filtered)
    if path.parent.parent.resolve() == USER_ESTIMATES_DIR.resolve():
        region = (_load_estimate_meta(path.parent.name) or {}).get('market_city')
        if region:
            estimate['Регион поиска'] = str(region)
    return merge_market_frames(estimate, market)


def _table_cell_text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        fv = float(value)
        if not math.isfinite(fv):
            return "—"
        if abs(fv - round(fv)) < 1e-9:
            return f"{int(round(fv)):,}".replace(",", " ")
        return f"{fv:,.2f}".replace(",", " ").replace(".", ",")
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return "—"
    if len(text) > 900:
        text = text[:900].rstrip() + "…"
    return text


def _build_table_view_from_df(
    df: pd.DataFrame,
    *,
    preferred_columns: list[str],
    fallback_limit: int = 10,
    max_rows: int = 300,
) -> dict:
    if getattr(df, "empty", True):
        return {"available": False, "columns": [], "rows": [], "truncated": False}
    columns = [c for c in preferred_columns if c in df.columns]
    if not columns:
        columns = [str(c) for c in list(df.columns)[:fallback_limit]]
    rows: list[list[str]] = []
    for _, row in df[columns].head(max_rows).iterrows():
        rows.append([_table_cell_text(row.get(col)) for col in columns])
    return {
        "available": bool(rows),
        "columns": columns,
        "rows": rows,
        "truncated": len(df.index) > len(rows),
    }


def _estimate_table_views(estimate_id: str, rows_filtered: list[dict]) -> dict[str, dict]:
    from autobot.market_analytics import COL_ITEM, COL_NAME, COL_QTY, COL_SUM, COL_UNIT, COL_UNIT_PRICE

    compare_df = _estimate_market_df_for_rows(_estimate_market_merged_path(estimate_id), rows_filtered)
    raw_df = _estimate_market_df_for_rows(_estimate_market_raw_path(estimate_id), rows_filtered)

    compare_view = _build_table_view_from_df(
        compare_df,
        preferred_columns=[
            COL_ITEM,
            "Тип",
            COL_NAME,
            COL_UNIT,
            COL_QTY,
            COL_UNIT_PRICE,
            COL_SUM,
            "Рынок цены за ед. (итог)",
            "Медиана цена за ед. (рынок)",
            "Ошибка / статус",
        ],
    )
    raw_view = _build_table_view_from_df(
        raw_df,
        preferred_columns=[
            COL_ITEM,
            "Тип",
            COL_NAME,
            "Поисковый запрос рынка",
            "Цены за ед. (рынок, руб)",
            "Рыночные источники",
            "Ошибка / статус",
        ],
    )
    return {
        "estimate": {"available": bool(rows_filtered)},
        "compare": compare_view,
        "sources": raw_view,
    }


def _pick_estimate_active_table_view(requested: str, table_views: dict[str, dict]) -> str:
    requested_key = str(requested or "").strip().lower()
    if requested_key in ("estimate", "compare", "sources") and table_views.get(requested_key, {}).get("available"):
        return requested_key
    return "estimate"


def _first_url_from_text(text: object) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    m = re.search(r"https?://[^\s<>'\"]+", raw)
    return m.group(0).strip() if m else ""


def _estimate_compare_rows(rows_filtered: list[dict], compare_df: pd.DataFrame) -> list[dict]:
    from autobot.price_comparison import price_difference
    from autobot.market_contract import confirmed_prices
    from autobot.market_analytics import COL_NAME
    from autobot.merge_estimate_market import _norm_key
    from autobot.tender_viability import _estimate_numeric_for_compare, _market_median_for_row, _rub_col

    from autobot.market_contract import clean, match_market_rows
    matches = match_market_rows(_estimate_rows_to_report_df(rows_filtered), compare_df)
    rc = _rub_col(compare_df) if not compare_df.empty else None
    out: list[dict] = []
    for src, matched in zip(rows_filtered, matches):
        merged = matched or {}
        section = _normalize_section_title(str(src.get("section") or "")) or "Без раздела"
        market_num = None
        est_num = None
        ratio = None
        if merged and rc:
            row_series = pd.Series(merged)
            est_num = _estimate_numeric_for_compare(row_series)
            market_num = _market_median_for_row(row_series, rc)
            if est_num and market_num and market_num > 0:
                ratio = est_num / market_num
        status = clean(merged.get("Ошибка / статус"))
        if status.casefold() in {"nan", "none", "null", "<na>"}:
            status = ""
        if market_num is None and not status:
            status = "Рынок пока не найден"
        first_url = ""
        if merged:
            first_url = _first_url_from_text(clean(merged.get("Ссылки (строго)"))) or _first_url_from_text(clean(merged.get("Источники (ссылки/телефоны)")))
            if not first_url:
                bundle = merged.get("Цена-сайт-телефон (json)")
                if isinstance(bundle, str) and bundle.strip():
                    try:
                        parsed = json.loads(bundle)
                    except Exception:
                        parsed = []
                    if isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict) and _first_url_from_text(clean(item.get("url"))):
                                first_url = _first_url_from_text(clean(item.get("url")))
                                break
            if not first_url:
                for i in range(1, 6):
                    maybe = _first_url_from_text(clean(merged.get(f"Ссылка объявления {i}")))
                    if maybe:
                        first_url = maybe
                        break
        site = urlparse(first_url).netloc.replace("www.", "") if first_url else ""
        if not site:
            site = clean(merged.get("Источник 1")) or clean(merged.get("Источник"))
            if site.casefold() in {"nan", "none", "null", "<na>"}:
                site = ""
        if ratio is None:
            compare_label = "Нет данных"
            compare_class = "muted"
        elif ratio < 0.92:
            compare_label = "Ниже рынка"
            compare_class = "bad"
        elif ratio > 1.08:
            compare_label = "Выше рынка"
            compare_class = "good"
        else:
            compare_label = "Около рынка"
            compare_class = "warn"
        out.append(
            {
                "section": section,
                "type_label": str(src.get("type_label") or ""),
                "name": str(src.get("name") or ""),
                "estimate_price": _fmt_money(est_num) if est_num else _fmt_money(_json_num(src.get("unit_price"))),
                "market_price": _fmt_money(market_num) if market_num else "—",
                "unit": str(src.get("unit") or "—"),
                "calculation_source_count": len(confirmed_prices(pd.Series(merged))) if market_num is not None else 0,
                **price_difference(est_num if est_num is not None else _json_num(src.get("unit_price")), market_num),
                "site": site or "—",
                "site_url": first_url,
                "status": status or compare_label,
                "compare_label": compare_label,
                "compare_class": compare_class,
                "ratio": ratio,
                "has_market": market_num is not None,
            }
        )
    return out


def _estimate_source_rows(rows_filtered: list[dict], raw_df: pd.DataFrame) -> list[dict]:
    from autobot.market_contract import clean, match_market_rows
    matches = match_market_rows(_estimate_rows_to_report_df(rows_filtered), raw_df)
    out: list[dict] = []
    for src, matched in zip(rows_filtered, matches):
        merged = matched or {}
        text = clean(merged.get("Рыночные источники"))
        query = clean(merged.get("Поисковый запрос рынка"))
        first_url = _first_url_from_text(text) or _first_url_from_text(clean(merged.get("Ссылки (строго)")))
        status = clean(merged.get("Ошибка / статус")) or ("Источник для проверки" if first_url else "Нет источников")
        site = urlparse(first_url).netloc.replace("www.", "") if first_url else ""
        out.append(
            {
                "section": _normalize_section_title(str(src.get("section") or "")) or "Без раздела",
                "name": str(src.get("name") or ""),
                "market_price": clean(merged.get("Цены за ед. (рынок, руб)")) or clean(merged.get("Рынок цены за ед. (итог)")) or "—",
                "site": site or "—",
                "site_url": first_url,
                "status": status,
                "query": query or "—",
            }
        )
    return out


def _estimate_viability_overview(compare_df: pd.DataFrame, compare_rows: list[dict], scope_info: dict | None = None) -> dict:
    from autobot.tender_viability import build_viability_section_html, compute_viability_stats

    scope_info = scope_info or {}
    if getattr(compare_df, "empty", True):
        if scope_info.get("has_notice"):
            return {
                "available": False,
                "title": "РЫНОК НЕ СОБРАН ДЛЯ ЭТОГО ТИПА",
                "subtitle": str(scope_info.get("text") or ""),
                "tone": "warn",
                "facts": [],
                "groups": [],
                "html": "",
            }
        return {
            "available": False,
            "title": "Недостаточно данных",
            "subtitle": "Сначала нужен поиск рынка хотя бы по части позиций.",
            "tone": "warn",
            "facts": [],
            "groups": [],
            "html": "",
        }
    stats = compute_viability_stats(compare_df)
    from autobot.tender_viability import _verdict_label
    title, verdict_class = _verdict_label(stats)
    tone = "bad" if verdict_class == "viability--tight" else "warn"

    types_seen = []
    for row in compare_rows:
        label = str(row.get("type_label") or "").strip()
        if row.get("has_market") and label and label not in types_seen:
            types_seen.append(label)
    comparable_types = ", ".join(types_seen) if types_seen else "пока без уверенного покрытия"
    facts = [
        {"label": "Проверено по сумме", "value": (f"{stats.coverage_cost_percent:.1f}%".replace(".", ",") if stats.coverage_cost_percent is not None else "Неизвестно")},
        {"label": "Без подтверждённой цены", "value": _fmt_money(stats.uncovered_estimate_total)},
        {"label": "Позиций с ценой", "value": f"{stats.comparable} из {stats.rows_considered}"},
        {"label": "Разница по проверенной части", "value": (_fmt_money(stats.comparable_gap_total) if stats.comparable_gap_total is not None else "—")},
    ]
    group_map: dict[str, dict] = {}
    for row in compare_rows:
        section = str(row.get("section") or "Без раздела")
        g = group_map.setdefault(section, {"title": section, "good": 0, "warn": 0, "bad": 0, "none": 0})
        cls = str(row.get("compare_class") or "muted")
        if cls == "good":
            g["good"] += 1
        elif cls == "bad":
            g["bad"] += 1
        elif cls == "warn":
            g["warn"] += 1
        else:
            g["none"] += 1
    groups = list(group_map.values())[:18]
    subtitle = str(scope_info.get("text") or f"Сейчас покрыты: {comparable_types}.")
    subtitle += f" Расчёт по распознанным позициям на {_fmt_money(stats.total_estimate)}. Разница со сметой ещё не прибыль: нужны все затраты и цена предложения."
    if stats.rows_without_amount:
        subtitle += f" Неизвестна сумма {stats.rows_without_amount} позиций; полнота по стоимости пока не определена."
    return {
        "available": True,
        "title": title,
        "subtitle": subtitle,
        "tone": tone,
        "facts": facts,
        "groups": groups,
        "html": build_viability_section_html(stats, "estimate"),
    }


def _estimate_market_scope_info(meta: dict | None, current_selected_types: list[str] | None) -> dict:
    labels_full = {"work": "Работы", "service": "Услуги", "product": "Товары/изделия", "material": "Материалы", "other": "Другое"}
    analyzed_types = _normalize_selected_estimate_types((meta or {}).get("market_selected_types") or [])
    current_types = _normalize_selected_estimate_types(current_selected_types)
    analyzed_set = set(analyzed_types)
    current_set = set(current_types)
    if not analyzed_types:
        return {
            "has_notice": False,
            "tone": "warn",
            "title": "",
            "text": "",
        }
    if not current_types:
        current_set = analyzed_set
    only_analyzed = current_set.issubset(analyzed_set)
    if only_analyzed:
        return {
            "has_notice": False,
            "tone": "warn",
            "title": "",
            "text": "",
        }
    analyzed_labels = ", ".join(labels_full.get(x, x) for x in analyzed_types)
    missing_labels = ", ".join(labels_full.get(x, x) for x in current_types if x not in analyzed_set)
    return {
        "has_notice": True,
        "tone": "warn",
        "title": "Рынок собран не для всех выбранных типов",
        "text": f"Сейчас в файле рынка есть только: {analyzed_labels}. Для этих типов ещё не собраны цены: {missing_labels}. Поэтому вывод ниже не может честно посчитать их как проанализированные.",
    }


def _simple_compare_export_df(compare_rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Раздел": row["section"],
                "Наименование": row["name"],
                "Цена сметы": row["estimate_price"],
                "Цена рынка": row["market_price"],
                "Сайт": row["site"],
                "Статус": row["status"],
            }
            for row in compare_rows
        ]
    )


def _simple_sources_export_df(source_rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Раздел": row["section"],
                "Наименование": row["name"],
                "Цена рынка": row["market_price"],
                "Сайт": row["site"],
                "Статус": row["status"],
            }
            for row in source_rows
        ]
    )


def _estimate_upload_log_append(job: dict, line: str) -> None:
    logs = list(job.get("log_lines") or [])
    stamp = datetime.now().strftime("%H:%M:%S")
    logs.append(f"{stamp} · {line}")
    job["log_lines"] = logs[-20:]


def _estimate_upload_job_path(job_id: str) -> Path | None:
    clean_job_id = str(job_id or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{16,40}", clean_job_id):
        return None
    return ESTIMATE_UPLOAD_JOBS_DIR / f"{clean_job_id}.json"


def _estimate_upload_persist_locked(job: dict, *, strict: bool = False) -> None:
    """Persist upload state so a page or container reload can recover it."""
    target = _estimate_upload_job_path(str(job.get("job_id") or ""))
    if target is None:
        return
    try:
        from autobot.uploaded_estimates import write_json
        write_json(target, job)
    except OSError:
        # Progress persistence must never abort OCR itself. The in-memory
        # status remains available until the process exits.
        if strict:
            raise
        return


def _estimate_upload_load_locked(job_id: str) -> dict | None:
    target = _estimate_upload_job_path(job_id)
    if target is None or not target.is_file():
        return None
    try:
        if target.stat().st_size > 64 * 1024:
            return None
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or str(payload.get("job_id") or "") != str(job_id):
        return None
    estimate_upload_jobs[job_id] = payload
    return payload





def _estimate_upload_set(job_id: str, **updates) -> None:
    with estimate_upload_lock:
        job = estimate_upload_jobs.get(job_id)
        if not job:
            return
        for key, value in updates.items():
            job[key] = value
        job["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _estimate_upload_persist_locked(job)








def _research_queries_from_text(raw: str, *, limit: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in str(raw or "").splitlines():
        q = re.sub(r"\s+", " ", line).strip(" \t,;")
        if len(q) < 2:
            continue
        key = q.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= limit:
            break
    return out


def _crm_login(session, base: str) -> None:
    creds = _crm_credentials()
    if not creds:
        raise RuntimeError("В .env auto_bot нужно задать PMBI_CRM_LOGIN и PMBI_CRM_PASSWORD.")
    response = session.post(
        f"{base}/api/auth/login",
        json={"login": creds[0], "password": creds[1]},
        timeout=15,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"CRM не приняла логин: HTTP {response.status_code}.")


def _crm_projects(session, base: str) -> list[dict]:
    response = session.get(f"{base}/api/projects", timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"CRM не вернула список объектов: HTTP {response.status_code}.")
    rows = response.json().get("projects") or []
    projects: list[dict] = []
    for row in rows:
        try:
            project_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if project_id <= 0:
            continue
        projects.append(
            {
                "id": project_id,
                "title": str(row.get("title") or f"Объект #{project_id}").strip(),
                "contract_no": str(row.get("contract_no") or row.get("contractNo") or "").strip(),
                "address": str(row.get("address") or "").strip(),
            }
        )
    return projects


def crm_projects_for_picker() -> list[dict]:
    import requests

    base = _crm_base_url()
    with requests.Session() as session:
        _crm_login(session, base)
        return _crm_projects(session, base)


def _requested_crm_project_id(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        project_id = int(value)
    except (TypeError, ValueError):
        raise RuntimeError("Некорректный объект CRM.")
    if project_id <= 0:
        raise RuntimeError("Некорректный объект CRM.")
    return project_id


def _import_crm_estimate(
    session,
    base: str,
    project_id: int,
    items: list[dict],
    source: dict,
    *,
    source_label: str,
    source_reference: str = "",
) -> dict:
    if not items:
        raise RuntimeError("В смете нет подходящих строк для добавления в объект.")
    response = session.post(
        f"{base}/api/projects/{project_id}/estimate-import",
        json={
            "items": items,
            "source": source,
            "sourceLabel": source_label,
            "sourceReference": source_reference,
            "replace_source": True,
        },
        timeout=90,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"CRM не импортировала смету: HTTP {response.status_code} {response.text[:300]}")
    return response.json()


def _research_specs_from_text(raw: str, *, limit: int = 5) -> list[dict[str, str]]:
    """Lines use `position | unit`; the unit is required for verified prices."""
    specs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in str(raw or "").splitlines():
        clean = re.sub(r"\s+", " ", line).strip(" \t,;")
        if len(clean) < 2:
            continue
        name, sep, unit = clean.rpartition("|")
        if not sep:
            name, unit = clean, ""
        name = name.strip(" ,;")
        unit = unit.strip(" ,;")[:32]
        if len(name) < 2:
            continue
        key = (name.casefold(), unit.casefold())
        if key in seen:
            continue
        seen.add(key)
        specs.append({"query": name, "unit": unit})
        if len(specs) >= limit:
            break
    return specs


def _estimate_upload_progress_cb(job_id: str):
    def _cb(percent: int, stage: str, detail: str = "") -> None:
        with estimate_upload_lock:
            job = estimate_upload_jobs.get(job_id)
            if not job:
                return
            reported_progress = int(percent)
            job["progress"] = max(int(job.get("progress") or 0), reported_progress)
            job["progress_estimated"] = int(job["progress"]) > reported_progress
            changed = (job.get("stage"), job.get("detail")) != (stage, detail)
            job["stage"] = stage
            job["detail"] = detail
            job["updated_at"] = datetime.now().isoformat(timespec="seconds")
            if changed:
                _estimate_upload_log_append(job, f"{stage}" + (f": {detail}" if detail else ""))
            _estimate_upload_persist_locked(job)
    return _cb


def _estimate_upload_heartbeat(job_id: str, stop_event: threading.Event) -> None:
    """Keep long blocking OCR passes visibly alive between real checkpoints."""
    worker_started = time.monotonic()
    while not stop_event.wait(7):
        with estimate_upload_lock:
            job = estimate_upload_jobs.get(job_id)
            if not job or not job.get("running"):
                return
            current = int(job.get("progress") or 0)
            if 30 <= current < 77:
                job["progress"] = current + 1
                job["progress_estimated"] = True
            job["elapsed_seconds"] = max(0, round(time.monotonic() - worker_started))
            job["updated_at"] = datetime.now().isoformat(timespec="seconds")
            _estimate_upload_persist_locked(job)


def _estimate_upload_cleanup(max_jobs: int = 16) -> None:
    with estimate_upload_lock:
        items = sorted(
            estimate_upload_jobs.items(),
            key=lambda kv: str(kv[1].get("started_at") or ""),
            reverse=True,
        )
        keep = dict(items[:max_jobs])
        keep.update((key, value) for key, value in items if value.get("running"))
        estimate_upload_jobs.clear()
        estimate_upload_jobs.update(keep)


def _estimate_upload_complete(job_id, estimate_id, rows, reconciliation, *, restored=False):
    with estimate_upload_lock:
        job = estimate_upload_jobs.get(job_id)
        if not job:
            return
        bits = [f"Строк: {len(rows)}", "смета сохранена"]
        count = int(_float_or_none(reconciliation.get("excluded_adjustment_count")) or 0)
        total = _float_or_none(reconciliation.get("excluded_adjustment_total"))
        difference = _float_or_none(reconciliation.get("unallocated_total"))
        if count:
            bits.append(f"корректировки: {count} ({_fmt_money(total)})")
        if difference is not None and abs(difference) > 0.01:
            bits.append(f"разница итога: {_fmt_money(difference)}")
        stamp = datetime.now().isoformat(timespec="seconds")
        job.update(running=False, ok=True, progress=100, progress_estimated=False,
                   stage="Готово", error="", detail=" · ".join(bits), estimate_id=estimate_id,
                   ended_at=stamp, updated_at=stamp)
        _estimate_upload_log_append(job, "Готово: " + ("восстановлена сохранённая смета" if restored else job["detail"]))
        _estimate_upload_persist_locked(job)


def _estimate_upload_failed(job_id, error):
    with estimate_upload_lock:
        job = estimate_upload_jobs.get(job_id)
        if job is None:
            return
        stamp = datetime.now().isoformat(timespec="seconds")
        job.update(running=False, ok=False, stage="Ошибка", detail="Не удалось завершить загрузку сметы",
                   error=str(error)[:500], ended_at=stamp, updated_at=stamp, progress_estimated=False)
        _estimate_upload_log_append(job, "Ошибка: " + str(error)[:300])
        _estimate_upload_persist_locked(job)


def _run_estimate_upload_worker(job_id: str, *, estimate_id: str, title_raw: str, original_name: str, src_path: Path) -> None:
    from autobot import uploaded_estimates
    from autobot.atomic_output import output_lock
    from autobot.estimate_parse_worker import snapshot
    job_path = _estimate_upload_job_path(job_id)
    if job_path is None:
        return
    try:
        with output_lock(job_path.with_suffix('.run'), timeout=0):
            try:
                with estimate_upload_lock:
                    job = _estimate_upload_load_locked(job_id) or estimate_upload_jobs.get(job_id)
                    if not job or not job.get('running'):
                        return
                if (not re.fullmatch(r'[0-9a-f]{16,40}', estimate_id)
                        or src_path.is_symlink()
                        or src_path.resolve().parent != (USER_ESTIMATES_DIR / estimate_id).resolve()
                        or not src_path.is_file()):
                    raise RuntimeError("Исходный файл сметы недоступен после перезапуска.")
                if job.get('source_sha256') and snapshot([src_path])[0]['sha256'] != job['source_sha256']:
                    raise RuntimeError("Принятый исходник изменился. Загрузите новую версию отдельным файлом.")
                saved = uploaded_estimates.meta(USER_ESTIMATES_DIR, estimate_id)
                if saved is not None:
                    if snapshot([src_path])[0]['sha256'] != saved['source_sha256']:
                        raise RuntimeError("Исходник сохранённой сметы изменился. Загрузите новую версию отдельным файлом.")
                    positions = uploaded_estimates.rows(USER_ESTIMATES_DIR, estimate_id)
                    if not positions or len(positions) != saved['row_count']:
                        raise RuntimeError("Сохранённая смета неполна; требуется проверка хранилища.")
                    _estimate_upload_complete(job_id, estimate_id, positions, saved.get('reconciliation') or {}, restored=True)
                    return
                with estimate_upload_lock:
                    attempts = int(job.get('attempts') or 0)
                    if attempts >= 3:
                        raise RuntimeError("Три попытки обработки были прерваны. Исходник сохранён; повторите загрузку после проверки сервера.")
                    job['attempts'] = attempts + 1
                    job['error'] = ''
                    if attempts:
                        job.update(stage="Возобновляю обработку", detail="Продолжаю загрузку после перезапуска сервера")
                        _estimate_upload_log_append(job, "Возобновление обработки, попытка " + str(attempts + 1))
                    _estimate_upload_persist_locked(job, strict=True)
                _execute_estimate_upload_worker(job_id, estimate_id=estimate_id, title_raw=title_raw,
                                                original_name=original_name, src_path=src_path)
            except Exception as error:
                _estimate_upload_failed(job_id, error)
    except TimeoutError:
        # Another process owns this job. Only its writer can update progress.
        pass
    finally:
        with estimate_upload_lock:
            estimate_upload_workers.discard(job_id)


def _start_estimate_upload_worker(job_id: str, *, recovering: bool = False) -> bool:
    with estimate_upload_lock:
        if job_id in estimate_upload_workers:
            return False
        job = _estimate_upload_load_locked(job_id) or estimate_upload_jobs.get(job_id)
        if not job or not job.get('running'):
            return False
        source = Path(str(job.get('source_path') or ''))
        if not source.is_absolute():
            source = REPO_ROOT / source
        kwargs = dict(job_id=job_id, estimate_id=str(job.get('target_estimate_id') or job.get('estimate_id') or ''),
                      title_raw=str(job.get('title_raw') or ''), original_name=str(job.get('original_name') or source.name),
                      src_path=source)
        estimate_upload_workers.add(job_id)
    try:
        threading.Thread(target=_run_estimate_upload_worker, kwargs=kwargs, daemon=True).start()
    except Exception as error:
        with estimate_upload_lock:
            estimate_upload_workers.discard(job_id)
        from autobot.atomic_output import output_lock
        try:
            with output_lock(_estimate_upload_job_path(job_id).with_suffix('.run'), timeout=0):
                _estimate_upload_failed(job_id, error)
        except TimeoutError:
            pass
        return False
    return True


def _execute_estimate_upload_worker(job_id: str, *, estimate_id: str, title_raw: str, original_name: str, src_path: Path) -> None:
    heartbeat_stop = threading.Event()
    threading.Thread(
        target=_estimate_upload_heartbeat,
        args=(job_id, heartbeat_stop),
        daemon=True,
    ).start()
    try:
        from types import SimpleNamespace
        from autobot.estimate_parse_worker import run_uploaded_parser, validate_snapshot

        with estimate_upload_lock:
            current_progress = int((estimate_upload_jobs.get(job_id) or {}).get("progress") or 0)
        file_kind = "PDF" if src_path.suffix.lower() == ".pdf" else "Excel"
        _estimate_upload_set(
            job_id,
            running=True,
            progress=max(30, current_progress),
            progress_estimated=False,
            stage="Файл получен",
            detail=f"Запускаю разбор {file_kind}",
        )
        parsed = run_uploaded_parser(src_path, progress_cb=_estimate_upload_progress_cb(job_id))
        source_version = parsed['sources'][0]['sha256']
        rows = [dict(_estimate_row_to_dict(SimpleNamespace(**row)), estimate_version=source_version)
                for row in parsed['rows']]
        summary = _summarize_estimate_rows(rows)
        reconciliation = dict(parsed.get('diagnostics') or {})
        _estimate_upload_set(job_id, progress=97, stage="Сохраняю смету", detail="Записываю карточку и таблицу")
        meta = {
            "id": estimate_id,
            "title": (title_raw or Path(original_name).stem)[:160],
            "original_filename": original_name,
            "created_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "row_count": len(rows),
            "total_sum": summary.get("total_sum"),
            "source_path": str(src_path.relative_to(REPO_ROOT)),
            "source_sha256": source_version,
        }
        if reconciliation:
            meta["reconciliation"] = reconciliation
        validate_snapshot(parsed['sources'])
        from autobot import uploaded_estimates
        uploaded_estimates.publish(USER_ESTIMATES_DIR, meta, rows)
        _estimate_upload_complete(job_id, estimate_id, rows, reconciliation)
    except Exception as e:
        with estimate_upload_lock:
            job = estimate_upload_jobs.get(job_id)
            if job:
                job["running"] = False
                job["ok"] = False
                job["error"] = str(e)[:500]
                job["stage"] = "Ошибка"
                job["detail"] = "Не удалось распознать или сохранить смету"
                job["ended_at"] = datetime.now().isoformat(timespec="seconds")
                job["updated_at"] = job["ended_at"]
                job["progress_estimated"] = False
                _estimate_upload_log_append(job, f"Ошибка: {str(e)[:300]}")
                _estimate_upload_persist_locked(job)
    finally:
        heartbeat_stop.set()
        with estimate_upload_lock:
            estimate_upload_workers.discard(job_id)
        _estimate_upload_cleanup()










RESEARCH_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  <title>Поиск по позиции</title>
  <style>
    :root { color-scheme: light; --bg:#f4f7fb; --panel:#ffffff; --panel2:#f7fafe; --border:#d9e3ef; --muted:#62748b; --text:#172235; --accent:#1f72dc; }
    body { margin:0; font-family: Segoe UI, Arial, sans-serif; background:linear-gradient(180deg,#ffffff 0,#f4f7fb 100%); color:var(--text); }
    .page { max-width:1220px; margin:0 auto; padding:26px 18px 44px; }
    h1 { margin:0 0 8px; font-size:34px; }
    .sub,.muted { color:var(--muted); }
    .tabs { display:flex; flex-wrap:wrap; gap:8px; margin:14px 0 18px; }
    .tab { display:inline-flex; padding:9px 12px; border-radius:999px; color:#35506f; text-decoration:none; background:#f4f8fd; border:1px solid var(--border); font-weight:700; font-size:13px; }
    .tab.is-active { color:#fff; background:linear-gradient(180deg,#2e80e8,#1f72dc); border-color:#2e80e8; }
    .panel,.card { background:linear-gradient(180deg,#ffffff,#f8fbff); border:1px solid var(--border); border-radius:16px; box-shadow:0 18px 45px rgba(28,49,84,.08); }
    .panel { padding:16px; margin-bottom:16px; }
    .grid { display:grid; gap:12px; }
    .form-grid { display:grid; grid-template-columns:minmax(320px,1.4fr) minmax(220px,.8fr); gap:12px; align-items:start; }
    label { display:grid; gap:6px; color:var(--muted); font-size:12px; }
    textarea,input { background:#fff; border:1px solid #cfd9e8; color:var(--text); border-radius:12px; padding:12px; font:inherit; }
    textarea { min-height:180px; resize:vertical; }
    .btn-row { display:flex; flex-wrap:wrap; gap:10px; margin-top:12px; }
    .btn { border:1px solid #2e80e8; background:linear-gradient(180deg,#2e80e8,#1f72dc); color:white; border-radius:10px; padding:10px 14px; font-weight:700; cursor:pointer; text-decoration:none; }
    .btn.secondary { background:#f4f8fd; border-color:#cfd9e8; color:#35506f; }
    .btn[disabled] { opacity:.6; cursor:not-allowed; }
    .results { display:grid; gap:12px; }
    .result-card { padding:14px; background:#fff; border:1px solid #dfe7f1; border-radius:14px; }
    .result-card h3 { margin:0 0 6px; font-size:18px; }
    .meta { color:#62748b; font-size:13px; margin-bottom:10px; }
    .offers { display:grid; gap:10px; }
    .offer { padding:11px 12px; border-radius:12px; background:#f8fbff; border:1px solid #dfe7f1; }
    .offer-top { display:flex; justify-content:space-between; gap:12px; margin-bottom:6px; }
    .offer-source { display:inline-flex; padding:4px 8px; border-radius:999px; background:#edf4fd; border:1px solid #cfd9e8; font-size:12px; color:#35506f; }
    .offer-price { font-weight:800; color:#2e8b57; white-space:nowrap; }
    .offer-price.is-candidate { color:#a06b18; }
    .offer.is-verified { border-color:#b9dfc7; background:#f4fcf7; }
    .offer.is-candidate { border-color:#ecd8aa; background:#fffaf0; }
    .verification { margin-top:8px; padding:7px 9px; border-radius:8px; font-size:12px; line-height:1.4; background:#fff; border:1px solid #dfe7f1; color:#50627a; }
    .verification b { color:#1f6f43; }.offer.is-candidate .verification b { color:#986315; }
    .strategy-note { margin:10px 0 0; padding:10px 12px; border-radius:10px; background:#eef5ff; color:#4d6480; font-size:12px; line-height:1.45; }
    .offer a { color:#1f72dc; font-weight:700; text-decoration:none; }
    .offer-snippet { margin-top:6px; color:#62748b; font-size:13px; }
    .empty { padding:16px; text-align:center; color:#62748b; }
    @media (max-width:760px){ .form-grid{grid-template-columns:1fr} .btn-row{flex-direction:column} .btn{width:100%;box-sizing:border-box} }
  </style>
  <link rel="stylesheet" href="/static/autobot-ui.css?v=20260902-tabs-1" />
</head>
<body class="autobot-page research-page">
  <header class="topbar autobot-section-bar">
    <a class="brand" href="/estimates">
      <span class="brand-mark" aria-hidden="true"><i></i></span>
      <span class="brand-copy"><strong>AutoBot</strong><small>Закупки без рутины</small></span>
    </a>
    <nav class="topnav" aria-label="Разделы AutoBot">
      <a class="topnav-primary" href="/estimates">Сметы</a>
      <a class="topnav-primary" href="/tenders">Тендеры</a>
      <a class="is-active" href="/research">Поиск позиции</a>
    </nav>
  </header>
  <div class="page">
    <header class="research-hero">
      <span class="eyebrow">Быстрая проверка рынка</span>
      <h1>Найти цену по позиции</h1>
      <p class="sub">AutoBot найдёт кандидатов, откроет прямые страницы и отдельно покажет проверенные цены и отклонённые источники.</p>
    </header>

    <section class="panel research-form-panel">
      <div class="form-grid">
        <label>Позиции для поиска
          <textarea id="researchQueries" placeholder="Название | единица&#10;Кабель ВВГнг 3х2,5 | м&#10;Укладка тротуарной плитки | м2">{{ default_query }}</textarea>
        </label>
        <div class="grid">
          <label>Город
            <input id="researchCity" type="text" placeholder="Например: Челябинск" value="{{ default_city }}" />
          </label>
          <div class="research-hint"><strong>Единица обязательна для проверки</strong><span>Формат строки: <b>название | единица</b>. Например: «Кабель ВВГнг 3х2,5 | м». Без единицы найденные цены останутся кандидатами. За раз — до 5 позиций.</span></div>
          <div class="btn-row">
            <button class="btn" id="researchRunBtn" type="button" onclick="runResearch()"><span class="research-button-icon" aria-hidden="true"></span><span data-research-button-label>Найти цены</span></button>
            <button class="btn secondary" type="button" onclick="fillExample()">Подставить пример</button>
          </div>
        </div>
      </div>
    </section>

    <section class="panel research-results-panel">
      <div class="research-status"><span aria-hidden="true"></span><div id="researchStatus" class="muted" aria-live="polite">Пока ничего не искали.</div></div>
      <div id="researchResults" class="results" style="margin-top:12px;"></div>
    </section>
  </div>

  <script src="/research/client.js?v=20260915-1"></script>
</body>
</html>
"""




ESTIMATE_MARKET_VIEW_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  <title>{{ meta.title }} · Сравнение цен</title>
  <style>
    :root { color-scheme: light; --bg:#f4f7fb; --panel:#ffffff; --border:#d9e3ef; --muted:#62748b; --text:#172235; }
    body { margin:0; font-family: Segoe UI, Arial, sans-serif; background:linear-gradient(180deg,#ffffff 0,#f4f7fb 100%); color:var(--text); }
    .page { max-width:1040px; margin:0 auto; padding:14px 12px 24px; }
    .panel { background:linear-gradient(180deg,#ffffff,#f8fbff); border:1px solid var(--border); border-radius:14px; box-shadow:0 14px 34px rgba(28,49,84,.08); padding:10px; margin-bottom:10px; }
    .muted,.where { color:var(--muted); }
    .chips { display:flex; flex-wrap:wrap; gap:6px; }
    .chip { display:inline-flex; align-items:center; gap:5px; text-decoration:none; border:1px solid #cfd9e8; background:#fff; color:#35506f; border-radius:10px; padding:6px 10px; font-weight:700; font-size:11px; }
    .chip.is-active { background:linear-gradient(180deg,#2e80e8,#1f72dc); border-color:#2e80e8; color:#fff; }
    .items { display:grid; gap:0; }
    .item { position:relative; border:1px solid #d9e3ef; border-radius:12px; background:linear-gradient(180deg,#ffffff,#f8fbff); padding:8px 9px; box-shadow:0 8px 18px rgba(28,49,84,.06); }
    .item + .item { margin-top:8px; }
    .item + .item::after { content:""; position:absolute; left:10px; right:10px; top:-12px; height:2px; background:linear-gradient(90deg, rgba(110,168,255,0), rgba(110,168,255,.55), rgba(46,139,87,.75), rgba(110,168,255,.55), rgba(110,168,255,0)); box-shadow:0 0 8px rgba(110,168,255,.16); }
    .item-head { display:flex; justify-content:space-between; gap:8px; align-items:flex-start; margin-bottom:6px; padding-bottom:6px; border-bottom:1px solid #e5ecf4; }
    .item-title { font-weight:700; font-size:12px; line-height:1.28; }
    .item-index { display:inline-flex; align-items:center; justify-content:center; min-width:22px; height:22px; padding:0 6px; border-radius:999px; background:linear-gradient(180deg,#2e80e8,#1f72dc); color:#fff; font-weight:800; font-size:10px; border:1px solid #2e80e8; box-shadow:0 4px 12px rgba(46,128,232,.16); }
    .tag { display:inline-flex; border-radius:999px; padding:1px 6px; border:1px solid #cfd9e8; background:#f4f8fd; color:#35506f; font-size:9px; white-space:nowrap; }
    .meta { display:flex; flex-wrap:wrap; gap:5px; margin-bottom:6px; }
    .offers { display:grid; gap:6px; margin-top:6px; }
    .offer { border:1px solid #dfe7f1; border-radius:9px; background:#fff; padding:6px; }
    .offer-top { display:flex; justify-content:space-between; gap:8px; align-items:flex-start; }
    .offer-title a { color:#1f72dc; text-decoration:none; }
    .offer-title a:hover { text-decoration:underline; }
    .offer-snippet { color:#62748b; font-size:10px; line-height:1.25; margin-top:4px; white-space:pre-wrap; }
    .status-note { color:#a06b18; font-size:10px; margin-top:5px; }
    .num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
  </style>
  <link rel="stylesheet" href="/static/autobot-ui.css?v=20260902-tabs-1" />
</head>
<body class="autobot-page market-view-page">
  <header class="topbar autobot-section-bar">
    <a class="brand" href="/estimates">
      <span class="brand-mark" aria-hidden="true"><i></i></span>
      <span class="brand-copy"><strong>AutoBot</strong><small>Закупки без рутины</small></span>
    </a>
    <nav class="topnav" aria-label="Разделы AutoBot">
      <a class="topnav-primary is-active" href="/estimates" aria-current="page">Сметы</a>
      <a class="topnav-primary" href="/tenders">Тендеры</a>
      <a href="/research">Поиск позиции</a>
    </nav>
  </header>
  <div class="page">
    <p style="margin:0 0 10px;"><a href="/estimates/{{ meta.id }}?{{ back_query }}">← Назад к смете</a> · <a href="/estimates">Все сметы</a> · <a href="/research">Поиск по позиции</a></p>
    <h1 style="margin:0 0 8px;">{{ meta.title }} · Сравнение цен</h1>
    <div class="muted">Показываю найденные сайты и цены по выбранному типу позиций.</div>

    <section class="panel">
      {% if market_links %}
      <div class="chips">
        {% for link in market_links %}
        <a class="chip{% if link.key == active_market_type %} is-active{% endif %}" href="{{ link.href }}">{{ link.label }} · {{ link.count }}</a>
        {% endfor %}
      </div>
      {% else %}
      <div>Пока нет сохранённых сравнений по текущему фильтру.</div>
      {% endif %}
    </section>

    <section class="panel">
      {% if active_section %}
      <div class="items">
        {% for item in active_section["items"] %}
        <article class="item">
          <span data-market-contract="1" hidden></span>
          <div class="item-head">
            <div style="display:flex; gap:12px; align-items:flex-start;">
              <div class="item-index">{{ item.position_index or loop.index }}</div>
              <div class="item-title">{{ item.name }}</div>
            </div>
            <span class="tag">{{ item.type_label }}</span>
          </div>
          <div class="meta">
            <span class="tag">Кол-во: {{ item.qty_fmt }}</span>
            <span class="tag">Ед.: {{ item.unit or "—" }}</span>
            <span class="tag">Смета за ед.: {{ item.estimate_price_fmt }}</span>
            <span class="tag">Смета всего: {{ item.estimate_total_fmt }}</span>
            <span class="tag">Рынок: {{ item.market_prices or "—" }}</span>
          </div>
          {% if item["offers"] %}
          <div class="offers">
            {% for offer in item["offers"] %}
            <div class="offer">
              <div class="offer-top">
                <div class="offer-title">
                  {% if offer.url %}
                  <a href="{{ offer.url }}" target="_blank" rel="noopener noreferrer">{{ offer.title }}</a>
                  {% else %}
                  {{ offer.title }}
                  {% endif %}
                </div>
                <div class="num">{{ offer.price_fmt }}</div>
              </div>
              {% if offer.source and offer.source != "Интернет" %}
              <div class="where">{{ offer.source }}</div>
              {% endif %}
              {% if offer.snippet %}
              <div class="offer-snippet">{{ offer.snippet }}</div>
              {% endif %}
            </div>
            {% endfor %}
          </div>
          {% endif %}
          <div class="status-note">{{ item.status }}</div>
          {% if item.candidates %}
          <details class="candidate-sources"><summary>Требуют проверки · {{ item.candidates|length }}</summary>
            {% for candidate in item.candidates %}<p><a href="{{ candidate.url }}" target="_blank" rel="noopener noreferrer">{{ candidate.title }}</a> · {{ candidate.price_fmt }}<br><small>{{ candidate.reason }}</small></p>{% endfor %}
          </details>
          {% endif %}
        </article>
        {% endfor %}
      </div>
      {% else %}
      <div>По выбранному типу пока нет сохранённых данных.</div>
      {% endif %}
    </section>
  </div>
</body>
</html>
"""

def _render_estimates_page_v2():
    def _sort_created_key(raw: object) -> tuple[int, float]:
        text = str(raw or "").strip()
        if not text:
            return (1, 0.0)
        try:
            dt = datetime.strptime(text, "%d.%m.%Y %H:%M")
            return (0, -dt.timestamp())
        except Exception:
            return (0, 0.0)

    type_order = ["material", "work", "service", "product", "other"]
    type_labels = {
        "material": "Материалы",
        "work": "Работы",
        "service": "Услуги",
        "product": "Товары",
        "other": "Другое",
    }
    cards = []
    total_rows = 0
    total_sum = 0.0
    total_sum_known = False
    with_compare = 0

    for meta in _read_estimates_index():
        if not isinstance(meta, dict):
            continue
        estimate_id = str(meta.get("id") or "")
        rows = _load_estimate_rows(estimate_id)
        summary = _summarize_estimate_rows(rows)
        type_counts = summary.get("type_counts") or {}
        type_badges = [{"key": key, "label": type_labels.get(key, key), "count": int(type_counts.get(key) or 0)} for key in type_order if int(type_counts.get(key) or 0) > 0]
        types_short = ", ".join(b["label"] for b in type_badges[:3]) if type_badges else "Без типов"
        has_market_compare = _estimate_market_merged_path(estimate_id).is_file()
        has_market_sources = _estimate_market_raw_path(estimate_id).is_file()
        market_done, market_total = _estimate_market_progress_for_card(estimate_id, rows)
        market_total = max(market_total, int(summary.get("row_count") or 0))
        market_done = max(0, min(market_done, market_total)) if market_total > 0 else 0
        market_pct = int(min(100, max(0, round(100.0 * market_done / market_total)))) if market_total > 0 else 0
        if market_total > 0 and market_done == market_total:
            market_status_label = "Цены собраны"
            market_status_class = "status-ready"
            market_summary = f"Подтверждены цены: {market_done} из {market_total}"
            market_summary_class = "metric-good"
            market_progress_note = "Откройте сравнение и проверьте условия предложений."
            with_compare += 1
        elif has_market_sources or has_market_compare:
            market_status_label = "Часть цен найдена" if market_done else "Проверить источники"
            market_status_class = "status-partial"
            market_summary = f"Подтверждены цены: {market_done} из {market_total}"
            market_summary_class = ""
            market_progress_note = "Продолжите поиск недостающих цен внутри сметы. Кандидаты в расчёт не входят."
            with_compare += int(market_done > 0)
        else:
            market_status_label = "Поиск не запускался"
            market_status_class = "status-idle"
            market_summary = "Рынок ещё не анализировали"
            market_summary_class = "metric-bad"
            market_progress_note = "Поиск цен ещё не запускался."
        from autobot import uploaded_market
        running = bool((_uploaded_market_call(uploaded_market.status, estimate_id) or {}).get('running'))
        if running:
            market_status_label = "Идёт поиск"
            market_status_class = "status-partial"
            market_progress_note = "Поиск продолжается. " + market_progress_note

        item = dict(meta)
        item["total_sum_fmt"] = _fmt_money(summary.get("total_sum"))
        item["type_badges"] = type_badges
        item["types_short"] = types_short
        item["market_status_label"] = market_status_label
        item["market_status_class"] = market_status_class
        item["market_summary"] = market_summary
        item["market_summary_class"] = market_summary_class
        item["market_progress_done"] = market_done
        item["market_progress_total"] = market_total
        item["market_progress_percent"] = market_pct
        item["market_progress_note"] = market_progress_note
        item["row_count"] = int(summary.get("row_count") or 0)
        item["total_sum_value"] = float(summary.get("total_sum") or 0.0)
        item["crm_import_capability"] = _issue_estimate_import_capability(estimate_id, ttl_seconds=3600)
        cards.append(item)

        total_rows += int(summary.get("row_count") or 0)
        sum_value = summary.get("total_sum")
        if sum_value is not None:
            total_sum += float(sum_value)
            total_sum_known = True

    cards.sort(key=lambda x: _sort_created_key(x.get("created_at")))
    overview = {
        "total_count": len(cards),
        "total_rows": total_rows,
        "with_compare": with_compare,
        "total_sum_fmt": _fmt_money(total_sum) if total_sum_known else "—",
    }
    response = make_response(
        render_template(
            "estimates.html",
            estimates=cards,
            overview=overview,
            crm_prefill={},
            crm_parent_origin=_configured_crm_parent_origin(),
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/estimates")
def estimates_page():
    return _render_estimates_page_v2()


@app.route("/research")
def research_page():
    return render_template_string(
        RESEARCH_TEMPLATE,
        default_query=(request.args.get("q", "") or "").strip(),
        default_city=(request.args.get("city", "") or "").strip(),
    )


@app.get("/research/client.js")
def research_client_script():
    return app.send_static_file("research.js")


@app.route("/research/items", methods=["POST"])
@app.route("/api/research-items", methods=["POST"])
def api_research_items():
    data = request.get_json(silent=True) or {}
    specs = _research_specs_from_text(str(data.get("queries") or data.get("query") or ""))
    city = re.sub(r"\s+", " ", str(data.get("city") or "").strip())[:120]
    if not specs:
        return jsonify({"ok": False, "message": "Нужна хотя бы одна позиция для поиска."}), 400

    from autobot.item_research import parse_sources, research_item

    sources = parse_sources(
        os.environ.get("MARKET_SUMMARY_SOURCES")
        or os.environ.get("MARKET_SOURCES")
        or "web"
    )

    results: list[dict] = []
    for spec in specs:
        query = spec["query"]
        unit = spec["unit"]
        try:
            item = research_item(query, unit=unit, region=city, sources=sources, max_results=3)
            offers = []
            for offer in item.offers[:5]:
                offers.append(
                    {
                        "source": str(offer.source or ""),
                        "title": str(offer.title or ""),
                        "price": float(offer.price or 0) if offer.price else 0,
                        "url": str(offer.url or ""),
                        "snippet": str(offer.snippet or "")[:500],
                        "verification": str(offer.verification or "candidate"),
                        "verified": str(offer.verification or "") == "verified",
                        "confidence": float(offer.confidence or 0),
                        "reason": str(offer.verification_reason or offer.page_error or "")[:500],
                        "matched_unit": str(offer.matched_unit or ""),
                        "page_checked": bool(offer.page_checked),
                        "adapter": str(offer.adapter or ""),
                        "price_scope": str(offer.price_scope or ""),
                    }
                )
            results.append(
                {
                    "query": item.query,
                    "unit": item.unit,
                    "region": item.region,
                    "position_type": item.position_type,
                    "position_label": item.position_label,
                    "strategy": item.strategy,
                    "warning": item.warning,
                    "offers": offers,
                    "errors": str(item.errors or ""),
                }
            )
        except Exception as e:
            results.append(
                {
                    "query": query,
                    "unit": unit,
                    "region": city,
                    "offers": [],
                    "errors": str(e)[:400],
                }
            )
    return jsonify(
        {
            "ok": True,
            "message": f"Готово. Проверено позиций: {len(results)}.",
            "results": results,
        }
    )


@app.route("/estimates/<estimate_id>")
def estimate_detail_page(estimate_id: str):
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    meta, rows_all = _load_estimate_document(estimate_id)
    if not meta:
        abort(404)
    q = (request.args.get("q", "") or "").strip()
    selected_types = _normalize_selected_estimate_types(request.args.getlist("types"))
    if not selected_types:
        legacy_type = (request.args.get("type", "") or "").strip()
        if legacy_type:
            selected_types = _normalize_selected_estimate_types([legacy_type])
        elif str(request.args.get("hide_work", "") or "").strip() in {"1", "true", "yes", "on"}:
            selected_types = [x for x in ["service", "product", "material", "other"]]
    rows = _filter_estimate_rows(rows_all, q=q, selected_types=selected_types)
    summary = _summarize_estimate_rows(rows)
    summary["total_sum_fmt"] = _fmt_money(summary.get("total_sum"))
    summary["avg_price_fmt"] = _fmt_money(summary.get("avg_price"))
    type_counts = _summarize_estimate_rows(rows_all).get("type_counts") or {}
    labels_full = {"work": "Работы", "service": "Услуги", "product": "Товары/изделия", "material": "Материалы", "other": "Другое"}
    order = ["work", "service", "product", "material", "other"]
    type_options = [
        {"key": k, "label": labels_full.get(k, k), "count": int(type_counts.get(k, 0))}
        for k in order
        if int(type_counts.get(k, 0)) > 0
    ]
    filter_query = urlencode([("q", q)] + [("types", t) for t in selected_types], doseq=True)
    rows_view = []
    display_no = 0
    last_section = None
    current_sheet = None
    current_sheet_total = 0.0
    current_sheet_has_sum = False
    for r in rows:
        rr = dict(r)
        sheet = str(rr.get("sheet") or "").strip()
        if current_sheet is None:
            current_sheet = sheet
        elif sheet != current_sheet:
            rows_view.append(
                {
                    "_is_sheet_total": True,
                    "sheet_title": current_sheet,
                    "sheet_total_fmt": _fmt_money(current_sheet_total if current_sheet_has_sum else None),
                }
            )
            rows_view.append({"_is_sheet_break": True})
            current_sheet = sheet
            current_sheet_total = 0.0
            current_sheet_has_sum = False
            last_section = None
        section = _normalize_section_title(str(rr.get("section") or ""))
        rr["section"] = section
        if section and section != last_section:
            rows_view.append(
                {
                    "_is_section": True,
                    "section_title": section,
                }
            )
            last_section = section
        display_no += 1
        rr["_is_section"] = False
        rr["display_no"] = display_no
        rr["qty_fmt"] = _fmt_qty(_json_num(rr.get("qty")))
        rr["unit_price_fmt"] = _fmt_money(_json_num(rr.get("unit_price")))
        rr["total_fmt"] = _fmt_money(_json_num(rr.get("total")))
        total_num = _json_num(rr.get("total"))
        if total_num is not None:
            current_sheet_total += total_num
            current_sheet_has_sum = True
        rows_view.append(rr)
    if current_sheet is not None and rows_view:
        rows_view.append(
            {
                "_is_sheet_total": True,
                "sheet_title": current_sheet,
                "sheet_total_fmt": _fmt_money(current_sheet_total if current_sheet_has_sum else None),
            }
        )
    # Capture before reading: a report replaced during rendering triggers a fresh page next poll.
    market_revision = _estimate_market_revision(estimate_id)
    market_path = _estimate_market_raw_path(estimate_id)
    if not market_path.is_file():
        market_path = _estimate_market_merged_path(estimate_id)
    compare_df = _estimate_market_df_for_rows(market_path, rows)
    raw_df = _estimate_market_df_for_rows(_estimate_market_raw_path(estimate_id), rows, preserve_candidates=True)
    compare_rows = _estimate_compare_rows(rows, compare_df)
    source_rows = _estimate_source_rows(rows, raw_df)
    scope_info = _estimate_market_scope_info(meta, selected_types)
    table_views = {
        "estimate": {"available": bool(rows)},
        "compare": {"available": not compare_df.empty},
        "sources": {"available": bool(rows) and not raw_df.empty},
    }
    active_table_view = _pick_estimate_active_table_view(request.args.get("table_view", ""), table_views)
    viability = _estimate_viability_overview(compare_df, compare_rows, scope_info)
    market_sections = _estimate_market_sections(estimate_id, rows, selected_types=selected_types)
    market_links = _estimate_market_links(estimate_id, market_sections, q=q, selected_types=selected_types)
    crm_prefill = _estimate_crm_prefill(estimate_id, document=(meta, rows_all))
    reconciliation = _estimate_reconciliation_view(meta)
    return render_template(
        "estimate_detail.html",
        meta=meta,
        original_available=_estimate_original_path(estimate_id, meta) is not None,
        market_revision=market_revision,
        scope_info=scope_info,
        rows=rows_view,
        q=q,
        selected_types=selected_types,
        filter_query=filter_query,
        market_city=str(meta.get("market_city") or ""),
        has_market_raw=_estimate_market_raw_path(estimate_id).is_file(),
        has_market_merged=_estimate_market_merged_path(estimate_id).is_file(),
        active_table_view=active_table_view,
        compare_table=table_views["compare"],
        compare_rows=compare_rows,
        sources_table=table_views["sources"],
        source_rows=source_rows,
        viability=viability,
        market_links=market_links,
        type_options=type_options,
        summary=summary,
        reconciliation=reconciliation,
        crm_prefill=crm_prefill,
        crm_parent_origin=_configured_crm_parent_origin(),
        estimate_import_capability=_issue_estimate_import_capability(estimate_id),
        legacy_crm_export_allowed=_legacy_browser_crm_export_allowed(),
    )


@app.get('/estimates/catalog.css')
def estimate_catalog_css():
    return app.send_static_file('estimate_catalog.css')


@app.get('/estimates/workspace.css')
def estimate_workspace_css():
    return app.send_static_file('estimate_workspace.css')


@app.get('/estimates/upload-client.js')
def estimate_upload_client_js():
    return app.send_static_file('estimate_upload.js')


@app.get('/estimates/workspace.js')
def estimate_workspace_js():
    return app.send_static_file('estimate_workspace.js')


@app.get('/estimates/<estimate_id>/original')
def estimate_original_download(estimate_id: str):
    if not re.fullmatch(r'[0-9a-fA-F-]{1,40}', estimate_id or ''):
        abort(404)
    meta = _load_estimate_original_meta(estimate_id)
    path = _estimate_original_path(estimate_id, meta) if meta else None
    if path is None:
        abort(404)
    name = re.split(r'[/\\]', str(meta.get('original_filename') or path.name))[-1]
    name = re.sub(r'[\x00-\x1f\x7f]', '', name) or path.name
    response = send_file(path, as_attachment=True, download_name=name, max_age=0)
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.route("/estimates/<estimate_id>/market-view")
def estimate_market_view_page(estimate_id: str):
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    meta = _load_estimate_meta(estimate_id)
    if not meta:
        abort(404)
    rows_all = _load_estimate_rows(estimate_id)
    q = (request.args.get("q", "") or "").strip()
    selected_types = _normalize_selected_estimate_types(request.args.getlist("types"))
    if not selected_types:
        legacy_type = (request.args.get("type", "") or "").strip()
        if legacy_type:
            selected_types = _normalize_selected_estimate_types([legacy_type])
        elif str(request.args.get("hide_work", "") or "").strip() in {"1", "true", "yes", "on"}:
            selected_types = [x for x in ["service", "product", "material", "other"]]
    rows = _filter_estimate_rows(rows_all, q=q, selected_types=selected_types)
    market_sections = _estimate_market_sections(estimate_id, rows, selected_types=selected_types)
    market_links = _estimate_market_links(estimate_id, market_sections, q=q, selected_types=selected_types)
    active_market_type = (request.args.get("market_type", "") or "").strip()
    active_section = None
    if market_sections:
        active_section = next((sec for sec in market_sections if str(sec.get("key") or "") == active_market_type), None) or market_sections[0]
        active_market_type = str(active_section.get("key") or "")
    back_query = urlencode([("q", q)] + [("types", t) for t in selected_types], doseq=True)
    return render_template_string(
        ESTIMATE_MARKET_VIEW_TEMPLATE,
        meta=meta,
        market_links=market_links,
        active_market_type=active_market_type,
        active_section=active_section,
        back_query=back_query,
    )


@app.route("/estimates/<estimate_id>/download.xlsx")
def estimate_detail_download_xlsx(estimate_id: str):
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    meta, rows_all = _load_estimate_document(estimate_id)
    if not meta:
        abort(404)
    q = (request.args.get("q", "") or "").strip()
    selected_types = _normalize_selected_estimate_types(request.args.getlist("types"))
    if not selected_types:
        legacy_type = (request.args.get("type", "") or "").strip()
        if legacy_type:
            selected_types = _normalize_selected_estimate_types([legacy_type])
        elif str(request.args.get("hide_work", "") or "").strip() in {"1", "true", "yes", "on"}:
            selected_types = [x for x in ["service", "product", "material", "other"]]
    rows = _filter_estimate_rows(rows_all, q=q, selected_types=selected_types)
    from openpyxl.styles import Font, PatternFill

    columns = ["№", "Тип", "Наименование", "Ед.", "Кол-во", "Цена за ед.", "Сумма", "Лист", "Строка Excel", "Раздел"]
    export_rows: list[dict] = []
    total_row_excel_numbers: list[int] = []
    break_row_excel_numbers: list[int] = []
    display_no = 0
    current_sheet = None
    current_sheet_total = 0.0
    current_sheet_has_sum = False

    def _append_sheet_total(sheet_name: str | None) -> None:
        export_rows.append(
            {
                "№": "",
                "Тип": "",
                "Наименование": f"Итого по листу: {sheet_name or 'без названия'}",
                "Ед.": "",
                "Кол-во": None,
                "Цена за ед.": None,
                "Сумма": current_sheet_total if current_sheet_has_sum else None,
                "Лист": sheet_name or "",
                "Строка Excel": None,
                "Раздел": "",
            }
        )
        total_row_excel_numbers.append(len(export_rows) + 1)

    def _append_break_row() -> None:
        export_rows.append({c: "" for c in columns})
        break_row_excel_numbers.append(len(export_rows) + 1)

    for r in rows:
        sheet = str(r.get("sheet") or "").strip()
        if current_sheet is None:
            current_sheet = sheet
        elif sheet != current_sheet:
            _append_sheet_total(current_sheet)
            _append_break_row()
            current_sheet = sheet
            current_sheet_total = 0.0
            current_sheet_has_sum = False

        display_no += 1
        total_num = _json_num(r.get("total"))
        if total_num is not None:
            current_sheet_total += total_num
            current_sheet_has_sum = True
        export_rows.append(
            {
                "№": display_no,
                "Тип": str(r.get("type_label") or ""),
                "Наименование": str(r.get("name") or ""),
                "Ед.": str(r.get("unit") or ""),
                "Кол-во": _json_num(r.get("qty")),
                "Цена за ед.": _json_num(r.get("unit_price")),
                "Сумма": total_num,
                "Лист": sheet,
                "Строка Excel": r.get("excel_row"),
                "Раздел": _normalize_section_title(str(r.get("section") or "")),
            }
        )

    if current_sheet is not None and rows:
        _append_sheet_total(current_sheet)

    df = pd.DataFrame(export_rows, columns=columns)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Смета")
        ws = writer.book["Смета"]
        total_fill = PatternFill(fill_type="solid", fgColor="2F4F6F")
        total_font = Font(color="FFD7D7", bold=True)
        break_fill = PatternFill(fill_type="solid", fgColor="6B2331")
        for row_no in total_row_excel_numbers:
            for cell in ws[row_no]:
                cell.fill = total_fill
                cell.font = total_font
        for row_no in break_row_excel_numbers:
            for cell in ws[row_no]:
                cell.fill = break_fill
            ws.row_dimensions[row_no].height = 12
    buf.seek(0)
    safe_stem = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._ -]+", "_", str(meta.get("title") or estimate_id)).strip(" ._") or estimate_id
    filename = f"{safe_stem}.xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
        max_age=0,
    )


@app.route("/estimates/<estimate_id>/market-sources.xlsx")
def estimate_market_sources_download_xlsx(estimate_id: str):
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    meta = _load_estimate_meta(estimate_id) or {}
    rows_all = _load_estimate_rows(estimate_id)
    if not rows_all:
        abort(404)
    q = (request.args.get("q", "") or "").strip()
    selected_types = _normalize_selected_estimate_types(request.args.getlist("types"))
    rows = _filter_estimate_rows(rows_all, q=q, selected_types=selected_types)
    raw_df = _estimate_market_df_for_rows(_estimate_market_raw_path(estimate_id), rows, preserve_candidates=True)
    source_rows = _estimate_source_rows(rows, raw_df)
    if not source_rows:
        abort(404)
    filename = _safe_download_stem(str(meta.get("title") or estimate_id), estimate_id)
    export_df = _simple_sources_export_df(source_rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False, sheet_name="Источники")
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{filename} - источники рынка.xlsx", max_age=0)


@app.route("/estimates/<estimate_id>/market-compare.xlsx")
def estimate_market_compare_download_xlsx(estimate_id: str):
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    meta = _load_estimate_meta(estimate_id) or {}
    rows_all = _load_estimate_rows(estimate_id)
    if not rows_all:
        abort(404)
    q = (request.args.get("q", "") or "").strip()
    selected_types = _normalize_selected_estimate_types(request.args.getlist("types"))
    rows = _filter_estimate_rows(rows_all, q=q, selected_types=selected_types)
    market_path = _estimate_market_raw_path(estimate_id)
    if not market_path.is_file():
        market_path = _estimate_market_merged_path(estimate_id)
    compare_df = _estimate_market_df_for_rows(market_path, rows)
    compare_rows = _estimate_compare_rows(rows, compare_df)
    if not compare_rows:
        abort(404)
    filename = _safe_download_stem(str(meta.get("title") or estimate_id), estimate_id)
    export_df = _simple_compare_export_df(compare_rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False, sheet_name="Сравнение")
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{filename} - сравнение рынка.xlsx", max_age=0)


@app.route("/tenders/<tender_id>/estimate.xlsx")
def tender_estimate_download_xlsx(tender_id: str):
    tid = (tender_id or "").strip()
    if not tid or "/" in tid or ".." in tid:
        abort(404)
    path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    if not path.is_file():
        abort(404)
    meta = load_tender_metadata().get(tid) or {}
    filename = _safe_download_stem(meta.get("title") or tid, tid)
    return send_file(path, as_attachment=True, download_name=f"{filename} - смета.xlsx", max_age=0)


@app.route("/tenders/<tender_id>/market-sources.xlsx")
def tender_market_sources_download_xlsx(tender_id: str):
    tid = (tender_id or "").strip()
    if not tid or "/" in tid or ".." in tid:
        abort(404)
    path = _price_output_path_for_tender(tid)
    if not path.is_file():
        abort(404)
    meta = load_tender_metadata().get(tid) or {}
    filename = _safe_download_stem(meta.get("title") or tid, tid)
    return send_file(path, as_attachment=True, download_name=f"{filename} - источники рынка.xlsx", max_age=0)


@app.route("/tenders/<tender_id>/svodka.xlsx")
def tender_svodka_download_xlsx(tender_id: str):
    tid = (tender_id or "").strip()
    if not tid or "/" in tid or ".." in tid:
        abort(404)
    from autobot.merge_estimate_market import OUT_PREFIX, _normalize_market_columns
    from autobot.market_contract import merge_market_frames, sanitize_market_frame

    path = REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx"
    raw_path = _price_output_path_for_tender(tid)
    market_path = raw_path if raw_path.is_file() else path
    if not market_path.is_file():
        abort(404)
    market = _normalize_market_columns(pd.read_excel(market_path))
    estimate_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    meta = load_tender_metadata().get(tid) or {}
    if estimate_path.is_file():
        estimate = pd.read_excel(estimate_path)
        if meta.get('region'):
            estimate['Регион поиска'] = str(meta['region'])
        frame = merge_market_frames(estimate, market)
    else:
        frame = sanitize_market_frame(market)
    buf = io.BytesIO()
    frame.to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)
    meta = load_tender_metadata().get(tid) or {}
    filename = _safe_download_stem(meta.get("title") or tid, tid)
    return send_file(buf, as_attachment=True, download_name=f"{filename} - сравнение рынка.xlsx", max_age=0)


@app.route("/api/estimates/<estimate_id>/market-status")
def api_estimate_market_status(estimate_id: str):
    from autobot import uploaded_market
    meta = _load_estimate_meta(estimate_id)
    if meta is None:
        return jsonify({'ok': False, 'message': 'Смета не найдена.'}), 404
    run_id = request.args.get('run_id')
    job = _uploaded_market_call(uploaded_market.status, estimate_id, run_id=run_id) or {}
    if run_id and not job:
        return jsonify({'ok': False, 'message': 'Запуск ещё не найден.'}), 404
    payload = dict(job, ok=True, result_ok=bool(job.get('ok')), running=bool(job.get('running')),
        city=job.get('city', meta.get('market_city', '')), selected_types=job.get('selected_types', meta.get('market_selected_types', [])),
        log_tail=job.get('log_lines', []), has_raw=_estimate_market_raw_path(estimate_id).is_file(),
        has_merged=_estimate_market_merged_path(estimate_id).is_file(), market_revision=_estimate_market_revision(estimate_id),
        available_sources=['web'])
    payload.pop('log_lines', None)
    response = jsonify(payload)
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route("/api/estimates/<estimate_id>/market-start", methods=["POST"])
def api_estimate_market_start(estimate_id: str):
    from autobot import uploaded_market
    from autobot.market_web_worker import web_worker_enabled
    if not web_worker_enabled():
        return jsonify({'ok':False,'message':'Поиск на сервере отключён. Повторите после его включения.'}),503
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'message': 'Ожидаются условия поиска.'}), 400
    if data.get('sources', ['web']) != ['web']:
        return jsonify({'ok': False, 'message': 'Сейчас доступен поиск по сайтам поставщиков. Авито ещё не подключён.'}), 400
    run, duplicate = _uploaded_market_call(uploaded_market.enqueue, estimate_id, city=data.get('city', ''),
        selected_types=data.get('selected_types'), operation_id=data.get('operation_id'), root=USER_ESTIMATES_DIR)
    return jsonify({'ok': True, 'accepted': True, 'run_id': run['run_id'], 'duplicate': duplicate,
                    'message': 'Поиск принят. Задания сохраняются после закрытия страницы.'})


@app.route("/api/estimates/<estimate_id>/market-stop", methods=["POST"])
def api_estimate_market_stop(estimate_id: str):
    from autobot import uploaded_market
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'message': 'Некорректный запрос.'}), 400
    count = _uploaded_market_call(uploaded_market.cancel, estimate_id, run_id=data.get('run_id'))
    return jsonify({'ok': True, 'canceled': count, 'message': 'Активные позиции отменены. Сохранённые цены доступны.'})


@app.route("/api/estimates/upload", methods=["POST"])
def api_estimates_upload():
    from autobot import upload_admission
    f = request.files.get("file")
    if not f or not getattr(f, "filename", None):
        return jsonify({"ok": False, "message": "Выберите файл сметы."}), 400
    if not _estimate_upload_allowed(f.filename):
        return jsonify({"ok": False, "message": "Нужен файл сметы: .xlsx, .xls, .xlsm или .pdf."}), 400
    try:
        job_id = upload_admission.operation_key(request.form.get('operation_id'))
        job, duplicate = upload_admission.receive(
            f.stream, key=job_id, original_name=_safe_upload_filename(f.filename),
            title=(request.form.get('title') or '').strip()[:160], source_root=USER_ESTIMATES_DIR,
            jobs_dir=ESTIMATE_UPLOAD_JOBS_DIR, repo_root=REPO_ROOT,
            max_bytes=_configured_max_upload_mb() * 1024 * 1024)
    except upload_admission.AdmissionError as error:
        return jsonify({'ok': False, 'message': str(error), 'retry_upload': error.retry_upload}), error.status
    except (OSError, TimeoutError):
        return jsonify({'ok': False, 'message': 'Не удалось подтвердить приём файла. Повторите эту загрузку позже.'}), 503
    with estimate_upload_lock:
        worker_active = job_id in estimate_upload_workers
        if not worker_active:
            estimate_upload_jobs[job_id] = job
    if job.get('running') and not worker_active and not _start_estimate_upload_worker(job_id):
        return jsonify({'ok': False, 'accepted': True, 'job_id': job_id,
                        'message': 'Файл принят, но обработчик не запустился. Проверьте статус загрузки.'}), 503
    return jsonify({'ok': True, 'accepted': True, 'job_id': job_id, 'duplicate': duplicate,
                    'progress': int(job.get('progress') or 26), 'stage': job.get('stage') or 'Файл получен',
                    'detail': 'Файл сохранён. Возвращаюсь к прежнему заданию' if duplicate else 'Сервер принял файл',
                    'message': 'Смета загружена на сервер.'})


def _recover_admitted_upload(job_id):
    from autobot import upload_admission
    try:
        job = upload_admission.restore(ESTIMATE_UPLOAD_JOBS_DIR, USER_ESTIMATES_DIR, REPO_ROOT, job_id)
    except upload_admission.AdmissionError:
        raise
    except (OSError, TimeoutError) as error:
        raise upload_admission.AdmissionError('Загрузка временно недоступна. Повторите проверку статуса.') from error
    if job is not None:
        with estimate_upload_lock:
            if job_id not in estimate_upload_workers:
                estimate_upload_jobs[job_id] = job
    return job


@app.route("/api/estimates/upload-status/<job_id>")
def api_estimates_upload_status(job_id: str):
    from autobot import upload_admission
    try:
        admitted = _recover_admitted_upload(job_id)
    except upload_admission.AdmissionError as error:
        return jsonify({'ok': False, 'message': str(error), 'retry_upload': error.retry_upload}), error.status
    with estimate_upload_lock:
        stored_job = admitted or _estimate_upload_load_locked(job_id) or estimate_upload_jobs.get(job_id)
        job = dict(stored_job or {})
        worker_active = job_id in estimate_upload_workers
    if not job:
        return jsonify({"ok": False, "message": "Статус загрузки не найден."}), 404
    if job.get("running") and not worker_active:
        _start_estimate_upload_worker(job_id, recovering=True)
        with estimate_upload_lock:
            job = dict(estimate_upload_jobs.get(job_id) or job)
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "estimate_id": job.get("estimate_id"),
            "original_url": '/estimates/uploads/' + job_id + '/original' if _upload_job_original_path(job) is not None else '',
            "original_filename": str(job.get('original_name') or ''),
            "running": bool(job.get("running")),
            "result_ok": bool(job.get("ok")),
            "progress": int(job.get("progress") or 0),
            "progress_estimated": bool(job.get("progress_estimated")),
            "stage": job.get("stage") or "",
            "detail": job.get("detail") or "",
            "error": job.get("error") or "",
            "started_at": job.get("started_at"),
            "ended_at": job.get("ended_at"),
            "updated_at": job.get("updated_at"),
            "elapsed_seconds": int(job.get("elapsed_seconds") or 0),
            "log_tail": list(job.get("log_lines") or [])[-12:],
        }
    )


def _upload_job_original_path(job):
    estimate_id = str(job.get('target_estimate_id') or job.get('estimate_id') or '')
    return _estimate_original_path(estimate_id, {'source_path': job.get('source_path')})


@app.get('/estimates/uploads/<job_id>/original')
def estimate_upload_original_download(job_id):
    with estimate_upload_lock:
        job = _estimate_upload_load_locked(job_id) or {}
    path = _upload_job_original_path(job)
    if path is None:
        abort(404)
    response = send_file(path, as_attachment=True, download_name=path.name, max_age=0)
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.route("/reports/<path:filename>")
def report_file(filename: str):
    target = REPORTS_DIR / filename
    if not target.exists() or not target.is_file():
        abort(404)
    return send_from_directory(REPORTS_DIR, filename)


MERGE_REPORTS_SITE_DIR = REPO_ROOT / "data" / "reports_site"
NMCK_PREVIEW_DIR = REPO_ROOT / "data" / "nmck_previews"


NMCK_PREVIEW_PAGE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  <title>{{ title|e }} — таблица НМЦК</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: #ffffff;
      --border: #d9e3ef;
      --text: #172235;
      --muted: #62748b;
      --accent: #1f72dc;
    }
    html, body { margin: 0; min-height: 100%; background: linear-gradient(180deg, #ffffff 0%, var(--bg) 100%); color: var(--text); font-family: Segoe UI, Arial, sans-serif; }
    .page { max-width: 100%; padding: 18px 16px 32px; box-sizing: border-box; }
    .head { max-width: 1400px; margin: 0 auto 14px; }
    h1 { font-size: 1.2rem; font-weight: 700; margin: 0 0 6px 0; letter-spacing: -0.02em; line-height: 1.35; word-break: break-word; }
    .sub { font-size: 13px; color: var(--muted); margin: 0 0 12px 0; }
    a.back { color: #1f72dc; font-size: 13px; text-decoration: none; }
    a.back:hover { text-decoration: underline; }
    .table-shell {
      max-width: 1400px;
      margin: 0 auto;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: linear-gradient(180deg, var(--panel), #f8fbff);
      overflow: hidden;
      box-shadow: 0 10px 30px rgba(28, 49, 84, 0.08);
    }
    .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
    table { border-collapse: collapse; width: 100%; font-size: 12px; }
    th, td {
      border: 1px solid #e5ecf4;
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
      line-height: 1.35;
    }
    th {
      background: #f1f6fc;
      color: #35506f;
      font-weight: 600;
      max-width: 28em;
      word-break: break-word;
    }
    tbody tr:nth-child(even) { background: #fafcff; }
    tbody tr:hover { background: #f3f8ff; }
    td { word-break: break-word; max-width: 36em; }
    td.num { font-variant-numeric: tabular-nums; white-space: nowrap; max-width: none; }
    .foot { max-width: 1400px; margin: 14px auto 0; font-size: 11px; color: #62748b; }
  </style>
</head>
<body>
  <div class="page">
    <div class="head">
      <p style="margin:0 0 8px 0;"><a class="back" href="/">← На главную</a></p>
      <h1>{{ title|e }}</h1>
      <p class="sub">{{ subtitle|e }}</p>
    </div>
    <div class="table-shell">
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              {% for col in columns %}
              <th>{{ col }}</th>
              {% endfor %}
            </tr>
          </thead>
          <tbody>
            {% for row in rows %}
            <tr>
              {% for col in columns %}
              {% set v = row.get(col) %}
              <td class="{% if v is number %}num{% endif %}">{{ v if v is not none else '' }}</td>
              {% endfor %}
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
    <p class="foot">Колонки и строки как в загруженном Excel (обоснование НМЦК).</p>
  </div>
</body>
</html>
"""


MISSING_MERGE_PAGE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  <title>Сравнение цен ещё не готово</title>
  <style>
    body { font-family: Segoe UI, sans-serif; background:linear-gradient(180deg,#ffffff 0,#f4f7fb 100%); color:#172235; margin:0; padding:24px; line-height:1.5; }
    a { color:#1f72dc; }
    .box { max-width:580px; margin:0 auto; border:1px solid #d9e3ef; border-radius:12px; padding:20px; background:#ffffff; box-shadow:0 12px 28px rgba(28,49,84,.08); }
    h1 { font-size:1.15rem; margin-top:0; }
    .btn { border:1px solid #2e80e8; background:linear-gradient(180deg,#2e80e8,#1f72dc); color:#ffffff; border-radius:8px; padding:10px 16px; cursor:pointer; font-size:14px; margin-top:12px; margin-right:8px; }
    .btn:disabled { opacity:.5; cursor:not-allowed; }
    .merge-bar-wrap { height:12px; background:#edf3fa; border-radius:8px; overflow:hidden; margin-top:12px; border:1px solid #d6e0ee; }
    .merge-bar-fill { height:100%; background:linear-gradient(90deg,#3d5290,#5ecf8a); transition:width .35s ease; }
    .logs { margin-top:10px; max-height:140px; overflow:auto; font-family:Consolas,monospace; font-size:11px; white-space:pre-wrap; background:#f8fbff; padding:8px; border-radius:8px; border:1px solid #dfe7f1; color:#576a84; }
    .hint { font-size:13px; color:#62748b; }
  </style>
</head>
<body>
  <div class="box">
    <h1>Сравнение цен ещё не готово</h1>
    <p>Закупка <strong>№ {{ tender_id }}</strong>. Чтобы увидеть результат, программе нужно извлечь смету, найти рыночные цены и собрать страницу сравнения.</p>
    <p class="hint">
    {% if not has_svodka_for_tid %}Для этой закупки рыночные цены ещё не найдены. Нажмите первую кнопку ниже.{% else %}Рыночные цены уже найдены, осталось обновить страницу результата.{% endif %}
    </p>
    <p><a href="/">← На главную</a> · <a id="retryLink" href="#">Обновить страницу</a></p>
    <button type="button" class="btn" id="genOneBtn">Подготовить сравнение для этой закупки</button>
    <button type="button" class="btn" id="genBtn" style="background:#283247;border-color:#4a567e;">Подготовить сравнения для всех</button>
    <div id="panel" style="margin-top:16px;display:none;">
      <div class="merge-bar-wrap"><div id="bar" class="merge-bar-fill" style="width:0%"></div></div>
      <p id="pct" style="margin:8px 0 0;font-size:14px;">0%</p>
      <div class="logs" id="logs"></div>
    </div>
    <p id="idleLine" class="hint" style="margin-top:12px;"></p>
  </div>
  <script>
    const REQUESTED_TID = {{ tender_id|tojson }};
    document.getElementById("retryLink").addEventListener("click", function(e) {
      e.preventDefault();
      location.reload();
    });
    let prevRun = false;
    async function tick() {
      try {
        const r = await fetch("/api/merge-site-status");
        const m = await r.json();
        const run = !!m.running;
        const panel = document.getElementById("panel");
        const idle = document.getElementById("idleLine");
        if (run || (m.log_tail && m.log_tail.length)) panel.style.display = "block";
        const pct = typeof m.percent === "number" ? m.percent : 0;
        document.getElementById("bar").style.width = Math.min(100, Math.max(0, pct)) + "%";
        document.getElementById("pct").textContent = pct + "% · " + (m.done||0) + " / " + (m.total||0) + (m.current_tid ? " · " + m.current_tid : "");
        document.getElementById("logs").textContent = (m.log_tail||[]).join("\\n");
        if (!run && m.last_ended_at) idle.textContent = "Последняя обработка: " + m.last_ended_at + " — " + (m.last_summary||"");
        else if (run) idle.textContent = "";
        document.getElementById("genBtn").disabled = run;
        document.getElementById("genOneBtn").disabled = run;
        if (prevRun && !run && m.total > 0) {
          try {
            const chk = await fetch("/merge-report/" + encodeURIComponent(REQUESTED_TID) + "/?t=" + Date.now(), { method: "GET", cache: "no-store" });
            if (chk.ok) location.reload();
          } catch (e) {}
        }
        prevRun = run;
      } catch (e) {}
    }
    document.getElementById("genBtn").addEventListener("click", async function() {
      try {
        const r = await fetch("/api/generate-merge-site-all", { method: "POST", headers: { "Content-Type": "application/json" } });
        const d = await r.json();
        if (!d.ok) alert(d.message || "Ошибка");
      } catch (e) { alert("Сеть"); }
      tick();
    });
    document.getElementById("genOneBtn").addEventListener("click", async function() {
      try {
        const r = await fetch("/api/generate-merge-site-one", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ tender_id: REQUESTED_TID }),
        });
        const d = await r.json();
        if (!d.ok) alert(d.message || "Ошибка");
      } catch (e) { alert("Сеть"); }
      tick();
    });
    setInterval(tick, 1000);
    tick();
  </script>
</body>
</html>
"""


def _svodka_xlsx_tender_ids() -> list[str]:
    from autobot.merge_estimate_market import OUT_PREFIX

    if not REPORTS_DIR.is_dir():
        return []
    out: list[str] = []
    for p in REPORTS_DIR.glob(f"{OUT_PREFIX}*.xlsx"):
        tid = p.stem[len(OUT_PREFIX) :]
        if tid:
            out.append(tid)
    return sorted(out)


def _estimate_xlsx_tender_ids() -> list[str]:
    prefix = "ОТЧЕТ_ПО_СМЕТАМ_"
    if not REPORTS_DIR.is_dir():
        return []
    out: list[str] = []
    for p in REPORTS_DIR.glob(f"{prefix}*.xlsx"):
        if "ОБЩИЙ" in p.name:
            continue
        tid = p.stem[len(prefix) :]
        if tid:
            out.append(tid)
    return sorted(set(out))


def _compute_reports_coverage() -> dict[str, int]:
    """Тендеры из tenders.json vs готовые веб-сводки и Excel СВОДКА_РЫНОК."""
    merge_root = REPO_ROOT / "data" / "reports_site"
    from autobot.merge_estimate_market import OUT_PREFIX

    meta = load_tender_metadata()
    tender_ids = list(meta.keys())
    n_t = len(tender_ids)
    n_merge = sum(1 for tid in tender_ids if (merge_root / tid / "index.html").is_file())
    n_svodka = len(_svodka_xlsx_tender_ids())
    n_estimate = len(_estimate_xlsx_tender_ids())
    no_est = 0
    no_svodka = 0
    no_html = 0
    for tid in tender_ids:
        est_ok = (REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx").is_file()
        sv_ok = (REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx").is_file()
        html_ok = (merge_root / tid / "index.html").is_file()
        if not est_ok:
            no_est += 1
        if est_ok and not sv_ok:
            no_svodka += 1
        if sv_ok and not html_ok:
            no_html += 1
    return {
        "tender_count": n_t,
        "merge_html_among_tenders": n_merge,
        "tenders_missing_merge_html": max(0, n_t - n_merge),
        "svodka_xlsx_count": n_svodka,
        "estimate_xlsx_count": n_estimate,
        "missing_no_estimate": no_est,
        "missing_no_svodka": no_svodka,
        "missing_no_html": no_html,
    }


def _merge_site_busy() -> bool:
    with merge_site_lock:
        return bool(merge_site_state["running"])


def _missing_or_error_tender_ids() -> list[str]:
    """Только тендеры из tenders.json, где нет HTML-сводки или нет/битая цепочка до неё."""
    merge_root = REPO_ROOT / "data" / "reports_site"
    from autobot.merge_estimate_market import OUT_PREFIX

    out: list[str] = []
    for tid in sorted(load_tender_metadata().keys()):
        est_ok = (REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx").is_file()
        sv_ok = (REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx").is_file()
        html_ok = (merge_root / tid / "index.html").is_file()
        if (not html_ok) or (est_ok and not sv_ok):
            out.append(tid)
    return out


def _extract_tender_id(raw: str) -> str:
    """
    Извлекает regNumber / tender_id из ссылки zakupki.gov.ru или прямого ввода.
    Поддерживает:
    - полный URL с regNumber=...
    - номер закупки 223-ФЗ (11 цифр) или 44-ФЗ (19 цифр)
    - любой текст, где встречается такой номер
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if s.isdigit() and len(s) in {11, 19}:
        return s
    try:
        p = urlparse(s)
        q = parse_qs(p.query)
        for key in ("regNumber", "purchaseNoticeNumber"):
            if key not in q or not q[key]:
                continue
            cand = (q[key][0] or "").strip()
            if cand.isdigit() and len(cand) in {11, 19}:
                return cand
    except Exception:
        pass
    import re

    m = re.search(r"(?<!\d)(\d{19}|\d{11})(?!\d)", s)
    if m:
        return m.group(1)
    return ""


def _truthy_env(name: str, default: str = "0") -> bool:
    v = (os.environ.get(name, default) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _run_market_for_tender(
    tid: str,
    *,
    force_no_resume: bool = False,
    max_rows_override: int | None = None,
    rerun_selected: bool = False,
    sources_override: str | None = None,
    only_without_verified: bool = False,
    avito_collect_only: bool = False,
) -> tuple[int, str]:
    rows_map = _estimate_rows_by_tender_id()
    max_rows_arg: str | None = None
    max_rows_raw = str(max_rows_override or "").strip()
    if not max_rows_raw:
        max_rows_raw = (os.environ.get("MARKET_MAX_ROWS") or os.environ.get("MARKET_MAX_ROWS") or "").strip()
    if max_rows_raw:
        try:
            cap_rows = int(max_rows_raw)
        except ValueError:
            cap_rows = 0
        if cap_rows > 0:
            cap_rows = min(cap_rows, 5000)
            est_n = int(rows_map.get(tid, 0) or 0)
            use = cap_rows if est_n <= 0 else min(est_n, cap_rows)
            max_rows_arg = str(max(1, use))
    pause = "0" if avito_collect_only else ((os.environ.get("MARKET_PAUSE_SEC") or os.environ.get("MARKET_PAUSE_SEC") or "4").strip() or "4")
    sources = (sources_override or os.environ.get("MARKET_SOURCES") or "web").strip() or "web"
    max_results = "3" if avito_collect_only else ((os.environ.get("MARKET_MAX_RESULTS") or "5").strip() or "5")
    cmd = [
        sys.executable,
        str(_TOOLS_RUN_MODULE),
        "autobot.real_market_scraper",
        "--tender-id",
        tid,
        "--pause",
        pause,
        "--sources",
        sources,
        "--max-results-per-row",
        max_results,
    ]
    if max_rows_arg:
        cmd.extend(["--max-rows", max_rows_arg])
    if force_no_resume or _truthy_env("MARKET_NO_RESUME") or _truthy_env("MARKET_NO_RESUME"):
        cmd.append("--no-resume")
    elif rerun_selected:
        cmd.append("--rerun-selected")
    if only_without_verified:
        cmd.append("--only-without-verified")
    if avito_collect_only:
        cmd.extend(["--avito-collect-only", "--avito-safe-interval-sec", "35"])
    timeout_raw = (os.environ.get("MARKET_TIMEOUT_SEC") or os.environ.get("MARKET_TIMEOUT_SEC") or "21600").strip() or "21600"
    try:
        timeout_sec = max(60, int(timeout_raw))
    except ValueError:
        timeout_sec = 21600
    try:
        r = subprocess.run(cmd, cwd=str(REPO_ROOT), timeout=timeout_sec)
        return int(r.returncode), " ".join(cmd)
    except subprocess.TimeoutExpired:
        return 124, " ".join(cmd) + f" [timeout={timeout_sec}s]"


def _run_main_fetch_for_tender(tid: str, tender_url: str) -> tuple[int, str, str]:
    cmd = [
        sys.executable,
        str(_TOOLS_RUN_MODULE),
        "autobot.main",
        "--from-tender-id",
        tid,
        "--from-tender-url",
        tender_url,
    ]
    r = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, errors="replace")
    out = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
    return int(r.returncode), " ".join(cmd), out.strip()


def _main_failure_reason(output: str) -> str:
    """Короткая причина из вывода main.py для Telegram."""
    if not output:
        return "main.py не вернул подробный вывод."
    lines = [x.strip() for x in str(output).splitlines() if x.strip()]
    if not lines:
        return "main.py не вернул подробный вывод."
    hints = [
        "Документы не скачались",
        "не скачались",
        "Не удалось",
        "RAR",
        "excel",
        "xlsx",
        "Error",
        "Traceback",
        "Timeout",
        "captcha",
        "403",
        "404",
    ]
    picked: list[str] = []
    for line in lines:
        if any(h.lower() in line.lower() for h in hints):
            picked.append(line)
    tail = picked[-2:] if picked else lines[-2:]
    txt = " | ".join(tail)
    if len(txt) > 320:
        txt = txt[:317] + "..."
    return txt


def _estimate_diagnostics(tid: str, *, report_exists: bool, report_rows: int | None = None) -> str:
    """
    Короткая диагностика, почему не получилась смета/ЛСР для тендера.
    Возвращает строку для лога/Telegram (1-2 фразы).
    """
    tid_s = str(tid or "").strip()
    dl_dir = REPO_ROOT / "data" / "downloads" / tid_s
    ex_dir = REPO_ROOT / "data" / "extracted" / tid_s

    dl_files = [p for p in dl_dir.rglob("*") if p.is_file()] if dl_dir.is_dir() else []
    ex_files = [p for p in ex_dir.rglob("*") if p.is_file()] if ex_dir.is_dir() else []
    all_files = dl_files + ex_files

    excel_cnt = sum(1 for p in all_files if p.suffix.lower() in (".xlsx", ".xls"))
    rar_cnt = sum(1 for p in all_files if p.suffix.lower() == ".rar")
    zip_cnt = sum(1 for p in all_files if p.suffix.lower() == ".zip")

    ok_dl = 0
    failed_dl = 0
    log_path = dl_dir / "download_log.json"
    if log_path.is_file():
        try:
            payload = json.loads(log_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                for row in payload:
                    st = str((row or {}).get("status") or "").strip().lower()
                    if st == "ok":
                        ok_dl += 1
                    elif st:
                        failed_dl += 1
        except Exception:
            pass

    if not dl_dir.is_dir():
        return "нет папки downloads по тендеру (документы не скачаны)."
    if ok_dl == 0 and failed_dl > 0:
        return f"скачивание документов неуспешно: ok=0, failed={failed_dl}."
    if ok_dl == 0 and not dl_files:
        return "скачивание не дало файлов (возможны капча/403/блокировка)."

    if not report_exists:
        if excel_cnt == 0:
            if rar_cnt > 0:
                return (
                    f"отчёт сметы не создан: Excel не найден, хотя есть архивы "
                    f"(RAR={rar_cnt}, ZIP={zip_cnt}); вероятно, не распаковано."
                )
            return "отчёт сметы не создан: среди скачанных файлов нет Excel."
        return f"отчёт сметы не создан при наличии Excel (найдено {excel_cnt})."

    if report_rows is not None and report_rows <= 0:
        if excel_cnt == 0:
            return "смета пустая (0 позиций): Excel-источники не обнаружены."
        if rar_cnt > 0 and ex_dir.is_dir() and not any(p.suffix.lower() in (".xlsx", ".xls") for p in ex_files):
            return (
                f"смета пустая (0 позиций): есть RAR={rar_cnt}, но из extracted нет Excel; "
                "проверьте 7-Zip/UnRAR."
            )
        return (
            f"смета пустая (0 позиций): Excel найдено {excel_cnt}, "
            "но структура ЛСР в них не распознана (возможен нестандартный формат)."
        )

    return "диагностика: смета и ЛСР найдены."


def _run_merge_site_all_worker(
    *,
    only_missing: bool = False,
    ids_override: list[str] | None = None,
    tender_url_by_id: dict[str, str] | None = None,
    force_market_no_resume: bool = False,
    market_max_rows: int | None = None,
    market_rerun_selected: bool = False,
    market_sources_override: str | None = None,
    market_only_without_verified: bool = False,
    market_avito_collect_only: bool = False,
) -> None:
    errors: list[str] = []
    ok_html = 0
    ok_full = 0
    ids: list[str] = []
    reason_counts = {"no_estimate": 0, "market_failed": 0, "merge_failed": 0, "html_failed": 0}
    try:
        from autobot.merge_estimate_market import merge_estimate_and_market
        from autobot.report_merge_html import write_tender_report_site

        if ids_override is not None:
            ids = [x for x in ids_override if str(x).strip()]
        else:
            ids = _missing_or_error_tender_ids() if only_missing else _estimate_xlsx_tender_ids()
        cap = 250
        with merge_site_lock:
            merge_site_state["running"] = True
            merge_site_state["total"] = len(ids)
            merge_site_state["done"] = 0
            merge_site_state["current_tid"] = ""
            merge_site_state["market_done"] = 0
            merge_site_state["market_total"] = 0
            merge_site_state["last_market_chat_done"] = 0
            merge_site_state["started_at"] = datetime.now().isoformat(timespec="seconds")
            merge_site_state["ended_at"] = None
            merge_site_state["error_ids"] = []
            merge_site_state["chat_events"] = []
            mode = (
                f"Авито: до {market_max_rows or 10} позиций без подтверждённой цены"
                if market_avito_collect_only else (
                    f"контрольная выборка по {market_max_rows} позиций"
                    if market_max_rows else ("только отсутствующие/ошибки" if only_missing else "все сметы")
                )
            )
            merge_site_state["log_lines"] = [f"Режим: {mode}. К обработке: {len(ids)}"]
            if not ids:
                merge_site_state["log_lines"].append(
                    "Нечего обрабатывать."
                )
                merge_site_state["running"] = False
                merge_site_state["ended_at"] = datetime.now().isoformat(timespec="seconds")
                merge_site_state["last_ended_at"] = merge_site_state["ended_at"]
                merge_site_state["last_summary"] = "Нет смет для обработки"
                merge_site_state["last_reason_counts"] = reason_counts
                return

        _merge_chat_add("start", f"Старт подготовки сравнений. Режим: {mode}. К обработке: {len(ids)}")
        for i, tid in enumerate(ids):
            pref = f"📊 <b>{i + 1}/{len(ids)}</b> · <code>{tid}</code>"
            with merge_site_lock:
                merge_site_state["current_tid"] = tid
                merge_site_state["market_done"] = 0
                merge_site_state["market_total"] = 0
                merge_site_state["last_market_chat_done"] = 0
                merge_site_state["log_lines"].append(f"[{i + 1}/{len(ids)}] {tid}…")
                merge_site_state["log_lines"] = merge_site_state["log_lines"][-cap:]
            _merge_chat_add("tender", f"📊 {i + 1}/{len(ids)} · тендер {tid}: старт", tender_id=tid)
            _tg_send(f"{pref}\n🟡 Старт")
            try:
                est_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
                cout = ""
                explicit_turl = ""
                if tender_url_by_id and tid in tender_url_by_id:
                    explicit_turl = (tender_url_by_id.get(tid) or "").strip()
                if not est_path.is_file() or explicit_turl:
                    turl = explicit_turl
                    if not turl:
                        # Пробуем URL из metadata для автодозагрузки.
                        md = load_tender_metadata().get(tid, {})
                        turl = str(md.get("url") or "").strip()
                    if turl:
                        _tg_send(f"{pref}\n🟡 Скачиваю смету…")
                        c, ccmd, cout = _run_main_fetch_for_tender(tid, turl)
                        with merge_site_lock:
                            merge_site_state["log_lines"].append(f"  main: {ccmd}")
                            merge_site_state["log_lines"].append(f"  main code: {c}")
                            if cout:
                                for line in cout.splitlines()[-8:]:
                                    merge_site_state["log_lines"].append("    " + line[:300])
                        if c != 0:
                            reason = _main_failure_reason(cout)
                            diag = _estimate_diagnostics(tid, report_exists=False)
                            _tg_send(
                                f"{pref}\n⚠️ main.py код <code>{c}</code>\n"
                                f"<code>{html_mod.escape(reason)}</code>\n<code>{html_mod.escape(diag)}</code>"
                            )
                        est_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
                    if not est_path.is_file():
                        reason_counts["no_estimate"] += 1
                        with merge_site_lock:
                            merge_site_state["log_lines"].append("  → пропуск: нет ОТЧЕТ_ПО_СМЕТАМ")
                        reason = _main_failure_reason(cout)
                        diag = _estimate_diagnostics(tid, report_exists=False)
                        _tg_send(
                            f"{pref}\n⚠️ Нет <code>ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx</code>\n"
                            f"<code>{html_mod.escape(reason)}</code>\n<code>{html_mod.escape(diag)}</code>\n"
                            "Если ссылка уже была, обычно это временная недоступность ЕИС (таймаут/капча/блокировка); повторите позже."
                        )
                        errors.append(tid)
                        continue
                try:
                    _cnt = int(len(pd.read_excel(est_path, usecols=[0])))
                except Exception:
                    _cnt = 0
                if _cnt > 0:
                    _merge_chat_add("estimate", f"Смета: {_cnt} позиций. Запускаю поиск рынка…", tender_id=tid)
                    _tg_send(f"{pref}\n🟡 Смета: <b>{_cnt}</b> поз.")
                    _tg_flush_spool()
                    # Старт поиска — до тяжёлых проверок и до subprocess, иначе при spool сообщение
                    # может уехать в конец и появиться после всех строк прогресса.
                    _tg_send(f"{pref}\n🟡 Поиск рынка…")
                    _tg_flush_spool()
                else:
                    reason_counts["no_estimate"] += 1
                    diag = _estimate_diagnostics(tid, report_exists=True, report_rows=_cnt)
                    with merge_site_lock:
                        merge_site_state["log_lines"].append(f"  → пропуск: смета пустая (0 позиций) [{diag}]")
                    _tg_send(
                        f"{pref}\n⚠️ Смета пустая (0 поз.)\n<code>{html_mod.escape(diag)}</code>"
                    )
                    errors.append(tid)
                    continue

                if market_max_rows:
                    done_before, total_works = 0, min(int(market_max_rows), _cnt)
                else:
                    done_before, total_works = _market_progress_for_tender(tid)
                rem_before = max(0, total_works - done_before)
                with merge_site_lock:
                    merge_site_state["market_done"] = int(done_before)
                    merge_site_state["market_total"] = int(total_works)
                if total_works > 0:
                    if rem_before > 0:
                        _merge_chat_add("market", f"Рынок: обработано строк сметы {done_before}/{total_works}, осталось {rem_before}", tender_id=tid, seq=done_before, total=total_works)
                        _tg_send(f"{pref}\n🟡 Рынок: обработано строк сметы <b>{done_before}/{total_works}</b>…")
                    else:
                        _merge_chat_add("market", f"Рынок уже обработал строки сметы: {done_before}/{total_works}", tender_id=tid, seq=done_before, total=total_works)
                        _tg_send(f"{pref}\n🟢 Рынок уже обработал строки сметы: <b>{done_before}/{total_works}</b>")
                    _tg_flush_spool()
                _tg_flush_spool()
                market_code, market_cmd = _run_market_for_tender(
                    tid,
                    force_no_resume=force_market_no_resume,
                    max_rows_override=market_max_rows,
                    rerun_selected=market_rerun_selected,
                    sources_override=market_sources_override,
                    only_without_verified=market_only_without_verified,
                    avito_collect_only=market_avito_collect_only,
                )
                with merge_site_lock:
                    merge_site_state["log_lines"].append(f"  market: {market_cmd}")
                if market_code != 0:
                    reason_counts["market_failed"] += 1
                    with merge_site_lock:
                        if market_code == 124:
                            merge_site_state["log_lines"].append("  → поиск рынка завис и остановлен по таймауту")
                        else:
                            merge_site_state["log_lines"].append(f"  → поиск рынка код {market_code}")
                    if market_code == 124:
                        _merge_chat_add("error", "⚠️ Поиск рынка завис и остановлен по таймауту", tender_id=tid)
                        _tg_send(f"{pref}\n⚠️ Поиск рынка завис и остановлен по таймауту")
                    else:
                        _merge_chat_add("error", f"⚠️ Поиск рынка завершился с кодом {market_code}", tender_id=tid)
                        _tg_send(f"{pref}\n⚠️ Рынок код <code>{market_code}</code>")
                    errors.append(tid)
                    continue

                if market_max_rows:
                    done_after, total_after = min(int(market_max_rows), _cnt), min(int(market_max_rows), _cnt)
                else:
                    done_after, total_after = _market_progress_for_tender(tid)
                rem_after = max(0, total_after - done_after)
                with merge_site_lock:
                    merge_site_state["market_done"] = int(done_after)
                    merge_site_state["market_total"] = int(total_after)
                if total_after > 0:
                    if rem_after > 0:
                        _merge_chat_add("market", f"Рынок: обработано строк сметы {done_after}/{total_after}, осталось {rem_after}", tender_id=tid, seq=done_after, total=total_after)
                        _tg_send(f"{pref}\n🟡 Рынок: обработано строк сметы <b>{done_after}/{total_after}</b>, осталось <b>{rem_after}</b>")
                    else:
                        _merge_chat_add("market", f"🟢 Рынок обработал строки сметы: {done_after}/{total_after}", tender_id=tid, seq=done_after, total=total_after)
                        _tg_send(f"{pref}\n🟢 Рынок: строки сметы обработаны <b>{done_after}/{total_after}</b>")

                out = merge_estimate_and_market(tid)
                _merge_chat_add("merge", "Собираю СВОДКА_РЫНОК и страницу сравнения…", tender_id=tid)
                _tg_send(f"{pref}\n🟡 Merge…")
                if not out or not out.is_file():
                    reason_counts["merge_failed"] += 1
                    _merge_chat_add("error", f"⚠️ Не собрался СВОДКА_РЫНОК_{tid}.xlsx", tender_id=tid)
                    with merge_site_lock:
                        merge_site_state["log_lines"].append("  → merge не собрал СВОДКА_РЫНОК")
                    _tg_send(f"{pref}\n⚠️ Нет <code>СВОДКА_РЫНОК_{tid}.xlsx</code>")
                    errors.append(tid)
                    continue
                p = write_tender_report_site(tid)
                if p and p.is_file():
                    ok_html += 1
                    ok_full += 1
                    _merge_chat_add("done", f"✅ Тендер {tid}: сравнение готово", tender_id=tid)
                    site_url = get_report_site_public_base()
                    link = f"{site_url}/tenders/{tid}" if site_url else ""
                    with merge_site_lock:
                        merge_site_state["log_lines"].append("  → OK (рынок + merge + HTML)")
                    if link:
                        safe_link = html_mod.escape(link, quote=True)
                        _tg_send(
                            f"{pref}\n✅ Готово\n🔗 <a href=\"{safe_link}\">Отчёт</a>"
                        )
                    else:
                        _tg_send(
                            f"{pref}\n✅ Готово\n<code>data/reports_site/{tid}/index.html</code>"
                        )
                    try:
                        from autobot.tender_viability import format_viability_for_telegram

                        vmsg = format_viability_for_telegram(tid)
                        if vmsg:
                            _tg_flush_spool()
                            _tg_send(vmsg)
                            _tg_flush_spool()
                    except Exception:
                        pass
                else:
                    reason_counts["html_failed"] += 1
                    with merge_site_lock:
                        merge_site_state["log_lines"].append("  → HTML не создан")
                    _tg_send(f"{pref}\n⚠️ HTML не создан")
                    errors.append(tid)
            except Exception as e:
                reason_counts["merge_failed"] += 1
                with merge_site_lock:
                    merge_site_state["log_lines"].append(f"  → ошибка: {e}")
                _tg_send(f"{pref}\n⚠️ Ошибка: <code>{html_mod.escape(str(e)[:280])}</code>")
                errors.append(tid)
            with merge_site_lock:
                merge_site_state["done"] = i + 1
                merge_site_state["market_done"] = 0
                merge_site_state["market_total"] = 0
                merge_site_state["last_market_chat_done"] = 0
                merge_site_state["log_lines"] = merge_site_state["log_lines"][-cap:]

        ended = datetime.now().isoformat(timespec="seconds")
        with merge_site_lock:
            merge_site_state["done"] = len(ids)
            merge_site_state["current_tid"] = ""
            merge_site_state["market_done"] = 0
            merge_site_state["market_total"] = 0
            merge_site_state["last_market_chat_done"] = 0
            merge_site_state["running"] = False
            merge_site_state["ended_at"] = ended
            merge_site_state["error_ids"] = errors
            merge_site_state["last_ended_at"] = ended
            merge_site_state["last_summary"] = (
                f"Готово сравнений: {ok_full} из {len(ids)} "
                f"(не удалось обработать: {len(errors)})"
            )
            merge_site_state["last_reason_counts"] = reason_counts
            merge_site_state["log_lines"].append("--- Готово. Можно обновить страницу тендера. ---")
            merge_site_state["log_lines"] = merge_site_state["log_lines"][-cap:]
    except Exception as e:
        t = datetime.now().isoformat(timespec="seconds")
        with merge_site_lock:
            merge_site_state.setdefault("log_lines", []).append(f"Критическая ошибка: {e}")
            merge_site_state["last_summary"] = str(e)[:240]
            merge_site_state["last_ended_at"] = t
            merge_site_state["ended_at"] = t
            merge_site_state["last_reason_counts"] = reason_counts
    finally:
        with merge_site_lock:
            if merge_site_state["running"]:
                merge_site_state["running"] = False
                merge_site_state["current_tid"] = ""
                merge_site_state["market_done"] = 0
                merge_site_state["market_total"] = 0
                merge_site_state["last_market_chat_done"] = 0
                t = datetime.now().isoformat(timespec="seconds")
                merge_site_state["ended_at"] = t
                if not merge_site_state.get("last_ended_at"):
                    merge_site_state["last_ended_at"] = t
                merge_site_state.setdefault("last_summary", "Прервано / ошибка")


@app.route("/merge-report/<tender_id>/")
@app.route("/merge-report/<tender_id>/index.html")
def merge_report_site(tender_id: str):
    """Сводка смета + рынок (report_merge_html → data/reports_site/<id>/index.html)."""
    tid = (tender_id or "").strip()
    if not tid or "/" in tid or ".." in tid:
        abort(404)
    folder = MERGE_REPORTS_SITE_DIR / tid
    target = folder / "index.html"
    from autobot.merge_estimate_market import OUT_PREFIX

    svodka = REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx"
    # Всегда пересобираем HTML из сводки — иначе остаётся старый index.html без карточек/скриптов.
    try:
        from autobot.report_merge_html import write_tender_report_site

        out_path = write_tender_report_site(tid)
        print(f"[merge-report] HTML attempt tender={tid} -> {out_path}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[merge-report] HTML FAILED tender={tid}: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
    if target.is_file():
        resp = make_response(send_from_directory(folder, "index.html"))
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

    n = len(_svodka_xlsx_tender_ids())
    has_svodka = svodka.is_file()
    return render_template_string(
        MISSING_MERGE_PAGE,
        tender_id=tid,
        svodka_count=n,
        has_svodka_for_tid=has_svodka,
    )


@app.route("/nmck-preview/<preview_id>/")
def nmck_preview_table(preview_id: str):
    """Таблица из последнего разбора Excel обоснования НМЦК (payload в data/nmck_previews/)."""
    pid = (preview_id or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{32}", pid):
        abort(404)
    path = NMCK_PREVIEW_DIR / pid / "payload.json"
    if not path.is_file():
        abort(404)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        abort(404)
    columns = data.get("columns") or []
    rows = data.get("rows") or []
    meta = data.get("meta") or {}
    title = (meta.get("filename") or "Обоснование НМЦК").strip() or "Обоснование НМЦК"
    subtitle = (
        f"{meta.get('row_count', '?')} поз. · лист «{meta.get('sheet', '')}» · "
        f"{meta.get('column_count', '?')} колонок"
    )
    resp = make_response(
        render_template_string(
            NMCK_PREVIEW_PAGE,
            title=title,
            subtitle=subtitle,
            columns=columns,
            rows=rows,
        )
    )
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


def _parse_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _cmd_display(cmd: list[str]) -> str:
    return " ".join(repr(x) if any(c in x for c in " \t\"") else x for x in cmd)


def _main_job_path():
    return DATA_DIR / 'main_jobs.sqlite3'


def _remember_main_job(row):
    from autobot.main_jobs import public_status
    payload = public_status(row)
    if payload:
        parse_state.update({key: value for key, value in payload.items() if key not in ('log_tail', 'log_lines_count')})
        parse_state['log_lines'] = list(row['logs'])


def _main_job_busy():
    """Legacy consumers must see the durable reservation after a web restart."""
    from autobot import main_jobs as jobs
    try:
        jobs.recover_interrupted(_main_job_path())
        row = jobs.latest(_main_job_path())
    except (OSError, ValueError, sqlite3.Error, TimeoutError):
        # Do not delete inputs or start another workflow while state is unknown.
        return True
    with parse_lock:
        if row:
            _remember_main_job(row)
        return bool(parse_state.get('running'))


def _run_main_worker(cli_args: list[str], task: str, run_id: str | None = None, tender_id: str | None = None) -> None:
    from autobot import main_jobs as jobs
    from autobot.main_job_runtime import launch
    path = _main_job_path()
    if run_id is None:
        row, _ = jobs.enqueue(path, {'kind': 'main', 'argv': cli_args}, task, tender_id)
        run_id = row['run_id']
    try:
        launch(path, run_id, env=_parse_env())
    finally:
        row = jobs.get(path, run_id)
        with parse_lock:
            if row and parse_state.get('run_id') == run_id:
                _remember_main_job(row)


def _admit_main_job(plan, task, tender_id=None, success_code=200):
    from autobot import main_jobs as jobs
    data = request.get_json(silent=True)
    operation = data.get('operation_id') if isinstance(data, dict) else None
    if operation is not None:
        import uuid
        try:
            operation = uuid.UUID(operation).hex
        except (ValueError, TypeError, AttributeError):
            return jsonify({'ok': False, 'message': 'Некорректный номер операции.'}), 400
    path = _main_job_path()
    try:
        jobs.recover_interrupted(path)
        with parse_lock:
            if parse_state.get('running') and operation != parse_state.get('run_id'):
                current = jobs.latest(path)
                if current is None:
                    return jsonify({'ok': False, 'message': 'Сейчас выполняется другая работа с документами.'}), 409
            row, duplicate = jobs.enqueue(path, plan, task, tender_id, run_id=operation)
            _remember_main_job(jobs.latest(path) if duplicate else row)
    except (jobs.JobBusy, jobs.JobConflict) as error:
        return jsonify({'ok': False, 'message': str(error)}), 409
    except ValueError as error:
        return jsonify({'ok': False, 'message': str(error)}), 400
    except (OSError, sqlite3.Error, TimeoutError):
        return jsonify({'ok': False, 'message': 'Не удалось сохранить задание. Исполнитель не запущен; повторите попытку.'}), 503
    if not duplicate:
        try:
            threading.Thread(target=_run_main_worker, kwargs={'cli_args': row['plan'].get('argv', []),
                'task': task, 'run_id': row['run_id'], 'tender_id': tender_id}, daemon=True).start()
        except RuntimeError:
            jobs.launch_failed(path, row['run_id'])
            with parse_lock:
                _remember_main_job(jobs.get(path, row['run_id']))
            return jsonify({'ok': False, 'message': 'Не удалось запустить исполнителя. Повторите попытку.'}), 500
    return jsonify({'ok': True, 'tender_id': tender_id, 'run_id': row['run_id'], 'duplicate': duplicate}), success_code


def _nmck_upload_allowed(filename: str) -> bool:
    fn = (filename or "").lower().strip()
    return fn.endswith((".xlsx", ".xls", ".xlsm"))


@app.route("/api/parse-nmck-justification", methods=["POST"])
def api_parse_nmck_justification():
    """Excel «Обоснование НМЦК» (приложение №2) → JSON (позиции и все колонки таблицы)."""
    f = request.files.get("file")
    if not f or not getattr(f, "filename", None):
        return jsonify({"ok": False, "message": "Выберите файл в поле «Обоснование НМЦК»."}), 400
    if not _nmck_upload_allowed(f.filename):
        return jsonify({"ok": False, "message": "Нужен файл Excel: .xlsx, .xls или .xlsm."}), 400
    try:
        raw = f.read()
    except Exception as e:
        return jsonify({"ok": False, "message": f"Не удалось прочитать файл: {e}"}), 400
    if not raw:
        return jsonify({"ok": False, "message": "Пустой файл."}), 400
    try:
        from autobot.nmck_justification_parse import parse_nmck_justification_excel

        out = parse_nmck_justification_excel(raw, original_name=f.filename)
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "message": f"Ошибка разбора Excel: {e}"}), 400
    preview_id = uuid.uuid4().hex
    folder = NMCK_PREVIEW_DIR / preview_id
    try:
        folder.mkdir(parents=True, exist_ok=True)
        payload = {"columns": out["columns"], "rows": out["rows"], "meta": out["meta"]}
        (folder / "payload.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        return jsonify({"ok": False, "message": f"Не удалось сохранить превью: {e}"}), 500
    out["preview_id"] = preview_id
    out["preview_url"] = f"/nmck-preview/{preview_id}/"
    return jsonify({"ok": True, **out})


@app.get('/tenders/search-profiles.js')
def search_profiles_script():
    # This existing protected page prefix is routed to AutoBot by the CRM proxy.
    return app.send_static_file('tender_search_profiles.js')


@app.route('/api/tender-search-profiles', methods=['GET', 'POST'])
@app.route('/api/search-profiles', methods=['GET', 'POST'])
def api_search_profiles():
    from autobot.tender_search_profiles import load_profiles, save_profile, ProfileConflict
    try:
        if request.method == 'POST':
            if request.content_length and request.content_length > 16384:
                return jsonify({'ok': False, 'message': 'Слишком большой профиль поиска.'}), 413
            result = save_profile(DATA_DIR, request.get_json(silent=True))
        else:
            result = load_profiles(DATA_DIR)
        return jsonify({'ok': True, **result})
    except ProfileConflict as error:
        return jsonify({'ok': False, 'message': str(error)}), 409
    except ValueError as error:
        return jsonify({'ok': False, 'message': str(error)}), 400
    except (OSError, TimeoutError):
        return jsonify({'ok': False, 'message': 'Не удалось сохранить или прочитать профили. Повторите попытку.'}), 503


@app.route('/api/tender-search/start', methods=['POST'])
@app.route("/api/start-parse", methods=["POST"])
def api_start_parse():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания подготовки сравнений цен."}), 409
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'message': 'Ожидается JSON-объект'}), 400
    mode = data.get('search_mode', 'fresh')
    if mode not in ('fresh', 'resume'):
        return jsonify({'ok': False, 'message': 'search_mode: fresh или resume'}), 400
    if mode == 'resume':
        from autobot import tender_search_state as search_state
        resume = search_state.public_resume(DATA_DIR)
        if not resume['available']:
            return jsonify({'ok': False, 'message': resume['reason']}), 409
        data = dict(resume['parameters'], catalog_only=False)
        if 'search_filters' in resume:
            data['search_filters'] = resume['search_filters']
    filters = None
    if 'search_filters' in data or 'search_profile' in data:
        from autobot.tender_search_profiles import validate_filters, filters_for_profile
        try:
            if 'search_filters' in data and 'search_profile' in data:
                raise ValueError('Укажите профиль или снимок условий, а не оба сразу.')
            filters = (validate_filters(data['search_filters']) if 'search_filters' in data else
                       filters_for_profile(DATA_DIR, data['search_profile']))
        except ValueError as error:
            return jsonify({'ok': False, 'message': str(error)}), 400
        data = dict(data, **{key: filters[key] for key in ('max_pages', 'max_tenders', 'days_back')})
    try:
        max_pages = int(data.get("max_pages", 2))
        max_tenders = int(data.get("max_tenders", 15))
        days_back = int(data.get("days_back", 60))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Некорректные числа (max_pages, max_tenders, days_back)"}), 400
    max_pages = max(1, min(max_pages, 20))
    max_tenders = max(1, min(max_tenders, 100))
    days_back = max(1, min(days_back, 365))
    args = [
        "--max-pages",
        str(max_pages),
        "--max-tenders",
        str(max_tenders),
        "--days-back",
        str(days_back),
    ]
    catalog_only_raw = data.get("catalog_only", True)
    catalog_only = catalog_only_raw not in (False, 0, "0", "false", "False", "no", "off")
    if catalog_only:
        args.append('--catalog-only')
    if filters is not None:
        args.extend(['--search-filters-json', json.dumps(filters, ensure_ascii=False)])
    if mode == 'resume':
        from argparse import Namespace
        from autobot.main import _checkpoint_signature
        expected = _checkpoint_signature(Namespace(max_pages=max_pages, max_tenders=max_tenders, days_back=days_back, catalog_only=False, search_filters=filters))
        try:
            search_state.checkpoint_for_resume(DATA_DIR / 'search_resume_checkpoint.json', signature=expected)
        except ValueError as error:
            return jsonify({'ok': False, 'message': str(error)}), 409
        args.append('--resume-downloads')
    if filters is None and mode == 'fresh':
        from autobot.tender_search_profiles import default_filters
        snapshot = dict(default_filters(), max_pages=max_pages, max_tenders=max_tenders, days_back=days_back)
        args.extend(['--search-filters-json', json.dumps(snapshot, ensure_ascii=False)])
    task = 'продолжение скачивания документов' if mode == 'resume' else 'поиск закупок для каталога' if catalog_only else 'поиск новых закупок'
    return _admit_main_job({'kind': 'main', 'argv': args}, task)


@app.post('/api/tenders/<tender_id>/refresh-documents')
def api_refresh_tender_documents(tender_id):
    if not re.fullmatch(r'\d{8,25}', tender_id):
        return jsonify({'ok': False, 'message': 'Некорректный номер тендера.'}), 400
    metadata = load_tender_metadata().get(tender_id)
    if metadata is None:
        return jsonify({'ok': False, 'message': 'Тендер не найден.'}), 404
    url = eis_notice_url(tender_id, metadata.get('url'))
    from urllib.parse import urlparse
    address = urlparse(url)
    if address.scheme != 'https' or not address.hostname or not (address.hostname == 'zakupki.gov.ru' or address.hostname.endswith('.zakupki.gov.ru')) or address.username or address.password:
        return jsonify({'ok': False, 'message': 'В карточке нет корректной HTTPS-ссылки на ЕИС.'}), 400
    task = 'скачивание документов и разбор сметы ' + tender_id
    return _start_document_job(tender_id, ['--from-tender-id', tender_id, '--from-tender-url', url], task, 202)


def _start_document_job(tender_id, cli_args, task, success_code=200):
    if _merge_site_busy():
        return jsonify({'ok': False, 'message': 'Дождитесь завершения текущего сравнения цен.'}), 409
    return _admit_main_job({'kind': 'main', 'argv': cli_args}, task, tender_id, success_code)


@app.route("/api/reports/rebuild", methods=["POST"])
@app.route("/api/rebuild-report", methods=["POST"])
def api_rebuild_report():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'message': 'Некорректный запрос.'}), 400
    tid = str(data.get("tender_id", "")).strip()
    if not re.fullmatch(r'\d{8,25}', tid):
        return jsonify({"ok": False, "message": "Укажите корректный tender_id"}), 400
    if tid not in load_tender_metadata():
        return jsonify({'ok': False, 'message': 'Тендер не найден.'}), 404
    if not _AUTOBOT_MAIN_FILE.is_file():
        return jsonify({"ok": False, "message": f"Не найден {_AUTOBOT_MAIN_FILE}"}), 500
    return _start_document_job(tid, ['--from-downloaded-tender-id', tid], f'повторное извлечение сметы {tid}')


@app.route("/api/reports/rebuild-all", methods=["POST"])
@app.route("/api/rebuild-all-reports", methods=["POST"])
def api_rebuild_all_reports():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания подготовки сравнений цен."}), 409
    if not _AUTOBOT_MAIN_FILE.is_file():
        return jsonify({"ok": False, "message": f"Не найден {_AUTOBOT_MAIN_FILE}"}), 500
    if not TENDERS_JSON.is_file():
        return jsonify({"ok": False, "message": "Нет файла tenders.json"}), 400
    meta = load_tender_metadata()
    if not meta:
        return jsonify({"ok": False, "message": "В tenders.json нет тендеров"}), 400
    response, status = _admit_main_job({'kind': 'batch', 'tender_ids': sorted(meta)}, 'пересбор всех отчётов')
    if status == 200:
        response = jsonify(dict(response.get_json(), count=len(meta)))
    return response, status


@app.route("/api/parse-status")
def api_parse_status():
    with parse_lock:
        payload = {
            "running": parse_state["running"],
            "task": parse_state["task"],
            "run_id": parse_state.get('run_id'),
            "tender_id": parse_state.get('tender_id'),
            "command": parse_state["command"],
            "started_at": parse_state["started_at"],
            "ended_at": parse_state["ended_at"],
            "exit_code": parse_state["exit_code"],
            "log_lines_count": len(parse_state["log_lines"]),
            "log_tail": parse_state["log_lines"][-80:],
        }
    from autobot import main_jobs as jobs
    try:
        requested_run = request.args.get('run_id')
        if requested_run and not re.fullmatch(r'[0-9a-f]{32}', requested_run):
            return jsonify({'ok': False, 'message': 'Некорректный номер запуска.'}), 400
        selected_tender = request.args.get('tender_id', '')
        row = jobs.get(_main_job_path(), requested_run) if requested_run else jobs.latest(_main_job_path())
        if not requested_run and re.fullmatch(r'[0-9]{8,25}', selected_tender):
            row = jobs.latest_for_tender(_main_job_path(), selected_tender) or row
        if requested_run and row is None:
            return jsonify({'ok': False, 'message': 'Сохранённое задание не найдено.'}), 404
        if row and row['status'] == 'running' and jobs.recover_interrupted(_main_job_path()):
            row = jobs.get(_main_job_path(), row['run_id'])
        if row:
            payload.update(jobs.public_status(row))
    except (OSError, ValueError, sqlite3.Error):
        return jsonify({'ok': False, 'message': 'Не удалось прочитать сохранённое задание; состояние не изменено.'}), 503
    from autobot import tender_search_state as search_state
    payload['search_summary'] = search_state.public_summary(DATA_DIR, running=payload['running'])
    payload['search_resume'] = search_state.public_resume(DATA_DIR)
    tender_id = request.args.get('tender_id', '')
    if re.fullmatch(r'\d{8,25}', tender_id):
        from autobot.document_bundle import display_status
        payload['document_status'] = display_status(REPORTS_DIR, tender_id)
        from autobot.estimate_publication import display_status as parse_status
        payload['document_parse'] = parse_status(REPORTS_DIR, tender_id)
    return jsonify(payload)


@app.route("/api/tenders")
def api_tenders():
    if not TENDERS_JSON.exists():
        return jsonify({"items": []})
    try:
        data = json.loads(TENDERS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return jsonify({"items": []})
    items = []
    for row in data:
        tid = str(row.get("tender_id", "") or "").strip()
        stage_raw = (str(row.get("stage") or "")).strip()
        stage_open = stage_raw == STAGE_SUBMISSION
        stage_display = stage_raw if stage_raw else "—"
        items.append(
            {
                "tender_id": row.get("tender_id"),
                "region": row.get("region"),
                "title": row.get("title"),
                "price_rub": row.get("price_rub"),
                "eis_url": eis_notice_url(tid, row.get("url")),
                "stage_display": stage_display,
                "stage_open": stage_open,
                "publish_date": (row.get("publish_date") or ""),
                "updated_date": (row.get("updated_date") or ""),
                "customer_name": (row.get("customer_name") or ""),
                "law": (row.get("law") or ""),
                "purchase_method": (row.get("purchase_method") or ""),
            }
        )
    items.sort(key=lambda x: (x.get("region") or "", str(x.get("tender_id") or "")))
    return jsonify({"items": items[:200]})


@app.route("/api/tenders/<tender_id>/delete", methods=["POST"])
def api_delete_tender(tender_id: str):
    tid = str(tender_id or "").strip()
    if not re.fullmatch(r"\d{8,25}", tid):
        return jsonify({"ok": False, "message": "Некорректный номер тендера."}), 400
    data = request.get_json(silent=True) or {}
    if str(data.get("confirm_tender_id") or "").strip() != tid:
        return jsonify({"ok": False, "message": "Удаление не подтверждено номером тендера."}), 400
    parse_running = _main_job_busy()
    with merge_site_lock:
        merge_running = bool(merge_site_state.get("running"))
    if parse_running or merge_running:
        return jsonify({"ok": False, "message": "Дождитесь завершения текущей задачи AutoBot и повторите удаление."}), 409
    try:
        with tender_delete_lock:
            result = delete_tender_data(tid)
    except FileNotFoundError:
        return jsonify({"ok": False, "message": "Тендер уже удалён или не найден."}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)[:500]}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Не удалось удалить тендер: {str(exc)[:500]}"}), 500
    return jsonify({"ok": True, **result})


@app.route("/api/estimates/<estimate_id>/crm-import-payload")
def api_estimate_crm_import_payload(estimate_id: str):
    """Return estimate data only; the authenticated PM.bi parent performs the write."""
    from autobot.uploaded_corrections import CorrectionError
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    fetch_site = str(request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site and fetch_site != "same-origin":
        return jsonify({"ok": False, "message": "Запрос данных сметы отклонён."}), 403
    capability = str(request.headers.get("X-AutoBot-Estimate-Capability") or "")
    if not _verify_estimate_import_capability(estimate_id, capability):
        return jsonify({"ok": False, "message": "Ссылка на смету устарела. Обновите страницу."}), 403
    if not estimate_id or not _load_estimate_meta(estimate_id):
        return jsonify({"ok": False, "message": "Смета не найдена."}), 404
    try:
        payload = _build_estimate_crm_import_payload(estimate_id)
    except CorrectionError as exc:
        response = jsonify({'ok': False, 'message': str(exc)})
        response.headers['Cache-Control'] = 'no-store'
        return response, exc.status
    except EstimateImportTooLargeError as exc:
        response = jsonify({"ok": False, "message": str(exc)})
        response.headers["Cache-Control"] = "no-store"
        return response, 413
    if not payload["items"]:
        return jsonify({"ok": False, "message": "В смете нет подходящих строк для добавления в объект."}), 400
    response = jsonify({"ok": True, "estimate_id": estimate_id, **payload})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/tenders/crm/projects")
@app.route("/api/crm/projects")
def api_crm_projects():
    try:
        projects = crm_projects_for_picker()
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)[:700]}), 502
    return jsonify({"ok": True, "projects": projects})


@app.route("/api/export-to-crm", methods=["POST"])
def api_export_to_crm():
    from autobot.uploaded_corrections import CorrectionError
    data = request.get_json(silent=True) or {}
    tid = str(data.get("tender_id", "")).strip()
    if not tid:
        return jsonify({"ok": False, "message": "Нужен tender_id"}), 400
    if tid not in load_tender_metadata():
        return jsonify({"ok": False, "message": "Такой тендер не найден в tenders.json"}), 404
    try:
        result = export_tender_to_crm(tid, project_id=data.get("project_id", data.get("projectId")))
    except CorrectionError as error:
        return jsonify({'ok':False,'message':str(error)}), error.status
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)[:700]}), 500
    return jsonify({"ok": True, "tender_id": tid, **result})


@app.route("/api/estimates/<estimate_id>/export-to-crm", methods=["POST"])
def api_export_estimate_to_crm(estimate_id: str):
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    meta = _load_estimate_meta(estimate_id)
    if not meta:
        return jsonify({"ok": False, "message": "Смета не найдена."}), 404
    data = request.get_json(silent=True) or {}
    try:
        result = export_estimate_to_crm(
            estimate_id,
            overrides=data,
            project_id=data.get("project_id", data.get("projectId")),
        )
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)[:700]}), 500
    return jsonify({"ok": True, "estimate_id": estimate_id, **result})


@app.route("/api/estimates/<estimate_id>/delete", methods=["POST"])
def api_delete_estimate(estimate_id: str):
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    if not estimate_id:
        return jsonify({"ok": False, "message": "Нужен estimate_id."}), 400
    try:
        delete_estimate(estimate_id)
    except Exception as e:
        message = str(e)[:700]
        status = 409 if "идёт поиск рынка" in message else 500
        if "не найдена" in message.casefold():
            status = 404
        return jsonify({"ok": False, "message": message}), status
    return jsonify({"ok": True, "estimate_id": estimate_id})


@app.route("/api/merge-site-status")
def api_merge_site_status():
    with merge_site_lock:
        total = int(merge_site_state["total"] or 0)
        done = int(merge_site_state["done"] or 0)
        running = bool(merge_site_state["running"])
        current_tid = merge_site_state.get("current_tid") or ""
        market_done = int(merge_site_state.get("market_done") or 0)
        market_total = int(merge_site_state.get("market_total") or 0)

    if running and current_tid:
        live_market_done, live_market_total = _market_progress_for_tender(current_tid)
        if live_market_total > 0:
            if live_market_total == market_total:
                market_done = max(market_done, int(live_market_done))
            else:
                market_done = int(live_market_done)
            market_total = int(live_market_total)
            with merge_site_lock:
                if merge_site_state.get("current_tid") == current_tid:
                    merge_site_state["market_done"] = market_done
                    merge_site_state["market_total"] = market_total

    market_percent = int(min(100, max(0, round(100.0 * market_done / market_total)))) if market_total > 0 else 0
    current_fraction = (market_done / market_total) if (running and market_total > 0) else 0.0
    if running and total > 0:
        pct = int(min(99, max(0, round(100.0 * (done + current_fraction) / total))))
        if market_done > 0 and done < total:
            pct = max(1, pct)
    elif not running and total > 0 and done >= total:
        pct = 100
    else:
        pct = 0 if total == 0 else int(min(100, max(0, round(100.0 * done / total))))

    market_events = _read_market_web_events(current_tid) if current_tid else []
    market_event_max_seq = max((int(e.get("seq") or 0) for e in market_events), default=0)
    if running and current_tid and market_total > 0 and market_done > 0 and market_done > market_event_max_seq:
        should_add_fallback = False
        with merge_site_lock:
            last_chat_done = int(merge_site_state.get("last_market_chat_done") or 0)
            if market_done > last_chat_done:
                merge_site_state["last_market_chat_done"] = market_done
                should_add_fallback = True
        if should_add_fallback:
            _merge_chat_add("done", f"✅ {market_done}/{market_total} · готово.", tender_id=current_tid, seq=market_done, total=market_total)
            if market_done < market_total:
                _merge_chat_add("begin", f"Работа {market_done + 1} из {market_total} началась.", tender_id=current_tid, seq=market_done + 1, total=market_total)

    with merge_site_lock:
        web_events = list(merge_site_state.get("chat_events") or [])
        chat_events = sorted(
            web_events + market_events,
            key=lambda e: (str(e.get("ts") or ""), str(e.get("source") or ""), int(e.get("seq") or 0)),
        )[-140:]
        payload = {
            "running": bool(merge_site_state["running"]),
            "total": total,
            "done": done,
            "percent": pct,
            "current_tid": merge_site_state.get("current_tid") or "",
            "market_done": market_done,
            "market_total": market_total,
            "market_left": max(0, market_total - market_done),
            "market_percent": market_percent,
            "started_at": merge_site_state.get("started_at"),
            "ended_at": merge_site_state.get("ended_at"),
            "error_ids": list(merge_site_state.get("error_ids") or []),
            "log_tail": (merge_site_state.get("log_lines") or [])[-60:],
            "chat_events": chat_events,
            "last_ended_at": merge_site_state.get("last_ended_at"),
            "last_summary": merge_site_state.get("last_summary") or "",
            "last_reason_counts": merge_site_state.get("last_reason_counts") or {},
        }
    return jsonify(payload)


@app.route("/api/avito-status")
def api_avito_status():
    """Read the persistent Avito pause without opening Avito."""
    try:
        from autobot.real_market_scraper import avito_guard_status

        status = avito_guard_status()
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Не удалось прочитать состояние Авито: {str(exc)[:300]}"}), 500
    blocked_until = float(status.get("blocked_until") or 0)
    status["blocked_until_iso"] = (
        datetime.fromtimestamp(blocked_until, timezone.utc).isoformat(timespec="seconds") if blocked_until > 0 else ""
    )
    return jsonify({"ok": True, **status})


_AGENT_MARKET_SOURCE_HINTS = (
    {
        "regions": ("яросл",),
        "markers": ("бетон в15", "бетон м200", "бст в15"),
        "urls": (
            "https://beton-yrs.ru/price/",
            "https://yaroslavl.gamma-beton.ru/price",
            "https://yar-beton.ru/",
        ),
    },
    {
        "regions": ("яросл",),
        "markers": ("песок строительный", "песок карьерный", "песок мелкий"),
        "urls": (
            "https://yaroslavl.scheben-rf.ru/pesok_karerniy/",
            "https://xn--90ahb6al8czar.xn--p1ai/karernyj-pesok/",
            "https://pesok-yaroslavl.ru/kariernyy-pesok",
            "https://postavka76.ru/pages/pesok.htm",
        ),
    },
    {
        "regions": ("яросл",),
        "markers": ("щебень",),
        "urls": (
            "https://yaroslavl.scheben-rf.ru/scheben_granitniy/",
            "https://yaroslavl.scheben-rf.ru/scheben_graviyniy/",
            "https://xn--90ahb6al8czar.xn--p1ai/shcheben-20-40/",
        ),
    },
)


def _agent_market_start_urls(name: object, queries: object, region: object) -> list[str]:
    region_text = str(region or "").casefold().replace("ё", "е")
    query_text = " ".join([str(name or ""), *(str(item or "") for item in list(queries or []))]).casefold().replace("ё", "е")
    for hint in _AGENT_MARKET_SOURCE_HINTS:
        if not any(marker in region_text for marker in hint["regions"]):
            continue
        if any(marker.casefold().replace("ё", "е") in query_text for marker in hint["markers"]):
            return list(hint["urls"])
    return []


def _agent_avito_search(name: object, position_type: object, region: object) -> tuple[str, str]:
    """Build a short native Avito query instead of sending an SEO-style web query."""

    from autobot.market_strategy import market_query_name

    query = re.sub(r"\s+", " ", market_query_name(name, str(position_type or ""))).strip()
    region_text = str(region or "").casefold().replace("ё", "е")
    location = "all"
    for marker, slug in (
        ("ярослав", "yaroslavl"),
        ("екатеринбург", "ekaterinburg"),
        ("свердлов", "sverdlovskaya_oblast"),
        ("санкт-петербург", "sankt-peterburg"),
        ("ленинград", "leningradskaya_oblast"),
        ("моск", "moskva"),
    ):
        if marker in region_text:
            location = slug
            break
    return query, f"https://www.avito.ru/{location}?" + urlencode({"q": query})


def _agent_market_research_key(
    name: object,
    unit: object,
    basis_code: object,
    position_type: object,
) -> str:
    """Collapse duplicate estimate rows into one external market lookup."""

    from autobot.market_strategy import market_query_name, normalize_unit

    query = re.sub(r"\s+", " ", market_query_name(name, str(position_type or ""))).strip().casefold().replace("ё", "е")
    canonical_unit = normalize_unit(unit)
    # Different estimate catalogue codes can still describe the same market
    # product (the Volga estimate contains exactly that case for concrete B20).
    # The compact query already retains grade/fraction/density where specified,
    # so the external lookup identity is product + comparable unit, not code.
    return "|".join((str(position_type or "").strip().casefold(), query, canonical_unit))


def _agent_market_offer_display_values(raw_offer: dict, outcome: dict) -> tuple[object, str]:
    """Present historical block-unit offers as a comparable base-unit price."""

    from autobot.market_strategy import normalize_unit
    from autobot.real_market_scraper import _agent_unit_multiplier

    raw_unit = str(raw_offer.get("unit") or outcome.get("matched_unit") or "").strip()
    display_unit = normalize_unit(outcome.get("matched_unit") or raw_unit) or raw_unit
    raw_price = raw_offer.get("price")
    # New imports expose both raw_price and the already-normalized price.
    # Historical imports did not, so normalize those once for display only.
    if outcome.get("raw_price") not in (None, ""):
        display_price = outcome.get("price", raw_price)
    else:
        display_price = raw_price
        try:
            multiplier = _agent_unit_multiplier(raw_unit)
            if multiplier > 1:
                display_price = float(display_price) / multiplier
        except (TypeError, ValueError):
            pass
    return display_price, display_unit


def _agent_market_public_offer_rows(job: dict) -> list[dict]:
    """Build small, safe result rows for the tender progress interface."""
    result = job.get("result") or {}
    imported = result.get("import") or {}
    outcomes = {
        str(item.get("url") or "").strip(): item
        for item in list(imported.get("offer_outcomes") or [])
        if isinstance(item, dict) and str(item.get("url") or "").strip()
    }
    raw_offers = [item for item in list(result.get("offers") or [])[:10] if isinstance(item, dict)]
    rows: list[dict] = []
    for raw_offer in raw_offers:
        url = str(raw_offer.get("url") or "").strip()
        outcome = outcomes.get(url) or {}
        verification = str(outcome.get("verification") or "").strip().casefold()
        if not verification:
            if imported.get("offer_outcomes") is not None:
                verification = "rejected"
            else:
                verification = "verified" if len(raw_offers) == 1 and int(imported.get("verified") or 0) else "candidate"
        if verification not in {"verified", "candidate"}:
            verification = "rejected"
        display_price, display_unit = _agent_market_offer_display_values(raw_offer, outcome)
        rows.append(
            {
                "job_id": job.get("id"),
                "position_key": job.get("position_key"),
                "position_name": job.get("position_name"),
                "title": str(raw_offer.get("title") or "Источник цены")[:500],
                "price": display_price,
                "unit": str(display_unit)[:80],
                "url": url,
                "evidence": str(raw_offer.get("evidence") or "")[:800],
                "verification": verification,
                "reason": str(outcome.get("verification_reason") or imported.get("message") or "")[:800],
                "completed_at": job.get("completed_at"),
            }
        )
    return rows


def _agent_market_compact_reason(job: dict, offers: list[dict]) -> str:
    status = str(job.get("status") or "").strip().casefold()
    avito = str(job.get("job_mode") or "web").strip().casefold() == "avito"
    if status == "queued":
        return "Ждёт подключения браузерного исполнителя" if avito else "Ждёт серверного поиска"
    if status == "leased":
        return "Браузерный исполнитель проверяет объявления" if avito else "Проверяются страницы поставщиков"
    if status == "failed":
        return str(job.get("error") or "Задание завершилось с ошибкой")[:220]
    verified = [item for item in offers if item.get("verification") == "verified"]
    candidates = [item for item in offers if item.get("verification") == "candidate"]
    if verified:
        return f"Подтверждено цен: {len(verified)}"
    if candidates:
        return str(candidates[0].get("reason") or "Найдены цены, но требуется ручная проверка")[:220]
    result = job.get("result") or {}
    imported = result.get("import") or {}
    detail = " ".join(
        str(value or "")
        for value in (job.get("error"), result.get("notes"), imported.get("message"))
    ).casefold()
    restriction_markers = (
        "доступ ограничен",
        "ограничение доступа",
        "показал ограничение",
        "показала ограничение",
        "показал captcha",
        "показала captcha",
        "captcha появилась",
        "проблема с ip",
    )
    no_restriction_markers = (
        "без captcha/огранич",
        "без captcha и огранич",
        "ограничение доступа не показ",
        "ограничения доступа не показ",
        "captcha не показ",
        "captcha или ограничение доступа не показ",
    )
    if any(marker in detail for marker in restriction_markers) and not any(
        marker in detail for marker in no_restriction_markers
    ):
        return "Авито ограничил доступ до подтверждения цены"
    if any(marker in detail for marker in ("нерелевант", "неподходящ", "не подход", "другой класс", "другая марка")):
        return "Подходящее объявление не удалось подтвердить"
    return "Подтверждённая цена не найдена"


def _agent_market_latest_run(jobs: list[dict]) -> dict:
    """Summarize only the latest launch instead of mixing it with all history."""
    empty = {
        "id": "",
        "status": "idle",
        "total": 0,
        "processed": 0,
        "queued": 0,
        "leased": 0,
        "completed": 0,
        "failed": 0,
        "canceled": 0,
        "percent": 0,
        "offers_found": 0,
        "verified_offers": 0,
        "candidate_offers": 0,
        "rejected_offers": 0,
        "positions_verified": 0,
        "positions_with_candidates": 0,
        "positions_without_offers": 0,
        "started_at": 0,
        "completed_at": 0,
        "elapsed_seconds": 0,
        "current_index": 0,
        "current": None,
        "positions": [],
    }
    if not jobs:
        return empty

    newest = max(jobs, key=lambda item: float(item.get("created_at") or 0))
    newest_payload = newest.get("payload") or {}
    batch_id = str(newest_payload.get("batch_id") or "").strip()
    if batch_id:
        run_jobs = [job for job in jobs if str((job.get("payload") or {}).get("batch_id") or "") == batch_id]
    else:
        # Old jobs did not have batch_id. Keep the newest creation cluster so
        # the interface can still show a useful summary for the control run.
        newest_created = float(newest.get("created_at") or 0)
        run_jobs = [job for job in jobs if newest_created - float(job.get("created_at") or 0) <= 10 * 60]
        oldest_created = min((float(job.get("created_at") or 0) for job in run_jobs), default=newest_created)
        batch_id = f"legacy-{int(oldest_created)}"

    # A canceled duplicate is a queue correction, not a researched position.
    # Hide it from the user when the launch also contains real processed jobs.
    non_canceled_jobs = [job for job in run_jobs if str(job.get("status") or "") != "canceled"]
    if non_canceled_jobs and not newest_payload.get('estimate_plan'):
        run_jobs = non_canceled_jobs

    latest_by_position: dict[str, dict] = {}
    for job in sorted(run_jobs, key=lambda item: float(item.get("created_at") or 0), reverse=True):
        key = str(job.get("position_key") or "").strip()
        if key and key not in latest_by_position:
            latest_by_position[key] = job
    run_jobs = list(latest_by_position.values())

    def item_order(job: dict) -> tuple[float, float]:
        raw_no = (job.get("payload") or {}).get("item_no")
        try:
            item_no = float(raw_no)
        except (TypeError, ValueError):
            item_no = 1_000_000.0
        return item_no, float(job.get("created_at") or 0)

    status_counts = {status: 0 for status in ("queued", "leased", "completed", "failed", "canceled")}
    positions: list[dict] = []
    verified_offers = candidate_offers = rejected_offers = 0
    positions_verified = positions_with_candidates = positions_without_offers = 0
    for job in sorted(run_jobs, key=item_order):
        status = str(job.get("status") or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        offer_rows = _agent_market_public_offer_rows(job)
        verified = sum(item.get("verification") == "verified" for item in offer_rows)
        candidates = sum(item.get("verification") == "candidate" for item in offer_rows)
        rejected = sum(item.get("verification") == "rejected" for item in offer_rows)
        verified_offers += verified
        candidate_offers += candidates
        rejected_offers += rejected
        if verified:
            state = "verified"
            positions_verified += 1
        elif candidates:
            state = "candidate"
        elif status in {"queued", "leased", "failed", "canceled"}:
            state = status
        else:
            state = "empty"
        if candidates:
            positions_with_candidates += 1
        if not verified and not candidates:
            positions_without_offers += 1
        positions.append(
            {
                "job_id": job.get("id"),
                "position_key": job.get("position_key"),
                "item_no": (job.get("payload") or {}).get("item_no"),
                "position_name": job.get("position_name"),
                "status": status,
                "state": state,
                "verified": verified,
                "candidate": candidates,
                "rejected": rejected,
                "offers_found": len(offer_rows),
                "reason": _agent_market_compact_reason(job, offer_rows),
                "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at"),
                "completed_at": job.get("completed_at"),
                "offers": offer_rows[:5],
            }
        )

    total = len(run_jobs)
    processed = sum(status_counts.get(status, 0) for status in ("completed", "failed", "canceled"))
    active = [item for item in positions if item.get("status") in {"queued", "leased"}]
    active.sort(key=lambda item: (0 if item.get("status") == "leased" else 1, item.get("created_at") or 0))
    started_at = min((float(job.get("created_at") or 0) for job in run_jobs), default=0)
    completed_at = (
        max((float(job.get("completed_at") or job.get("updated_at") or 0) for job in run_jobs), default=0)
        if total and processed >= total
        else 0
    )
    end_at = completed_at or time.time()
    status = "running" if active else ("completed" if total and processed >= total else "idle")
    return {
        **empty,
        "id": batch_id,
        "status": status,
        "total": total,
        "processed": processed,
        "queued": status_counts.get("queued", 0),
        "leased": status_counts.get("leased", 0),
        "completed": status_counts.get("completed", 0),
        "failed": status_counts.get("failed", 0),
        "canceled": status_counts.get("canceled", 0),
        "percent": int(round((processed / total) * 100)) if total else 0,
        "offers_found": verified_offers + candidate_offers + rejected_offers,
        "verified_offers": verified_offers,
        "candidate_offers": candidate_offers,
        "rejected_offers": rejected_offers,
        "positions_verified": positions_verified,
        "positions_with_candidates": positions_with_candidates,
        "positions_without_offers": positions_without_offers,
        "started_at": started_at,
        "completed_at": completed_at,
        "elapsed_seconds": max(0, int(round(end_at - started_at))) if started_at else 0,
        "current_index": min(total, processed + 1) if active else processed,
        "current": active[0] if active else None,
        "positions": positions[:250],
        "results": [offer for position in positions for offer in position["offers"]][:60],
        "positions_truncated": len(positions) > 250,
        "estimate_plan": newest_payload.get('estimate_plan') or {},
    }


@app.route("/api/tenders/<tender_id>/agent-market/jobs", methods=["GET", "POST"])
def api_tender_agent_market_jobs(tender_id: str):
    """Create browser-agent jobs from real estimate rows or show their current state."""
    tid = str(tender_id or "").strip()
    if not re.fullmatch(r"\d{8,25}", tid):
        return jsonify({"ok": False, "message": "Некорректный номер тендера"}), 400
    from autobot.agent_market_queue import enqueue_jobs, job_progress, job_summary, latest_position_jobs

    requested_mode = str(request.args.get("mode") or "web").strip().casefold()
    job_mode = "avito" if requested_mode == "avito" else "web"

    if request.method == "GET":
        from autobot.market_web_worker import web_worker_enabled
        server_executor = job_mode == "web" and web_worker_enabled()
        jobs = latest_position_jobs(tid, mode=job_mode)
        latest_jobs: dict[str, dict] = {}
        for job in jobs:
            key = str(job.get("position_key") or "").strip()
            if key and key not in latest_jobs:
                latest_jobs[key] = job

        public_results: list[dict] = []
        result_totals = {"found": 0, "verified": 0, "candidate": 0, "rejected": 0}
        for job in latest_jobs.values():
            for row in _agent_market_public_offer_rows(job):
                verification = row["verification"]
                result_totals["found"] += 1
                result_totals[verification] += 1
                public_results.append(row)
        public_jobs = [
            {
                "id": job.get("id"),
                "position_key": job.get("position_key"),
                "position_name": job.get("position_name"),
                "job_mode": job.get("job_mode") or "web",
                "status": job.get("status"),
                "attempts": job.get("attempts"),
                "worker_id": job.get("worker_id"),
                "error": job.get("error"),
                "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at"),
                "completed_at": job.get("completed_at"),
                "offers_found": len((job.get("result") or {}).get("offers") or []),
                "notes": str((job.get("result") or {}).get("notes") or "")[:500],
                "import": (job.get("result") or {}).get("import") or {},
            }
            for job in jobs[:250]
        ]
        return jsonify(
            {
                "ok": True,
                "enabled": server_executor or bool(_agent_market_token()),
                "executor": "server" if server_executor else "external",
                "mode": job_mode,
                "summary": job_summary(tid, mode=job_mode),
                "progress": job_progress(tid, mode=job_mode),
                "latest_run": _agent_market_latest_run(jobs),
                "jobs": public_jobs,
                "results": public_results[:60],
                "result_totals": result_totals,
            }
        )

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'message': 'Ожидается объект параметров'}), 400
    requested_mode = str(data.get("mode") or requested_mode).strip().casefold()
    job_mode = "avito" if requested_mode == "avito" else "web"
    if data.get('action') == 'cancel_pending':
        from autobot.agent_market_queue import cancel_pending_jobs
        stopped = cancel_pending_jobs(tid, mode=job_mode)
        return jsonify({'ok': True, 'canceled': stopped, 'progress': job_progress(tid, mode=job_mode)})
    estimate_path = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx"
    if not estimate_path.is_file():
        return jsonify({"ok": False, "message": "Для тендера ещё нет распознанной сметы"}), 404
    batch_id = uuid.uuid4().hex
    scope = str(data.get('scope') or '').strip()
    all_positions = job_mode == 'web' and scope == 'all_without_verified'
    requested_values = data.get('position_keys') or []
    if not isinstance(requested_values, list) or len(requested_values) > 5000:
        return jsonify({'ok': False, 'message': 'Выберите не больше 5 000 строк или запустите поиск по всей смете'}), 400
    requested_keys = {
        str(value or "").strip()
        for value in requested_values
        if str(value or "").strip()
    }
    if all_positions:
        requested_keys = set()
    try:
        default_limit = 5 if job_mode == "avito" else 20
        limit = max(1, min(int(data.get("limit") or default_limit), 50))
    except (TypeError, ValueError):
        limit = 5 if job_mode == "avito" else 20
    metadata = load_tender_metadata()
    meta = dict(metadata.get(tid) or {})
    workflow_items, _ = _tenders_items()
    workflow = next((dict(item) for item in workflow_items if str(item.get("tender_id") or "") == tid), {})
    tender = build_tender_detail(tid, meta, workflow)
    if all_positions:
        limit = max(1, len(tender.get('positions') or []))
    elif job_mode == 'web' and scope == 'selected' and requested_keys:
        limit = len(requested_keys)
    eligible_positions: list[tuple[int, int, dict, list[str]]] = []
    skipped_ineligible: list[dict[str, str]] = []
    type_priority = {"material": 0, "product": 0, "service": 1, "work": 1}
    for row_index, position in enumerate(tender.get("positions") or []):
        key = str(position.get("position_key") or "")
        if requested_keys and key not in requested_keys:
            continue
        if not requested_keys and position.get("verified_count"):
            continue
        queries = [str(query or "").strip() for query in list(position.get("queries") or []) if str(query or "").strip()]
        position_type = str(position.get("type_slug") or "").strip().casefold()
        unit = str(position.get("unit") or "").strip()
        can_auto_price = position.get("can_auto_price")
        if can_auto_price is None:
            can_auto_price = bool(queries and unit and unit != "—")
        reason = ""
        if not can_auto_price:
            reason = str(position.get("warning") or "Позиция не готова к автоматическому сравнению")
        elif not queries:
            reason = "Нет безопасного рыночного запроса"
        elif position_type in {"aggregate", "other"}:
            reason = "Сводную или неоднозначную строку сначала нужно разложить"
        elif not unit or unit == "—":
            reason = "Нет единицы измерения"
        if reason:
            skipped_ineligible.append({"position_key": key, "name": str(position.get("name") or ""), "reason": reason})
            continue
        eligible_positions.append((type_priority.get(position_type, 2), row_index, position, queries))

    selected: list[dict] = []
    selected_research_keys: dict[str, dict] = {}
    estimate_plan = {
        'scope': scope or 'limited',
        'total_positions': len(tender.get('positions') or []),
        'already_verified': sum(bool(row.get('verified_count')) for row in tender.get('positions') or []),
        'needs_details': len(skipped_ineligible),
        'searchable_positions': len(eligible_positions),
    }
    def search_order(item):
        try:
            amount = max(0.0, float(item[2].get('estimate_total') or 0)) if all_positions else 0.0
            if not math.isfinite(amount):
                amount = 0.0
        except (TypeError, ValueError):
            amount = 0.0
        return item[0], -amount, item[1]

    for search_rank, (_, row_index, position, queries) in enumerate(sorted(eligible_positions, key=search_order)):
        key = str(position.get("position_key") or "")
        primary_query = queries[0]
        region = str(tender.get("region") or "").strip()
        position_type = str(position.get("type_slug") or "").strip().casefold()
        research_key = _agent_market_research_key(
            position.get("name"),
            position.get("unit"),
            position.get("basis_code"),
            position_type,
        )
        duplicate_of = None if all_positions or scope == 'selected' else selected_research_keys.get(research_key)
        if duplicate_of is not None:
            duplicate_of.setdefault("equivalent_positions", []).append(
                {
                    "position_key": key,
                    "name": str(position.get("name") or ""),
                    "item_no": position.get("item_no"),
                }
            )
            skipped_ineligible.append(
                {
                    "position_key": key,
                    "name": str(position.get("name") or ""),
                    "reason": "Дубликат уже выбранной рыночной позиции",
                    "duplicate_of": str(duplicate_of.get("position_key") or ""),
                }
            )
            continue
        start_urls = _agent_market_start_urls(position.get("name"), queries, region)
        payload = {
            "schema_version": 2,
            "batch_id": batch_id,
            "estimate_plan": estimate_plan,
            "batch_created_at": time.time(),
            "tender_id": tid,
            "position_key": key,
            "item_no": position.get("item_no"),
            "name": position.get("name"),
            "unit": position.get("unit"),
            "quantity": position.get("quantity"),
            "section": position.get("section"),
            "source_file": position.get("source_file"),
            "basis_code": position.get("basis_code"),
            "position_type": position_type,
            "region": region,
            "requirements": position.get("requirements") or {},
            "estimate_unit_price": position.get("estimate_unit"),
            "queries": queries,
            "job_mode": job_mode,
            "max_offers": 3,
            "max_sources": 3,
            "max_turns": 16,
            "max_seconds": 180,
            "max_attempts": 2 if job_mode == "web" else 1,
            "retry_policy": "network_only",
            "queue_priority": (60 if position_type in {"material", "product"} else 70) + search_rank,
            "start_urls": start_urls,
            "result_schema": {
                "schema_version": 2,
                "position_key": key,
                "offers": [
                    {
                        "title": "",
                        "price": 0,
                        "currency": "RUB",
                        "unit": "",
                        "url": "",
                        "evidence": "",
                        "observed_at": "ISO-8601",
                        "published_at": "",
                        "location": "",
                        "confidence": 0.0,
                    }
                ],
                "notes": "",
            },
        }
        if job_mode == "avito":
            avito_query, avito_url = _agent_avito_search(position.get("name"), position_type, region)
            payload.update(
                {
                    "search_mode": "avito_agent",
                    "market_query": avito_query,
                    "comparison_unit": position.get("unit"),
                    "max_offers": 2,
                    "max_sources": 2,
                    "max_turns": 24,
                    "max_seconds": 360,
                    "allowed_domains": ["avito.ru"],
                    "start_urls": [avito_url],
                    "task": (
                        "Ищи цену только на Авито через обычный браузер доступными инструментами управления вкладкой. "
                        f"Начни с готовой страницы поиска: {avito_url}. "
                        f"Короткий запрос Авито: {avito_query}. Дождись появления карточек выдачи. "
                        "Для подтверждения цены открой само объявление; поисковая выдача не подтверждает цену. "
                        "До открытия сравни заголовок карточки с коротким запросом и пропускай другой материал, марку или класс. "
                        "Открой не более 2 подходящих объявлений и верни только прямые ссылки вида avito.ru/..._123456789. "
                        "Для каждого предложения запиши точное название, цену, единицу, город и короткий видимый фрагмент страницы в evidence. "
                        "Включи в evidence характеристики товара: если марка или класс противоречат заголовку, не скрывай расхождение. "
                        "В price пиши число ровно как на странице, а в unit — его знаменатель (например, 650 и м или 65000 и 100 м); не пересчитывай сам. "
                        "Если цена указана за упаковку или рулон, обязательно перепиши в evidence видимые размеры рулона; размеры не выдумывай. "
                        "Не используй сниппеты поисковиков, другие домены, цену доставки, кредита или похожего товара. "
                        "Не обходи CAPTCHA и ограничения доступа, не перезагружай заблокированную страницу многократно. "
                        "Если Авито показал CAPTCHA или ограничение IP до первой валидной цены, верни пустой offers и укажи причину в notes. "
                        "Если ограничение появилось после уже собранных валидных объявлений, не теряй их: сразу верни собранные offers и укажи ограничение в notes. "
                        "Остановись после 2 валидных объявлений и верни только JSON по схеме result_schema."
                    ),
                }
            )
        else:
            source_instruction = (
                "Сначала открой эти прямые источники по порядку: " + ", ".join(start_urls) + ". "
                if start_urls
                else ""
            )
            payload.update(
                {
                    "search_mode": "fast_web",
                    "excluded_domains": ["avito.ru"],
                    "task": (
                    "Быстрый поиск только по обычным сайтам поставщиков, производителей и подрядчиков. "
                    + source_instruction
                    + f"Используй запросы из queries по порядку; товар/работа уже определены как {position_type}. "
                    "Не используй Авито. Открой не более 3 наиболее перспективных прямых страниц, "
                    "не делай искусственных пауз и не обходи CAPTCHA или ограничения сайта. "
                    "Остановись сразу после 2 валидных цен. Если 3 страницы не дали цену, сразу верни результат. "
                    "Не используй цену доставки, кредита или похожего товара. Верни только JSON по схеме result_schema."
                    " В price верни цену ровно как она видна на странице, а в unit — точную единицу/блок; сам цену не умножай. Evidence должен содержать видимые название, цену и единицу."
                ),
                }
            )
        selected_research_keys[research_key] = payload
        selected.append(payload)
        if len(selected) >= limit:
            break
    if not selected:
        if all_positions:
            return jsonify({'ok': True, 'mode': job_mode, 'created': 0, 'skipped_active': 0,
                            'skipped_ineligible': skipped_ineligible, 'estimate_plan': estimate_plan,
                            'jobs': [], 'summary': job_summary(tid, mode=job_mode), 'batch_id': ''})
        return jsonify({"ok": False, "message": "Нет позиций, которые можно безопасно сравнить с рынком", "skipped_ineligible": skipped_ineligible}), 400
    outcome = enqueue_jobs(tid, selected, priority=80 if job_mode == "avito" else 100)
    return jsonify(
        {
            "ok": True,
            "mode": job_mode,
            "created": len(outcome["created"]),
            "skipped_active": len(outcome["skipped_active"]),
            "skipped_ineligible": skipped_ineligible,
            "estimate_plan": estimate_plan,
            "jobs": outcome["created"],
            "summary": job_summary(tid, mode=job_mode),
            "batch_id": batch_id,
        }
    )


@app.route("/api/tenders/<tender_id>/agent-market/jobs/<job_id>/cancel", methods=["POST"])
def api_tender_agent_market_cancel(tender_id: str, job_id: str):
    from autobot.agent_market_queue import cancel_job

    if not cancel_job(str(job_id or ""), str(tender_id or "")):
        return jsonify({"ok": False, "message": "Активное задание не найдено"}), 404
    return jsonify({"ok": True})


@app.route("/api/agent-market/v1/status")
def api_agent_market_worker_status():
    auth_error = _require_agent_market_token()
    if auth_error:
        return auth_error
    return jsonify({"ok": True, "schema_version": 1, "service": "autobot-agent-market",
                    "features": ["lease_token", "durable_completion"]})


def _agent_market_lease_seconds(data: dict, *, default: int = 600) -> int:
    raw_value = data.get("lease_seconds", default)
    if raw_value is None:
        return default
    if type(raw_value) is not int:
        raise ValueError("lease_seconds должен быть целым числом")
    return raw_value


@app.route("/api/agent-market/v1/claim", methods=["POST"])
def api_agent_market_claim():
    auth_error = _require_agent_market_token()
    if auth_error:
        return auth_error
    from autobot.agent_market_queue import claim_job

    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "message": "Тело запроса должно быть JSON-объектом"}), 400
    worker_id = str(data.get("worker_id") or "").strip()
    if not worker_id:
        return jsonify({"ok": False, "message": "Нужен worker_id"}), 400
    try:
        lease_seconds = _agent_market_lease_seconds(data)
        mode = data.get("mode")
        if mode is not None and mode not in ("web", "avito"):
            raise ValueError("mode must be web or avito")
        from autobot.market_web_worker import web_worker_enabled
        if web_worker_enabled():
            # The built-in worker claims web jobs directly. Legacy external
            # clients must not race it, including clients without a mode.
            if mode == "web":
                return jsonify({"ok": True, "job": None, "executor": "server"})
            mode = "avito"
        job = claim_job(worker_id, lease_seconds=lease_seconds, mode=mode)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    if not job:
        return jsonify({"ok": True, "job": None}), 200
    return jsonify({"ok": True, "job": job})


@app.route("/api/agent-market/v1/jobs/<job_id>/heartbeat", methods=["POST"])
def api_agent_market_heartbeat(job_id: str):
    auth_error = _require_agent_market_token()
    if auth_error:
        return auth_error
    from autobot.agent_market_queue import heartbeat_job

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "message": "Тело запроса должно быть JSON-объектом"}), 400
    worker_id = str(data.get("worker_id") or "").strip()
    try:
        lease_seconds = _agent_market_lease_seconds(data)
    except ValueError:
        return jsonify({"ok": False, "message": "lease_seconds должен быть целым числом"}), 400
    if not heartbeat_job(job_id, worker_id, lease_seconds=lease_seconds, lease_token=data.get('lease_token')):
        return jsonify({"ok": False, "message": "Задание не принадлежит этому агенту"}), 409
    return jsonify({"ok": True})


@app.route("/api/agent-market/v1/jobs/<job_id>/complete", methods=["POST"])
def api_agent_market_complete(job_id: str):
    auth_error = _require_agent_market_token()
    if auth_error:
        return auth_error
    from autobot.agent_market_queue import get_job
    from autobot.agent_market_delivery import complete_agent_result, DeliveryConflict

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'message': 'Тело запроса должно быть JSON-объектом'}), 400
    worker_id = str(data.get('worker_id') or '').strip()
    job = get_job(job_id)
    if not job:
        return jsonify({'ok': False, 'message': 'Задание не найдено'}), 409
    try:
        validated = _validate_agent_market_result(data.get('result'), str(job.get('position_key') or ''))
        completed = complete_agent_result(job_id, worker_id, validated, lease_token=data.get('lease_token'))
    except DeliveryConflict as exc:
        return jsonify({'ok': False, 'message': str(exc)}), 409
    except (OSError, ValueError, TypeError) as exc:
        return jsonify({'ok': False, 'message': f'Результат не принят: {str(exc)[:500]}'}), 422
    pending = bool(completed.get('delivery_pending'))
    return jsonify({'ok': True, 'job_id': job_id, 'delivery_pending': pending,
                    'import': (completed.get('result') or {}).get('import') or {}}), 202 if pending else 200


@app.route("/api/agent-market/v1/jobs/<job_id>/fail", methods=["POST"])
def api_agent_market_fail(job_id: str):
    auth_error = _require_agent_market_token()
    if auth_error:
        return auth_error
    from autobot.agent_market_queue import fail_job, get_job, owns_current_lease
    from autobot.agent_market_delivery import complete_agent_result, DeliveryConflict
    from autobot.real_market_scraper import probe_agent_market_start_urls

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'message': 'Тело запроса должно быть JSON-объектом'}), 400
    worker_id = str(data.get('worker_id') or '').strip()
    lease_token = data.get('lease_token')
    if not owns_current_lease(job_id, worker_id, lease_token=lease_token):
        return jsonify({'ok': False, 'message': 'Попытка задания истекла или больше не принадлежит этому агенту'}), 409
    job = get_job(job_id)
    payload = job.get('payload') or {}
    if str(job.get('job_mode') or 'web') == 'web' and payload.get('start_urls'):
        try:
            recovered = probe_agent_market_start_urls(str(job.get('tender_id') or ''), payload, max_sources=1)
            if recovered.get('offers'):
                validated = _validate_agent_market_result(recovered, str(job.get('position_key') or ''))
                validated['_autobot_direct_probe'] = True
                completed = complete_agent_result(job_id, worker_id, validated, lease_token=lease_token)
                pending = bool(completed.get('delivery_pending'))
                return jsonify({'ok': True, 'recovered': True, 'job_id': job_id, 'delivery_pending': pending,
                                'import': (completed.get('result') or {}).get('import') or {}}), 202 if pending else 200
        except DeliveryConflict as exc:
            return jsonify({'ok': False, 'message': str(exc)}), 409
        except (OSError, ValueError, TypeError):
            pass
    try:
        ok = fail_job(job_id, worker_id, str(data.get('error') or 'Ошибка агента'),
                      retry=bool(data.get('retry')), lease_token=lease_token)
    except (TypeError, ValueError) as exc:
        return jsonify({'ok': False, 'message': str(exc)}), 400
    if not ok:
        return jsonify({'ok': False, 'message': 'Задание больше не принадлежит этому агенту'}), 409
    return jsonify({'ok': True})


@app.route("/api/reports-coverage")
def api_reports_coverage():
    return jsonify(_compute_reports_coverage())


@app.route("/api/workflow-overview")
def api_workflow_overview():
    include_storage = str(request.args.get("storage") or "").strip().lower() in {"1", "true", "yes"}
    return jsonify(build_workflow_payload(include_storage=include_storage))


@app.route("/api/tenders/storage-overview")
@app.route("/api/storage-overview")
def api_storage_overview():
    return jsonify({"storage": [item.to_dict() for item in build_storage_overview()]})


@app.route("/api/push-state")
def api_push_state():
    cov = _compute_reports_coverage()
    pr_running = _main_job_busy()
    with parse_lock:
        pr_exit = parse_state.get("exit_code")
        pr_end = parse_state.get("ended_at")
    with merge_site_lock:
        mr_running = bool(merge_site_state.get("running"))
        mr_last_end = merge_site_state.get("last_ended_at")
        mr_summary = str(merge_site_state.get("last_summary") or "")
    return jsonify(
        {
            "parse_running": pr_running,
            "parse_exit_code": pr_exit,
            "parse_ended_at": pr_end,
            "merge_running": mr_running,
            "merge_last_ended_at": mr_last_end,
            "merge_last_summary": mr_summary,
            "coverage_merge_html": int(cov.get("merge_html_among_tenders", 0) or 0),
        }
    )


@app.route("/api/generate-merge-site-all", methods=["POST"])
def api_generate_merge_site_all():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сравнения цен уже подготавливаются."}), 409
    if _main_job_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания текущей работы с документами."}), 409
    threading.Thread(target=_run_merge_site_all_worker, kwargs={"only_missing": False}, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/generate-merge-site-missing", methods=["POST"])
def api_generate_merge_site_missing():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сравнения цен уже подготавливаются."}), 409
    if _main_job_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания текущей работы с документами."}), 409
    threading.Thread(target=_run_merge_site_all_worker, kwargs={"only_missing": True}, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/generate-merge-site-selected", methods=["POST"])
def api_generate_merge_site_selected():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сравнения цен уже подготавливаются."}), 409
    if _main_job_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания текущей работы с документами."}), 409
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("tender_ids")
    if not isinstance(raw_ids, list):
        return jsonify({"ok": False, "message": "Передайте список tender_ids."}), 400
    ids: list[str] = []
    seen: set[str] = set()
    known_ids = set(load_tender_metadata().keys())
    for raw in raw_ids:
        tid = str(raw or "").strip()
        if not re.fullmatch(r"\d{8,25}", tid) or tid in seen or tid not in known_ids:
            continue
        seen.add(tid)
        ids.append(tid)
        if len(ids) >= 100:
            break
    if not ids:
        return jsonify({"ok": False, "message": "Выберите хотя бы один тендер из каталога."}), 400
    threading.Thread(
        target=_run_merge_site_all_worker,
        kwargs={"ids_override": ids},
        daemon=True,
    ).start()
    return jsonify({"ok": True, "tender_ids": ids, "count": len(ids)})


@app.route("/api/generate-merge-site-one", methods=["POST"])
def api_generate_merge_site_one():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сравнения цен уже подготавливаются."}), 409
    if _main_job_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания текущей работы с документами."}), 409
    data = request.get_json(silent=True) or {}
    tid = str(data.get("tender_id", "")).strip()
    if not tid:
        return jsonify({"ok": False, "message": "Нужен tender_id"}), 400
    threading.Thread(target=_run_merge_site_all_worker, kwargs={"ids_override": [tid], "market_only_without_verified": True}, daemon=True).start()
    return jsonify({"ok": True, "tender_id": tid})


@app.route("/api/generate-merge-site-one-rerun-market", methods=["POST"])
def api_generate_merge_site_one_rerun_market():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сравнения цен уже подготавливаются."}), 409
    if _main_job_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания текущей работы с документами."}), 409
    data = request.get_json(silent=True) or {}
    tid = str(data.get("tender_id", "")).strip()
    if not tid:
        return jsonify({"ok": False, "message": "Нужен tender_id"}), 400
    threading.Thread(
        target=_run_merge_site_all_worker,
        kwargs={"ids_override": [tid], "force_market_no_resume": True},
        daemon=True,
    ).start()
    return jsonify({"ok": True, "tender_id": tid, "mode": "rerun_market_no_resume"})


@app.route("/api/generate-merge-site-one-sample-market", methods=["POST"])
def api_generate_merge_site_one_sample_market():
    """Recheck a small batch without deleting the remaining market report."""
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сравнения цен уже подготавливаются."}), 409
    if _main_job_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания текущей работы с документами."}), 409
    data = request.get_json(silent=True) or {}
    tid = str(data.get("tender_id", "")).strip()
    if not tid:
        return jsonify({"ok": False, "message": "Нужен tender_id"}), 400
    try:
        sample_rows = int(data.get("sample_rows") or 10)
    except (TypeError, ValueError):
        sample_rows = 10
    sample_rows = max(1, min(20, sample_rows))
    threading.Thread(
        target=_run_merge_site_all_worker,
        kwargs={
            "ids_override": [tid],
            "market_max_rows": sample_rows,
            "market_rerun_selected": True,
        },
        daemon=True,
    ).start()
    return jsonify({"ok": True, "tender_id": tid, "mode": "sample_market", "sample_rows": sample_rows})


@app.route("/api/generate-avito-safe-sample", methods=["POST"])
def api_generate_avito_safe_sample():
    """Collect a small fresh Avito sample after the persistent cooldown expires."""
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сравнения цен уже подготавливаются."}), 409
    if _main_job_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания текущей работы с документами."}), 409
    data = request.get_json(silent=True) or {}
    tid = str(data.get("tender_id", "")).strip()
    if not re.fullmatch(r"\d{8,25}", tid):
        return jsonify({"ok": False, "message": "Нужен корректный tender_id."}), 400
    if tid not in set(_estimate_xlsx_tender_ids()):
        return jsonify({"ok": False, "message": "Для этого тендера ещё нет готовой сметы."}), 404
    try:
        from autobot.real_market_scraper import avito_guard_status

        avito_status = avito_guard_status()
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Не удалось прочитать состояние Авито: {str(exc)[:300]}"}), 500
    if bool(avito_status.get("blocked")):
        remaining_minutes = max(1, int((int(avito_status.get("remaining_seconds") or 0) + 59) // 60))
        remaining_hours, remaining_tail = divmod(remaining_minutes, 60)
        remaining_text = (
            f"{remaining_hours} ч {remaining_tail} мин"
            if remaining_hours and remaining_tail
            else (f"{remaining_hours} ч" if remaining_hours else f"{remaining_tail} мин")
        )
        return jsonify(
            {
                "ok": False,
                "message": f"Авито пока на паузе — осталось примерно {remaining_text}. AutoBot не будет обращаться к сайту раньше.",
                "avito": avito_status,
            }
        ), 429
    try:
        sample_rows = int(data.get("sample_rows") or 2)
    except (TypeError, ValueError):
        sample_rows = 2
    # Одна строка сметы = одна полноценная поисковая навигация Авито. Для
    # домашнего IP безопасная проба намеренно мала; большой объём берём из кэша
    # и локального индекса, а не повторными открытиями сайта.
    sample_rows = max(1, min(2, sample_rows))
    threading.Thread(
        target=_run_merge_site_all_worker,
        kwargs={
            "ids_override": [tid],
            "market_max_rows": sample_rows,
            "market_rerun_selected": True,
            "market_sources_override": "avito",
            "market_only_without_verified": True,
            "market_avito_collect_only": True,
        },
        daemon=True,
    ).start()
    return jsonify(
        {
            "ok": True,
            "tender_id": tid,
            "mode": "avito_safe_collect",
            "sample_rows": sample_rows,
            "message": "Бережный сбор Авито запущен.",
            "safety": {
                "max_rows": 2,
                "daily_remaining": avito_status.get("daily_remaining"),
                "next_request_in_seconds": avito_status.get("next_request_in_seconds"),
            },
        }
    )


@app.route("/api/generate-merge-site-by-link", methods=["POST"])
def api_generate_merge_site_by_link():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сравнения цен уже подготавливаются."}), 409
    if _main_job_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания текущей работы с документами."}), 409
    data = request.get_json(silent=True) or {}
    raw = str(data.get("tender_link", "")).strip()
    tid = _extract_tender_id(raw)
    if not tid:
        return jsonify({"ok": False, "message": "Не удалось извлечь номер тендера из ссылки/текста."}), 400
    turl = raw if raw.startswith("http://") or raw.startswith("https://") else ""
    kwargs = {"ids_override": [tid]}
    if turl:
        kwargs["tender_url_by_id"] = {tid: turl}
    threading.Thread(target=_run_merge_site_all_worker, kwargs=kwargs, daemon=True).start()
    return jsonify({"ok": True, "tender_id": tid})


@app.route("/api/tender-viability-refresh", methods=["POST"])
def api_tender_viability_refresh():
    """Пересборка HTML отчёта с блоком оценки + опционально Telegram (по готовой СВОДКА_РЫНОК)."""
    data = request.get_json(silent=True) or {}
    tid = str(data.get("tender_id", "")).strip()
    if not tid:
        return jsonify({"ok": False, "message": "Нужен tender_id"}), 400
    from autobot.merge_estimate_market import OUT_PREFIX

    sv = REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx"
    if not sv.is_file():
        return jsonify(
            {
                "ok": False,
                "message": "Для этой закупки ещё нет готового сравнения цен. Сначала найдите рыночные цены.",
            }
        ), 400
    try:
        from autobot.report_merge_html import write_tender_report_site

        out_path = write_tender_report_site(tid)
        if not out_path or not out_path.is_file():
            return jsonify({"ok": False, "message": "Не удалось записать index.html в data/reports_site."}), 500
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)[:500]}), 500

    telegram_sent = False
    try:
        from autobot.tender_viability import format_viability_for_telegram

        vmsg = format_viability_for_telegram(tid)
        if vmsg and _telegram_cfg():
            _tg_flush_spool()
            _tg_send(vmsg)
            _tg_flush_spool()
            telegram_sent = True
    except Exception:
        pass

    base = (get_report_site_public_base() or "").strip().rstrip("/")
    report_url = f"{base}/tenders/{tid}" if base else ""
    return jsonify(
        {
            "ok": True,
            "tender_id": tid,
            "message": "Страница отчёта и блок «Оценка по сравнению» обновлены.",
            "report_url": report_url,
            "telegram_sent": telegram_sent,
        }
    )


if __name__ == "__main__":
    # Для ссылки из Telegram с телефона в той же Wi‑Fi: WEB_UI_HOST=0.0.0.0 и в .env
    # REPORT_SITE_PUBLIC_BASE_URL=http://<IP_ПК>:8765
    _host = (os.environ.get("WEB_UI_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    _port = int((os.environ.get("WEB_UI_PORT") or "8765").strip() or "8765")
    from autobot.estimate_publication_recovery import recover_pending_publications
    recover_pending_publications(REPORTS_DIR)
    from autobot.agent_market_delivery import start_delivery_recovery
    start_delivery_recovery()
    from autobot.market_web_worker import start_web_worker
    start_web_worker()
    from autobot.main_job_runtime import start_recovery
    start_recovery(_main_job_path(), env=_parse_env())
    app.run(host=_host, port=_port, debug=False)
