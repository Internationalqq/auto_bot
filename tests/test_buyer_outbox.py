from contextlib import closing
import copy
import json
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from flask import Flask
from autobot import buyer_outbox as box, buyer_routes as routes
from autobot.buyer_sender import execute, read_receipt
from autobot.hermes_buyer import BuyerError
from autobot.uploaded_corrections import CorrectionError


JOB = {'id': 'draft1', 'status': 'completed', 'payload': {'draft_task': {
    'schema_version': 2, 'tender_id': '123456789012345', 'positions': [{'position_key': 'p1'}]}},
    'result': {'drafts': [{'position_keys': ['p1'], 'subject': 'Щебень М1200', 'body': 'Здравствуйте! Нужен щебень М1200 20–40 мм, 57,859 м³. Укажите вашу цену.'}], 'questions': []}}


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        for p in [patch.object(box, 'DB_PATH', self.path/'outbox.db'),
                  patch.object(box.buyer_jobs, 'jobs', side_effect=lambda tid: [copy.deepcopy(JOB)] if tid == '123456789012345' else [])]:
            p.start(); self.addCleanup(p.stop)

    def enqueue(self, recipient='sales@example.org'):
        return box.enqueue('123456789012345', 'draft1', 0, recipient)

    def test_double_click_and_repeat_after_send_are_deduplicated(self):
        first = self.enqueue()
        self.assertEqual(first, self.enqueue('SALES@example.org'))
        job = box.claim('mac')
        receipt = {'status': 'sent', 'detail': 'Проверено в отправленных', 'evidence': 'sent.png hash'}
        self.assertTrue(box.update(first, 'mac', job['token'], receipt))
        self.assertTrue(box.update(first, 'mac', job['token'], receipt))
        self.assertEqual(first, self.enqueue())
        self.assertIsNone(box.claim('mac'))
        self.assertEqual(len(box.listing('123456789012345')), 1)

    def test_stale_send_is_not_reassigned_or_retried(self):
        first = self.enqueue(); job = box.claim('mac')
        with closing(box.connect()) as db, db:
            db.execute('UPDATE outbound SET lease_until=0 WHERE id=?', (first,))
        self.assertIsNone(box.claim('another-mac'))
        self.assertEqual(box.listing('123456789012345')[0]['status'], 'uncertain')
        self.assertEqual(box.claim('mac')['token'], job['token'])
        self.assertFalse(box.update(first, 'other', job['token']))
        self.assertFalse(box.update(first, 'mac', 'wrong'))
        with self.assertRaises(BuyerError): box.retry_blocked('123456789012345', first)

    def test_later_sent_proof_preserves_failure_and_is_idempotent(self):
        key = self.enqueue(); job = box.claim('mac')
        previous = {'status': 'uncertain', 'detail': 'Connection error', 'evidence': ''}
        proof = {'status': 'sent', 'detail': 'Письмо найдено в отправленных', 'evidence': 'sent.txt sha256:abc'}
        box.update(key, 'mac', job['token'], previous)
        self.assertFalse(box.update(key, 'mac', job['token'], proof))
        self.assertTrue(box.reconcile_sent(key, 'mac', job['token'], previous, proof))
        self.assertTrue(box.reconcile_sent(key, 'mac', job['token'], previous, proof))
        row = box.listing('123456789012345')[0]
        self.assertEqual(row['receipt'], proof)
        self.assertEqual([a['receipt'] for a in row['attempts']], [previous])
        self.assertIsNone(box.claim('mac'))
        self.assertEqual(self.enqueue(), key)
        self.assertFalse(box.update(key, 'mac', job['token'], previous))

    def test_reconciliation_fences_active_unfinished_and_wrong_owner(self):
        key = self.enqueue(); job = box.claim('mac')
        previous = {'status': 'uncertain', 'detail': 'Connection error', 'evidence': ''}
        proof = {'status': 'sent', 'detail': 'Письмо найдено', 'evidence': 'sent.txt sha256:abc'}
        self.assertFalse(box.reconcile_sent(key, 'mac', job['token'], previous, proof))
        with closing(box.connect()) as db, db:
            db.execute('UPDATE outbound SET lease_until=0 WHERE id=?', (key,))
        box.listing('123456789012345')
        self.assertFalse(box.reconcile_sent(key, 'mac', job['token'], previous, proof))
        box.update(key, 'mac', job['token'], previous)
        for worker, token, expected in [('foreign', job['token'], previous),
                                        ('mac', 'stale', previous),
                                        ('mac', job['token'], {**previous, 'detail': 'stale'})]:
            self.assertFalse(box.reconcile_sent(key, worker, token, expected, proof))
        with self.assertRaises(BuyerError):
            box.reconcile_sent(key, 'mac', job['token'], previous, {**proof, 'evidence': ''})
        with self.assertRaises(BuyerError):
            box.reconcile_sent(key, 'mac', job['token'], previous, {**proof, 'status': 'blocked'})
        self.assertEqual(box.listing('123456789012345')[0]['receipt'], previous)

    def test_reconciliation_endpoint_requires_auth_and_lease(self):
        key = self.enqueue(); job = box.claim('mac')
        previous = {'status': 'uncertain', 'detail': 'Connection error', 'evidence': ''}
        proof = {'status': 'sent', 'detail': 'Письмо найдено', 'evidence': 'sent.txt sha256:abc'}
        box.update(key, 'mac', job['token'], previous)
        app = Flask(__name__)
        with patch.object(routes.campaigns, 'launch'):
            app.register_blueprint(routes.blueprint)
        client = app.test_client(); url = routes.WORKER_API + '/outbox/' + key + '/reconcile_sent'
        data = {'worker_id': 'mac', 'lease_token': job['token'], 'previous_receipt': previous, 'receipt': proof}
        self.assertEqual(client.post(url, json=data).status_code, 401)
        with patch.dict('os.environ', {'BUYER_WORKER_TOKEN': 'x'*48}):
            headers = {'Authorization': 'Bearer ' + 'x'*48}
            self.assertEqual(client.post(url, json={**data, 'lease_token': 'stale'}, headers=headers).status_code, 409)
            self.assertEqual(client.post(url, json={**data, 'previous_receipt': None}, headers=headers).status_code, 422)
            self.assertEqual(client.post(url, json=data, headers=headers).status_code, 200)
            self.assertEqual(client.post(url, json=data, headers=headers).status_code, 200)

    def test_shared_browser_sends_sequentially_even_with_two_workers(self):
        first=self.enqueue(); second=self.enqueue('second@example.org')
        claim=box.claim('mac')
        self.assertIsNone(box.claim('other-mac'))
        with closing(box.connect()) as db,db:
            db.execute('UPDATE outbound SET lease_until=0 WHERE id=?',(first,))
        self.assertIsNone(box.claim('other-mac'))
        self.assertEqual(box.claim('mac')['id'],first)
        box.update(first,'mac',claim['token'],{'status':'sent','detail':'В отправленных','evidence':'sent.png hash'})
        self.assertEqual(box.claim('other-mac')['id'],second)

    def test_explicit_blocked_retry_preserves_history_and_fences_old_worker(self):
        key = self.enqueue(); old = box.claim('mac')
        receipt = {'status': 'blocked', 'detail': 'Нет инструмента, отправки не было', 'evidence': ''}
        box.update(key, 'mac', old['token'], receipt)
        box.retry_blocked('123456789012345', key)
        box.retry_blocked('123456789012345', key)
        new = box.claim('mac')
        self.assertNotEqual(new['token'], old['token'])
        self.assertFalse(box.update(key, 'mac', old['token'], receipt))
        with closing(box.connect()) as db:
            saved = db.execute('SELECT previous_result FROM outbound_attempt_history').fetchall()
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads(saved[0][0]), receipt)

    def test_invalid_recipient_legacy_and_other_tender_rejected(self):
        for address in ['a@example.org,b@example.org', 'a@example.org\r\nBcc:x@evil.org', '', 'abc']:
            with self.assertRaises(BuyerError): self.enqueue(address)
        with self.assertRaises(BuyerError): box.enqueue('999999999999999', 'draft1', 0, 'a@example.org')
        with patch.object(box.buyer_jobs, 'jobs', return_value=[{**JOB, 'payload': {'draft_task': {'schema_version': 1}}}]):
            with self.assertRaises(BuyerError): self.enqueue()

    def test_no_sent_without_evidence_and_no_tokens_in_user_response(self):
        first = self.enqueue(); job = box.claim('mac')
        with self.assertRaises(BuyerError):
            box.update(first, 'mac', job['token'], {'status': 'sent', 'detail': '', 'evidence': ''})
        for key in ('token', 'worker', 'lease_until'):
            self.assertNotIn(key, box.listing('123456789012345')[0])
        self.assertEqual(box.listing('123456789012345')[0]['body'], JOB['result']['drafts'][0]['body'])

    def test_changed_text_cannot_duplicate_active_or_sent_message(self):
        key = self.enqueue()
        altered = {'subject':'Щебень — наличие', 'body':'Добрый день! Есть щебень М1200 20–40 мм, 57,859 м³?'}
        self.assertEqual(box.enqueue('123456789012345','draft1',0,'sales@example.org',message=altered), key)
        claim = box.claim('mac')
        box.update(key,'mac',claim['token'],{'status':'sent','detail':'В отправленных','evidence':'snapshot hash'})
        self.assertEqual(box.enqueue('123456789012345','draft1',0,'sales@example.org',message=altered), key)

    def test_edited_message_is_validated_and_snapshotted(self):
        message = {'subject':'Щебень — наличие', 'body':'Добрый день! Есть щебень М1200 20–40 мм, 57,859 м³?'}
        key = box.enqueue('123456789012345','draft1',0,'sales@example.org',message=message)
        self.assertEqual(box.listing('123456789012345')[0]['body'], message['body'])
        with self.assertRaises(BuyerError):
            box.enqueue('123456789012345','draft1',0,'other@example.org',message={**message,'body':'Бюджет 40000 рублей'})

    def test_old_blocked_retry_cannot_duplicate_edited_message(self):
        old = self.enqueue(); claim = box.claim('mac')
        box.update(old,'mac',claim['token'],{'status':'blocked','detail':'Не отправлялось','evidence':''})
        edited = box.enqueue('123456789012345','draft1',0,'sales@example.org',message={
            'subject':'Новая формулировка запроса','body':'Добрый день! Нужен щебень М1200.'})
        self.assertNotEqual(old, edited)
        for status in ('queued','sending','sent','uncertain'):
            with closing(box.connect()) as db, db: db.execute('UPDATE outbound SET status=? WHERE id=?',(status,edited))
            with self.assertRaises(BuyerError):box.retry_blocked('123456789012345',old)
        self.assertEqual(box.listing('123456789012345')[0]['status'],'blocked')

    def test_http_access_and_worker_auth(self):
        app = Flask(__name__); app.register_blueprint(routes.blueprint); c = app.test_client()
        url = '/api/tenders/123456789012345/buyer/outbox'
        with patch.object(routes.crm_actor, 'resolve', side_effect=CorrectionError('Forbidden', 403)):
            self.assertEqual(c.post(url, json={}).status_code, 403)
        with patch.object(routes.crm_actor, 'resolve', return_value={'id': 1}):
            response = c.post(url, json={'draft_job_id': 'draft1', 'draft_index': 0, 'recipient': 'a@example.org', 'body': 'tampered'})
            self.assertEqual(response.status_code, 202)
        api = routes.WORKER_API + '/outbox/claim'
        self.assertEqual(c.post(api, json={'worker_id': 'mac'}).status_code, 401)
        with patch.dict('os.environ', {'BUYER_WORKER_TOKEN': 'x'*48}):
            data = c.post(api, json={'worker_id': 'mac'}, headers={'Authorization': 'Bearer '+'x'*48}).json['job']
            self.assertEqual(data['body'], JOB['result']['drafts'][0]['body'])

    def test_restart_does_not_rerun_agent_after_uncertain_start(self):
        job = {'id': 'q1', 'recipient': 'a@example.org', 'status': 'sending', 'token': 'token'}
        folder = self.path/'q1'/hashlib.sha256(b'token').hexdigest()[:24]; folder.mkdir(parents=True)
        (folder/'state.json').write_text('{"started_at":1}')
        with patch('autobot.buyer_sender.sender_client') as client:
            receipt = execute(job, {'outbox_dir': str(self.path)}, Mock())
            self.assertEqual(receipt['status'], 'uncertain')
            client.assert_not_called()
            self.assertEqual(execute(job, {'outbox_dir': str(self.path)}, Mock()), receipt)

    def test_receipt_requires_matching_recipient_and_local_artifact(self):
        job = {'id': 'q1', 'recipient': 'a@example.org'}
        data = {'job_id': 'q1', 'recipient': 'a@example.org', 'status': 'sent', 'detail': 'В отправленных', 'evidence': 'sent.txt'}
        (self.path/'receipt.json').write_text(json.dumps(data))
        self.assertEqual(read_receipt(self.path, job)['status'], 'uncertain')
        (self.path/'sent.txt').write_text('Actual sent-folder browser snapshot with recipient and subject')
        self.assertEqual(read_receipt(self.path, job)['status'], 'sent')
        data['recipient'] = 'different@example.org'
        (self.path/'receipt.json').write_text(json.dumps(data))
        self.assertEqual(read_receipt(self.path, job)['status'], 'uncertain')

    def test_ambiguous_api_post_is_never_repeated(self):
        job = {'id':'q2','recipient':'a@example.org','status':'sending','token':'token','subject':'Запрос','body':'Здравствуйте!'}
        client = Mock(); client.request.side_effect = BuyerError('lost response')
        config = {'outbox_dir':str(self.path),'sender_email':'buyer@example.org'}
        with patch('autobot.buyer_sender.sender_client', return_value=client):
            first = execute(job, config, Mock())
            self.assertEqual(first['status'], 'uncertain')
            self.assertEqual(execute(job, config, Mock()), first)
            self.assertEqual(client.request.call_count, 1)

    def test_api_restart_polls_saved_run_without_new_post(self):
        job = {'id':'q3','recipient':'a@example.org','status':'sending','token':'token'}
        folder = self.path/'q3'/hashlib.sha256(b'token').hexdigest()[:24];folder.mkdir(parents=True)
        (folder/'state.json').write_text(json.dumps({'run_id':'run_saved','started_at':time.time()}))
        (folder/'receipt.json').write_text(json.dumps({'job_id':'q3','recipient':'a@example.org','status':'blocked','detail':'Login required','evidence':''}))
        client = Mock();client.request.return_value={'run_id':'run_saved','status':'completed'}
        with patch('autobot.buyer_sender.sender_client', return_value=client):
            receipt = execute(job, {'outbox_dir':str(self.path)}, Mock())
        self.assertEqual(receipt['status'], 'blocked')
        client.request.assert_called_once_with('GET','/v1/runs/run_saved')
        client.release_events.assert_called_once_with('run_saved')

    def test_legacy_cli_attempt_is_not_restarted_by_api_upgrade(self):
        job = {'id':'q4','recipient':'a@example.org','status':'sending','token':'token','attempt_number':0}
        folder=self.path/'q4';folder.mkdir();(folder/'state.json').write_text('{"started_at":1}')
        with patch('autobot.buyer_sender.sender_client') as client:
            self.assertEqual(execute(job, {'outbox_dir':str(self.path)}, Mock())['status'], 'uncertain')
            client.assert_not_called()


if __name__ == '__main__': unittest.main()
