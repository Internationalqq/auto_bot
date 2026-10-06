import tempfile
import unittest
import threading
from pathlib import Path
from unittest.mock import Mock
from browser_turn_queue import acquire_turn, wait_for_gulya
try:
    import fcntl
except ImportError:
    fcntl = None


@unittest.skipUnless(fcntl, 'Mac/Linux OS lock tests')
class QueueTests(unittest.TestCase):
    def test_ivan_yields_to_waiting_gulya_then_can_acquire(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);lock=Mock();lock.operation.return_value={'status':'acquired'}
            with (root/'gulya-waiter.lock').open('a+') as f:
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertEqual(acquire_turn(lock,root,'commercial')['owner'],'gulya')
                lock.operation.assert_not_called()
            self.assertEqual(acquire_turn(lock,root,'commercial')['status'],'acquired')

    def test_waiter_preserves_existing_owner_and_gets_next_turn(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);lock=Mock()
            lock.operation.side_effect=[{'status':'busy','owner':'commercial'}, {'status':'acquired','ticket':'test'}]
            checked=[]
            def sleep(_):
                other=Mock()
                checked.append(acquire_turn(other,root,'commercial')['status'])
                other.operation.assert_not_called()
            result=wait_for_gulya(lock,root,sleep=sleep)
            self.assertEqual(result['ticket'],'test');self.assertEqual(checked,['busy'])
            self.assertTrue(all(c.args[1]=='acquire' for c in lock.operation.call_args_list))

    def test_timeout_does_not_release_another_owner(self):
        with tempfile.TemporaryDirectory() as d:
            lock=Mock();lock.operation.return_value={'status':'busy','owner':'commercial'}
            clock=iter([0,901])
            result=wait_for_gulya(lock,Path(d),now=lambda:next(clock))
            self.assertEqual(result['reason'],'gui_wait_timeout')
            self.assertEqual(lock.operation.call_count,1)

    def test_real_gui_lock_passes_ivan_to_gulya_then_back(self):
        import browser_lock
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);ivan=browser_lock.operation(root,'acquire','commercial')
            waiting=threading.Event();release_waiter=threading.Event();results=[]
            def sleep(_):
                waiting.set()
                if not release_waiter.wait(3):raise RuntimeError('Test failed to release waiter')
            thread=threading.Thread(target=lambda:results.append(wait_for_gulya(browser_lock,root,sleep=sleep)))
            thread.start()
            self.assertTrue(waiting.wait(3))
            self.assertEqual(browser_lock.operation(root,'status')['owner'],'commercial')
            self.assertEqual(browser_lock.operation(root,'release','commercial',ivan['ticket'])['status'],'released')
            self.assertEqual(acquire_turn(browser_lock,root,'commercial')['owner'],'gulya')
            release_waiter.set();thread.join(3);self.assertFalse(thread.is_alive())
            self.assertEqual(results[0]['status'],'acquired')
            self.assertEqual(acquire_turn(browser_lock,root,'commercial')['status'],'busy')
            browser_lock.operation(root,'release','gulya',results[0]['ticket'])
            again=acquire_turn(browser_lock,root,'commercial');self.assertEqual(again['status'],'acquired')
            browser_lock.operation(root,'release','commercial',again['ticket'])


if __name__ == '__main__':unittest.main()
