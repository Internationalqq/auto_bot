"""Explicit local start/stop/status; no scheduler, browser or model runtime."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from autobot.atomic_output import output_lock
from autobot.buyer_mail_service import load_config
from autobot.buyer_sender import save
from autobot.hermes_buyer import BuyerError


def running(root):
    try:
        with output_lock(root/'sender', timeout=0):
            return False
    except TimeoutError:
        return True


def status(config):
    root = Path(config['outbox_dir'])
    active = running(root)
    try:
        last = json.loads((root/'service-status.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        last = {}
    stopping = active and (root/'stop-request').exists()
    fresh = time.time()-last.get('checked_at', 0) < 300
    return {'running': active, 'stopping': stopping,
            'state': 'Останавливается после текущей операции' if stopping else
                     'Работает' if active else 'Остановлен',
            'connected': bool(active and fresh and last.get('ok') and last.get('queue_connected', True)),
            'receiving': bool(active and fresh and last.get('receiving')),
            'sending': bool(active and fresh and last.get('sending')),
            'checked_at': last.get('checked_at'),
            'detail': (last.get('detail') or last.get('send_detail', '')) if active else ''}


def start(config_path, *, inbox_only=False):
    config_path = config_path.resolve()
    config = load_config(config_path)
    if config.get('local_control') is not True:
        raise BuyerError('Нужна локальная конфигурация с local_control=true')
    root = Path(config['outbox_dir'])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with output_lock(root/'control', timeout=0):
        if running(root):
            return status(config) | {'already_running':True}
        for key in ('queue_token_file', 'mail_password_file'):
            if not Path(config[key]).is_file():
                raise BuyerError('Сначала настройте ключ очереди и пароль приложения почты')
        (root/'stop-request').unlink(missing_ok=True)
        save(root/'service-status.json', {'ok':False,'checked_at':time.time(),'detail':'Подключается'})
        args = [sys.executable, '-m', 'autobot.buyer_mail_service', 'run', '--config', str(config_path)]
        if inbox_only: args.append('--inbox-only')
        env = dict(os.environ, BUYER_DISCOVERY_WORKER='0', PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        # The installed source lives beside this module, independently of the
        # caller's current directory and the Codex app's runtime.
        source = Path(__file__).resolve().parents[1]
        env['PYTHONPATH'] = str(source)
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session':True}
        with (root/'service.log').open('ab') as log:
            child = subprocess.Popen(args, cwd=source, env=env, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, **options)
        for _ in range(50):
            if running(root):return status(config) | {'started':True}
            if child.poll() is not None:
                raise BuyerError('Процесс не запустился; подробности в локальном service.log')
            time.sleep(.1)
        raise BuyerError('Запуск ещё не подтверждён; проверьте статус перед повтором')


def stop(config):
    if config.get('local_control') is not True:
        raise BuyerError('Остановка доступна только для локальной конфигурации')
    root = Path(config['outbox_dir'])
    with output_lock(root/'control', timeout=0):
        if running(root):
            (root/'stop-request').touch(mode=0o600)
    return status(config)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start','stop','status','check'))
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--inbox-only', action='store_true')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        config = load_config(args.config)
        if args.action == 'start': result = start(args.config, inbox_only=args.inbox_only)
        elif args.action == 'stop': result = stop(config)
        elif args.action == 'status': result = status(config)
        else:
            from autobot.buyer_mail_transport import check_connection
            result = check_connection(config, protocols=('IMAP',) if args.inbox_only else ('SMTP','IMAP'))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except (BuyerError, OSError, ValueError) as error:
        print(str(error) if isinstance(error, BuyerError) else 'Не удалось прочитать локальную конфигурацию или занять журнал процесса.', file=sys.stderr)
        return 1


if __name__ == '__main__': raise SystemExit(main())
