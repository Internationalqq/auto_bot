import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock,patch
import unittest

from autobot import buyer_mailru_inbox as inbox, buyer_inbox
from autobot.hermes_buyer import BuyerError

NOW=datetime(2026,9,26,15,30,tzinfo=timezone.utc).timestamp()
SUBJECT='Кабель — наличие и цена [AB-CAB-ABCDEF01]'
JOB={'id':'outbound','token':'lease','recipient':'sales@example.org','subject':SUBJECT,
     'created_at':NOW-120,'positions':[{'line':1,'name':'Кабель АВБбШв 4х150','unit':'пм','quantity':'351.9'}],
     'mapping_trusted':True}


def view(sender='sales@example.org',body='Кабель — 500 руб/м, с НДС. В наличии.',subject=SUBJECT):
    values=['light.mail.ru/message/12345/?folder=0',subject,'Поставщик','<'+sender+'>',
            'Кому:','buyer@mail.ru','Сегодня, 18:30',body,'Быстрый ответ']
    return {'elements':[{'role':'AXStaticText','label':t} for t in values]}


def results(count=1,folder='Отправленные'):
    values=[{'index':-5,'role':'AXStaticText','label':'light.mail.ru/search/?q_query=AB-CAB-ABCDEF01'},
            {'index':-4,'role':'AXStaticText','label':'Найдено во всех папках'},
            {'index':-3,'role':'AXLink','label':str(count)+' '+folder,'bounds':[96,281,230,31]},
            {'index':-2,'role':'AXStaticText','label':'Найдено за все время'},
            {'index':1,'role':'AXStaticText','label':'Результаты поиска'},
            {'index':2,'role':'AXStaticText','label':str(count)+' письмо'}]
    if count:
        values += [{'index':3,'role':'AXRow','label':'','bounds':[90,299,1100,37]},
                   {'index':3,'role':'AXLink','label':'Реклама','bounds':[90,230,1100,40]},
                   {'index':3,'role':'AXLink','label':'Имя','bounds':[100,300,120,32]},
                   {'index':4,'role':'AXLink','label':SUBJECT,'bounds':[250,300,900,32]},
                   {'index':5,'role':'AXLink','label':SUBJECT,'bounds':[250,300,900,32]},
                   {'index':6,'role':'AXLink','label':'Пометить флажком','bounds':[220,309,16,35]}]
    return {'window_title':'Поиск - AB-CAB-ABCDEF01 - buyer@mail.ru - Почта Mail.ru',
            'elements':values+[{'index':10,'role':'AXStaticText','label':'Почтовый ящик:'}]}


class InboxScriptTests(unittest.TestCase):
    def test_search_deduplicates_native_mirrors_and_requires_all_results(self):
        self.assertEqual(len(inbox.search_rows(results(),'AB-CAB-ABCDEF01')),1)
        with self.assertRaises(BuyerError):inbox.search_rows(results(2),'AB-CAB-ABCDEF01')
        with self.assertRaises(BuyerError):inbox.search_rows(results(),'OTHER')
        self.assertEqual(inbox.search_rows(results(0),'AB-CAB-ABCDEF01'),[])

    def test_outgoing_is_never_counted_as_a_supplier_reply(self):
        self.assertIsNone(inbox.message(view(sender='buyer@mail.ru'),JOB,'buyer@mail.ru',NOW,'Europe/Moscow'))

    def test_reply_preserves_header_date_and_literal_price_with_review(self):
        result=inbox.message(view(),JOB,'buyer@mail.ru',NOW,'Europe/Moscow')
        self.assertEqual(result['received_at'],NOW)
        self.assertEqual(result['received_precision'],'minute')
        self.assertEqual(result['subject'],SUBJECT)
        self.assertEqual(result['prices'][0]['price'],'500')
        self.assertEqual(result['prices'][0]['vat'],'с НДС')
        self.assertFalse(result['prices'][0]['exact_match'])

    def test_quoted_customer_content_and_ambiguous_prices_are_not_extracted(self):
        body='Спасибо, уточним.\n> Кабель 600 руб/м с НДС'
        result=inbox.message(view(body=body),JOB,'buyer@mail.ru',NOW,'Europe/Moscow')
        self.assertEqual(result['text'],'Спасибо, уточним.')
        self.assertEqual(result['prices'],[])
        self.assertEqual(inbox.prices('500 руб/м или 600 руб/м',JOB['positions']),[])
        self.assertEqual(inbox.prices('500 руб/м',JOB['positions']*2),[])

    def test_wrong_marker_or_old_date_is_not_this_request(self):
        with self.assertRaises(BuyerError):inbox.message(view(subject='[AB-OTHER]'),JOB,'buyer@mail.ru',NOW,'Europe/Moscow')
        old=view();old['elements'][6]['label']='Вчера, 18:30'
        with self.assertRaises(BuyerError):inbox.message(old,JOB,'buyer@mail.ru',NOW,'Europe/Moscow')

    def test_exact_calendar_date_and_unknown_date(self):
        self.assertEqual(inbox.received_time('26 сентября 2026, 18:30',NOW,'Europe/Moscow'),NOW)
        with self.assertRaises(BuyerError):inbox.received_time('Неизвестно',NOW,'Europe/Moscow')

    def test_completed_search_with_only_our_outgoing_means_no_reply(self):
        with tempfile.TemporaryDirectory() as temp:
            browser=Mock();browser.search.return_value=results();browser.capture.return_value=view(sender='buyer@mail.ru')
            result=inbox.execute(JOB,{'sender_email':'buyer@mail.ru'},Mock(),Path(temp),browser_factory=lambda c:browser)
            self.assertEqual(result['status'],'checked');self.assertEqual(result['messages'],[])
            browser.click.assert_not_called();browser.in_folder.assert_not_called()
            browser.close.assert_called_once()
            self.assertTrue((Path(temp)/'reply.json').exists())

    def test_saved_draft_is_not_opened_and_incomplete_folder_counts_block(self):
        with tempfile.TemporaryDirectory() as temp:
            browser=Mock();browser.search.return_value=results(folder='Черновики')
            result=inbox.execute(JOB,{'sender_email':'buyer@mail.ru'},Mock(),Path(temp),browser_factory=lambda c:browser)
            self.assertEqual(result['status'],'checked');browser.click.assert_not_called()
            state=results();state['elements'][2]['label']='2 Отправленные'
            browser.search.return_value=state
            result=inbox.execute(JOB,{'sender_email':'buyer@mail.ru'},Mock(),Path(temp),browser_factory=lambda c:browser)
            self.assertEqual(result['status'],'blocked')

    def test_incoming_folder_reply_is_collected(self):
        with tempfile.TemporaryDirectory() as temp, patch('time.time',return_value=NOW):
            browser=Mock();browser.search.return_value=results(folder='Входящие')
            browser.in_folder.return_value=results(folder='Входящие');browser.capture.return_value=view()
            result=inbox.execute(JOB,{'sender_email':'buyer@mail.ru'},Mock(),Path(temp),browser_factory=lambda c:browser)
            self.assertEqual(result['status'],'checked');self.assertEqual(len(result['messages']),1)
            browser.in_folder.assert_called_once_with('AB-CAB-ABCDEF01','Входящие',1)

    def test_folder_limited_search_cannot_claim_absence(self):
        state=results();state['elements'][0]['label']+='&q_folder=500000'
        with self.assertRaises(BuyerError):inbox.search_folders(state,'AB-CAB-ABCDEF01')

    def test_incomplete_search_is_error_not_no_reply(self):
        with tempfile.TemporaryDirectory() as temp:
            browser=Mock();browser.search.return_value=results(2)
            result=inbox.execute(JOB,{'sender_email':'buyer@mail.ru'},Mock(),Path(temp),browser_factory=lambda c:browser)
            self.assertEqual(result['status'],'blocked');browser.click.assert_not_called()

    def test_script_mode_does_not_start_a_model(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(buyer_inbox,'sender_client') as model, patch.object(inbox,'execute',return_value={'status':'checked'}) as collect:
            self.assertEqual(buyer_inbox.execute(JOB,{'outbox_dir':temp,'inbox_mode':'mailru_lite_script'},Mock()),{'status':'checked'})
            model.assert_not_called();collect.assert_called_once()


if __name__=='__main__':unittest.main()
