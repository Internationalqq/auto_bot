from contextlib import closing
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from flask import Flask

from autobot import buyer_suppliers as suppliers, buyer_jobs as jobs, buyer_outbox as box
from autobot import buyer_replies as replies, buyer_campaigns as campaigns, buyer_routes as routes
from autobot.hermes_buyer import BuyerError

TID='123456789012345'


def row(key,name,section='Раздел 1',unit='м',kind='material'):
    return dict(position_key=key,name=name,section=section,quantity=10,unit=unit,type_slug=kind,estimate_unit=12345678)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        for p in [patch.object(jobs,'DB_PATH',root/'jobs.db'),patch.object(box,'DB_PATH',root/'outbox.db'),patch.object(campaigns,'launch')]:
            p.start();self.addCleanup(p.stop)
        self.source=dict(tender_id=TID,region='Ярославская область',positions=[row('c','Кабель ВВГнг 3х2,5'),row('l','Светильник 40 Вт','Раздел 8','шт'),row('s','Знак дорожный 1.23','Раздел 3','шт'),row('w','Монтаж кабеля',kind='work')])

    def prepared(self):
        suppliers.prepare(self.source)
        return next(j for j in jobs.jobs(TID) if j['payload']['draft_task']['supplier']['id']=='technolight')

    def sent(self):
        j=self.prepared();key=box.enqueue(TID,j['id'],0,'info@tl-electro.ru')
        job=box.claim('mac');box.update(key,'mac',job['token'],dict(status='sent',detail='Проверено',evidence='sent.txt sha256:example'))
        replies.request_check(TID,key)
        return key,replies.claim('reader')

    def result(self,**price):
        q=dict(line=1,price='120,50',unit='м',vat='с НДС',availability='в наличии',delivery='',exact_match=True,quote='Кабель ВВГнг 3х2,5 — 120,50 руб за м, с НДС, в наличии.')
        q.update(price)
        return dict(status='checked',detail='',messages=[dict(message_id='mail-1',sender='info@tl-electro.ru',received_at=time.time(),text='Кабель ВВГнг 3х2,5 — 120,50 руб за м, с НДС, в наличии.',evidence='sender, subject and message view captured',prices=[q])])

    def test_supplier_groups_cross_sections_preserve_keys_and_accounting_is_private(self):
        result=suppliers.prepare(self.source)
        self.assertEqual(result['position_count'],3)
        self.assertEqual([p['position_key'] for p in result['uncovered']],['w'])
        j=self.prepared();payload=j['payload']['draft_task']
        self.assertEqual([p['position_key'] for p in payload['positions']],['c','l'])
        self.assertNotIn('12345678',str(j['result']))
        self.assertNotIn('Раздел',j['result']['drafts'][0]['body'])
        self.assertEqual(len(j['result']['drafts']),1)

    def test_preparation_repeat_and_reopen_are_idempotent(self):
        a=suppliers.prepare(self.source);b=suppliers.prepare(copy.deepcopy(self.source))
        self.assertEqual(a['job_ids'],b['job_ids']);self.assertEqual(len(jobs.jobs(TID)),3)
        self.assertEqual(suppliers.coverage(TID),b)

    def test_uncovered_positions_survive_api_reopen(self):
        expected=suppliers.prepare(self.source)
        app=Flask(__name__);app.register_blueprint(routes.blueprint)
        with patch.object(routes.crm_actor,'resolve',return_value={'id':7}):
            actual=app.test_client().get('/api/tenders/'+TID+'/buyer/jobs').get_json()
        self.assertEqual(actual['coverage'],expected)
        self.assertEqual(actual['coverage']['uncovered'][0]['position_key'],'w')

    def test_missing_quantities_other_region_and_works_not_sent(self):
        with self.assertRaises(BuyerError): suppliers.prepare({**self.source,'region':'Москва'})
        self.source['positions'][0]['quantity']=0
        result=suppliers.prepare(self.source)
        self.assertIn('положительное',next(p['reason'] for p in result['uncovered'] if p['position_key']=='c'))
        self.assertIsNone(suppliers.category(row('w','Кабель монтаж',kind='work')))

    def test_scaled_units_are_not_silently_shrunk(self):
        self.source['positions']=[row('c','Кабель ВВГнг',unit='100 м')]
        j=self.prepared();self.assertIn('10 × 100 м',j['result']['drafts'][0]['body'])

    def test_contact_rechecked_only_for_target_supplier(self):
        j=self.prepared();key=campaigns.start(TID,j['id'],0)
        with patch.object(campaigns,'fetch_contact',return_value='info@tl-electro.ru') as fetch:
            campaigns.run_one();self.assertEqual(fetch.call_count,1)
        self.assertEqual(len(campaigns.listing(TID)[0]['contacts']),1)
        self.assertEqual(box.listing(TID)[0]['recipient'],'info@tl-electro.ru')

    def test_expanded_registry_requests_only_new_rows_for_existing_contact(self):
        j=self.prepared();box.enqueue(TID,j['id'],0,'info@tl-electro.ru')
        self.source['positions'].append(row('new','Кабель АВВГ'))
        ids=suppliers.prepare(self.source)['job_ids']
        self.assertIn(j['id'],ids)
        newer=next(item for item in jobs.jobs(TID) if item['id'] in ids and item['id']!=j['id'] and item['payload']['draft_task']['supplier']['id']=='technolight')
        self.assertEqual(newer['result']['drafts'][0]['position_keys'],['new'])
        box.enqueue(TID,newer['id'],0,'info@tl-electro.ru')
        again=suppliers.prepare(self.source)
        self.assertEqual(again['position_count'],4)
        self.assertEqual(len(box.listing(TID)),2)

    def test_materials_and_work_route_separately_and_exact_brands_are_preserved(self):
        cases=[('Кабель ВВГнг','material','cable'),('Кабель до 35 кВ','work','electrical_work'),
               ('Измерение сопротивления изоляции','work','electrical_testing'),
               ('Бордюрный камень Тиманфайа','material','gotika'),
               ('Светильник AF123456789012','product','alfresco'),
               ('Стеклошарики для разметки','material','marking_material'),
               ('Бурение ям глубиной до 2 м','work','drilling')]
        for name,kind,expected in cases:
            with self.subTest(name=name): self.assertEqual(suppliers.category(row('x',name,kind=kind)),expected)
        self.assertIsNone(suppliers.category(row('x','Неизвестная работа',kind='work')))
        self.assertIsNone(suppliers.category(row('x','Кабель до 35 кВ',kind='aggregate')))

    def test_contractor_request_separates_labor_materials_and_machine_costs(self):
        self.source['positions']=[row('w','Установка опор наружного освещения',kind='work')]
        result=suppliers.prepare(self.source)
        self.assertEqual(result['position_count'],1)
        body=jobs.jobs(TID)[0]['result']['drafts'][0]['body']
        self.assertIn('выполнить такие работы',body)
        self.assertIn('отдельно стоимость работ, материалов и техники',body)
        self.assertNotIn('12345678',body)
        self.assertNotIn('наличие',body)

    def test_nonlocal_manufacturer_requires_explicit_evidence(self):
        source=suppliers.source_for('td-souz')
        self.assertIn('Москвы',source['company'])
        html='Фабрика Готика: продажа с доставкой в регионы. zakaz@td-souz.ru'
        self.assertEqual(campaigns.extract_contact(source,html),'zakaz@td-souz.ru')
        with self.assertRaises(BuyerError): campaigns.extract_contact(source,html.replace('доставкой в регионы','самовывоз'))

    def test_reply_partial_prices_saved_and_shown_by_position_without_overwriting_estimate(self):
        key,claim=self.sent();self.assertTrue(replies.update(key,'reader',claim['token'],self.result()))
        data=replies.listing(TID);p=data['messages'][0]['prices'][0]
        self.assertEqual(p['price_kopecks'],12050);self.assertEqual(p['state'],'comparable')
        rows=copy.deepcopy(self.source['positions']);replies.annotate(TID,rows)
        self.assertEqual(rows[0]['buyer_quotes'][0]['price_kopecks'],12050)
        self.assertEqual(rows[0]['estimate_unit'],12345678);self.assertFalse(rows[1]['buyer_quotes'])

    def test_same_reply_is_not_inserted_twice_and_changed_message_rejected(self):
        key,claim=self.sent();result=self.result();replies.update(key,'reader',claim['token'],result)
        replies.request_check(TID,key);second=replies.claim('reader');replies.update(key,'reader',second['token'],result)
        self.assertEqual(len(replies.listing(TID)['messages']),1)
        replies.request_check(TID,key);third=replies.claim('reader');result['messages'][0]['text']='changed'
        with self.assertRaises(BuyerError): replies.update(key,'reader',third['token'],result)

    def test_foreign_sender_made_up_quote_unknown_line_and_duplicate_line_roll_back(self):
        key,claim=self.sent()
        for mutate in [lambda m:m.update(sender='other@example.org'),lambda m:m['prices'][0].update(quote='invented'),lambda m:m['prices'][0].update(line=9),lambda m:m['prices'].append(m['prices'][0])]:
            result=self.result();mutate(result['messages'][0])
            with self.assertRaises(BuyerError): replies.update(key,'reader',claim['token'],result)
            self.assertFalse(replies.listing(TID)['messages'])

    def test_unknown_vat_wrong_unit_alternative_and_wrong_amount_require_review(self):
        for change in [dict(vat='НДС'),dict(unit='шт'),dict(exact_match=False),dict(price='12')]:
            item=self.result(**change)['messages'][0]
            parsed=replies.parse_price(item['prices'][0],self.source['positions'][0],item['text'])
            self.assertEqual(parsed[-2],'review')

    def test_stale_or_foreign_reader_cannot_complete(self):
        key,claim=self.sent()
        self.assertFalse(replies.update(key,'other',claim['token'],self.result()))
        self.assertFalse(replies.update(key,'reader','wrong',self.result()))
        with closing(replies.connect()) as db,db: db.execute('UPDATE buyer_inbox_checks SET lease_until=0')
        self.assertFalse(replies.update(key,'reader',claim['token'],self.result()))
        self.assertIsNone(replies.claim('another-reader'))
        new=replies.claim('reader');self.assertEqual(new['token'],claim['token'])
        self.assertTrue(replies.update(key,'reader',new['token'],self.result()))

    def test_reader_and_sender_do_not_share_the_browser_at_the_same_time(self):
        key,claim=self.sent()
        draft=next(j for j in jobs.jobs(TID) if j['payload']['draft_task'].get('supplier',{}).get('id')=='ruselectrica')
        second=box.enqueue(TID,draft['id'],0,'other@example.org')
        self.assertIsNone(box.claim('mac'))
        replies.update(key,'reader',claim['token'],dict(status='checked',messages=[],detail='Ответа пока нет'))
        sending=box.claim('mac');self.assertEqual(sending['id'],second)
        replies.request_check(TID,key)
        self.assertIsNone(replies.claim('reader'))

    def test_no_reply_does_not_create_price_and_no_worker_secrets_returned(self):
        key,claim=self.sent();replies.update(key,'reader',claim['token'],dict(status='checked',messages=[],detail='Ответа пока нет'))
        result=replies.listing(TID);self.assertFalse(result['messages'])
        self.assertNotIn('token',str(result));self.assertNotIn('worker',str(result))

    def test_changed_position_does_not_use_old_quote(self):
        key,claim=self.sent();replies.update(key,'reader',claim['token'],self.result())
        changed=copy.deepcopy(self.source['positions']);changed[0]['quantity']=200
        replies.annotate(TID,changed);self.assertFalse(changed[0]['buyer_quotes'])

    def test_changed_requirements_and_region_do_not_use_old_quote(self):
        self.source['positions'][0]['specification']={'requirements':{'specifications':['Медный кабель']}}
        key,claim=self.sent();replies.update(key,'reader',claim['token'],self.result())
        rows=copy.deepcopy(self.source['positions']);replies.annotate(TID,rows,region=self.source['region'])
        self.assertEqual(len(rows[0]['buyer_quotes']),1)
        rows[0]['specification']['requirements']['specifications']=['Алюминиевый кабель']
        replies.annotate(TID,rows,region=self.source['region']);self.assertFalse(rows[0]['buyer_quotes'])
        rows=copy.deepcopy(self.source['positions']);replies.annotate(TID,rows,region='Москва')
        self.assertFalse(rows[0]['buyer_quotes'])

    def test_edited_request_cannot_automatically_bind_reply_lines(self):
        j=self.prepared();draft=copy.deepcopy(j['result']['drafts'][0])
        lines=draft['body'].splitlines()
        numbered=[i for i,line in enumerate(lines) if line.startswith(('1.','2.'))]
        self.assertEqual(len(numbered),2)
        a,b=numbered;lines[a],lines[b]=lines[b],lines[a];draft['body']='\n'.join(lines)
        key=box.enqueue(TID,j['id'],0,'info@tl-electro.ru',message={'subject':draft['subject'],'body':draft['body']})
        sent=box.claim('mac');box.update(key,'mac',sent['token'],dict(status='sent',detail='Проверено',evidence='sent.txt'))
        replies.request_check(TID,key);claim=replies.claim('reader')
        self.assertFalse(claim['mapping_trusted'])
        replies.update(key,'reader',claim['token'],self.result())
        price=replies.listing(TID)['messages'][0]['prices'][0]
        self.assertEqual(price['state'],'review');self.assertIn('привязку',price['reason'])
        rows=copy.deepcopy(self.source['positions']);replies.annotate(TID,rows)
        self.assertTrue(all(not p['buyer_quotes'] for p in rows))

    def test_unsent_and_other_tender_cannot_request_inbox(self):
        j=self.prepared();key=box.enqueue(TID,j['id'],0,'info@tl-electro.ru')
        with self.assertRaises(BuyerError): replies.request_check(TID,key)
        self.assertIsNone(replies.claim('reader'))
        with self.assertRaises(BuyerError): replies.request_check('999999999999',key)

    def test_inbox_api_auth_and_token_required(self):
        app=Flask(__name__);app.register_blueprint(routes.blueprint);c=app.test_client()
        endpoint=routes.WORKER_API+'/inbox/claim'
        self.assertEqual(c.post(endpoint,json={'worker_id':'reader'}).status_code,401)
        with patch.dict('os.environ',{'BUYER_WORKER_TOKEN':'x'*48}):
            h={'Authorization':'Bearer '+'x'*48}
            self.assertEqual(c.post(endpoint,json={'worker_id':'reader'},headers=h).status_code,200)
            self.assertEqual(c.post(routes.WORKER_API+'/inbox/missing/complete',json={'worker_id':'reader','result':{}},headers=h).status_code,409)

    def test_only_explicit_unit_scaling_is_converted(self):
        self.assertEqual(replies.comparison_amount(12050,'м','1000 м'),12050000)
        self.assertEqual(replies.comparison_amount(12050000,'т','кг'),12050)
        self.assertIsNone(replies.comparison_amount(12050,'м','шт'))
        self.assertEqual(replies.comparison_amount(1250,'пм','м'),1250)


if __name__=='__main__': unittest.main()
