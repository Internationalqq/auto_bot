from contextlib import contextmanager
from datetime import datetime,timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime
import hashlib
import imaplib
import json
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import Mock,patch

from autobot import buyer_mail_transport as mail, buyer_sender, buyer_inbox
from autobot import buyer_mail_service as service
from autobot.buyer_reply_text import web_reply_fingerprint
from autobot.buyer_worker import LostLease
from autobot.hermes_buyer import BuyerError

NOW=datetime(2026,9,27,12,30,tzinfo=timezone.utc).timestamp()
ACCOUNT='buyer@mail.ru'
JOB={'id':'mail-1','token':'lease-1','status':'sending','recipient':'sales@example.org',
     'subject':'Кабель — цена [AB-CABLE-1234]','body':'Добрый день!\nКабель — 100 м. Какая цена?',
     'created_at':NOW-600,'mapping_trusted':True,
     'positions':[{'line':1,'name':'Кабель','unit':'м','quantity':100}]}


def message(body='Кабель — 500 руб/м с НДС, в наличии.',**headers):
    result=EmailMessage(policy=policy.SMTP)
    default={'From':JOB['recipient'],'To':ACCOUNT,'Subject':'Re: '+JOB['subject'],
             'Date':format_datetime(datetime.fromtimestamp(NOW,timezone.utc)),'Message-ID':'<reply-1@example.org>'}
    for name,value in {**default,**headers}.items():
        if value is not None:result[name]=value
    result.set_content(body)
    return result


@contextmanager
def connection(value):yield value


class MailTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.secret=self.root/'password';self.secret.write_text('test-only-secret');self.secret.chmod(0o600)
        self.config={'sender_mode':'smtp_imap','inbox_mode':'smtp_imap','draft_mode':'template','collect_replies':True,
                     'sender_email':ACCOUNT,'mail_password_file':str(self.secret),'smtp_host':'smtp.mail.ru',
                     'imap_host':'imap.mail.ru','outbox_dir':str(self.root),'worker_id':'server-mail',
                     'queue_token_file':str(self.root/'token'),'queue_url':'https://example.org/autobot/api/agent-market/v1/buyer'}
        self.remote=Mock()
        self.smtp=Mock();self.smtp.mail.return_value=(250,b'ok');self.smtp.rcpt.return_value=(250,b'ok');self.smtp.data.return_value=(250,b'accepted')
        self.connect=patch.object(mail,'smtp_connection',side_effect=lambda _:connection(self.smtp)).start()
        self.addCleanup(patch.stopall)
        self.sent=patch.object(mail,'sent_copy',return_value=True).start()
        patch.object(buyer_sender,'sender_client',side_effect=AssertionError('No agent permitted')).start()

    def execute(self,job=None):return buyer_sender.execute(job or JOB,self.config,self.remote)

    def journal(self):
        return self.root/JOB['id']/hashlib.sha256(JOB['token'].encode()).hexdigest()[:24]/'state.json'

    def parse(self,value=None,**job):
        return mail.parse_message((value or message()).as_bytes(),b'INTERNALDATE "27-Sep-2026 12:30:00 +0000"',{**JOB,**job},ACCOUNT,NOW)

    def collect(self,values,job=None):
        imap=Mock();imap.select.return_value=('OK',[b'2'])
        imap.list.return_value=('OK',[b'(\\HasNoChildren) "/" "INBOX"',b'(\\Sent) "/" "Sent"'])
        with patch.object(mail,'imap_connection',side_effect=lambda _:connection(imap)), \
             patch.object(mail,'search_header',return_value=[str(i).encode() for i in range(len(values))]), \
             patch.object(mail,'read_message',side_effect=lambda _,uid:(values[int(uid)].as_bytes(),b'')), \
             patch.object(mail.time,'time',return_value=NOW):
            return mail.collect(job or JOB,self.config,self.remote,self.root)

    def test_submission_exactly_once_and_reopen_uses_durable_receipt(self):
        receipt=self.execute()
        self.assertEqual(receipt['status'],'sent');self.assertIn('SMTP 250',receipt['evidence'])
        transmitted=BytesParser(policy=policy.default).parsebytes(self.smtp.data.call_args.args[0])
        self.assertEqual(str(transmitted['To']),JOB['recipient']);self.assertEqual(str(transmitted['From']),ACCOUNT)
        self.assertEqual(str(transmitted['Subject']),JOB['subject'])
        self.assertEqual(transmitted.get_content().strip().replace('\r\n','\n'),JOB['body'])
        self.assertEqual(self.execute(),receipt);self.smtp.data.assert_called_once()
        self.assertEqual(json.loads(self.journal().read_text())['phase'],'accepted')

    def test_refused_envelope_never_transmits_data(self):
        self.smtp.mail.return_value=(550,b'test-only-secret must never be logged')
        receipt=self.execute()
        self.assertEqual(receipt['status'],'blocked');self.assertNotIn('test-only-secret',str(receipt))
        self.smtp.rcpt.assert_not_called();self.smtp.data.assert_not_called()

    def test_refused_recipient_never_transmits_data(self):
        self.smtp.rcpt.return_value=(550,b'No mailbox')
        self.assertEqual(self.execute()['status'],'blocked');self.smtp.data.assert_not_called()

    def test_lost_lease_before_data_never_sends(self):
        self.remote.request.side_effect=LostLease('lease lost')
        self.assertEqual(self.execute()['status'],'blocked');self.smtp.data.assert_not_called()

    def test_explicit_data_rejection_is_safe_to_retry_manually(self):
        self.smtp.data.side_effect=smtplib.SMTPDataError(554,b'rejected')
        self.assertEqual(self.execute()['status'],'blocked')

    def test_disconnect_during_data_is_uncertain_and_never_retried(self):
        self.smtp.data.side_effect=smtplib.SMTPServerDisconnected('test-only-secret')
        first=self.execute()
        self.assertEqual(first['status'],'uncertain');self.assertNotIn('test-only-secret',str(first))
        self.assertEqual(self.execute(),first);self.smtp.data.assert_called_once();self.sent.assert_not_called()

    def test_restart_after_possible_transmission_only_searches_sent(self):
        self.smtp.data.side_effect=SystemExit('simulated crash')
        with self.assertRaises(SystemExit):self.execute()
        self.sent.return_value=False
        self.assertEqual(self.execute()['status'],'uncertain')
        self.smtp.data.assert_called_once();self.sent.assert_called_once()
        self.assertFalse(self.sent.call_args.kwargs['append'])

    def test_restart_recovers_sent_copy_without_resend(self):
        self.smtp.data.side_effect=SystemExit('simulated crash')
        with self.assertRaises(SystemExit):self.execute()
        self.assertEqual(self.execute()['status'],'sent');self.smtp.data.assert_called_once()

    def test_restart_after_smtp_acceptance_never_repeats_smtp(self):
        self.sent.side_effect=SystemExit('crash before complete')
        with self.assertRaises(SystemExit):self.execute()
        self.sent.side_effect=None
        self.assertEqual(self.execute()['status'],'sent');self.smtp.data.assert_called_once()

    def test_smtp_acceptance_survives_sent_copy_outage(self):
        self.sent.side_effect=BuyerError('IMAP unavailable')
        self.assertEqual(self.execute()['status'],'sent');self.smtp.data.assert_called_once()

    def test_foreign_journal_never_falls_back_to_agent_or_smtp(self):
        target=self.journal();target.parent.mkdir(parents=True)
        buyer_sender.save(target,{'run_id':'old-agent-run','started_at':NOW})
        self.assertEqual(self.execute()['status'],'uncertain');self.connect.assert_not_called()

    def test_changed_payload_cannot_reuse_existing_attempt(self):
        self.sent.side_effect=SystemExit('crash')
        with self.assertRaises(SystemExit):self.execute()
        self.assertEqual(self.execute({**JOB,'body':'changed'})['status'],'uncertain')
        self.smtp.data.assert_called_once()

    def test_expired_job_without_journal_is_not_sent(self):
        self.assertEqual(self.execute({**JOB,'status':'uncertain'})['status'],'uncertain')
        self.connect.assert_not_called()

    def test_header_injection_and_multiple_recipients_blocked(self):
        for changes in ({'recipient':'one@example.org,two@example.org'},{'subject':'Hello\nBcc: other@example.org'}):
            with self.subTest(changes=changes),self.assertRaises(BuyerError):mail.outgoing({**JOB,**changes},ACCOUNT)

    def test_literal_price_parsed_but_manual_mapping_not_guessed(self):
        incoming=self.parse()
        self.assertEqual(incoming['prices'][0]['price'],'500');self.assertTrue(incoming['prices'][0]['exact_match'])
        self.assertEqual(self.parse(mapping_trusted=False)['prices'],[])

    def test_outgoing_wrong_subject_wrong_recipient_old_message_ignored(self):
        for headers in ({'From':ACCOUNT},{'Subject':'Unrelated'},{'To':'other@example.org'},
                        {'Date':format_datetime(datetime.fromtimestamp(NOW-3600,timezone.utc))}):
            with self.subTest(headers=headers):self.assertIsNone(self.parse(message(**headers)))

    def test_legacy_shared_subject_only_matches_original_supplier(self):
        self.assertIsNone(self.parse(message(From='other@example.org',Subject='Re: Cable'),subject='Cable'))
        self.assertIsNotNone(self.parse(message(Subject='Re: Cable'),subject='Cable'))

    def test_quoted_request_prices_never_become_supplier_prices(self):
        incoming=self.parse(message('Уточните адрес.\n\nFrom: Buyer\nКабель 100 руб/м с НДС'))
        self.assertEqual(incoming['text'],'Уточните адрес.');self.assertEqual(incoming['prices'],[])

    def test_html_cyrillic_and_embedded_attachment_are_handled(self):
        value=message();value.clear_content();value.set_content('<p>Цена 450 руб/м с НДС</p><blockquote>Цена 99 руб/м</blockquote>',subtype='html')
        self.assertEqual(self.parse(value)['prices'][0]['price'],'450')
        attachment=message('Старая цена 999 руб/м с НДС')
        value=message('Уточните адрес.');value.add_attachment(attachment)
        self.assertEqual(self.parse(value)['text'],'Уточните адрес.')
        self.assertEqual(self.parse(value)['prices'],[])

    def test_attachment_only_reply_preserves_fact_without_fake_price(self):
        value=message('');value.add_attachment(b'content',maintype='application',subtype='pdf',filename='price.pdf')
        result=self.parse(value)
        self.assertIn('price.pdf',result['text']);self.assertEqual(result['prices'],[])

    def test_date_fallback_and_no_invented_date(self):
        self.assertEqual(self.parse(message(Date=None))['received_at'],NOW)
        with self.assertRaises(BuyerError):mail.parse_message(message(Date=None).as_bytes(),b'',JOB,ACCOUNT,NOW)
        with self.assertRaises(BuyerError):self.parse(message(Date=format_datetime(datetime.fromtimestamp(NOW+600,timezone.utc))))

    def test_duplicate_folder_messages_are_returned_once_and_inbox_readonly(self):
        result=self.collect([message()])
        self.assertEqual(result['status'],'checked');self.assertEqual(len(result['messages']),1)
        self.assertEqual(len(self.remote.request.call_args_list),4)

    def test_conflicting_message_id_does_not_publish_partial_result(self):
        result=self.collect([message(),message('Другая цена 900 руб/м')])
        self.assertEqual(result['status'],'blocked');self.assertEqual(result['messages'],[])

    def test_old_web_reply_not_duplicated_after_transport_switch(self):
        known=web_reply_fingerprint(JOB['recipient'],'Кабель — 500 руб/м с НДС, в наличии.',NOW)
        result=self.collect([message()],{**JOB,'known_web_replies':[known]})
        self.assertEqual(result['status'],'checked');self.assertEqual(result['messages'],[])

    def test_two_distinct_same_minute_replies_require_review_not_silent_drop(self):
        known=web_reply_fingerprint(JOB['recipient'],'Кабель — 500 руб/м с НДС, в наличии.',NOW)
        result=self.collect([message(),message(**{'Message-ID':'<reply-2@example.org>'})],{**JOB,'known_web_replies':[known]})
        self.assertEqual(result['status'],'blocked')

    def test_known_web_reply_fingerprint_uses_sender_body_and_minute(self):
        original=web_reply_fingerprint('X@example.org',' text\n here ',NOW)
        self.assertEqual(original,web_reply_fingerprint('x@example.org','text here',NOW+40))
        for sender,body,stamp in [('y@example.org','text here',NOW),('x@example.org','changed',NOW),('x@example.org','text here',NOW+60)]:
            self.assertNotEqual(original,web_reply_fingerprint(sender,body,stamp))

    def test_mailru_russian_quote_header_does_not_duplicate_legacy_reply(self):
        body='Добрый день, укажите адрес объекта\nС уважением, ООО Поставщик'
        legacy=body+'\nСуббота, 26 сентября 2026, 11:42 +03:00 от Иван <buyer@mail.ru>\nКабель — 500 руб/м'
        known=web_reply_fingerprint(JOB['recipient'],legacy,NOW)
        result=self.collect([message(body)],{**JOB,'known_web_replies':[known]})
        self.assertEqual(result['status'],'checked')
        self.assertEqual(result['messages'],[])

    def test_inbox_transport_does_not_touch_legacy_agent(self):
        with patch.object(buyer_inbox,'sender_client',side_effect=AssertionError('No agents')),patch.object(mail,'collect',return_value={'status':'checked','messages':[]}) as collector:
            self.assertEqual(buyer_inbox.execute(JOB,self.config,self.remote)['status'],'checked')
            collector.assert_called_once()

    def test_fetch_prechecks_size_and_uses_peek(self):
        imap=Mock();raw=message().as_bytes()
        imap.uid.side_effect=[('OK',[b'1 (RFC822.SIZE '+str(len(raw)).encode()+b')']),('OK',[(b'1 (BODY[]',raw),b')'])]
        self.assertEqual(mail.read_message(imap,b'1')[0],raw)
        self.assertEqual(imap.uid.call_args.args,('fetch',b'1','(BODY.PEEK[] INTERNALDATE)'))
        imap.reset_mock();imap.uid.side_effect=[('OK',[b'1 (RFC822.SIZE 2000001)'])]
        with self.assertRaises(BuyerError):mail.read_message(imap,b'1')
        imap.uid.assert_called_once()

    def test_failed_search_is_not_claimed_as_no_replies(self):
        imap=Mock();imap.uid.return_value=('NO',[b'No search'])
        with self.assertRaises(BuyerError):mail.search_header(imap,'Subject',JOB['subject'])
        with patch.object(mail,'imap_connection',side_effect=imaplib.IMAP4.error('secret')):
            result=mail.collect(JOB,self.config,self.remote,self.root)
        self.assertEqual(result['status'],'blocked');self.assertNotIn('secret',str(result))

    def test_sent_verification_checks_full_body_including_quoted_tail(self):
        # Call the real helper while the sender tests use a mock.
        patch.stopall()
        raw,identifier=mail.outgoing(JOB,ACCOUNT)
        changed=BytesParser(policy=policy.default).parsebytes(raw)
        changed.set_content(JOB['body']+'\nFrom: Someone\nextra content')
        imap=Mock();imap.select.return_value=('OK',[])
        imap.list.return_value=('OK',[b'(\\Sent) "/" "Sent"'])
        with patch.object(mail,'imap_connection',side_effect=lambda _:connection(imap)),patch.object(mail,'search_header',return_value=[b'1']),patch.object(mail,'read_message',return_value=(changed.as_bytes(),b'')):
            with self.assertRaises(BuyerError):mail.sent_copy(self.config,identifier,raw,append=False)
        imap.append.assert_not_called()

    def test_secret_permission_and_error_sanitizing(self):
        self.assertEqual(mail.settings(self.config)[0],ACCOUNT)
        self.assertNotIn('password123',mail.failure(smtplib.SMTPAuthenticationError(535,b'password123'),'SMTP'))
        self.secret.unlink()
        with self.assertRaises(BuyerError):mail.settings(self.config)

    def test_preflight_authenticates_both_tls_connections_without_sending_or_reading(self):
        patch.stopall()
        smtp=Mock();imap=Mock()
        with patch.object(mail.smtplib,'SMTP_SSL',return_value=smtp) as outbound,patch.object(mail.imaplib,'IMAP4_SSL',return_value=imap) as inbound:
            self.assertEqual(mail.check_connection(self.config),{'smtp':True,'imap':True,'account':ACCOUNT})
        self.assertTrue(outbound.call_args.kwargs['context'].check_hostname)
        self.assertTrue(inbound.call_args.kwargs['ssl_context'].check_hostname)
        smtp.login.assert_called_once_with(ACCOUNT,'test-only-secret');imap.login.assert_called_once_with(ACCOUNT,'test-only-secret')
        smtp.data.assert_not_called();smtp.mail.assert_not_called();imap.uid.assert_not_called()

    def test_starttls_must_complete_before_authentication(self):
        patch.stopall()
        smtp=Mock();smtp.starttls.side_effect=smtplib.SMTPNotSupportedError('TLS unavailable')
        with patch.object(mail.smtplib,'SMTP',return_value=smtp):
            with self.assertRaises(smtplib.SMTPNotSupportedError):
                with mail.smtp_connection({**self.config,'smtp_security':'starttls'}):pass
        smtp.login.assert_not_called();smtp.data.assert_not_called()

    def test_service_requires_explicit_script_transports(self):
        configfile=self.root/'config.json'
        for changes in ({'sender_mode':'hermes'},{'inbox_mode':'mailru_lite_script'},{'draft_mode':'hermes'}):
            configfile.write_text(json.dumps({**self.config,**changes}))
            with self.assertRaises(BuyerError):service.load_config(configfile)

    def test_service_does_not_claim_jobs_before_preflight(self):
        drafts=Mock()
        runner=service.Service(self.config,self.remote,drafts)
        with patch.object(service,'check_connection',side_effect=BuyerError('SMTP unavailable')),patch.object(service,'process_next') as next_job:
            self.assertEqual(runner.run(once=True),1);next_job.assert_not_called()
        self.remote.request.assert_not_called();drafts.step.assert_not_called()

    def test_service_processes_templates_and_mail_without_agents(self):
        drafts=Mock();drafts.step.return_value='completed'
        runner=service.Service(self.config,self.remote,drafts)
        with patch.object(service,'check_connection') as check,patch.object(service,'process_next',return_value=('inbox','checked')):
            state=runner.step()
            self.assertEqual((state['direction'],state['result'],state['draft']),('inbox','checked','completed'))
            self.assertTrue(state['receiving']);self.assertTrue(state['sending'])
            runner.step();self.assertEqual(check.call_count,2)
        self.assertFalse(runner.prefer_inbox)

    def test_smtp_outage_keeps_imap_working_without_claiming_send(self):
        drafts=Mock()
        runner=service.Service(self.config,self.remote,drafts)
        def health(config,*,protocols):
            if protocols==('SMTP',):raise BuyerError('SMTP unavailable')
        self.remote.request.return_value={'job':None}
        with patch.object(service,'check_connection',side_effect=health) as check:
            state=runner.step();runner.step()
        self.assertTrue(state['receiving']);self.assertFalse(state['sending'])
        self.assertEqual(state['send_detail'],'SMTP unavailable')
        self.assertEqual(check.call_count,2)  # independent health checks are cached
        self.assertEqual([c.args[0] for c in self.remote.request.call_args_list],['/inbox/claim']*2)
        drafts.step.assert_not_called()

    def test_inbox_only_does_not_connect_smtp_or_run_drafts(self):
        drafts=Mock();runner=service.Service(self.config,self.remote,drafts,inbox_only=True)
        self.remote.request.return_value={'job':None}
        with patch.object(service,'check_connection') as check:
            state=runner.step()
        check.assert_called_once_with(self.config,protocols=('IMAP',))
        self.remote.request.assert_called_once_with('/inbox/claim')
        drafts.step.assert_not_called();self.assertFalse(state['sending'])

    def test_smtp_recovery_restores_sending_after_backoff(self):
        runner=service.Service(self.config,self.remote,Mock())
        self.remote.request.return_value={'job':None}
        with patch.object(service.time,'time',return_value=100),patch.object(service,'check_connection',side_effect=[None,BuyerError('SMTP unavailable')]):
            self.assertFalse(runner.step()['sending'])
        with patch.object(service.time,'time',return_value=161),patch.object(service,'check_connection') as check:
            self.assertTrue(runner.step()['sending'])
        check.assert_called_once_with(self.config,protocols=('SMTP',))

    def test_password_setup_rejects_noninteractive_input(self):
        with patch.object(service.sys.stdin,'isatty',return_value=False),patch.object(service.getpass,'getpass') as prompt:
            with self.assertRaises(BuyerError):service.configure(self.config)
            prompt.assert_not_called()

    def test_password_setup_is_atomic_private_and_not_logged(self):
        with patch.object(service.sys.stdin,'isatty',return_value=True),patch.object(service.getpass,'getpass',side_effect=['new secret','new secret']),patch('builtins.print') as output:
            service.configure(self.config)
        self.assertEqual(self.secret.read_text(),'new secret');self.assertNotIn('new secret',str(output.call_args_list))


if __name__=='__main__':unittest.main()
