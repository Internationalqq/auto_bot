"""Shared position classification and evidence checks for market research.

The module deliberately contains no network code.  Both the scraper and the web
card use these rules so a row cannot be displayed as a material while being
searched as a construction service (or vice versa).
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urlparse


_STOP_WORDS = {
    "для", "при", "под", "над", "без", "или", "как", "что", "это", "его",
    "она", "они", "из", "от", "до", "по", "на", "в", "во", "с", "со", "и",
    "к", "а", "не", "за", "у", "ед", "изм", "руб", "цена", "стоимость",
}

_SUMMARY_KEYS = (
    "итого", "всего", "непредвиденные затраты", "прочие затраты",
    "сводный сметный расчет", "глава ", "благоустройство территории",
)
_TECHNICAL_NORM_KEYS = (
    "при изменении", "на каждые последующие", "за каждые последующие", "добавлять или уменьшать",
    "добавлять (уменьшать)", "коэффициент к норм", "поправка к норм", "доплата к расценке",
)
_SERVICE_KEYS = (
    "аренда", "перевозка", "доставка", "вывоз", "утилизация", "погруз",
    "разгруз", "обслуживание", "испытание", "пусконалад", "охрана",
    "проектирование", "обследование", "технический надзор",
)
_WORK_KEYS = (
    "устройство", "установка", "монтаж", "демонтаж", "разборка", "снятие",
    "прокладка", "окраска", "ремонт", "очистка", "расчистка", "штукатур",
    "облицов", "сверление", "засыпка", "разработка", "укладка", "изоляция",
    "замена", "строительство", "благоустройство", "озеленение", "нанесение",
    "планировка", "армирование", "уплотнение", "посев", "резка", "покрытие",
)
_MATERIAL_KEYS = (
    "бетон", "раствор", "смесь", "цемент", "песок", "щебень", "грунт",
    "краска", "эмаль", "плитк", "кирпич", "труба", "кабель", "провод",
    "арматур", "битум", "мастик", "лист", "профил", "доска", "брус",
    "изоляц", "линолеум", "ламинат", "керамзит", "геотекстил", "асфальт",
    "крепеж", "саморез", "гвозд", "гипсокартон",
)
_PRODUCT_KEYS = (
    "насос", "шкаф", "щит", "светильник", "радиатор", "кран", "задвижк",
    "клапан", "вентил", "люк", "двер", "окно", "блок", "прибор",
    "оборудован", "издели", "унитаз", "раковин", "смесител", "тройник",
    "угольник", "муфт", "фланец", "камера", "сервер", "контроллер",
)

_SEARCH_HOSTS = {
    "bing.com", "www.bing.com", "duckduckgo.com", "html.duckduckgo.com",
    "google.com", "www.google.com", "yandex.ru", "ya.ru", "search.yahoo.com",
}
_MATERIAL_SELLERS = (
    "petrovich.ru", "vseinstrumenti.ru", "lemanapro.ru", "220-volt.ru",
    "ozon.ru", "market.yandex.ru", "avito.ru",
)

# One canonical registry for estimate rows, web adapters and the local index.
# OKEI/UNECE codes occur in XML/XLSX exports just as often as Russian labels.
UNIT_ALIASES: dict[str, tuple[str, ...]] = {
    "м2": ("м2", "м^2", "m2", "m^2", "mtk", "квадрат"),
    "м3": ("м3", "м^3", "m3", "m^3", "mtq", "кубичес", "кубометр"),
    "пог.м": ("пог.м", "погон", "м.п.", "м/п"),
    "кг": ("кг", "килограмм", "kg", "kgm"),
    "т": ("тонн", " т", "tne", "ton"),
    "л": ("литр", " л", "ltr", "liter", "litre"),
    "шт": ("шт", "штук", "единиц", "комплект", "компл", "к-т", "к-кт", "pce", "pcs", "piece"),
    "час": ("час", "чел.-ч", "чел-ч", "маш.-ч", "маш-ч", "hur"),
    "смена": ("смен",),
    "м": ("метр", " м", "mtr", "meter", "metre"),
}

_UNIT_EXACT_ALIASES = {
    alias: canonical
    for canonical, aliases in UNIT_ALIASES.items()
    for alias in aliases
    if re.fullmatch(r"[0-9a-zа-я.^/-]+", alias)
}


@dataclass(frozen=True)
class PositionClass:
    slug: str
    label: str
    bucket: str
    bucket_label: str
    confidence: float
    reason: str
    needs_decomposition: bool = False


@dataclass(frozen=True)
class MarketSearchPlan:
    position: PositionClass
    queries: tuple[str, ...]
    strategy_label: str
    source_label: str
    normalized_unit: str
    can_auto_price: bool
    warning: str = ""
    requirements: dict | None = None


@dataclass(frozen=True)
class OfferCheck:
    status: str
    confidence: float
    reason: str
    matched_unit: str
    observed_at: str


@dataclass(frozen=True)
class PricePlausibility:
    status: str
    ratio: float | None
    estimate_base_price: float | None
    multiplier: float
    reason: str


@dataclass(frozen=True)
class MarketMedianCheck:
    status: str
    ratio: float | None
    median: float | None
    sample_size: int
    reason: str


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return "" if text.casefold() in {"nan", "none", "nat", "<na>"} else text


def _fold(value: object) -> str:
    return _text(value).casefold().replace("ё", "е")


def normalize_unit(unit: object) -> str:
    raw = _fold(unit).replace("²", "2").replace("³", "3")
    if raw in {'', '-', '—', '–', '?', 'н/д', 'нет'}:
        return ''
    raw = (
        raw.replace("кв. м", "м2")
        .replace("кв.м", "м2")
        .replace("кв м", "м2")
        .replace("куб. м", "м3")
        .replace("куб.м", "м3")
        .replace("куб м", "м3")
        .replace("пог. м", "пог.м")
        .replace("пог м", "пог.м")
    )
    raw = re.sub(r"^\s*\d+(?:[.,]\d+)?\s*", "", raw).strip(" .")
    exact = _UNIT_EXACT_ALIASES.get(raw)
    if exact:
        return exact
    for canonical, aliases in UNIT_ALIASES.items():
        for alias in aliases:
            marker = alias.strip()
            if not marker:
                continue
            if len(marker) <= 2:
                if re.search(rf"(?<![0-9a-zа-я]){re.escape(marker)}(?![0-9a-zа-я])", raw):
                    return canonical
            elif marker in raw:
                return canonical
    return raw[:32] if raw else ""


def estimate_unit_multiplier(
    name: object,
    unit: object,
    *,
    estimate_price: object = None,
    quantity: object = None,
    total: object = None,
) -> float:
    """Return the estimate block behind a market base unit (for example 1000 m3)."""

    unit_text = _text(unit).replace("\xa0", " ")
    direct = re.match(r"^\s*(10|100|1000)(?:[.,]0+)?(?:\s+|(?=[a-zа-я]))", unit_text, flags=re.IGNORECASE)
    if direct:
        return float(direct.group(1))

    # OCR/PDF tables often split a normative unit such as "100 м2" and leave
    # only "м2" in the extracted row.  The missing block is recoverable from
    # the row identity: unit_price * quantity / total.  Accept only standard
    # estimate blocks and a tight tolerance, so market prices never influence
    # this reconstruction.
    try:
        price_value = float(estimate_price or 0)
        quantity_value = float(quantity or 0)
        total_value = float(total or 0)
    except (TypeError, ValueError):
        price_value = quantity_value = total_value = 0.0
    if all(math.isfinite(value) and value > 0 for value in (price_value, quantity_value, total_value)):
        inferred = price_value * quantity_value / total_value
        for standard_block in (10.0, 100.0, 1000.0):
            if abs(inferred - standard_block) / standard_block <= 0.03:
                return standard_block

    unit_norm = normalize_unit(unit)
    trailing = re.search(r"\s+(10|100|1000)(?:[.,]0+)?\s*$", _text(name))
    if not trailing:
        return 1.0
    multiplier = float(trailing.group(1))
    before = _fold(_text(name)[: trailing.start(1)]).strip()
    # A trailing "до 10 м" is a work characteristic, not a block of ten units.
    if multiplier == 10:
        if unit_norm != "шт" or re.search(r"(?:до|последующ\w*)\s*$", before):
            return 1.0
    elif unit_norm not in {"м", "пог.м", "м2", "м3", "шт"}:
        return 1.0
    return multiplier


def assess_price_plausibility(
    *,
    estimate_price: object,
    market_price: object,
    name: object,
    unit: object,
    quantity: object = None,
    total: object = None,
) -> PricePlausibility:
    """Check scale sanity after the title, direct URL and unit already matched.

    The estimate is only a reference band, never a market source.  A large gap
    downgrades the offer to manual review instead of forcing it to equal the
    estimate.
    """

    try:
        estimate_value = float(estimate_price or 0)
        market_value = float(market_price or 0)
    except (TypeError, ValueError):
        estimate_value = market_value = 0.0
    multiplier = estimate_unit_multiplier(
        name,
        unit,
        estimate_price=estimate_price,
        quantity=quantity,
        total=total,
    )
    if estimate_value <= 0 or market_value <= 0:
        return PricePlausibility(
            "unknown", None, None, multiplier,
            "Недостаточно данных для проверки масштаба цены",
        )
    estimate_base = estimate_value / max(1.0, multiplier)
    ratio = market_value / estimate_base if estimate_base > 0 else None
    if ratio is None or not math.isfinite(ratio):
        return PricePlausibility("unknown", None, estimate_base, multiplier, "Не удалось сравнить цены")
    ratio = round(ratio, 4)
    if 0.25 <= ratio <= 4.0:
        return PricePlausibility(
            "plausible", ratio, estimate_base, multiplier,
            f"Цена сопоставима со сметой: ×{ratio:.2f}",
        )
    if 0.10 <= ratio <= 10.0:
        return PricePlausibility(
            "review", ratio, estimate_base, multiplier,
            f"Цена отличается от сметы в ×{max(ratio, 1 / ratio):.1f}; нужна ручная проверка состава и единицы",
        )
    return PricePlausibility(
        "extreme", ratio, estimate_base, multiplier,
        f"Аномальный масштаб цены относительно сметы: ×{max(ratio, 1 / ratio):.1f}",
    )


def assess_market_median_anomaly(
    market_price: object,
    reference_prices: Iterable[object],
    *,
    threshold: float = 3.0,
    min_sources: int = 3,
) -> MarketMedianCheck:
    """Compare a new source with an independent market reference group.

    The source is never silently discarded: an outlier is returned as
    ``review`` so the caller can keep it in the audit trail without including
    it in an automatic weighted median.
    """

    try:
        value = float(market_price or 0)
    except (TypeError, ValueError):
        value = 0.0
    clean: list[float] = []
    for raw in reference_prices:
        try:
            number = float(raw or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            clean.append(number)
    required = max(1, int(min_sources or 1))
    if not math.isfinite(value) or value <= 0 or len(clean) < required:
        return MarketMedianCheck(
            "unknown", None, statistics.median(clean) if clean else None, len(clean),
            f"Для проверки по рынку нужно не меньше {required} сопоставимых источников",
        )
    median = float(statistics.median(clean))
    if median <= 0:
        return MarketMedianCheck("unknown", None, None, len(clean), "У рыночной группы нет положительной медианы")
    ratio = round(value / median, 4)
    limit = max(1.1, float(threshold or 3.0))
    if 1 / limit <= ratio <= limit:
        return MarketMedianCheck(
            "plausible", ratio, median, len(clean),
            f"Цена сопоставима с медианой {len(clean)} рыночных источников: ×{ratio:.2f}",
        )
    direction = "выше" if ratio > 1 else "ниже"
    difference = ratio if ratio > 1 else 1 / ratio
    return MarketMedianCheck(
        "review", ratio, median, len(clean),
        f"Цена в ×{difference:.1f} {direction} медианы {len(clean)} рыночных источников; требуется проверка",
    )


def units_compatible(left: object, right: object) -> bool:
    """Compare normalized market units, including linear-metre notation."""

    left_norm = normalize_unit(left)
    right_norm = normalize_unit(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    return {left_norm, right_norm} == {"м", "пог.м"}


def price_unit_factor(source: object, target: object) -> float | None:
    """Source units in one target unit; never infer density or package size."""
    if units_compatible(source, target):
        return 1.0
    return {('кг', 'т'): 1000.0, ('т', 'кг'): 0.001}.get(
        (normalize_unit(source), normalize_unit(target)))


def classify_position(name: object, unit: object = "", basis_code: object = "", section: object = "") -> PositionClass:
    title = _fold(name)
    text = f"{title} {_fold(unit)} {_fold(section)}"
    basis = _fold(basis_code)
    no_unit = not normalize_unit(unit)

    if not title:
        return PositionClass("other", "Не определено", "other", "Требуют разбора", 0.0, "Нет названия", True)
    if _fold(unit)=='%' or ('ненормируемые материальные ресурсы' in title):
        return PositionClass('aggregate','Расчётное начисление','other','Требуют разбора',1.0,
                             'Процентное начисление сметы, а не отдельный товар',True)
    if any(key in title for key in _SUMMARY_KEYS) and no_unit:
        return PositionClass("aggregate", "Укрупнённая строка", "other", "Требуют разбора", 0.98, "Сводная строка без единицы измерения", True)
    if any(key in title for key in _TECHNICAL_NORM_KEYS):
        return PositionClass("aggregate", "Корректирующий норматив", "other", "Требуют разбора", 0.96, "Это поправка к расценке, а не самостоятельный предмет рынка", True)
    basis_compact = re.sub(r"[\s._-]+", "", basis)
    if re.match(r"^(?:гэсн|фер|тер)", basis_compact):
        return PositionClass("work", "Работа", "works", "Работы и услуги", 0.98, "Норматив работы в шифре расценки", no_unit)
    if re.match(r"^(?:фссц|тсц|ссц|фсбц)", basis_compact):
        return PositionClass("material", "Материал", "materials", "Материалы и товары", 0.98, "Сборник сметных цен в шифре", no_unit)
    # A product's application or section ("монтаж", "доставка") is not its
    # type. Respect explicit price-code classifications above, then the noun
    # being purchased before descriptive words later in the title.
    material_head = re.match(r'^(?:земл[яи]\s+раститель\w*|раститель\w*\s+грунт\w*|грунт\s+раститель\w*|'
                             r'бордюр\w*|камень\s+бортов\w*|георешет\w*|геополот\w*|геотекст\w*|пен[аы]\s+монтаж\w*|'
                             r'штукатурк[аи]\s+(?:гипсов\w*|цемент\w*|декоратив\w*)|'
                             r'шпаклев\w*|смес[ьи]\s+сух\w*)\b', title)
    product_head = re.match(r'^(?:монтажн\w*\s+коробк\w*|кабел\w*|труб[аы]\w*|щит\w*|шкаф\w*|светильник\w*|'
                            r'насос\w*|крепеж\w*|болт\w*|саморез\w*|краск\w*|цемент\w*|'
                            r'кирпич\w*|бетон\w*|щебен\w*|песок)\b', title)
    if material_head:
        return PositionClass('material', 'Материал', 'materials', 'Материалы и товары', 0.92,
                             'Название самостоятельного материала', no_unit)
    if product_head:
        slug = 'material' if any(key in title for key in _MATERIAL_KEYS) else 'product'
        return PositionClass(slug, 'Материал' if slug == 'material' else 'Товар', 'materials',
                             'Материалы и товары', 0.92, 'Название самостоятельного товара', no_unit)
    if any(key in title for key in _SERVICE_KEYS):
        return PositionClass("service", "Услуга", "works", "Работы и услуги", 0.91, "Признак услуги в названии", no_unit)
    if any(key in title for key in _WORK_KEYS):
        return PositionClass("work", "Работа", "works", "Работы и услуги", 0.89, "Признак работы в названии", no_unit)
    if any(key in text for key in _MATERIAL_KEYS):
        return PositionClass("material", "Материал", "materials", "Материалы и товары", 0.88, "Признак материала в названии", no_unit)
    if any(key in text for key in _PRODUCT_KEYS):
        return PositionClass("product", "Товар", "materials", "Материалы и товары", 0.86, "Признак товара в названии", no_unit)
    unit_norm = normalize_unit(unit)
    if unit_norm in {"шт", "кг", "т", "л"}:
        return PositionClass("product", "Товар", "materials", "Материалы и товары", 0.56, "Тип предположен по единице измерения")
    return PositionClass("other", "Нужно определить", "other", "Требуют разбора", 0.25, "Недостаточно признаков", True)


def _query_name(name: object, max_words: int = 16, position_type: str = "") -> str:
    value = re.sub(r"[()]", " ", _text(name))
    value = re.sub(r"[|¦]", " ", value)
    value = re.sub(r"\s+(?:10|100|1000)\s*$", "", value)
    value = re.sub(r"\s+", " ", value).strip(" ,.;:-")
    folded = value.casefold().replace("ё", "е")
    position_slug = str(position_type or "").strip().casefold()
    if position_slug not in {'work', 'service'} and 'кабел' in folded:
        from autobot.market_requirements import technical_specs
        specs = technical_specs(value)
        if any(s['kind'] == 'cable_model' for s in specs):
            parts = []
            for kind in ('cable_model', 'dimensions', 'cable_stranding', 'cable_voltage'):
                parts.extend(s['value'] + 'В' if kind == 'cable_voltage' else s['evidence']
                             for s in specs if s['kind'] == kind)
            return 'кабель ' + ' '.join(parts)
    # Сметные формулировки плохо ищутся дословно. Для рынка используем обычное
    # название той же операции, но исходное название в отчёте не меняем.
    if "тротуар" in folded and "плит" in folded and ("покрыт" in folded or "устройств" in folded):
        return "укладка тротуарной плитки"
    if "бортов" in folded and "кам" in folded and ("установ" in folded or "устройств" in folded):
        return "установка бетонного бордюра"
    if "бортов" in folded and "кам" in folded:
        return ' '.join(value.split()[:max_words])
    if (
        position_slug in {"work", "service"}
        and "размет" in folded
        and any(marker in folded for marker in ("нанес", "устройств", "выполн"))
    ):
        return "нанесение дорожной разметки"
    if "разработка грунта" in folded and "самосвал" in folded:
        return "разработка грунта экскаватором с погрузкой"
    if "разработка грунта" in folded and folded.endswith(" до"):
        return "разработка грунта экскаватором"
    if "перевоз" in folded and "самосвал" in folded:
        return "перевозка грунта самосвалом"
    if "подстилающ" in folded and "пес" in folded:
        return "устройство песчаного основания"
    if "щебень" in folded and "плотн" in folded:
        return "щебень из плотных горных пород"
    if "щебень" in folded:
        fraction = re.search(r"\b(\d{1,3})\s*[-–—]\s*(\d{1,3})\b", folded)
        rock = next((label for stem, label in [('гранит', 'гранитный'), ('гравийн', 'гравийный'),
                     ('известня', 'известняковый'), ('вторичн', 'вторичный')]
                     if stem in folded), '')
        if rock:
            return f"щебень {rock}" + (f" фракции {fraction.group(1)}-{fraction.group(2)}" if fraction else '')
        if fraction:
            return f"щебень фракции {fraction.group(1)}-{fraction.group(2)}"
        return "щебень строительный"
    concrete_class = re.search(r"\b(?:в|b)\s*(\d{1,2}(?:[.,]\d+)?)\b", folded)
    if "бетон" in folded and concrete_class:
        class_value = concrete_class.group(1).replace(",", ".")
        concrete_grades = {
            "7.5": "М100",
            "10": "М150",
            "12.5": "М150",
            "15": "М200",
            "20": "М250",
            "22.5": "М300",
            "25": "М350",
            "30": "М400",
            "35": "М450",
            "40": "М500",
        }
        display_class = class_value.replace(".", ",")
        grade = concrete_grades.get(class_value, "")
        return f"бетон В{display_class}" + (f" {grade}" if grade else "")
    if "песок" in folded and "строитель" in folded:
        return "песок строительный мелкий" if "мелк" in folded else "песок строительный"
    if position_slug not in {'work', 'service'} and ("геополотно" in folded or "геотекст" in folded):
        density = re.search(r"\b(\d{2,4})\s*г(?:р(?:амм(?:а|ов)?)?)?\.?\s*/?\s*м(?:2|²)\b", folded)
        traits = ' нетканый' if 'неткан' in folded else ''
        traits += ' иглопробивной' if 'иглопробив' in folded else ''
        traits += ' полиэфирный' if 'полиэфир' in folded else ''
        return "геотекстиль" + traits + (f" {density.group(1)} г/м²" if density else "")
    if position_slug not in {'work', 'service'} and "георешет" in folded:
        material = next((label for stem, label in [('полиэтилен', 'полиэтиленовая'),
                         ('полипропилен', 'полипропиленовая'), ('композит', 'композитная')]
                         if stem in folded), '')
        return "георешетка" + (' ' + material if material else '')
    if "земл" in folded and "растител" in folded:
        return "земля растительная"
    if "лент" in folded and "сигнал" in folded:
        model = re.search(r"\bлс[эе]-?\s*\d+\b", folded)
        length = re.search(r"\b(\d{2,4})\s*м\b", folded)
        suffix = ""
        if model:
            suffix += " " + model.group(0).upper().replace(" ", "")
        if length:
            suffix += f" {length.group(1)} м"
        return "лента сигнальная" + suffix
    if "щитс монтаж" in folded or ("щит" in folded and "монтажн" in folded and "панел" in folded):
        dimensions = re.search(r"\b(\d{2,4})\s*[xх×]\s*(\d{2,4})\s*[xх×]\s*(\d{2,4})\b", folded)
        protection = re.search(r"\bip\s*\d{2}\b", folded)
        suffix = f" {dimensions.group(1)}х{dimensions.group(2)}х{dimensions.group(3)}" if dimensions else ""
        if protection:
            suffix += " " + protection.group(0).upper().replace(" ", "")
        return "щит с монтажной панелью" + suffix
    if "цементно-песчан" in folded and ("смес" in folded or "cmecu" in folded):
        return "смесь цементно-песчаная"
    if "эмульси" in folded and "битум" in folded:
        return "эмульсия битумная дорожная"
    if "краск" in folded and "дорожн" in folded and "размет" in folded:
        return "краска дорожная для разметки"
    if "стеклошар" in folded and "размет" in folded:
        return "стеклошарики для дорожной разметки"
    if "уплотнение грунта" in folded and "трамб" in folded:
        return "уплотнение грунта трамбовкой"
    if "прослойк" in folded and ("неткан" in folded or "нсм" in folded):
        return "укладка геотекстиля"
    if position_slug in {'work', 'service'}:
        if 'затягиван' in folded and 'провод' in folded and 'труб' in folded:
            return 'протяжка провода в трубе'
        if 'опор' in folded and 'освещен' in folded and 'фланцев' in folded:
            return 'монтаж фланцевых опор освещения'
        if 'георешет' in folded:
            return 'укладка георешетки'
        if 'посев' in folded and 'газон' in folded:
            return 'посев газона вручную' if 'вручную' in folded else 'посев газона'
    return " ".join(value.split()[:max_words])


def market_query_name(name: object, position_type: str = "") -> str:
    """Обычное рыночное название для поиска и проверки найденной страницы."""
    from autobot.market_requirements import preserve_query_specs
    return preserve_query_specs(name, _query_name(name, position_type=position_type))


def search_unit_marker(unit: object) -> str:
    """Human search marker shared by all web discovery queries."""

    return {
        "м2": "₽/м²",
        "м3": "₽/м³",
        "пог.м": "₽/пог.м",
        "м": "₽/м",
        "кг": "₽/кг",
        "т": "₽/т",
        "л": "₽/л",
        "шт": "₽/шт",
        "час": "₽/час",
        "смена": "₽/смену",
    }.get(normalize_unit(unit), "цена в рублях")


def build_search_plan(
    name: object,
    unit: object = "",
    basis_code: object = "",
    section: object = "",
    region: object = "",
) -> MarketSearchPlan:
    position = classify_position(name, unit, basis_code, section)
    title = market_query_name(name, position.slug)
    region_text = _text(region)
    unit_norm = normalize_unit(unit)
    from autobot.market_requirements import requirement_passport
    can_search = bool(unit_norm and not position.needs_decomposition and position.bucket in {'materials', 'works'})
    passport = requirement_passport(name, unit, position_type=position.slug,
                                   normalized_unit=unit_norm, can_search=can_search)
    place = f" {region_text}" if region_text else ""
    price_marker = search_unit_marker(unit_norm)
    safe_title = title.replace('"', " ").strip()
    exact_title = f'"{safe_title}"' if safe_title else title
    broad_unit = f"за {unit_norm}" if unit_norm else "в рублях"
    discovery_title = title
    if position.bucket == 'materials' and 'бетон' in _fold(title):
        # Broader discovery only; the original row still owns verification.
        # Supplier cards usually omit the estimate adjective «тяжёлый».
        discovery_title = re.sub(r'\bтяж[её]л\w*\s*', '', title, flags=re.I).strip()

    if position.bucket == "materials":
        queries = (
            f"{discovery_title}{place} цена {broad_unit}".strip(),
            f"{exact_title} цена прайс {price_marker}{place}".strip(),
            f"{title} купить поставщик цена {broad_unit}{place}".strip(),
        )
        # Long estimate descriptions are poor discovery queries for devices.
        # Try the printed article first, then retain full regional queries;
        # validation still uses every requirement in the original passport.
        articles=[spec['evidence'] for spec in passport['specifications']
                  if spec['kind']=='hardware_model' and len(spec['value'])>=6
                  and not re.fullmatch(r'ip\d{2}',spec['value'])]
        factory=[value for value in articles if re.fullmatch(r'AF\d{8,}',value,re.I)]
        if factory or len(articles)==1:
            article=(factory or articles)[0].replace('"','').strip()
            queries=(f'"{article}" купить цена в рублях',queries[0],queries[2])
        return MarketSearchPlan(
            position, queries, "Товар: точная модель/характеристики и цена за единицу",
            "Каталоги поставщиков; объявления — только как резерв", unit_norm,
            passport['can_search'],
            '; '.join(passport['issues']) if not passport['can_search'] else '',
            requirements=passport,
        )
    if position.bucket == "works":
        queries = (
            f"{title}{place} цена {broad_unit}".strip(),
            f"{exact_title} стоимость работ прайс {price_marker}{place}".strip(),
            f"{title} подрядчик стоимость работы {broad_unit}{place}".strip(),
        )
        return MarketSearchPlan(
            position, queries, "Работа: цена выполнения без стоимости материалов",
            "Прайсы подрядчиков и объявления услуг", unit_norm,
            passport['can_search'],
            '; '.join(passport['issues']) if not passport['can_search'] else '',
            requirements=passport,
        )
    return MarketSearchPlan(
        position, (), "Сначала разложить строку на работы и материалы",
        "Автоматический поиск отключён", unit_norm, False,
        "Укрупнённая или неоднозначная позиция: требуется детализация сметы",
        requirements=passport,
    )


def normalize_grade_notation(value: object) -> str:
    """М300, М 300 and M300 describe the same printed concrete grade."""
    return re.sub(r'\b([мmвb])\s*(\d{1,3}(?:[.,]\d+)?)',
                  lambda m: m[1].translate(str.maketrans('mb', 'мв')) + m[2].replace(',', '.'), _fold(value))


def _tokens(value: object) -> set[str]:
    from autobot.market_requirements import comparable_wording
    words = re.findall(r"[0-9a-zа-я]{3,}", normalize_grade_notation(comparable_wording(value)))
    return {word for word in words if word not in _STOP_WORDS and not word.isdigit()}


def _host_is(host: str, root: str) -> bool:
    return host == root or host.endswith("." + root)


def is_direct_source_url(url: object, *, supplier_evidence: str = "") -> bool:
    try:
        parsed = urlparse(_text(url))
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = parsed.netloc.casefold().split(":", 1)[0]
    if host in _SEARCH_HOSTS or any(_host_is(host, root) for root in _SEARCH_HOSTS):
        return False
    from autobot.supplier_evidence import reference_source
    if reference_source(_text(url)):
        return False
    if not parsed.path or parsed.path == "/":
        from autobot.supplier_catalogs import is_catalog_price_url
        return is_catalog_price_url(_text(url)) or bool(supplier_evidence and not parsed.query)
    if "avito.ru" in host and not re.search(r"_\d{6,}(?:/)?$", parsed.path):
        return False
    return True


def _unit_evidence(unit_norm: str, text: str, position: PositionClass, host: str) -> tuple[bool, str]:
    if not unit_norm:
        return False, ""
    folded = _fold(text).replace("²", "2").replace("³", "3")
    aliases = {
        "м2": ("м2", "м²", "кв. м", "кв.м", "за м"),
        "м3": ("м3", "м³", "куб. м", "куб.м"),
        "пог.м": ("пог.м", "погонн"),
        "м": ("за метр", " /м", "1 м"),
        "кг": ("кг", "килограмм"),
        "т": ("тонн", "за т"),
        "л": ("литр", "за л"),
        "шт": ("шт", "за шту", "1 шту", "единиц"),
        "час": ("час", "чел.-ч", "маш.-ч"),
        "смена": ("смен",),
    }
    if any(alias in folded for alias in aliases.get(unit_norm, (unit_norm,))):
        return True, unit_norm
    # Product cards normally show the price of one item even if the snippet omits "шт".
    if unit_norm == "шт" and position.bucket == "materials" and any(_host_is(host, root) for root in _MATERIAL_SELLERS):
        return True, "шт (карточка товара)"
    return False, ""


def check_offer(
    *,
    name: object,
    unit: object,
    basis_code: object = "",
    section: object = "",
    title: object,
    snippet: object,
    url: object,
    price: object,
    page_checked: bool = False,
    source_unit: object = "",
    supplier_evidence: str = "",
) -> OfferCheck:
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    position = classify_position(name, unit, basis_code, section)
    if position.needs_decomposition or position.bucket == "other":
        return OfferCheck("candidate", 0.15, "Позицию сначала нужно детализировать", "", observed_at)
    try:
        price_value = float(price or 0)
    except (TypeError, ValueError):
        price_value = 0.0
    if price_value <= 0:
        return OfferCheck("rejected", 0.0, "На странице не распознана положительная цена", "", observed_at)
    if not is_direct_source_url(url, supplier_evidence=supplier_evidence):
        return OfferCheck("rejected", 0.0, "Ссылка ведёт не на карточку источника", "", observed_at)
    if not page_checked:
        return OfferCheck("candidate", 0.22, "Цена не подтверждена на странице источника", "", observed_at)

    parsed = urlparse(_text(url))
    host = parsed.netloc.casefold().split(":", 1)[0]
    evidence_text = f"{_text(title)} {_text(snippet)}"
    from autobot.market_evidence_policy import specification_reason
    conflict = specification_reason(name, evidence_text)
    if conflict:
        return OfferCheck("candidate", 0.30, conflict, "", observed_at)
    wanted = _tokens(name)
    found = _tokens(evidence_text)
    common = wanted & found
    overlap = len(common) / max(1, min(len(wanted), 7))
    name_folded = _fold(name)
    evidence_folded = _fold(evidence_text)
    dense_rock_crushed_stone = (
        position.bucket == "materials"
        and "щебен" in name_folded
        and "плотн" in name_folded
        and "горн" in name_folded
        and "пород" in name_folded
        and "щебен" in evidence_folded
        and any(rock in evidence_folded for rock in ("гранит", "базальт", "диабаз", "габбро"))
    )
    fine_sand_required = (
        position.bucket == "materials"
        and "песок" in name_folded
        and "мелк" in name_folded
    )
    sand_module_seen = False
    fine_sand_module = False
    for module_match in re.finditer(
        r"(?:модул\w*\s+крупност\w*|\bмкр?\b)[^0-9]{0,20}(\d+(?:[.,]\d+)?)\s*[-–—]\s*(\d+(?:[.,]\d+)?)",
        evidence_folded,
    ):
        sand_module_seen = True
        try:
            module_min, module_max = sorted(float(value.replace(",", ".")) for value in module_match.groups())
        except ValueError:
            continue
        # ГОСТ 8736-2025: «мелкий» — свыше 1,5 до 2,0. Диапазон
        # 1,0–1,5 относится к «очень мелкому» и не является точной заменой.
        if module_min >= 1.5 and module_max <= 2.0:
            fine_sand_module = True
            break
    if re.search(r"(?:модул\w*\s+крупност\w*|\bмкр?\b)[^0-9]{0,20}до\s*\d+(?:[.,]\d+)?", evidence_folded):
        # Одна верхняя граница не доказывает принадлежность всей партии
        # диапазону «мелкий», даже если так написано в названии товара.
        sand_module_seen = True
    if fine_sand_required and sand_module_seen and not fine_sand_module:
        return OfferCheck(
            "candidate",
            0.42,
            "Указанный модуль крупности не соответствует группе «мелкий песок»",
            "",
            observed_at,
        )
    fine_natural_sand = (
        fine_sand_required
        and "песок" in evidence_folded
        and (
            fine_sand_module
            or ("мелк" in evidence_folded and "круп" not in evidence_folded and "пгс" not in evidence_folded)
        )
    )
    if fine_sand_required and not fine_natural_sand:
        return OfferCheck(
            "candidate",
            0.40,
            "Источник не подтверждает группу «мелкий песок» или модуль крупности 1,5–2,0",
            "",
            observed_at,
        )
    geotextile_required = "геополотно" in name_folded or "геотекст" in name_folded
    geotextile_seen = "геополотно" in evidence_folded or "геотекст" in evidence_folded
    density_pattern = r"\b(\d{2,4})\s*г(?:р(?:амм(?:а|ов)?)?)?\.?\s*/?\s*м(?:2|²)\b"
    required_density = re.search(density_pattern, name_folded)
    if geotextile_required and required_density:
        offered_density = re.search(density_pattern, evidence_folded)
        if not offered_density or offered_density.group(1) != required_density.group(1):
            return OfferCheck(
                "candidate",
                0.42,
                "Источник не подтверждает ту же поверхностную плотность геотекстиля",
                "",
                observed_at,
            )
    geotextile_identity_match = geotextile_required and geotextile_seen
    plant_earth_required = "земл" in name_folded and "растител" in name_folded
    plant_earth_seen = (
        ("растител" in evidence_folded and ("земл" in evidence_folded or "грунт" in evidence_folded))
        or ("плодород" in evidence_folded and "грунт" in evidence_folded)
    )
    steel_strip_required = "прокат" in name_folded and "полос" in name_folded and "стал" in name_folded
    steel_strip_seen = "полос" in evidence_folded and "стал" in evidence_folded
    if steel_strip_required and steel_strip_seen:
        canonical_name = name_folded.replace("стз", "ст3")
        canonical_evidence = evidence_folded.replace("стз", "ст3")
        required_steel_grades = set(re.findall(r"\bст3(?:сп|пс)\d*\b", canonical_name))
        offered_steel_grades = set(re.findall(r"\bст3(?:сп|пс)\d*\b", canonical_evidence))
        if required_steel_grades and not required_steel_grades.intersection(offered_steel_grades):
            return OfferCheck("candidate", 0.43, "Источник не подтверждает ту же марку полосовой стали", "", observed_at)
    signal_tape_required = "лент" in name_folded and "сигнал" in name_folded
    if signal_tape_required:
        required_model = re.search(r"\bлс[эе]-?\s*\d+\b", name_folded)
        required_length = re.search(r"\b(\d{2,4})\s*м\b", name_folded)
        compact_evidence = re.sub(r"\s+", "", evidence_folded)
        if required_model and re.sub(r"\s+", "", required_model.group(0)) not in compact_evidence:
            return OfferCheck("candidate", 0.43, "Источник не подтверждает ту же марку сигнальной ленты", "", observed_at)
        if required_length and not re.search(rf"\b{re.escape(required_length.group(1))}\s*м", evidence_folded):
            return OfferCheck("candidate", 0.43, "Источник не подтверждает ту же длину рулона", "", observed_at)
    from autobot.market_requirements import technical_specs
    requested_models = {s['value'] for s in technical_specs(name) if s['kind'] == 'hardware_model'}
    offered_models = {s['value'] for s in technical_specs(evidence_text) if s['kind'] == 'hardware_model'}
    exact_product_model = position.bucket == 'materials' and bool(requested_models) and requested_models.issubset(offered_models)
    semantic_identity_match = (
        exact_product_model
        or
        dense_rock_crushed_stone
        or fine_natural_sand
        or geotextile_identity_match
        or (plant_earth_required and plant_earth_seen)
        or (steel_strip_required and steel_strip_seen)
    )
    if semantic_identity_match:
        overlap = max(overlap, 0.55)
    if not semantic_identity_match and (
        len(common) < min(2, max(1, len(wanted))) or overlap < 0.28
    ):
        return OfferCheck("candidate", 0.30, "Слабое совпадение с названием позиции", "", observed_at)

    unit_norm = normalize_unit(unit)
    inspected_unit = normalize_unit(source_unit)
    if unit_norm and units_compatible(inspected_unit, unit_norm):
        unit_ok, matched_unit = True, inspected_unit
    else:
        unit_ok, matched_unit = _unit_evidence(unit_norm, evidence_text, position, host)
    if not unit_ok:
        return OfferCheck("candidate", min(0.59, 0.35 + overlap * 0.25), "Не подтверждена та же единица цены", "", observed_at)

    confidence = min(0.96, 0.58 + overlap * 0.28 + (0.08 if unit_ok else 0.0))
    return OfferCheck("verified", round(confidence, 2), "Совпали позиция, единица цены и прямая ссылка", matched_unit, observed_at)


def bucket_for_position(name: object, unit: object = "", basis_code: object = "", section: object = "") -> tuple[str, str]:
    position = classify_position(name, unit, basis_code, section)
    return position.bucket, position.label
