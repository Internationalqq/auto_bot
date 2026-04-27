# Tender Parser MVP

Архитектура: **[docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)**.

MVP-скрипт для:
- поиска тендеров на `zakupki.gov.ru` по регионам и ключевым словам;
- фильтрации по цене (`20-100 млн`) и этапу (`Подача заявок`);
- скачивания архивов документов (`zip/rar`);
- извлечения работ и цен из Excel-файлов в итоговый отчёт.

## Установка

1. Установите Python 3.11+.
2. Установите зависимости:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Запуск

Из корня репозитория (чтобы подхватился пакет `autobot/`):

```bash
py -3 tools/run_module.py autobot.main
```

Дополнительно:

```bash
py -3 tools/run_module.py autobot.main --max-tenders 20 --days-back 14
```

## UI для отчетов

После генерации HTML-отчетов можно открыть удобный интерфейс:

```bash
pip install -r requirements.txt
py -3 tools/launch_web_ui.py
```

Из корня репозитория. Откройте в браузере: `http://127.0.0.1:8765` (или `py -3 botctl.py web`).

В интерфейсе:
- список тендеров слева (сгруппирован по региону);
- выбор тендера;
- просмотр соответствующего HTML-отчета справа.

## Что создаётся

- `data/tenders.json` — список найденных тендеров;
- `data/downloads/<tender_id>/` — скачанные документы;
- `data/extracted/<tender_id>/` — распакованные архивы;
- `data/reports/works_report_<timestamp>.xlsx` — итоговая таблица.

## Важные ограничения

- Форматы смет сильно различаются, поэтому извлечение колонок делается эвристически.
- Для `rar` может понадобиться установленный `unrar` в системе, иначе такие архивы будут пропущены.
- Верстка/селекторы `zakupki.gov.ru` могут меняться; в таком случае нужно обновить селекторы в `autobot/main.py`.
