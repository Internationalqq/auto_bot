"""POSIX integration tests for the Mac plugin; no real desktop or accounts."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

PLUGIN = Path(__file__).resolve().parents[1]/'integrations/hermes/desktop-queue/__init__.py'
if os.name == 'posix':
    spec = importlib.util.spec_from_file_location('desktop_queue_test', PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


@unittest.skipUnless(os.name == 'posix', 'Mac/Linux advisory locks')
class DesktopQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'desktop.lock'
        self.handler = Mock(return_value='ok')
        self.reset = Mock()
        self.queue = self.make_queue(self.handler, self.reset)

    def make_queue(self, handler=None, reset=None):
        queue = module.DesktopQueue(self.path, handler or Mock(return_value='ok'),
                                    reset or Mock(), 'test', wait_seconds=0)
        self.addCleanup(queue.close)
        return queue

    def test_two_turns_get_fresh_backend_and_keep_all_arguments(self):
        for task in ('a', 'b'):
            self.assertEqual(self.queue.dispatch({'action':'capture'}, task_id=task), 'ok')
            self.queue.dispatch({'action':'key','keys':'Escape'}, task_id=task)
            self.queue.finish(task_id=task)
        self.assertEqual(self.reset.call_count, 4)
        self.assertEqual(self.handler.call_count, 4)
        self.handler.assert_called_with({'action':'key','keys':'Escape'}, task_id='b')

    def test_different_profile_cannot_read_or_type_until_turn_finishes(self):
        other = self.make_queue()
        self.queue.dispatch({'action':'capture'}, task_id='a')
        for action in ('capture', 'type', 'key', 'focus_app'):
            self.assertEqual(json.loads(other.dispatch({'action':action}, task_id='b'))['error'], 'desktop_busy')
        other.handler.assert_not_called()
        other.reset.assert_not_called()
        self.queue.finish(task_id='a')
        self.assertEqual(other.dispatch({'action':'capture'}, task_id='b'), 'ok')

    def test_wrong_task_cannot_release_owner(self):
        self.queue.dispatch({}, task_id='a')
        self.queue.finish(task_id='b')
        self.assertEqual(self.queue.owner, 'a')
        self.assertEqual(json.loads(self.queue.dispatch({}, task_id='b'))['error'], 'desktop_busy')

    def test_missing_task_fails_closed(self):
        self.assertEqual(json.loads(self.queue.dispatch({}))['error'], 'desktop_task_id_required')
        self.handler.assert_not_called()

    def test_dispatch_failure_is_not_replayed_or_handed_to_another_owner(self):
        self.handler.side_effect = RuntimeError('input outcome unknown')
        with self.assertRaises(RuntimeError):
            self.queue.dispatch({'action':'type'}, task_id='a')
        self.handler.assert_called_once()
        self.assertEqual(json.loads(self.make_queue().dispatch({}, task_id='b'))['error'], 'desktop_busy')
        self.queue.finish(task_id='a', interrupted=True)
        self.assertIsNone(self.queue.owner)

    def test_setup_failure_never_dispatches_and_unlocks(self):
        self.reset.side_effect = RuntimeError('reset failed')
        self.assertEqual(json.loads(self.queue.dispatch({}, task_id='a'))['error'], 'desktop_queue_unavailable')
        self.handler.assert_not_called()
        self.assertEqual(self.make_queue().dispatch({}, task_id='b'), 'ok')

    def test_finish_is_idempotent(self):
        self.queue.dispatch({}, task_id='a')
        self.queue.finish(task_id='a')
        self.queue.finish(task_id='a')
        self.assertEqual(self.reset.call_count, 2)

    def test_waiter_proceeds_after_release_without_replaying_action(self):
        other = self.make_queue()
        other.wait_seconds = 2
        self.queue.dispatch({}, task_id='a')
        result = []
        worker = threading.Thread(target=lambda: result.append(other.dispatch({'action':'type'}, task_id='b')))
        worker.start()
        self.queue.finish(task_id='a')
        worker.join(3)
        self.assertEqual(result, ['ok'])
        other.handler.assert_called_once_with({'action':'type'}, task_id='b')

    def test_failed_cleanup_does_not_keep_file_lock(self):
        self.queue.dispatch({}, task_id='a')
        self.reset.side_effect = RuntimeError('cleanup failed')
        with self.assertRaises(RuntimeError):
            self.queue.finish(task_id='a')
        self.assertEqual(self.make_queue().dispatch({}, task_id='b'), 'ok')

    def test_subprocess_lock_and_crash_recovery(self):
        script = 'import fcntl,sys,time; f=open(sys.argv[1],"a+"); fcntl.flock(f,fcntl.LOCK_EX); print("ready",flush=True); time.sleep(30)'
        child = subprocess.Popen([sys.executable, '-c', script, str(self.path)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            self.assertEqual(json.loads(self.queue.dispatch({}, task_id='a'))['error'], 'desktop_busy')
        finally:
            child.terminate()
            child.wait(timeout=5)
            child.stdout.close()
        self.assertEqual(self.queue.dispatch({}, task_id='a'), 'ok')

    def test_same_turn_parallel_actions_are_serialized(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []
        def handler(args, **kw):
            calls.append(args['n'])
            if args['n'] == 1:
                entered.set()
                self.assertTrue(release.wait(3))
            return 'ok'
        self.queue.handler = handler
        one = threading.Thread(target=self.queue.dispatch, args=({'n':1},), kwargs={'task_id':'a'})
        two = threading.Thread(target=self.queue.dispatch, args=({'n':2},), kwargs={'task_id':'a'})
        one.start()
        self.assertTrue(entered.wait(3))
        two.start()
        self.assertEqual(calls, [1])
        release.set()
        one.join(3); two.join(3)
        self.assertEqual(calls, [1, 2])

    def installer(self):
        spec = importlib.util.spec_from_file_location('desktop_installer', PLUGIN.parents[3]/'scripts/install_hermes_desktop_queue.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        return installer

    def test_install_preserves_config_and_backup_and_can_repeat(self):
        import yaml
        root = Path(self.temp.name)/'hermes'
        profile = root/'profiles/mail'
        profile.mkdir(parents=True)
        before = b'model: existing-model\nplugins:\n  enabled: [existing-plugin]\n'
        (profile/'config.yaml').write_bytes(before)
        installer = self.installer()
        first = installer.install(root, PLUGIN.parent, ['mail'], self.path)
        self.assertEqual((Path(first[0]['backup'])/'config.yaml').read_bytes(), before)
        installer.install(root, PLUGIN.parent, ['mail'], self.path)
        result = yaml.safe_load((profile/'config.yaml').read_text())
        self.assertEqual(result['model'], 'existing-model')
        self.assertEqual(result['plugins']['enabled'], ['existing-plugin', 'desktop-queue'])
        self.assertEqual(result['plugins']['entries']['desktop-queue']['lock_path'], str(self.path))

    def test_installer_rejects_path_escape(self):
        with self.assertRaises(ValueError):
            self.installer().install(Path(self.temp.name), PLUGIN.parent, ['../../elsewhere'], self.path)


if __name__ == '__main__':
    unittest.main()
