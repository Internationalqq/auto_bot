import copy
from contextlib import closing, nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from flask import Flask
from autobot import buyer_jobs as jobs, buyer_routes as routes
from autobot.hermes_buyer import BuyerError, DraftJournal
from autobot.buyer_worker import Worker, LostLease, QueueClient
from autobot.uploaded_corrections import CorrectionError


SOURCE = {'tender_id': '123456789012345', 'region': 'Ярославская область', 'positions': [
    {'position_key': 'cable', 'name': 'ВВГнг-LS 3х2,5', 'quantity': 120, 'unit': 'м', 'section': 'Электрика', 'type_slug': 'material'},
    {'position_key': 'work', 'name': 'Прокладка кабеля', 'quantity': 120, 'unit': 'м', 'section': 'Электрика', 'type_slug': 'work'},
    {'position_key': 'sign', 'name': 'Знак 3.1', 'quantity': 6, 'unit': 'шт', 'section': 'Дорожные знаки', 'type_slug': 'product'},
]}


def result(payload):
    return {'drafts': [{'position_keys': [p['position_key'] for p in payload['positions']],
                        'subject': 'Запрос', 'body': 'Уточните стоимость и условия'}], 'questions': ['Доставка?']}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'queue.sqlite3'
        self.patch = patch.object(jobs, 'DB_PATH', self.db)
        self.patch.start(); self.addCleanup(self.patch.stop)
        outbox_patch = patch.object(routes.outbox, 'DB_PATH', Path(self.tmp.name) / 'outbox.sqlite3')
        outbox_patch.start(); self.addCleanup(outbox_patch.stop)

    def test_groups_separate_work_material_signs_and_repeat(self):
        ids = jobs.enqueue(SOURCE)
        self.assertEqual(len(ids), 3)
        self.assertEqual(ids, jobs.enqueue(SOURCE))
        records = jobs.jobs(SOURCE['tender_id'])
        self.assertEqual(len(records), 3)
        for item in records:
            self.assertEqual(item['payload']['draft_task']['region'], SOURCE['region'])
            self.assertIsNone(item['payload']['draft_task']['conditions'])
        self.assertEqual({p['position_key'] for j in records for p in j['payload']['draft_task']['positions']}, {'cable', 'work', 'sign'})

    def test_bounded_groups(self):
        source = copy.deepcopy(SOURCE)
        source['positions'] = [dict(source['positions'][0], position_key=str(i)) for i in range(61)]
        jobs.enqueue(source)
        self.assertEqual(sorted(len(j['payload']['draft_task']['positions']) for j in jobs.jobs(source['tender_id'])), [11, 25, 25])

    def test_result_validation_fencing_and_idempotent_delivery(self):
        jobs.enqueue(SOURCE)
        first = jobs.claim('mac')
        payload = first['payload']['draft_task']
        args = (first['id'], 'mac', first['lease_token'])
        self.assertFalse(jobs.complete(first['id'], 'mac', '', result(payload)))
        self.assertFalse(jobs.complete(first['id'], 'other', first['lease_token'], result(payload)))
        with self.assertRaises(BuyerError):
            jobs.complete(*args, {'drafts': [], 'questions': []})
        self.assertTrue(jobs.complete(*args, result(payload)))
        self.assertTrue(jobs.complete(*args, result(payload)))
        changed = result(payload); changed['questions'] = []
        self.assertFalse(jobs.complete(*args, changed))
        self.assertEqual(len(jobs.enqueue(SOURCE)), 3)
        self.assertEqual(len(jobs.jobs(SOURCE['tender_id'])), 3)

    def test_expired_attempt_and_cancel_cannot_publish(self):
        jobs.enqueue(SOURCE)
        job = jobs.claim('mac')
        with closing(jobs.queue._connect(self.db)) as db:
            db.execute('UPDATE agent_market_jobs SET lease_until=0 WHERE id=?', (job['id'],))
        self.assertFalse(jobs.complete(job['id'], 'mac', job['lease_token'], result(job['payload']['draft_task'])))
        jobs.cancel(SOURCE['tender_id'])
        self.assertFalse(jobs.heartbeat(job['id'], 'mac', job['lease_token']))
        self.assertFalse(jobs.complete(job['id'], 'mac', job['lease_token'], result(job['payload']['draft_task'])))
        self.assertIsNone(jobs.claim('mac'))

    def test_route_authentication_redaction_and_queue(self):
        app = Flask(__name__); app.register_blueprint(routes.blueprint)
        client = app.test_client()
        url = '/api/tenders/' + SOURCE['tender_id'] + '/buyer/jobs'
        with patch.object(routes.crm_actor, 'resolve', side_effect=CorrectionError('Нет доступа', 403)):
            self.assertEqual(client.get(url).status_code, 403)
            self.assertEqual(client.post(url, json={}).status_code, 403)
        with patch.dict('os.environ', {'BUYER_WORKER_TOKEN': 'a' * 48}):
            api = routes.WORKER_API
            self.assertEqual(client.post(api + '/claim', json={'worker_id': 'mac'}).status_code, 401)
            headers = {'Authorization': 'Bearer ' + 'a' * 48}
            jobs.enqueue(SOURCE)
            claimed = client.post(api + '/claim', json={'worker_id': 'mac'}, headers=headers).json['job']
            body = {'worker_id': 'mac', 'lease_token': claimed['lease_token'], 'result': result(claimed['payload']['draft_task'])}
            self.assertEqual(client.post(api + '/jobs/' + claimed['id'] + '/complete', json=body, headers=headers).status_code, 200)
        with patch.object(routes.crm_actor, 'resolve', return_value={'id': 1}):
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('lease_token', response.text)
            self.assertNotIn('worker_id', response.text)

    def test_route_uses_authoritative_positions_and_excludes_verified(self):
        app = Flask(__name__); app.register_blueprint(routes.blueprint)
        client = app.test_client(); tid = SOURCE['tender_id']
        reports = Path(self.tmp.name)
        (reports / f'ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx').touch()
        source = copy.deepcopy(SOURCE); source['positions'][0]['verified_count'] = 1
        source['positions'][1]['price_state'] = 'excluded'
        web = SimpleNamespace(REPORTS_DIR=reports, load_tender_metadata=lambda: {}, build_tender_detail=lambda *a: source)
        with patch.object(routes.crm_actor, 'resolve', return_value={'id': 1}), patch.object(routes, 'consistent_report', return_value=nullcontext()), patch.dict('sys.modules', {'autobot.web_ui': web}):
            url = '/api/tenders/' + tid + '/buyer/jobs'
            self.assertEqual(client.post(url, json={'action': 'oops'}).status_code, 400)
            response = client.post(url, json={})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json['position_count'], 1)
            payload = jobs.jobs(tid)[0]['payload']['draft_task']
            self.assertEqual(payload['positions'][0]['position_key'], 'sign')
            self.assertEqual(client.post(url, json={'position_keys': ['unknown']}).status_code, 400)


class FakeHermes:
    base_url = 'http://127.0.0.1:8644'
    def __init__(self): self.posts = 0
    def check(self): pass
    def release_events(self, run_id): self.released = run_id
    def request(self, method, path, **kwargs):
        if method == 'POST':
            self.posts += 1
            self.payload = json.loads(kwargs['json']['input'])
            return {'run_id': 'run_1'}
        return {'run_id': 'run_1', 'status': 'completed', 'output': result(self.payload)}


class FakeQueue:
    def __init__(self):
        self.job = {'id': 'q1', 'lease_token': 'lease1', 'payload': {'draft_task': SOURCE}}
        self.done = []; self.lost = False; self.fail_ack = False
    def request(self, path, **data):
        if path == '/claim': return {'job': self.job}
        if self.lost: raise LostLease('cancelled')
        if path.endswith('/complete'):
            self.done.append(data['result'])
            if self.fail_ack:
                self.fail_ack = False
                raise BuyerError('lost ACK')
        return {'ok': True}


class WorkerTests(unittest.TestCase):
    def test_restart_reuses_run_and_lost_ack_reuses_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = DraftJournal(Path(tmp) / 'journal.db')
            remote, hermes = FakeQueue(), FakeHermes()
            self.assertEqual(Worker(remote, hermes, journal).step(), 'running')
            worker = Worker(remote, hermes, journal)
            remote.fail_ack = True
            with self.assertRaises(BuyerError): worker.step()
            self.assertEqual(worker.step(), 'completed')
            self.assertEqual(hermes.posts, 1)
            self.assertEqual(hermes.released, 'run_1')
            self.assertEqual(remote.done[0], remote.done[1])

    def test_cancel_does_not_submit_or_deliver(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, hermes = FakeQueue(), FakeHermes(); remote.lost = True
            worker = Worker(remote, hermes, DraftJournal(Path(tmp) / 'journal.db'))
            self.assertEqual(worker.step(), 'lease_lost')
            self.assertEqual(hermes.posts, 0)
            self.assertEqual(remote.done, [])

    def test_unsafe_queue_urls_rejected(self):
        for url in ('http://host/autobot/api/agent-market/v1/buyer', 'https://user:password@host/autobot/api/agent-market/v1/buyer', 'https://host/other'):
            with self.assertRaises(BuyerError): QueueClient(url, 'a' * 48, 'mac')


if __name__ == '__main__': unittest.main()
