"""Evidence freshness and explicit specification conflicts, without model guesses."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import os
import re
from urllib.parse import urlsplit


def text(value: object) -> str:
    value = str(value or '').strip()
    return '' if value.casefold() in {'nan', 'none', 'nat', '<na>'} else value


def observed_timestamp(value: object) -> float | None:
    """Missing or malformed evidence dates never acquire today's timestamp."""
    raw = text(value)
    if not raw:
        return None
    try:
        if re.fullmatch(r'\d+(?:\.\d+)?', raw):
            result = float(raw)
        else:
            instant = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if instant.tzinfo is None:
                return None
            result = instant.timestamp()
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _source_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or '').casefold().removeprefix('www.')
    except ValueError:
        return ''


def evidence_ttl_days(bucket: str, url: str) -> int:
    configured = os.environ.get('MARKET_INDEX_TTL_DAYS', '').strip()
    if configured:
        try:
            return max(1, min(365, int(configured)))
        except ValueError:
            pass
    host = _source_host(url)
    if host == 'avito.ru' or host.endswith('.avito.ru'):
        return 14
    return {'materials': 30, 'works': 60}.get(bucket, 45)


def freshness_reason(offer: dict, bucket: str, *, now: float | None = None) -> str:
    current = datetime.now(timezone.utc).timestamp() if now is None else now
    observed = observed_timestamp(offer.get('observed_at'))
    if observed is None:
        return 'Неизвестна дата проверки цены'
    if observed > current + 900:
        return 'Дата проверки цены находится в будущем'
    ttl = evidence_ttl_days(bucket, text(offer.get('url'))) * 86400
    if current - observed > ttl:
        return 'Цена устарела: требуется повторная проверка источника'
    if text(offer.get('published_at')):
        published = observed_timestamp(offer['published_at'])
        if published is None or published > current + 900 or current - published > ttl:
            return 'Дата прайса требует обновления цены'
    return ''


def region_key(value: object) -> str:
    return re.sub(r'\s+', ' ', text(value).casefold().replace('ё', 'е')).strip(' ,.')


def price_terms_reason(offer: dict) -> str:
    """A lower bound, range or editorial example is not an executable quote."""
    try:
        path = urlsplit(text(offer.get('url'))).path.casefold()
    except ValueError:
        path = ''
    if re.search(r'/(?:articles?|blog|news|stati|novosti)(?:/|$)', path):
        return 'Цена из статьи: требуется предложение поставщика'
    evidence = text(offer.get('evidence') or offer.get('snippet')).casefold()
    # Supplier notes can qualify an otherwise exact-looking table amount.
    # Older captures sometimes stored the note with delivery terms.
    conditions = evidence + ' ' + text(offer.get('delivery_terms')).casefold()
    if re.search(r'\bцена\s+(?:крупный\s+)?опт\b|\bоптовая\s+цена\b',evidence):
        tiers=offer.get('quantity_terms') or []
        if not any(isinstance(t,dict) and t.get('minimum') is not None and
                   'опт' in text(t.get('evidence')).casefold() for t in tiers):
            return 'Оптовая цена: источник не подтвердил условия партии для этой расценки'
    if (text(offer.get('matched_unit')).replace('²','2') == 'м2'
            and re.search(r'рул', evidence)
            and not re.search(r'(?:руб\.?|₽|р\.)\s*(?:/|за)\s*м[2²]|'
                              r'цен[аы]\s+за\s+м[2²]\s*:?\s*\d', evidence)):
        return 'Цена рулона не подтверждает цену за м²; требуется отдельная цена за площадь'
    if re.search(r'цен[аы].{0,100}(?:не\s+(?:совсем\s+)?актуальн|устарел)', conditions):
        return 'Поставщик предупреждает, что опубликованные цены устарели; требуется актуальная стоимость'
    # A table can qualify the entire price column, separated from its amount
    # by the unit/name columns. Do not confuse "от 20 м3" (quantity tier)
    # with "Цена руб. от" (a lower-bound price).
    if re.search(r'\b(?:цен[аы]|стоимость)\s*[,:(]?\s*'
                 r'(?:(?:руб(?:лей)?\.?|₽|р\.)(?:\s*/\s*[\w²³]+)?\s*[,):]?\s*)?'
                 r'от(?=\s*(?:$|[(:;|]|ед\.?\s*изм))', conditions):
        return 'Заголовок прайса указывает цену «от»; требуется стоимость для нужного объёма'
    if re.search(r'\b(?:минимальн\w*|ориентировочн\w*|приблизительн\w*)\s+(?:цен\w*|стоимост\w*)|\b(?:цен\w*|стоимост\w*)\s+(?:минимальн\w*|ориентировочн\w*|приблизительн\w*)', conditions):
        return 'Поставщик указал минимальную или ориентировочную цену; точная стоимость требует расчёта'
    if re.search(r'(?:/|за\s+)\s*км\b', evidence) and re.search(r'\bм\s*[3³]\b', evidence):
        return 'Тариф зависит от объёма и километража; это не цена за один м³'
    amount = r'\d[\d\s\u00a0\u202f]*(?:[.,]\d+)?'
    currency = r'(?:₽|руб\w*\.?|р\.?(?![a-zа-я]))'
    if re.search(r'\b(?:от|около|примерно|до)\s*' + amount + r'\s*' + currency, evidence):
        return 'Указана ориентировочная цена или цена «от»: нужна точная стоимость для этого объёма'
    for match in re.finditer(r'(?<![\w.,–—-])' + amount + r'\s*[-–—]\s*' + amount + r'\s*' + currency, evidence):
        if not re.search(r'(?<!\w)\d+\s*[-–—]\s*$', evidence[:match.start()]):
            return 'Указан диапазон цен: требуется цена конкретного предложения'
    if re.search(r'\b(?:рассрочк|ежемесячн|в кредит|первоначальн)', evidence):
        return 'Платёж по рассрочке не подтверждает полную цену'
    return ''


def specification_reason(name: object, evidence: object, *, position_bucket: str = '') -> str:
    """Reject explicit conflicts; lack of a detected conflict is not verification."""
    wanted, found = (text(value).casefold().replace('ё', 'е') for value in (name, evidence))
    if not wanted or not found:
        return ''
    if position_bucket == 'works' or re.match(r'^(?:укладк|устройств|монтаж|установк|прокладк|затягиван|протяж|измерен|определен|испытан|уплотнен|планировк|засыпк|перевозк|погрузк|посев|разработк)', wanted):
        from autobot.work_requirements import work_match_reason
        work_reason = work_match_reason(name, evidence, declared_work=position_bucket == 'works')
        if work_reason:
            return work_reason
    curb = re.compile(r'бордюр\w*|бортов\w*\s+кам\w*|кам\w*\s+бортов\w*|\bб[рв]\s*\d')
    if curb.search(wanted) and not curb.search(found):
        return 'Цена материала не подтверждает стоимость готового бортового камня'
    manual = re.compile(r'\b(?:ручн\w*|вручную)\b')
    machine = re.compile(r'\b(?:механиз\w*|экскават\w*|бульдозер\w*|автомобил\w*|автосамосвал\w*)\b')
    if manual.search(wanted) and not machine.search(wanted) and machine.search(found) and not manual.search(found):
        return 'В источнике другой способ работы: механизированный вместо ручного'
    if machine.search(wanted) and not manual.search(wanted) and manual.search(found) and not machine.search(found):
        return 'В источнике ручная работа; соответствие способу выполнения не подтверждено'
    demolition = re.compile(r'\b(?:демонтаж\w*|разборк\w*|снят\w*)\b')
    installation = re.compile(r'\b(?:монтаж\w*|установк\w*|укладк\w*)\b')
    if demolition.search(wanted) and not installation.search(wanted) and installation.search(found) and not demolition.search(found):
        return 'Цена монтажа не подтверждает стоимость демонтажа'
    if installation.search(wanted) and not demolition.search(wanted) and demolition.search(found) and not installation.search(found):
        return 'Цена демонтажа не подтверждает стоимость монтажа'

    # Compare labelled characteristics only. Arbitrary digits can be item
    # numbers, a price, volume or a delivery distance and must not be guessed.
    if 'бетон' in wanted and 'бетон' in found:
        # A listing title can advertise М300 while its explicit characteristics
        # say М200/В15. The requested word elsewhere must not hide that conflict.
        labelled = re.findall(
            r'(?:марка\s+бетона(?:\s*/\s*класс\s+прочности)?|класс\s+(?:бетона|прочности))'
            r'\s*:\s*((?:[мmвb]\s*\d{1,3}(?:[.,]\d+)?\s*(?:[/;]\s*)?){1,2})', found)
        for pattern, label in [(r'\b[мm]\s*(\d{2,3})\b', 'марка бетона'),
                               (r'\b[вb]\s*(\d{1,2}(?:[.,]\d+)?)\b', 'класс бетона')]:
            values = lambda value: {item.replace(',', '.') for item in re.findall(pattern, value)}
            left, right = values(wanted), values(found)
            if any(values(characteristic) and left.isdisjoint(values(characteristic))
                   for characteristic in labelled) and left:
                return 'В характеристиках источника не совпадает ' + label
            if left and right and left.isdisjoint(right):
                return 'Не совпадает ' + label
    if 'щеб' in wanted and 'щеб' in found:
        pattern = r'(?:фракци\w*\s*)?(\d{1,3})\s*[-–]\s*(\d{1,3})(?:\s*мм)?'
        left, right = set(re.findall(pattern, wanted)), set(re.findall(pattern, found))
        if left and right and left.isdisjoint(right):
            return 'Не совпадает фракция щебня'
    from autobot.market_requirements import technical_conflict
    return technical_conflict(name, evidence)


def price_origin_reason(offer: dict) -> str:
    if _source_host(text(offer.get('url'))) == 'agroserver.ru':
        # Listing pages combine unrelated sellers' contacts and delivery terms;
        # legacy captures do not retain a verifiable seller-scoped boundary.
        return 'Для этой площадки не подтверждена привязка цены и доставки к одному продавцу'
    if offer.get('index_hit') and not text(offer.get('extractor')) and not text(offer.get('catalog_item_id')):
        return 'В старом индексе не сохранено место извлечения цены; нужна повторная проверка страницы'
    if text(offer.get('extractor')) == 'metadata':
        return 'Цена найдена только в заголовке страницы; нужна цена в карточке товара'
    if re.search(r'похожие\s+товары|рекомендуемые\s+товары|с\s+этим\s+товаром\s+покупают', text(offer.get('evidence')), re.I):
        return 'Цена из блока рекомендаций; нужна цена основной карточки товара'
    return ''


def independent_source_key(offer: dict) -> str:
    host = _source_host(text(offer.get('url')))
    if not host:
        return ''
    seller = text(offer.get('seller_id'))
    return host + ('|' + seller if seller else '')


def select_independent_offers(offers: list[dict]) -> list[dict]:
    """One actual quote per known seller/host; retain all other evidence in UI."""
    def confidence(offer):
        try:
            value = float(offer.get('confidence') or 0)
            return value if math.isfinite(value) else 0
        except (ValueError, TypeError):
            return 0

    ordered = sorted(offers, key=lambda offer: (
        -(observed_timestamp(offer.get('observed_at')) or 0),
        -confidence(offer), text(offer.get('url')),
    ))
    seen, result = set(), []
    for offer in ordered:
        identity = independent_source_key(offer)
        if identity and identity not in seen:
            seen.add(identity)
            result.append(offer)
    return result
