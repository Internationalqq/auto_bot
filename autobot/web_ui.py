from __future__ import annotations

from autobot.paths import REPO_ROOT
import io
import json
import os
import re
import subprocess
import uuid
import sys
import time
import traceback
import threading
import html as html_mod
from datetime import datetime
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
    jsonify,
    make_response,
    render_template_string,
    request,
    send_file,
    send_from_directory,
)

from autobot.site_public_url import get_report_site_public_base

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

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 26 * 1024 * 1024  # загрузка Excel обоснования НМЦК

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
estimate_market_jobs: dict[str, dict] = {}
estimate_market_lock = threading.Lock()


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


def _market_web_events_path(tender_id: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", (tender_id or "unknown").strip())[:80] or "unknown"
    return REPO_ROOT / "data" / "logs" / f"market_web_events_{safe}.jsonl"


def _read_market_web_events(tender_id: str, *, limit: int = 80) -> list[dict]:
    paths = [_market_web_events_path(tender_id), _market_web_events_path(tender_id)]
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
  <title>Помощник по госзакупкам</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect x='8' y='10' width='34' height='44' rx='8' fill='%23121a30' stroke='%236db7ff' stroke-width='3'/%3E%3Cpath d='M18 22h14M18 30h14M18 38h10' stroke='%239fd2ff' stroke-width='3' stroke-linecap='round'/%3E%3Ccircle cx='45' cy='42' r='10' fill='none' stroke='%235ecf8a' stroke-width='4'/%3E%3Cpath d='M52 49l6 6' stroke='%235ecf8a' stroke-width='4' stroke-linecap='round'/%3E%3C/svg%3E" />
  <style>
    :root {
      --bg: #0b1020;
      --panel: #121a30;
      --panel-soft: #10172b;
      --border: #27355d;
      --border-soft: #223154;
      --text: #e8ecf1;
      --muted: #9fb0d6;
      --muted-soft: #8a9bc4;
      --accent: #397ed1;
      --accent-2: #285da4;
      --accent-bright: #6db7ff;
      --ok: #5ecf8a;
      --danger: #a04048;
      --shadow: 0 16px 42px rgba(0, 0, 0, 0.3);
    }
    html, body { min-height: 100%; margin: 0; box-sizing: border-box; }
    *, *::before, *::after { box-sizing: inherit; }
    body { font-family: "Segoe UI", Arial, sans-serif; background: radial-gradient(1200px 700px at 20% -200px, #1c2b56 0%, var(--bg) 45%); color: var(--text); }
    .page { max-width: 960px; margin: 0 auto; padding: 22px 18px 40px; display: flex; flex-direction: column; }
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
      padding: 10px 11px;
      border-radius: 10px;
      border: 1px solid #2a3962;
      background: linear-gradient(145deg, #1a2442, #141d34);
      color: var(--text);
      transition: transform .15s ease, border-color .15s, box-shadow .15s;
      min-height: 0;
      min-width: 0;
      overflow: hidden;
    }
    .tender-card:hover { transform: translateY(-2px); border-color: #607dce; box-shadow: 0 8px 20px rgba(0,0,0,.28); }
    .tender-card[data-href] { cursor: pointer; }
    .tender-card.no-data { border-left: 3px solid var(--danger); }
    .tender-card-link {
      display: block;
      min-width: 0;
      text-decoration: none;
      color: inherit;
    }
    .tender-card-link--more { flex: 1 1 auto; margin-top: 2px; }
    .tender-card .title { font-size: 13px; font-weight: 650; line-height: 1.3; max-height: 3.9em; overflow: hidden; word-break: break-word; }
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
      padding: 8px 9px;
      border-radius: 10px;
      background: rgba(10, 18, 34, 0.52);
      border: 1px solid rgba(109, 183, 255, 0.12);
    }
    .tender-meta-item--wide { grid-column: 1 / -1; }
    .tender-meta-label {
      display: block;
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: #7d8fbb;
      margin-bottom: 4px;
    }
    .tender-meta-value {
      display: block;
      font-size: 12px;
      color: #edf3ff;
      line-height: 1.35;
      word-break: break-word;
    }
    .tender-meta-value--mono { font-variant-numeric: tabular-nums; color: #d9e7ff; }
    .tender-card-pub {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px 10px;
      margin-top: 10px;
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
      margin-top: 10px;
      padding: 9px 10px;
      border-radius: 10px;
      background: rgba(10, 18, 34, 0.58);
      border: 1px solid rgba(109, 183, 255, 0.16);
    }
    .tender-progress-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      font-size: 11px;
      color: #b7c8ea;
      margin-bottom: 7px;
    }
    .tender-progress-label { font-weight: 700; letter-spacing: 0.02em; }
    .tender-progress-value { color: #ecf2ff; font-weight: 700; font-variant-numeric: tabular-nums; }
    .tender-progress-track {
      height: 8px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.05);
    }
    .tender-progress-fill {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #4b7dff, #5ecf8a);
    }
    .tender-progress-note {
      margin-top: 7px;
      font-size: 11px;
      color: #92a6d2;
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
      width: 28px; height: 28px; border-radius: 7px; border: 1px solid #3a4677; background: rgba(15,19,36,.85);
      color: #c8d8f8; cursor: pointer; font-weight: 700; line-height: 1;
    }
    .tender-menu {
      display: none; position: absolute; top: 31px; right: 0; min-width: 260px; background: #13182b; border: 1px solid #2b365e;
      border-radius: 8px; padding: 6px; box-shadow: 0 10px 28px rgba(0,0,0,.45);
    }
    .tender-menu-wrap.menu-open .tender-menu { display: block; }
    .tender-menu button {
      width: 100%; text-align: left; background: transparent; color: #e8ecf1; border: none; padding: 8px; border-radius: 6px; cursor: pointer; font-size: 12px;
    }
    .tender-menu button:hover { background: rgba(255,255,255,.08); }
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
      display: flex; flex-direction: column; gap: 7px;
      margin-top: 12px; padding-top: 12px;
      border-top: 1px solid rgba(42, 57, 98, 0.65);
    }
    .tender-act {
      display: inline-flex; align-items: center; justify-content: center;
      border: 1px solid #3a4677; background: rgba(15, 19, 36, 0.85);
      color: #c8d8f8; border-radius: 9px; padding: 8px 10px;
      font-size: 12px; font-weight: 600; cursor: pointer; text-decoration: none;
      line-height: 1.25; text-align: center;
    }
    .tender-act:hover { background: rgba(75, 101, 187, 0.35); color: #fff; border-color: rgba(140, 175, 255, 0.55); }
    .tender-act:disabled { opacity: 0.45; cursor: not-allowed; transform: none; }
    .tender-act--primary { border-color: var(--accent); background: linear-gradient(180deg, #334b93, #2a3f82); color: #ecf2ff; }
    .tender-act--main { width: 100%; min-height: 43px; font-size: 13px; padding: 10px 12px; }
    .tender-next {
      margin: 0; font-size: 12px; color: var(--muted-soft); line-height: 1.45;
    }
    details.tender-more {
      border: 1px solid rgba(58, 70, 119, 0.72);
      border-radius: 8px;
      background: rgba(10, 14, 28, 0.38);
    }
    details.tender-more > summary {
      list-style: none; cursor: pointer; padding: 7px 9px;
      color: #a8b8e6; font-size: 12px; font-weight: 600; user-select: none;
    }
    details.tender-more > summary::-webkit-details-marker { display: none; }
    details.tender-more > summary::after { content: " +"; color: #7891cc; }
    details.tender-more[open] > summary::after { content: " -"; }
    details.tender-more[open] > summary {
      border-bottom: 1px solid rgba(58, 70, 119, 0.55); color: #d2defa;
    }
    .tender-more-actions {
      display: grid; grid-template-columns: 1fr; gap: 5px; padding: 7px;
    }
    .tender-more-actions .tender-act { width: 100%; justify-content: flex-start; text-align: left; }
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
      <a class="main-tab is-active" href="/">📋 Тендеры</a>
      <a class="main-tab" href="/estimates">📊 Сметы</a>
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
      <form class="tender-filter-row" method="get" action="/">
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
        <a href="/?sort={{ sort_mode }}{% if show_all %}&all=1{% endif %}">сбросить регион</a>
        {% endif %}
      </form>
      <p class="section-lead" style="margin-top:-4px;">
        Показано <strong>{{ visible_count }}</strong> из <strong>{{ tender_count }}</strong> тендеров
        {% if selected_region %}
        · регион: <strong>{{ selected_region }}</strong>
        {% endif %}
        {% if show_all %}
        · все этапы
        · <a href="/?sort={{ sort_mode }}{% if selected_region %}&region={{ selected_region|urlencode }}{% endif %}" style="color:#87bbff;">показать только «Подача заявок»</a>
        {% else %}
        · только этап «Подача заявок»
        · <a href="/?all=1&sort={{ sort_mode }}{% if selected_region %}&region={{ selected_region|urlencode }}{% endif %}" style="color:#87bbff;">показать все этапы</a>
        {% endif %}
      </p>

      {% for group in grouped %}
      <div class="tender-group">
        <div class="tender-group-title">{{ group.title }}</div>
        <div class="tender-group-body">
          <div class="tender-grid-main">
        {% for t in group.tenders %}
          <div class="tender-cell">
          <div class="tender-card{% if not t.has_estimate %} no-data{% endif %}" data-href="/merge-report/{{ t.tender_id }}/">
            {% if t.has_merge_report %}
            <a class="tender-card-link" href="/merge-report/{{ t.tender_id }}/">
              <div class="title">{{ t.display_title }}</div>
            </a>
            {% else %}
            <div class="tender-card-link tender-card-link--disabled">
              <div class="title">{{ t.display_title }}</div>
            </div>
            {% endif %}

            <div class="tender-card-meta">
              <div class="tender-meta-item tender-meta-item--wide">
                <span class="tender-meta-label">&#1058;&#1077;&#1085;&#1076;&#1077;&#1088;</span>
                <span class="tender-meta-value tender-meta-value--mono">{{ t.tender_id }}</span>
              </div>
              <div class="tender-meta-item">
                <span class="tender-meta-label">&#1057;&#1084;&#1077;&#1090;&#1072;</span>
                <span class="tender-meta-value">{% if t.has_estimate %}{{ t.estimate_rows }} &#1089;&#1090;&#1088;&#1086;&#1082;{% else %}&#1077;&#1097;&#1105; &#1085;&#1077; &#1089;&#1086;&#1073;&#1088;&#1072;&#1085;&#1072;{% endif %}</span>
              </div>
              <div class="tender-meta-item">
                <span class="tender-meta-label">&#1069;&#1090;&#1072;&#1087;</span>
                <span class="tender-meta-value">{{ t.stage_display }}</span>
              </div>
            </div>

            {% if t.market_progress_total > 0 %}
            <div class="tender-progress">
              <div class="tender-progress-head">
                <span class="tender-progress-label">&#1055;&#1086;&#1080;&#1089;&#1082; &#1094;&#1077;&#1085;</span>
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
              <p class="tender-next">Готово или частично готово: сохранённые строки Алисы будут вверху таблицы.</p>
              {% elif t.has_market_partial %}
              <a class="tender-act tender-act--primary tender-act--main" href="/merge-report/{{ t.tender_id }}/">Посмотреть частичные цены</a>
              <p class="tender-next">Есть сохранённый прогресс Алисы. Можно открыть карточку и продолжить поиск.</p>
              {% elif t.has_estimate %}
              <a class="tender-act tender-act--primary tender-act--main" href="/merge-report/{{ t.tender_id }}/">Открыть карточку тендера</a>
              <p class="tender-next">Смета готова. В карточке можно запустить поиск цен и смотреть сохранённые строки.</p>
              {% else %}
              <button type="button" class="tender-act tender-act--primary tender-act--main tender-act-btn" data-tid="{{ t.tender_id }}" onclick="runFullForTender('{{ t.tender_id }}')">Скачать документы и подготовить сравнение</button>
              <p class="tender-next">Смета не извлечена. Программа попробует скачать документы повторно.</p>
              {% endif %}
              <details class="tender-more">
                <summary>Дополнительные действия</summary>
                <div class="tender-more-actions">
                  <button type="button" class="tender-act tender-act-btn" data-tid="{{ t.tender_id }}" onclick="runFullForTender('{{ t.tender_id }}')" title="Продолжить поиск недостающих рыночных цен и заново собрать страницу сравнения.">Продолжить или обновить поиск цен</button>
                  <button type="button" class="tender-act tender-act-btn" data-tid="{{ t.tender_id }}" onclick="rerunMarketForTender('{{ t.tender_id }}')" title="Удалить прогресс поиска цен и опросить Алису по всем позициям заново.">Начать поиск цен заново</button>
                  <button type="button" class="tender-act tender-act-btn" data-tid="{{ t.tender_id }}" onclick="rebuildReportForTender('{{ t.tender_id }}')" title="Повторно прочитать уже скачанные документы. Поиск рыночных цен не запускается.">Повторно извлечь смету из файлов</button>
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
      </div>
    {% endfor %}
    {% if not grouped %}
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
        const r = await fetch("/api/start-parse", {
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
        const r = await fetch("/api/rebuild-report", {
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
        const r = await fetch("/api/rebuild-report", {
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
        const r = await fetch("/api/rebuild-all-reports", { method: "POST" });
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
        const match = line.match(/^\[([0-9]+)\/([0-9]+)\] ([0-9]+):/);
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
      const foundMatch = joined.match(/Итого после фильтров:\s*([0-9]+)/);
      const addedMatch = joined.match(/Добавлено в систему:\s*([0-9]+)/);
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


def collect_sidebar_tenders() -> tuple[list[dict], int, int, int]:
    """
    Все тендеры из tenders.json + признаки: есть файл отчёта и есть ли в нём блоки позиций
    (иначе внутри отчёта только «Нет данных для отображения»).
    """
    from autobot.merge_estimate_market import OUT_PREFIX

    meta = load_tender_metadata()
    reports_map = _html_reports_by_tender_id()
    rows_map = _estimate_rows_by_tender_id()
    merge_root = REPO_ROOT / "data" / "reports_site"
    items: list[dict] = []
    for tid, tmeta in meta.items():
        report_file = reports_map.get(tid, "")
        has_report = bool(report_file) and (REPORTS_DIR / report_file).is_file()
        if not has_report:
            report_file = ""
        rp = REPORTS_DIR / report_file if report_file else None
        has_display_data = bool(rp) and _smet_report_html_has_position_groups(rp)
        estimate_rows = int(rows_map.get(tid, 0))
        market_partial_exists = _price_output_path_for_tender(tid).is_file()
        merge_html_exists = (merge_root / tid / "index.html").is_file()
        svodka_exists = (REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx").is_file()
        market_done, market_total = _market_progress_for_tender(tid) if estimate_rows > 0 else (0, 0)
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
                "has_estimate": estimate_rows > 0,
                "has_merge_report": merge_html_exists or svodka_exists or market_partial_exists or estimate_rows > 0,
                "has_svodka": svodka_exists,
                "has_market_partial": market_partial_exists,
                "report_file": report_file,
                "stage_open": stage_open,
                "stage_display": stage_display,
                "estimate_rows": estimate_rows,
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


SIMPLE_INDEX_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Тендеры</title>
  <style>
    :root { color-scheme: dark; }
    body { margin:0; font-family: Segoe UI, Arial, sans-serif; background:#0f1724; color:#e8eefc; }
    .wrap { max-width: 1180px; margin: 0 auto; padding: 24px; }
    .top { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom: 18px; }
    .card { background:#182235; border:1px solid #2a3852; border-radius:16px; padding:18px; box-shadow: 0 10px 30px rgba(0,0,0,.18); }
    .muted { color:#9fb0d0; }
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
    .track { width:100%; height:10px; background:#0d1420; border-radius:999px; overflow:hidden; border:1px solid #2d3a52; }
    .fill { height:100%; background:linear-gradient(90deg, #4f8cff, #63d1ff); }
    .tags { display:flex; flex-wrap:wrap; gap:8px; margin:10px 0 14px; }
    .tag { font-size:12px; padding:5px 9px; border-radius:999px; background:#23314a; border:1px solid #32445f; }
    .tag.ok { background:#183725; border-color:#2b6842; color:#b8f0ca; }
    .tag.warn { background:#3b2e13; border-color:#7b5f17; color:#ffe08a; }
    .tag.bad { background:#3a1f24; border-color:#74404a; color:#ffb8c2; }
    .actions { display:flex; flex-wrap:wrap; gap:8px; }
    .btn { border:0; border-radius:10px; padding:10px 14px; cursor:pointer; font-size:14px; background:#305baf; color:#fff; }
    .btn.secondary { background:#26364f; color:#dce7ff; }
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
              <button class="btn secondary" type="button" onclick="runAction('/api/rebuild-report', '{{ t.tender_id }}', 'Пересобираю карточку…')">Пересобрать карточку</button>
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
    async function runAction(url, tenderId, startMessage) {
      try {
        const body = tenderId ? { tender_id: tenderId } : {};
        if (startMessage) alert(startMessage);
        const resp = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || data.message || ("HTTP " + resp.status));
        alert(data.message || "Команда отправлена.");
        location.reload();
      } catch (err) {
        alert("Ошибка: " + (err.message || err));
      }
    }
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    sidebar_items, tender_count, report_count, display_report_count = collect_sidebar_tenders()
    show_all = (request.args.get("all", "") or "").strip().lower() in ("1", "true", "yes", "on")
    sort_mode = (request.args.get("sort", "") or "publish_desc").strip().lower()
    if sort_mode not in ("publish_desc", "publish_asc"):
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
    visible_count = len(visible_items)
    grouped_map: dict[str, list[dict]] = {}
    for r in visible_items:
        grouped_map.setdefault(r["region"], []).append(r)

    grouped = [
        {"title": region, "tenders": items}
        for region, items in sorted(grouped_map.items(), key=lambda x: x[0])
    ]
    rebuild_options = [
        {"tender_id": x["tender_id"], "display_title": x["display_title"]} for x in sidebar_items
    ]
    coverage = _compute_reports_coverage()
    return render_template_string(
        INDEX_TEMPLATE,
        grouped=grouped,
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
    )


USER_ESTIMATES_DIR = REPO_ROOT / "data" / "user_estimates"
USER_ESTIMATES_INDEX = USER_ESTIMATES_DIR / "index.json"


def _estimate_upload_allowed(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in (".xlsx", ".xls", ".xlsm")


def _safe_upload_filename(filename: str) -> str:
    raw = Path(filename or "estimate.xlsx").name
    stem = Path(raw).stem
    suffix = Path(raw).suffix.lower()
    stem = re.sub(r"[^0-9A-Za-zА-Яа-я_. -]+", "_", stem).strip(" ._")[:80] or "estimate"
    if suffix not in (".xlsx", ".xls", ".xlsm"):
        suffix = ".xlsx"
    return f"{stem}{suffix}"


def _read_estimates_index() -> list[dict]:
    if not USER_ESTIMATES_INDEX.is_file():
        return []
    try:
        data = json.loads(USER_ESTIMATES_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _write_estimates_index(items: list[dict]) -> None:
    USER_ESTIMATES_DIR.mkdir(parents=True, exist_ok=True)
    USER_ESTIMATES_INDEX.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _estimate_meta_path(estimate_id: str) -> Path:
    return USER_ESTIMATES_DIR / estimate_id / "meta.json"


def _estimate_rows_path(estimate_id: str) -> Path:
    return USER_ESTIMATES_DIR / estimate_id / "rows.json"


def _estimate_market_raw_path(estimate_id: str) -> Path:
    return USER_ESTIMATES_DIR / estimate_id / "market_sources.xlsx"


def _estimate_market_merged_path(estimate_id: str) -> Path:
    return USER_ESTIMATES_DIR / estimate_id / "market_compare.xlsx"


def _load_estimate_meta(estimate_id: str) -> dict | None:
    p = _estimate_meta_path(estimate_id)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _load_estimate_rows(estimate_id: str) -> list[dict]:
    p = _estimate_rows_path(estimate_id)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


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


def _position_type(name: str, unit: str = "") -> tuple[str, str]:
    text = f"{name} {unit}".casefold().replace("ё", "е")
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
        return "service", "Услуга"
    if any(k in text for k in work_keys):
        return "work", "Работа"
    if any(k in text for k in material_keys):
        return "material", "Материал"
    if any(k in text for k in product_keys):
        return "product", "Товар/изделие"
    return "other", "Другое"


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{float(v):,.2f}".replace(",", " ").replace(".", ",") + " ₽"


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
    type_key, type_label = _position_type(row.name, row.unit)
    return {
        "idx": int(row.idx),
        "name": row.name,
        "unit": row.unit,
        "qty": _json_num(row.qty),
        "unit_price": _json_num(row.unit_price),
        "total": _json_num(row.total),
        "item_no": row.item_no,
        "sheet": row.sheet,
        "excel_row": row.excel_row,
        "section": row.section,
        "source": row.source,
        "type": type_key,
        "type_label": type_label,
    }


def _estimate_rows_to_report_df(rows: list[dict]) -> pd.DataFrame:
    from autobot.market_analytics import COL_ITEM, COL_NAME, COL_QTY, COL_SUM, COL_UNIT, COL_UNIT_PRICE

    data: list[dict] = []
    for r in rows:
        data.append(
            {
                COL_ITEM: str(r.get("item_no") or ""),
                COL_NAME: str(r.get("name") or ""),
                COL_UNIT: str(r.get("unit") or ""),
                COL_QTY: _json_num(r.get("qty")),
                COL_UNIT_PRICE: _json_num(r.get("unit_price")),
                COL_SUM: _json_num(r.get("total")),
                "Лист": str(r.get("sheet") or ""),
                "Строка Excel": r.get("excel_row"),
                "Раздел": str(r.get("section") or ""),
                "Тип": str(r.get("type_label") or ""),
            }
        )
    return pd.DataFrame(data)


def _merge_uploaded_estimate_market_df(est_df: pd.DataFrame, market_df: pd.DataFrame) -> pd.DataFrame:
    from autobot.market_analytics import COL_NAME, extract_ruble_amounts, recalc_estimate_qty_price_from_unit
    from autobot.merge_estimate_market import _agg_text, _norm_key, _normalize_market_columns

    est = recalc_estimate_qty_price_from_unit(est_df.copy())
    ali = _normalize_market_columns(market_df.copy())
    if COL_NAME not in est.columns or COL_NAME not in ali.columns:
        return est

    est["__merge_key"] = est[COL_NAME].map(_norm_key)
    ali["__merge_key"] = ali[COL_NAME].map(_norm_key)
    ali = ali.drop(columns=[COL_NAME], errors="ignore")
    agg_cols = [c for c in ali.columns if c != "__merge_key"]
    if agg_cols:
        ali = ali.groupby("__merge_key", as_index=False).agg({c: _agg_text for c in agg_cols})
    merged = est.merge(ali, on="__merge_key", how="left").drop(columns=["__merge_key"], errors="ignore")
    merged = recalc_estimate_qty_price_from_unit(merged)

    def _rub_line(txt) -> str:
        if txt is None or (isinstance(txt, float) and pd.isna(txt)):
            return ""
        amounts = extract_ruble_amounts(str(txt))
        if not amounts:
            return ""
        uniq = sorted({round(x, 2) for x in amounts})[:12]
        return "; ".join(f"{v:,.0f}".replace(",", " ") for v in uniq)

    if "Рыночные источники" in merged.columns:
        merged["Суммы из текста ответа (авто)"] = merged["Рыночные источники"].map(_rub_line)
    if "Цены за ед. (рынок, руб)" in merged.columns:
        strict_prices = merged["Цены за ед. (рынок, руб)"].fillna("").astype(str).str.strip()
        fallback = merged.get("Суммы из текста ответа (авто)", pd.Series([""] * len(merged), index=merged.index))
        merged["Рынок цены за ед. (итог)"] = strict_prices.where(strict_prices != "", fallback)
    elif "Суммы из текста ответа (авто)" in merged.columns:
        merged["Рынок цены за ед. (итог)"] = merged["Суммы из текста ответа (авто)"]
    return merged


def _estimate_upload_log_append(job: dict, line: str) -> None:
    logs = list(job.get("log_lines") or [])
    stamp = datetime.now().strftime("%H:%M:%S")
    logs.append(f"{stamp} · {line}")
    job["log_lines"] = logs[-20:]


def _estimate_market_log_append(job: dict, line: str) -> None:
    logs = list(job.get("log_lines") or [])
    stamp = datetime.now().strftime("%H:%M:%S")
    logs.append(f"{stamp} · {line}")
    job["log_lines"] = logs[-30:]


def _estimate_upload_set(job_id: str, **updates) -> None:
    with estimate_upload_lock:
        job = estimate_upload_jobs.get(job_id)
        if not job:
            return
        for key, value in updates.items():
            job[key] = value


def _estimate_market_set(estimate_id: str, **updates) -> None:
    with estimate_market_lock:
        job = estimate_market_jobs.get(estimate_id)
        if not job:
            return
        for key, value in updates.items():
            job[key] = value


def _estimate_market_cleanup(max_jobs: int = 16) -> None:
    with estimate_market_lock:
        items = sorted(
            estimate_market_jobs.items(),
            key=lambda kv: str(kv[1].get("started_at") or ""),
            reverse=True,
        )
        keep = dict(items[:max_jobs])
        estimate_market_jobs.clear()
        estimate_market_jobs.update(keep)


def _estimate_upload_progress_cb(job_id: str):
    def _cb(percent: int, stage: str, detail: str = "") -> None:
        with estimate_upload_lock:
            job = estimate_upload_jobs.get(job_id)
            if not job:
                return
            job["progress"] = max(int(job.get("progress") or 0), int(percent))
            job["stage"] = stage
            job["detail"] = detail
            _estimate_upload_log_append(job, f"{stage}" + (f": {detail}" if detail else ""))
    return _cb


def _estimate_upload_cleanup(max_jobs: int = 16) -> None:
    with estimate_upload_lock:
        items = sorted(
            estimate_upload_jobs.items(),
            key=lambda kv: str(kv[1].get("started_at") or ""),
            reverse=True,
        )
        keep = dict(items[:max_jobs])
        estimate_upload_jobs.clear()
        estimate_upload_jobs.update(keep)


def _run_estimate_upload_worker(job_id: str, *, estimate_id: str, title_raw: str, original_name: str, src_path: Path) -> None:
    try:
        from autobot.estimate_excel_analysis import load_estimate_session

        _estimate_upload_set(job_id, running=True, progress=max(30, int(estimate_upload_jobs.get(job_id, {}).get("progress") or 0)), stage="Файл получен", detail="Запускаю разбор Excel")
        _estimate_upload_set(job_id, updated_at=datetime.now().isoformat(timespec="seconds"))
        session = load_estimate_session(src_path, progress_cb=_estimate_upload_progress_cb(job_id))
        rows = [_estimate_row_to_dict(r) for r in session.rows]
        summary = _summarize_estimate_rows(rows)
        _estimate_upload_set(job_id, progress=97, stage="Сохраняю смету", detail="Записываю карточку и таблицу")
        meta = {
            "id": estimate_id,
            "title": (title_raw or Path(original_name).stem)[:160],
            "original_filename": original_name,
            "created_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "row_count": len(rows),
            "total_sum": summary.get("total_sum"),
            "source_path": str(src_path.relative_to(REPO_ROOT)),
        }
        _estimate_rows_path(estimate_id).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        _estimate_meta_path(estimate_id).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        index_items = [x for x in _read_estimates_index() if str(x.get("id") or "") != estimate_id]
        index_items.insert(0, meta)
        _write_estimates_index(index_items)
        with estimate_upload_lock:
            job = estimate_upload_jobs.get(job_id)
            if job:
                job["running"] = False
                job["ok"] = True
                job["progress"] = 100
                job["stage"] = "Готово"
                job["detail"] = f"Строк: {len(rows)} · смета сохранена"
                job["estimate_id"] = estimate_id
                job["ended_at"] = datetime.now().isoformat(timespec="seconds")
                _estimate_upload_log_append(job, f"Готово: смета сохранена, строк {len(rows)}")
    except Exception as e:
        with estimate_upload_lock:
            job = estimate_upload_jobs.get(job_id)
            if job:
                job["running"] = False
                job["ok"] = False
                job["error"] = str(e)[:500]
                job["stage"] = "Ошибка"
                job["detail"] = "Не удалось распарсить Excel или сохранить смету"
                job["ended_at"] = datetime.now().isoformat(timespec="seconds")
                _estimate_upload_log_append(job, f"Ошибка: {str(e)[:300]}")
    finally:
        _estimate_upload_cleanup()


def _run_estimate_market_worker(estimate_id: str, *, city: str, sources: list[str]) -> None:
    try:
        from autobot.market_analytics import COL_NAME
        from autobot.merge_estimate_market import _norm_key
        from autobot.real_market_scraper import (
            AvitoBrowserFetcher,
            _build_output_row,
            _compact_query,
            _eligible_rows,
            _merge_rows,
            _processed_keys,
            _read_previous,
            search_market,
        )

        meta = _load_estimate_meta(estimate_id) or {}
        rows_json = _load_estimate_rows(estimate_id)
        if not rows_json:
            raise ValueError("У этой сметы нет строк для поиска рынка.")
        est_df = _estimate_rows_to_report_df(rows_json)
        total_rows = len(_eligible_rows(est_df))
        raw_path = _estimate_market_raw_path(estimate_id)
        merged_path = _estimate_market_merged_path(estimate_id)
        prev = _read_previous(raw_path)
        done_keys = _processed_keys(prev)
        eligible = _eligible_rows(est_df)
        total = len(eligible)
        _estimate_market_set(
            estimate_id,
            running=True,
            progress=3,
            stage="Готовлю строки сметы",
            detail=f"К обработке: {total} строк" + (f" · город: {city}" if city else ""),
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
        use_browser = (os.environ.get("MARKET_AVITO_BROWSER", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
        browser_headless = (os.environ.get("MARKET_AVITO_HEADLESS", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
        max_results = max(1, min(10, int((os.environ.get("MARKET_MAX_RESULTS") or "5").strip() or "5")))
        pause = max(0.0, float((os.environ.get("MARKET_PAUSE_SEC") or "4").strip() or "4"))
        new_rows: list[dict] = []
        with AvitoBrowserFetcher(enabled=use_browser and "avito" in sources, headless=browser_headless) as browser:
            for seq, (_, row) in enumerate(eligible, start=1):
                work_name = str(row.get(COL_NAME, "") or "").strip()
                key = _norm_key(work_name)
                if key in done_keys:
                    continue
                query = _compact_query(work_name)
                _estimate_market_set(
                    estimate_id,
                    progress=max(4, min(96, int(round(((seq - 1) / max(1, total)) * 100)))),
                    stage="Ищу цены",
                    detail=f"{seq}/{total} · {work_name[:140]}",
                    current_item=work_name[:220],
                    done=max(0, len(done_keys) + len(new_rows)),
                    total=total,
                )
                with estimate_market_lock:
                    job = estimate_market_jobs.get(estimate_id)
                    if job:
                        _estimate_market_log_append(job, f"Поиск: {seq}/{total} · {work_name[:160]}")
                offers, err = search_market(
                    query,
                    region=city,
                    sources=sources,
                    max_results=max_results,
                    browser_fetcher=browser,
                )
                new_rows.append(_build_output_row(row, offers=offers, query=query, err=err))
                merged_raw = _merge_rows(prev, new_rows)
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                merged_raw.to_excel(raw_path, index=False)
                merged_df = _merge_uploaded_estimate_market_df(est_df, merged_raw)
                merged_df.to_excel(merged_path, index=False)
                detail = f"{len(offers)} ист." if offers else "ничего не найдено"
                if err and not offers:
                    detail += f" · {err[:120]}"
                with estimate_market_lock:
                    job = estimate_market_jobs.get(estimate_id)
                    if job:
                        _estimate_market_log_append(job, f"Готово: {seq}/{total} · {detail}")
                if pause > 0 and seq < total:
                    time.sleep(pause)
        if new_rows:
            merged_raw = _merge_rows(prev, new_rows)
        else:
            merged_raw = prev
        if merged_raw is None or (hasattr(merged_raw, "empty") and merged_raw.empty and not raw_path.is_file()):
            pd.DataFrame().to_excel(raw_path, index=False)
        else:
            merged_raw.to_excel(raw_path, index=False)
        _merge_uploaded_estimate_market_df(est_df, merged_raw).to_excel(merged_path, index=False)
        meta["market_city"] = city
        meta["market_sources"] = ",".join(sources)
        meta["market_updated_at"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        _estimate_meta_path(estimate_id).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        with estimate_market_lock:
            job = estimate_market_jobs.get(estimate_id)
            if job:
                job["running"] = False
                job["ok"] = True
                job["progress"] = 100
                job["stage"] = "Готово"
                job["detail"] = f"Поиск рынка завершён · строк: {total_rows}"
                job["done"] = total
                job["total"] = total
                job["ended_at"] = datetime.now().isoformat(timespec="seconds")
                job["has_raw"] = raw_path.is_file()
                job["has_merged"] = merged_path.is_file()
                _estimate_market_log_append(job, "Готово: файлы рынка сохранены")
    except Exception as e:
        with estimate_market_lock:
            job = estimate_market_jobs.get(estimate_id)
            if job:
                job["running"] = False
                job["ok"] = False
                job["stage"] = "Ошибка"
                job["detail"] = "Не удалось выполнить поиск рынка по этой смете"
                job["error"] = str(e)[:500]
                job["ended_at"] = datetime.now().isoformat(timespec="seconds")
                _estimate_market_log_append(job, f"Ошибка: {str(e)[:300]}")
    finally:
        _estimate_market_cleanup()


ESTIMATES_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Сметы</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1020; --panel:#121a30; --panel2:#0f1729; --border:#2a385f; --muted:#9fb0d6; --text:#e8eefc; --accent:#4f8cff; --ok:#5ecf8a; }
    body { margin:0; font-family: Segoe UI, Arial, sans-serif; background:radial-gradient(circle at top left,#17294f 0,#0b1020 42%,#070b15 100%); color:var(--text); }
    .page { max-width:1220px; margin:0 auto; padding:26px 18px 44px; }
    h1 { margin:0 0 8px; font-size:34px; }
    .sub,.muted { color:var(--muted); }
    .tabs { display:flex; flex-wrap:wrap; gap:8px; margin:14px 0 18px; }
    .tab { display:inline-flex; padding:9px 12px; border-radius:999px; color:#c8d8f8; text-decoration:none; background:rgba(15,22,44,.72); border:1px solid var(--border); font-weight:700; font-size:13px; }
    .tab.is-active { color:#fff; background:linear-gradient(180deg,#345095,#263d78); border-color:#6d8fe8; }
    .panel,.card { background:rgba(18,26,48,.92); border:1px solid var(--border); border-radius:16px; box-shadow:0 18px 45px rgba(0,0,0,.22); }
    .panel { padding:16px; margin-bottom:16px; }
    .upload-row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
    input[type=file], input[type=text], select { background:#0c1325; border:1px solid #33466f; color:var(--text); border-radius:10px; padding:10px; }
    input[type=text] { min-width:280px; }
    .btn { border:1px solid #5b7ddd; background:linear-gradient(180deg,#3b61ba,#294c9c); color:white; border-radius:10px; padding:10px 14px; font-weight:700; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; }
    .btn.secondary { background:#26364f; border-color:#3a4c70; color:#dce7ff; }
    .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(290px,1fr)); gap:12px; }
    .card { padding:14px; text-decoration:none; color:var(--text); }
    .card:hover { border-color:#6d8fe8; transform:translateY(-1px); }
    .card h3 { margin:0 0 9px; font-size:15px; line-height:1.35; }
    .meta { display:grid; grid-template-columns:1fr 1fr; gap:8px; font-size:12px; }
    .meta b { display:block; color:#fff; font-size:15px; margin-top:2px; }
    .table-wrap { overflow:auto; border-radius:14px; border:1px solid var(--border); background:#0c1325; }
    table { width:100%; border-collapse:collapse; font-size:12px; }
    th,td { padding:9px 10px; border-bottom:1px solid #223150; vertical-align:top; }
    th { position:sticky; top:0; background:#121c34; color:#bfd2ff; text-align:left; z-index:1; }
    tr:hover td { background:rgba(79,140,255,.07); }
    .num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
    .tag { display:inline-flex; border-radius:999px; padding:3px 8px; border:1px solid #3a4c70; background:#18243d; color:#cfe0ff; font-size:11px; }
    .summary { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin-top:12px; }
    .summary-item { padding:11px; background:#0c1325; border:1px solid #263858; border-radius:12px; }
    .summary-item span { display:block; color:var(--muted); font-size:11px; }
    .summary-item b { display:block; margin-top:4px; font-size:16px; }
    .upload-progress { margin-top:14px; padding:14px; border-radius:14px; border:1px solid #324770; background:linear-gradient(180deg, rgba(17,25,46,.95), rgba(10,16,31,.92)); }
    .upload-progress[hidden] { display:none; }
    .upload-progress-head { display:flex; justify-content:space-between; gap:10px; align-items:center; margin-bottom:8px; }
    .upload-progress-title { font-size:14px; font-weight:700; color:#eaf1ff; }
    .upload-progress-pct { font-size:13px; color:#9fd2ff; font-variant-numeric:tabular-nums; }
    .upload-progress-bar { height:12px; border-radius:999px; overflow:hidden; background:#0a1223; border:1px solid #2a3f61; }
    .upload-progress-fill { height:100%; width:0%; background:linear-gradient(90deg, #4f8cff, #5ecf8a); transition:width .28s ease; }
    .upload-progress-stage { margin-top:10px; color:#dbe7ff; font-size:13px; font-weight:700; }
    .upload-progress-detail { margin-top:5px; color:#9fb0d6; font-size:12px; line-height:1.45; }
    .upload-progress-steps { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
    .upload-step { border:1px solid #33466f; background:#14203a; color:#99add7; border-radius:999px; padding:4px 9px; font-size:11px; }
    .upload-step.is-active { color:#fff; border-color:#5b7ddd; background:#23437f; }
    .upload-step.is-done { color:#dff7e6; border-color:#3f8a5f; background:#1c3b29; }
    .upload-progress-error { margin-top:10px; color:#ffbfca; font-size:12px; white-space:pre-wrap; }
    .upload-progress-logs { margin-top:10px; border-radius:10px; border:1px solid #263858; background:#09111f; padding:9px; max-height:180px; overflow:auto; font-size:11px; color:#aebfe4; line-height:1.45; white-space:pre-wrap; }
    .empty { text-align:center; padding:28px; color:var(--muted); }
    @media (max-width:720px){ h1{font-size:28px}.upload-row{align-items:stretch;flex-direction:column}.btn,input[type=text],select{width:100%;box-sizing:border-box}.meta{grid-template-columns:1fr} }
  </style>
</head>
<body>
  <div class="page">
    <h1>Сметы</h1>
    <div class="sub">Загрузите Excel-смету, сохраните её карточкой и смотрите все найденные позиции в таблице.</div>
    <nav class="tabs">
      <a class="tab" href="/">📋 Тендеры</a>
      <a class="tab is-active" href="/estimates">📊 Сметы</a>
    </nav>

    <section class="panel">
      <h2 style="margin:0 0 10px;font-size:18px;">Загрузить Excel-смету</h2>
      <form id="estimateUploadForm" class="upload-row">
        <input type="file" name="file" accept=".xlsx,.xls,.xlsm,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" required />
        <input type="text" name="title" placeholder="Название сметы (необязательно)" />
        <button class="btn" type="submit">Загрузить и распарсить</button>
      </form>
      <div id="uploadStatus" class="muted" style="margin-top:10px;"></div>
      <div id="estimateUploadProgress" class="upload-progress" hidden>
        <div class="upload-progress-head">
          <div class="upload-progress-title" id="estimateUploadStage">Подготовка…</div>
          <div class="upload-progress-pct" id="estimateUploadPct">0%</div>
        </div>
        <div class="upload-progress-bar"><div id="estimateUploadFill" class="upload-progress-fill"></div></div>
        <div id="estimateUploadDetail" class="upload-progress-detail"></div>
        <div class="upload-progress-steps" id="estimateUploadSteps">
          <span class="upload-step" data-step="upload">Отправка файла</span>
          <span class="upload-step" data-step="received">Файл получен</span>
          <span class="upload-step" data-step="parse">Разбор Excel</span>
          <span class="upload-step" data-step="catalogue">Каталог позиций</span>
          <span class="upload-step" data-step="save">Сохранение</span>
          <span class="upload-step" data-step="done">Готово</span>
        </div>
        <div id="estimateUploadError" class="upload-progress-error" hidden></div>
        <div id="estimateUploadLogs" class="upload-progress-logs" hidden></div>
      </div>
    </section>

    <section class="panel">
      <h2 style="margin:0 0 10px;font-size:18px;">Список смет</h2>
      {% if estimates %}
      <div class="grid">
        {% for e in estimates %}
        <a class="card" href="/estimates/{{ e.id }}">
          <h3>{{ e.title }}</h3>
          <div class="muted" style="font-size:12px;margin-bottom:10px;">{{ e.original_filename }}</div>
          <div class="meta">
            <div><span class="muted">Строк</span><b>{{ e.row_count }}</b></div>
            <div><span class="muted">Сумма</span><b>{{ e.total_sum_fmt }}</b></div>
            <div><span class="muted">Загружено</span><b style="font-size:12px;">{{ e.created_at }}</b></div>
            <div><span class="muted">Типы</span><b style="font-size:12px;">{{ e.types_text }}</b></div>
          </div>
        </a>
        {% endfor %}
      </div>
      {% else %}
      <div class="empty">Пока нет загруженных смет.</div>
      {% endif %}
    </section>
  </div>
  <script>
    (function() {
      const form = document.getElementById("estimateUploadForm");
      const status = document.getElementById("uploadStatus");
      const panel = document.getElementById("estimateUploadProgress");
      const fill = document.getElementById("estimateUploadFill");
      const pct = document.getElementById("estimateUploadPct");
      const stage = document.getElementById("estimateUploadStage");
      const detail = document.getElementById("estimateUploadDetail");
      const errBox = document.getElementById("estimateUploadError");
      const logs = document.getElementById("estimateUploadLogs");
      const stepNodes = Array.from(document.querySelectorAll("#estimateUploadSteps .upload-step"));
      let activePoll = 0;

      function showProgress() {
        panel.hidden = false;
        logs.hidden = false;
      }

      function markStep(progress, currentStage, done) {
        const stageLow = String(currentStage || "").toLowerCase();
        const currentKey =
          done ? "done"
          : progress < 25 ? "upload"
          : progress < 40 ? "received"
          : stageLow.includes("каталог") ? "catalogue"
          : stageLow.includes("сохраня") ? "save"
          : progress >= 40 ? "parse"
          : "received";
        const order = ["upload", "received", "parse", "catalogue", "save", "done"];
        const currentIndex = order.indexOf(currentKey);
        stepNodes.forEach(function(node) {
          const key = node.getAttribute("data-step");
          const idx = order.indexOf(key);
          node.classList.toggle("is-done", idx >= 0 && idx < currentIndex);
          node.classList.toggle("is-active", key === currentKey);
        });
      }

      function renderProgress(data) {
        const value = Math.max(0, Math.min(100, Number(data.progress || 0)));
        fill.style.width = value + "%";
        pct.textContent = value + "%";
        stage.textContent = data.stage || "Подготовка…";
        detail.textContent = data.detail || "";
        const resultOk = !!data.result_ok;
        status.textContent = data.running ? "Смета обрабатывается…" : (resultOk ? "Смета готова." : (data.error ? "Во время обработки возникла ошибка." : ""));
        if (Array.isArray(data.log_tail) && data.log_tail.length) {
          logs.hidden = false;
          logs.textContent = data.log_tail.join("\\n");
        } else {
          logs.hidden = true;
          logs.textContent = "";
        }
        if (data.error) {
          errBox.hidden = false;
          errBox.textContent = "Ошибка: " + data.error;
        } else {
          errBox.hidden = true;
          errBox.textContent = "";
        }
        markStep(value, data.stage || "", resultOk && !data.running);
      }

      async function pollJob(jobId) {
        const pollId = ++activePoll;
        for (;;) {
          if (pollId !== activePoll) return;
          let resp, data;
          try {
            resp = await fetch("/api/estimates/upload-status/" + encodeURIComponent(jobId), { cache: "no-store" });
            data = await resp.json();
          } catch (e) {
            status.textContent = "Не удалось обновить статус обработки.";
            return;
          }
          if (!resp.ok || !data.ok) {
            status.textContent = (data && data.message) || "Статус обработки недоступен.";
            return;
          }
          renderProgress(data);
          if (!data.running) {
            if (data.result_ok && data.estimate_id) {
              status.textContent = "Готово, открываю смету…";
              setTimeout(function() { location.href = "/estimates/" + data.estimate_id; }, 450);
            }
            return;
          }
          await new Promise(function(resolve) { setTimeout(resolve, 500); });
        }
      }

      form.addEventListener("submit", function(e) {
        e.preventDefault();
        const fd = new FormData(form);
        const xhr = new XMLHttpRequest();
        activePoll += 1;
        showProgress();
        errBox.hidden = true;
        errBox.textContent = "";
        logs.textContent = "";
        logs.hidden = true;
        status.textContent = "Начинаю загрузку файла…";
        renderProgress({ progress: 2, stage: "Отправляю файл", detail: "Загружаю Excel на сервер", running: true, result_ok: false, log_tail: [] });
        xhr.open("POST", "/api/estimates/upload");
        xhr.upload.addEventListener("progress", function(ev) {
          if (!ev.lengthComputable) return;
          showProgress();
          const uploadPct = Math.max(2, Math.min(24, Math.round(ev.loaded / ev.total * 24)));
          renderProgress({ progress: uploadPct, stage: "Отправляю файл", detail: "Передано " + ev.loaded + " из " + ev.total + " байт", running: true, result_ok: false, log_tail: [] });
        });
        xhr.onreadystatechange = function() {
          if (xhr.readyState !== 4) return;
          let data = {};
          try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) {}
          if (xhr.status < 200 || xhr.status >= 300 || !data.ok) {
            showProgress();
            renderProgress({
              progress: 100,
              stage: "Ошибка",
              detail: "Загрузка или запуск обработки не удались",
              running: false,
              result_ok: false,
              error: data.message || ("HTTP " + xhr.status),
              log_tail: []
            });
            status.textContent = "Ошибка: " + (data.message || ("HTTP " + xhr.status));
            return;
          }
          showProgress();
          renderProgress({
            progress: Math.max(26, Number(data.progress || 26)),
            stage: data.stage || "Файл получен",
            detail: data.detail || "Сервер принял файл и начал разбор",
            running: true,
            result_ok: false,
            log_tail: data.log_tail || []
          });
          status.textContent = "Файл получен, идёт разбор сметы…";
          pollJob(data.job_id);
        };
        xhr.send(fd);
      });
    })();
  </script>
</body>
</html>
"""


ESTIMATE_DETAIL_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{{ meta.title }} · Смета</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1020; --panel:#121a30; --border:#2a385f; --muted:#9fb0d6; --text:#e8eefc; --accent:#4f8cff; }
    body { margin:0; font-family: Segoe UI, Arial, sans-serif; background:#0b1020; color:var(--text); }
    .page { max-width:1380px; margin:0 auto; padding:24px 16px 42px; }
    a { color:#9fc2ff; }
    h1 { margin:0 0 8px; font-size:28px; }
    .muted { color:var(--muted); }
    .panel { background:rgba(18,26,48,.94); border:1px solid var(--border); border-radius:16px; padding:15px; margin-bottom:14px; }
    .filters { display:flex; flex-wrap:wrap; gap:10px; align-items:end; }
    label { display:grid; gap:5px; color:var(--muted); font-size:12px; }
    input,select { background:#0c1325; border:1px solid #33466f; color:var(--text); border-radius:10px; padding:10px; min-width:220px; }
    .btn { border:1px solid #5b7ddd; background:linear-gradient(180deg,#3b61ba,#294c9c); color:white; border-radius:10px; padding:10px 14px; font-weight:700; cursor:pointer; text-decoration:none; }
    .btn.secondary { background:#26364f; border-color:#3a4c70; color:#dce7ff; }
    .actions-row { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
    .type-picker { margin-top:12px; padding:14px; background:#10182c; border:1px solid #2a3a5c; border-radius:14px; }
    .type-picker-title { margin:0 0 10px; font-size:14px; font-weight:700; color:#dce7ff; }
    .type-checks { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:10px; }
    .type-check { display:flex; align-items:flex-start; gap:10px; padding:11px 12px; background:#0c1325; border:1px solid #2b3d60; border-radius:12px; min-height:58px; }
    .type-check input { min-width:18px; width:18px; height:18px; margin-top:2px; accent-color:#5f8fff; }
    .type-check strong { display:block; font-size:14px; color:#eef4ff; }
    .type-check span { display:block; color:#9fb0d6; font-size:12px; margin-top:2px; }
    .summary { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; }
    .summary-item { padding:11px; background:#0c1325; border:1px solid #263858; border-radius:12px; }
    .summary-item span { display:block; color:var(--muted); font-size:11px; }
    .summary-item b { display:block; margin-top:4px; font-size:16px; }
    .table-wrap { overflow:auto; border-radius:14px; border:1px solid var(--border); background:#0c1325; }
    table { width:100%; border-collapse:collapse; font-size:12px; min-width:980px; }
    th,td { padding:8px 9px; border-bottom:1px solid #223150; vertical-align:top; }
    th { position:sticky; top:0; background:#121c34; color:#bfd2ff; text-align:left; z-index:1; }
    tr:hover td { background:rgba(79,140,255,.07); }
    tr.section-row td { background:#14213d; color:#eaf1ff; font-weight:700; border-bottom-color:#31456d; }
    tr.section-row:hover td { background:#14213d; }
    tr.sheet-total-row td { background:#16233f; color:#ffd8d8; font-weight:700; border-top:1px solid #6f3d4a; border-bottom:1px solid #6f3d4a; }
    tr.sheet-break-row td { background:#4a1f2a; border-bottom:0; height:14px; padding:0; }
    .num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
    .name { min-width:360px; }
    .tag { display:inline-flex; border-radius:999px; padding:3px 8px; border:1px solid #3a4c70; background:#18243d; color:#cfe0ff; font-size:11px; white-space:nowrap; }
    .where { color:#91a3c8; font-size:11px; line-height:1.35; }
    .market-box { display:grid; gap:10px; }
    .status-box { background:#0c1325; border:1px solid #263858; border-radius:12px; padding:12px; }
    .status-line { color:var(--muted); font-size:12px; }
    .logs { margin:0; background:#0a1020; border:1px solid #223150; border-radius:12px; padding:10px; max-height:200px; overflow:auto; white-space:pre-wrap; font-size:12px; color:#dbe6ff; }
    @media (max-width:760px){ .filters{align-items:stretch;flex-direction:column}.btn,input,select{width:100%;box-sizing:border-box} }
  </style>
</head>
<body>
  <div class="page">
    <p style="margin:0 0 10px;"><a href="/estimates">← Все сметы</a> · <a href="/">Тендеры</a></p>
    <h1>{{ meta.title }}</h1>
    <div class="muted">{{ meta.original_filename }} · загружено {{ meta.created_at }} · всего строк {{ meta.row_count }}</div>

    <section class="panel">
      <form class="filters" method="get" action="/estimates/{{ meta.id }}" id="estimateFilterForm">
        <label>Поиск по наименованию
          <input type="text" name="q" value="{{ q }}" placeholder="например: бетон, демонтаж, труба" />
        </label>
        <button class="btn" type="submit">Применить</button>
        <a class="btn secondary" href="/estimates/{{ meta.id }}">Сбросить</a>
        <a class="btn" href="/estimates/{{ meta.id }}/download.xlsx?{{ filter_query }}">Скачать Excel</a>
      </form>
      <div class="type-picker">
        <div class="type-picker-title">Фильтр по типам позиций</div>
        <div class="type-checks">
          {% for opt in type_options %}
          <label class="type-check">
            <input type="checkbox" name="types" value="{{ opt.key }}" form="estimateFilterForm" {% if opt.key in selected_types %}checked{% endif %} />
            <span>
              <strong>{{ opt.label }}</strong>
              <span>Строк: {{ opt.count }}</span>
            </span>
          </label>
          {% endfor %}
        </div>
      </div>
      <div class="summary" style="margin-top:12px;">
        <div class="summary-item"><span>Строк после фильтра</span><b>{{ summary.row_count }}</b></div>
        <div class="summary-item"><span>Общее количество / объём</span><b>{{ summary.qty_text }}</b></div>
        <div class="summary-item"><span>Общая сумма</span><b>{{ summary.total_sum_fmt }}</b></div>
        <div class="summary-item"><span>Средняя цена</span><b>{{ summary.avg_price_fmt }}</b></div>
      </div>
    </section>

    <section class="panel">
      <div class="market-box">
        <div>
          <h2 style="margin:0 0 8px;">Поиск цен по этой смете</h2>
          <div class="muted">Ищем цены по интернету и на Авито для строк этой сметы. Можно указать город, чтобы сузить выдачу.</div>
        </div>
        <div class="filters">
          <label>Город для поиска
            <input type="text" id="marketCityInput" value="{{ market_city }}" placeholder="например: Челябинск" />
          </label>
          <button class="btn" type="button" id="marketStartBtn" onclick="startEstimateMarket()">Найти цены</button>
          <a class="btn secondary" id="marketMergedBtn" href="/estimates/{{ meta.id }}/market-compare.xlsx" {% if not has_market_merged %}hidden{% endif %}>Скачать сравнение рынка</a>
          <a class="btn secondary" id="marketRawBtn" href="/estimates/{{ meta.id }}/market-sources.xlsx" {% if not has_market_raw %}hidden{% endif %}>Скачать источники рынка</a>
        </div>
        <div class="status-box">
          <div id="marketStatusMain">Пока поиск рынка не запускался.</div>
          <div class="status-line" id="marketStatusDetail"></div>
        </div>
        <pre class="logs" id="marketLogs">—</pre>
      </div>
    </section>

    <section class="panel">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>№</th>
              <th>Тип</th>
              <th class="name">Наименование</th>
              <th>Ед.</th>
              <th class="num">Кол-во</th>
              <th class="num">Цена за ед.</th>
              <th class="num">Сумма</th>
              <th>Где найдено</th>
            </tr>
          </thead>
          <tbody>
            {% for r in rows %}
            {% if r._is_section %}
            <tr class="section-row">
              <td colspan="8">{{ r.section_title }}</td>
            </tr>
            {% elif r._is_sheet_total %}
            <tr class="sheet-total-row">
              <td colspan="6">Итого по листу: {{ r.sheet_title or "без названия" }}</td>
              <td class="num">{{ r.sheet_total_fmt }}</td>
              <td></td>
            </tr>
            {% elif r._is_sheet_break %}
            <tr class="sheet-break-row">
              <td colspan="8"></td>
            </tr>
            {% else %}
            <tr>
              <td>{{ r.display_no }}</td>
              <td><span class="tag">{{ r.type_label }}</span></td>
              <td class="name">{{ r.name }}</td>
              <td>{{ r.unit or "—" }}</td>
              <td class="num">{{ r.qty_fmt }}</td>
              <td class="num">{{ r.unit_price_fmt }}</td>
              <td class="num">{{ r.total_fmt }}</td>
              <td class="where">
                {% if r.sheet %}лист: {{ r.sheet }}<br>{% endif %}
                {% if r.excel_row %}строка Excel: {{ r.excel_row }}<br>{% endif %}
              </td>
            </tr>
            {% endif %}
            {% endfor %}
            {% if not rows %}
            <tr><td colspan="8" style="text-align:center;color:#9fb0d6;padding:24px;">По фильтру ничего не найдено.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    </section>
  </div>
  <script>
    async function refreshEstimateMarketStatus() {
      try {
        const resp = await fetch("/api/estimates/{{ meta.id }}/market-status");
        if (!resp.ok) return;
        const data = await resp.json();
        const main = document.getElementById("marketStatusMain");
        const detail = document.getElementById("marketStatusDetail");
        const logs = document.getElementById("marketLogs");
        const startBtn = document.getElementById("marketStartBtn");
        const mergedBtn = document.getElementById("marketMergedBtn");
        const rawBtn = document.getElementById("marketRawBtn");
        if (startBtn) startBtn.disabled = !!data.running;
        if (mergedBtn) mergedBtn.hidden = !data.has_merged;
        if (rawBtn) rawBtn.hidden = !data.has_raw;
        if (main) {
          if (data.running) {
            main.textContent = "Идёт поиск цен: " + (data.done || 0) + " / " + (data.total || 0);
          } else if (data.result_ok) {
            main.textContent = "Поиск рынка завершён.";
          } else if (data.error) {
            main.textContent = "Поиск завершился с ошибкой.";
          } else {
            main.textContent = "Пока поиск рынка не запускался.";
          }
        }
        if (detail) {
          const bits = [];
          if (data.stage) bits.push(data.stage);
          if (data.detail) bits.push(data.detail);
          if (data.city) bits.push("город: " + data.city);
          detail.textContent = bits.join(" · ");
        }
        if (logs) {
          const arr = Array.isArray(data.log_tail) ? data.log_tail : [];
          logs.textContent = arr.length ? arr.join("\\n") : "—";
          logs.scrollTop = logs.scrollHeight;
        }
      } catch (e) {}
    }

    async function startEstimateMarket() {
      const cityInput = document.getElementById("marketCityInput");
      const city = cityInput ? String(cityInput.value || "").trim() : "";
      const btn = document.getElementById("marketStartBtn");
      if (btn) btn.disabled = true;
      try {
        const resp = await fetch("/api/estimates/{{ meta.id }}/market-start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ city })
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
          alert(data.message || "Не удалось запустить поиск рынка");
        }
      } catch (e) {
        alert("Не удалось запустить поиск рынка");
      } finally {
        refreshEstimateMarketStatus();
      }
    }

    refreshEstimateMarketStatus();
    setInterval(refreshEstimateMarketStatus, 3000);
  </script>
</body>
</html>
"""


@app.route("/estimates")
def estimates_page():
    cards = []
    for meta in _read_estimates_index():
        if not isinstance(meta, dict):
            continue
        rows = _load_estimate_rows(str(meta.get("id") or ""))
        summary = _summarize_estimate_rows(rows)
        type_counts = summary.get("type_counts") or {}
        labels = {"work": "работ", "service": "услуг", "product": "товаров", "material": "материалов", "other": "другое"}
        types_text = ", ".join(f"{labels.get(k, k)}: {v}" for k, v in type_counts.items()) or "—"
        item = dict(meta)
        item["total_sum_fmt"] = _fmt_money(summary.get("total_sum"))
        item["types_text"] = types_text
        cards.append(item)
    cards.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return render_template_string(ESTIMATES_TEMPLATE, estimates=cards)


@app.route("/estimates/<estimate_id>")
def estimate_detail_page(estimate_id: str):
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
    return render_template_string(
        ESTIMATE_DETAIL_TEMPLATE,
        meta=meta,
        rows=rows_view,
        q=q,
        selected_types=selected_types,
        filter_query=filter_query,
        market_city=str(meta.get("market_city") or ""),
        has_market_raw=_estimate_market_raw_path(estimate_id).is_file(),
        has_market_merged=_estimate_market_merged_path(estimate_id).is_file(),
        type_options=type_options,
        summary=summary,
    )


@app.route("/estimates/<estimate_id>/download.xlsx")
def estimate_detail_download_xlsx(estimate_id: str):
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
    path = _estimate_market_raw_path(estimate_id)
    meta = _load_estimate_meta(estimate_id) or {}
    if not path.is_file():
        abort(404)
    filename = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._ -]+", "_", str(meta.get("title") or estimate_id)).strip(" ._") or estimate_id
    return send_file(path, as_attachment=True, download_name=f"{filename} - источники рынка.xlsx", max_age=0)


@app.route("/estimates/<estimate_id>/market-compare.xlsx")
def estimate_market_compare_download_xlsx(estimate_id: str):
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    path = _estimate_market_merged_path(estimate_id)
    meta = _load_estimate_meta(estimate_id) or {}
    if not path.is_file():
        abort(404)
    filename = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._ -]+", "_", str(meta.get("title") or estimate_id)).strip(" ._") or estimate_id
    return send_file(path, as_attachment=True, download_name=f"{filename} - сравнение рынка.xlsx", max_age=0)


@app.route("/api/estimates/<estimate_id>/market-status")
def api_estimate_market_status(estimate_id: str):
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    meta = _load_estimate_meta(estimate_id) or {}
    with estimate_market_lock:
        job = dict(estimate_market_jobs.get(estimate_id) or {})
    payload = {
        "ok": True,
        "running": bool(job.get("running")),
        "result_ok": bool(job.get("ok")),
        "progress": int(job.get("progress") or 0),
        "stage": str(job.get("stage") or ""),
        "detail": str(job.get("detail") or ""),
        "error": str(job.get("error") or ""),
        "done": int(job.get("done") or 0),
        "total": int(job.get("total") or 0),
        "city": str(job.get("city") or meta.get("market_city") or ""),
        "log_tail": list(job.get("log_lines") or []),
        "started_at": job.get("started_at"),
        "ended_at": job.get("ended_at"),
        "has_raw": _estimate_market_raw_path(estimate_id).is_file(),
        "has_merged": _estimate_market_merged_path(estimate_id).is_file(),
    }
    return jsonify(payload)


@app.route("/api/estimates/<estimate_id>/market-start", methods=["POST"])
def api_estimate_market_start(estimate_id: str):
    estimate_id = re.sub(r"[^0-9a-fA-F-]", "", estimate_id or "")[:40]
    meta = _load_estimate_meta(estimate_id)
    if not meta:
        return jsonify({"ok": False, "message": "Смета не найдена."}), 404
    rows = _load_estimate_rows(estimate_id)
    if not rows:
        return jsonify({"ok": False, "message": "У этой сметы нет строк для поиска рынка."}), 400
    with estimate_market_lock:
        cur = estimate_market_jobs.get(estimate_id)
        if cur and cur.get("running"):
            return jsonify({"ok": False, "message": "Поиск рынка уже выполняется."}), 409
    data = request.get_json(silent=True) or {}
    city = re.sub(r"\s+", " ", str(data.get("city") or "").strip())[:120]
    sources = ["avito", "web"]
    with estimate_market_lock:
        estimate_market_jobs[estimate_id] = {
            "running": True,
            "ok": False,
            "progress": 1,
            "stage": "Старт",
            "detail": "Запускаю поиск цен по строкам сметы",
            "error": "",
            "city": city,
            "sources": ",".join(sources),
            "done": 0,
            "total": 0,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "ended_at": None,
            "log_lines": [f"{datetime.now().strftime('%H:%M:%S')} · Старт поиска рынка" + (f" · город: {city}" if city else "")],
        }
    threading.Thread(
        target=_run_estimate_market_worker,
        kwargs={"estimate_id": estimate_id, "city": city, "sources": sources},
        daemon=True,
    ).start()
    return jsonify({"ok": True, "message": "Поиск рынка запущен."})


@app.route("/api/estimates/upload", methods=["POST"])
def api_estimates_upload():
    f = request.files.get("file")
    if not f or not getattr(f, "filename", None):
        return jsonify({"ok": False, "message": "Выберите Excel-файл со сметой."}), 400
    if not _estimate_upload_allowed(f.filename):
        return jsonify({"ok": False, "message": "Нужен Excel-файл: .xlsx, .xls или .xlsm."}), 400
    estimate_id = uuid.uuid4().hex[:16]
    job_id = uuid.uuid4().hex[:16]
    est_dir = USER_ESTIMATES_DIR / estimate_id
    est_dir.mkdir(parents=True, exist_ok=True)
    original_name = _safe_upload_filename(f.filename)
    src_path = est_dir / original_name
    f.save(src_path)
    title_raw = (request.form.get("title", "") or "").strip()
    with estimate_upload_lock:
        estimate_upload_jobs[job_id] = {
            "job_id": job_id,
            "estimate_id": None,
            "running": True,
            "ok": False,
            "progress": 26,
            "stage": "Файл получен",
            "detail": "Сохраняю Excel и запускаю разбор",
            "error": "",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "ended_at": None,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "log_lines": [f"{datetime.now().strftime('%H:%M:%S')} · Файл получен: {original_name}"],
        }
    threading.Thread(
        target=_run_estimate_upload_worker,
        kwargs={
            "job_id": job_id,
            "estimate_id": estimate_id,
            "title_raw": title_raw,
            "original_name": original_name,
            "src_path": src_path,
        },
        daemon=True,
    ).start()
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "progress": 26,
            "stage": "Файл получен",
            "detail": "Сервер принял файл и начал разбор",
            "message": "Смета загружена на сервер.",
        }
    )


@app.route("/api/estimates/upload-status/<job_id>")
def api_estimates_upload_status(job_id: str):
    with estimate_upload_lock:
        job = dict(estimate_upload_jobs.get(job_id) or {})
    if not job:
        return jsonify({"ok": False, "message": "Статус загрузки не найден."}), 404
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "estimate_id": job.get("estimate_id"),
            "running": bool(job.get("running")),
            "result_ok": bool(job.get("ok")),
            "progress": int(job.get("progress") or 0),
            "stage": job.get("stage") or "",
            "detail": job.get("detail") or "",
            "error": job.get("error") or "",
            "started_at": job.get("started_at"),
            "ended_at": job.get("ended_at"),
            "log_tail": list(job.get("log_lines") or [])[-12:],
        }
    )


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
  <title>{{ title|e }} — таблица НМЦК</title>
  <style>
    :root {
      --bg: #0b1020;
      --panel: #121a30;
      --border: #27355d;
      --text: #e8ecf1;
      --muted: #9fb0d6;
      --accent: #4b65bb;
    }
    html, body { margin: 0; min-height: 100%; background: radial-gradient(1200px 700px at 20% -200px, #1c2b56 0%, var(--bg) 45%); color: var(--text); font-family: Segoe UI, Arial, sans-serif; }
    .page { max-width: 100%; padding: 18px 16px 32px; box-sizing: border-box; }
    .head { max-width: 1400px; margin: 0 auto 14px; }
    h1 { font-size: 1.2rem; font-weight: 700; margin: 0 0 6px 0; letter-spacing: -0.02em; line-height: 1.35; word-break: break-word; }
    .sub { font-size: 13px; color: var(--muted); margin: 0 0 12px 0; }
    a.back { color: #87bbff; font-size: 13px; text-decoration: none; }
    a.back:hover { text-decoration: underline; }
    .table-shell {
      max-width: 1400px;
      margin: 0 auto;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: linear-gradient(180deg, var(--panel), #10172b);
      overflow: hidden;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.28);
    }
    .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
    table { border-collapse: collapse; width: 100%; font-size: 12px; }
    th, td {
      border: 1px solid #223154;
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
      line-height: 1.35;
    }
    th {
      background: #1a2442;
      color: #d2defa;
      font-weight: 600;
      max-width: 28em;
      word-break: break-word;
    }
    tbody tr:nth-child(even) { background: rgba(10, 14, 28, 0.35); }
    tbody tr:hover { background: rgba(75, 101, 187, 0.12); }
    td { word-break: break-word; max-width: 36em; }
    td.num { font-variant-numeric: tabular-nums; white-space: nowrap; max-width: none; }
    .foot { max-width: 1400px; margin: 14px auto 0; font-size: 11px; color: #8a9bc4; }
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
  <title>Сравнение цен ещё не готово</title>
  <style>
    body { font-family: Segoe UI, sans-serif; background:#0f1220; color:#e8ecf1; margin:0; padding:24px; line-height:1.5; }
    a { color:#7eb8ff; }
    .box { max-width:580px; margin:0 auto; border:1px solid #2a3359; border-radius:12px; padding:20px; background:#13182b; }
    h1 { font-size:1.15rem; margin-top:0; }
    .btn { border:1px solid #4b65bb; background:#2a3f82; color:#e7eeff; border-radius:8px; padding:10px 16px; cursor:pointer; font-size:14px; margin-top:12px; margin-right:8px; }
    .btn:disabled { opacity:.5; cursor:not-allowed; }
    .merge-bar-wrap { height:12px; background:#0f1324; border-radius:8px; overflow:hidden; margin-top:12px; border:1px solid #2b365e; }
    .merge-bar-fill { height:100%; background:linear-gradient(90deg,#3d5290,#5ecf8a); transition:width .35s ease; }
    .logs { margin-top:10px; max-height:140px; overflow:auto; font-family:Consolas,monospace; font-size:11px; white-space:pre-wrap; background:#0f1324; padding:8px; border-radius:8px; border:1px solid #2b365e; }
    .hint { font-size:13px; color:#9fb0d6; }
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
    - просто номер (15+ цифр)
    - любой текст, где встречается длинный числовой id
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if s.isdigit() and len(s) >= 15:
        return s
    try:
        p = urlparse(s)
        q = parse_qs(p.query)
        if "regNumber" in q and q["regNumber"]:
            cand = (q["regNumber"][0] or "").strip()
            if cand.isdigit() and len(cand) >= 15:
                return cand
    except Exception:
        pass
    import re

    m = re.search(r"\b(\d{15,25})\b", s)
    if m:
        return m.group(1)
    return ""


def _truthy_env(name: str, default: str = "0") -> bool:
    v = (os.environ.get(name, default) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _run_market_for_tender(tid: str, *, force_no_resume: bool = False) -> tuple[int, str]:
    rows_map = _estimate_rows_by_tender_id()
    max_rows_arg: str | None = None
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
    pause = (os.environ.get("MARKET_PAUSE_SEC") or os.environ.get("MARKET_PAUSE_SEC") or "4").strip() or "4"
    sources = (os.environ.get("MARKET_SOURCES") or "avito,web").strip() or "avito,web"
    max_results = (os.environ.get("MARKET_MAX_RESULTS") or "5").strip() or "5"
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
            mode = "только отсутствующие/ошибки" if only_missing else "все сметы"
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
                if not est_path.is_file():
                    turl = ""
                    if tender_url_by_id and tid in tender_url_by_id:
                        turl = (tender_url_by_id.get(tid) or "").strip()
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
                market_code, market_cmd = _run_market_for_tender(tid, force_no_resume=force_market_no_resume)
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
                    link = f"{site_url}/merge-report/{tid}/" if site_url else ""
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


def _stream_main_py(cli_args: list[str], *, log_cap: int = 400) -> int:
    """Один запуск main.py; дописывает строки в parse_state['log_lines']. Возвращает код выхода."""
    cmd = [sys.executable, "-u", str(_TOOLS_RUN_MODULE), "autobot.main"] + cli_args
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            env=_parse_env(),
            bufsize=1,
        )
    except OSError as e:
        with parse_lock:
            parse_state["log_lines"].append(f"Ошибка запуска процесса: {e}")
            parse_state["log_lines"] = parse_state["log_lines"][-log_cap:]
        return -1

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        with parse_lock:
            parse_state["log_lines"].append(line)
            parse_state["log_lines"] = parse_state["log_lines"][-log_cap:]
    return proc.wait()


def _run_main_worker(cli_args: list[str], task: str) -> None:
    """Запуск main.py тем же Python, что и веб-сервер (не py.exe из PATH)."""
    cmd = [sys.executable, "-u", str(_TOOLS_RUN_MODULE), "autobot.main"] + cli_args
    cmd_display = _cmd_display(cmd)
    with parse_lock:
        parse_state["running"] = True
        parse_state["task"] = task
        parse_state["command"] = cmd_display
        parse_state["started_at"] = datetime.now().isoformat(timespec="seconds")
        parse_state["ended_at"] = None
        parse_state["exit_code"] = None
        parse_state["log_lines"] = [f">>> {cmd_display}"]
    exit_code = -1
    try:
        exit_code = _stream_main_py(cli_args, log_cap=300)
    except Exception:
        err = traceback.format_exc(limit=8)
        with parse_lock:
            parse_state["log_lines"].append("!!! Внутренняя ошибка фонового парсинга:")
            parse_state["log_lines"].extend(err.rstrip().splitlines())
            parse_state["log_lines"] = parse_state["log_lines"][-300:]
    finally:
        with parse_lock:
            parse_state["running"] = False
            parse_state["task"] = ""
            parse_state["command"] = ""
            parse_state["ended_at"] = datetime.now().isoformat(timespec="seconds")
            parse_state["exit_code"] = exit_code
            if exit_code == 0:
                parse_state["log_lines"].append(
                    "--- Готово. Обновите страницу (F5), чтобы подтянуть список тендеров. ---"
                )


def _run_rebuild_all_worker() -> None:
    """Последовательно пересобирает Excel/HTML для каждого тендера из tenders.json (как --from-downloaded-tender-id)."""
    meta = load_tender_metadata()
    ids = sorted(meta.keys(), key=lambda x: x)
    started = datetime.now().isoformat(timespec="seconds")
    with parse_lock:
        parse_state["running"] = True
        parse_state["task"] = "пересбор всех отчётов"
        parse_state["command"] = f"{len(ids)} тендеров"
        parse_state["started_at"] = started
        parse_state["ended_at"] = None
        parse_state["exit_code"] = None
        if not ids:
            parse_state["log_lines"] = ["В tenders.json нет тендеров — нечего пересобирать."]
            parse_state["running"] = False
            parse_state["task"] = ""
            parse_state["ended_at"] = datetime.now().isoformat(timespec="seconds")
            parse_state["exit_code"] = 0
            return
        parse_state["log_lines"] = [
            f">>> Пересбор отчётов для {len(ids)} тендеров из tenders.json (по очереди, тот же алгоритм, что «Пересобрать отчёт»)."
        ]

    failed: list[tuple[str, int]] = []
    cap = 600
    for i, tid in enumerate(ids, 1):
        with parse_lock:
            parse_state["task"] = f"пересбор {i}/{len(ids)}: {tid}"
            parse_state["command"] = _cmd_display(
                [
                    sys.executable,
                    str(_TOOLS_RUN_MODULE),
                    "autobot.main",
                    "--from-downloaded-tender-id",
                    tid,
                ]
            )
            parse_state["log_lines"].append(f"--- [{i}/{len(ids)}] {tid} ---")
            parse_state["log_lines"] = parse_state["log_lines"][-cap:]

        code = _stream_main_py(["--from-downloaded-tender-id", tid], log_cap=cap)
        if code != 0:
            failed.append((tid, code))
        with parse_lock:
            parse_state["log_lines"].append(f"--- конец {tid}, код {code} ---")
            parse_state["log_lines"] = parse_state["log_lines"][-cap:]

    with parse_lock:
        parse_state["running"] = False
        parse_state["task"] = ""
        parse_state["command"] = ""
        parse_state["ended_at"] = datetime.now().isoformat(timespec="seconds")
        parse_state["exit_code"] = 0 if not failed else 1
        parse_state["log_lines"].append(
            "--- Пересбор всех отчётов завершён. Обновите страницу (F5). ---"
        )
        if failed:
            parse_state["log_lines"].append(
                "Тендеры с ненулевым кодом выхода: " + ", ".join(f"{t} ({c})" for t, c in failed)
            )
        parse_state["log_lines"] = parse_state["log_lines"][-cap:]


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


@app.route("/api/start-parse", methods=["POST"])
def api_start_parse():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания подготовки сравнений цен."}), 409
    with parse_lock:
        if parse_state["running"]:
            return jsonify({"ok": False, "message": "Уже выполняется задание"}), 409
    data = request.get_json(silent=True) or {}
    try:
        max_pages = int(data.get("max_pages", 2))
        max_tenders = int(data.get("max_tenders", 15))
        days_back = int(data.get("days_back", 60))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Некорректные числа (max_pages, max_tenders, days_back)"}), 400
    max_pages = max(1, min(max_pages, 20))
    max_tenders = max(1, min(max_tenders, 50))
    days_back = max(1, min(days_back, 365))
    args = [
        "--max-pages",
        str(max_pages),
        "--max-tenders",
        str(max_tenders),
        "--days-back",
        str(days_back),
    ]
    worker = threading.Thread(
        target=_run_main_worker,
        kwargs={"cli_args": args, "task": "поиск новых закупок"},
        daemon=True,
    )
    with parse_lock:
        if parse_state["running"]:
            return jsonify({"ok": False, "message": "Уже выполняется задание"}), 409
        parse_state["running"] = True
        parse_state["task"] = "запуск поиска новых закупок"
        parse_state["command"] = ""
        parse_state["started_at"] = datetime.now().isoformat(timespec="seconds")
        parse_state["ended_at"] = None
        parse_state["exit_code"] = None
        parse_state["log_lines"] = ["Подготавливаем запуск поиска…"]
    try:
        worker.start()
    except RuntimeError as e:
        with parse_lock:
            parse_state["running"] = False
            parse_state["task"] = ""
            parse_state["ended_at"] = datetime.now().isoformat(timespec="seconds")
            parse_state["exit_code"] = -1
            parse_state["log_lines"].append(f"Не удалось запустить фоновую задачу: {e}")
        return jsonify({"ok": False, "message": "Не удалось запустить фоновую задачу поиска."}), 500
    return jsonify({"ok": True})


@app.route("/api/rebuild-report", methods=["POST"])
def api_rebuild_report():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания подготовки сравнений цен."}), 409
    with parse_lock:
        if parse_state["running"]:
            return jsonify({"ok": False, "message": "Уже выполняется задание"}), 409
    data = request.get_json(silent=True) or {}
    tid = str(data.get("tender_id", "")).strip()
    if not tid:
        return jsonify({"ok": False, "message": "Укажите tender_id"}), 400
    if not _AUTOBOT_MAIN_FILE.is_file():
        return jsonify({"ok": False, "message": f"Не найден {_AUTOBOT_MAIN_FILE}"}), 500
    threading.Thread(
        target=_run_main_worker,
        kwargs={"cli_args": ["--from-downloaded-tender-id", tid], "task": f"повторное извлечение сметы {tid}"},
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/api/rebuild-all-reports", methods=["POST"])
def api_rebuild_all_reports():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания подготовки сравнений цен."}), 409
    with parse_lock:
        if parse_state["running"]:
            return jsonify({"ok": False, "message": "Уже выполняется задание"}), 409
    if not _AUTOBOT_MAIN_FILE.is_file():
        return jsonify({"ok": False, "message": f"Не найден {_AUTOBOT_MAIN_FILE}"}), 500
    if not TENDERS_JSON.is_file():
        return jsonify({"ok": False, "message": "Нет файла tenders.json"}), 400
    meta = load_tender_metadata()
    if not meta:
        return jsonify({"ok": False, "message": "В tenders.json нет тендеров"}), 400
    threading.Thread(target=_run_rebuild_all_worker, daemon=True).start()
    return jsonify({"ok": True, "count": len(meta)})


@app.route("/api/parse-status")
def api_parse_status():
    with parse_lock:
        payload = {
            "running": parse_state["running"],
            "task": parse_state["task"],
            "command": parse_state["command"],
            "started_at": parse_state["started_at"],
            "ended_at": parse_state["ended_at"],
            "exit_code": parse_state["exit_code"],
            "log_lines_count": len(parse_state["log_lines"]),
            "log_tail": parse_state["log_lines"][-80:],
        }
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
            }
        )
    items.sort(key=lambda x: (x.get("region") or "", str(x.get("tender_id") or "")))
    return jsonify({"items": items[:200]})


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


@app.route("/api/reports-coverage")
def api_reports_coverage():
    return jsonify(_compute_reports_coverage())


@app.route("/api/push-state")
def api_push_state():
    cov = _compute_reports_coverage()
    with parse_lock:
        pr_running = bool(parse_state.get("running"))
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
    with parse_lock:
        if parse_state["running"]:
            return jsonify({"ok": False, "message": "Сначала дождитесь окончания текущей работы с документами."}), 409
    threading.Thread(target=_run_merge_site_all_worker, kwargs={"only_missing": False}, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/generate-merge-site-missing", methods=["POST"])
def api_generate_merge_site_missing():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сравнения цен уже подготавливаются."}), 409
    with parse_lock:
        if parse_state["running"]:
            return jsonify({"ok": False, "message": "Сначала дождитесь окончания текущей работы с документами."}), 409
    threading.Thread(target=_run_merge_site_all_worker, kwargs={"only_missing": True}, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/generate-merge-site-one", methods=["POST"])
def api_generate_merge_site_one():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сравнения цен уже подготавливаются."}), 409
    with parse_lock:
        if parse_state["running"]:
            return jsonify({"ok": False, "message": "Сначала дождитесь окончания текущей работы с документами."}), 409
    data = request.get_json(silent=True) or {}
    tid = str(data.get("tender_id", "")).strip()
    if not tid:
        return jsonify({"ok": False, "message": "Нужен tender_id"}), 400
    threading.Thread(target=_run_merge_site_all_worker, kwargs={"ids_override": [tid]}, daemon=True).start()
    return jsonify({"ok": True, "tender_id": tid})


@app.route("/api/generate-merge-site-one-rerun-market", methods=["POST"])
def api_generate_merge_site_one_rerun_market():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сравнения цен уже подготавливаются."}), 409
    with parse_lock:
        if parse_state["running"]:
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


@app.route("/api/generate-merge-site-by-link", methods=["POST"])
def api_generate_merge_site_by_link():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сравнения цен уже подготавливаются."}), 409
    with parse_lock:
        if parse_state["running"]:
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
    report_url = f"{base}/merge-report/{tid}/" if base else ""
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
    app.run(host=_host, port=_port, debug=False)
