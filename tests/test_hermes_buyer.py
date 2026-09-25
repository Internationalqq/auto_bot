import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import requests

from autobot.hermes_buyer import BuyerError, DraftJournal, HermesClient, task_payload, validate_draft


SOURCE = {'tender_id': 'example', 'region': 'Ярославская область', 'positions': [
    {'position_key': 'cable', 'name': 'Кабель ВВГнг-LS 3х2,5', 'quantity': 120, 'unit': 'м', 'type_slug': 'material'},
    {'position_key': 'labour', 'name': 'Прокладка кабеля', 'quantity': 120, 'unit': 'м', 'type_slug': 'work'},
    {'position_key': 'sign', 'name': 'Знак дорожный', 'quantity': None, 'unit': 'шт', 'type_slug': 'product'}]}
DRAFT = {'drafts': [{'position_keys': [key], 'subject': 'Запрос цены', 'body': 'Просим сообщить цену и условия.'}
                    for key in ['cable', 'labour', 'sign']], 'questions': ['Уточните количество знаков']}


class HermesBuyerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'drafts.sqlite3'
        self.journal = DraftJournal(self.path)

    def fake(self):
        client = Mock()
        client.base_url = 'http://127.0.0.1:8642/p/autobot-buyer'
        client.request.return_value = {'run_id': 'run_test', 'status': 'started'}
        return client

    def test_all_positions_and_unknown_conditions_preserved(self):
        payload = task_payload(SOURCE)
        self.assertEqual(len(payload['positions']), 3)
        self.assertEqual(payload['positions'][0]['quantity'], 120)
        self.assertIsNone(payload['positions'][2]['quantity'])
        self.assertIsNone(payload['conditions'])
        self.assertEqual(payload['region'], SOURCE['region'])
        self.assertEqual(payload['mode'], 'draft_only')

    def test_duplicate_position_rejected(self):
        source = copy.deepcopy(SOURCE)
        source['positions'].append(source['positions'][0])
        with self.assertRaises(BuyerError):
            self.journal.enqueue(source)

    def test_nan_rejected(self):
        source = copy.deepcopy(SOURCE)
        source['positions'][0]['quantity'] = float('nan')
        with self.assertRaises(BuyerError):
            self.journal.enqueue(source)

    def test_concurrent_enqueue_returns_one_job(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(lambda _: self.journal.enqueue(SOURCE)['id'], range(12)))
        self.assertEqual(len(set(ids)), 1)

    def test_changed_quantity_is_a_new_task(self):
        first = self.journal.enqueue(SOURCE)
        source = copy.deepcopy(SOURCE)
        source['positions'][0]['quantity'] += 1
        self.assertNotEqual(first['id'], self.journal.enqueue(source)['id'])

    def test_preflight_failure_keeps_job_queued(self):
        job = self.journal.enqueue(SOURCE)
        client = self.fake()
        client.check.side_effect = BuyerError('Unavailable')
        with self.assertRaises(BuyerError):
            self.journal.advance(job['id'], client)
        self.assertEqual(self.journal.get(job['id'])['status'], 'queued')
        client.request.assert_not_called()

    def test_ambiguous_post_is_not_repeated_after_restart(self):
        job = self.journal.enqueue(SOURCE)
        client = self.fake()
        client.request.side_effect = BuyerError('Timeout')
        with self.assertRaises(BuyerError):
            self.journal.advance(job['id'], client)
        restarted = DraftJournal(self.path)
        result = restarted.advance(job['id'], client)
        self.assertEqual(result['status'], 'submission_uncertain')
        self.assertEqual(client.request.call_count, 1)

    def test_resume_polls_saved_run_and_keeps_draft_distinct_from_price(self):
        job = self.journal.enqueue(SOURCE)
        client = self.fake()
        self.journal.advance(job['id'], client)
        client.request.return_value = {'run_id': 'run_test', 'status': 'completed', 'output': json.dumps(DRAFT)}
        restarted = DraftJournal(self.path)
        result = restarted.advance(job['id'], client)
        self.assertEqual(result['status'], 'draft_ready')
        self.assertEqual(result['result'], DRAFT)
        client.request.assert_called_with('GET', '/v1/runs/run_test')
        count = client.request.call_count
        restarted.advance(job['id'], client)
        self.assertEqual(client.request.call_count, count)

    def test_poll_timeout_preserves_run(self):
        job = self.journal.enqueue(SOURCE)
        client = self.fake()
        self.journal.advance(job['id'], client)
        client.request.side_effect = BuyerError('Timeout')
        with self.assertRaises(BuyerError):
            self.journal.advance(job['id'], client)
        self.assertEqual(self.journal.get(job['id'])['run_id'], 'run_test')

    def test_missing_duplicate_or_invented_positions_rejected(self):
        for keys in [['cable'], ['cable', 'cable', 'sign'], ['cable', 'labour', 'invented']]:
            with self.subTest(keys=keys), self.assertRaises(BuyerError):
                validate_draft({'drafts': [{'position_keys': keys, 'subject': 'a', 'body': 'b'}], 'questions': []}, task_payload(SOURCE))

    def test_invalid_output_is_terminal_and_never_a_price(self):
        job = self.journal.enqueue(SOURCE)
        client = self.fake()
        self.journal.advance(job['id'], client)
        client.request.return_value = {'run_id': 'run_test', 'status': 'completed', 'output': '{"price": 123}'}
        with self.assertRaises(BuyerError):
            self.journal.advance(job['id'], client)
        self.assertEqual(self.journal.get(job['id'])['status'], 'invalid_result')
        self.assertIsNone(self.journal.get(job['id'])['result'])

    def test_interrupted_not_resubmitted(self):
        job = self.journal.enqueue(SOURCE)
        client = self.fake()
        self.journal.advance(job['id'], client)
        client.request.return_value = {'run_id': 'run_test', 'status': 'interrupted'}
        self.assertEqual(self.journal.advance(job['id'], client)['status'], 'interrupted')
        self.journal.advance(job['id'], client)
        self.assertEqual(client.request.call_count, 2)

    def test_wrong_run_rejected(self):
        job = self.journal.enqueue(SOURCE)
        client = self.fake()
        self.journal.advance(job['id'], client)
        client.request.return_value = {'run_id': 'different', 'status': 'completed', 'output': json.dumps(DRAFT)}
        with self.assertRaises(BuyerError):
            self.journal.advance(job['id'], client)

    def test_changed_endpoint_does_not_poll_another_agent(self):
        job = self.journal.enqueue(SOURCE)
        client = self.fake()
        self.journal.advance(job['id'], client)
        client.base_url = 'https://another/p/autobot-buyer'
        with self.assertRaises(BuyerError):
            self.journal.advance(job['id'], client)
        self.assertEqual(client.request.call_count, 1)


class HermesTransportTests(unittest.TestCase):
    def make_client(self, value, status=200):
        session = Mock()
        response = session.request.return_value
        response.status_code = status
        response.content = json.dumps(value).encode()
        response.json.return_value = value
        return HermesClient('http://127.0.0.1:8642/p/autobot-buyer', 'private-key', session), session

    def test_only_separate_profile_and_local_plain_http(self):
        for url in ['http://host/p/autobot-buyer', 'http://localhost', 'https://host/p/default',
                    'https://user:password@host/p/autobot-buyer', 'https://host/p/autobot-buyer?key=x']:
            with self.subTest(url=url), self.assertRaises(BuyerError):
                HermesClient(url, 'key')

    def test_tools_blocked_before_running(self):
        for tools in [[{'enabled': True, 'tools': ['send_message']}], [{'enabled': True, 'tools': ['terminal']}], {}]:
            client, _ = self.make_client(tools)
            with self.assertRaises(BuyerError):
                client.check()

    def test_disabled_tools_allow_drafts(self):
        client, session = self.make_client([{'enabled': False, 'tools': ['terminal']}])
        self.assertTrue(client.check()['ready_for_drafts'])
        kwargs = session.request.call_args.kwargs
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer private-key')
        self.assertFalse(session.trust_env)

    def test_errors_dont_echo_secrets(self):
        for status in [301, 401, 403, 404, 500]:
            client, _ = self.make_client({'error': 'private-key'}, status)
            with self.assertRaises(BuyerError) as error:
                client.check()
            self.assertNotIn('private-key', str(error.exception))
        client, session = self.make_client([])
        session.request.side_effect = requests.Timeout('private-key')
        with self.assertRaises(BuyerError) as error:
            client.check()
        self.assertNotIn('private-key', str(error.exception))

    def test_real_http_round_trip_in_isolated_journal(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value, code=200):
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def do_GET(self):
                if self.headers.get('Authorization') != 'Bearer test-key':
                    return self.reply({}, 401)
                if self.path.endswith('/toolsets'):
                    self.reply([])
                else:
                    self.reply({'run_id': 'run_http', 'status': 'completed', 'output': json.dumps(DRAFT)})

            def do_POST(self):
                calls.append((self.path, self.headers.get('Idempotency-Key'),
                              json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.reply({'run_id': 'run_http', 'status': 'started'}, 202)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                journal = DraftJournal(Path(folder) / 'jobs.sqlite3')
                client = HermesClient(f'http://127.0.0.1:{server.server_port}/p/autobot-buyer', 'test-key')
                job = journal.enqueue(SOURCE)
                self.assertEqual(journal.advance(job['id'], client)['status'], 'running')
                result = DraftJournal(journal.path).advance(job['id'], client)
                self.assertEqual(result['status'], 'draft_ready')
                self.assertEqual(result['result'], DRAFT)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][1], 'autobot-draft-' + job['id'])
                self.assertEqual(json.loads(calls[0][2]['input'])['region'], SOURCE['region'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
