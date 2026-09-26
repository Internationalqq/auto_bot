"""Sequential Mail.ru Light sender via the installed Mac CUA driver, without AI.

Uses the existing Firefox login. No cookies, passwords, browser restarts or
weakened window guards. A durable reservation precedes the one Send click.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from autobot.hermes_buyer import BuyerError


def unique(elements):
    return list({(e['role'], e.get('label', ''), tuple(e['bounds'])): e for e in elements}.values())


def validate_form(raw, job):
    for label, expected in [('Кому:', job['recipient']), ('Тема:', job['subject']), ('Копия:', ''), ('Скрытая:', '')]:
        fields = [e for e in raw['elements'] if e['role'] == 'AXTextField' and e.get('label') == label]
        if not fields or any(e.get('value', '') != expected for e in fields):
            raise BuyerError('Поле письма не прошло проверку: ' + label)
    areas = [e for e in raw['elements'] if e['role'] == 'AXTextArea']
    if len(areas) != 1 or areas[0].get('value') != job['body']:
        raise BuyerError('Текст письма не совпадает с заявкой')


def sent_evidence(snapshot, job, sender):
    elements = snapshot['elements']
    urls = [e['label'] for e in elements if e['role'] == 'AXStaticText' and
            re.fullmatch(r'light\.mail\.ru/message/\d+/\?folder=500000', e['label'])]
    texts = [e['label'] for e in elements if e['role'] == 'AXStaticText']
    if len(urls) != 1 or job['subject'] not in texts or '<' + sender + '>' not in texts:
        raise BuyerError('Нет подтверждения письма в отправленных')
    recipient = texts.index('Кому:') if 'Кому:' in texts else -1
    if recipient < 0 or texts[recipient + 1:recipient + 2] != [job['recipient']]:
        raise BuyerError('Адресат отправленного письма не совпадает')
    # Light renders date after recipient, then the message, then Quick reply.
    end = texts.index('Быстрый ответ') if 'Быстрый ответ' in texts else -1
    body = texts[recipient + 3:end]
    if end <= recipient or ' '.join(' '.join(body).split()) != ' '.join(job['body'].split()):
        raise BuyerError('Содержимое отправленного письма не совпадает')
    return {'url': 'https://' + urls[0], 'recipient': job['recipient'],
            'sender': sender, 'subject': job['subject'], 'body': '\n\n'.join(body), 'checked_at': time.time()}


class FirefoxLight:
    def __init__(self, config):
        self.sender = config['sender_email']
        profile = Path.home() / '.hermes/profiles/autobot-mail'
        repo = Path.home() / '.hermes/hermes-agent'
        if config.get('sender_profile') != 'autobot-mail' or not repo.is_dir():
            raise BuyerError('Не настроен отдельный исполнитель autobot-mail')
        self.cwd = Path.cwd()
        os.environ['HERMES_HOME'] = str(profile)
        os.environ['HERMES_CUA_DRIVER_CMD'] = str(Path.home() / '.local/bin/cua-driver')
        sys.path.insert(0, str(repo))
        os.chdir(repo)
        from dotenv import load_dotenv
        load_dotenv(profile / '.env')
        from hermes_cli.plugins import discover_plugins, invoke_hook
        discover_plugins()
        from tools.registry import registry
        self.handler = registry._tools['computer_use'].handler
        self.exit_hook = invoke_hook
        self.task = 'autobot-script-' + str(os.getpid())

    def close(self):
        try: self.exit_hook('on_turn_exit', task_id=self.task)
        finally: os.chdir(self.cwd)

    def call(self, args):
        value = self.handler(args, task_id=self.task)
        value = json.loads(value) if isinstance(value, str) else value
        if value.get('error') or value.get('ok') is False:
            raise BuyerError('Драйвер Firefox не подтвердил действие')
        return value

    def capture(self):
        state = self.call({'action': 'capture', 'app': 'Firefox', 'mode': 'ax', 'max_elements': 1200})
        if self.sender + ' - Почта Mail.ru' not in state.get('window_title', ''):
            tabs = unique([e for e in state.get('elements', []) if e['role'] == 'AXRadioButton'
                           and e.get('label', '').endswith(self.sender + ' - Почта Mail.ru')])
            if len(tabs) == 1:
                # Restore only the observed tab for this account, preserving
                # other tabs. All account/URL guards still run after selection.
                self.click(tabs[0])
                state = self.call({'action': 'capture', 'app': 'Firefox', 'mode': 'ax', 'max_elements': 1200})
        if self.sender + ' - Почта Mail.ru' not in state.get('window_title', ''):
            raise BuyerError('Откройте рабочую почту в упрощённой версии Mail.ru в Firefox')
        if not any(e['role'] == 'AXStaticText' and e['label'].startswith('light.mail.ru/') for e in state['elements']):
            raise BuyerError('Не подтверждён адрес рабочей почты')
        return state

    def click(self, element):
        self.call({'action': 'click', 'element': element['index']})
        self.call({'action': 'wait', 'seconds': 1})

    def link(self, label):
        state = self.capture()
        links = unique([e for e in state['elements'] if e['role'] == 'AXLink' and e['label'] == label])
        if len(links) != 1: raise BuyerError('Не найдена однозначная ссылка: ' + label)
        self.click(links[0])

    def find_sent(self, job):
        state = self.capture()
        if state['window_title'].startswith('Новое письмо'):
            raise BuyerError('В Firefox открыт черновик. Завершите или закройте его')
        if state['window_title'].startswith('Поиск - '):
            # The inbox collector leaves search results open; that page has
            # folder filters rather than the normal Sent navigation link.
            self.link('Назад во «Входящие»')
        self.link('Отправленные')
        for attempt in range(5):
            state = self.capture()
            if state['window_title'].startswith('Отправленные'): break
            if attempt == 4: raise BuyerError('Не открылась папка отправленных')
            self.call({'action':'wait','seconds':1})
        recipients = unique([e for e in state['elements'] if e['role'] == 'AXLink' and e['label'] == job['recipient']])
        candidates = unique([e for e in state['elements'] if e['role'] == 'AXLink' and e['label'] == job['subject'] and
                             any(abs(e['bounds'][1] - r['bounds'][1]) < 2 for r in recipients)])
        if len(candidates) > 1: raise BuyerError('Найдено несколько похожих отправленных писем')
        if not candidates: return None
        self.click(candidates[0])
        for attempt in range(3):
            try: return sent_evidence(self.capture(), job, self.sender)
            except BuyerError:
                if attempt == 2: raise
                self.call({'action': 'wait', 'seconds': 1})

    def prepare(self, job):
        self.link('Написать')
        for label, value in [('Кому:', job['recipient']), ('Тема:', job['subject'])]:
            state = self.capture()
            if not state['window_title'].startswith('Новое письмо'): raise BuyerError('Не открылась форма письма')
            fields = unique([e for e in state['elements'] if e['role'] == 'AXTextField' and e['label'] == label])
            if len(fields) != 1: raise BuyerError('Поле письма неоднозначно')
            self.call({'action': 'set_value', 'element': fields[0]['index'], 'value': value})
        state = self.capture()
        areas = [e for e in state['elements'] if e['role'] == 'AXTextArea']
        if len(areas) != 1: raise BuyerError('Не найдено тело письма')
        self.call({'action': 'set_value', 'element': areas[0]['index'], 'value': job['body']})
        self.validate(job)

    def validate(self, job):
        state = self.capture()
        if not state['window_title'].startswith('Новое письмо'): raise BuyerError('Форма письма закрыта')
        from tools.computer_use.tool import _get_backend
        backend = _get_backend(); session = backend._session
        result = session._bridge.run(session._session.call_tool('get_window_state', {
            'pid': backend._active_pid, 'window_id': backend._active_window_id,
            'include_screenshot': False, 'max_elements': 1200}))
        if result.isError or not result.structuredContent: raise BuyerError('Нет проверяемых значений полей')
        validate_form(result.structuredContent, job)

    def send(self, job):
        self.validate(job)
        state = self.capture()
        buttons = [e for e in state['elements'] if e['role'] == 'AXButton' and e['label'] == 'Отправить']
        if not buttons: raise BuyerError('Не найдена кнопка отправки')
        self.click(min(buttons, key=lambda e: e['bounds'][1]))
        # Mail.ru can finish submitting after the first read-back. Observe only;
        # never click Send again while waiting for confirmation.
        for attempt in range(5):
            if self.capture()['window_title'].startswith('Письмо отправлено'): return
            if attempt < 4: self.call({'action':'wait','seconds':1})
        raise BuyerError('Почта не подтвердила отправку; повтор запрещён')


def execute(job, config, remote, folder, state, *, browser_factory=FirefoxLight):
    from autobot.buyer_sender import save
    def confirmed(proof):
        save(folder / 'sent-proof.json', proof)
        digest = hashlib.sha256((folder / 'sent-proof.json').read_bytes()).hexdigest()
        return {'status': 'sent', 'detail': 'Скрипт проверил адресата и полный текст в отправленных Mail.ru.',
                'evidence': str(folder / 'sent-proof.json') + ' sha256:' + digest}
    browser = None
    reserved = bool(state)
    try:
        browser = browser_factory(config)
        proof = browser.find_sent(job)
        if proof is None:
            if reserved or job['status'] != 'sending':
                raise BuyerError('Нет подтверждения прежней попытки. Автоматический повтор запрещён')
            remote.request('/outbox/' + job['id'] + '/heartbeat', lease_token=job['token'])
            browser.prepare(job)
            state = {'transport': 'mailru_lite', 'phase': 'send_reserved', 'started_at': time.time()}
            save(folder / 'request.json', {k: job[k] for k in ('id', 'recipient', 'subject', 'body')})
            save(folder / 'state.json', state)
            reserved = True
            browser.send(job)
            proof = browser.find_sent(job)
            if proof is None: raise BuyerError('Отправка не найдена в отправленных; повтор запрещён')
        receipt = confirmed(proof)
    except Exception as error:
        detail = str(error) if isinstance(error, BuyerError) else 'Ошибка браузерного исполнителя: ' + type(error).__name__
        receipt = {'status': 'uncertain' if reserved else 'blocked', 'detail': detail[:1900], 'evidence': ''}
        if reserved and browser is not None:
            # A delayed page transition may have finished after the first
            # read-back. Retry only finding proof, never preparing or sending.
            try:
                proof = browser.find_sent(job)
                if proof is not None: receipt = confirmed(proof)
            except Exception:
                pass  # Preserve the first concrete failure and uncertainty.
    finally:
        if browser is not None: browser.close()
    save(folder / 'state.json', {**state, 'transport': 'mailru_lite', 'receipt': receipt})
    return receipt
