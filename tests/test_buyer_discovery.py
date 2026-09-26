from contextlib import closing, nullcontext
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from autobot import buyer_needs as needs, buyer_store as store, buyer_discovery as discovery
from autobot import buyer_outbox as box, buyer_jobs as jobs, buyer_suppliers as suppliers
from autobot import buyer_workflow as workflow, buyer_report as report, buyer_campaigns as campaigns
from autobot.hermes_buyer import BuyerError


def row(key='c', name='Кабель ВВГнг-LS 3х2,5', kind='material'):
    return dict(position_key=key, name=name, type_slug=kind, quantity='10.4', unit='100 м',
                section='Раздел 1', price=987654, budget=123456,
                specification={'secret':'PRIVATE', 'requirements':{'specifications':[
                    {'kind':'grade','label':'Марка','value':'ВВГнг-LS','estimate_price':777777}]}})


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(box,'DB_PATH',Path(self.tmp.name)/'outbox.db'),
                  patch.object(jobs,'DB_PATH',Path(self.tmp.name)/'jobs.db')):
            p.start(); self.addCleanup(p.stop)
        self.source = dict(tender_id='123456789012345', region='Москва', positions=[row()])
        def current(tid, keys=None):
            return nullcontext(self.source | {'positions':[p for p in self.source['positions'] if keys is None or p['position_key'] in keys]})
        p = patch.object(workflow, 'current_source', side_effect=current);p.start();self.addCleanup(p.stop)
        p = patch.object(campaigns, 'launch');p.start();self.addCleanup(p.stop)

    def test_snapshot_allowlist_excludes_money_and_preserves_scaled_units(self):
        data = needs.snapshot(self.source)
        for secret in ('987654','123456,','777777','PRIVATE','estimate_price'):
            self.assertNotIn(secret,json.dumps(data))
        self.assertEqual(data['positions'][0]['quantity'],'10.4')
        self.assertEqual(data['positions'][0]['unit'],'100 м')
        self.assertEqual(data['positions'][0]['specification']['requirements']['specifications'][0]['value'],'ВВГнг-LS')

    def test_invalid_rows_retained_as_reasons_not_queries(self):
        self.source['positions'] += [dict(row('negative'),quantity=-1),dict(row('total'),type_slug='aggregate')]
        data = needs.snapshot(self.source)
        self.assertEqual(len(data['positions']),1)
        self.assertEqual(len(data['rejected']),2)

    def test_duplicate_ids_across_batches_are_rejected(self):
        self.source['positions'] = [row(str(i)) for i in range(101)] + [row('0')]
        with self.assertRaises(BuyerError): needs.snapshot(self.source)

    def test_works_have_service_and_avito_queries(self):
        self.source['positions'] = [row('w','Монтаж электропроводки','work')]
        queries = needs.queries(needs.snapshot(self.source))
        self.assertEqual(len(queries),2)
        self.assertTrue(all(q['bucket']=='works' for q in queries))
        self.assertTrue(any('site:avito.ru' in q['query'] for q in queries))

    def test_repeat_and_reopen_resume_one_snapshot(self):
        first = store.enqueue(self.source)
        self.assertEqual(first,store.enqueue(copy.deepcopy(self.source)))
        self.assertEqual(store.listing(self.source['tender_id'])[0]['id'],first)
        self.source['positions'][0]['quantity']='20'
        self.assertNotEqual(first,store.enqueue(self.source))

    def test_business_date_computed_at_enqueue(self):
        with patch.object(store,'today_iso',return_value='2030-01-02'):
            store.enqueue(self.source)
        self.assertEqual(store.listing(self.source['tender_id'])[0]['business_date'],'2030-01-02')

    def test_search_result_and_followups_commit_once(self):
        store.enqueue(self.source); step=store.claim()
        link={'url':'https://supplier.example/catalog','title':'Магазин'}
        self.assertTrue(store.finish(step,links=[link,link]))
        self.assertFalse(store.finish(step,links=[link]))
        following=store.claim()
        while following['kind']=='search':
            store.finish(following,links=[link]);following=store.claim()
        self.assertEqual(following['kind'],'inspect')
        self.assertEqual(following['payload']['url'],link['url'])

    def test_stale_lease_cannot_publish_results(self):
        store.enqueue(self.source); old=store.claim()
        with closing(box.connect()) as db,db: db.execute('UPDATE buyer_search_steps SET lease_until=0')
        current=store.claim()
        self.assertNotEqual(old['token'],current['token'])
        self.assertFalse(store.finish(old))
        self.assertTrue(store.finish(current))

    def test_canceled_run_rejects_inflight_result_and_resumes(self):
        key=store.enqueue(self.source); step=store.claim()
        store.cancel(self.source['tender_id'])
        self.assertFalse(store.finish(step))
        self.assertIsNone(store.claim())
        store.retry(self.source['tender_id'],key)
        self.assertIsNotNone(store.claim())

    def test_error_distinct_from_empty_success_and_retry(self):
        key=store.enqueue(self.source); step=store.claim()
        store.finish(step,error='Источник недоступен')
        store.finish(store.claim());store.settle()
        discovery.run_once()
        self.assertEqual(store.source(self.source['tender_id'],key)['status'],'partial')
        store.retry(self.source['tender_id'],key)
        store.finish(store.claim());discovery.run_once();store.settle()
        self.assertEqual(store.source(self.source['tender_id'],key)['status'],'completed')

    def test_wrong_tender_cannot_read_or_retry_search(self):
        key=store.enqueue(self.source)
        with self.assertRaises(BuyerError): store.source('999999999999',key)
        with self.assertRaises(BuyerError): store.retry('999999999999',key)

    def test_contacts_only_from_actual_page(self):
        data=discovery.page_facts('https://supplier.example/','<h1>Поставщик</h1><a href="mailto:sales@example.org">Написать</a><a href="tel:+79990000000">Позвонить</a>')
        self.assertEqual(data['emails'],['sales@example.org'])
        self.assertEqual([c['channel'] for c in data['channels']],['phone'])

    def test_privacy_operator_and_site_creator_are_not_supplier_contacts(self):
        html = '''<h1>Электромонтажные работы</h1>
        <div><ol><li>Даю согласие оператору на обработку персональных данных,
        email: info@agency.example</li><li>Остальной текст согласия</li></ol></div>
        <p>Разработка сайта <a href="mailto:info@studio.example">Студия</a></p>
        <p><a href="mailto:info@supplier.example">info@supplier.example</a></p>
        <p>Менеджер: supplier-sales@yandex.ru</p><!-- archived@old.example -->'''
        facts = discovery.page_facts('https://supplier.example/', html)
        self.assertEqual(facts['emails'], ['info@supplier.example','supplier-sales@yandex.ru'])
        old = {'email':'info@agency.example','evidence_pages':[{'url':'https://supplier.example/'}]}
        with patch.object(discovery,'fetch_html',return_value=('https://supplier.example/',html)):
            with self.assertRaises(BuyerError): discovery.verify_contact(old)

    def test_explicit_rot13_mailto_is_decoded_without_running_site_script(self):
        html = '<a href="znvygb:vasb@ryrpgeb-neg.pbz">vasb@ryrpgeb-neg.pbz</a><script>untrusted()</script>'
        facts = discovery.page_facts('https://electro-art.com/',html)
        self.assertEqual(facts['emails'],['info@electro-art.com'])
        self.assertNotIn('vasb@',facts['text'])

    def test_supplier_alias_email_is_not_rejected_only_for_different_domain(self):
        facts = discovery.page_facts('https://supplier.example/',
            '<address>E-mail: <a href="mailto:sales@trade.example">sales@trade.example</a></address>')
        self.assertEqual(facts['emails'],['sales@trade.example'])

    def test_directory_support_is_not_a_supplier_or_verifiable_old_recipient(self):
        for url in ('https://yar.spravker.ru/kabel/','https://www.rusprofile.ru/id/1',
                    'https://2gis.ru/yaroslavl/search/electro','https://vsem-podryad.ru/purchase/1'):
            with self.subTest(url=url):
                with self.assertRaises(BuyerError):
                    discovery.page_facts(url,'Кабель. Электромонтажные работы support@portal.example')
                with patch.object(discovery,'fetch_html',return_value=(url,'Кабель support@portal.example')):
                    with self.assertRaises(BuyerError): discovery.verify_contact({
                        'email':'support@portal.example','evidence_pages':[{'url':url}]})
        self.assertFalse(discovery.directory_source('https://spravker.ru.supplier.example/'))

    def test_search_keeps_ten_supplier_domains_after_excluding_directories(self):
        found = [SimpleNamespace(url='https://yar.spravker.ru/kabel/',title='Каталог')]
        found += [SimpleNamespace(url=f'https://supplier{i}.example/',title=str(i)) for i in range(12)]
        with patch.object(discovery,'search_api',return_value=found):
            links=discovery.search('кабель')
        self.assertEqual([x['title'] for x in links],[str(i) for i in range(10)])

    def test_avito_query_never_accepts_a_providers_unrelated_fallback_results(self):
        found=[SimpleNamespace(url=url,title='Электрик') for url in (
            'https://article.example/','https://avito.ru.fake.example/x',
            'https://www.avito.ru/yaroslavl/electrician_123456789',
            'https://m.avito.ru/yaroslavl/electrician_123456790')]
        with patch.object(discovery,'search_api',return_value=found):
            links=discovery.search('site:avito.ru электромонтаж Ярославль')
        self.assertEqual(len(links),2)
        self.assertTrue(all('electrician_' in x['url'] for x in links))

    def test_product_query_uses_written_model_and_qualification_rejects_wrong_model(self):
        self.source['positions']=[dict(row('clamp','Сжим типа У733М для магистральных и ответвительных проводов и кабелей'),unit='100 шт')]
        source=needs.snapshot(self.source)
        task=needs.queries(source)[-1]|{'url':'https://supplier.example/product'}
        self.assertEqual(task['intent'],'product')
        self.assertIn('"У733М"',task['query'])
        self.assertNotIn('для магистральных',task['query'])
        wrong='<h1>Сжим У734М</h1>Купить товар, в наличии. sales@supplier.example'
        with self.assertRaisesRegex(BuyerError,'модель'):
            discovery.inspect(task,source,fetch=lambda url:(url,wrong))
        correct=wrong.replace('У734М','У733М')
        self.assertEqual(discovery.inspect(task,source,fetch=lambda url:(url,correct))['position_keys'],['clamp'])

    def test_article_navigation_does_not_prove_a_supplier_profile(self):
        data=needs.snapshot(self.source);task=needs.queries(data)[0]|{'url':'https://article.example/guide'}
        html='<title>Драйверы видеокарты</title><h1>Настройки компьютера</h1><main>Новости технологий</main><footer>Продажа кабеля ВВГнг-LS. admin@article.example</footer>'
        with self.assertRaises(BuyerError): discovery.inspect(task,data,fetch=lambda url:(url,html))
        with self.assertRaises(BuyerError): discovery.inspect(task,data,fetch=lambda url:(url,'<h1>Кабель</h1>Энциклопедическое определение. admin@article.example'))

    def test_service_instruction_is_not_a_contractor_offer(self):
        self.source['positions']=[row('work','Монтаж электропроводки','work')]
        data=needs.snapshot(self.source);task=needs.queries(data)[0]|{'url':'https://article.example/how'}
        html='<h1>Как смонтировать электропроводку</h1>Электромонтажные работы своими руками, стоимость услуг. admin@article.example'
        with self.assertRaises(BuyerError): discovery.inspect(task,data,fetch=lambda url:(url,html))
        offer='<h1>Электромонтажные работы</h1>Оказываем услуги в Москве. Оставьте заявку. office@supplier.example'
        self.assertEqual(discovery.inspect(task,data,fetch=lambda url:(url,offer))['position_keys'],['work'])

    def test_new_qualification_policy_requires_new_run_and_blocks_old_send(self):
        run_id=self.pipeline()
        job=jobs.jobs(self.source['tender_id'])[0]
        with patch.object(store,'DISCOVERY_VERSION',store.DISCOVERY_VERSION+1):
            fresh=store.enqueue(self.source)
            self.assertNotEqual(run_id,fresh)
            self.assertFalse(report.build(self.source['tender_id'],run_id)['request_current'])
            with self.assertRaisesRegex(BuyerError,'Правила проверки'):
                box.enqueue(self.source['tender_id'],job['id'],0,'sales@example.org')
            with self.assertRaisesRegex(BuyerError,'Правила проверки'):
                workflow.prepare_run(self.source['tender_id'],run_id)

    def test_contact_anchor_does_not_refetch_the_same_page(self):
        facts=discovery.page_facts('https://supplier.example/',
            '<a href="#contacts">Контакты</a><a href="/contacts">Контакты</a>')
        self.assertEqual(facts['links'],['https://supplier.example/contacts'])

    def test_contact_redirect_cannot_mix_another_website_into_the_company(self):
        main='<h1>Продажа кабеля</h1>Кабель ВВГнг. sales@supplier.example <a href="/contacts">Контакты</a>'
        fetch=Mock(side_effect=[('https://supplier.example/',main),
                               ('https://another.example/','Кабель info@another.example')])
        candidate=discovery.inspect({'url':'https://supplier.example/','position_keys':['c'],
            'bucket':'materials','category':'cable'},needs.snapshot(self.source),fetch=fetch)
        self.assertEqual(candidate['emails'],['sales@supplier.example'])
        self.assertEqual(len(candidate['evidence_pages']),1)

    def test_supplier_identity_and_photo_are_grounded_in_page_metadata(self):
        facts = discovery.page_facts('https://supplier.example/catalog/item', '<meta property="og:site_name" content="Кабельная компания"><meta property="og:image" content="/media/cable.jpg"><h1>Кабель 3х2,5</h1>')
        self.assertEqual(facts['name'],'Кабельная компания')
        self.assertEqual(facts['image'], {'url':'https://supplier.example/media/cable.jpg','source_url':'https://supplier.example/catalog/item'})
        fallback = discovery.page_facts('https://supplier.example/', '<h1>Купить кабель</h1><meta property="og:image" content="file:///secret">')
        self.assertEqual(fallback['name'],'supplier.example')
        self.assertIsNone(fallback['image'])

    def test_image_cache_rejects_markup_even_with_image_content_type(self):
        from autobot import buyer_media as media
        media.thumbnail.cache_clear()
        with patch.object(media,'fetch_public',return_value=('https://supplier.example/x', b'<html>secret</html>','image/png')):
            self.assertIsNone(media.thumbnail('https://supplier.example/x',0))
        with patch.object(media,'fetch_public',return_value=('https://supplier.example/p', b'\x89PNG\r\n\x1a\nimage','image/png')) as fetch:
            self.assertEqual(media.thumbnail('https://supplier.example/p',0)[1],'image/png')
            media.thumbnail('https://supplier.example/p',0)
            self.assertEqual(fetch.call_count,1)
        media.thumbnail.cache_clear()

    def test_images_share_private_network_protection(self):
        with patch.object(discovery.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',80))]),patch.object(discovery.socket,'create_connection') as connect:
            with self.assertRaises(BuyerError): discovery.fetch_public('http://supplier.example/p.png',image=True)
            connect.assert_not_called()

    def test_supplier_image_requires_session_saved_candidate_and_correct_tender(self):
        from flask import Flask
        from autobot import buyer_routes as routes, buyer_media as media
        from autobot.uploaded_corrections import CorrectionError
        key=self.pipeline(html='<meta property="og:image" content="/photo.png">Продажа кабеля ВВГнг-LS 3х2,5 в Москве. sales@example.org')
        company=report.build(self.source['tender_id'],key)['companies'][0]
        self.assertEqual(company['draft_job_ids'],[jobs.jobs(self.source['tender_id'])[0]['id']])
        path=company['image_url']
        app=Flask(__name__);app.register_blueprint(routes.blueprint)
        with app.test_client() as client, patch.object(media,'thumbnail',return_value=(b'\x89PNG\r\n\x1a\nimage','image/png')) as fetch:
            with patch.object(routes.crm_actor,'resolve',side_effect=CorrectionError('Нет доступа',401)):
                self.assertEqual(client.get(path).status_code,401)
                fetch.assert_not_called()
            with patch.object(routes.crm_actor,'resolve',return_value={}):
                self.assertEqual(client.get(path.replace(self.source['tender_id'],'99999999999')).status_code,400)
                self.assertEqual(client.get(path.replace(company['id'],'unknown')).status_code,404)
                fetch.assert_not_called()
                response=client.get(path)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.content_type,'image/png')
                self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
                self.assertEqual(fetch.call_args.args[0],'https://supplier.example/photo.png')

    def test_dangerous_urls_rejected(self):
        for url in ('file:///etc/passwd','http://user:pass@example.org','http://example.org:8080/','https://a.example\\@localhost/'):
            with self.subTest(url=url),self.assertRaises(BuyerError): discovery.public_url(url)

    def test_private_dns_never_connects(self):
        with patch.object(discovery.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',80))]),patch.object(discovery.socket,'create_connection') as connect:
            with self.assertRaises(BuyerError):discovery.fetch_html('http://supplier.example')
            connect.assert_not_called()

    def test_cyrillic_urls_are_encoded_for_http_without_double_encoding(self):
        value=discovery.public_url('https://пример.рф/каталог?q=ВВГнг+3х2,5&path=%2F')
        self.assertTrue(value.isascii())
        self.assertIn('path=%2F',value)
        self.assertEqual(discovery.public_url(value),value)

    def test_redirect_to_private_dns_is_blocked(self):
        responses=[[(2,1,6,'',('93.184.216.34',80))],[(2,1,6,'',('10.0.0.1',80))]]
        connection=Mock(); response=Mock(status=302)
        response.getheader.return_value='http://private.example/';connection.getresponse.return_value=response
        with patch.object(discovery.socket,'getaddrinfo',side_effect=responses),patch.object(discovery.socket,'create_connection') as connect,patch.object(discovery.http.client,'HTTPConnection',return_value=connection):
            with self.assertRaises(BuyerError): discovery.fetch_html('http://supplier.example')
            self.assertEqual(connect.call_count,1)

    def test_ten_distinct_sources_and_no_snippet_price(self):
        from autobot import real_market_scraper as scraper
        offers=[{'href':f'https://s{i}.example/product','title':f'Supplier {i}','body':'Цена 1 руб'} for i in range(12)]
        with patch.object(scraper,'_ddgs_text',return_value=offers),patch.dict('os.environ',{'BUYER_SEARCH_API_KEY':'','BUYER_SEARCH_FOLDER_ID':''}):
            links=discovery.search('кабель Москва')
        self.assertEqual(len(links),10)
        self.assertNotIn('price',links[0])

    def test_unrelated_site_rejected(self):
        data=needs.snapshot(self.source);task=needs.queries(data)[0]|{'url':'https://example.org'}
        with self.assertRaises(BuyerError): discovery.inspect(task,data,fetch=lambda url:(url,'<h1>Садовые цветы</h1>Семена розы sales@example.org'))

    def test_candidate_source_and_grouped_draft_with_second_region(self):
        self.source['positions'].append(dict(row('c2'),name='Кабель АВВГ 4х16',section='Другой раздел'))
        data=needs.snapshot(self.source); task=needs.queries(data)[0]|{'url':'https://supplier.example/'}
        supplier=discovery.inspect(task,data,fetch=lambda url:(url,'<h1>Электроматериалы</h1><p>Кабель ВВГнг-LS и кабели АВВГ. Продажа в Москве.</p><a href="mailto:sales@example.org">Email</a>'))
        self.assertEqual(supplier['position_keys'],['c','c2'])
        self.assertTrue(supplier['evidence_pages'][0]['sha256'])
        result=suppliers.prepare(self.source,discovered=[supplier])
        self.assertEqual(result['supplier_count'],1)
        self.assertEqual(result['position_count'],2)
        job=jobs.jobs(self.source['tender_id'])[0]
        self.assertEqual(job['result']['drafts'][0]['position_keys'],['c','c2'])
        self.assertNotIn('987654',job['result']['drafts'][0]['body'])
        self.assertEqual(result['job_ids'],suppliers.prepare(self.source,discovered=[supplier])['job_ids'])

    def test_reading_without_discovery_does_not_create_tables(self):
        self.assertEqual(store.listing(self.source['tender_id']),[])
        self.assertFalse(box.DB_PATH.exists())

    def pipeline(self, delivery='draft', html=None):
        html = html or '<h1>Электроматериалы</h1>Кабель ВВГнг и светильники в Москве. <a href="mailto:sales@example.org">Отдел продаж</a>'
        key = store.enqueue(self.source, delivery=delivery)
        original = discovery.inspect
        with patch.object(discovery,'search',return_value=[{'url':'https://supplier.example/','title':'Компания'}]), patch.object(discovery,'inspect',side_effect=lambda task,source:original(task,source,fetch=lambda url:(url,html))):
            for _ in range(150):
                if not discovery.run_once(): break
            else: self.fail('Pipeline did not settle')
        return key

    def test_headless_pipeline_prepares_and_exports_without_a_browser_or_send(self):
        self.source['positions'].append(dict(row('lamp','Светильник LED'),section='Раздел 8',unit='шт'))
        key=self.pipeline()
        result=report.build(self.source['tender_id'],key)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(result['companies']),1)
        company=result['companies'][0]
        self.assertEqual(company['status'],'prepared')
        self.assertEqual(set(company['messages'][0]['position_keys']),{'c','lamp'})
        self.assertIn('Контакт: email:',report.plain(result))
        self.assertNotIn('987654',json.dumps(result))
        self.assertEqual(box.listing(self.source['tender_id']),[])
        self.assertEqual(key,store.enqueue(self.source))
        self.assertFalse(discovery.run_once())

    def test_send_policy_is_persisted_and_groups_one_campaign(self):
        key=self.pipeline('email')
        self.assertEqual(len(campaigns.listing(self.source['tender_id'])),1)
        with patch.object(campaigns,'fetch_contact',return_value='sales@example.org'):
            campaigns.run_one()
        self.assertEqual(len(box.listing(self.source['tender_id'])),1)
        self.assertEqual(key,store.enqueue(self.source,delivery='email'))

    def test_estimate_change_blocks_both_enqueue_and_already_queued_send(self):
        self.pipeline()
        job=jobs.jobs(self.source['tender_id'])[0]
        key=box.enqueue(self.source['tender_id'],job['id'],0,'sales@example.org')
        self.source['positions'][0]['quantity']='99'
        with self.assertRaisesRegex(BuyerError,'Смета изменилась'):
            box.enqueue(self.source['tender_id'],job['id'],0,'another@example.org')
        self.assertIsNone(box.claim('mac'))
        self.assertEqual(box.listing(self.source['tender_id'])[0]['status'],'blocked')

    def test_same_company_different_mailbox_cannot_duplicate_request(self):
        self.pipeline()
        job=jobs.jobs(self.source['tender_id'])[0]
        box.enqueue(self.source['tender_id'],job['id'],0,'sales@example.org')
        with self.assertRaises(BuyerError): box.enqueue(self.source['tender_id'],job['id'],0,'info@example.org')
        self.assertEqual(len(box.listing(self.source['tender_id'])),1)

    def test_avito_lead_keeps_text_but_never_uses_platform_email(self):
        data=needs.snapshot(self.source);task=needs.queries(data)[0]|{'url':'https://www.avito.ru/example/123'}
        supplier=discovery.inspect(task,data,fetch=lambda url:(url,'<h1>Кабель ВВГнг</h1>support@avito.ru'))
        self.assertFalse(supplier['email'])
        self.assertEqual(supplier['channels'][0]['channel'],'avito')
        result=suppliers.prepare(data,discovered=[supplier])
        self.assertEqual(len(result['job_ids']),1)

    def test_report_requires_session_and_cannot_cross_tender(self):
        from flask import Flask
        from autobot import buyer_routes as routes
        from autobot.uploaded_corrections import CorrectionError
        key=self.pipeline()
        app=Flask(__name__);app.register_blueprint(routes.blueprint)
        with app.test_client() as client,patch.object(routes.crm_actor,'resolve',side_effect=CorrectionError('Нет доступа',401)):
            self.assertEqual(client.get(f'/api/tenders/{self.source["tender_id"]}/buyer/report').status_code,401)
        with app.test_client() as client,patch.object(routes.crm_actor,'resolve',return_value={}):
            self.assertEqual(client.get(f'/api/tenders/99999999999/buyer/report?run_id={key}').status_code,400)
            response=client.get(f'/api/tenders/{self.source["tender_id"]}/buyer/report?format=text')
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.content_type.startswith('text/plain'))

    def test_missing_run_does_not_create_a_database(self):
        with self.assertRaises(BuyerError):store.source(self.source['tender_id'],'missing')
        self.assertFalse(box.DB_PATH.exists())

    def test_inbox_is_serviced_even_when_outbox_is_always_nonempty(self):
        from autobot import buyer_sender as sender, buyer_inbox as inbox
        remote=Mock()
        remote.request.side_effect=lambda path,**kw: {'job':{'id':'j','token':'t'}} if path.endswith('/claim') else {'ok':True}
        with patch.object(sender,'execute',return_value={'status':'sent'}),patch.object(inbox,'execute',return_value={'status':'checked'}) as collect:
            direction,_=sender.process_next({'collect_replies':True},remote,prefer_inbox=False)
            self.assertEqual(direction,'outbox')
            direction,_=sender.process_next({'collect_replies':True},remote,prefer_inbox=direction!='inbox')
            self.assertEqual(direction,'inbox');collect.assert_called_once()

    def test_thirty_rows_across_sections_form_one_complete_company_message(self):
        self.source['positions']=[dict(row(str(i),f'Кабель ВВГнг-LS 3х2,5 для участка {i}'),section=f'Раздел {i%5}',quantity=i+1) for i in range(30)]
        key=self.pipeline()
        company=report.build(self.source['tender_id'],key)['companies'][0]
        self.assertEqual(len(company['messages']),1)
        self.assertEqual(len(company['messages'][0]['position_keys']),30)
        self.assertEqual(set(company['messages'][0]['position_keys']),{str(i) for i in range(30)})

    def test_two_concurrent_commands_create_one_outbound(self):
        from concurrent.futures import ThreadPoolExecutor
        self.pipeline();job=jobs.jobs(self.source['tender_id'])[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            values=list(pool.map(lambda _:box.enqueue(self.source['tender_id'],job['id'],0,'sales@example.org'),range(2)))
        self.assertEqual(values[0],values[1]);self.assertEqual(len(box.listing(self.source['tender_id'])),1)

    def test_source_revision_change_is_visible_in_machine_and_text_report(self):
        key=self.pipeline();self.source['positions'][0]['quantity']='25'
        result=report.build(self.source['tender_id'],key)
        self.assertFalse(result['request_current'])
        self.assertIn('Смета изменилась',report.plain(result))

    def test_unknown_price_is_preserved_in_plain_report(self):
        key=self.pipeline();result=report.build(self.source['tender_id'],key)
        result['companies'][0]['prices']=[dict(position_key='c',price_kopecks=None,unit='м',origin='reply',state='review')]
        self.assertIn('не определена',report.plain(result))

    def test_configured_search_api_keeps_order_and_does_not_expose_key(self):
        import base64,requests
        xml='<yandexsearch><response><results><grouping>'+''.join(f'<group><doc><url>https://s{i}.example/</url><title>Компания {i}</title></doc></group>' for i in range(12))+'</grouping></results></response></yandexsearch>'
        response=Mock(status_code=200)
        response.iter_content.return_value=[json.dumps({'rawData':base64.b64encode(xml.encode()).decode()}).encode()]
        context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False)
        with patch.dict('os.environ',{'BUYER_SEARCH_API_KEY':'SECRET-KEY','BUYER_SEARCH_FOLDER_ID':'folder'}),patch.object(requests,'post',return_value=context):
            result=discovery.search('кабель')
        self.assertEqual(len(result),10)
        self.assertEqual(result[0]['url'],'https://s0.example/')
        self.assertNotIn('SECRET-KEY',json.dumps(result))

    def test_search_api_transport_failure_has_no_credentials_or_response_body(self):
        import requests
        with patch.dict('os.environ',{'BUYER_SEARCH_API_KEY':'SECRET-KEY','BUYER_SEARCH_FOLDER_ID':'folder'}),patch.object(requests,'post',side_effect=requests.ConnectionError('SECRET-KEY')):
            with self.assertRaises(BuyerError) as caught:discovery.search('кабель')
        self.assertNotIn('SECRET-KEY',str(caught.exception))

    def test_common_generic_word_does_not_confirm_unknown_assortment(self):
        self.source['positions']=[row('x','Панель акустическая базальтовая')]
        data=needs.snapshot(self.source);task=needs.queries(data)[0]|{'url':'https://supplier.example/'}
        with self.assertRaises(BuyerError):discovery.inspect(task,data,fetch=lambda url:(url,'<h1>Панель управления котлом</h1>sales@example.org'))


if __name__=='__main__': unittest.main()
