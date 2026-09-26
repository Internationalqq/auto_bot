from contextlib import closing
import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from autobot import buyer_campaigns as campaigns, buyer_outbox as outbox
from autobot.hermes_buyer import BuyerError

TID='123456789012345'
JOB={'id':'j1','status':'completed','payload':{'draft_task':{'schema_version':2,'tender_id':TID,
     'region':'Ярославская область','positions':[{'position_key':'p1','name':'Щебень М1200','type_slug':'material'}]}},
     'result':{'drafts':[{'position_keys':['p1'],'subject':'Щебень','body':'Добрый день! Нужен щебень М1200 20–40, 57,859 м³.'}],'questions':[]}}


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.job=copy.deepcopy(JOB)
        for item in (patch.object(outbox,'DB_PATH',Path(self.tmp.name)/'outbox.db'),
                     patch.object(outbox.buyer_jobs,'jobs',side_effect=lambda tid:[self.job] if tid==TID else []),
                     patch.object(campaigns,'launch')):
            item.start(); self.addCleanup(item.stop)

    def test_verified_sources_queue_once_and_keep_company_channel(self):
        key=campaigns.start(TID,'j1',0)
        self.assertEqual(campaigns.start(TID,'j1',0),key)
        with patch.object(campaigns,'fetch_contact',side_effect=lambda s:s['email']):
            self.assertTrue(campaigns.run_one())
            self.assertFalse(campaigns.run_one())
        saved=campaigns.listing(TID)[0]
        self.assertEqual(saved['status'],'completed')
        self.assertEqual(len(saved['contacts']),3)
        self.assertEqual(len(outbox.listing(TID)),3)
        self.assertTrue(all(c['company'] and c['channel']=='email' and c['source_url'].startswith('https://') for c in saved['contacts']))

    def test_source_contact_missing_never_queues_mail(self):
        key=campaigns.start(TID,'j1',0)
        with patch.object(campaigns,'fetch_contact',side_effect=BuyerError('Контакт пропал')):
            campaigns.run_one()
        self.assertEqual(outbox.listing(TID),[])
        self.assertTrue(all(c['error']=='Контакт пропал' for c in campaigns.listing(TID)[0]['contacts']))
        self.assertEqual(campaigns.start(TID,'j1',0),key)
        with patch.object(campaigns,'fetch_contact',side_effect=lambda s:s['email']):campaigns.run_one()
        self.assertEqual(len(outbox.listing(TID)),3)

    def test_restart_resumes_expired_discovery_without_duplicate_send(self):
        key=campaigns.start(TID,'j1',0)
        outbox.enqueue(TID,'j1',0,campaigns.SOURCES[0]['email'])
        with closing(campaigns.connect()) as db, db:
            db.execute("UPDATE buyer_campaigns SET status='checking',lease_until=0 WHERE id=?",(key,))
        with patch.object(campaigns,'fetch_contact',side_effect=lambda s:s['email']):campaigns.run_one()
        self.assertEqual(len(outbox.listing(TID)),3)

    def test_active_discovery_not_claimed_twice(self):
        key=campaigns.start(TID,'j1',0)
        with closing(campaigns.connect()) as db, db:
            db.execute("UPDATE buyer_campaigns SET status='checking',lease_until=9999999999 WHERE id=?",(key,))
        with patch.object(campaigns,'fetch_contact') as fetch:
            self.assertFalse(campaigns.run_one());fetch.assert_not_called()

    def test_other_tender_region_category_and_work_rejected(self):
        with self.assertRaises(BuyerError): campaigns.start('987654321','j1',0)
        task=self.job['payload']['draft_task']
        for field,value in [('region','Москва'),('name','Кабель'),('type_slug','work')]:
            target=task if field=='region' else task['positions'][0]
            old=target[field];target[field]=value
            with self.assertRaises(BuyerError):campaigns.start(TID,'j1',0)
            target[field]=old

    def test_source_requires_visible_region_category_and_published_contact(self):
        source=campaigns.SOURCES[0]
        html=f'<h1>Щебень в Ярославле</h1><a href="mailto:{source["email"]}">Почта</a>'
        self.assertEqual(campaigns.extract_contact(source,html),source['email'])
        for bad in [html.replace('Ярославле','Москве'), html.replace('Щебень','Песок'),
                    html.replace(source['email'],'different@example.org')]:
            with self.assertRaises(BuyerError):campaigns.extract_contact(source,bad)


if __name__=='__main__':unittest.main()
