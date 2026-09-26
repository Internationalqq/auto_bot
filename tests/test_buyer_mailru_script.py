import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from autobot import buyer_mailru_script as script
from autobot.buyer_sender import execute as dispatch
from autobot.hermes_buyer import BuyerError

JOB = {'id': 'job1', 'status': 'sending', 'token': 'token', 'recipient': 'supplier@example.org',
       'subject': 'Щебень', 'body': 'Добрый день!\n\nНужен щебень М1200 — 57,859 м³.\n\nСпасибо!'}


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.browser = Mock(); self.remote = Mock()
        self.proof = {'recipient': JOB['recipient'], 'subject': JOB['subject'], 'body': JOB['body']}

    def run_script(self, state=None):
        return script.execute(JOB, {}, self.remote, self.folder, state or {}, browser_factory=lambda config: self.browser)

    def test_send_reserved_before_click_and_confirmed_from_sent(self):
        self.browser.find_sent.side_effect = [None, self.proof]
        def send(job):
            state = json.loads((self.folder/'state.json').read_text())
            self.assertEqual(state['phase'], 'send_reserved')
            self.assertEqual(json.loads((self.folder/'request.json').read_text())['recipient'], JOB['recipient'])
        self.browser.send.side_effect = send
        result = self.run_script()
        self.assertEqual(result['status'], 'sent')
        self.assertIn('sha256:', result['evidence'])
        self.browser.send.assert_called_once_with(JOB)
        self.browser.close.assert_called_once()

    def test_existing_exact_message_does_not_send(self):
        self.browser.find_sent.return_value = self.proof
        self.assertEqual(self.run_script()['status'], 'sent')
        self.browser.prepare.assert_not_called(); self.browser.send.assert_not_called()

    def test_restart_after_click_without_ack_only_reconciles(self):
        self.browser.find_sent.return_value = None
        result = self.run_script({'transport': 'mailru_lite', 'phase': 'send_reserved'})
        self.assertEqual(result['status'], 'uncertain')
        self.browser.prepare.assert_not_called(); self.browser.send.assert_not_called()

    def test_click_error_is_uncertain_not_retryable(self):
        self.browser.find_sent.return_value = None
        self.browser.send.side_effect = BuyerError('Нет подтверждения')
        self.assertEqual(self.run_script()['status'], 'uncertain')
        self.assertEqual(json.loads((self.folder/'state.json').read_text())['phase'], 'send_reserved')

    def test_delayed_confirmation_observes_without_another_send_click(self):
        browser=object.__new__(script.FirefoxLight)
        browser.validate=Mock();browser.click=Mock();browser.call=Mock()
        browser.capture=Mock(side_effect=[
            {'elements':[{'role':'AXButton','label':'Отправить','bounds':[1,1,10,10],'index':9}]},
            {'window_title':'Новое письмо'}, {'window_title':'Новое письмо'},
            {'window_title':'Письмо отправлено - Mail.ru'}])
        browser.send(JOB)
        browser.click.assert_called_once()

    def test_wrong_account_before_prepare_is_blocked(self):
        self.browser.find_sent.side_effect = BuyerError('Другой ящик')
        self.assertEqual(self.run_script()['status'], 'blocked')
        self.browser.send.assert_not_called()

    def test_sending_after_reply_search_returns_to_mail_navigation(self):
        browser=object.__new__(script.FirefoxLight);browser.link=Mock()
        browser.capture=Mock(side_effect=[{'window_title':'Поиск - AB-CAB-01'},
            {'window_title':'Отправленные - Mail.ru','elements':[]}])
        self.assertIsNone(browser.find_sent(JOB))
        self.assertEqual([c.args[0] for c in browser.link.call_args_list],['Назад во «Входящие»','Отправленные'])

    def test_capture_restores_only_one_observed_tab_for_our_account(self):
        browser=object.__new__(script.FirefoxLight);browser.sender='buyer@mail.ru';browser.click=Mock()
        tab={'role':'AXRadioButton','label':'Поиск - AB-CAB-01 - buyer@mail.ru - Почта Mail.ru','index':7,'bounds':[1,1,20,20]}
        valid={'window_title':tab['label'],'elements':[{'role':'AXStaticText','label':'light.mail.ru/search/?q_query=AB-CAB-01'}]}
        browser.call=Mock(side_effect=[{'window_title':'Другая вкладка','elements':[tab]},valid])
        self.assertEqual(browser.capture(),valid);browser.click.assert_called_once_with(tab)
        browser.click.reset_mock();browser.call=Mock(return_value={'window_title':'Другая вкладка','elements':[]})
        with self.assertRaises(BuyerError):browser.capture()
        browser.click.assert_not_called()

    def test_script_dispatch_never_contacts_model_and_completed_is_reused(self):
        config = {'sender_mode': 'mailru_lite_script', 'outbox_dir': str(self.folder)}
        with patch('autobot.buyer_sender.sender_client') as model, patch.object(script, 'execute', return_value={'status': 'sent'}) as run:
            self.assertEqual(dispatch(JOB, config, self.remote), {'status': 'sent'})
            run.assert_called_once(); model.assert_not_called()

    def test_legacy_attempt_is_not_restarted_by_switching_mode(self):
        import hashlib
        folder = self.folder/JOB['id']/hashlib.sha256(JOB['token'].encode()).hexdigest()[:24]
        folder.mkdir(parents=True)
        (folder/'state.json').write_text('{"started_at": 1}')
        with patch.object(script, 'execute') as run:
            self.assertEqual(dispatch(JOB, {'sender_mode': 'mailru_lite_script', 'outbox_dir': str(self.folder)}, self.remote)['status'], 'uncertain')
            run.assert_not_called()

    def test_field_readback_checks_recipient_body_and_empty_copy(self):
        raw = {'elements': [{'role': 'AXTextField', 'label': label, 'value': value} for label, value in
                          [('Кому:', JOB['recipient']), ('Тема:', JOB['subject']), ('Копия:', ''), ('Скрытая:', '')]] +
                          [{'role': 'AXTextArea', 'value': JOB['body']}]}
        script.validate_form(raw, JOB)
        for index in range(len(raw['elements'])):
            old = raw['elements'][index]['value']; raw['elements'][index]['value'] = 'unexpected'
            with self.assertRaises(BuyerError): script.validate_form(raw, JOB)
            raw['elements'][index]['value'] = old

    def test_sent_evidence_checks_folder_recipient_full_body(self):
        texts = ['light.mail.ru/message/12345/?folder=500000', JOB['subject'], '<buyer@example.org>',
                 'Кому:', JOB['recipient'], 'Сегодня, 13:40', *JOB['body'].split('\n\n'), 'Быстрый ответ']
        def snapshot(values): return {'elements': [{'role': 'AXStaticText', 'label': text} for text in values]}
        proof = script.sent_evidence(snapshot(texts), JOB, 'buyer@example.org')
        self.assertEqual(proof['body'], JOB['body'])
        for bad in [texts[:-1], [texts[0].replace('500000', '0')] + texts[1:],
                    texts[:4] + ['someone@example.org'] + texts[5:], texts[:-1] + ['Лишняя подпись', texts[-1]]]:
            with self.assertRaises(BuyerError): script.sent_evidence(snapshot(bad), JOB, 'buyer@example.org')


if __name__ == '__main__': unittest.main()
