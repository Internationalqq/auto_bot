import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


def module(name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).resolve().parents[1]/'scripts/hermes'/f'{name}.py')
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result


worker=module('run_volga_full')
relay=module('sync_volga_full')


class FullRunTests(unittest.TestCase):
    def test_headless_prompt_keeps_evidence_contract_without_gui_steps(self):
        prompt=worker.headless_prompt(Path('/tmp/batch-122'),{'name':'Кабель','position_key':'cable'},'Ярославская область')
        self.assertIn('browser_navigate',prompt)
        self.assertIn('supplier_confirmed',prompt)
        self.assertIn('price_found только',prompt)
        self.assertIn('/tmp/batch-122/result.json',prompt.replace('\\','/'))
        self.assertNotIn('Сначала capture Chrome',prompt)
        self.assertNotIn('cmd+l',prompt)

    def test_previous_evidence_is_marked_prior_without_mutating_history(self):
        items=[{'offers':[{'observation':'current','price_rub':100}],
                'attempts':[{'observation':'current','url':'https://shop.example'}]}]
        previous=worker.prior_evidence(items)
        self.assertEqual(previous[0]['offers'][0]['observation'],'prior')
        self.assertEqual(previous[0]['attempts'][0]['observation'],'prior')
        self.assertEqual(items[0]['offers'][0]['observation'],'current')

    def entry(self, index, attempt=1, complete=False):
        return {'batch':index,'attempt':attempt,'position_key':str(index),'name':str(index),
                'finished_at':1,'status':'attempted','result':{
                    'status':'completed' if complete else 'partial','items':[{
                        'position_key':str(index),'outcome':'price_found' if complete else 'needs_clarification',
                        'offers':[{'observation':'current','price_rub':100,'unit':'шт','evidence':'100 руб/шт','url':'https://shop.example'}]}]}}

    def test_retry_starts_after_all_first_attempts_and_survives_restart(self):
        rows=[{'position_key':str(i)} for i in (1,2,3)]
        state={'batches':[self.entry(1),self.entry(2,complete=True)]}
        self.assertEqual(worker.next_work(state,rows)[:3],(3,rows[2],1))
        self.assertNotIn('retry_plan',state)
        state['batches'].append(self.entry(3))
        self.assertEqual(worker.next_work(state,rows)[:3],(1,rows[0],2))
        self.assertEqual(state['retry_plan'],[1,3])
        state=json.loads(json.dumps(state))
        state['batches'].append(self.entry(1,2))
        self.assertEqual(worker.next_work(state,rows)[:3],(3,rows[2],2))
        state['batches'].append(self.entry(3,2,True))
        self.assertIsNone(worker.next_work(state,rows))
        worker.update_progress(state)
        self.assertEqual(state['completed'],3)
        self.assertEqual(state['attempts_completed'],5)
        self.assertEqual(state['retry_completed'],2)
        self.assertEqual([x['batch'] for x in state['unresolved']],[1])

    def test_timeout_and_prior_prices_are_never_skipped_by_retry(self):
        entry=self.entry(1,complete=True)
        entry['status']='timed_out'
        self.assertTrue(worker.needs_retry(entry))
        entry['status']='attempted'
        entry['result']['items'][0]['offers'][0]['observation']='prior'
        self.assertTrue(worker.needs_retry(entry))
        self.assertTrue(worker.needs_retry({'result':{'status':'completed','items':[]}}))

    def test_retry_delivery_keeps_legacy_receipts_and_is_repeatable(self):
        one=self.entry(1)
        two=self.entry(1,2)
        for e in (one,two): e['links']=[{'url':'https://shop.example','position_keys':['1']}]
        source={'source':{'tender_id':'t','region':'r','positions':[{'position_key':'1'}]}}
        state={'batches':[one,two]}
        synced={'1':{'run_id':'legacy'}}
        groups=list(relay.groups(state,source,synced))
        self.assertEqual(groups[0][0],[two])
        synced[relay.receipt_key(two)]={'run_id':'retry'}
        self.assertEqual(list(relay.groups(state,source,synced)),[])
        self.assertEqual(len(list(relay.groups(state,source,{}))),2)

    def test_watchdog_allows_progress_past_old_cutoff_but_limits_stalls(self):
        self.assertIsNone(worker.session_stop_reason(100,500,600,5000))
        self.assertEqual(worker.session_stop_reason(100,200,501,5000),'no_activity')
        self.assertEqual(worker.session_stop_reason(100,995,1000,5000),'session_limit')
        self.assertEqual(worker.session_stop_reason(100,195,200,210),'run_deadline')

    def test_browser_failure_stops_queue_even_with_prior_offers(self):
        self.assertTrue(worker.browser_unavailable({'items':[
            {'outcome':'browser_error','offers':[{'observation':'prior','url':'https://shop.example'}]}]}))

    def test_individual_shop_failure_does_not_stop_queue(self):
        for outcome in ('site_blocked','not_found','price_found','no_price'):
            self.assertFalse(worker.browser_unavailable({'items':[{'outcome':outcome}]}))

    def test_result_cannot_be_assigned_to_another_position(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            worker.save(root/'result.json',{'items':[{'position_key':'other'}]})
            self.assertEqual(worker.result_or_error(root,'wanted')['items'],[])

    def test_imports_only_observed_https_urls_not_model_prices(self):
        result={'items':[{'position_key':'one','offers':[
            {'url':'https://shop.example/product','price_rub':999},
            {'url':'https://shop.example/product'}, {'url':'javascript:x'},
            {'url':'https://user:password@shop.example/private'}]},
            {'position_key':'other','offers':[{'url':'https://wrong.example'}]}]}
        self.assertEqual(worker.observed_links(result,'one'),[{'url':'https://shop.example/product','position_keys':['one']}])

    def test_relay_only_groups_finished_unsynced_rows(self):
        source={'source':{'tender_id':'t','region':'r','positions':[{'position_key':str(i)} for i in range(3)]}}
        state={'batches':[{'position_key':'0','finished_at':1,'links':[]},
                          {'position_key':'1','finished_at':1,'links':[{'url':'https://shop.example','position_keys':['1']}]},
                          {'position_key':'2','links':[]}]}
        batches=list(relay.groups(state,source,{'0':{'run_id':'old'}}))
        self.assertEqual(len(batches),1)
        self.assertEqual(batches[0][1]['source']['positions'],[{'position_key':'1'}])
        self.assertNotIn('price',json.dumps(batches[0][1]))


if __name__=='__main__':unittest.main()
