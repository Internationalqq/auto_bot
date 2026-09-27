"""Standalone server worker: templates, SMTP sending and IMAP replies only.

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
from autobot.buyer_sender import process_next, save
from autobot.buyer_worker import QueueClient, Worker
from autobot.hermes_buyer import BuyerError, DraftJournal


def load_config(path):
    config=json.loads(path.read_text(encoding='utf-8'))
    if (config.get('sender_mode')!='smtp_imap' or config.get('inbox_mode')!='smtp_imap'
            or config.get('draft_mode')!='template' or config.get('collect_replies') is not True):
        raise BuyerError('Серверный сервис требует smtp_imap, шаблоны и проверку ответов')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}',str(config.get('worker_id',''))):
        raise BuyerError('Не задан постоянный идентификатор серверного исполнителя')
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
    def __init__(self,config,remote,drafts):
        self.config,self.remote,self.drafts=config,remote,drafts
        self.root=Path(config['outbox_dir'])
        self.ready_until=0
        self.prefer_inbox=True

    def step(self):
        # No queue is claimed while credentials or the mail network are down.
        if time.time()>=self.ready_until:
            check_connection(self.config)
            self.ready_until=time.time()+300
        draft_status=self.drafts.step()
        direction,status=process_next(self.config,self.remote,prefer_inbox=self.prefer_inbox)
        self.prefer_inbox=direction!='inbox'
        if status in ('blocked','uncertain'):self.ready_until=0
        return {'direction':direction,'result':status,'draft':draft_status}

    def run(self,once=False):
        while True:
            delay=10
            try:
                result=self.step()
                state={'ok':True,'checked_at':time.time(),**result}
            except BuyerError as error:
                self.ready_until=0;delay=60
                state={'ok':False,'checked_at':time.time(),'detail':str(error)}
            except Exception:
                # No SMTP responses, credentials or message bodies in logs.
                self.ready_until=0;delay=60
                state={'ok':False,'checked_at':time.time(),'detail':'Внутренняя ошибка сервиса; повтор отправки защищён журналом'}
            save(self.root/'service-status.json',state)
            if not state['ok'] or state.get('direction') or state.get('draft')!='idle':
                print(json.dumps(state,ensure_ascii=False),flush=True)
            if once:return 0 if state['ok'] else 1
            time.sleep(delay)


def main():
    parser=argparse.ArgumentParser(description='AutoBot server mail without agents')
    parser.add_argument('command',choices=['configure','check','run','status'])
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    os.umask(0o077)
    try:
        config=load_config(args.config)
        if args.command=='configure':configure(config);return 0
        if args.command=='check':
            print(json.dumps(check_connection(config),ensure_ascii=False));return 0
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
        import fcntl
        with (root/'sender.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            drafts=Worker(QueueClient(config['queue_url'],token,config['worker_id']+'-draft'),None,DraftJournal(root/'drafts.sqlite3'))
            return Service(config,remote,drafts).run(args.once)
    except BuyerError as error:
        print(str(error),file=sys.stderr);return 1
    except (OSError,ValueError,KeyError):
        print('Проверьте конфигурацию, секреты и журнал сервиса; другой экземпляр может быть уже запущен.',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
