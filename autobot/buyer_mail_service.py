"""Standalone Windows/macOS/Linux worker: templates, SMTP and IMAP only.

Run/check require explicit transports. A misconfiguration cannot fall back to
a browser or an agent. Credentials are read from private files, never argv.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import sys
import time

from autobot.buyer_mail_transport import check_connection
from autobot.atomic_output import output_lock
from autobot.buyer_sender import process_next, save
from autobot.buyer_worker import QueueClient, Worker
from autobot.hermes_buyer import BuyerError, DraftJournal


def load_config(path):
    config=json.loads(path.read_text(encoding='utf-8'))
    if (config.get('sender_mode')!='smtp_imap' or config.get('inbox_mode')!='smtp_imap'
            or config.get('draft_mode')!='template' or config.get('collect_replies') is not True):
        raise BuyerError('Почтовый сервис требует smtp_imap, шаблоны и проверку ответов')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}',str(config.get('worker_id',''))):
        raise BuyerError('Не задан постоянный идентификатор исполнителя')
    for key in ('outbox_dir','mail_password_file','queue_token_file'):
        if not isinstance(config.get(key),str) or not Path(config[key]).is_absolute():
            raise BuyerError('В конфигурации нужны абсолютные пути журналов и секретов')
    return config


def configure(config):
    """Human-operated terminal only; no echo, shell history or model access."""
    if not sys.stdin.isatty():
        raise BuyerError('Откройте интерактивный терминал для скрытого ввода пароля')
    secret=getpass.getpass('Mail.ru application password (input hidden): ').strip()
    if not secret or len(secret)>500 or '\n' in secret or '\r' in secret:
        raise BuyerError('Пароль не сохранён: пустое или некорректное значение')
    if secret!=getpass.getpass('Repeat application password: ').strip():
        raise BuyerError('Пароли не совпали; прежний пароль сохранён')
    path=Path(config['mail_password_file'])
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_suffix('.new')
    # O_EXCL/O_NOFOLLOW prevent accidentally writing through a stale symlink.
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as output:
            output.write(secret);output.flush();os.fsync(output.fileno())
        os.replace(temp,path)
        if os.name=='posix':
            directory=os.open(path.parent,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:
        if temp.exists():temp.unlink()
    print('Password saved privately. The worker has not been started.',flush=True)


class Service:
    def __init__(self,config,remote,drafts,*,inbox_only=False):
        self.config,self.remote,self.drafts=config,remote,drafts
        self.root=Path(config['outbox_dir'])
        self.inbox_only=inbox_only
        self.next_check={}
        self.protocol_errors={}
        self.prefer_inbox=True

    def publish(self, state):
        if self.config.get('local_control'):
            public = {key: state.get(key) is True for key in ('running','ok','receiving','sending')}
            try:
                self.remote.request('/service/status', state=public)
                state['queue_connected'] = True
            except BuyerError:
                state['queue_connected'] = False
        save(self.root/'service-status.json', state)

    def step(self):
        # SMTP outages must not hide incoming quotations. Each direction has
        # its own health/backoff, and sending also needs IMAP for reconciliation.
        protocols=('IMAP',) if self.inbox_only else ('IMAP','SMTP')
        for protocol in protocols:
            if time.time()<self.next_check.get(protocol,0): continue
            try:
                check_connection(self.config,protocols=(protocol,))
                self.protocol_errors.pop(protocol,None)
                self.next_check[protocol]=time.time()+300
            except BuyerError as error:
                self.protocol_errors[protocol]=str(error)
                self.next_check[protocol]=time.time()+60
        if 'IMAP' in self.protocol_errors:
            raise BuyerError(self.protocol_errors['IMAP'])
        can_send=not self.inbox_only and 'SMTP' not in self.protocol_errors
        draft_status=self.drafts.step() if can_send else 'idle'
        direction,status=process_next(self.config,self.remote,prefer_inbox=self.prefer_inbox,allow_outbox=can_send)
        self.prefer_inbox=direction!='inbox'
        if status in ('blocked','uncertain'):self.next_check.clear()
        return {'direction':direction,'result':status,'draft':draft_status,
                'receiving':True,'sending':can_send,
                'send_detail':'Отправка приостановлена: включён режим получения' if self.inbox_only else self.protocol_errors.get('SMTP','')}

    def run(self,once=False):
        while True:
            if self.config.get('local_control') and (self.root/'stop-request').exists():
                self.publish({'ok':True,'running':False,'checked_at':time.time(),
                    'detail':'Остановлено пользователем'})
                return 0
            delay=10
            try:
                result=self.step()
                state={'ok':True,'checked_at':time.time(),**result}
            except BuyerError as error:
                delay=60
                state={'ok':False,'checked_at':time.time(),'detail':str(error)}
            except Exception:
                # No SMTP responses, credentials or message bodies in logs.
                self.next_check.clear();delay=60
                state={'ok':False,'checked_at':time.time(),'detail':'Внутренняя ошибка сервиса; повтор отправки защищён журналом'}
            self.publish(state | {'running':True,'pid':os.getpid()})
            if not state['ok'] or state.get('direction') or state.get('draft')!='idle':
                print(json.dumps(state,ensure_ascii=False),flush=True)
            if once:return 0 if state['ok'] else 1
            # Finish the current SMTP/IMAP operation before a requested stop.
            # No process killing: an in-flight submission keeps its receipt.
            for _ in range(delay):
                if self.config.get('local_control') and (self.root/'stop-request').exists():break
                time.sleep(1)


def main():
    parser=argparse.ArgumentParser(description='AutoBot mail without agents or browsers')
    parser.add_argument('command',choices=['configure','check','run','status'])
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--inbox-only',action='store_true',help='Read existing supplier replies; never claim an outgoing message')
    args=parser.parse_args()
    os.umask(0o077)
    try:
        config=load_config(args.config)
        if args.command=='configure':configure(config);return 0
        if args.command=='check':
            protocols=('IMAP',) if args.inbox_only else ('SMTP','IMAP')
            print(json.dumps(check_connection(config,protocols=protocols),ensure_ascii=False));return 0
        root=Path(config['outbox_dir'])
        if args.command=='status':
            state=json.loads((root/'service-status.json').read_text())
            print(json.dumps(state,ensure_ascii=False))
            return 0 if state.get('ok') and time.time()-state.get('checked_at',0)<1800 else 1
        token_path=Path(config['queue_token_file'])
        if os.name=='posix' and token_path.stat().st_mode & 0o077:
            raise BuyerError('Файл ключа очереди должен иметь права 0600')
        token=token_path.read_text().strip()
        remote=QueueClient(config['queue_url'],token,config['worker_id']+'-sender')
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        with output_lock(root/'sender',timeout=0):
            drafts=Worker(QueueClient(config['queue_url'],token,config['worker_id']+'-draft'),None,DraftJournal(root/'drafts.sqlite3'))
            return Service(config,remote,drafts,inbox_only=args.inbox_only).run(args.once)
    except BuyerError as error:
        print(str(error),file=sys.stderr);return 1
    except (OSError,ValueError,KeyError):
        print('Проверьте конфигурацию, секреты и журнал сервиса; другой экземпляр может быть уже запущен.',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
