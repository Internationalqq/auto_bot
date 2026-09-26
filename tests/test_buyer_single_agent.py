import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from autobot.buyer_worker import Worker, draft_client
from autobot.hermes_buyer import BuyerError, DraftJournal


SOURCE = {'tender_id': '0171200001926000664', 'region': 'Ярославская область',
          'budget': 912345, 'positions': [
    {'position_key': '0171200001926000664:1', 'name': 'Кабель ВВГнг-LS 3х2,5',
     'quantity': 10.4, 'unit': '100 м', 'type_slug': 'material', 'price': 912345}]}


class SingleBuyerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'journal.db'
        self.journal = DraftJournal(self.path)

    def test_template_never_reads_credentials_or_creates_second_agent(self):
        with patch('autobot.buyer_worker.HermesClient') as client, patch.object(Path, 'read_text') as read:
            self.assertIsNone(draft_client({'draft_mode': 'template'}))
        client.assert_not_called(); read.assert_not_called()
        with self.assertRaises(BuyerError): draft_client({'draft_mode': 'typo'})

    def test_local_roundtrip_exact_quantity_and_no_internal_prices(self):
        job = self.journal.enqueue(SOURCE)
        ready = self.journal.prepare_locally(job['id'])
        body = ready['result']['drafts'][0]['body']
        self.assertIn('10,4 × 100 м', body)
        self.assertIn(SOURCE['region'], body)
        for secret in ('912345', SOURCE['tender_id']): self.assertNotIn(secret, body)
        self.assertIsNone(ready['run_id'])
        self.assertEqual(DraftJournal(self.path).prepare_locally(job['id']), ready)

    def test_missing_data_stays_unknown_and_every_row_is_preserved(self):
        source = copy.deepcopy(SOURCE); source['region'] = None
        source['positions'][0].update(quantity=None, unit=None)
        job = self.journal.enqueue(source)
        ready = self.journal.prepare_locally(job['id'])['result']
        self.assertEqual(ready['drafts'][0]['position_keys'], [source['positions'][0]['position_key']])
        self.assertEqual(len(ready['questions']), 3)
        self.assertIn('количество уточняется', ready['drafts'][0]['body'])
        self.assertNotIn('None', ready['drafts'][0]['body'])

    def test_pending_agent_run_cannot_be_regenerated(self):
        job = self.journal.enqueue(SOURCE)
        for status in ('running', 'submission_uncertain'):
            with self.journal.connect() as db:
                db.execute('update buyer_drafts set status=?,run_id=? where id=?', (status, 'old-run', job['id']))
            db.close()
            with self.assertRaises(BuyerError): self.journal.prepare_locally(job['id'])
            self.assertEqual(self.journal.get(job['id'])['run_id'], 'old-run')

    def test_reviewed_agent_draft_is_preserved(self):
        job = self.journal.enqueue(SOURCE)
        ready = self.journal.prepare_locally(job['id'])
        with self.journal.connect() as db:
            db.execute("update buyer_drafts set run_id='old-run' where id=?", (job['id'],))
        db.close()
        with patch('autobot.buyer_drafts.draft', side_effect=AssertionError('Do not regenerate')):
            again = self.journal.prepare_locally(job['id'])
        self.assertEqual(again['result'], ready['result'])
        self.assertEqual(again['run_id'], 'old-run')

    def test_invalid_volume_does_not_loop_forever(self):
        source = copy.deepcopy(SOURCE); source['positions'][0]['quantity'] = -4
        job = self.journal.enqueue(source)
        with self.assertRaises(BuyerError): self.journal.prepare_locally(job['id'])
        self.assertEqual(self.journal.get(job['id'])['status'], 'invalid_result')

    def test_lost_ack_and_restart_reuse_same_draft_without_ai(self):
        remote = Mock()
        claim = {'id':'q1', 'lease_token':'lease', 'payload':{'draft_task':SOURCE}}
        remote.request.side_effect = [ {'job':claim}, {}, BuyerError('lost ack') ]
        worker = Worker(remote, None, self.journal)
        with self.assertRaises(BuyerError): worker.step()
        first = remote.request.call_args.kwargs['result']
        remote.request.side_effect = [{'job':claim}, {}, {'ok':True}]
        restarted = Worker(remote, None, DraftJournal(self.path))
        self.assertEqual(restarted.step(), 'completed')
        self.assertEqual(remote.request.call_args.kwargs['result'], first)


if __name__ == '__main__': unittest.main()
