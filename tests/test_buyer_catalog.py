from contextlib import closing
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from autobot import buyer_catalog as catalog, buyer_store as store, buyer_outbox as box
from autobot import supplier_catalog_store as price_store, market_price_index
from autobot.hermes_buyer import BuyerError


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        p = patch.object(box, 'DB_PATH', Path(self.tmp.name)/'test.db')
        p.start(); self.addCleanup(p.stop)
        self.catalog_path = Path(self.tmp.name)/'catalog.db'
        p = patch.object(market_price_index, 'INDEX_DB', self.catalog_path)
        p.start(); self.addCleanup(p.stop)
        self.row = {'position_key':'a','name':'Сжим У733М','unit':'шт','quantity':'172','type_slug':'material'}
        self.source = {'tender_id':'1234567890','region':'Ярославская область','positions':[self.row]}
        self.url = 'https://supplier.example/u733'
        self.stamp = time.time()-10

    def remember(self, *, stamp=None, accepted=True, price=3411):
        stamp = self.stamp if stamp is None else stamp
        run = store.enqueue(self.source)
        candidate = {'id':'supplier','url':self.url,'company':'Public company','email':'private@example.org',
            'emails':['public@example.org','private@example.org'],'channels':[], 'position_keys':['a'],
            'evidence_pages':[{'url':self.url,'checked_at':stamp,'excerpt':'Сжим У733М public@example.org'}],
            'prices':[{'position_key':'a','source_url':self.url,'price_kopecks':price,'unit':'шт',
                       'evidence':'Сжим У733М','observed_at':stamp,'vat':'с НДС'}] if accepted else [],
            'price_checks':[{'position_key':'a','source_url':self.url,'accepted':accepted,
                             'observed_at':stamp,'evidence':'Сжим У733М','reason':'' if accepted else 'Нет в наличии'}]}
        with closing(box.connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO buyer_search_candidates VALUES (?,?,?)',(run,'supplier',json.dumps(candidate)))
        return run

    def test_readonly_empty_catalog_does_not_create_database(self):
        self.assertEqual(catalog.catalog(self.source)['items'], [])
        self.assertFalse(box.DB_PATH.exists())

    def test_reuses_public_url_across_tenders_and_quantities_not_private_conditions(self):
        self.remember()
        other = copy.deepcopy(self.source)
        other['tender_id']='9999999999'; other['positions'][0]['quantity']='12'
        data = catalog.catalog(other)
        self.assertEqual(data['items'][0]['latest']['price_kopecks'],3411)
        self.assertEqual(data['items'][0]['freshness'],'recently_checked')
        text=json.dumps(data)
        for secret in ('private@example.org','1234567890','quantity','tender_id'):
            self.assertNotIn(secret,text)
        self.assertEqual(data['items'][0]['contacts'][0]['address'],'public@example.org')
        links=store.cached_links({'intent':'product','position_keys':['a']},other)
        self.assertEqual(links,[{'url':self.url,'title':'supplier.example','reused':True}])
        self.assertNotIn('price_kopecks',json.dumps(links))

    def test_changed_model_or_unit_does_not_reuse_offer(self):
        self.remember()
        for change in ({'name':'Сжим У734М'},{'unit':'100 шт'}):
            other=self.source|{'positions':[self.row|change]}
            self.assertEqual(catalog.catalog(other)['items'],[])

    def test_expired_observation_is_labeled_but_url_can_be_rechecked(self):
        self.remember(stamp=self.stamp-8*86400)
        data=catalog.catalog(self.source)
        self.assertEqual(data['items'][0]['freshness'],'needs_refresh')
        self.assertEqual(len(catalog.links({'position_keys':['a']},self.source)),1)

    def test_latest_rejection_does_not_restore_old_price(self):
        self.remember()
        self.source=self.source|{'tender_id':'5555555555'}
        self.remember(stamp=self.stamp+5,accepted=False)
        item=catalog.catalog(self.source)['items'][0]
        self.assertEqual(len(item['observations']),2)
        self.assertEqual(item['latest']['state'],'review')
        self.assertIsNone(item['latest']['price_kopecks'])

    def test_work_sources_do_not_cross_regions(self):
        self.row.update(name='Укладка тротуарной плитки',type_slug='work',unit='м2')
        self.remember(accepted=False)
        self.assertEqual(catalog.catalog(self.source|{'region':'Москва'})['items'],[])

    def test_source_run_is_idempotent_draft_only_and_does_not_search(self):
        links=[{'url':self.url,'position_keys':['a']}]
        a=store.enqueue_sources(self.source,links)
        b=store.enqueue_sources(self.source,links+links)
        self.assertEqual(a,b)
        run=store.source(self.source['tender_id'],a)
        self.assertEqual(run['payload']['delivery'],'draft')
        with closing(box.connect()) as db:
            kinds=[r[0] for r in db.execute('SELECT kind FROM buyer_search_steps WHERE run_id=?',(a,))]
            self.assertEqual(sorted(kinds),['inspect','prepare'])
            self.assertEqual(db.execute('SELECT count(*) FROM outbound').fetchone()[0],0)

    def test_unknown_positions_and_directories_rejected_before_enqueue(self):
        for links in ([{'url':self.url,'position_keys':['missing']}],
                      [{'url':'https://rusprofile.ru/','position_keys':['a']}],
                      [{'url':'file:///tmp/a','position_keys':['a']}]):
            with self.assertRaises(BuyerError):store.enqueue_sources(self.source,links)
        self.assertFalse(box.DB_PATH.exists())

    def test_claim_rotates_between_tenders(self):
        runs=[store.enqueue(self.source|{'tender_id':str(1234567890+i)}) for i in range(3)]
        picked=[store.claim()['run_id'] for _ in range(3)]
        self.assertEqual(set(picked),set(runs))

    def test_existing_price_base_is_consulted_without_buyer_database(self):
        offer = {'url':self.url, 'title':'Сжим У733М', 'price':34.11}
        with patch('autobot.supplier_catalog_match.lookup', return_value=[offer]) as lookup:
            links = store.cached_links({'intent':'product','position_keys':['a']}, self.source)
        self.assertEqual(links,[{'url':self.url,'title':'Сжим У733М','reused':True}])
        self.assertEqual(lookup.call_args.kwargs['quantity'],'172')
        self.assertNotIn('price',links[0])
        self.assertFalse(box.DB_PATH.exists())

    def test_public_capture_enters_existing_catalog_with_actual_product_and_history(self):
        price_store.initialize(self.catalog_path)
        html = '<h1>Сжим У734М</h1><p>Купить сжим У734М: 42 руб / шт</p><a href="mailto:sales@example.org">sales@example.org</a>'
        candidate = {'url':self.url,'email':'private@example.org','evidence_pages':[{'url':self.url}]}
        task = {'bucket':'materials','position_keys':['a']}
        captures = {self.url:(html,self.stamp)}
        catalog.publish_pages(candidate,captures,task,path=self.catalog_path)
        catalog.publish_pages(candidate,captures,task,path=self.catalog_path)
        data = price_store.items(path=self.catalog_path)
        item = data['items'][0]
        self.assertEqual(item['name'],'Сжим У734М')
        self.assertEqual(len(price_store.history(item['id'],self.catalog_path)),1)
        self.assertNotIn('private@example.org',json.dumps(data))
        self.assertIn('sales@example.org',item['contact_evidence'])
        self.assertEqual(price_store.document(item['document_id'],self.catalog_path)['body'],html)
        catalog.publish_pages(candidate,{self.url:(html.replace('42 руб','45 руб'),self.stamp+1)},task,path=self.catalog_path)
        self.assertEqual(len(price_store.history(item['id'],self.catalog_path)),2)

    def test_catalog_offline_does_not_create_or_migrate_a_database(self):
        self.assertEqual(catalog.publish_pages({'url':self.url},{}, {'bucket':'materials'},path=self.catalog_path),0)
        self.assertFalse(self.catalog_path.exists())

    def test_planar_dimensions_allow_explicit_thickness_but_not_partial_numbers(self):
        from autobot.buyer_needs import identifier_matches
        for text in ('300x100x60 мм','300 × 100 × 60мм','Размер 300х100 мм'):
            self.assertTrue(identifier_matches('300x100',text),text)
        for text in ('1300х100х60','300х1000х60','300х100х6000x80'):
            self.assertFalse(identifier_matches('300x100',text),text)
        self.assertFalse(identifier_matches('У733М','У733М1'))

    def test_kerb_installation_is_a_paving_contractor_not_just_material_sales(self):
        from autobot.buyer_discovery import inspect
        source=self.source|{'positions':[self.row|{'name':'Установка бортовых камней бетонных','type_slug':'work','unit':'100 м'}]}
        task={'url':self.url,'position_keys':['a'],'bucket':'works','category':'paving_work','intent':'supplier'}
        result=inspect(task,source,fetch=lambda url:(url,'<h1>Установка бордюров</h1><p>Заказать услуги в Рыбинске</p>'))
        self.assertEqual(result['position_keys'],['a'])
        with self.assertRaises(BuyerError):
            inspect(task,source,fetch=lambda url:(url,'<h1>Бордюры</h1><p>Продажа бордюров. Цена по запросу.</p>'))


if __name__=='__main__':unittest.main()
