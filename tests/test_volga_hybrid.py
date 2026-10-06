import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1]/'scripts/hermes'/f'{name}.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


runner = module('run_volga_full')
session = module('volga_headless_session')
supervisor = module('supervise_volga')


class HybridTests(unittest.TestCase):
    def select(self, gui_status, hybrid=True):
        lock = Mock()
        lock.operation.return_value = {'status':'acquired','ticket':'isolated-test'}
        turn = Mock(return_value={'status':gui_status,'owner':'gulya'})
        result = runner.acquire_transport(lock, turn, Path('shared'), Path('isolated'), hybrid=hybrid)
        return result, lock, turn

    def test_free_gui_is_preferred_and_isolated_not_touched(self):
        result, lock, _ = self.select('acquired')
        self.assertFalse(result[0])
        self.assertEqual(result[1], Path('shared'))
        lock.operation.assert_not_called()

    def test_busy_gui_uses_only_isolated_lock(self):
        result, lock, _ = self.select('busy')
        self.assertTrue(result[0])
        self.assertEqual(result[1], Path('isolated'))
        lock.operation.assert_called_once_with(Path('isolated'), 'acquire', 'commercial')

    def test_disabled_hybrid_waits_and_error_is_not_a_switch_reason(self):
        for status, enabled in [('busy',False),('denied',True),('error',True)]:
            result, lock, _ = self.select(status, enabled)
            self.assertFalse(result[0])
            lock.operation.assert_not_called()

    def test_next_position_reconsiders_gui_without_changing_prior_choice(self):
        first, _, _ = self.select('busy')
        second, _, _ = self.select('acquired')
        self.assertTrue(first[0])
        self.assertFalse(second[0])

    def test_busy_isolated_lock_is_preserved(self):
        lock = Mock()
        lock.operation.return_value = {'status':'busy','owner':'commercial'}
        result = runner.acquire_transport(lock, Mock(return_value={'status':'busy'}),
            Path('shared'), Path('isolated'), hybrid=True)
        self.assertEqual(result[2]['status'], 'busy')
        self.assertEqual(lock.operation.call_count, 1)

    def test_explicit_human_check_stops_recovery_even_after_interruption(self):
        for output in [{'data':{'url':'https://www.google.com/sorry/index'}},
                       {'data':{'snapshot':'Our systems have detected unusual traffic'}}]:
            self.assertTrue(session.access_challenge(output))
        self.assertFalse(session.access_challenge({'success':False,'error':'ERR_CONNECTION_CLOSED'}))
        self.assertFalse(session.access_challenge({'data':{'snapshot':'Монтаж от 450 руб./шт'}}))
        for status in ['needs_attention','interrupted']:
            state={'deadline':1000,'status':status,'batches':[{'stop_reason':'access_challenge',
                'exit_code':0,'result':{'items':[{'outcome':'browser_error'}]}}]}
            self.assertEqual(supervisor.decision(state,False,100),'needs_attention')


if __name__ == '__main__':
    unittest.main()
