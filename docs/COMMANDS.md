# Команды запуска (auto_bot)

Все команды выполняйте **из корня репозитория** (каталог, где лежат `botctl.py`, `tools/`, `autobot/`).

На Windows ниже используется **`py -3`**. Если у вас только `python`, замените на `python` (или `python3` на Linux).

Почему не `py -3 -m autobot…`: у launcher `py` часто бывает изоляция окружения, из‑за чего **`-m`** и **`PYTHONPATH`** могут не подхватить пакет. Скрипты в **`tools/`** добавляют корень репо в `sys.path` — это надёжный способ.

---

## Один раз (зависимости)

```bash
pip install -r requirements.txt
py -3 -m playwright install chromium
```

---

## Обёртка `botctl.py`

```bash
py -3 botctl.py install     # расписание: Windows — Task Scheduler; Linux — cron
py -3 botctl.py remove      # снять расписание
py -3 botctl.py run-now     # один полный прогон пайплайна сейчас
py -3 botctl.py web         # веб-интерфейс
```

---

## Веб-интерфейс (без botctl)

```bash
py -3 tools/launch_web_ui.py
```

Обычно в браузере: `http://127.0.0.1:8765` (точный порт смотрите в выводе консоли или в `.env`).

---

## Плановый пайплайн вручную

Как при срабатывании расписания:

```bash
py -3 tools/launch_scheduled_pipeline.py
```

С дополнительным тендером (даже если он не «новый» в списке):

```bash
py -3 tools/launch_scheduled_pipeline.py --with-tender-id 0171200001926001291
```

---

## Парсинг ЕИС (`autobot.main`)

```bash
py -3 tools/run_module.py autobot.main --help
py -3 tools/run_module.py autobot.main
py -3 tools/run_module.py autobot.main --max-tenders 20 --days-back 14
```

Пересборка отчёта из уже скачанных файлов:

```bash
py -3 tools/run_module.py autobot.main --from-downloaded-tender-id <TENDER_ID>
```

Точечная загрузка по карточке тендера:

```bash
py -3 tools/run_module.py autobot.main --from-tender-id <TENDER_ID> --from-tender-url "<URL>"
```

---

## Алиса (`autobot.alice_market_scraper`)

```bash
py -3 tools/run_alice.py --help
py -3 tools/run_alice.py --tender-id <TENDER_ID>
```

Из **cmd** (из корня репо):

```cmd
tools\run_alice.cmd --tender-id <TENDER_ID>
```

Универсальный вариант (тот же модуль):

```bash
py -3 tools/run_module.py autobot.alice_market_scraper --tender-id <TENDER_ID>
```

Лимит строк Алисы в `.env`:

```bash
ALICE_MAX_ROWS=0
```

`0` — без лимита (по умолчанию). Если нужен потолок, поставьте число, например `50`.

---

## Склейка смета + Алиса (`merge_estimate_alice`)

```bash
py -3 tools/run_module.py autobot.merge_estimate_alice --tender-id <TENDER_ID>
```

Справка по флагам:

```bash
py -3 tools/run_module.py autobot.merge_estimate_alice --help
```

---

## Любой модуль пакета `autobot`

```bash
py -3 tools/run_module.py autobot.<имя_модуля> [аргументы...]
```

Примеры:

```bash
py -3 tools/run_module.py autobot.report_merge_html --help
py -3 tools/run_module.py autobot.serve_report_site --help
```

---

## Связанные документы

- Общая архитектура и поток данных: [ARCHITECTURE.md](./ARCHITECTURE.md)
