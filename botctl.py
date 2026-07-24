#!/usr/bin/env python3
"""
Единая точка входа для AutoBot.

Одна команда для установки расписания:
  py -3 botctl.py install      (Windows)
  python3 botctl.py install    (Linux/Server)

Запуск вручную (один прогон):
  py -3 botctl.py run-now

Запуск веб-интерфейса:
  py -3 botctl.py web

Справка по товару/услуге/материалу:
  py -3 botctl.py info "бетон м300"
  py -3 botctl.py tg-bot
"""

from __future__ import annotations

import argparse
import html
import platform
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def _notify_tg_schedule(kind: str) -> None:
    """Уведомление в Telegram после install/remove расписания. kind: install | remove."""
    root = str(BASE_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE_DIR / ".env")
    except ImportError:
        pass
    try:
        from autobot.telegram_notify import send_message, telegram_config

        cfg = telegram_config()
        if not cfg:
            print("Telegram: нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — уведомление о расписании не отправлено.")
            return
        tok, chat = cfg
        host = html.escape(platform.node())
        if kind == "remove":
            text = "<b>Расписание снято</b>\n" f"ПК: <code>{host}</code>"
        else:
            if "windows" in platform.system().lower():
                detail = "Планировщик Windows: задача <code>AutoBotEISPipeline</code> (времена из <code>PIPELINE_SCHEDULE_TIMES</code> в .env или 09:00, 18:00, 21:00)."
            else:
                detail = "Cron: записи из <code>tools/install_cron_tasks.sh</code> (см. crontab)."
            text = (
                "<b>Расписание установлено</b>\n"
                + detail
                + "\n"
                + f"ПК: <code>{host}</code>\n"
                + "<i>Проверка: Windows — <code>taskschd.msc</code>; Linux — <code>crontab -l</code>.</i>"
            )
        send_message(tok, chat, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        print(f"Telegram: не удалось отправить уведомление о расписании: {e}")


def _run(cmd: list[str]) -> int:
    print(">>>", " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(BASE_DIR))
    return int(r.returncode)


def cmd_install(remove: bool = False) -> int:
    sysname = platform.system().lower()
    if "windows" in sysname:
        script = BASE_DIR / "tools" / "install_scheduled_tasks.ps1"
        args = [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
        if remove:
            # Для Windows удаляем задачу напрямую.
            task_name = "AutoBotEISPipeline"
            rc = _run(["schtasks", "/Delete", "/TN", task_name, "/F"])
            if rc == 0:
                _notify_tg_schedule("remove")
            return rc
        rc = _run(args)
        if rc == 0:
            _notify_tg_schedule("install")
        return rc

    script = BASE_DIR / "tools" / "install_cron_tasks.sh"
    args = ["bash", str(script)]
    if remove:
        args.append("--remove")
    rc = _run(args)
    if rc == 0:
        _notify_tg_schedule("remove" if remove else "install")
    return rc


def cmd_run_now() -> int:
    return _run([sys.executable, str(BASE_DIR / "tools" / "launch_scheduled_pipeline.py")])


def cmd_web() -> int:
    return _run([sys.executable, str(BASE_DIR / "tools" / "launch_web_ui.py")])


def cmd_info(query: str, *, send_telegram: bool = False) -> int:
    cmd = [
        sys.executable,
        str(BASE_DIR / "tools" / "run_module.py"),
        "autobot.item_research",
        query,
    ]
    if send_telegram:
        cmd.append("--send-telegram")
    return _run(cmd)


def cmd_tg_bot() -> int:
    return _run([sys.executable, str(BASE_DIR / "tools" / "run_module.py"), "autobot.telegram_research_bot"])


def main() -> int:
    ap = argparse.ArgumentParser(description="Управление пайплайном AutoBot одной командой")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("install", help="Установить расписание (Windows Task Scheduler или cron)")
    sub.add_parser("remove", help="Удалить расписание")
    sub.add_parser("run-now", help="Запустить один прогон сейчас")
    sub.add_parser("web", help="Запустить веб-интерфейс (tools/launch_web_ui.py)")
    p_info = sub.add_parser("info", help="Собрать краткую сводку по услуге, товару или материалу")
    p_info.add_argument("query", nargs="+", help="Например: бетон м300")
    p_info.add_argument("--send-telegram", action="store_true", help="Отправить сводку в TELEGRAM_CHAT_ID")
    sub.add_parser("tg-bot", help="Запустить Telegram-обработчик команд /info и /research")

    args = ap.parse_args()

    if args.cmd == "install":
        return cmd_install(remove=False)
    if args.cmd == "remove":
        return cmd_install(remove=True)
    if args.cmd == "run-now":
        return cmd_run_now()
    if args.cmd == "web":
        return cmd_web()
    if args.cmd == "info":
        return cmd_info(" ".join(args.query), send_telegram=bool(args.send_telegram))
    if args.cmd == "tg-bot":
        return cmd_tg_bot()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
