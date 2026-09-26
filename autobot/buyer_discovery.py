"""Bounded discovery and public-source verification; never sends messages."""
from __future__ import annotations

import base64
import codecs
from decimal import Decimal, ROUND_HALF_UP
import http.client
import ipaddress
import logging
import os
import re
import socket
import ssl
import threading
import time
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote, quote
from bs4 import BeautifulSoup, Comment
from autobot.hermes_buyer import BuyerError
from autobot.buyer_needs import digest

log = logging.getLogger(__name__)

# These sources identify other businesses or publish tenders; their own
# support/advertising contacts are not procurement recipients.
_DIRECTORY_HOSTS = frozenset({
    '2gis.ru', 'spravker.ru', 'orgsprav.com', 'rusprofile.ru', 'optsbyt.ru',
    'metaprom.ru', 'vsem-podryad.ru', 'ruscable.ru',
    'wikipedia.org', 'vc.ru', 'dtf.ru',
})
_EMAIL = re.compile(r'[\w.%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}')


def directory_source(url):
    host = (urlsplit(url).hostname or '').lower().removeprefix('www.')
    return any(host == domain or host.endswith('.'+domain) for domain in _DIRECTORY_HOSTS)


def service_contact(node):
    """Exclude privacy operators and website credits, not supplier contacts."""
    for parent in [node, *list(node.parents)[:5]]:
        if parent.name in ('html','body','[document]'): break
        attrs = str(parent.get('id',''))+' '+' '.join(parent.get('class',[]))
        if re.search(r'privacy|personal[-_]?data|consent|agreement', attrs, re.I): return True
    blocks = ['p','li','address','td','dd']
    block = node if node.name in blocks else node.find_parent(blocks) or node.parent
    if block is None: return False
    context = block.get_text(' ',strip=True)
    return len(context) < 1600 and bool(re.search(
        r'(?:разработк|создани|продвижени)\w*.{0,40}сайт|'
        r'обработк\w*.{0,60}персональн|персональн\w*.{0,20}данн|'
        r'(?:по вопросам|размещени\w*).{0,35}реклам', context, re.I))


def public_url(value):
    try:
        if not isinstance(value,str) or len(value)>4096: raise ValueError()
        p = urlsplit(value)
        if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
            raise ValueError()
        host = p.hostname.encode('idna').decode('ascii').lower()
        if any(ord(c)<32 for c in value) or '\\' in value: raise ValueError()
        authority = ('['+host+']' if ':' in host else host) + (f':{p.port}' if p.port else '')
        return urlunsplit((p.scheme, authority, quote(p.path or '/',safe="/%:@!$&'()*+,;=-._~"),
                           quote(p.query,safe="=&%:@!$'()*+,;/?-._~"), ''))
    except (ValueError, UnicodeError):
        raise BuyerError('Недопустимый адрес источника') from None


def fetch_public(url, *, image=False):
    """Resolve and pin a public IP, retaining TLS SNI/Host. No ambient proxy/auth."""
    deadline = time.monotonic()+35
    for _ in range(4):
        url = public_url(url)
        p = urlsplit(url); port = p.port or (443 if p.scheme == 'https' else 80)
        addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(p.hostname, port, type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise BuyerError('Источник ведёт в локальную или служебную сеть')
        connection = http.client.HTTPConnection(p.hostname, port, timeout=6)
        try:
            connection.sock = socket.create_connection((addresses[0], port), timeout=6)
            if p.scheme == 'https':
                connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=p.hostname)
            connection.request('GET', urlunsplit(('', '', p.path or '/', p.query, '')),
                               headers={'Host': p.netloc, 'User-Agent': 'AutoBot/1.0 supplier verification', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location: raise BuyerError('Источник вернул пустое перенаправление')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise BuyerError(f'Источник недоступен: HTTP {response.status}')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise BuyerError('Источник вернул неподдерживаемое сжатие')
            content_type = response.getheader('Content-Type', '').lower()
            allowed = ('image/jpeg','image/png','image/webp','image/gif') if image else ('text/html','application/xhtml+xml')
            if content_type.split(';')[0].strip() not in allowed:
                raise BuyerError('Неподдерживаемый формат источника')
            chunks, size = [], 0
            while True:
                chunk = response.read(32768)
                if not chunk: break
                size += len(chunk)
                if size > 2_000_000 or time.monotonic() > deadline:
                    raise BuyerError('Превышен размер или время загрузки источника')
                chunks.append(chunk)
            body = b''.join(chunks)
            if image:
                return url, body, content_type.split(';')[0].strip()
            charset = re.search(r'charset=([\w-]+)', content_type)
            encoding = charset[1] if charset else 'utf-8'
            try: html = body.decode(encoding, errors='replace')
            except LookupError: html = body.decode('utf-8', errors='replace')
            return url, html
        finally:
            connection.close()
    raise BuyerError('Слишком много перенаправлений источника')


def fetch_html(url):
    return fetch_public(url)


def search_api(query):
    """Optional official Yandex API. Credentials never enter errors/results."""
    key = os.environ.get('BUYER_SEARCH_API_KEY', '').strip()
    folder = os.environ.get('BUYER_SEARCH_FOLDER_ID', '').strip()
    if not key and not folder: return None
    if not key or not folder: raise BuyerError('Для Search API нужны ключ и folder ID в настройках сервера')
    import requests
    payload = {'query':{'searchType':'SEARCH_TYPE_RU','queryText':query[:400],'page':'0'},
               'groupSpec':{'groupMode':'GROUP_MODE_DEEP','groupsOnPage':'20','docsInGroup':'1'},
               'folderId':folder,'responseFormat':'FORMAT_XML'}
    try:
        with requests.post('https://searchapi.api.cloud.yandex.net/v2/web/search', json=payload,
                           headers={'Authorization':'Api-Key '+key}, timeout=(5,20), stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                raise BuyerError(f'Search API временно недоступен: HTTP {response.status_code}')
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 4_000_000: raise BuyerError('Слишком большой ответ Search API')
                chunks.append(chunk)
        import json
        xml = base64.b64decode(json.loads(b''.join(chunks))['rawData'], validate=True)
        if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper(): raise ValueError()
        root = ET.fromstring(xml)
        if root.find('.//error') is not None: raise BuyerError('Search API вернул ошибку поиска; повторите позже')
        return [SimpleNamespace(url=node.findtext('url') or '', title=''.join(node.find('title').itertext()) if node.find('title') is not None else '') for node in root.findall('.//doc')]
    except requests.RequestException:
        raise BuyerError('Search API временно недоступен; запрос сохранён') from None
    except (ValueError, KeyError, TypeError, ET.ParseError):
        raise BuyerError('Search API вернул некорректный ответ') from None


def search(query):
    # Reuse the bounded provider lock, but retain search order, not price sorting.
    from autobot.real_market_scraper import _ddgs_text, _parse_bing_rss
    import requests
    found, error = search_api(query), ''
    official = found is not None
    if found is None:
        found = []
        try:
            from ddgs import DDGS
            items = _ddgs_text(DDGS, query, timeout=12, region='ru-ru', max_results=20, backend='brave')
            found = [SimpleNamespace(url=item.get('href') or item.get('url') or '', title=item.get('title') or '') for item in items]
        except Exception:
            error = 'резервный поисковик не ответил'
    if len(found) < 10 and not official:
        try:
            response = requests.get('https://www.bing.com/search', params={'format':'rss','q':query}, timeout=(4, 8))
            response.raise_for_status()
            found.extend(_parse_bing_rss(response.text[:1_000_000], max_results=20))
        except requests.RequestException:
            if not found: raise BuyerError('Поисковики временно недоступны. Поиск сохранён для повтора') from None
    links, seen = [], set()
    site = re.search(r'(?:^|\s)site:([a-z0-9.-]+)', query, re.I)
    for item in found:
        try: url = public_url(item.url)
        except BuyerError: continue
        if directory_source(url): continue
        host = urlsplit(url).hostname.removeprefix('www.')
        if site and host != site[1].lower() and not host.endswith('.'+site[1].lower()): continue
        identity = url if host == 'avito.ru' else host
        if identity in seen: continue
        seen.add(identity)
        links.append({'url': url, 'title': item.title[:240]})
        if len(links) >= 10: break
    if not links and error: raise BuyerError('Поиск временно недоступен: '+error[:250])
    return links


def page_facts(url, html):
    if directory_source(url):
        raise BuyerError('Справочник или площадка не является поставщиком; её служебные контакты исключены')
    soup = BeautifulSoup(html, 'html.parser')
    image_tag = soup.find('meta', attrs={'property':'og:image'}) or soup.find('meta', attrs={'name':'twitter:image'})
    image = None
    if image_tag and image_tag.get('content'):
        try: image = {'url':public_url(urljoin(url,image_tag['content'])), 'source_url':url}
        except BuyerError: pass
    # HostCMS publishes ROT13 mailto links. Decode this explicit format only;
    # never run the page's JavaScript or guess an address from a company name.
    for a in soup.select('a[href^="znvygb:"]'):
        a['href'] = codecs.decode(a['href'], 'rot_13')
        a.clear()
        a.append(unquote(a['href'][7:]).split('?')[0])
    for node in soup(['script', 'style', 'noscript']): node.decompose()
    text = soup.get_text(' ', strip=True)
    heading = ' '.join(node.get_text(' ',strip=True) for node in soup.select('title,h1'))
    main = soup.find('main') or soup.find('article') or soup.body or soup
    content = BeautifulSoup(str(main),'html.parser')
    for node in content(['header','nav','footer','aside']): node.decompose()
    content = content.get_text(' ',strip=True)
    if not heading and len(content) < 600: heading = content
    editorial = bool(re.search(r'/(?:blog|news|articles?|wiki|forum|flood|computer_technology)(?:/|$)',urlsplit(url).path,re.I)
                     or re.match(r'\s*(?:как\s|что\s+такое|обзор\b|инструкци|руководство|рейтинг\b)',heading,re.I))
    if re.search(r'подтвердите,? что вы не робот|доступ ограничен|checking your browser', text[:10000], re.I):
        raise BuyerError('Сайт ограничил автоматическую проверку')
    emails = set()
    for value in soup.find_all(string=lambda t: not isinstance(t, Comment) and bool(_EMAIL.search(str(t)))):
        if not service_contact(value.parent): emails.update(_EMAIL.findall(str(value)))
    emails.update(unquote(a['href'][7:]).split('?')[0] for a in soup.select('a[href^="mailto:"]') if not service_contact(a))
    host = (urlsplit(url).hostname or '').removeprefix('www.')
    def email_order(value):
        domain = value.rsplit('@',1)[-1]
        own = host == domain or host.endswith('.'+domain) or domain.endswith('.'+host)
        return (not own, not bool(re.match(r'(info|sale|zakaz|order|office|mail)', value)), value)
    emails = sorted({v.lower() for v in emails if len(v) <= 254 and _EMAIL.fullmatch(v) and not re.search(r'\.(png|jpg|webp|svg)$', v, re.I)}, key=email_order)
    channels = []
    contact_pages = []
    for a in soup.select('a[href]'):
        href = a['href']
        if href.startswith('tel:'): channels.append({'channel':'phone','address':unquote(href[4:]),'source_url':url})
        elif href.startswith(('https://t.me/', 'https://wa.me/', 'https://max.ru/')):
            channels.append({'channel':'telegram' if 't.me/' in href else 'whatsapp' if 'wa.me/' in href else 'max','address':href,'source_url':url})
        elif re.search(r'контакт|contact|доставк|delivery', a.get_text(' ',strip=True)+' '+href, re.I):
            try: target = public_url(urljoin(url,href))
            except BuyerError: continue
            if target != public_url(url) and urlsplit(target).hostname == urlsplit(url).hostname and target not in contact_pages:
                contact_pages.append(target)
    for phone in re.findall(r'(?<!\d)(?:\+7|8)[ (\-]*\d{3}[ )\-]*\d{3}[ \-]*\d{2}[ \-]*\d{2}(?!\d)',text):
        normalized = '+7'+re.sub(r'\D','',phone)[1:]
        if not any(re.sub(r'\D','',c['address'])[-10:]==normalized[-10:] for c in channels if c['channel']=='phone'):
            channels.append({'channel':'phone','address':normalized,'source_url':url})
    site_name = soup.find('meta', attrs={'property':'og:site_name'})
    return {'text':text, 'heading':heading, 'content':content, 'editorial':editorial,
            'emails':emails, 'channels':channels, 'links':contact_pages[:3],
            'image':image,
            'name':str(site_name['content'])[:180] if site_name and site_name.get('content') else urlsplit(url).hostname.removeprefix('www.')}


_CATEGORY_EVIDENCE = {
    'cable':r'кабел|провод', 'electrical':r'электроматериал|электротехнич|выключател|распределительн|кабел|сжим|муфт',
    'lighting':r'светильник|освещен|прожектор|ламп', 'gravel':r'щеб', 'sand':r'пес[окч]',
    'soil':r'растительн\w* грунт|плодород|чернозем', 'concrete':r'бетон', 'steel':r'металлопрокат|сталь|стальн',
    'signs':r'дорожн\w* знак|знак\w* дорожн', 'curb':r'бордюр|бортов\w* кам',
    'dry_mix':r'сух\w* смес|строительн\w* смес', 'asphalt':r'асфальт', 'gotika':r'готика|gothic|gotika',
    'alfresco':r'alfresco|альфреско|опор\w* освещ', 'fiber':r'оптич|волс',
    'network_equipment':r'сетев\w* оборуд|коммутатор|видеонаблюд|ибп|сервер',
    'geosynthetics':r'геореш|геотекст|геосет', 'marking_material':r'разметк|стеклошар',
    'water_pipe':r'труб|пнд', 'electrical_work':r'электромонтаж|электрик|электропровод|прокладк\w* кабел',
    'electrical_testing':r'электролаборатор|электроизмер|измерени\w* сопротивлен',
    'network_work':r'волс|сет\w* связи|оптоволок|видеонаблюд', 'marking_work':r'разметк',
    'haulage':r'грузоперевоз|перевозк|самосвал', 'earthwork':r'землян|транше|экскаватор|разработк\w* грунта',
    'paving_work':r'тротуар|мощен|асфальтир', 'landscape_work':r'озеленен|газон|благоустройств',
    'concrete_work':r'бетонн\w* работ|монолит|фундамент', 'metal_work':r'металлоконструкц|сварочн',
    'geogrid_work':r'геореш|укреплен\w* откос', 'drilling':r'бурен|ямобур',
}


def inspect(task, source, *, fetch=fetch_html):
    from autobot.market_source_adapters import inspect_source_page, source_region_evidence
    url, html = fetch(task['url'])
    facts = page_facts(url, html)
    if facts['editorial']:
        raise BuyerError('Статья или инструкция не подтверждает предложение поставщика')
    pages = [(url, html, facts)]
    for target in facts['links']:
        try:
            target, body = fetch(target)
            if urlsplit(target).hostname.removeprefix('www.') != urlsplit(url).hostname.removeprefix('www.'):
                continue
            pages.append((target, body, page_facts(target, body)))
        except (BuyerError, OSError): continue
    combined = ' '.join(p[2]['text'] for p in pages)
    rows = [r for r in source['positions'] if r['position_key'] in task['position_keys']]
    # Category keys can be internal; ground relevance in the actual requested names.
    ignored = {'работы','устройство','установка','выполнение','материалы','монтаж','строительные','стоимость','согласно','типом','типа'}
    anchors = {w[:6].casefold() for r in rows for w in re.findall(r'[а-яё]{4,}', r['name'], re.I) if w.casefold() not in ignored}
    pattern = _CATEGORY_EVIDENCE.get(task['category'])
    topic = facts['heading']
    # Navigation, footer and unrelated contact pages cannot prove assortment.
    if re.search(r'каталог|магазин|товар|материал|постав|продаж|производ', topic, re.I):
        topic += ' '+facts['content'][:4000]
    relevant = bool(re.search(pattern, topic, re.I)) if pattern else bool(anchors) and sum(a in topic.casefold() for a in anchors) >= min(2,len(anchors))
    if not relevant:
        raise BuyerError('Страница не подтверждает нужный ассортимент или вид работ')
    commerce = r'заказ|заявк|вызвать|выполняем|оказываем|услуг|стоимость|прайс|цен[аыу]' if task['bucket']=='works' else r'поставк|продаж|купить|заказ|налич|прайс|каталог|производител|производств|магазин|товар|корзин'
    host = urlsplit(url).hostname.removeprefix('www.')
    if not re.search(commerce, facts['heading']+' '+facts['content'][:12000], re.I) and host != 'avito.ru':
        raise BuyerError('Не подтверждено коммерческое предложение компании')
    if task.get('intent') == 'product':
        from autobot.buyer_needs import product_identifiers
        def comparable(value):
            value=value.casefold().replace('ё','е').replace('×','х').translate(str.maketrans('abcehkmoptxy','авсенкмортху'))
            return re.sub(r'[^\w]','',value)
        product_text = comparable(facts['heading']+' '+facts['content'][:12000])
        rows = [r for r in rows if all(comparable(term) in product_text for term in product_identifiers(r['name']))]
        if not rows:
            raise BuyerError('Страница не подтверждает запрошенную модель или размер товара')
    regional = next((source_region_evidence(p[1], source['region'], task['bucket']) for p in pages
                     if source_region_evidence(p[1], source['region'], task['bucket'])), '')
    emails = list(dict.fromkeys(e for p in pages for e in p[2]['emails']))
    email = emails[0] if emails else ''
    if host == 'avito.ru' or host.endswith('.avito.ru'):
        # Platform support contacts are not the advertiser's contacts.
        email, emails = '', []
        facts['channels'] = [{'channel':'avito','address':url,'source_url':url}]
    identity = 'page:'+url if host == 'avito.ru' else 'site:'+host
    evidence_pages = [{'url': p[0], 'sha256': digest(p[1]), 'checked_at':time.time(),
                       'excerpt':p[2]['text'][:2000]} for p in pages]
    prices = []
    for row in rows:
        offer = inspect_source_page(html, url, name=row['name'], target_unit=row['unit'], position_bucket=task['bucket'], quantity=row['quantity'])
        if offer.accepted and offer.price is not None:
            prices.append({'position_key':row['position_key'], 'price_kopecks':int((Decimal(str(offer.price))*100).quantize(Decimal('1'), rounding=ROUND_HALF_UP)),
                           'unit':offer.unit,'source_url':url,'evidence':offer.evidence,'state':'published','observed_at':time.time()})
    return {'id':'discovered-'+digest(identity)[:24], 'company':facts['name'], 'url':url,
            'email':email,'emails':emails,'channels':[c for p in pages for c in p[2]['channels']],
            'position_keys':[r['position_key'] for r in rows], 'categories':[task['category']],
            'region':source['region'], 'region_note':regional or 'Регион поставки или выполнения работ нужно подтвердить',
            'evidence_pages':evidence_pages,'prices':prices,'image':facts['image'],'discovered':True}


def verify_contact(supplier):
    email = supplier.get('email')
    if not email: raise BuyerError('Нет опубликованного email')
    for page in supplier.get('evidence_pages', [])[:4]:
        try:
            url, html = fetch_html(page['url'])
            if email in page_facts(url, html)['emails']: return email
        except (BuyerError, OSError): continue
    raise BuyerError('Email больше не подтверждается на сайте')


def run_once():
    from autobot import buyer_store as store
    step = store.claim()
    if not step:
        store.settle()
        return False
    try:
        if step['kind'] == 'search': store.finish(step, links=search(step['payload']['query']))
        elif step['kind'] == 'prepare':
            from autobot.buyer_workflow import prepare_run
            store.finish(step, prepared=prepare_run(step['source']['tender_id'], step['run_id']))
        else: store.finish(step, candidate=inspect(step['payload'], step['source']))
    except (BuyerError, OSError, ValueError) as error:
        store.finish(step,error=str(error),retry=isinstance(error,OSError) or 'временно' in str(error))
    except Exception:
        log.exception('Supplier discovery failed: %s', step['id'])
        store.finish(step,error='Не удалось проверить источник')
    store.settle()
    return True


_thread = None
_lock = threading.Lock()


def start_worker():
    global _thread
    if os.environ.get('BUYER_DISCOVERY_WORKER') != '1': return
    with _lock:
        if _thread and _thread.is_alive(): return
        def work():
            from autobot.atomic_output import output_lock
            from autobot.paths import DATA_DIR
            while True:
                try:
                    with output_lock(DATA_DIR/'buyer_discovery_worker', timeout=.1):
                        while True:
                            run_once()
                            time.sleep(1)
                except TimeoutError: time.sleep(5)
                except Exception:
                    log.exception('Supplier discovery worker unavailable')
                    time.sleep(5)
        _thread = threading.Thread(target=work,name='buyer-discovery',daemon=True)
        _thread.start()
