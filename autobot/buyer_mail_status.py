"""Bounded, authenticated local mail heartbeats; no credentials or mail text."""
import json
import time

from autobot import buyer_outbox
from autobot.atomic_output import output_lock
from autobot.buyer_sender import save
from autobot.hermes_buyer import BuyerError

FIELDS = ('running', 'ok', 'receiving', 'sending')
FRESH_SECONDS = 300


def path():
    return buyer_outbox.DB_PATH.parent / 'mail-transport' / 'local-service-status.json'


def records():
    try:
        target = path()
        if target.stat().st_size > 16000: return {}
        value = json.loads(target.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def record(worker_id, state):
    if not isinstance(state, dict) or any(type(state.get(key)) is not bool for key in FIELDS):
        raise BuyerError('Ожидается состояние локальной почты')
    stamp = time.time()  # The client clock cannot extend freshness.
    current = {key: state[key] for key in FIELDS} | {'checked_at': stamp}
    if not current['running'] or not current['ok']:
        current['sending'] = current['receiving'] = False
    target = path()
    with output_lock(target):
        previous = {key: value for key, value in records().items()
                    if isinstance(value, dict) and isinstance(value.get('checked_at'), (int, float))
                    and 0 <= stamp - value['checked_at'] < 86400}
        previous[worker_id] = current
        bounded = dict(sorted(previous.items(), key=lambda pair: pair[1]['checked_at'], reverse=True)[:8])
        save(target, bounded)


def read(*, now=None):
    if not path().exists(): return None
    now = time.time() if now is None else now
    values = [value for value in records().values() if isinstance(value, dict)
              and isinstance(value.get('checked_at'), (int, float))]
    fresh = [value for value in values if value.get('running') is True
             and 0 <= now - value['checked_at'] < FRESH_SECONDS]
    current = max(fresh or values, key=lambda item: item['checked_at'], default={})
    stamp = current.get('checked_at')
    if not fresh:
        state, label = 'offline', 'Локальная почта остановлена или компьютер не в сети'
    elif current.get('ok') is not True:
        state, label = 'blocked', 'Локальная почта: проверьте подключение'
    elif current.get('receiving') and current.get('sending'):
        state, label = 'ready', 'Почта работает с вашего компьютера'
    elif current.get('receiving'):
        state, label = 'receiving', 'Ответы проверяются с компьютера; отправка приостановлена'
    else:
        state, label = 'blocked', 'Локальная почта подключается'
    return {'state': state, 'checked_at': stamp, 'label': label, 'execution': 'local'}
