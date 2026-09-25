from contextlib import closing
import copy
import json
import hashlib
from pathlib import Path
import tempfile
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
        for key in ('token', 'worker', 'lease_until', 'body'):
            self.assertNotIn(key, box.listing('123456789012345')[0])

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
        with patch('autobot.buyer_sender.subprocess.Popen') as popen:
            receipt = execute(job, {'outbox_dir': str(self.path)}, Mock())
            self.assertEqual(receipt['status'], 'uncertain')
            popen.assert_not_called()
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


if __name__ == '__main__': unittest.main()
