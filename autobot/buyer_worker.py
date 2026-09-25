"""Outbound Mac worker. Local Hermes credentials never leave the Mac.

An expired server lease can be reclaimed, but the durable local journal reuses
the same run/result. An ambiguous Hermes submission never starts a second run.
"""
import argparse
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

import requests
from autobot.hermes_buyer import BuyerError, DraftJournal, HermesClient


class LostLease(BuyerError):
    pass


class QueueClient:
    def __init__(self, url, token, worker_id, session=None):
        address = urlsplit(url)
        if (address.scheme != 'https' or not address.hostname or address.username or
                address.password or address.query or address.fragment or
                address.path.rstrip('/') != '/autobot/api/agent-market/v1/buyer'):
            raise BuyerError('Нужен HTTPS-адрес очереди закупщика')
        if len(token) < 32 or any(c in token for c in '\r\n'):
            raise BuyerError('Не задан ключ очереди закупщика')
        self.url, self.token, self.worker_id = url.rstrip('/'), token, worker_id
        self.session = session or requests.Session()
        self.session.trust_env = False

    def request(self, path, **data):
        try:
            response = self.session.post(self.url + path,
                headers={'Authorization': 'Bearer ' + self.token},
                json={'worker_id': self.worker_id, **data}, timeout=(5, 20), allow_redirects=False)
            if response.status_code == 409:
                raise LostLease('Задание отменено или передано другой попытке')
            if response.status_code != 200 or len(response.content) > 1_000_000:
                raise BuyerError('Очередь недоступна: HTTP ' + str(response.status_code))
            result = response.json()
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise BuyerError('Некорректный ответ очереди')
            return result
        except (requests.RequestException, ValueError) as error:
            if isinstance(error, BuyerError):
                raise
            raise BuyerError('Нет связи с очередью') from None


class Worker:
    def __init__(self, remote, hermes, journal):
        self.remote, self.hermes, self.journal = remote, hermes, journal
        self.job = None

    def step(self):
        if self.job is None:
            self.job = self.remote.request('/claim').get('job')
            if self.job is None:
                return 'idle'
        job = self.job
        path = '/jobs/' + job['id']
        lease = {'lease_token': job['lease_token']}
        try:
            # Confirm the lease before any model work or local result delivery.
            self.remote.request(path + '/heartbeat', **lease)
            local = self.journal.enqueue(job['payload']['draft_task'])
            if local['status'] in {'queued', 'running'}:
                # Retain the current job on connection errors; the next iteration
                # heartbeats and polls the saved run instead of creating another.
                local = self.journal.advance(local['id'], self.hermes)
            if local['status'] == 'draft_ready':
                self.remote.request(path + '/complete', **lease, result=local['result'])
                self.job = None
                return 'completed'
            if local['status'] not in {'queued', 'running'}:
                self.remote.request(path + '/fail', **lease, error=local['status'])
                self.job = None
                return 'failed'
            return 'running'
        except LostLease:
            # A cancelled job may finish inside Hermes, but cannot publish back.
            self.job = None
            return 'lease_lost'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    token = Path(config['queue_token_file']).read_text().strip()
    # Read only the dedicated profile's API key, never its provider auth file.
    env = dict(line.split('=', 1) for line in Path(config['hermes_env_file']).read_text().splitlines()
               if '=' in line and not line.lstrip().startswith('#'))
    hermes = HermesClient(config['hermes_url'], env['API_SERVER_KEY'], standalone=True)
    journal = DraftJournal(config['journal'])
    worker = Worker(QueueClient(config['queue_url'], token, config['worker_id']), hermes, journal)
    # launchd and manual diagnostics must not run two pollers against one journal.
    import fcntl
    with open(str(config['journal']) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                state = worker.step()
                if state != 'idle':
                    print(state, flush=True)
            except BuyerError as error:
                print(str(error), flush=True)  # sanitized client errors only
            if args.once:
                break
            time.sleep(10)


if __name__ == '__main__':
    main()
