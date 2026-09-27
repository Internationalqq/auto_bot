from contextlib import closing
from html import escape
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from autobot import buyer_followups as follow, buyer_outbox as box, buyer_replies as replies
from autobot.hermes_buyer import BuyerError

TID='0171200001926000664'
ADDRESS='Ярославская область, г. Рыбинск, ул. Волжская набережная, участок от ДС Полет до д. 4к1'


class FollowupTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); self.root=Path(temp.name)
        for p in [patch.object(box,'DB_PATH',self.root/'outbox.db'),patch.object(follow,'DATA_DIR',self.root)]:
            p.start(); self.addCleanup(p.stop)

    def doc(self,name,paragraphs):
        path=self.root/'extracted'/TID/name; path.parent.mkdir(parents=True,exist_ok=True)
        with ZipFile(path,'w') as zip:
            zip.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+''.join('<w:p><w:r><w:t>'+escape(p)+'</w:t></w:r></w:p>' for p in paragraphs)+'</w:body></w:document>')
        return path

    def seed(self,sender='supplier@example.org'):
        now=time.time()
        with closing(replies.connect()) as db,db:
            db.execute('''INSERT INTO outbound (id,fingerprint,tender_id,draft_job_id,draft_index,recipient,subject,body,status,created_at,updated_at)
                VALUES ('parent','fp',?,'draft',0,'supplier@example.org','Щебень','Запрос','sent',?,?)''',(TID,now-100,now-90))
            db.execute('INSERT INTO buyer_replies VALUES (?,?,?,?,?,?,?,?)',('reply','parent','message1',sender,'Скажите, пожалуйста, адрес объекта, куда везти. Цена зависит от этого',now-50,'mail proof',now))

    def test_address_is_labelled_jobsite_not_customer_office(self):
        self.doc('contract.docx',['Юридический адрес: Москва, улица Другая 1','6.3. Место выполнения работ: '+ADDRESS+'.'])
        self.doc('terms.docx',['Место выполнения работ',ADDRESS])
        source,error=follow.delivery_address(TID)
        self.assertFalse(error);self.assertEqual(source['address'],ADDRESS);self.assertEqual(len(source['sha256']),64)

    def test_conflicts_never_guess_an_address(self):
        self.doc('one.docx',['Адрес объекта: '+ADDRESS])
        self.doc('two.docx',['Адрес объекта: Ярославская область, г. Ярославль, улица Другая 12'])
        self.assertIsNone(follow.delivery_address(TID)[0])

    def test_question_in_quoted_history_is_not_a_new_request(self):
        self.assertFalse(follow.address_question('Спасибо, цена 2000 руб.\n> Уточните адрес объекта, куда везти'))
        self.assertTrue(follow.address_question('Куда везти?'))
        self.assertFalse(follow.address_question('Адрес объекта получен, спасибо!'))
        self.assertFalse(follow.address_question('Адрес доставки: Рыбинск, набережная. Цена 2000 руб.'))

    def test_restart_and_repeated_collection_enqueue_only_one_reply(self):
        self.doc('contract.docx',['Место выполнения работ: '+ADDRESS]);self.seed()
        follow.process_pending();follow.process_pending()
        rows=box.listing(TID)
        self.assertEqual(len(rows),2)
        child=next(row for row in rows if row['id']!='parent')
        self.assertEqual(child['recipient'],'supplier@example.org');self.assertEqual(child['status'],'queued')
        self.assertIn(ADDRESS,child['body']);self.assertEqual(child['subject'],'Re: Щебень')
        self.assertEqual(follow.listing(TID)[0]['outbound_id'],child['id'])
        follow.validate_source(child | {'tender_id':TID})
        # Another question in the same conversation never sends a loop.
        with closing(replies.connect()) as db,db:
            db.execute('INSERT INTO buyer_replies SELECT ?,outbound_id,?,sender,raw_text,received_at,evidence,created_at FROM buyer_replies WHERE id=?',('reply2','message2','reply'))
        follow.process_pending();self.assertEqual(len(box.listing(TID)),2)
        self.doc('contract.docx',['Место выполнения работ: '+ADDRESS+' дом 12'])
        with self.assertRaises(BuyerError):follow.validate_source(child | {'tender_id':TID})

    def test_missing_source_or_different_sender_requires_review(self):
        self.seed();follow.process_pending()
        self.assertEqual(len(box.listing(TID)),1);self.assertEqual(follow.listing(TID)[0]['status'],'review')
        self.doc('contract.docx',['Место выполнения работ: '+ADDRESS])
        with closing(replies.connect()) as db,db:
            db.execute("UPDATE buyer_replies SET sender='other@example.org'")
            db.execute('UPDATE buyer_followups SET next_at=0')
        follow.process_pending();self.assertEqual(len(box.listing(TID)),1)

    def test_manual_reply_prevents_an_extra_automatic_address_reply(self):
        from autobot.buyer_messages import enqueue
        self.doc('contract.docx',['Место выполнения работ: '+ADDRESS]); self.seed()
        enqueue(TID,'parent','Адрес уточняю, напишу позже.','manual-request-123456',7)
        follow.process_pending()
        self.assertEqual(len(box.listing(TID)),2)
        self.assertEqual(follow.listing(TID)[0]['status'],'review')
        self.assertIn('Пользователь уже написал',follow.listing(TID)[0]['reason'])

    def test_proven_unsent_reply_can_retry_without_resending_parent(self):
        self.doc('contract.docx',['Место выполнения работ: '+ADDRESS]);self.seed();follow.process_pending()
        child=next(row for row in box.listing(TID) if row['id']!='parent')['id']
        receipt=box.encoded({'status':'blocked','detail':'Не открылась почтовая навигация','evidence':''})
        with closing(box.connect()) as db,db:
            db.execute("UPDATE outbound SET status='blocked',receipt=? WHERE id=?",(receipt,child))
        with patch.object(box.buyer_jobs,'jobs',return_value=[]):
            self.assertEqual(box.retry_blocked(TID,child),child)
            self.assertEqual(box.retry_blocked(TID,child),child)
            self.assertEqual(len(box.listing(TID)),2)
            with closing(box.connect()) as db,db:
                self.assertEqual(db.execute('SELECT count(*) FROM outbound_attempt_history').fetchone()[0],1)
                self.assertEqual(db.execute("SELECT status FROM outbound WHERE id='parent'").fetchone()[0],'sent')
                db.execute("UPDATE outbound SET status='uncertain',receipt=? WHERE id=?",(receipt,child))
            with self.assertRaises(BuyerError): box.retry_blocked(TID,child)
            with closing(box.connect()) as db,db:
                db.execute("UPDATE outbound SET status='blocked' WHERE id=?",(child,))
                db.execute('''INSERT INTO outbound (id,fingerprint,tender_id,draft_job_id,draft_index,recipient,subject,body,status,created_at,updated_at)
                    SELECT 'duplicate','other',tender_id,draft_job_id,draft_index,recipient,subject,body,'sent',created_at,updated_at FROM outbound WHERE id=?''',(child,))
            with self.assertRaises(BuyerError): box.retry_blocked(TID,child)


if __name__=='__main__':unittest.main()
