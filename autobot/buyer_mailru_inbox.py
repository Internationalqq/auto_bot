"""Read only the requested Mail.ru conversation through the existing Mac UI.

Search results must be complete before absence is reported. Incoming text is
saved as evidence; conservative single-line prices remain subject to review.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from autobot.buyer_mailru_script import FirefoxLight, unique
from autobot.hermes_buyer import BuyerError


def texts(snapshot):
    return [e['label'] for e in snapshot['elements'] if e['role']=='AXStaticText']


def query_for(job):
    marker=re.search(r'\[AB-[A-Z0-9-]{4,60}\]',job['subject'])
    return marker[0][1:-1] if marker else job['subject']


def search_controls(snapshot):
    windows=[e['bounds'] for e in snapshot['elements'] if e['role']=='AXWindow']
    if len(windows)!=1: raise BuyerError('Не определено окно поиска почты')
    wx,wy,ww,wh=windows[0]
    fields=unique([e for e in snapshot['elements'] if e['role']=='AXTextField'])
    pairs=[]
    for field in fields:
        x,y,w,h=field['bounds']
        if not (wx<=x and wy<=y and x+w<=wx+ww and y+h<=wy+wh): continue
        buttons=unique([e for e in snapshot['elements'] if e['role']=='AXButton' and not e.get('label')
                        and abs(e['bounds'][1]-y)<10 and 0<e['bounds'][0]-x-w<100])
        if len(buttons)==1: pairs.append((field,buttons[0]))
    if len(pairs)!=1: raise BuyerError('Не найден однозначный поиск в почте')
    return pairs[0]


def search_rows(snapshot,query):
    if not snapshot['window_title'].startswith('Поиск - '+query+' - '):
        raise BuyerError('Почта не подтвердила поисковый запрос')
    elements=snapshot['elements'];labels=texts(snapshot)
    starts=[e['index'] for e in elements if e['role']=='AXStaticText' and e['label']=='Результаты поиска']
    ends=[e['index'] for e in elements if e['role']=='AXStaticText' and e['label']=='Почтовый ящик:']
    counts=[int(m[1]) for label in labels if (m:=re.fullmatch(r'(\d+)\s+(?:письмо|письма|писем)',label))]
    if len(starts)!=1 or len(ends)!=1 or len(set(counts))!=1:
        raise BuyerError('Не удалось проверить полноту результатов поиска почты')
    count=counts[0]
    if count>20: raise BuyerError('В переписке больше 20 писем: требуется отдельная проверка')
    links=unique([e for e in elements if e['role']=='AXLink' and starts[0]<e['index']<ends[0]
                  and e['bounds'][2]>=200])
    # Ad links also occur between the heading and footer. Only message table
    # rows count, with the widest link being the subject rather than an action.
    table_rows=unique([e for e in elements if e['role']=='AXRow' and starts[0]<e['index']<ends[0]])
    rows=[]
    for row in sorted(table_rows,key=lambda e:e['bounds'][1]):
        x,y,w,h=row['bounds']
        candidates=[e for e in links if x<=e['bounds'][0] and y<=e['bounds'][1]
                    and e['bounds'][0]+e['bounds'][2]<=x+w and e['bounds'][1]+e['bounds'][3]<=y+h]
        if not candidates: raise BuyerError('Не найдена ссылка письма в строке поиска')
        rows.append(max(candidates,key=lambda e:e['bounds'][2]))
    if len(rows)!=count:
        raise BuyerError('Результаты поиска показаны не полностью; отсутствие ответа не подтверждено')
    return rows


def search_address(snapshot,query):
    urls=[t for t in texts(snapshot) if t.startswith('light.mail.ru/search/?')]
    if len(urls)!=1: raise BuyerError('Не подтверждён адрес поиска писем')
    params=parse_qs(urlsplit('https://'+urls[0]).query)
    if params.get('q_query')!=[query]: raise BuyerError('Адрес поиска содержит другую метку')
    return params


def search_folders(snapshot,query):
    if search_address(snapshot,query).get('q_folder'):
        raise BuyerError('Поиск ограничен папкой; полный список не проверен')
    count=len(search_rows(snapshot,query))
    elements=snapshot['elements']
    starts=[e['index'] for e in elements if e['role']=='AXStaticText' and e['label']=='Найдено во всех папках']
    ends=[e['index'] for e in elements if e['role']=='AXStaticText' and e['label']=='Найдено за все время']
    if len(starts)!=1 or len(ends)!=1: raise BuyerError('Не удалось проверить папки найденных писем')
    links=unique([e for e in elements if e['role']=='AXLink' and starts[0]<e['index']<ends[0]])
    folders=[]
    for link in links:
        match=re.fullmatch(r'(\d+)\s+(.+)',link['label'])
        if not match: raise BuyerError('Не распознан список папок поиска')
        folders.append({'name':match[2],'count':int(match[1]),'element':link})
    if sum(f['count'] for f in folders)!=count or len({f['name'] for f in folders})!=len(folders):
        raise BuyerError('Распределение писем по папкам не совпадает с результатами')
    return folders


_MONTHS={'янв':1,'фев':2,'мар':3,'апр':4,'мая':5,'май':5,'июн':6,
         'июл':7,'авг':8,'сен':9,'окт':10,'ноя':11,'дек':12}


def received_time(value,now,timezone):
    zone=ZoneInfo(timezone);today=datetime.fromtimestamp(now,zone)
    value=value.strip().casefold()
    clock=re.search(r'(\d{1,2}):(\d{2})',value)
    if not clock: raise BuyerError('Не определено время письма')
    if value.startswith('сегодня'): day=today
    elif value.startswith('вчера'): day=today-timedelta(days=1)
    else:
        date=re.search(r'(\d{1,2})\s+([а-я]+)\.?\s*(\d{4})?',value)
        if not date or date[2][:3] not in _MONTHS: raise BuyerError('Не распознана дата письма')
        day=today.replace(year=int(date[3]) if date[3] else today.year,month=_MONTHS[date[2][:3]],day=int(date[1]))
    try: return day.replace(hour=int(clock[1]),minute=int(clock[2]),second=0,microsecond=0).timestamp()
    except ValueError: raise BuyerError('Некорректное время письма') from None


def unquoted(body):
    # Remove recognizable quoted-message boundaries, keeping original evidence
    # in the snapshot. Never derive a price from the customer's quoted request.
    cut=re.search(r'(?im)^(?:\s*>|\s*-{3,}.*(?:сообщени|message)|\s*On .+ wrote:|\s*(?:От|From):\s|.*(?:писал|писала)\s*:)',body)
    return body[:cut.start()].strip() if cut else body.strip()


def prices(body,positions):
    if len(positions)!=1: return []
    matches=list(re.finditer(r'(?<!\w)(\d+(?:[ \u00a0]\d{3})*(?:[.,]\d{1,2})?)\s*(?:руб(?:\.|лей|ля)?|₽|RUB)\s*(?:/|за)\s*(пог\.\s*м|пм|м[²³23]?|шт|кг|т)(?!\w)',body,re.I))
    if len(matches)!=1: return []
    match=matches[0]
    start=body.rfind('\n',0,match.start())+1
    end=body.find('\n',match.end());quote=body[start:end if end>=0 else len(body)]
    vat=re.search(r'без\s+НДС|с\s+НДС|включая\s+НДС|НДС\s+(?:включ[её]н|не\s+облагается)',body,re.I)
    availability=re.search(r'[^\n]*\b(?:в наличии|под заказ|нет в наличии)[^\n]*',body,re.I)
    delivery=re.search(r'[^\n]*доставк[^\n]*',body,re.I)
    return [{'line':1,'price':match[1],'unit':match[2].replace(' ',''),'vat':vat[0] if vat else '',
             'availability':availability[0][:400] if availability else '',
             'delivery':delivery[0][:600] if delivery else '',
             'exact_match':False,'quote':quote}]


def message(snapshot,job,account,now,timezone):
    content=texts(snapshot)
    urls=[t for t in content if re.match(r'light\.mail\.ru/message/\d+/',t)]
    if len(urls)!=1: raise BuyerError('Не открыт проверяемый адрес письма')
    if 'Кому:' not in content or 'Быстрый ответ' not in content:
        raise BuyerError('Почта не показала заголовок или границы письма')
    to=content.index('Кому:');end=content.index('Быстрый ответ')
    senders=[t[1:-1].lower() for t in content[:to] if re.fullmatch(r'<[^<>\s@]+@[^<>\s@]+>',t)]
    if len(senders)!=1: raise BuyerError('Не определён отправитель письма')
    if senders[0]==account.lower(): return None
    subjects=[t for t in content[:to] if query_for(job) in t and not t.startswith('light.mail.ru/')]
    # The browser tab title may contain the subject; use an actual header text.
    subjects=[s for s in subjects if not s.endswith(' - Почта Mail.ru')]
    if len(set(subjects))!=1: raise BuyerError('Тема письма не подтверждает метку обращения')
    subject=subjects[0]
    if not re.search(r'\[AB-',job['subject']) and senders[0]!=job['recipient'].lower():
        raise BuyerError('У старого обращения не совпадает отправитель')
    if account.casefold() not in content[to+1].casefold():
        raise BuyerError('Письмо адресовано другому ящику')
    received=received_time(content[to+2],now,timezone)
    # UI displays minutes; preserve that precision without inventing seconds.
    if received+59<job['created_at'] or received>now+300:
        raise BuyerError('Дата письма не соответствует отправленному обращению')
    body=unquoted('\n'.join(content[to+3:end]))
    if not body: raise BuyerError('Не удалось выделить текст ответа')
    if len(body)>50000: raise BuyerError('Ответ слишком большой для автоматической проверки')
    return {'message_id':urls[0].split('?')[0], 'sender':senders[0],'subject':subject,
            'received_at':received,'received_precision':'minute','text':body,
            'evidence':'https://'+urls[0]+'; дата в почте (точность до минуты): '+content[to+2],
            'prices':prices(body,job['positions']) if job.get('mapping_trusted') else []}


class Mailbox(FirefoxLight):
    def search(self,query):
        state=self.capture()
        if state['window_title'].startswith('Новое письмо'):
            raise BuyerError('В почте открыт черновик: чтение не меняет и не закрывает его')
        field,_=search_controls(state)
        self.call({'action':'set_value','element':field['index'],'value':query})
        _,button=search_controls(self.capture())
        self.click(button)
        for attempt in range(4):
            state=self.capture()
            if state['window_title'].startswith('Поиск - '+query+' - '):
                if not search_address(state,query).get('q_folder'): return state
                links=unique([e for e in state['elements'] if e['role']=='AXLink' and e['label']=='Найдено во всех папках'])
                if len(links)!=1: raise BuyerError('Нельзя подтвердить поиск во всех папках')
                self.click(links[0])
            if attempt<3: self.call({'action':'wait','seconds':1})
        raise BuyerError('Поиск в почте не завершился')

    def in_folder(self,query,name,count):
        state=self.search(query)
        matches=[f for f in search_folders(state,query) if f['name']==name and f['count']==count]
        if len(matches)!=1: raise BuyerError('Список писем изменился во время проверки')
        self.click(matches[0]['element'])
        for attempt in range(4):
            state=self.capture()
            if search_address(state,query).get('q_folder'):
                if len(search_rows(state,query))!=count: raise BuyerError('Неполный результат поиска в папке')
                return state
            if attempt<3: self.call({'action':'wait','seconds':1})
        raise BuyerError('Почта не подтвердила выбор папки')


def execute(job,config,remote,folder,*,browser_factory=Mailbox):
    from autobot.buyer_sender import save
    import time
    browser=None
    try:
        browser=browser_factory(config)
        query=query_for(job)
        state=browser.search(query)
        save(folder/'search.json',state)
        folders=search_folders(state,query);count=sum(f['count'] for f in folders)
        found=[];seen=set()
        for mail_folder in folders:
            # Opening a draft enters the composer and blocks all later sends.
            # Confirm the full folder distribution, then only read incoming mail.
            if mail_folder['name'] in ('Отправленные','Черновики'): continue
            for index in range(mail_folder['count']):
                remote.request('/inbox/'+job['id']+'/heartbeat',lease_token=job['token'])
                state=browser.in_folder(query,mail_folder['name'],mail_folder['count'])
                rows=search_rows(state,query)
                browser.click(rows[index]);snapshot=browser.capture()
                item=message(snapshot,job,config['sender_email'],time.time(),config.get('mail_timezone','Europe/Moscow'))
                if item and item['message_id'] not in seen:
                    seen.add(item['message_id'])
                    path=folder/('message-'+hashlib.sha256(item['message_id'].encode()).hexdigest()[:16]+'.json')
                    save(path,snapshot)
                    item['evidence']+='; snapshot sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()
                    found.append(item)
        result={'status':'checked','detail':f'Поиск по метке: {count} писем; ответов: {len(found)}. Вложения не открывались.','messages':found}
    except (BuyerError,OSError,ValueError,KeyError,IndexError) as error:
        detail=str(error) if isinstance(error,BuyerError) else 'Не удалось прочитать почту: '+type(error).__name__
        result={'status':'blocked','detail':detail,'messages':[]}
    finally:
        if browser is not None: browser.close()
    save(folder/'reply.json',result)
    return result
