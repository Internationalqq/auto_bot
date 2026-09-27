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
    def test_matching_url_is_not_enough_until_search_counts_render(self):
        browser=object.__new__(inbox.Mailbox)
        complete=results(1)
        incomplete=results(1);incomplete['elements']=[e for e in incomplete['elements'] if e.get('label')!='1 письмо']
        browser.capture=Mock(side_effect=[complete,complete,incomplete,complete]);browser.click=Mock();browser.call=Mock()
        with patch.object(inbox,'search_controls',return_value=({'index':1},{'index':2})):
            self.assertEqual(browser.search('AB-CAB-ABCDEF01'),complete)
        self.assertIn(unittest.mock.call({'action':'wait','seconds':1}),browser.call.call_args_list)

    def test_blank_read_page_recovers_once_without_touching_a_composer(self):
        browser=object.__new__(inbox.Mailbox)
        blank={'window_title':'Поиск - old - buyer@mail.ru - Почта Mail.ru','elements':[{'index':30,'role':'AXButton','label':'Обновить','bounds':[10,10,20,20]}]}
        ready=results(0)
        browser.capture=Mock(side_effect=[blank,ready,ready,ready]);browser.click=Mock();browser.call=Mock()
        controls=({'index':1},{'index':2})
        with patch.object(inbox,'search_controls',side_effect=[BuyerError('blank'),controls,controls]):
            self.assertEqual(browser.search('AB-CAB-ABCDEF01'),ready)
        self.assertEqual(browser.click.call_args_list[0].args[0]['label'],'Обновить')
        browser.capture=Mock(return_value={'window_title':'Новое письмо','elements':[]})
        browser.click.reset_mock()
        with self.assertRaises(BuyerError):browser.search('anything')
        browser.click.assert_not_called()

    def test_search_deduplicates_native_mirrors_and_requires_all_results(self):
        self.assertEqual(len(inbox.search_rows(results(),'AB-CAB-ABCDEF01')),1)
        with self.assertRaises(BuyerError):inbox.search_rows(results(2),'AB-CAB-ABCDEF01')
        with self.assertRaises(BuyerError):inbox.search_rows(results(),'OTHER')
        self.assertEqual(inbox.search_rows(results(0),'AB-CAB-ABCDEF01'),[])

    def test_outgoing_is_never_counted_as_a_supplier_reply(self):
        self.assertIsNone(inbox.message(view(sender='buyer@mail.ru'),JOB,'buyer@mail.ru',NOW,'Europe/Moscow'))

    def test_legacy_shared_subject_does_not_mix_supplier_conversations(self):
        job={**JOB,'subject':'Щебень 20–40 — наличие и цена'}
        self.assertIsNone(inbox.message(view(sender='other@example.org',subject=job['subject']),job,'buyer@mail.ru',NOW,'Europe/Moscow'))
        reply=inbox.message(view(subject=job['subject']),job,'buyer@mail.ru',NOW,'Europe/Moscow')
        self.assertEqual(reply['sender'],JOB['recipient'])

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

    def test_grouped_reply_keeps_request_numbers_and_partial_results(self):
        positions=[{'line':1,'name':'Кабель ВВГнг 3х2,5'},{'line':2,'name':'Муфта концевая КВТ'},{'line':3,'name':'Монтаж светильника'}]
        body='3. Монтаж светильника — 350 руб/шт, без НДС\n1. Кабель ВВГнг 3х2,5 — 120,50 ₽/м, с НДС\n2. Муфта концевая КВТ — нет в наличии'
        result=inbox.prices(body,positions)
        self.assertEqual([p['line'] for p in result],[3,1])
        self.assertEqual([p['vat'] for p in result],['без НДС','с НДС'])
        self.assertTrue(all(p['quote'] in body and p['exact_match'] is False for p in result))

    def test_grouped_reply_requires_number_or_unique_full_name(self):
        positions=[{'line':1,'name':'Кабель АВБбШв 4х150'},{'line':2,'name':'Муфта КВТ'}]
        self.assertEqual(inbox.prices('500 руб/м\n600 руб/шт',positions),[])
        result=inbox.prices('Муфта КВТ: 600 руб/шт\nКабель АВБбШв 4х150: 500 руб/м',positions)
        self.assertEqual([p['line'] for p in result],[2,1])
        self.assertEqual(inbox.prices('1. Муфта КВТ — 600 руб/шт',positions),[])
        self.assertEqual(inbox.prices('9. Что-то другое — 600 руб/шт',positions),[])

    def test_grouped_alternatives_do_not_hide_other_unambiguous_prices(self):
        positions=[{'line':1,'name':'Кабель'},{'line':2,'name':'Муфта'}]
        body='1. Кабель 500 руб/м\n1. Другой вариант 600 руб/м\n2. Муфта 800 руб/шт'
        self.assertEqual([p['line'] for p in inbox.prices(body,positions)],[2])
        self.assertEqual(inbox.prices('1. Кабель 500 руб/м или 600 руб/м',positions),[])

    def test_grouped_vat_is_not_borrowed_from_another_position(self):
        positions=[{'line':1,'name':'Кабель'},{'line':2,'name':'Муфта'}]
        result=inbox.prices('1. Кабель 500 руб/м с НДС\n2. Муфта 600 руб/шт',positions)
        self.assertEqual([p['vat'] for p in result],['с НДС',''])
        result=inbox.prices('Все цены с НДС\n1. Кабель 500 руб/м\n2. Муфта 600 руб/шт',positions)
        self.assertEqual([p['vat'] for p in result],['с НДС','с НДС'])
        result=inbox.prices('1. Кабель 500 руб/м\n2. Муфта: стоимость уточним, без НДС\nДоставка с НДС',positions)
        self.assertEqual([p['vat'] for p in result],[''])

    def test_totals_delivery_and_quoted_prices_never_become_item_prices(self):
        positions=[{'line':1,'name':'Кабель'},{'line':2,'name':'Муфта'}]
        self.assertEqual(inbox.prices('Итого за комплекс 1000 руб/шт\nДоставка 200 руб/м\n> 1. Кабель 500 руб/м',positions),[])
        self.assertEqual(inbox.prices('Доставка 200 руб/м',JOB['positions']),[])
        result=inbox.prices('500.25 руб/м. Доставка завтра.',JOB['positions'])
        self.assertEqual(result[0]['price'],'500.25')

    def test_edited_grouped_request_retains_reply_but_no_price_mapping(self):
        job={**JOB,'mapping_trusted':False,'positions':JOB['positions']*2}
        result=inbox.message(view(body='1. Кабель 500 руб/м\n2. Муфта 100 руб/шт'),job,'buyer@mail.ru',NOW,'Europe/Moscow')
        self.assertEqual(result['prices'],[])
        self.assertIn('Муфта',result['text'])

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
