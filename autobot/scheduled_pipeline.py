#!/usr/bin/env python3
"""
Плановый прогон для Планировщика Windows (или cron).

1) Тестовое сообщение в Telegram (время по Asia/Yekaterinburg в тексте).
2) main.py — парсинг ЕИС, отчёты, уведомления о новых тендерах.
3) Для каждого нового id (и при необходимости — для id из --with-tender-id):
   real_market_scraper.py → merge_estimate_market → HTML-сайт + (опц.) Excel в беседу.

Сайт: data/reports_site/<id>/index.html; отдаётся через тот же Flask, что и UI: py -3 tools/launch_web_ui.py
Путь в браузере: /merge-report/<id>/  В .env: REPORT_SITE_PUBLIC_BASE_URL или WEB_UI_PUBLIC_HOST+WEB_UI_PORT
С телефона в Wi‑Fi: WEB_UI_HOST=0.0.0.0 при запуске web_ui.
TELEGRAM_SEND_MERGE_EXCEL=1 — дополнительно прикрепить Excel сводки в чат (по умолчанию не шлём).

Проверка цепочки без «новых» тендеров в tenders.json:
  py scheduled_pipeline.py --with-tender-id 0171200001926001291
  (main всё равно отработает; сравнение цен пойдёт и по этому id, даже если он не новый.)

Время запуска:
- Windows: install_scheduled_tasks.ps1 (локальное время Windows).
- Linux/сервер: install_cron_tasks.sh (cron с CRON_TZ=Asia/Yekaterinburg).

Переменные (.env):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (id группы)
  SKIP_PERPLEXITY_TXT=1 — не слать .txt для Perplexity
  RUN_MARKET=1 — после main запускать Алису по новым id (0 — пропустить)
  MARKET_MAX_ROWS=0 — без лимита позиций на тендер (или задайте число)
  MARKET_AVITO_HEADLESS=1 — браузер без окна (нужен залогиненный профиль Яндекса)
  MARKET_SOURCES=1 — второе сообщение Алисе про сайты/телефоны
"""

from __future__ import annotations

from autobot.paths import DATA_DIR, REPO_ROOT, REPORTS_DIR

import html
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

from autobot.site_public_url import get_report_site_public_base

NEW_IDS_FILE = DATA_DIR / "last_new_tender_ids.txt"
_RUN_MODULE = REPO_ROOT / "tools" / "run_module.py"


def _tg_send_test() -> None:
    from autobot.telegram_notify import send_message, telegram_config

    cfg = telegram_config()
    if not cfg:
        print("Telegram: нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID в .env — тест не отправлен.")
        return
    tok, chat = cfg
    try:
        ekb = datetime.now(ZoneInfo("Asia/Yekaterinburg"))
        text = (
            "🧪 <b>Плановый запуск</b>\n"
            f"Время (Екатеринбург): <code>{ekb.strftime('%d.%m.%Y %H:%M')}</code>\n"
            "Далее — парсинг ЕИС и отчёты."
        )
        send_message(tok, chat, text)
    except Exception as e:
        print(f"Telegram тест не отправлен: {e}")


def _run_main(emit_path: Path) -> int:
    cmd = [
        sys.executable,
        str(_RUN_MODULE),
        "autobot.main",
        "--max-pages",
        os.environ.get("MAIN_MAX_PAGES", "2"),
        "--max-tenders",
        os.environ.get("MAIN_MAX_TENDERS", "15"),
        "--days-back",
        os.environ.get("MAIN_DAYS_BACK", "60"),
        "--emit-new-ids-to",
        str(emit_path),
    ]
    print(">>>", " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(REPO_ROOT))
    return int(r.returncode)


def _run_main_from_downloaded(tender_id: str) -> tuple[int, str]:
    cmd = [
        sys.executable,
        str(_RUN_MODULE),
        "autobot.main",
        "--from-downloaded-tender-id",
        (tender_id or "").strip(),
    ]
    print(">>>", " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, errors="replace")
    out = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
    return int(r.returncode), out.strip()


def _run_main_from_tender_url(tender_id: str, tender_url: str) -> tuple[int, str]:
    cmd = [
        sys.executable,
        str(_RUN_MODULE),
        "autobot.main",
        "--from-tender-id",
        (tender_id or "").strip(),
        "--from-tender-url",
        (tender_url or "").strip(),
    ]
    print(">>>", " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, errors="replace")
    out = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
    return int(r.returncode), out.strip()


def _read_new_ids(path: Path) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [x.strip() for x in lines if x.strip()]


def _parse_with_tender_ids(argv: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--with-tender-id" and i + 1 < len(argv):
            tid = argv[i + 1].strip()
            if tid:
                out.append(tid)
            i += 2
            continue
        i += 1
    return out


def _merge_tender_id_lists(primary: list[str], extra: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for tid in primary + extra:
        if tid and tid not in seen:
            seen.add(tid)
            merged.append(tid)
    return merged


def _run_market(tender_id: str) -> int:
    max_rows_raw = (os.environ.get("MARKET_MAX_ROWS") or os.environ.get("MARKET_MAX_ROWS") or "").strip()
    pause = (os.environ.get("MARKET_PAUSE_SEC") or os.environ.get("MARKET_PAUSE_SEC") or "4").strip() or "4"
    sources = (os.environ.get("MARKET_SOURCES") or "avito,web").strip() or "avito,web"
    max_results = (os.environ.get("MARKET_MAX_RESULTS") or "5").strip() or "5"
    cmd = [
        sys.executable,
        str(_RUN_MODULE),
        "autobot.real_market_scraper",
        "--tender-id",
        tender_id,
        "--pause",
        pause,
        "--sources",
        sources,
        "--max-results-per-row",
        max_results,
    ]
    if max_rows_raw:
        cmd.extend(["--max-rows", max_rows_raw])
    if (
        os.environ.get("MARKET_NO_RESUME", "").strip().lower() in ("1", "true", "yes", "on")
        or os.environ.get("MARKET_NO_RESUME", "").strip().lower() in ("1", "true", "yes", "on")
    ):
        cmd.append("--no-resume")
    print(">>>", " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(REPO_ROOT))
    return int(r.returncode)


def _run_merge(tender_id: str) -> Path | None:
    from autobot.merge_estimate_market import merge_estimate_and_market

    return merge_estimate_and_market(tender_id)


def _send_file(path: Path, caption: str) -> None:
    from autobot.telegram_notify import send_document_file, telegram_config

    cfg = telegram_config()
    if not cfg or not path.is_file():
        return
    tok, chat = cfg
    try:
        send_document_file(tok, chat, path, caption=caption[:1024])
    except Exception as e:
        print(f"Не удалось отправить файл {path}: {e}")


def _safe_tg_send(cfg: tuple[str, str] | None, text: str) -> None:
    if not cfg:
        return
    from autobot.telegram_notify import send_message

    try:
        send_message(cfg[0], cfg[1], text, parse_mode="HTML", disable_web_page_preview=False)
    except Exception:
        pass


def _estimate_positions_count(tender_id: str) -> int | None:
    """Количество работ в отчёте сметы ОТЧЕТ_ПО_СМЕТАМ_<id>.xlsx."""
    p = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.xlsx"
    if not p.is_file():
        return None
    try:
        import pandas as pd

        df = pd.read_excel(p)
        return int(len(df))
    except Exception:
        return None


def _report_exists(tender_id: str) -> bool:
    p = REPORTS_DIR / f"ОТЧЕТ_ПО_СМЕТАМ_{(tender_id or '').strip()}.xlsx"
    return p.is_file()


def _downloads_exist(tender_id: str) -> bool:
    d = DATA_DIR / "downloads" / (tender_id or "").strip()
    if not d.is_dir():
        return False
    try:
        next(d.rglob("*"))
        return True
    except StopIteration:
        return False
    except Exception:
        return False


def _downloads_count(tender_id: str) -> int:
    d = DATA_DIR / "downloads" / (tender_id or "").strip()
    if not d.is_dir():
        return 0
    try:
        return sum(1 for p in d.rglob("*") if p.is_file())
    except Exception:
        return 0


def _main_reason(output: str) -> str:
    if not output:
        return "подробный вывод отсутствует"
    lines = [x.strip() for x in output.splitlines() if x.strip()]
    if not lines:
        return "подробный вывод отсутствует"
    keys = [
        "документы не скачались",
        "ссылки на документы не найдены",
        "nav-error",
        "не скачал",
        "не удалось",
        "traceback",
        "error",
        "timeout",
        "timed out",
        "connecttimeout",
        "max retries exceeded",
        "captcha",
        "excel",
        "xlsx",
        "rar",
        "403",
        "404",
    ]
    picked = [ln for ln in lines if any(k in ln.lower() for k in keys)]
    msg = " | ".join((picked[-2:] if picked else lines[-2:]))
    return msg[:320] + ("..." if len(msg) > 320 else "")


def _load_tender_meta() -> dict[str, dict]:
    try:
        from autobot.report_prompt import load_tender_metadata

        return load_tender_metadata()
    except Exception:
        return {}


def _collect_backfill_ids(limit: int = 10) -> list[str]:
    """
    Тендеры из tenders.json, у которых нет сметного отчета.
    Нужны для единоразового "дожима", даже если новых в этом прогоне нет.
    """
    meta = _load_tender_meta()
    out: list[str] = []
    for tid in sorted(meta.keys()):
        # Отсекаем явно нерелевантные короткие номера (не ЕИС).
        if not str(tid).isdigit() or len(str(tid)) < 15:
            continue
        if not _report_exists(tid):
            out.append(tid)
        if len(out) >= limit:
            break
    return out


def _tender_publish_date(tender_id: str) -> str:
    """Дата публикации тендера из tenders.json (если есть)."""
    try:
        from autobot.report_prompt import load_tender_metadata

        meta = load_tender_metadata().get((tender_id or "").strip(), {})
        dt = str(meta.get("publish_date") or "").strip()
        return dt
    except Exception:
        return ""


def _progress_prefix(idx: int, total: int, tid: str) -> str:
    return f"📊 <b>{idx}/{total}</b> · <code>{tid}</code>"


def _report_site_public_url(tender_id: str) -> str | None:
    base = get_report_site_public_base()
    if not base:
        return None
    tid = (tender_id or "").strip()
    # Тот же Flask, что и web_ui.py (порт 8765 по умолчанию)
    return f"{base}/merge-report/{tid}/"


def _telegram_send_merge_excel() -> bool:
    """По умолчанию Excel в чат не шлём — только ссылка на сайт. Включить: TELEGRAM_SEND_MERGE_EXCEL=1."""
    explicit = (os.environ.get("TELEGRAM_SEND_MERGE_EXCEL") or "").strip().lower()
    if explicit in ("1", "true", "yes", "on"):
        return True
    return False


def main() -> None:
    test_only = "--test-only" in sys.argv
    extra_ids = _parse_with_tender_ids(sys.argv)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    _tg_send_test()
    if test_only and not extra_ids:
        print("Режим --test-only: main.py и сравнение цен не запускались.")
        return

    emit = NEW_IDS_FILE
    ids_from_file: list[str] = []
    if not test_only:
        code = _run_main(emit)
        if code != 0:
            print(f"main.py завершился с кодом {code}")
            from autobot.telegram_notify import telegram_config

            _safe_tg_send(
                telegram_config(),
                f"⚠️ <b>main.py</b> завершился с кодом <code>{code}</code>. Продолжаю обработку того, что удалось собрать.",
            )
        ids_from_file = _read_new_ids(emit)
    else:
        print("Режим --test-only: main.py не запускался (только --with-tender-id, если указан).")

    ids = _merge_tender_id_lists(ids_from_file, extra_ids)
    print(f"Новых id из main: {len(ids_from_file)} — {ids_from_file}")
    if extra_ids:
        print(f"Дополнительно --with-tender-id: {extra_ids}")
    print(f"Итого id для Алисы/merge: {len(ids)} — {ids}")

    # Если новых нет, но есть тендеры без сметного отчёта, пробуем их дожать.
    backfill_enabled = os.environ.get("BACKFILL_MISSING_REPORTS", "1").strip().lower() not in ("0", "false", "no", "off")
    backfill_recovered: list[str] = []
    if backfill_enabled:
        try:
            max_backfill = int((os.environ.get("BACKFILL_MAX_TENDERS") or "8").strip() or "8")
        except ValueError:
            max_backfill = 8
        max_backfill = max(1, min(max_backfill, 50))
        backfill_ids = _collect_backfill_ids(limit=max_backfill)
        if backfill_ids:
            from autobot.telegram_notify import telegram_config

            cfg_b = telegram_config()
            _safe_tg_send(
                cfg_b,
                f"🛠️ Запускаю дожим отчётов для тендеров без сметы: <b>{len(backfill_ids)}</b> шт.",
            )
            meta = _load_tender_meta()
            total_backfill = len(backfill_ids)
            for j, tid in enumerate(backfill_ids, start=1):
                pref = f"🧩 Дожим {j}/{total_backfill} · <code>{tid}</code>"
                url = str((meta.get(tid, {}) or {}).get("url") or "").strip()
                if _downloads_exist(tid):
                    _safe_tg_send(cfg_b, f"🟡 {pref}: есть downloads, собираю смету из скачанного.")
                    c, out_text = _run_main_from_downloaded(tid)
                elif url:
                    _safe_tg_send(cfg_b, f"🟡 {pref}: скачиваю документы по сохраненной ссылке.")
                    c, out_text = _run_main_from_tender_url(tid, url)
                else:
                    _safe_tg_send(cfg_b, f"⚠️ {pref}: нет URL в tenders.json, пропуск.")
                    continue
                if c == 0 and _report_exists(tid):
                    backfill_recovered.append(tid)
                else:
                    cnt = _downloads_count(tid)
                    reason = _main_reason(out_text)
                    _safe_tg_send(
                        cfg_b,
                        f"⚠️ {pref}: не удалось собрать смету (код {c}, файлов в downloads: {cnt}).\n"
                        f"Причина: <code>{html.escape(reason)}</code>",
                    )
                    low_reason = reason.lower()
                    if (
                        "err_connection_timed_out" in low_reason
                        or "connecttimeout" in low_reason
                        or "timed out" in low_reason
                        or "max retries exceeded" in low_reason
                    ):
                        _safe_tg_send(
                            cfg_b,
                            "⛔ Похоже, сейчас нет доступа к zakupki.gov.ru (сетевой timeout). "
                            "Останавливаю дожим в этом прогоне, чтобы не слать одинаковые ошибки.",
                        )
                        break
            if backfill_recovered:
                _safe_tg_send(
                    cfg_b,
                    f"✅ Дожим смет завершён: собрано <b>{len(backfill_recovered)}</b> отчётов. Запускаю Алису/сводку.",
                )
                ids = _merge_tender_id_lists(ids, backfill_recovered)
                print(f"Backfill recovered ids: {backfill_recovered}")

    run_market = os.environ.get("RUN_MARKET", "1").strip().lower() not in ("0", "false", "no", "off")
    if not run_market:
        print("RUN_MARKET отключён — пропуск market/merge.")
        return
    if not ids:
        print("Нет id для рынка: список новых пуст и --with-tender-id не задан.")
        from autobot.telegram_notify import telegram_config

        _safe_tg_send(
            telegram_config(),
            "ℹ️ <b>Рынок и сводка</b> не запускались: в этом прогоне нет <b>новых</b> id "
            "(файл новых id пуст — все найденные тендеры уже были в tenders.json).\n"
            "<i>Чтобы прогнать рынок по конкретному номеру: сайт → меню у тендера или "
            "<code>scheduled_pipeline.py --with-tender-id …</code>.</i>",
        )
        return

    from autobot.telegram_notify import send_message, telegram_config

    cfg = telegram_config()
    total = len(ids)
    for i, tid in enumerate(ids, start=1):
        print(f"--- Рынок: {tid} ---")
        pref = _progress_prefix(i, total, tid)
        pub_date = _tender_publish_date(tid)
        date_line = f"\n📅 Дата публикации: <code>{html.escape(pub_date)}</code>" if pub_date else ""
        _safe_tg_send(cfg, f"{pref}\n🟡 Старт")
        if date_line:
            _safe_tg_send(cfg, f"{pref}{date_line}")

        pos_count = _estimate_positions_count(tid)
        if pos_count is not None:
            _safe_tg_send(cfg, f"{pref}\n🟡 Смета: <b>{pos_count}</b> поз.")
        else:
            _safe_tg_send(cfg, f"{pref}\n🟡 Смета не найдена — продолжаю")

        _safe_tg_send(cfg, f"{pref}\n🟡 Поиск рынка…")
        ac = _run_market(tid)
        if ac != 0:
            if cfg:
                try:
                    send_message(cfg[0], cfg[1], f"⚠️ Рынок код <code>{ac}</code> · <code>{tid}</code>", parse_mode="HTML")
                except Exception:
                    pass
            continue
        _safe_tg_send(cfg, f"{pref}\n🟡 Merge…")
        out = _run_merge(tid)
        if out and out.is_file():
            site_url = None
            try:
                from autobot.report_merge_html import write_tender_report_site

                hp = write_tender_report_site(tid)
                if hp and hp.is_file():
                    site_url = _report_site_public_url(tid)
                    print(f"HTML-отчёт: {hp}")
                    _safe_tg_send(cfg, f"{pref}\n🟡 HTML готов")
            except Exception as e:
                print(f"report_merge_html: {e}")
                _safe_tg_send(cfg, f"{pref}\n⚠️ HTML: <code>{html.escape(str(e))}</code>")

            if _telegram_send_merge_excel():
                _send_file(
                    out,
                    f"Сводка {tid}",
                )

            if cfg:
                try:
                    if site_url:
                        safe_href = html.escape(site_url, quote=True)
                        send_message(
                            cfg[0],
                            cfg[1],
                            "✅ Готово · <code>"
                            + tid
                            + "</code>"
                            + (f"\n📅 <code>{html.escape(pub_date)}</code>" if pub_date else "")
                            + "\n🔗 <a href=\""
                            + safe_href
                            + "\">Отчёт</a>",
                            parse_mode="HTML",
                            disable_web_page_preview=False,
                        )
                    else:
                        send_message(
                            cfg[0],
                            cfg[1],
                            "✅ Готово · <code>"
                            + html.escape(tid)
                            + "</code>"
                            + (f"\n📅 <code>{html.escape(pub_date)}</code>" if pub_date else "")
                            + "\nExcel не в чате.\n"
                            f"<code>{html.escape(out.name)}</code>\n"
                            f"<code>data/reports_site/{html.escape(tid)}/index.html</code>\n"
                            "<i>Ссылка в сообщении — задайте REPORT_SITE_PUBLIC_BASE_URL в .env</i>",
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    try:
                        from autobot.tender_viability import format_viability_for_telegram

                        vmsg = format_viability_for_telegram(tid)
                        if vmsg:
                            send_message(
                                cfg[0],
                                cfg[1],
                                vmsg,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                    except Exception:
                        pass
                except Exception:
                    pass
        else:
            if cfg:
                try:
                    send_message(
                        cfg[0],
                        cfg[1],
                        f"ℹ️ Для № <code>{tid}</code> нет пары отчёт+рынок для merge (проверь файлы в data/reports/).",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass


if __name__ == "__main__":
    main()
