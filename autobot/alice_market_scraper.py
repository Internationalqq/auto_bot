"""
Сбор ответов веб-Алисы (alice.yandex.ru) по строкам сметы: запрос с регионом и сохранение текста ответа.

ВАЖНО:
- Автоматизация браузерного чата может противоречить пользовательскому соглашению Яндекса; возможны капча,
  блокировка, изменение вёрстки. Используйте на свой риск, разумные паузы, не злоупотребляйте объёмом.
- Нужен вход в аккаунт Яндекса: первый раз запустите с --headed, откройте сессию в профиле (--user-data-dir).
- После обновления сайта селекторы могут сломаться: задайте ALICE_INPUT_SELECTOR в окружении или правьте DEFAULT_INPUT_SELECTORS.

Зависимости:
  pip install playwright pandas openpyxl
  playwright install chromium

По умолчанию в запрос добавляется фраза вида: «Можешь посмотреть на сайтах и дать цены на <регион>…».
  С --two-step та же мысль уходит вторым сообщением (первое без этого блока). Отключить: --no-sites-in-prompt.

Пример:
  py alice_market_scraper.py --xlsx data/reports/ОТЧЕТ_ПО_СМЕТАМ_XXX.xlsx --region "Ставропольский край" --max-rows 5 --headed
  py alice_market_scraper.py --tender-id 0121200004726000378 --two-step --pause 25

На Windows, если «python» без pip: запуск через run_alice.cmd или py -3 alice_market_scraper.py ...

Повторный запуск по тому же тендеру: по умолчанию подхватывается уже сохранённый АЛИСА_РЫНОК_*.xlsx — в лимит
max-rows попадают только работы без ориентира рынка (как в merge). Полный перезапрос первых строк: --no-resume.

В Telegram (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID): короткий «живой» статус по позиции —
сообщение «в процессе» удаляется после ответа, остаётся краткое «✅ N/M» до начала следующей строки;
ошибки/пустые ответы в чат не дублируются, пишутся в stderr и в data/logs/alice_row_issues.log.
Отключить весь прогресс в TG: ALICE_TG_CHAT_PROGRESS=0.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from autobot.paths import REPO_ROOT
from autobot.market_analytics import (
    COL_DUP,
    COL_ITEM,
    COL_NAME,
    COL_QTY,
    COL_UNIT,
    COL_UNIT_PRICE,
    _load_report_xlsx,
    extract_ruble_amounts,
)
from autobot.merge_estimate_alice import _norm_key
from autobot.report_prompt import REPORTS_DIR, load_tender_metadata
from autobot.text_contacts import collect_phones, collect_urls

ALICE_URL = "https://alice.yandex.ru/"

# Порядок: пробуем по очереди (Playwright locator строка).
DEFAULT_INPUT_SELECTORS: list[str] = [
    "textarea",
    '[contenteditable="true"]',
    'div[role="textbox"]',
    '[placeholder*="просите" i]',
    '[placeholder*="Спросите" i]',
    '[placeholder*="Алис" i]',
    '[aria-label*="Сообщение" i]',
    '[data-testid*="input" i]',
]

_TWO_STEP_SEP = "\n\n--- уточнение ---\n\n"
RUB_PRICE_RE = re.compile(
    r"(?P<n>\d{1,3}(?:[\s\u00a0]\d{3})+|\d+(?:[.,]\d+)?)\s*(?P<scale>тыс\.?|млн\.?)?\s*(?:руб(?:\.|лей|ля)?|₽)\b",
    re.IGNORECASE,
)

_MOJIBAKE_ALICE_COLUMNS = {
    "РћС‚РІРµС‚ РђР»РёСЃС‹": "Ответ Алисы",
    "РћС‚РІРµС‚ РђР»РёСЃС‹ (РїРѕР»РЅС‹Р№)": "Ответ Алисы (полный)",
    "Р¦РµРЅС‹ Р·Р° РµРґ. (СЂС‹РЅРѕРє, СЂСѓР±)": "Цены за ед. (рынок, руб)",
    "РњРµРґРёР°РЅР° С†РµРЅР° Р·Р° РµРґ. (СЂС‹РЅРѕРє)": "Медиана цена за ед. (рынок)",
    "РњРёРЅ С†РµРЅР° Р·Р° РµРґ. (СЂС‹РЅРѕРє)": "Мин цена за ед. (рынок)",
    "РњР°РєСЃ С†РµРЅР° Р·Р° РµРґ. (СЂС‹РЅРѕРє)": "Макс цена за ед. (рынок)",
    "РўРµР»РµС„РѕРЅС‹ (СЃС‚СЂРѕРіРѕ)": "Телефоны (строго)",
    "РЎСЃС‹Р»РєРё (СЃС‚СЂРѕРіРѕ)": "Ссылки (строго)",
    "Р¦РµРЅР°-СЃР°Р№С‚-С‚РµР»РµС„РѕРЅ (json)": "Цена-сайт-телефон (json)",
    "РСЃС‚РѕС‡РЅРёРєРё (СЃСЃС‹Р»РєРё/С‚РµР»РµС„РѕРЅС‹)": "Источники (ссылки/телефоны)",
    "РћС€РёР±РєР° / СЃС‚Р°С‚СѓСЃ": "Ошибка / статус",
}


def _alice_web_event_path(tender_id: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", (tender_id or "unknown").strip())[:80] or "unknown"
    return REPO_ROOT / "data" / "logs" / f"alice_web_events_{safe}.jsonl"


def _append_alice_web_event(
    tender_id: str,
    kind: str,
    seq: int,
    total: int,
    *,
    work_name: str = "",
    detail: str = "",
) -> None:
    if total <= 0 or seq <= 0:
        return
    event = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": "alice",
        "kind": kind,
        "tender_id": (tender_id or "").strip(),
        "seq": int(seq),
        "total": int(total),
        "work_name": (work_name or "").strip()[:500],
        "detail": (detail or "").strip()[:500],
    }
    try:
        path = _alice_web_event_path(tender_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def sites_request_phrase(region: str) -> str:
    """
    Уточнение «как второе сообщение» в переписке: посмотреть на сайтах и дать цены по региону.
    """
    reg = (region or "").strip() or "этот регион"
    return (
        f"Посмотри коммерческие сайты по региону {reg} и верни только факты без пояснений. "
        "Важно: в ЦЕНЫ_РУБ укажи только ориентиры **цены за единицу измерения** этой работы "
        "(руб за м², за шт, за п.м и т.п.), **не** полную стоимость по объёму закупки и не итоги по объекту.\n"
        "Верни блок ИСТОЧНИКИ, где у КАЖДОЙ цены обязательно есть URL источника:\n"
        "ИСТОЧНИКИ:\n"
        "1) ЦЕНА_РУБ=<число>; URL=<https://... или домен вида example.ru>; ТЕЛ=<номер или ->\n"
        "2) ЦЕНА_РУБ=<число>; URL=<https://... или домен>; ТЕЛ=<номер или ->\n"
        "...\n"
        "Если нет URL для цены — НЕ включай такую цену в список.\n"
        "После блока ИСТОЧНИКИ добавь сводку (для совместимости парсера):\n"
        "ЦЕНЫ_РУБ: <числа через ;>\n"
        "ТЕЛЕФОНЫ: <номера через ;>\n"
        "ССЫЛКИ: <url через ;>\n"
        "КОМПАНИИ: <кратко через ;>"
    )


def _resolve_followup(two_step: bool, followup_text: str | None, region: str) -> str | None:
    """Второе сообщение: свой текст или шаблон с регионом."""
    ft = (followup_text or "").strip()
    if ft:
        return ft
    if two_step:
        return sites_request_phrase(region)
    return None

# Подписи интерфейса alice.yandex.ru, часто попадающие в inner_text(body)
_UI_EXACT_LINES: frozenset[str] = frozenset(
    {
        "я",
        "ты",
        "вы",
        "алиса",
        "alice",
        "промптхаб",
        "prompthub",
        "новый чат",
        "чат",
        "alice ai",
        "в промптхаб",
    }
)


def _is_na_scalar(x) -> bool:
    if x is None:
        return True
    try:
        return bool(pd.isna(x))
    except (TypeError, ValueError):
        return False


def _normalize_unit_for_prompt(unit: str, qty) -> str:
    """
    В отчётах иногда в «Ед. изм.» попадает «100 м2» вместо «м2» — в запрос уходит только измерение + кол-во.
    """
    u = (unit or "").strip().replace("\xa0", " ")
    if not u and _is_na_scalar(qty):
        return ""
    m = re.match(r"^[\d\s.,]+\s+(.+)$", u)
    if m:
        u = m.group(1).strip()
    parts: list[str] = []
    if u:
        parts.append(u)
    if not _is_na_scalar(qty):
        try:
            qf = float(qty)
            if qf > 0:
                q_str = f"{qf:g}" if qf == int(qf) else f"{qf:.4g}".rstrip("0").rstrip(".")
                parts.insert(0, f"кол-во по смете {q_str}")
        except (TypeError, ValueError):
            pass
    if not parts:
        return ""
    return " (" + ", ".join(parts) + ")"


def build_alice_prompt(
    work_name: str,
    region: str,
    unit: str = "",
    qty=None,
    *,
    include_sites_request: bool = True,
) -> str:
    """
    Работа + стоимость + регион (+ кол-во/ед. изм.).
    Если include_sites_request — в том же сообщении добавляется просьба посмотреть сайты и дать цены по региону
    (как ваше второе сообщение в чате). Если ответ пойдёт отдельным follow-up, передайте include_sites_request=False.
    """
    w = (work_name or "").strip()
    reg = (region or "").strip()
    if not w:
        return ""
    base = f"{w} стоимость {reg}".strip()
    suffix = _normalize_unit_for_prompt(unit, qty)
    if suffix:
        base = f"{base}{suffix}"
    if include_sites_request and reg:
        base = f"{base}. {sites_request_phrase(reg)}"
    return base


def _is_ui_chrome_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    low = s.casefold()
    if low in _UI_EXACT_LINES:
        return True
    if len(s) <= 48 and "промптхаб" in low:
        return True
    return False


def _strip_echoed_user_message(text: str, echo: str) -> str:
    """Убираем повтор отправленного запроса из сырого inner_text."""
    t = text.strip()
    e = (echo or "").strip()
    if not e or not t:
        return t
    for _ in range(4):
        changed = False
        if t.startswith(e):
            t = t[len(e) :].lstrip("\n").strip()
            changed = True
        first, _, rest = t.partition("\n")
        if first.strip() == e:
            t = rest.strip()
            changed = True
        if not changed:
            break
    return t


def _strip_leading_ui_block(t: str) -> str:
    """Срезает подряд идущие строки-хром (в т.ч. после удаления эха запроса)."""
    lines = t.split("\n")
    i = 0
    while i < len(lines) and _is_ui_chrome_line(lines[i]):
        i += 1
    return "\n".join(lines[i:]).strip()


def sanitize_alice_reply(raw: str, user_echo: str) -> str:
    """
    Убирает эхо сообщения пользователя, подписи «Я / Алиса / Промптхаб» и лишние пустые строки.
    Для двухшагового сценария вызывайте отдельно для ответа на основной запрос и на follow-up.
    """
    if not (raw or "").strip():
        return ""
    t = raw.replace("\r\n", "\n").strip()
    t = _strip_leading_ui_block(t)
    t = re.sub(r"^\s*\n+", "", t)
    echo = (user_echo or "").strip()
    if echo:
        t = _strip_echoed_user_message(t, echo)
    t = _strip_leading_ui_block(t)
    t = re.sub(r"^(алиса|alice)\s*\n+", "", t, count=1, flags=re.IGNORECASE).strip()
    t = _strip_leading_ui_block(t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


def _parse_ru_number(text: str) -> float | None:
    s = (text or "").replace("\u00a0", " ").strip()
    if not s:
        return None
    s = s.replace(" ", "").replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    if v <= 0 or v != v:  # noqa: PLR0124
        return None
    return v


def _parse_ceny_rub_line(text: str) -> list[float]:
    """
    Берёт числа только из строки «ЦЕНЫ_РУБ: …», без обхода всего inner_text страницы
    (там часто попадают чужие суммы и итоги).
    """
    if not text:
        return []
    t = text.replace("\r\n", "\n")
    out: list[float] = []
    for raw_line in t.split("\n"):
        line = raw_line.strip()
        m = re.match(r"(?i)цены[_\s]руб\s*:\s*(.*)$", line)
        if not m:
            continue
        rest = (m.group(1) or "").strip()
        if not rest or rest in ("—", "-", "нет", "н/д"):
            return []
        for part in re.split(r"\s*;\s*", rest):
            part = (part or "").strip()
            if not part:
                continue
            part = re.sub(r"\s*(?:руб\.?|₽|рублей)\s*$", "", part, flags=re.I).strip()
            compact = re.sub(r"[\s\u00a0\u202f]+", "", part.replace(",", "."))
            v = _parse_ru_number(compact)
            if v is not None and 1.0 <= v <= 500_000_000:
                out.append(round(v, 2))
        return sorted(set(out))
    return []


def _extract_market_prices_strict(text: str) -> list[float]:
    if not text:
        return []
    out: list[float] = []
    for m in RUB_PRICE_RE.finditer(text):
        v = _parse_ru_number(m.group("n") or "")
        if v is None:
            continue
        scale = (m.group("scale") or "").strip().casefold()
        if scale.startswith("тыс"):
            v *= 1_000
        elif scale.startswith("млн"):
            v *= 1_000_000
        if 50 <= v <= 500_000_000:
            out.append(round(v, 2))
    return sorted(set(out))


def _filter_market_prices_by_estimate_unit(
    prices: list[float],
    unit_price: object,
    qty: object,
) -> list[float]:
    """
    Отбрасывает значения уровня «сумма по строке сметы» и прочие выбросы,
    если из сметы известны цена за ед. и количество.
    """
    if not prices:
        return []
    nums = sorted({float(p) for p in prices if p == p and float(p) > 0})
    if not nums:
        return []
    up: float | None = None
    try:
        if not _is_na_scalar(unit_price):
            up = float(unit_price)
    except (TypeError, ValueError):
        up = None
    qv: float | None = None
    try:
        if not _is_na_scalar(qty):
            qv = float(qty)
    except (TypeError, ValueError):
        qv = None
    if up and up > 0 and qv and qv > 0:
        ceiling = up * qv * 1.22
        nums = [p for p in nums if p <= ceiling * 1.02]
    if up and up > 0:
        lo = max(0.5, up * 0.02)
        hi = min(5_000_000.0, up * 250)
        nums = [p for p in nums if lo <= p <= hi]
    if not nums and up and up > 0:
        # мягкий fallback: только потолок по сумме строки
        nums = sorted({float(p) for p in prices if p == p and float(p) > 0})
        if qv and qv > 0:
            ceiling = up * qv * 1.22
            nums = [p for p in nums if p <= ceiling * 1.02]
    if not nums:
        nums = sorted({float(p) for p in prices if p == p and float(p) > 0})
        if len(nums) >= 4:
            med = statistics.median(nums)
            nums = [p for p in nums if med * 0.05 <= p <= med * 80]
    return sorted(set(nums))[:12]


def _extract_unit_prices_from_alice_reply(text: str, unit_price: object, qty: object) -> list[float]:
    """Приоритет: строка ЦЕНЫ_РУБ; иначе рубли по всему тексту (хуже), затем фильтр по смете."""
    from_line = _parse_ceny_rub_line(text)
    if from_line:
        return _filter_market_prices_by_estimate_unit(from_line, unit_price, qty)
    strict = _extract_market_prices_strict(text)
    return _filter_market_prices_by_estimate_unit(strict, unit_price, qty)


def _parse_ceny_rub_line_ordered(text: str) -> list[float]:
    """Цены из строки ЦЕНЫ_РУБ без сортировки/дедупа (для связки цена↔ссылка↔телефон)."""
    if not text:
        return []
    t = text.replace("\r\n", "\n")
    out: list[float] = []
    for raw_line in t.split("\n"):
        line = raw_line.strip()
        m = re.match(r"(?i)цены[_\s]руб\s*:\s*(.*)$", line)
        if not m:
            continue
        rest = (m.group(1) or "").strip()
        if not rest or rest in ("—", "-", "нет", "н/д"):
            return []
        for part in re.split(r"\s*;\s*", rest):
            part = (part or "").strip()
            if not part:
                continue
            part = re.sub(r"\s*(?:руб\.?|₽|рублей)\s*$", "", part, flags=re.I).strip()
            compact = re.sub(r"[\s\u00a0\u202f]+", "", part.replace(",", "."))
            v = _parse_ru_number(compact)
            if v is not None and 1.0 <= v <= 500_000_000:
                out.append(round(v, 2))
        return out
    return []


def _parse_labeled_list_line(text: str, patterns: list[str]) -> list[str]:
    """Значения из строки формата `ЛЕЙБЛ: v1; v2; ...` (если есть)."""
    if not text:
        return []
    t = text.replace("\r\n", "\n")
    for raw_line in t.split("\n"):
        line = raw_line.strip()
        for pat in patterns:
            m = re.match(pat, line, flags=re.IGNORECASE)
            if not m:
                continue
            rest = (m.group(1) or "").strip()
            if not rest or rest in ("—", "-", "нет", "н/д"):
                return []
            parts = [x.strip() for x in re.split(r"\s*;\s*", rest) if x and x.strip()]
            return list(dict.fromkeys(parts))
    return []


def _extract_urls(text: str) -> list[str]:
    if not text:
        return []
    from_line = _parse_labeled_list_line(
        text,
        patterns=[
            r"ссылки\s*:\s*(.*)$",
            r"url(?:s)?\s*:\s*(.*)$",
            r"источники\s*:\s*(.*)$",
        ],
    )
    if from_line:
        out: list[str] = []
        for val in from_line:
            out.extend(collect_urls(val))
        if out:
            return list(dict.fromkeys(out))[:24]
    return list(dict.fromkeys(collect_urls(text)))[:24]


def _extract_phones(text: str) -> list[str]:
    if not text:
        return []
    from_line = _parse_labeled_list_line(
        text,
        patterns=[
            r"телефоны\s*:\s*(.*)$",
            r"контакты\s*:\s*(.*)$",
            r"телефон\s*:\s*(.*)$",
        ],
    )
    if from_line:
        out: list[str] = []
        for val in from_line:
            out.extend(collect_phones(val))
        if out:
            return list(dict.fromkeys(out))[:24]
    return list(dict.fromkeys(collect_phones(text)))[:24]


def _build_price_source_phone_bundle(
    text: str,
    *,
    unit_price: object,
    qty: object,
) -> list[dict[str, str]]:
    """
    Пары/тройки для показа «цена — сайт — телефон» в том же порядке, что в ответе Алисы.
    """
    def _triplets_from_sources_block(src_text: str) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        if not src_text:
            return out
        t = src_text.replace("\r\n", "\n")
        for raw_line in t.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            if not re.match(r"^\s*\d+\)", line):
                continue
            # Приоритет: явная метка ЦЕНА_РУБ=...
            p_val: float | None = None
            mp = re.search(r"(?i)цена[_\s]?руб\s*[:=]\s*([0-9][0-9\s.,]*)", line)
            if mp:
                p_val = _parse_ru_number(mp.group(1))
            if p_val is None:
                for m in RUB_PRICE_RE.finditer(line):
                    p_val = _parse_ru_number(m.group("n") or "")
                    if p_val is not None:
                        scale = (m.group("scale") or "").strip().casefold()
                        if scale.startswith("тыс"):
                            p_val *= 1_000
                        elif scale.startswith("млн"):
                            p_val *= 1_000_000
                        break
            urls = collect_urls(line)
            phs = collect_phones(line)
            if p_val is None:
                continue
            out.append(
                {
                    "price": f"{round(float(p_val), 2):.2f}",
                    "url": (urls[0] if urls else ""),
                    "phone": (re.sub(r"\s+", " ", phs[0].strip()) if phs else ""),
                }
            )
        return out

    triplets = _triplets_from_sources_block(text)
    if triplets:
        triplet_prices = [float(x["price"]) for x in triplets if x.get("price")]
        filtered_trip = _filter_market_prices_by_estimate_unit(triplet_prices, unit_price, qty)
        allowed_trip = {round(float(x), 2) for x in filtered_trip}
        out_trip: list[dict[str, str]] = []
        seen_trip: set[float] = set()
        for it in triplets:
            try:
                rp = round(float(it.get("price", "")), 2)
            except (TypeError, ValueError):
                continue
            if allowed_trip and rp not in allowed_trip:
                continue
            if rp in seen_trip:
                continue
            seen_trip.add(rp)
            out_trip.append(
                {
                    "price": f"{rp:.2f}",
                    "url": str(it.get("url", "") or "").strip(),
                    "phone": str(it.get("phone", "") or "").strip(),
                }
            )
        if out_trip:
            return out_trip[:12]

    ordered_prices = _parse_ceny_rub_line_ordered(text)
    filtered = _filter_market_prices_by_estimate_unit(ordered_prices, unit_price, qty)
    allowed = {round(float(x), 2) for x in filtered}
    kept_prices: list[float] = []
    seen: set[float] = set()
    for p in ordered_prices:
        rp = round(float(p), 2)
        if allowed and rp not in allowed:
            continue
        if rp in seen:
            continue
        seen.add(rp)
        kept_prices.append(rp)
    if not kept_prices and filtered:
        kept_prices = [round(float(x), 2) for x in filtered]

    links = _parse_labeled_list_line(text, [r"ссылки\s*:\s*(.*)$", r"url(?:s)?\s*:\s*(.*)$", r"источники\s*:\s*(.*)$"])
    urls: list[str] = []
    for val in links:
        urls.extend(collect_urls(val))
    if not urls:
        urls = _extract_urls(text)

    tel_vals = _parse_labeled_list_line(text, [r"телефоны\s*:\s*(.*)$", r"контакты\s*:\s*(.*)$", r"телефон\s*:\s*(.*)$"])
    phones: list[str] = []
    for val in tel_vals:
        phones.extend(collect_phones(val))
    if not phones:
        phones = _extract_phones(text)
    phones = [re.sub(r"\s+", " ", x.strip()) for x in phones if x and str(x).strip()]

    n = max(len(kept_prices), len(urls), len(phones))
    if n <= 0:
        return []
    out: list[dict[str, str]] = []
    for i in range(n):
        price_v = kept_prices[i] if i < len(kept_prices) else None
        out.append(
            {
                "price": f"{price_v:.2f}" if price_v is not None else "",
                "url": str(urls[i]).strip() if i < len(urls) else "",
                "phone": str(phones[i]).strip() if i < len(phones) else "",
            }
        )
    return out


def _fmt_prices(values: list[float]) -> str:
    if not values:
        return ""
    return "; ".join(f"{v:,.0f}".replace(",", " ") for v in values[:12])


def _diff_appended_text(before: str, after: str) -> str:
    """Новый фрагмент ленты: обычно ответ дописывается к body.inner_text()."""
    b = (before or "").strip()
    a = (after or "").strip()
    if not a:
        return ""
    if a.startswith(b):
        return a[len(b) :].strip()
    # общий префикс
    i = 0
    lim = min(len(b), len(a))
    while i < lim and b[i] == a[i]:
        i += 1
    return a[i:].strip()


def _find_input_locator(page, selectors: list[str]):
    from playwright.sync_api import Page

    assert isinstance(page, Page)
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue
            first = loc.first
            try:
                first.wait_for(state="visible", timeout=5000)
            except Exception:
                continue
            return first
        except Exception:
            continue
    return None


def _body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=15000) or ""
    except Exception:
        return ""


def _send_message(page, text: str, selectors: list[str]) -> str | None:
    """Ввод и отправка. Возвращает текст ошибки или None."""
    inp = _find_input_locator(page, selectors)
    if inp is None:
        return "не найдено поле ввода (см. ALICE_INPUT_SELECTOR и DEFAULT_INPUT_SELECTORS)"
    try:
        inp.click(timeout=5000)
    except Exception:
        pass
    try:
        if inp.evaluate("el => el.tagName === 'TEXTAREA' || el.tagName === 'INPUT'"):
            inp.fill("")
            inp.fill(text)
        else:
            inp.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.insert_text(text)
    except Exception as e:
        return f"ввод текста: {e}"[:400]
    time.sleep(0.2)
    try:
        page.keyboard.press("Enter")
    except Exception as e:
        return f"Enter: {e}"[:200]
    return None


def _wait_for_growth_stable(
    page,
    snapshot_before: str,
    *,
    min_new_chars: int,
    timeout_sec: float,
    stable_rounds: int = 3,
    poll: float = 1.0,
) -> str:
    """
    Ждём, пока к тексту страницы добавится блок >= min_new_chars и несколько опросов подряд не меняется.
    """
    deadline = time.monotonic() + timeout_sec
    last_growth = ""
    stable = 0
    while time.monotonic() < deadline:
        time.sleep(poll)
        after = _body_text(page)
        growth = _diff_appended_text(snapshot_before, after)
        if len(growth) >= min_new_chars:
            if growth == last_growth:
                stable += 1
                if stable >= stable_rounds:
                    return growth
            else:
                stable = 0
                last_growth = growth
        else:
            last_growth = growth
            stable = 0
    return last_growth


def _input_selectors() -> list[str]:
    custom = (os.environ.get("ALICE_INPUT_SELECTOR") or "").strip()
    if custom:
        return [custom] + [s for s in DEFAULT_INPUT_SELECTORS if s != custom]
    return list(DEFAULT_INPUT_SELECTORS)


def _alice_tg_chat_progress_enabled() -> bool:
    v = (os.environ.get("ALICE_TG_CHAT_PROGRESS") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _log_alice_row_issue(
    kind: str,
    *,
    tender_id: str,
    seq: int,
    total: int,
    work_name: str,
    detail: str = "",
) -> None:
    """Ошибки/пустые ответы Алисы — в stderr и в лог-файл (чат не засоряем)."""
    tid = (tender_id or "").strip()
    wn = (work_name or "").replace("\n", " ")[:400]
    det = (detail or "").replace("\n", " ")[:800]
    line = (
        f"{datetime.now().isoformat(timespec='seconds')} [{kind}] "
        f"tender={tid!r} row={seq}/{total} work={wn!r} detail={det!r}"
    )
    print(line, file=sys.stderr, flush=True)
    try:
        log_p = REPO_ROOT / "data" / "logs" / "alice_row_issues.log"
        log_p.parent.mkdir(parents=True, exist_ok=True)
        with log_p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


class _AliceTgEphemeralProgress:
    """
    В чате держим максимум одно «текущее» + одно краткое «готово»; при переходе к следующей строке удаляем предыдущее «готово».
    """

    def __init__(self, tender_id: str) -> None:
        self.tender_id = (tender_id or "").strip()
        self._tok = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        self._chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        self._start_mid: int | None = None
        self._done_mid: int | None = None

    def _active(self) -> bool:
        return _alice_tg_chat_progress_enabled() and bool(self._tok and self._chat)

    def _head_html(self) -> str:
        if self.tender_id:
            return f"🔎 Алиса · тендер <code>{html_mod.escape(self.tender_id[:64])}</code>\n"
        return "🔎 Алиса\n"

    def _delete(self, mid: int | None) -> None:
        if mid is None:
            return
        try:
            from autobot.telegram_notify import delete_message

            delete_message(self._tok, self._chat, mid)
        except Exception:
            pass

    def begin(self, seq: int, total: int, work_name: str) -> None:
        _append_alice_web_event(self.tender_id, "begin", seq, total, work_name=work_name)
        if not self._active() or total <= 0 or seq <= 0:
            return
        from autobot.telegram_notify import send_message_first_chunk_message_id

        self._delete(self._done_mid)
        self._done_mid = None
        self._delete(self._start_mid)
        self._start_mid = None

        raw = (work_name or "").strip()
        w = html_mod.escape(raw[:300])
        if len(raw) > 300:
            w += "…"
        text = self._head_html() + f"Работа <b>{seq}</b> из <b>{total}</b>.\n<code>{w}</code>"
        self._start_mid = send_message_first_chunk_message_id(
            self._tok,
            self._chat,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    def finish_ok(self, seq: int, total: int) -> None:
        _append_alice_web_event(self.tender_id, "done", seq, total)
        self._delete(self._start_mid)
        self._start_mid = None
        if not self._active() or total <= 0 or seq <= 0:
            return
        from autobot.telegram_notify import send_message_first_chunk_message_id

        text = self._head_html() + f"✅ <b>{seq}/{total}</b> · готово."
        self._done_mid = send_message_first_chunk_message_id(
            self._tok,
            self._chat,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    def finish_warn(self, seq: int, total: int, work_name: str) -> None:
        _append_alice_web_event(self.tender_id, "warn", seq, total, work_name=work_name, detail="Пустой ответ")
        self._delete(self._start_mid)
        self._start_mid = None
        _log_alice_row_issue(
            "WARN_EMPTY_REPLY",
            tender_id=self.tender_id,
            seq=seq,
            total=total,
            work_name=work_name,
        )

    def finish_err(self, seq: int, total: int, work_name: str, detail: str) -> None:
        _append_alice_web_event(self.tender_id, "error", seq, total, work_name=work_name, detail=detail)
        self._delete(self._start_mid)
        self._start_mid = None
        _log_alice_row_issue(
            "ERR",
            tender_id=self.tender_id,
            seq=seq,
            total=total,
            work_name=work_name,
            detail=detail,
        )


def _normalize_alice_xlsx_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Старые выгрузки Алисы → имена колонок как в merge_estimate_alice."""
    if df is None or df.empty:
        return df
    df = df.copy()
    for old, new in _MOJIBAKE_ALICE_COLUMNS.items():
        if old not in df.columns:
            continue
        if new in df.columns:
            old_s = df[old].fillna("").astype(str)
            new_s = df[new].fillna("").astype(str)
            df[new] = df[new].where(new_s.str.strip() != "", df[old])
            df = df.drop(columns=[old], errors="ignore")
        else:
            df = df.rename(columns={old: new})
    ren: dict[str, str] = {}
    if "Цены за ед. (рынок, руб)" not in df.columns and "Цены (строго, руб)" in df.columns:
        ren["Цены (строго, руб)"] = "Цены за ед. (рынок, руб)"
    if "Медиана цена за ед. (рынок)" not in df.columns and "Медиана цена (строго, руб)" in df.columns:
        ren["Медиана цена (строго, руб)"] = "Медиана цена за ед. (рынок)"
    if "Мин цена за ед. (рынок)" not in df.columns and "Мин цена (строго, руб)" in df.columns:
        ren["Мин цена (строго, руб)"] = "Мин цена за ед. (рынок)"
    if "Макс цена за ед. (рынок)" not in df.columns and "Макс цена (строго, руб)" in df.columns:
        ren["Макс цена (строго, руб)"] = "Макс цена за ед. (рынок)"
    return df.rename(columns=ren) if ren else df


def _alice_row_has_orientir_like_merge(row: pd.Series) -> bool:
    """
    True, если в сводке после merge была бы непустая «Рынок цены за ед. (итог)»:
    есть строгая колонка или извлекаются суммы из «Ответ Алисы».
    """
    strict = str(row.get("Цены за ед. (рынок, руб)", "") or "").strip()
    if strict and strict.casefold() not in ("nan", "none", "—", "-", "н/д", "нет"):
        return True
    reply = str(row.get("Ответ Алисы", "") or "").strip()
    if not reply:
        return False
    return bool(extract_ruble_amounts(reply))


def _filled_merge_keys_from_prev(prev: pd.DataFrame | None) -> set[str]:
    if prev is None or prev.empty or COL_NAME not in prev.columns:
        return set()
    prev = _normalize_alice_xlsx_columns(prev)
    out: set[str] = set()
    for _, prow in prev.iterrows():
        if not _alice_row_has_orientir_like_merge(prow):
            continue
        k = _norm_key(str(prow.get(COL_NAME, "") or ""))
        if k:
            out.add(k)
    return out


def _merge_alice_runs(prev: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """По одному ключу названия работы: новые строки заменяют старые; остальные старые сохраняются."""
    if prev.empty:
        return new
    if new.empty:
        return prev
    cols = list(dict.fromkeys(list(prev.columns) + list(new.columns)))
    p = prev.reindex(columns=cols)
    n = new.reindex(columns=cols)
    p = p.copy()
    n = n.copy()
    p["_mk"] = p[COL_NAME].map(_norm_key)
    n["_mk"] = n[COL_NAME].map(_norm_key)
    nk = set(n["_mk"])
    p_kept = p[~p["_mk"].isin(nk)].drop(columns=["_mk"], errors="ignore")
    n2 = n.drop(columns=["_mk"], errors="ignore")
    return pd.concat([p_kept, n2], ignore_index=True).reindex(columns=cols)


def _build_alice_output_row(m: dict, ans: str, err: str) -> dict:
    prices = _extract_unit_prices_from_alice_reply(
        ans,
        m.get(COL_UNIT_PRICE),
        m.get(COL_QTY),
    )
    bundle = _build_price_source_phone_bundle(
        ans,
        unit_price=m.get(COL_UNIT_PRICE),
        qty=m.get(COL_QTY),
    )
    phones = _extract_phones(ans)
    urls = _extract_urls(ans)
    m2 = {
        k: v
        for k, v in m.items()
        if k != "_id" and not str(k).startswith("_alice")
    }
    return {
        **m2,
        "Ответ Алисы": ans,
        "Ответ Алисы (полный)": ans,
        "Цены за ед. (рынок, руб)": _fmt_prices(prices),
        "Медиана цена за ед. (рынок)": (round(float(statistics.median(prices)), 2) if prices else ""),
        "Мин цена за ед. (рынок)": (prices[0] if prices else ""),
        "Макс цена за ед. (рынок)": (prices[-1] if prices else ""),
        "Телефоны (строго)": "; ".join(phones[:10]),
        "Ссылки (строго)": "; ".join(urls[:10]),
        "Цена-сайт-телефон (json)": (json.dumps(bundle, ensure_ascii=False) if bundle else ""),
        "Источники (ссылки/телефоны)": (
            (("; ".join(urls[:10])) + (" | " if urls and phones else "") + ("; ".join(phones[:10])))
        ),
        "Ошибка / статус": err or ("ок" if ans else "пустой ответ"),
    }


def _persist_partial_alice_rows(
    rows: list[dict],
    *,
    out_columns: list[str],
    out_path: Path | None,
    prev_alice_df: pd.DataFrame | None,
    verbose: bool,
) -> None:
    if out_path is None or not rows:
        return
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cur = pd.DataFrame(rows, columns=out_columns)
        if prev_alice_df is not None and not prev_alice_df.empty:
            cur = _merge_alice_runs(prev_alice_df, cur)
        cur.to_excel(out_path, index=False)
    except Exception as e:
        if verbose:
            print(f"Partial save skipped: {e}", flush=True)


def run_table(
    df: pd.DataFrame,
    region: str,
    *,
    max_rows: int,
    pause_sec: float,
    skip_duplicates: bool,
    user_data_dir: Path,
    headed: bool,
    response_timeout: float,
    min_response_chars: int,
    navigation_timeout: float,
    two_step: bool,
    followup_text: str | None,
    include_sites_in_first_message: bool,
    verbose: bool,
    resume: bool = True,
    prev_alice_df: pd.DataFrame | None = None,
    tender_id: str | None = None,
    partial_out_path: Path | None = None,
) -> pd.DataFrame:
    out_columns = [
        COL_ITEM,
        COL_NAME,
        COL_UNIT,
        COL_QTY,
        COL_UNIT_PRICE,
        "Регион в запросе",
        "Запрос Алисе",
        "Ответ Алисы",
        "Ответ Алисы (полный)",
        "Цены за ед. (рынок, руб)",
        "Медиана цена за ед. (рынок)",
        "Мин цена за ед. (рынок)",
        "Макс цена за ед. (рынок)",
        "Телефоны (строго)",
        "Ссылки (строго)",
        "Цена-сайт-телефон (json)",
        "Источники (ссылки/телефоны)",
        "Ошибка / статус",
    ]
    rows: list[dict] = []
    batch: list[tuple[str, str]] = []
    meta: list[dict] = []

    fu_resolved = _resolve_followup(two_step, followup_text, region)
    # Просьбу про сайты либо в первом сообщении, либо во втором — не дублируем
    sites_in_first = bool(include_sites_in_first_message and not fu_resolved)

    prev_norm = _normalize_alice_xlsx_columns(prev_alice_df) if resume and prev_alice_df is not None else None
    done_keys = _filled_merge_keys_from_prev(prev_norm) if resume else set()
    if verbose and resume and done_keys:
        print(f"Resume: пропускаем {len(done_keys)} работ с уже заполненным ориентиром рынка.", flush=True)

    # Порядковые номера среди строк сметы, которые вообще могут идти в Алису (дубли/короткие названия отброшены).
    eligible: list[tuple[pd.Series, int]] = []
    seq_eligible = 0
    for _, row in df.iterrows():
        if skip_duplicates and str(row.get(COL_DUP, "")).strip() == "Да":
            continue
        name0 = str(row.get(COL_NAME, "") or "").strip()
        if len(name0) < 8:
            continue
        seq_eligible += 1
        eligible.append((row, seq_eligible))
    eligible_total = len(eligible)

    pending: list[tuple[pd.Series, int]] = []
    for row, seq_among_eligible in eligible:
        mk = _norm_key(str(row.get(COL_NAME, "") or "").strip())
        if mk and mk in done_keys:
            continue
        pending.append((row, seq_among_eligible))

    batch_rows = pending if max_rows <= 0 else pending[:max_rows]
    tid_chat = (tender_id or "").strip()

    for row, seq_among_eligible in batch_rows:
        name = str(row.get(COL_NAME, "") or "").strip()
        unit = str(row.get(COL_UNIT, "") or "")
        prompt = build_alice_prompt(
            name,
            region,
            unit,
            qty=row.get(COL_QTY),
            include_sites_request=sites_in_first,
        )
        internal_id = str(len(batch))
        batch.append((internal_id, prompt))
        meta.append(
            {
                "_id": internal_id,
                COL_ITEM: row.get(COL_ITEM, ""),
                COL_NAME: name,
                COL_UNIT: unit,
                COL_QTY: row.get(COL_QTY),
                COL_UNIT_PRICE: row.get(COL_UNIT_PRICE),
                "Регион в запросе": region,
                "Запрос Алисе": prompt,
                "_alice_seq": seq_among_eligible,
                "_alice_total": eligible_total,
            }
        )

    if not batch:
        if verbose:
            hint = ""
            if skip_duplicates and COL_DUP in df.columns:
                dup = df[COL_DUP].astype(str).str.strip().eq("Да")
                if dup.any():
                    hint = " Все подходящие строки помечены «Явный дубликат» — попробуйте без фильтра дублей (флаг --include-duplicates)."
            lim_txt = "без лимита" if max_rows <= 0 else str(max_rows)
            print(f"Нет строк для обработки (лимит {lim_txt}, короткие названия отброшены).{hint}", flush=True)
        return pd.DataFrame(rows, columns=out_columns)

    fu = fu_resolved
    if verbose:
        print(
            f"Браузер: профиль {user_data_dir}, строк: {len(batch)}, "
            f"двухшаговый диалог: {'да' if fu else 'нет'}, "
            f"просьба про сайты в первом сообщении: {'да' if sites_in_first else 'нет'}",
            flush=True,
        )

    # Один запуск браузера на весь пакет — не открываем Chromium на каждую строку.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise SystemExit(
            "Нужен Playwright: pip install playwright && playwright install chromium"
        ) from e

    user_data_dir.mkdir(parents=True, exist_ok=True)
    tg_prog = _AliceTgEphemeralProgress(tid_chat)
    save_every = max(1, int((os.environ.get("ALICE_SAVE_EVERY") or "1").strip() or "1"))

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=not headed,
            locale="ru-RU",
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(int(navigation_timeout * 1000))
        page.goto(ALICE_URL, wait_until="domcontentloaded")
        time.sleep(3.0)

        selectors = _input_selectors()

        for idx, (_, prompt) in enumerate(batch):
            if verbose:
                short = (prompt[:100] + "…") if len(prompt) > 100 else prompt
                print(f"[{idx + 1}/{len(batch)}] {short}", flush=True)
            m_row = meta[idx] if idx < len(meta) else {}
            row_seq = int(m_row.get("_alice_seq", idx + 1))
            row_total = int(m_row.get("_alice_total", max(1, eligible_total)))
            row_name = str(m_row.get(COL_NAME, "") or "")
            tg_prog.begin(row_seq, row_total, row_name)
            snap = _body_text(page)
            err = _send_message(page, prompt.strip(), selectors)
            ans = ""
            err2 = ""
            if err:
                err2 = err
            else:
                growth = _wait_for_growth_stable(
                    page,
                    snap,
                    min_new_chars=min_response_chars,
                    timeout_sec=response_timeout,
                )
                ans = sanitize_alice_reply(growth, prompt.strip())
                if fu and fu.strip():
                    snap2 = _body_text(page)
                    ef = _send_message(page, fu.strip(), selectors)
                    if ef:
                        err2 = (err2 + "; " if err2 else "") + ef
                    else:
                        g2 = _wait_for_growth_stable(
                            page,
                            snap2,
                            min_new_chars=max(40, min_response_chars // 2),
                            timeout_sec=response_timeout,
                        )
                        if g2:
                            a2 = sanitize_alice_reply(g2, fu.strip())
                            if a2:
                                ans = (ans + _TWO_STEP_SEP + a2).strip()
            rows.append(_build_alice_output_row(m_row, ans, err2))
            if len(rows) % save_every == 0 or idx + 1 == len(batch):
                _persist_partial_alice_rows(
                    rows,
                    out_columns=out_columns,
                    out_path=partial_out_path,
                    prev_alice_df=prev_norm,
                    verbose=verbose,
                )
            if err2:
                tg_prog.finish_err(row_seq, row_total, row_name, err2)
            elif ans:
                tg_prog.finish_ok(row_seq, row_total)
            else:
                tg_prog.finish_warn(row_seq, row_total, row_name)
            if pause_sec > 0 and idx + 1 < len(batch):
                if verbose:
                    print(f"    пауза {pause_sec} с…", flush=True)
                time.sleep(pause_sec)

        context.close()

    return pd.DataFrame(rows, columns=out_columns)

    for m in meta:
        iid = str(m.get("_id", ""))
        pr, ans, err = results_map.get(iid, ("", "", "нет результата"))
        prices = _extract_unit_prices_from_alice_reply(
            ans,
            m.get(COL_UNIT_PRICE),
            m.get(COL_QTY),
        )
        bundle = _build_price_source_phone_bundle(
            ans,
            unit_price=m.get(COL_UNIT_PRICE),
            qty=m.get(COL_QTY),
        )
        phones = _extract_phones(ans)
        urls = _extract_urls(ans)
        m2 = {
            k: v
            for k, v in m.items()
            if k != "_id" and not str(k).startswith("_alice")
        }
        rows.append(
            {
                **m2,
                "Ответ Алисы": ans,
                "Ответ Алисы (полный)": ans,
                "Цены за ед. (рынок, руб)": _fmt_prices(prices),
                "Медиана цена за ед. (рынок)": (round(float(statistics.median(prices)), 2) if prices else ""),
                "Мин цена за ед. (рынок)": (prices[0] if prices else ""),
                "Макс цена за ед. (рынок)": (prices[-1] if prices else ""),
                "Телефоны (строго)": "; ".join(phones[:10]),
                "Ссылки (строго)": "; ".join(urls[:10]),
                "Цена-сайт-телефон (json)": (json.dumps(bundle, ensure_ascii=False) if bundle else ""),
                "Источники (ссылки/телефоны)": (
                    (("; ".join(urls[:10])) + (" | " if urls and phones else "") + ("; ".join(phones[:10])))
                ),
                "Ошибка / статус": err or ("ок" if ans else "пустой ответ"),
            }
        )

    return pd.DataFrame(rows, columns=out_columns)


def main() -> None:
    ap = argparse.ArgumentParser(description="Сбор ответов Алисы (веб) по строкам отчёта сметы")
    ap.add_argument("--tender-id", default="", help="ID тендера (файл ОТЧЕТ_ПО_СМЕТАМ_<id>.xlsx)")
    ap.add_argument("--xlsx", default="", help="Путь к .xlsx")
    ap.add_argument("--region", default="", help="Регион (иначе из tenders.json)")
    ap.add_argument("--max-rows", type=int, default=0, help="Сколько позиций (0 = без лимита)")
    ap.add_argument("--pause", type=float, default=15.0, help="Пауза между запросами, сек")
    ap.add_argument("--include-duplicates", action="store_true", help="Не пропускать «Явный дубликат»")
    ap.add_argument(
        "--user-data-dir",
        default="",
        help="Профиль Chromium (куки Яндекса). По умолчанию: data/alice_playwright_profile",
    )
    ap.add_argument("--headed", action="store_true", help="Показать окно браузера (рекомендуется)")
    ap.add_argument("--headless", action="store_true", help="Без окна (часто ломается без логина)")
    ap.add_argument("--response-timeout", type=float, default=120.0, help="Макс. ожидание ответа, сек")
    ap.add_argument("--min-chars", type=int, default=60, help="Мин. длина нового текста для 'готовности'")
    ap.add_argument("--nav-timeout", type=float, default=60.0, help="Таймаут навигации Playwright, сек")
    ap.add_argument(
        "--two-step",
        action="store_true",
        help="После основного ответа отправить уточнение про сайты/телефоны (как в переписке)",
    )
    ap.add_argument(
        "--followup",
        default="",
        help="Свой текст второго сообщения (иначе при --two-step — «Можешь посмотреть на сайтах…» с вашим регионом)",
    )
    ap.add_argument(
        "--no-sites-in-prompt",
        action="store_true",
        help="Не добавлять в первое сообщение просьбу «посмотреть на сайтах…» (--two-step по-прежнему шлёт второе сообщение)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Только показать запросы, без браузера")
    ap.add_argument(
        "--no-resume",
        action="store_true",
        help="Не подхватывать АЛИСА_РЫНОК_*.xlsx: заново опросить первые max-rows (игнор уже заполненных)",
    )
    args = ap.parse_args()

    path = _load_report_xlsx(args.tender_id or None, args.xlsx or None)
    df = pd.read_excel(path)
    need = {COL_NAME}
    if not need.issubset(df.columns):
        raise SystemExit(f"В файле нет колонок {need}. Есть: {list(df.columns)}")

    region = (args.region or "").strip()
    if not region and args.tender_id:
        meta = load_tender_metadata().get(args.tender_id.strip(), {})
        region = (meta.get("region") or "").strip()
    if not region:
        region = "Россия"

    udir = Path(args.user_data_dir) if args.user_data_dir.strip() else REPO_ROOT / "data" / "alice_playwright_profile"

    headed = args.headed and not args.headless
    if not args.headless and not args.headed:
        headed = True

    lim = 0 if args.max_rows <= 0 else max(1, min(args.max_rows, 5000))
    skip_dup = not args.include_duplicates
    followup = (args.followup or "").strip() or None

    stem = path.stem
    safe_stem = re.sub(r'[<>:"/\\\\|?*]', "_", stem)[:180]
    prev_path = REPORTS_DIR / f"АЛИСА_РЫНОК_{safe_stem}.xlsx"
    prev_alice = pd.DataFrame()
    if not args.no_resume and prev_path.is_file():
        try:
            prev_alice = pd.read_excel(prev_path)
        except OSError:
            prev_alice = pd.DataFrame()
    prev_for_run = prev_alice if not prev_alice.empty else None

    if args.dry_run:
        fu_dr = _resolve_followup(args.two_step, followup, region)
        sites_first_dr = (not args.no_sites_in_prompt) and not fu_dr
        done_dr = _filled_merge_keys_from_prev(prev_for_run) if not args.no_resume else set()
        if done_dr:
            print(f"(dry-run resume: пропуск {len(done_dr)} ключей с ориентиром)", flush=True)
        elig_dr: list[tuple[object, int]] = []
        s_el = 0
        for _, row in df.iterrows():
            if skip_dup and str(row.get(COL_DUP, "")).strip() == "Да":
                continue
            name = str(row.get(COL_NAME, "") or "").strip()
            if len(name) < 8:
                continue
            s_el += 1
            elig_dr.append((row, s_el))
        tot_el = len(elig_dr)
        pend_dr: list[tuple[object, int]] = []
        for row, sn in elig_dr:
            if _norm_key(str(row.get(COL_NAME, "") or "").strip()) in done_dr:
                continue
            pend_dr.append((row, sn))
        n = 0
        dr_rows = pend_dr if lim <= 0 else pend_dr[:lim]
        for row, seq_among in dr_rows:
            name = str(row.get(COL_NAME, "") or "").strip()
            unit = str(row.get(COL_UNIT, "") or "")
            p1 = build_alice_prompt(
                name,
                region,
                unit,
                qty=row.get(COL_QTY),
                include_sites_request=sites_first_dr,
            )
            print(f"[{seq_among}/{tot_el or 1}] {p1}", flush=True)
            if fu_dr:
                print(f"  [2-е сообщение] {fu_dr}", flush=True)
            n += 1
        print(f"(dry-run, строк: {n})", flush=True)
        if n == 0 and skip_dup and COL_DUP in df.columns:
            if df[COL_DUP].astype(str).str.strip().eq("Да").any():
                print(
                    "Подсказка: строки с «Явный дубликат» = Да пропускаются. "
                    "Добавьте --include-duplicates, чтобы их обработать.",
                    flush=True,
                )
        return

    out_df = run_table(
        df,
        region,
        max_rows=lim,
        pause_sec=max(0.0, args.pause),
        skip_duplicates=skip_dup,
        user_data_dir=udir,
        headed=headed,
        response_timeout=max(20.0, args.response_timeout),
        min_response_chars=max(20, args.min_chars),
        navigation_timeout=max(15.0, args.nav_timeout),
        two_step=args.two_step,
        followup_text=followup,
        include_sites_in_first_message=not args.no_sites_in_prompt,
        verbose=True,
        resume=not args.no_resume,
        prev_alice_df=prev_for_run,
        tender_id=(args.tender_id or "").strip() or None,
        partial_out_path=prev_path,
    )

    if not args.no_resume and not prev_alice.empty:
        if out_df.empty:
            out_df = prev_alice.copy()
        else:
            prev_alice = _normalize_alice_xlsx_columns(prev_alice)
            out_df = _merge_alice_runs(prev_alice, out_df)

    out_path = REPORTS_DIR / f"АЛИСА_РЫНОК_{safe_stem}.xlsx"
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out_df.to_excel(out_path, index=False)
    except PermissionError:
        out_path = REPORTS_DIR / f"АЛИСА_РЫНОК_{safe_stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_df.to_excel(out_path, index=False)
        print("Файл занят — записана копия с датой во имени.", flush=True)

    print(f"Готово: {out_path} (строк: {len(out_df)})", flush=True)


if __name__ == "__main__":
    main()
