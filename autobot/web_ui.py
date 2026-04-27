from __future__ import annotations

from autobot.paths import REPO_ROOT
import json
import os
import subprocess
import sys
import traceback
import threading
import html as html_mod
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

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
    "started_at": None,
    "ended_at": None,
    "error_ids": [],
    "log_lines": [],
    "last_ended_at": None,
    "last_summary": "",
    "last_reason_counts": {},
}
merge_site_lock = threading.Lock()


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
  <title>Тендеры</title>
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
      --accent: #4b65bb;
      --accent-2: #3d5290;
      --ok: #5ecf8a;
      --danger: #a04048;
      --shadow: 0 10px 30px rgba(0, 0, 0, 0.28);
    }
    html, body { min-height: 100%; margin: 0; box-sizing: border-box; }
    *, *::before, *::after { box-sizing: inherit; }
    body { font-family: Segoe UI, Arial, sans-serif; background: radial-gradient(1200px 700px at 20% -200px, #1c2b56 0%, var(--bg) 45%); color: var(--text); }
    .page { max-width: 960px; margin: 0 auto; padding: 22px 18px 40px; }
    h1 { margin: 0 0 6px 0; font-size: 1.45rem; font-weight: 700; letter-spacing: -0.02em; }
    .sub { color: var(--muted); font-size: 13px; margin: 0 0 18px 0; line-height: 1.45; max-width: 52ch; }
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
      background: linear-gradient(180deg, #334b93, #2a3f82);
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
    .tender-grid-main { display: grid; grid-template-columns: repeat(auto-fill, minmax(285px, 1fr)); gap: 9px; }
    .tender-cell { display: flex; flex-direction: column; gap: 5px; position: relative; min-width: 0; }
    .tender-card {
      display: flex;
      flex-direction: column;
      padding: 10px 38px 9px 11px;
      border-radius: 10px;
      border: 1px solid #2a3962;
      background: linear-gradient(145deg, #1a2442, #141d34);
      color: var(--text);
      transition: transform .15s ease, border-color .15s, box-shadow .15s;
      min-height: 96px;
      min-width: 0;
      overflow: hidden;
    }
    .tender-card:hover { transform: translateY(-2px); border-color: #607dce; box-shadow: 0 8px 20px rgba(0,0,0,.28); }
    .tender-card.no-data { border-left: 3px solid var(--danger); }
    .tender-card-link {
      display: block;
      min-width: 0;
      text-decoration: none;
      color: inherit;
    }
    .tender-card-link--more { flex: 1 1 auto; margin-top: 2px; }
    .tender-card .title { font-size: 13px; line-height: 1.3; max-height: 3.9em; overflow: hidden; word-break: break-word; }
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
    .tender-card-pub {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px 10px;
      margin-top: 8px;
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
    .tender-card-tags-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px 8px;
      margin-top: 8px;
      min-width: 0;
    }
    .tender-card-tags-hit {
      flex: 1 1 auto;
      min-width: 0;
      text-decoration: none;
      color: inherit;
    }
    .tender-card .tags { margin-top: 0; display: flex; flex-wrap: wrap; gap: 5px; align-items: center; }
    .tender-card-tags-row .eis-in-card { margin-left: auto; }
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
    .tag { font-size: 10px; padding: 2px 6px; border-radius: 999px; font-weight: 700; letter-spacing: .1px; }
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
    .parse-progress-panel { margin-top: 10px; padding: 10px 12px; border-radius: 10px; background: #1a2238; border: 1px solid #3d5290; }
    .parse-progress-panel[hidden] { display: none !important; }
    .parse-progress-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; font-size: 13px; color: #c8d8f8; }
    .parse-pulse { width: 10px; height: 10px; border-radius: 50%; background: #5ecf8a; flex-shrink: 0; animation: parsePulse 1.2s ease-in-out infinite; box-shadow: 0 0 8px #5ecf8a; }
    @keyframes parsePulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.55; transform: scale(0.92); } }
    .parse-progress-time { font-size: 13px; color: #9df0b8; font-variant-numeric: tabular-nums; }
    .parse-progress-hint { font-size: 11px; color: #9fb0d6; margin-top: 6px; line-height: 1.35; }
    .parse-status-line { margin-top: 6px; font-size: 11px; color: #8a9bc4; word-break: break-all; }
    .merge-bar-wrap { height: 12px; background: #0f1324; border-radius: 8px; overflow: hidden; margin-top: 10px; border: 1px solid #2b365e; }
    .merge-bar-fill { height: 100%; background: linear-gradient(90deg, #3d5290, #5ecf8a); transition: width 0.35s ease; border-radius: 8px; }
    .merge-logs { margin-top: 8px; max-height: 140px; overflow: auto; border: 1px solid #2b365e; border-radius: 8px; background: #0f1324; padding: 8px; font-family: Consolas, monospace; font-size: 11px; white-space: pre-wrap; }
    .cov-banner { padding: 9px 12px; border-radius: 10px; margin-bottom: 10px; font-size: 12px; line-height: 1.45; }
    .cov-warn { background: rgba(90, 26, 34, 0.45); border: 1px solid #a04048; color: #ffc9cc; }
    .cov-partial { background: rgba(77, 53, 30, 0.45); border: 1px solid #8a623d; color: #ffd7a8; }
    .cov-ok { background: rgba(30, 77, 53, 0.35); border: 1px solid #3d8a67; color: #9df0b8; }
    @media (max-width: 980px) {
      .opts { grid-template-columns: 1fr 1fr; }
    }
    @media (max-width: 720px) {
      .page { padding: 12px 10px 24px; }
      .opts { grid-template-columns: 1fr; }
      .tender-grid-main { grid-template-columns: 1fr; }
      .btn-row .btn { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="page">
    <h1>Тендеры</h1>
    <p class="sub">Откройте карточку — сводка <strong>смета и рынок (Алиса)</strong>. Две кнопки ниже закрывают типичный сценарий; остальное спрятано в «Дополнительно».</p>

    <div id="reportCoverageBanner" class="cov-banner stat-strip {% if coverage.tender_count == 0 %}cov-warn{% elif coverage.tenders_missing_merge_html > 0 %}{% if coverage.merge_html_among_tenders == 0 and coverage.svodka_xlsx_count == 0 %}cov-warn{% else %}cov-partial{% endif %}{% else %}cov-ok{% endif %}">
      {% if coverage.tender_count == 0 %}
      В базе пока нет тендеров — нажмите «Обновить из ЕИС».
      {% else %}
      Сводок <strong>смета + Алиса</strong> на сайте: <strong>{{ coverage.merge_html_among_tenders }}</strong> / {{ coverage.tender_count }}
      {% if coverage.tenders_missing_merge_html > 0 %}
      · не готово: <strong>{{ coverage.tenders_missing_merge_html }}</strong> (кнопка «Сводки для всех»).
      {% endif %}
      {% endif %}
    </div>

    <form class="filters" method="get" action="/">
      <label>
        <input type="checkbox" name="all" value="1" {% if show_all %}checked{% endif %} onchange="this.form.submit()"/>
        <span>Все этапы</span>
      </label>
      <label>
        <span>Сортировка:</span>
        <select name="sort" onchange="this.form.submit()">
          <option value="publish_desc" {% if sort_mode == "publish_desc" %}selected{% endif %}>по дате публикации: новые сверху</option>
          <option value="publish_asc" {% if sort_mode == "publish_asc" %}selected{% endif %}>по дате публикации: старые сверху</option>
        </select>
      </label>
      <span class="muted">{{ visible_count }} из {{ tender_count }}</span>
      {% if show_all %}
      <a class="eis-below" href="/?sort={{ sort_mode }}">Только подача заявок</a>
      {% endif %}
    </form>

    <div class="controls">
      <div class="action-bar">
        <button class="btn btn-lg" type="button" id="startBtn" onclick="startParsing()">Обновить из ЕИС</button>
        <button class="btn btn-lg secondary" type="button" id="genMergeSiteBtn" onclick="generateMergeSiteAll()">Сводки для всех</button>
      </div>
      <p class="controls-hint">Первая кнопка — поиск и новые карточки. Вторая — Алиса, merge и HTML для сводок по сметам из <code>data/reports</code>.</p>

      <details class="advanced">
        <summary>Дополнительно: по ссылке, один тендер, настройки поиска…</summary>
        <div class="advanced-body">
          <div class="opts">
            <label>Страниц поиска (регион × ключ)
              <input type="number" id="optMaxPages" min="1" max="20" value="2" />
            </label>
            <label>Макс. тендеров за прогон
              <input type="number" id="optMaxTenders" min="1" max="50" value="15" />
            </label>
            <label style="grid-column: 1 / -1;">Не старше (дней)
              <input type="number" id="optDaysBack" min="1" max="365" value="60" />
            </label>
          </div>
          <div class="link-row">
            <span>Ссылка или ID:</span>
            <input id="tenderLinkInput" type="text" placeholder="Ссылка на извещение или 19-значный номер" />
            <button class="btn secondary" type="button" id="runByLinkBtn" onclick="runByTenderLink()">Запустить по ссылке</button>
          </div>
          <div class="rebuild-row">
            <span>Пересобрать смету (Excel+HTML) для:</span>
            <select id="rebuildTenderSelect" {% if not rebuild_options %}disabled{% endif %}>
              {% for o in rebuild_options %}
              <option value="{{ o.tender_id }}">{{ o.tender_id }} — {{ o.display_title }}</option>
              {% endfor %}
              {% if not rebuild_options %}
              <option value="">— нет тендеров —</option>
              {% endif %}
            </select>
          </div>
          <div class="btn-row">
            <button class="btn secondary" type="button" id="rebuildBtn" onclick="rebuildReport()">Пересобрать смету</button>
            <button class="btn secondary" type="button" id="rebuildAllBtn" onclick="rebuildAllReports()" {% if tender_count < 1 %}disabled title="Нет тендеров в базе"{% endif %}>Все сметы заново</button>
            <button class="btn secondary" type="button" id="genMergeMissingBtn" onclick="generateMergeSiteMissing()">Досбор без сводки</button>
            <a class="link-refresh" href="#" onclick="location.reload(); return false;">Обновить страницу</a>
          </div>
        </div>
      </details>
      <div id="mergeSitePanel" class="parse-progress-panel" hidden>
        <div class="parse-progress-head">
          <span class="parse-pulse" aria-hidden="true"></span>
          <strong id="mergeSiteLabel">Сводки для всех</strong>
        </div>
        <div class="merge-bar-wrap"><div id="mergeBarFill" class="merge-bar-fill" style="width:0%"></div></div>
        <div class="parse-progress-time" id="mergePercentText">0%</div>
        <div class="parse-progress-hint" id="mergeSiteDetail"></div>
        <div class="merge-logs" id="mergeSiteLogs"></div>
      </div>
      <div id="mergeIdleSummary" class="meta" style="margin-top:4px;"></div>
      <div id="mergeMissingReason" class="meta" style="margin-top:4px;"></div>
      <div id="parseProgressPanel" class="parse-progress-panel" hidden>
        <div class="parse-progress-head">
          <span class="parse-pulse" aria-hidden="true"></span>
          <strong id="parseProgressLabel">Выполняется…</strong>
        </div>
        <div class="parse-bar-wrap"><div id="parseBarFill" class="parse-bar-fill"></div></div>
        <div class="parse-progress-time" id="parseProgressTime">Прошло: 0 с</div>
        <div class="status" id="parseStatus"></div>
        <div class="parse-status-line" id="parseCommandLine"></div>
        <div id="parseProgressLogCount" class="parse-progress-hint" style="margin-top:4px;color:#b8c7ea;"></div>
        <div class="parse-progress-hint">Лог по мере выполнения.</div>
        <div class="logs" id="parseLogs"></div>
      </div>
    </div>

    {% for region_name, items in grouped %}
    <section class="region-block">
      <h2 class="region-title">{{ region_name }}</h2>
      <div class="tender-grid-main">
        {% for t in items %}
        <div class="tender-cell">
          <div class="tender-menu-wrap">
            <button type="button" class="tender-menu-btn" onclick="event.stopPropagation();event.preventDefault();">⋯</button>
            <div class="tender-menu">
              <button type="button" onclick="event.stopPropagation();event.preventDefault();runFullForTender('{{ t.tender_id }}');">Сводка с Алисой</button>
              <button type="button" onclick="event.stopPropagation();event.preventDefault();rerunAliceForTender('{{ t.tender_id }}');">Алиса заново</button>
              <button type="button" onclick="event.stopPropagation();event.preventDefault();runViabilityOnly('{{ t.tender_id }}');">Анализ в Telegram</button>
            </div>
          </div>
          <div class="tender-card{% if not t.has_display_data %} no-data{% endif %}">
            <a class="tender-card-link" href="/merge-report/{{ t.tender_id }}/">
              <div class="title">{{ t.display_title }}</div>
            </a>
            <div class="tender-card-row">
              <a class="tender-card-sub" href="/merge-report/{{ t.tender_id }}/">
                <span class="tid">№ {{ t.tender_id }} · {{ t.estimate_rows }} поз.</span>
              </a>
            </div>
            <a class="tender-card-link tender-card-link--more" href="/merge-report/{{ t.tender_id }}/">
              <div class="tender-card-pub">
                <span class="tender-card-pub-label">Публикация</span>
                <span class="tender-card-pub-date">{{ t.publish_date or "—" }}</span>
              </div>
            </a>
            <div class="tender-card-tags-row">
              <a class="tender-card-tags-hit" href="/merge-report/{{ t.tender_id }}/">
                <div class="tags">
                  {% if t.has_display_data %}<span class="tag tag-ok">смета</span>{% else %}<span class="tag tag-nodata">нет сметы</span>{% endif %}
                  <span class="tag {% if t.stage_open %}tag-stage-open{% else %}tag-stage-closed{% endif %}">{{ t.stage_display }}</span>
                </div>
              </a>
              {% if t.eis_url %}
              <a class="eis-in-card" href="{{ t.eis_url }}" target="_blank" rel="noopener noreferrer" title="Карточка на zakupki.gov.ru">ЕИС ↗</a>
              {% endif %}
            </div>
          </div>
        </div>
        {% endfor %}
      </div>
    </section>
    {% endfor %}
    {% if not grouped %}
    {% if tender_count == 0 %}
    <p class="sub">База пуста — нажмите «Обновить из ЕИС».</p>
    {% elif not show_all %}
    <p class="sub">Нет закупок на этапе «Подача заявок». Включите «Все этапы» вверху, чтобы увидеть остальные.</p>
    {% else %}
    <p class="sub">Нет данных для отображения.</p>
    {% endif %}
    {% endif %}

    {% if tender_count %}
    <footer class="page-footer">В базе {{ tender_count }} · отчёты сметы с таблицей: {{ display_report_count }} / {{ report_count }} · <span style="color:#d89090;">красная метка</span> — в HTML нет блоков работ.</footer>
    {% endif %}
  </div>
  <script>
    (function setupTenderMenus() {
      const wraps = Array.from(document.querySelectorAll(".tender-menu-wrap"));
      wraps.forEach((wrap) => {
        let hideTimer = null;
        const openMenu = () => {
          if (hideTimer) {
            clearTimeout(hideTimer);
            hideTimer = null;
          }
          wrap.classList.add("menu-open");
        };
        const delayedClose = () => {
          if (hideTimer) clearTimeout(hideTimer);
          hideTimer = setTimeout(() => {
            wrap.classList.remove("menu-open");
            hideTimer = null;
          }, 1000);
        };
        wrap.addEventListener("mouseenter", openMenu);
        wrap.addEventListener("mouseleave", delayedClose);
        const btn = wrap.querySelector(".tender-menu-btn");
        if (btn) {
          btn.addEventListener("click", function(ev) {
            ev.preventDefault();
            ev.stopPropagation();
            if (wrap.classList.contains("menu-open")) {
              delayedClose();
            } else {
              openMenu();
            }
          });
        }
      });
      document.addEventListener("click", () => {
        wraps.forEach((w) => w.classList.remove("menu-open"));
      });
    })();

    (function bindRebuildSelect() {
      const sel = document.getElementById("rebuildTenderSelect");
      if (!sel) return;
      sel.addEventListener("change", function() {
        applyToolbarDisabled(parseRunning, !!window.__mergeRunLive);
      });
    })();

    function getRebuildTenderId() {
      const s = document.getElementById("rebuildTenderSelect");
      return s && s.value ? String(s.value).trim() : "";
    }
    const TENDER_COUNT = {{ tender_count }};

    let parseRunning = false;
    let parseStartMs = null;

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

    function applyToolbarDisabled(parseRun, mergeRun) {
      const busy = parseRun || mergeRun;
      const startBtn = document.getElementById("startBtn");
      const rebuildBtn = document.getElementById("rebuildBtn");
      const rebuildAllBtn = document.getElementById("rebuildAllBtn");
      const genBtn = document.getElementById("genMergeSiteBtn");
      const genMissingBtn = document.getElementById("genMergeMissingBtn");
      const runByLinkBtn = document.getElementById("runByLinkBtn");
      if (startBtn) startBtn.disabled = busy;
      if (rebuildBtn) rebuildBtn.disabled = busy || !getRebuildTenderId();
      if (rebuildAllBtn) rebuildAllBtn.disabled = busy || TENDER_COUNT < 1;
      if (genBtn) genBtn.disabled = busy;
      if (genMissingBtn) genMissingBtn.disabled = busy;
      if (runByLinkBtn) runByLinkBtn.disabled = busy;
    }

    async function startParsing() {
      applyToolbarDisabled(true, false);
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
        if (!data.ok) {
          alert(data.message || "Не удалось запустить парсинг");
          refreshStatus();
        }
      } catch (e) {
        alert("Ошибка запуска парсинга");
        refreshStatus();
      }
    }

    async function rebuildReport() {
      const tid = getRebuildTenderId();
      if (!tid) { alert("Выберите тендер в списке (блок «Дополнительно»)"); return; }
      if (!confirm("Пересобрать Excel и HTML из уже скачанных файлов в data/downloads/" + tid + " ?")) return;
      applyToolbarDisabled(true, false);
      try {
        const r = await fetch("/api/rebuild-report", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tender_id: tid }),
        });
        const data = await r.json();
        if (!data.ok) {
          alert(data.message || "Не удалось запустить пересборку");
          refreshStatus();
        }
      } catch (e) {
        alert("Ошибка запроса пересборки");
        refreshStatus();
      }
    }

    async function rebuildAllReports() {
      if (TENDER_COUNT < 1) { alert("В tenders.json нет тендеров"); return; }
      if (!confirm(
        "Пересобрать отчёты (Excel + HTML) для всех " + TENDER_COUNT + " тендеров из tenders.json?\\n\\n"
        + "По очереди запустится main.py --from-downloaded-tender-id для каждого номера. Это может занять много времени."
      )) return;
      applyToolbarDisabled(true, false);
      try {
        const r = await fetch("/api/rebuild-all-reports", { method: "POST" });
        const data = await r.json();
        if (!data.ok) {
          alert(data.message || "Не удалось запустить пересборку всех отчётов");
          refreshStatus();
        }
      } catch (e) {
        alert("Ошибка запроса");
        refreshStatus();
      }
    }

    async function generateMergeSiteAll() {
      if (!confirm("Запустить «Сводки для всех»? (Алиса, merge, HTML по сметам — может занять долго.)")) return;
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
      if (!confirm("Сделать только те тендеры, где нет /merge-report/<id>/ или прошлый прогон дал ошибку?")) return;
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
      if (!confirm("Сделать полный отчёт для тендера " + t + " (Алиса → merge → HTML)?")) return;
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

    async function rerunAliceForTender(tid) {
      const t = String(tid || "").trim();
      if (!t) return;
      if (!confirm("Пересобрать Алису с нуля для тендера " + t + " (без resume), затем merge + HTML?")) return;
      try {
        const r = await fetch("/api/generate-merge-site-one-rerun-alice", {
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
      if (!confirm("Пересобрать «Оценку по сравнению» и страницу отчёта для " + t + "? Нужна готовая сводка СВОДКА_РЫНОК (после merge).")) return;
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
      if (!raw) { alert("Вставьте ссылку на тендер или номер тендера."); return; }
      if (!confirm("Запустить анализ по этой ссылке/ID (Алиса → merge → HTML)?")) return;
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
          el.innerHTML = "В базе нет тендеров — «Обновить из ЕИС».";
          return;
        }
        if (miss > 0) cls = mh === 0 && sx === 0 ? "cov-banner stat-strip cov-warn" : "cov-banner stat-strip cov-partial";
        el.className = cls;
        let html = "Сводок <strong>смета + Алиса</strong>: <strong>" + mh + "</strong> / " + nt;
        if (miss > 0) {
          html += " · не готово: <strong>" + miss + "</strong>";
          html += "<br/><span style=\\"opacity:.85;font-size:11px\\">Подсказка: без сметы " + rs_no_est + ", без сводки " + rs_no_svodka + ", без HTML " + rs_no_html + ".</span>";
        }
        el.innerHTML = html;
      } catch (e) {}
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
        parseRunning = !!pr.running;
        if (pr.running && pr.started_at) {
          const ms = Date.parse(pr.started_at);
          parseStartMs = Number.isNaN(ms) ? null : ms;
        } else {
          parseStartMs = null;
        }

        const hasParseHistory = !!(
          (pr.log_tail && pr.log_tail.length)
          || pr.command
          || pr.ended_at
          || pr.exit_code !== null && pr.exit_code !== undefined
        );
        const panel = document.getElementById("parseProgressPanel");
        if (panel) panel.hidden = !(pr.running || hasParseHistory);

        const label = document.getElementById("parseProgressLabel");
        if (label) {
          if (pr.running) {
            label.textContent = pr.task ? "Сейчас: " + pr.task : "Сейчас: выполняется задание…";
          } else if (pr.exit_code !== null && pr.exit_code !== undefined) {
            label.textContent = pr.exit_code === 0 ? "Завершено успешно" : "Завершено с ошибкой";
          } else {
            label.textContent = "Ожидание";
          }
        }

        const lc = document.getElementById("parseProgressLogCount");
        if (lc && (pr.running || hasParseHistory)) {
          const n = pr.log_lines_count ?? 0;
          lc.textContent = pr.running
            ? "Строк в логе: " + n + " (растёт, пока идёт вывод)."
            : "Строк в логе: " + n + ".";
        } else if (lc) lc.textContent = "";

        const bar = document.getElementById("parseBarFill");
        if (bar) {
          if (pr.running) {
            bar.classList.add("running");
            bar.style.width = "65%";
          } else {
            bar.classList.remove("running");
            bar.style.width = (pr.exit_code === 0 ? "100%" : "100%");
          }
        }

        const status = document.getElementById("parseStatus");
        const logs = document.getElementById("parseLogs");
        const cmdLine = document.getElementById("parseCommandLine");
        let st = pr.running ? "идёт выполнение" : "ожидание";
        if (!pr.running && pr.exit_code !== null && pr.exit_code !== undefined) {
          st += " | код выхода: " + pr.exit_code;
        }
        if (pr.ended_at && !pr.running) st += " | завершено: " + pr.ended_at;
        status.textContent = "Статус: " + st + (pr.task && pr.running ? " («" + pr.task + "»)" : "");
        if (cmdLine) {
          cmdLine.textContent = pr.running && pr.command ? "Команда: " + pr.command : (pr.command && !pr.running ? "Последняя команда: " + pr.command : "");
        }
        if (logs) {
          logs.textContent = (pr.log_tail && pr.log_tail.length ? pr.log_tail.join("\\n") : "");
          logs.scrollTop = logs.scrollHeight;
        }

        if (pr.running) {
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
        if (fill) fill.style.width = Math.min(100, Math.max(0, pct)) + "%";
        if (ptext) {
          ptext.textContent = pct + "% · " + (mr.done ?? 0) + " / " + (mr.total ?? 0) + (mr.current_tid ? " · сейчас: " + mr.current_tid : "");
        }
        if (det) {
          det.textContent = mergeRun ? "Алиса → сводка → страница отчёта…" : "";
        }
        if (mlogs) {
          mlogs.textContent = (mr.log_tail && mr.log_tail.length ? mr.log_tail.join("\\n") : "");
          mlogs.scrollTop = mlogs.scrollHeight;
        }

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
          const txt = "Ошибки/пропуски: без сметы " + (reasons.no_estimate || 0)
            + ", Алиса " + (reasons.alice_failed || 0)
            + ", merge " + (reasons.merge_failed || 0)
            + ", HTML " + (reasons.html_failed || 0);
          mreason.textContent = !mergeRun && mr.last_ended_at ? txt : "";
        }

        window.__mergeRunLive = mergeRun;
        applyToolbarDisabled(!!pr.running, mergeRun);
        if (typeof window._wasMergeRun === "undefined") window._wasMergeRun = false;
        if (window._wasMergeRun && !mergeRun) refreshCoverage();
        window._wasMergeRun = mergeRun;
      } catch (e) {}
    }

    setInterval(refreshStatus, 2000);
    setInterval(refreshCoverage, 5000);
    refreshStatus();
    refreshCoverage();
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


def _alice_progress_for_tender(tid: str) -> tuple[int, int]:
    """
    Прогресс Алисы по тендеру: (готово, всего) по уникальным работам сметы.
    Логика "готово" совпадает с merge: есть строгие цены или из ответа извлекаются суммы.
    """
    from autobot.market_analytics import COL_NAME, extract_ruble_amounts
    from autobot.merge_estimate_alice import _norm_key

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
    total_keys = {_norm_key(x) for x in est[COL_NAME].fillna("").astype(str).tolist() if _norm_key(x)}
    total = len(total_keys)
    if total <= 0:
        return 0, 0

    stem = est_path.stem
    alice_path = REPORTS_DIR / f"АЛИСА_РЫНОК_{stem}.xlsx"
    if not alice_path.is_file():
        return 0, total
    try:
        ali = pd.read_excel(alice_path)
    except Exception:
        return 0, total
    if COL_NAME not in ali.columns:
        return 0, total

    # Совместимость со старыми файлами Алисы.
    ren: dict[str, str] = {}
    if "Цены за ед. (рынок, руб)" not in ali.columns and "Цены (строго, руб)" in ali.columns:
        ren["Цены (строго, руб)"] = "Цены за ед. (рынок, руб)"
    if ren:
        ali = ali.rename(columns=ren)

    done: set[str] = set()
    for _, row in ali.iterrows():
        k = _norm_key(str(row.get(COL_NAME, "") or ""))
        if not k or k not in total_keys:
            continue
        strict = str(row.get("Цены за ед. (рынок, руб)", "") or "").strip()
        has_strict = strict and strict.casefold() not in ("nan", "none", "—", "-", "н/д", "нет")
        if has_strict:
            done.add(k)
            continue
        reply = str(row.get("Ответ Алисы", "") or "").strip()
        if reply and extract_ruble_amounts(reply):
            done.add(k)
    return len(done), total


def collect_sidebar_tenders() -> tuple[list[dict], int, int, int]:
    """
    Все тендеры из tenders.json + признаки: есть файл отчёта и есть ли в нём блоки позиций
    (иначе внутри отчёта только «Нет данных для отображения»).
    """
    meta = load_tender_metadata()
    reports_map = _html_reports_by_tender_id()
    rows_map = _estimate_rows_by_tender_id()
    items: list[dict] = []
    for tid, tmeta in meta.items():
        report_file = reports_map.get(tid, "")
        has_report = bool(report_file) and (REPORTS_DIR / report_file).is_file()
        if not has_report:
            report_file = ""
        rp = REPORTS_DIR / report_file if report_file else None
        has_display_data = bool(rp) and _smet_report_html_has_position_groups(rp)
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
                "report_file": report_file,
                "stage_open": stage_open,
                "stage_display": stage_display,
                "estimate_rows": int(rows_map.get(tid, 0)),
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


@app.route("/")
def index():
    sidebar_items, tender_count, report_count, display_report_count = collect_sidebar_tenders()
    show_all = (request.args.get("all", "") or "").strip().lower() in ("1", "true", "yes", "on")
    sort_mode = (request.args.get("sort", "") or "publish_desc").strip().lower()
    if sort_mode not in ("publish_desc", "publish_asc"):
        sort_mode = "publish_desc"
    only_submission = not show_all  # True = только «Подача заявок» (режим по умолчанию)
    visible_items = [x for x in sidebar_items if (x.get("stage_open") if only_submission else True)]
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

    grouped = sorted(grouped_map.items(), key=lambda x: x[0])
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
    )


@app.route("/reports/<path:filename>")
def report_file(filename: str):
    target = REPORTS_DIR / filename
    if not target.exists() or not target.is_file():
        abort(404)
    return send_from_directory(REPORTS_DIR, filename)


MERGE_REPORTS_SITE_DIR = REPO_ROOT / "data" / "reports_site"


MISSING_MERGE_PAGE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Нет веб-сводки</title>
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
    <h1>Веб-отчёт не найден (404)</h1>
    <p>Тендер <strong>№ {{ tender_id }}</strong>. Страница строится из <code>data/reports_site/{{ tender_id }}/index.html</code> после генерации.</p>
    <p class="hint">Файлов <code>СВОДКА_РЫНОК_*.xlsx</code> в <code>data/reports/</code>: <strong>{{ svodka_count }}</strong>.
    {% if not has_svodka_for_tid %}<strong>Для этого номера нет</strong> <code>СВОДКА_РЫНОК_{{ tender_id }}.xlsx</code> — кнопка ниже не создаст страницу именно для него, пока не будет merge с Алисой.{% elif svodka_count == 0 %}Нужен сначала пайплайн с Алисой и merge (Excel сводка).{% endif %}</p>
    <p><a href="/">← На главную</a> · <a id="retryLink" href="#">Обновить эту страницу</a></p>
    <button type="button" class="btn" id="genBtn">Алиса + сводка для всех</button>
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
        if (!run && m.last_ended_at) idle.textContent = "Последний прогон (Алиса + сводка): " + m.last_ended_at + " — " + (m.last_summary||"");
        else if (run) idle.textContent = "";
        document.getElementById("genBtn").disabled = run;
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
    setInterval(tick, 1000);
    tick();
  </script>
</body>
</html>
"""


def _svodka_xlsx_tender_ids() -> list[str]:
    from autobot.merge_estimate_alice import OUT_PREFIX

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
    from autobot.merge_estimate_alice import OUT_PREFIX

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
    from autobot.merge_estimate_alice import OUT_PREFIX

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


def _run_alice_for_tender(tid: str, *, force_no_resume: bool = False) -> tuple[int, str]:
    rows_map = _estimate_rows_by_tender_id()
    max_rows_arg: str | None = None
    max_rows_raw = (os.environ.get("ALICE_MAX_ROWS") or "").strip()
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
    pause = (os.environ.get("ALICE_PAUSE_SEC") or "18").strip() or "18"
    cmd = [
        sys.executable,
        str(_TOOLS_RUN_MODULE),
        "autobot.alice_market_scraper",
        "--tender-id",
        tid,
        "--pause",
        pause,
    ]
    if max_rows_arg:
        cmd.extend(["--max-rows", max_rows_arg])
    if _truthy_env("ALICE_TWO_STEP", "1"):
        cmd.append("--two-step")
    if _truthy_env("ALICE_HEADLESS", "1"):
        cmd.append("--headless")
    else:
        cmd.append("--headed")
    if force_no_resume or _truthy_env("ALICE_NO_RESUME"):
        cmd.append("--no-resume")
    r = subprocess.run(cmd, cwd=str(REPO_ROOT))
    return int(r.returncode), " ".join(cmd)


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
    force_alice_no_resume: bool = False,
) -> None:
    errors: list[str] = []
    ok_html = 0
    ok_full = 0
    ids: list[str] = []
    reason_counts = {"no_estimate": 0, "alice_failed": 0, "merge_failed": 0, "html_failed": 0}
    try:
        from autobot.merge_estimate_alice import merge_estimate_and_alice
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
            merge_site_state["started_at"] = datetime.now().isoformat(timespec="seconds")
            merge_site_state["ended_at"] = None
            merge_site_state["error_ids"] = []
            mode = "только отсутствующие/ошибки" if only_missing else "все сметы"
            merge_site_state["log_lines"] = [f"Режим: {mode}. К обработке: {len(ids)}"]
            if not ids:
                merge_site_state["log_lines"].append(
                    "Нечего обрабатывать."
                )
                merge_site_state["running"] = False
                merge_site_state["ended_at"] = datetime.now().isoformat(timespec="seconds")
                merge_site_state["last_ended_at"] = merge_site_state["ended_at"]
                merge_site_state["last_summary"] = "0 файлов сметы"
                merge_site_state["last_reason_counts"] = reason_counts
                return

        for i, tid in enumerate(ids):
            pref = f"📊 <b>{i + 1}/{len(ids)}</b> · <code>{tid}</code>"
            with merge_site_lock:
                merge_site_state["current_tid"] = tid
                merge_site_state["log_lines"].append(f"[{i + 1}/{len(ids)}] {tid}…")
                merge_site_state["log_lines"] = merge_site_state["log_lines"][-cap:]
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
                            "Нужна ссылка на тендер или парсинг."
                        )
                        errors.append(tid)
                        continue
                try:
                    _cnt = int(len(pd.read_excel(est_path, usecols=[0])))
                except Exception:
                    _cnt = 0
                if _cnt > 0:
                    _tg_send(f"{pref}\n🟡 Смета: <b>{_cnt}</b> поз.")
                    _tg_flush_spool()
                    # «Запускаю Алису» — до тяжёлых проверок и до subprocess, иначе при spool сообщение
                    # может уехать в конец и появиться после всех строк Алисы.
                    _tg_send(f"{pref}\n🟡 Алиса…")
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

                done_before, total_works = _alice_progress_for_tender(tid)
                rem_before = max(0, total_works - done_before)
                if total_works > 0:
                    if rem_before > 0:
                        _tg_send(f"{pref}\n🟡 Алиса <b>{done_before}/{total_works}</b>…")
                    else:
                        _tg_send(f"{pref}\n🟢 Алиса уже <b>{done_before}/{total_works}</b>")
                    _tg_flush_spool()
                _tg_flush_spool()
                alice_code, alice_cmd = _run_alice_for_tender(tid, force_no_resume=force_alice_no_resume)
                with merge_site_lock:
                    merge_site_state["log_lines"].append(f"  alice: {alice_cmd}")
                if alice_code != 0:
                    reason_counts["alice_failed"] += 1
                    with merge_site_lock:
                        merge_site_state["log_lines"].append(f"  → Алиса код {alice_code}")
                    _tg_send(f"{pref}\n⚠️ Алиса код <code>{alice_code}</code>")
                    errors.append(tid)
                    continue

                done_after, total_after = _alice_progress_for_tender(tid)
                rem_after = max(0, total_after - done_after)
                if total_after > 0:
                    if rem_after > 0:
                        _tg_send(f"{pref}\n🟡 Алиса <b>{done_after}/{total_after}</b> (−{rem_after})")
                    else:
                        _tg_send(f"{pref}\n🟢 Алиса <b>{done_after}/{total_after}</b>")

                out = merge_estimate_and_alice(tid)
                _tg_send(f"{pref}\n🟡 Merge…")
                if not out or not out.is_file():
                    reason_counts["merge_failed"] += 1
                    with merge_site_lock:
                        merge_site_state["log_lines"].append("  → merge не собрал СВОДКА_РЫНОК")
                    _tg_send(f"{pref}\n⚠️ Нет <code>СВОДКА_РЫНОК_{tid}.xlsx</code>")
                    errors.append(tid)
                    continue
                p = write_tender_report_site(tid)
                if p and p.is_file():
                    ok_html += 1
                    ok_full += 1
                    site_url = get_report_site_public_base()
                    link = f"{site_url}/merge-report/{tid}/" if site_url else ""
                    with merge_site_lock:
                        merge_site_state["log_lines"].append("  → OK (Алиса + merge + HTML)")
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
                merge_site_state["log_lines"] = merge_site_state["log_lines"][-cap:]

        ended = datetime.now().isoformat(timespec="seconds")
        with merge_site_lock:
            merge_site_state["done"] = len(ids)
            merge_site_state["current_tid"] = ""
            merge_site_state["running"] = False
            merge_site_state["ended_at"] = ended
            merge_site_state["error_ids"] = errors
            merge_site_state["last_ended_at"] = ended
            merge_site_state["last_summary"] = (
                f"Полный прогон: {ok_full} из {len(ids)} "
                f"(ошибок/пропусков: {len(errors)}, HTML: {ok_html})"
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
                t = datetime.now().isoformat(timespec="seconds")
                merge_site_state["ended_at"] = t
                if not merge_site_state.get("last_ended_at"):
                    merge_site_state["last_ended_at"] = t
                merge_site_state.setdefault("last_summary", "Прервано / ошибка")


@app.route("/merge-report/<tender_id>/")
@app.route("/merge-report/<tender_id>/index.html")
def merge_report_site(tender_id: str):
    """Сводка смета + Алиса (report_merge_html → data/reports_site/<id>/index.html)."""
    tid = (tender_id or "").strip()
    if not tid or "/" in tid or ".." in tid:
        abort(404)
    folder = MERGE_REPORTS_SITE_DIR / tid
    target = folder / "index.html"
    from autobot.merge_estimate_alice import OUT_PREFIX

    svodka = REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx"
    # Всегда пересобираем HTML из сводки — иначе остаётся старый index.html без карточек/скриптов.
    if svodka.is_file():
        try:
            from autobot.report_merge_html import write_tender_report_site

            out_path = write_tender_report_site(tid)
            print(f"[merge-report] HTML ok tender={tid} -> {out_path}", file=sys.stderr, flush=True)
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
    ), 404


def _parse_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    return env


def _cmd_display(cmd: list[str]) -> str:
    return " ".join(repr(x) if any(c in x for c in " \t\"") else x for x in cmd)


def _stream_main_py(cli_args: list[str], *, log_cap: int = 400) -> int:
    """Один запуск main.py; дописывает строки в parse_state['log_lines']. Возвращает код выхода."""
    cmd = [sys.executable, str(_TOOLS_RUN_MODULE), "autobot.main"] + cli_args
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            env=_parse_env(),
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
    cmd = [sys.executable, str(_TOOLS_RUN_MODULE), "autobot.main"] + cli_args
    cmd_display = _cmd_display(cmd)
    with parse_lock:
        parse_state["running"] = True
        parse_state["task"] = task
        parse_state["command"] = cmd_display
        parse_state["started_at"] = datetime.now().isoformat(timespec="seconds")
        parse_state["ended_at"] = None
        parse_state["exit_code"] = None
        parse_state["log_lines"] = [f">>> {cmd_display}"]

    exit_code = _stream_main_py(cli_args, log_cap=300)
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


@app.route("/api/start-parse", methods=["POST"])
def api_start_parse():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания прогона «Алиса + сводка»."}), 409
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
    threading.Thread(
        target=_run_main_worker,
        kwargs={"cli_args": args, "task": "парсинг ЕИС"},
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/api/rebuild-report", methods=["POST"])
def api_rebuild_report():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания прогона «Алиса + сводка»."}), 409
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
        kwargs={"cli_args": ["--from-downloaded-tender-id", tid], "task": f"пересбор {tid}"},
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/api/rebuild-all-reports", methods=["POST"])
def api_rebuild_all_reports():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Сначала дождитесь окончания прогона «Алиса + сводка»."}), 409
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
        if merge_site_state["running"] and total > 0:
            pct = int(min(100, max(0, round(100.0 * done / total))))
        elif not merge_site_state["running"] and total > 0 and done >= total:
            pct = 100
        else:
            pct = 0 if total == 0 else int(min(100, max(0, round(100.0 * done / total))))
        payload = {
            "running": bool(merge_site_state["running"]),
            "total": total,
            "done": done,
            "percent": pct,
            "current_tid": merge_site_state.get("current_tid") or "",
            "started_at": merge_site_state.get("started_at"),
            "ended_at": merge_site_state.get("ended_at"),
            "error_ids": list(merge_site_state.get("error_ids") or []),
            "log_tail": (merge_site_state.get("log_lines") or [])[-60:],
            "last_ended_at": merge_site_state.get("last_ended_at"),
            "last_summary": merge_site_state.get("last_summary") or "",
            "last_reason_counts": merge_site_state.get("last_reason_counts") or {},
        }
    return jsonify(payload)


@app.route("/api/reports-coverage")
def api_reports_coverage():
    return jsonify(_compute_reports_coverage())


@app.route("/api/generate-merge-site-all", methods=["POST"])
def api_generate_merge_site_all():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Прогон «Алиса + сводка» уже выполняется."}), 409
    with parse_lock:
        if parse_state["running"]:
            return jsonify({"ok": False, "message": "Сначала дождитесь окончания парсинга или пересбора (main.py)."}), 409
    threading.Thread(target=_run_merge_site_all_worker, kwargs={"only_missing": False}, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/generate-merge-site-missing", methods=["POST"])
def api_generate_merge_site_missing():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Прогон «Алиса + сводка» уже выполняется."}), 409
    with parse_lock:
        if parse_state["running"]:
            return jsonify({"ok": False, "message": "Сначала дождитесь окончания парсинга или пересбора (main.py)."}), 409
    threading.Thread(target=_run_merge_site_all_worker, kwargs={"only_missing": True}, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/generate-merge-site-one", methods=["POST"])
def api_generate_merge_site_one():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Прогон «Алиса + сводка» уже выполняется."}), 409
    with parse_lock:
        if parse_state["running"]:
            return jsonify({"ok": False, "message": "Сначала дождитесь окончания парсинга или пересбора (main.py)."}), 409
    data = request.get_json(silent=True) or {}
    tid = str(data.get("tender_id", "")).strip()
    if not tid:
        return jsonify({"ok": False, "message": "Нужен tender_id"}), 400
    threading.Thread(target=_run_merge_site_all_worker, kwargs={"ids_override": [tid]}, daemon=True).start()
    return jsonify({"ok": True, "tender_id": tid})


@app.route("/api/generate-merge-site-one-rerun-alice", methods=["POST"])
def api_generate_merge_site_one_rerun_alice():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Прогон «Алиса + сводка» уже выполняется."}), 409
    with parse_lock:
        if parse_state["running"]:
            return jsonify({"ok": False, "message": "Сначала дождитесь окончания парсинга или пересбора (main.py)."}), 409
    data = request.get_json(silent=True) or {}
    tid = str(data.get("tender_id", "")).strip()
    if not tid:
        return jsonify({"ok": False, "message": "Нужен tender_id"}), 400
    threading.Thread(
        target=_run_merge_site_all_worker,
        kwargs={"ids_override": [tid], "force_alice_no_resume": True},
        daemon=True,
    ).start()
    return jsonify({"ok": True, "tender_id": tid, "mode": "rerun_alice_no_resume"})


@app.route("/api/generate-merge-site-by-link", methods=["POST"])
def api_generate_merge_site_by_link():
    if _merge_site_busy():
        return jsonify({"ok": False, "message": "Прогон «Алиса + сводка» уже выполняется."}), 409
    with parse_lock:
        if parse_state["running"]:
            return jsonify({"ok": False, "message": "Сначала дождитесь окончания парсинга или пересбора (main.py)."}), 409
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
    from autobot.merge_estimate_alice import OUT_PREFIX

    sv = REPORTS_DIR / f"{OUT_PREFIX}{tid}.xlsx"
    if not sv.is_file():
        return jsonify(
            {
                "ok": False,
                "message": "Нет файла СВОДКА_РЫНОК для этого номера. Сначала «Сделать отчёт (Алиса + merge + HTML)» или merge_estimate_alice.",
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
