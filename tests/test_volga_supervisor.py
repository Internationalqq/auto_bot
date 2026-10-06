import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('supervisor',Path(__file__).parents[1]/'scripts/hermes/supervise_volga.py')
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SupervisorTests(unittest.TestCase):
    def test_browser_failure_can_preflight(self):
        s={'deadline':1000,'status':'needs_attention','batches':[{'exit_code':0,'result':{'items':[{'outcome':'browser_error'}]}}]}
        self.assertEqual(m.decision(s,False,100),'preflight')

    def test_api_or_other_error_does_not_restart(self):
        s={'deadline':1000,'status':'needs_attention','batches':[{'exit_code':1,'result':{'items':[]}}]}
        self.assertEqual(m.decision(s,False,100),'needs_attention')

    def test_does_not_spawn_duplicate_or_extend_deadline(self):
        s={'deadline':1000,'status':'running'}
        self.assertEqual(m.decision(s,True,100),'running')
        self.assertEqual(m.decision(s,False,1000),'complete')
        self.assertEqual(m.decision(s,False,100),'preflight')

    def test_explicit_stop_and_finished_do_not_restart(self):
        self.assertEqual(m.decision({'deadline':1000,'status':'stopped'},False,100),'needs_attention')
        self.assertEqual(m.decision({'deadline':1000,'status':'finished'},False,100),'complete')


if __name__=='__main__':unittest.main()
