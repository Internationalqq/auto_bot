"""TLS SMTP/IMAP transport. No browser, model or desktop dependency.

SMTP acceptance is durable evidence of submission, not proof of delivery.
Once DATA may have been submitted, recovery only looks for evidence; it never
repeats the transmission automatically.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses, parsedate_to_datetime
import hashlib
import imaplib
import json
from pathlib import Path
import re
import smtplib
import ssl
import time

from autobot.buyer_sender import save
from autobot.buyer_reply_text import prices, unquoted, web_reply_fingerprint
from autobot.hermes_buyer import BuyerError

MAX_MESSAGE_BYTES = 2_000_000
MAX_SEARCH_RESULTS = 200


def address(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}',value) or len(value)>254:
        raise BuyerError('Укажите один корректный email почтового ящика')
    return value.lower()


def settings(config):
    account=address(config.get('sender_email'))
    for key in ('smtp_host','imap_host'):
        if not re.fullmatch(r'[A-Za-z0-9.-]+',str(config.get(key,''))):
            raise BuyerError('Не настроен адрес почтового сервера')
    if config.get('smtp_security','ssl') not in ('ssl','starttls'):
        raise BuyerError('Отправка требует TLS')
    for key,default in (('smtp_port',465),('imap_port',993)):
        if type(config.get(key,default)) is not int or not 1<=config.get(key,default)<=65535:
            raise BuyerError('Некорректный порт почтового сервера')
    try:
        path=Path(config['mail_password_file'])
        import os
        if os.name=='posix' and path.stat().st_mode & 0o077:
            raise BuyerError('Файл пароля почты должен иметь права 0600')
        secret=path.read_text().strip()
        if not secret or len(secret)>500 or '\n' in secret or '\r' in secret:
            raise ValueError()
    except (OSError,KeyError,ValueError):
        raise BuyerError('Не настроен пароль внешнего приложения почты') from None
    return account,secret


@contextmanager
def smtp_connection(config):
    account,secret=settings(config)
    context=ssl.create_default_context()
    connection=None
    try:
        if config.get('smtp_security','ssl')=='ssl':
            connection=smtplib.SMTP_SSL(config['smtp_host'],config.get('smtp_port',465),timeout=15,context=context)
        else:
            connection=smtplib.SMTP(config['smtp_host'],config.get('smtp_port',587),timeout=15)
            connection.ehlo()
            connection.starttls(context=context)
        connection.ehlo()
        connection.login(account,secret)
        yield connection
    finally:
        if connection is not None:
            try:connection.quit()
            except (OSError,smtplib.SMTPException):connection.close()


@contextmanager
def imap_connection(config):
    account,secret=settings(config)
    connection=None
    try:
        connection=imaplib.IMAP4_SSL(config['imap_host'],config.get('imap_port',993),ssl_context=ssl.create_default_context(),timeout=15)
        connection.login(account,secret)
        yield connection
    finally:
        if connection is not None:
            try:connection.logout()
            except (OSError,imaplib.IMAP4.error):pass


def failure(error, protocol):
    if isinstance(error,(smtplib.SMTPAuthenticationError,imaplib.IMAP4.error)):
        return f'{protocol}: не удалось войти или выполнить команду. Проверьте пароль приложения и доступ к почте.'
    if isinstance(error,BuyerError):return str(error)
    if isinstance(error,smtplib.SMTPResponseException):
        return f'{protocol}: почтовый сервер отклонил запрос, код {error.smtp_code}.'
    return f'{protocol}: нет защищённого соединения с почтовым сервером.'


def check_connection(config):
    """Authenticate without sending, downloading messages or claiming jobs."""
    settings(config)
    for label,connect in (('SMTP',smtp_connection),('IMAP',imap_connection)):
        try:
            with connect(config):pass
        except (OSError,smtplib.SMTPException,imaplib.IMAP4.error,BuyerError) as error:
            raise BuyerError(failure(error,label)) from None
    return {'smtp':True,'imap':True,'account':config['sender_email']}


def mailboxes(connection):
    status,data=connection.list()
    if status!='OK' or not data or len(data)>100:
        raise BuyerError('IMAP: не получен полный список папок')
    result=[]
    for entry in data:
        if not isinstance(entry,bytes):raise BuyerError('IMAP: неподдерживаемое имя папки')
        match=re.fullmatch(rb'\(([^)]*)\)\s+(?:"(?:\\.|[^"])*"|NIL)\s+(.+)',entry)
        if not match:raise BuyerError('IMAP: не распознана папка')
        flags=match[1].lower().split()
        if b'\\noselect' not in flags:result.append((match[2],flags))
    if not result:raise BuyerError('IMAP: нет доступных папок')
    return result


def select(connection, mailbox):
    if connection.select(mailbox,readonly=True)[0]!='OK':
        raise BuyerError('IMAP: папка переписки недоступна')


def search_header(connection, field, value):
    quoted=('"'+value.replace('\\','\\\\').replace('"','\\"')+'"').encode('utf-8')
    args=('HEADER',field,quoted)
    if not value.isascii():args=('CHARSET','UTF-8',*args)
    status,data=connection.uid('search',*args)
    if status!='OK' or not isinstance(data,list) or len(data)!=1 or not isinstance(data[0],bytes):
        raise BuyerError('IMAP: поиск переписки не завершён')
    result=data[0].split()
    if any(not uid.isdigit() for uid in result) or len(result)>MAX_SEARCH_RESULTS:
        raise BuyerError('IMAP: переписка превышает лимит автоматической проверки')
    return result


def read_message(connection, uid):
    status,data=connection.uid('fetch',uid,'(RFC822.SIZE)')
    sizes=[int(m[1]) for item in data or [] if isinstance(item,bytes) and (m:=re.search(rb'RFC822.SIZE (\d+)',item))]
    if status!='OK' or len(sizes)!=1 or not 0<sizes[0]<=MAX_MESSAGE_BYTES:
        raise BuyerError('IMAP: письмо недоступно или превышает 2 МБ; нужна отдельная проверка вложений')
    status,data=connection.uid('fetch',uid,'(BODY.PEEK[] INTERNALDATE)')
    values=[item for item in data or [] if isinstance(item,tuple) and len(item)==2 and isinstance(item[1],bytes)]
    if status!='OK' or len(values)!=1 or len(values[0][1])>MAX_MESSAGE_BYTES:
        raise BuyerError('IMAP: письмо прочитано не полностью')
    return values[0][1],values[0][0]


def text_body(message, *, strip_quotes=True):
    plain=[];html=[];attachments=[]
    def parts(part):
        # Do not walk into an attached/forwarded message and quote its prices.
        if part.get_content_disposition()=='attachment' or part.get_content_maintype()=='message':
            attachments.append(part.get_filename() or 'вложение')
        elif part.is_multipart():
            for child in part.iter_parts():yield from parts(child)
        else:yield part
    for part in parts(message):
        if part.get_content_type() not in ('text/plain','text/html'):continue
        try:content=part.get_content()
        except (LookupError,UnicodeError,ValueError):raise BuyerError('IMAP: не удалось прочитать кодировку письма') from None
        if not isinstance(content,str):continue
        if part.get_content_type()=='text/plain':plain.append(content)
        else:
            from bs4 import BeautifulSoup
            soup=BeautifulSoup(content,'html.parser')
            for node in soup(['script','style','head']+(['blockquote'] if strip_quotes else [])):node.decompose()
            html.append(soup.get_text('\n',strip=True))
    text='\n'.join(plain or html).strip()
    if strip_quotes:text=unquoted(text)
    if not text and attachments:
        text='[Ответ содержит вложения: '+', '.join(attachments)[:1000]+'. Автоматический разбор вложений пока не подключён.]'
    if not text or len(text)>50000:
        raise BuyerError('IMAP: текст ответа пуст или превышает лимит автоматической проверки')
    return text


def parse_message(raw, metadata, job, account, now):
    message=BytesParser(policy=policy.default).parsebytes(raw)
    senders=[email.lower() for _,email in getaddresses(message.get_all('From',[]))]
    if len(senders)!=1:raise BuyerError('IMAP: не определён отправитель письма')
    sender=address(senders[0])
    if sender==account.lower():return None
    subject=str(message.get('Subject',''))
    if len(subject)>500:raise BuyerError('IMAP: тема ответа превышает лимит')
    marker=re.search(r'\[AB-[A-Z0-9-]{4,60}\]',job['subject'])
    if marker:
        if marker[0] not in subject:return None
    elif sender!=job['recipient'].lower() or job['subject'] not in subject:return None
    recipients={value.lower() for _,value in getaddresses(message.get_all('To',[])+message.get_all('Cc',[])+message.get_all('Delivered-To',[]))}
    if account.lower() not in recipients:return None
    try:
        received=parsedate_to_datetime(str(message.get('Date','')))
        if received.tzinfo is None:raise ValueError()
    except (TypeError,ValueError,OverflowError):
        internal=re.search(rb'INTERNALDATE "([^"]+)"',metadata)
        if not internal:raise BuyerError('IMAP: не определено время ответа') from None
        received=datetime.strptime(internal[1].decode('ascii'),'%d-%b-%Y %H:%M:%S %z')
    timestamp=received.timestamp()
    if timestamp<job['created_at']:return None
    if timestamp>now+300:raise BuyerError('IMAP: дата ответа находится в будущем')
    text=text_body(message)
    identifier=str(message.get('Message-ID','')).strip()
    if not re.fullmatch(r'<[^\s<>]{3,460}>',identifier):
        identifier='sha256:'+hashlib.sha256(raw).hexdigest()
    else:identifier='rfc822:'+identifier
    return {'message_id':identifier,'sender':sender,'subject':subject,'received_at':timestamp,
            'text':text,'evidence':'IMAP TLS; Message-ID: '+identifier+'; sha256: '+hashlib.sha256(raw).hexdigest(),
            'prices':prices(text,job['positions']) if job.get('mapping_trusted') else []}


def collect(job, config, remote, folder):
    try:
        account,_=settings(config)
        marker=re.search(r'\[AB-[A-Z0-9-]{4,60}\]',job['subject'])
        query=marker[0] if marker else job['subject']
        messages={};searched=0
        with imap_connection(config) as connection:
            for mailbox,flags in mailboxes(connection):
                if b'\\drafts' in flags:continue
                remote.request('/inbox/'+job['id']+'/heartbeat',lease_token=job['token'])
                select(connection,mailbox)
                uids=search_header(connection,'Subject',query)
                searched+=len(uids)
                if searched>MAX_SEARCH_RESULTS:raise BuyerError('IMAP: слишком много писем в переписке')
                for uid in uids:
                    remote.request('/inbox/'+job['id']+'/heartbeat',lease_token=job['token'])
                    raw,metadata=read_message(connection,uid)
                    incoming=parse_message(raw,metadata,job,account,time.time())
                    if incoming is None:continue
                    key=incoming['message_id']
                    if key in messages and (messages[key]['text'],messages[key]['sender'])!=(incoming['text'],incoming['sender']):
                        raise BuyerError('IMAP: один Message-ID содержит разные ответы')
                    messages[key]=incoming
                    if len(messages)>20:raise BuyerError('IMAP: больше 20 ответов, требуется отдельная проверка')
        for fingerprint in set(job.get('known_web_replies',[])):
            matches=[key for key,m in messages.items() if web_reply_fingerprint(m['sender'],m['text'],m['received_at'])==fingerprint]
            if len(matches)>1:
                raise BuyerError('IMAP: несколько писем совпадают с прежним ответом; нужна сверка без автоматического объединения')
            if matches:del messages[matches[0]]
        result={'status':'checked','detail':f'IMAP: проверена переписка; ответов: {len(messages)}. Вложения автоматически не разбирались.',
                'messages':sorted(messages.values(),key=lambda m:m['received_at'])}
    except (OSError,imaplib.IMAP4.error,BuyerError,ValueError) as error:
        result={'status':'blocked','detail':failure(error,'IMAP'),'messages':[]}
    save(folder/'reply.json',result)
    return result


def outgoing(job, account):
    recipient=address(job['recipient'])
    if not isinstance(job.get('subject'),str) or not job['subject'] or len(job['subject'])>500 or re.search(r'[\r\n]',job['subject']):
        raise BuyerError('Некорректная тема письма')
    if not isinstance(job.get('body'),str) or not job['body'].strip() or len(job['body'])>100000:
        raise BuyerError('Некорректный текст письма')
    identity=hashlib.sha256(json.dumps([job['id'],job['token']],ensure_ascii=False).encode()).hexdigest()
    identifier='<autobot.'+identity+'@'+account.rsplit('@',1)[1]+'>'
    message=EmailMessage(policy=policy.SMTP)
    message['From']=account;message['To']=recipient;message['Subject']=job['subject']
    message['Message-ID']=identifier
    message['Date']=format_datetime(datetime.fromtimestamp(job['created_at'],timezone.utc))
    message.set_content(job['body'])
    return message.as_bytes(),identifier


def sent_folder(connection):
    folders=mailboxes(connection)
    matches=[name for name,flags in folders if b'\\sent' in flags]
    if not matches:
        matches=[name for name,flags in folders if name.strip(b'"').lower() in (b'sent',b'sent messages',b'sent items')]
    if len(matches)!=1:raise BuyerError('IMAP: не определена папка отправленных')
    return matches[0]


def sent_copy(config, identifier, raw, *, append):
    with imap_connection(config) as connection:
        mailbox=sent_folder(connection);select(connection,mailbox)
        uids=search_header(connection,'Message-ID',identifier)
        for uid in uids:
            existing,_=read_message(connection,uid)
            candidate=BytesParser(policy=policy.default).parsebytes(existing)
            original=BytesParser(policy=policy.default).parsebytes(raw)
            if (str(candidate.get('Message-ID')),str(candidate.get('From')),str(candidate.get('To')),str(candidate.get('Subject')),text_body(candidate,strip_quotes=False)) == (
                    identifier,str(original['From']),str(original['To']),str(original['Subject']),text_body(original,strip_quotes=False)):
                return True
        if uids:raise BuyerError('IMAP: Message-ID отправленного письма не совпадает с заданием')
        if not append:return False
        if connection.append(mailbox,'\\Seen',imaplib.Time2Internaldate(time.time()),raw)[0]!='OK':
            raise BuyerError('IMAP: не удалось сохранить копию отправленного письма')
        return True


def send(job, config, remote, folder, state):
    path=folder/'state.json'
    # Any existing unrecognized journal may describe a transmitted message.
    submitted=bool(state)
    try:
        account,_=settings(config)
        raw,identifier=outgoing(job,account)
        signature=hashlib.sha256(raw).hexdigest()
        if state and (state.get('transport')!='smtp_imap' or state.get('sha256')!=signature or state.get('phase') not in ('submitting','accepted')):
            raise BuyerError('Журнал отправки не соответствует письму; повтор запрещён')
        if state.get('phase')=='submitting':
            confirmed=sent_copy(config,identifier,raw,append=False)
            receipt={'status':'sent' if confirmed else 'uncertain',
                     'detail':'Письмо сверено по Message-ID и содержимому в отправленных; повтор не выполнялся.' if confirmed else 'Соединение прервалось при отправке. Подтверждения нет; автоматический повтор запрещён.',
                     'evidence':('IMAP Message-ID: '+identifier+'; sha256: '+signature) if confirmed else ''}
            save(path,{**state,'receipt':receipt});return receipt
        if state.get('phase')!='accepted':
            if job['status']!='sending':
                return {'status':'uncertain','detail':'Нет журнала действующей отправки; автоматический повтор запрещён.','evidence':''}
            with smtp_connection(config) as connection:
                remote.request('/outbox/'+job['id']+'/heartbeat',lease_token=job['token'])
                for command,value in ((connection.mail,account),(connection.rcpt,job['recipient'])):
                    code,detail=command(value)
                    if code not in (250,251):raise smtplib.SMTPResponseException(code,b'Envelope rejected')
                remote.request('/outbox/'+job['id']+'/heartbeat',lease_token=job['token'])
                state={'transport':'smtp_imap','phase':'submitting','message_id':identifier,'sha256':signature,'started_at':time.time()}
                save(path,state)
                submitted=True
                try:code,detail=connection.data(raw)
                except smtplib.SMTPDataError:
                    submitted=False
                    raise
                if code!=250:
                    submitted=False
                    raise smtplib.SMTPResponseException(code,b'DATA rejected')
                state.update(phase='accepted',accepted_at=time.time(),smtp_code=code)
                save(path,state)
        proof={'message_id':identifier,'sha256':signature,'smtp_code':state['smtp_code'],'accepted_at':state['accepted_at']}
        save(folder/'smtp-proof.json',proof)
        copy_ok=False
        try:copy_ok=sent_copy(config,identifier,raw,append=True)
        except (OSError,imaplib.IMAP4.error,BuyerError):pass
        receipt={'status':'sent','detail':'Почтовый сервер принял письмо (SMTP 250). Доставка получателю не подтверждена.'+(' Копия сохранена в отправленных.' if copy_ok else ' Копия в отправленных пока не подтверждена.'),
                 'evidence':'SMTP 250; Message-ID: '+identifier+'; sha256: '+signature}
    except (OSError,smtplib.SMTPException,imaplib.IMAP4.error,BuyerError,ValueError) as error:
        receipt={'status':'uncertain' if submitted else 'blocked',
                 'detail':failure(error,'Почта')+(' Отправка могла состояться; автоматический повтор запрещён.' if submitted else ' Письмо не отправлялось.'),'evidence':''}
    save(path,{**state,'receipt':receipt})
    return receipt
