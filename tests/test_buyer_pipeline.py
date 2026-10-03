from contextlib import closing
import copy
import json
import sqlite3
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from flask import Flask
from autobot import buyer_pipeline as pipeline, buyer_store, buyer_jobs, buyer_outbox, buyer_replies, buyer_suppliers, buyer_routes
from autobot.buyer_reply_text import prices, exact_identity_reason

TID = '0171200001926000664'
REGION = 'Ярославская область'


def row(key='c', name='Кабель ВВГнг 3х2,5', **extra):
    return dict(position_key=key, name=name, quantity=10, unit='м', type_slug='material', **extra)


def source(rows):
    return dict(tender_id=TID, region=REGION, positions=rows)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for target, name in [(buyer_jobs, 'jobs.db'), (buyer_outbox, 'outbox.db')]:
            p = patch.object(target, 'DB_PATH', root / name)
            p.start(); self.addCleanup(p.stop)
        self.rows = [row(), row('s', 'Светильник 40 Вт') | {'unit': 'шт'}, row('reserve', 'Непредвиденные затраты') | {'type_slug': 'aggregate', 'price_state': 'excluded'}]

    def sent(self):
        buyer_suppliers.prepare(source(self.rows[:2]))
        job = next(j for j in buyer_jobs.jobs(TID) if j['payload']['draft_task']['supplier']['id'] == 'technolight')
        key = buyer_outbox.enqueue(TID, job['id'], 0, 'info@tl-electro.ru')
        claim = buyer_outbox.claim('test')
        buyer_outbox.update(key, 'test', claim['token'], {'status': 'sent', 'detail': 'Test', 'evidence': 'receipt-test'})
        return job, key

    def project(self):
        return pipeline.project(TID, REGION, self.rows, drafts=buyer_jobs.jobs(TID), **pipeline.stored(TID))

    def reply(self, key, text, mid='reply-1'):
        buyer_replies.request_check(TID, key)
        claim = buyer_replies.claim('test-inbox')
        parsed = prices(text, claim['positions'])
        result = {'status': 'checked', 'messages': [{'sender': 'info@tl-electro.ru', 'subject': 'Re: ' + claim['subject'],
            'message_id': mid, 'text': text, 'received_at': time.time(), 'evidence': 'test-only', 'prices': parsed}]}
        self.assertTrue(buyer_replies.update(key, 'test-inbox', claim['token'], result))
        return parsed

    def test_no_database_created_on_snapshot_and_mail_read(self):
        self.assertFalse(buyer_outbox.DB_PATH.exists())
        self.assertEqual(pipeline.stored(TID)['runs'], [])
        self.assertEqual(pipeline.stored_drafts(TID), [])
        self.assertEqual(pipeline.mail_status()['state'], 'unknown')
        self.assertFalse(buyer_outbox.DB_PATH.exists())
        self.assertFalse(buyer_jobs.DB_PATH.exists())

    def test_readonly_drafts_preserve_old_correspondence_beyond_queue_page(self):
        job, _ = self.sent()
        with closing(sqlite3.connect(buyer_jobs.DB_PATH)) as db, db:
            columns = [r[1] for r in db.execute('PRAGMA table_info(agent_market_jobs)')]
            record = dict(zip(columns, db.execute('SELECT * FROM agent_market_jobs WHERE id=?', (job['id'],)).fetchone()))
            for number in range(251):
                duplicate = {**record, 'id': 'later-' + str(number), 'position_key': 'later-' + str(number)}
                db.execute('INSERT INTO agent_market_jobs (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                           [duplicate[c] for c in columns])
        before = buyer_jobs.DB_PATH.read_bytes()
        saved = pipeline.stored_drafts(TID)
        self.assertGreater(len(saved), 250)
        self.assertIn(job['id'], {j['id'] for j in saved})
        self.assertEqual(pipeline.stored_drafts('999999999'), [])
        self.assertEqual(buyer_jobs.DB_PATH.read_bytes(), before)

    def test_site_candidate_and_verified_price_are_distinct_no_money_is_changed(self):
        self.rows[0].update(estimate_unit=999, sources=[{'url': 'https://example.org/cable', 'price': 76, 'comparison_price': 76, 'verified': False, 'reason': 'Другой размер'}])
        self.rows[1].update(verified_count=1, sources=[{'url': 'https://example.org/lamp', 'comparison_price': 120, 'verified': True}])
        before = copy.deepcopy(self.rows)
        result = pipeline.project(TID, REGION, self.rows)
        self.assertEqual(result['coverage']['denominator'], 2)
        self.assertEqual(result['coverage']['comparable'], 50.0)
        self.assertEqual(result['coverage']['confirmed'], 0.0)
        self.assertEqual(result['summary'], {'total': 3, 'eligible': 2, 'excluded': 1, 'candidates': 2, 'comparable': 1, 'contacts': 0, 'sent': 0, 'replied': 0, 'confirmed': 0})
        self.assertEqual(self.rows, before)
        self.assertEqual(result['positions'][0]['label'], 'Есть варианты')
        self.assertEqual(result['positions'][1]['offers'][0]['price_kopecks'], 12000)

    def test_discovery_keeps_exact_units_and_stale_results_out(self):
        payload = buyer_store.snapshot(source(self.rows[:2])) | {'discovery_version': buyer_store.DISCOVERY_VERSION}
        company = {'id': 'a', 'company': 'Test', 'url': 'https://example.org', 'email': 'a@example.org', 'position_keys': ['c'], 'prices': [
            {'position_key': 'c', 'price_kopecks': 12300, 'unit': 'шт', 'source_url': 'https://example.org', 'evidence': self.rows[0]['name'], 'state': 'published'}]}
        run = {'payload': payload, 'status': 'completed', 'updated_at': 100, 'candidates': [company]}
        data = pipeline.project(TID, REGION, self.rows, runs=[run])
        self.assertEqual(data['summary']['contacts'], 1)
        self.assertEqual(data['summary']['comparable'], 0)
        self.assertIn('Единица', data['positions'][0]['offers'][0]['reason'])
        self.rows[0]['quantity'] = 20
        stale = pipeline.project(TID, REGION, self.rows, runs=[run])
        self.assertEqual(stale['summary']['contacts'], 0)
        self.assertEqual(stale['positions'][0]['state'], 'stale')
        self.assertEqual(pipeline.project(TID, 'Москва', self.rows, runs=[run])['summary']['candidates'], 0)

    def test_queue_uncertain_and_sent_are_not_the_same(self):
        job, key = self.sent()
        for status, count in [('queued', 0), ('uncertain', 0), ('sent', 2)]:
            with closing(buyer_outbox.connect()) as db, db:
                db.execute('UPDATE outbound SET status=? WHERE id=?', (status, key))
            self.assertEqual(self.project()['summary']['sent'], count)

    def test_campaign_contacts_follow_selected_draft_not_whole_job(self):
        job = {'id': 'j', 'payload': {'draft_task': buyer_store.snapshot(source(self.rows[:2]))},
               'result': {'drafts': [{'position_keys': ['c']}, {'position_keys': ['s']}]}}
        campaign = {'draft_job_id': 'j', 'draft_index': 0, 'contacts': [
            {'company': 'Cable supplier', 'contact': 'c@example.org', 'channel': 'email',
             'source_url': 'https://example.org', 'checked_at': 100}]}
        result = pipeline.project(TID, REGION, self.rows, drafts=[job], campaigns=[campaign])
        self.assertEqual(result['summary']['contacts'], 1)
        self.assertEqual(result['positions'][0]['contacts'][0]['address'], 'c@example.org')
        self.assertEqual(result['positions'][1]['contacts'], [])
        campaign['draft_index'] = 9
        result = pipeline.project(TID, REGION, self.rows, drafts=[job], campaigns=[campaign])
        self.assertEqual(result['summary']['contacts'], 0)

    def test_saved_directory_does_not_count_as_supplier_evidence(self):
        run={'payload':buyer_store.snapshot(source(self.rows)) | {'discovery_version':buyer_store.DISCOVERY_VERSION},
             'status':'completed','candidates':[{'company':'Directory','url':'https://stroyka-ms.ru/suppliers/',
                'email':'support@portal.example','position_keys':['c'],'prices':[]}]}
        result=pipeline.project(TID,REGION,self.rows,runs=[run])
        self.assertEqual(result['summary']['contacts'],0)
        self.assertFalse(result['positions'][0]['flags']['candidates'])

    def test_question_reply_is_not_a_confirmed_price(self):
        _, key = self.sent()
        self.reply(key, 'Уточните адрес доставки, пожалуйста.')
        result = self.project()
        self.assertEqual(result['summary']['replied'], 2)
        self.assertEqual(result['summary']['confirmed'], 0)
        self.assertEqual(result['positions'][0]['state'], 'replied')

    def test_sent_followup_returns_to_waiting_without_losing_received_reply_count(self):
        _, key = self.sent()
        self.reply(key, 'Уточните адрес доставки, пожалуйста.')
        with closing(buyer_outbox.connect()) as db, db:
            db.execute('UPDATE outbound SET updated_at=? WHERE id=?', (time.time() + 10, key))
        result = self.project()
        self.assertEqual(result['summary']['replied'], 2)
        self.assertEqual(result['positions'][0]['state'], 'sent')
        self.assertIn('уточнение', result['positions'][0]['reason'])

    def test_exact_partial_reply_roundtrip_duplicate_and_reopen(self):
        _, key = self.sent()
        text = '1. Кабель ВВГнг 3х2,5 — 120,50 руб/м, с НДС, в наличии.'
        self.assertTrue(self.reply(key, text)[0]['exact_match'])
        self.reply(key, text)
        result = self.project()
        self.assertEqual(result['summary']['confirmed'], 1)
        self.assertEqual(result['positions'][0]['offers'][0]['price_kopecks'], 12050)
        self.assertEqual(len(pipeline.stored(TID)['replies']), 1)
        self.assertEqual(self.project(), result)
        self.rows[0]['quantity'] = 11
        self.assertEqual(self.project()['summary']['confirmed'], 0)
        self.assertEqual(pipeline.stored('999999999')['replies'], [])

    def test_latest_ambiguous_offer_does_not_reuse_earlier_confirmed_quote(self):
        _, key = self.sent()
        self.reply(key, '1. Кабель ВВГнг 3х2,5 — 120 руб/м, с НДС')
        self.reply(key, '1. Кабель ВВГнг 3х2,5 — ориентировочно 110 руб/м, с НДС', 'reply-2')
        result = self.project()
        self.assertEqual(result['summary']['confirmed'], 0)
        self.assertEqual(len(result['positions'][0]['offers']), 1)

    def test_scaled_unit_quote_converts_only_matching_measure(self):
        self.assertEqual(buyer_replies.comparison_amount(12345, 'м', '100 м'), 1234500)
        self.assertIsNone(buyer_replies.comparison_amount(12345, 'шт', 'м2'))

    def test_short_ruble_currency_roundtrip_keeps_server_validation(self):
        target = row(name='Стеклошарики 100–600 мкм') | {'unit': 'кг'}
        for currency in ('р', 'р.', 'руб.', '₽', 'RUB'):
            with self.subTest(currency=currency):
                quote = target['name'] + ' — 74,50 ' + currency + '/кг, с НДС'
                parsed = prices(quote, [target])
                self.assertEqual(len(parsed), 1)
                values = buyer_replies.parse_price(parsed[0], target, quote)
                self.assertEqual(values[1], 7450)
                self.assertEqual(values[-2], 'comparable')

    def test_short_ruble_currency_does_not_confirm_alternatives_or_unknown_units(self):
        target = row(name='Стеклошарики 100–600 мкм') | {'unit': 'кг'}
        for quote in ('Стеклошарики 106–600 мкм — 74 р/кг, с НДС',
                      target['name'] + ' — 74 р/шт, с НДС',
                      target['name'] + ' — 74 р/кг',
                      target['name'] + ' — от 74 р/кг, с НДС'):
            with self.subTest(quote=quote):
                parsed = prices(quote, [target])
                self.assertEqual(len(parsed), 1)
                self.assertEqual(buyer_replies.parse_price(parsed[0], target, quote)[-2], 'review')
        for quote in (target['name'] + ' — 74 p/кг, с НДС',
                      target['name'] + ' — 74 р/месяц, с НДС',
                      target['name'] + ' — 74 р/кг или 80 р/кг, с НДС',
                      'Уточните адрес\nFrom: purchaser\n' + target['name'] + ' — 74 р/кг, с НДС'):
            with self.subTest(quote=quote):
                self.assertEqual(prices(quote, [target]), [])

    def test_server_does_not_trust_exact_match_flag_or_quoted_customer_text(self):
        target = row(name='Кабель ВВГнг 4х150')
        for quote in ['Кабель ВВГнг 4х15 — 120 руб/м, с НДС', 'Кабель ВВГнг 4х150: замена 4х70 — 120 руб/м, с НДС']:
            values = buyer_replies.parse_price({'quote': quote, 'price': '120', 'unit': 'м', 'vat': 'с НДС', 'exact_match': True}, target, quote)
            self.assertEqual(values[-2], 'review')
        quote = 'Кабель ВВГнг 4х150 — 120 руб/м, с НДС'
        values = buyer_replies.parse_price({'quote': quote, 'price': '120', 'unit': 'м', 'vat': 'с НДС', 'exact_match': True}, target, 'Уточните адрес\nFrom: purchaser\n' + quote)
        self.assertEqual(values[-2], 'review')

    def test_quantity_or_second_price_cannot_be_accepted_as_unit_price(self):
        target = row()
        for quote in [target['name'] + ' — 10 м по 120 руб/м, с НДС', target['name'] + ' — 120 руб/м или 130 руб/м, с НДС']:
            for amount in ('10', '130'):
                values = buyer_replies.parse_price({'quote': quote, 'price': amount, 'unit': 'м', 'vat': 'с НДС'}, target, quote)
                self.assertEqual(values[-2], 'review')

    def test_decimal_dimensions_and_missing_extra_requirements_do_not_match(self):
        self.assertTrue(exact_identity_reason(row(name='Кабель ВВГнг 4х1,5'), 'Кабель ВВГнг 4х15 — 120 руб/м'))
        target = row(requirements={'specifications': [{'label': 'Высота', 'evidence': 'высота 100 мм'}]})
        self.assertIn('Высота', exact_identity_reason(target, target['name'] + ' — 120 руб/м'))

    def test_tender_requirements_and_immutable_snapshot_have_same_identity(self):
        target = row(requirements={'specifications': [{'kind': 'dimensions', 'label': 'Сечение', 'value': '3х2,5', 'evidence': '3х2,5'}]})
        saved = buyer_store.snapshot(source([{**target, 'specification': {'requirements': target['requirements']}}]))['positions'][0]
        self.assertEqual(pipeline.need(target, TID, REGION), pipeline.need(saved, TID, REGION))
        saved['specification']['requirements']['specifications'][0]['value'] = '4х150'
        self.assertNotEqual(pipeline.need(target, TID, REGION), pipeline.need(saved, TID, REGION))
        self.assertTrue(exact_identity_reason(saved, target['name'] + ' — 120 руб/м, с НДС'))

    def test_mail_status_detects_pause_failure_and_does_not_expose_error_text(self):
        path = Path(self.tmp.name) / 'status.json'
        for state, expected in [({'checked_at': 100, 'ok': True}, 'ready'), ({'checked_at': 100, 'ok': False, 'detail': 'secret'}, 'blocked'), ({'checked_at': 1, 'ok': True}, 'offline')]:
            path.write_text(json.dumps(state))
            result = pipeline.mail_status(path, now=1850)
            self.assertEqual(result['state'], expected)
            self.assertNotIn('secret', str(result))

    def test_receiving_only_does_not_claim_sending_is_connected(self):
        path=Path(self.tmp.name)/'status.json'
        path.write_text(json.dumps({'checked_at':100,'ok':True,'receiving':True,'sending':False,'send_detail':'secret'}))
        result=pipeline.mail_status(path,now=200)
        self.assertEqual(result['state'],'receiving')
        self.assertIn('отправка приостановлена',result['label'])
        self.assertNotIn('secret',str(result))

    def test_endpoint_requires_current_crm_actor_and_calls_tender_scoped_build(self):
        from autobot.uploaded_corrections import CorrectionError
        app = Flask(__name__)
        with patch('autobot.buyer_discovery.start_worker'), patch('autobot.buyer_campaigns.launch'):
            app.register_blueprint(buyer_routes.blueprint)
        with patch.object(buyer_routes.crm_actor, 'resolve', side_effect=CorrectionError('Войдите', 401)), patch.object(pipeline, 'build') as build:
            self.assertEqual(app.test_client().get(f'/api/tenders/{TID}/buyer/pipeline').status_code, 401)
            build.assert_not_called()
        with patch.object(buyer_routes.crm_actor, 'resolve', return_value={'id': 7}), patch.object(pipeline, 'build', return_value={'positions': []}) as build:
            response = app.test_client().get(f'/api/tenders/{TID}/buyer/pipeline')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
            build.assert_called_once_with(TID)


if __name__ == '__main__':
    unittest.main()
