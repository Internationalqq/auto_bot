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
