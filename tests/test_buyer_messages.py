from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from flask import Flask
from autobot import buyer_messages as messages, buyer_outbox as box, buyer_replies as replies, buyer_routes as routes
from autobot.hermes_buyer import BuyerError
from autobot.uploaded_corrections import CorrectionError

TID = '0171200001926000664'
BODY = 'Добрый день!\nПодскажите срок доставки и стоимость разгрузки.'
REQUEST = 'request-1234567890'


class MessageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        for p in (patch.object(box, 'DB_PATH', Path(tmp.name)/'outbox.sqlite3'),
                  patch.object(box.buyer_jobs, 'jobs', return_value=[])):
            p.start(); self.addCleanup(p.stop)
        with closing(box.connect()) as db, db:
            db.execute('''INSERT INTO outbound
                (id,fingerprint,tender_id,draft_job_id,draft_index,recipient,subject,body,status,created_at,updated_at)
                VALUES ('parent','original',?,'draft',0,'supplier@example.org','Кабель [AB-RFQ-ABCDEF]','Исходный запрос','sent',?,?)''', (TID,time.time()-1000,time.time()-900))

    def enqueue(self, **kwargs):
        args = dict(tid=TID,parent_id='parent',body=BODY,request_id=REQUEST,actor_id=7)
        return messages.enqueue(**(args|kwargs))

    def incoming(self, key='reply-1', sender='manager@example.org', parent='parent', stamp=None):
        with closing(replies.connect()) as db, db:
            db.execute('INSERT INTO buyer_replies VALUES (?,?,?,?,?,?,?,?)',
                       (key,parent,'rfc822:<'+key+'@example.org>',sender,'Ответ',stamp or time.time(),'IMAP',time.time()))

    def test_reply_routes_to_manager_and_freezes_reference_across_restart(self):
        self.incoming()
        key = self.enqueue()
        self.incoming(key='reply-2', sender='other-manager@example.org', stamp=time.time()+1)
        self.assertEqual(self.enqueue(), key)
        job = box.claim('local')
        self.assertEqual(job['recipient'], 'manager@example.org')
        self.assertEqual(job['in_reply_to'], '<reply-1@example.org>')
        again = box.claim('local')
        self.assertEqual((again['id'], again['in_reply_to']), (key, job['in_reply_to']))
        box.update(key,'local',job['token'],dict(status='sent',detail='SMTP',evidence='proof'))
        later = self.enqueue(request_id='new-request-123456789')
        self.assertEqual(next(r for r in box.listing(TID) if r['id']==later)['recipient'], 'other-manager@example.org')
        self.assertEqual(box.listing(TID)[0]['recipient'], 'supplier@example.org')

    def test_foreign_reply_or_public_provider_neighbor_does_not_redirect(self):
        self.incoming(sender='manager@evil.example.org')
        key = self.enqueue()
        self.assertEqual(next(r for r in box.listing(TID) if r['id']==key)['recipient'], 'supplier@example.org')
        self.incoming(key='elsewhere', sender='manager@example.org', parent='different-tender')
        next_key = self.enqueue(request_id='new-request-123456789')
        with closing(box.connect()) as db:
            self.assertEqual(messages.threading_headers(db, next_key), {})
        self.assertIsNone(messages.reply_target({'recipient':'one@mail.ru'},
            {'sender':'two@mail.ru','message_id':'rfc822:<one@mail.ru>'}))
        self.assertIsNone(messages.reply_target({'recipient':'one@example.org'},
            {'sender':'two@example.org','message_id':'rfc822:<a@example.org>\r\nBcc: x@evil.org'}))

    def test_saved_manager_target_cannot_be_replaced_by_another_recipient_or_thread(self):
        self.incoming()
        key = self.enqueue()
        with closing(box.connect()) as db, db:
            db.execute("UPDATE outbound SET recipient='other@example.org' WHERE id=?", (key,))
        self.assertIsNone(box.claim('local'))
        self.assertEqual(next(r for r in box.listing(TID) if r['id']==key)['status'], 'blocked')
        self.incoming(key='other-reply', parent='other-thread')
        next_key = self.enqueue(request_id='new-request-123456789')
        with closing(box.connect()) as db, db:
            db.execute('UPDATE buyer_manual_reply_targets SET reply_id=? WHERE outbound_id=?', ('other-reply',next_key))
        self.assertIsNone(box.claim('local'))

    def test_additive_schema_reopen_preserves_existing_messages(self):
        with closing(box.connect()) as db:
            before=[tuple(r) for r in db.execute('SELECT * FROM outbound')]
        with closing(box.connect()) as db:
            self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM outbound')])
            self.assertEqual(db.execute('SELECT count(*) FROM buyer_manual_reply_targets').fetchone()[0],0)

    def test_exact_text_recipient_subject_actor_and_concurrent_repeat(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            ids = list(pool.map(lambda _: self.enqueue(),range(3)))
        self.assertEqual(len(set(ids)),1)
        row = next(r for r in box.listing(TID) if r['id']==ids[0])
        self.assertEqual(row['body'],BODY)
        self.assertEqual(row['recipient'],'supplier@example.org')
        self.assertEqual(row['subject'],'Re: Кабель [AB-RFQ-ABCDEF]')
        self.assertTrue(row['manual']); self.assertEqual(row['parent_outbound_id'],'parent')
        with closing(box.connect()) as db:
            self.assertEqual(db.execute('SELECT actor_id FROM buyer_manual_messages').fetchone()[0],7)
        self.assertEqual(self.enqueue(),ids[0], 'A new connection after restart recovers the same operation')
        with self.assertRaises(BuyerError): self.enqueue(body='Другой текст')
        with self.assertRaises(BuyerError): self.enqueue(tid='123456789012345')

    def test_unconfirmed_or_cross_tender_parent_and_invalid_text_are_rejected(self):
        for body in ('', '  ', 'x'*10001, '\x00mail', None, {}):
            with self.subTest(body=str(body)[:20]), self.assertRaises(BuyerError): self.enqueue(body=body)
        for status in ('queued','sending','uncertain','blocked'):
            with closing(box.connect()) as db, db: db.execute('UPDATE outbound SET status=?',(status,))
            with self.subTest(status=status), self.assertRaises(BuyerError): self.enqueue()
        self.assertEqual(len(box.listing(TID)),1)

    def test_manual_message_claim_does_not_revalidate_changed_estimate(self):
        key = self.enqueue()
        with patch.object(box,'draft_message',side_effect=BuyerError('Смета изменилась')):
            job = box.claim('mac')
        self.assertEqual(job['id'],key); self.assertEqual(job['body'],BODY)
        self.assertTrue(box.update(key,'mac',job['token'],{'status':'sent','detail':'Отправленные','evidence':'proof'}))
        self.assertEqual(self.enqueue(),key)
        self.assertIsNone(box.claim('mac'))
        self.assertEqual(len(box.listing(TID)),2)

    def test_sent_child_resolves_root_and_only_blocked_retries(self):
        key = self.enqueue(); job = box.claim('mac')
        box.update(key,'mac',job['token'],{'status':'blocked','detail':'Вход в почту недоступен','evidence':''})
        self.assertEqual(box.retry_blocked(TID,key),key)
        self.assertEqual(box.retry_blocked(TID,key),key)
        job = box.claim('mac')
        box.update(key,'mac',job['token'],{'status':'sent','detail':'Отправленные','evidence':'proof'})
        with self.assertRaises(BuyerError): box.retry_blocked(TID,key)
        second = self.enqueue(parent_id=key, request_id='another-request-12345',body='Спасибо!')
        child = next(r for r in box.listing(TID) if r['id']==second)
        self.assertEqual(child['parent_outbound_id'],'parent')
        self.assertEqual(child['subject'],'Re: Кабель [AB-RFQ-ABCDEF]')
        with closing(box.connect()) as db, db:
            db.execute("UPDATE outbound SET status='uncertain',receipt=? WHERE id=?",(box.encoded({'status':'uncertain','detail':'Сбой','evidence':''}),second))
        with self.assertRaises(BuyerError): box.retry_blocked(TID,second)

    def test_one_inbox_check_and_conservative_price_mapping(self):
        key = self.enqueue(); job = box.claim('mac')
        box.update(key,'mac',job['token'],{'status':'sent','detail':'Отправленные','evidence':'proof'})
        replies.request_check(TID,key)
        payload={'positions':[{'position_key':'p1','name':'Кабель','quantity':100,'unit':'м'}],'region':'Ярославль'}
        with patch.object(box,'draft_message',return_value=(payload,{'position_keys':['p1'],'body':'Исходный запрос'})):
            inbox = replies.claim('mac')
        self.assertEqual(inbox['id'],'parent'); self.assertFalse(inbox['mapping_trusted'])
        with closing(replies.connect()) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT outbound_id FROM buyer_inbox_checks')],['parent'])

    def test_tampered_manual_link_never_sends_to_different_recipient(self):
        key = self.enqueue()
        with closing(box.connect()) as db, db: db.execute("UPDATE outbound SET recipient='other@example.org' WHERE id=?",(key,))
        self.assertIsNone(box.claim('mac'))
        self.assertEqual(box.listing(TID)[-1]['status'],'blocked')

    def test_http_auth_binds_actor_and_never_accepts_client_recipient(self):
        app=Flask(__name__)
        with patch('autobot.buyer_discovery.start_worker'), patch.object(routes.campaigns,'launch'):
            app.register_blueprint(routes.blueprint)
        client=app.test_client(); url=f'/api/tenders/{TID}/buyer/outbox'
        data=dict(action='message',parent_id='parent',body=BODY,request_id=REQUEST,actor_id=999,recipient='wrong@example.org')
        for status in (401,403):
            with patch.object(routes.crm_actor,'resolve',side_effect=CorrectionError('Нет доступа',status)):
                self.assertEqual(client.post(url,json=data).status_code,status)
        with patch.object(routes.crm_actor,'resolve',return_value={'id':7,'name':'Пользователь'}), patch('autobot.tender_activity.record'):
            response=client.post(url,json=data)
            self.assertEqual(response.status_code,202)
            self.assertEqual(client.post(url,json=data).json['id'],response.json['id'])
        with closing(box.connect()) as db:
            self.assertEqual(db.execute('SELECT actor_id FROM buyer_manual_messages').fetchone()[0],7)
        self.assertEqual(box.listing(TID)[-1]['recipient'],'supplier@example.org')


if __name__=='__main__': unittest.main()
