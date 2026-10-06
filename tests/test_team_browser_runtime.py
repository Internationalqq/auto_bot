import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('runtime', Path(__file__).parents[1]/'scripts/hermes/team_browser_runtime.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class BrowserRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.team = Path(self.tmp.name)
        (self.team/'state').mkdir()
        (self.team/'unattended-policy.json').write_text(json.dumps({'profiles':{'gulya':['Firefox','Telegram']}}))
        (self.team/'state/active.json').write_text(json.dumps({'owner':'gulya'}))
        self.env = patch.dict(os.environ, HERMES_HOME='/Users/egor/.hermes/profiles/gulya')
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_assigned_action_no_prompt_and_no_global_grant(self):
        self.assertEqual(m.approval('focus_app', {'app':'Firefox'}, self.team), 'approve_once')
        self.assertTrue(m.approval('focus_app', {'app':'Safari'}, self.team).startswith('deny:'))
        self.assertTrue(m.approval('kill_app', {'app':'Firefox'}, self.team).startswith('deny:'))

    def test_lock_owned_by_other_agent_never_approves(self):
        (self.team/'state/active.json').write_text(json.dumps({'owner':'commercial'}))
        self.assertTrue(m.approval('click', {'app':'Firefox'}, self.team).startswith('deny:'))

    def test_unconfigured_profile_keeps_original_approval(self):
        with patch.dict(os.environ, HERMES_HOME='/Users/egor/.hermes/profiles/unknown'):
            self.assertIsNone(m.approval('click', {'app':'Firefox'}, self.team))

    def test_no_restart_for_permission_denial(self):
        backend=Obj(capture=lambda **kw: (_ for _ in ()).throw(RuntimeError('permission denied')))
        with patch.object(m.os,'kill') as kill:
            with self.assertRaisesRegex(RuntimeError,'permission denied'):
                m.recover_browser(backend,'Firefox',self.team)
            kill.assert_not_called()

    def test_visible_content_does_not_restart_or_focus(self):
        cap=Obj(elements=[1])
        backend=Obj(capture=lambda **kw:cap)
        self.assertIs(m.recover_browser(backend,'Firefox',self.team),cap)

    def test_bounded_restart_then_fresh_capture_no_action_replay(self):
        calls=[]
        def capture(**kw):
            if 'stop' in calls:return Obj(elements=[1])
            raise RuntimeError('No on-screen windows available')
        backend=Obj(capture=capture,focus_app=lambda *a,**kw:Obj(ok=False,message='No on-screen window'),
                    launch_app=lambda **kw:calls.append('open'),
                    list_apps=lambda:[] if 'stop' in calls else [{'name':'Firefox','pid':123}])
        with patch.object(m.os,'kill',side_effect=lambda *a:calls.append('stop')):
            m.recover_browser(backend,'Firefox',self.team,sleep=lambda _:None,now=lambda:10000)
        self.assertEqual(calls,['open','stop','open'])
        self.assertEqual(json.loads((self.team/'recovery-gulya.json').read_text())['status'],'ready')

    def test_cooldown_does_not_kill_browser_again(self):
        (self.team/'recovery-gulya.json').write_text(json.dumps({'restarts':[9990]}))
        backend=Obj(capture=lambda **kw:(_ for _ in ()).throw(RuntimeError('No on-screen windows available')),
                    focus_app=lambda *a,**kw:Obj(ok=False,message='No on-screen window'),launch_app=lambda **kw:None)
        with patch.object(m.os,'kill') as kill:
            with self.assertRaisesRegex(RuntimeError,'cooldown'):
                m.recover_browser(backend,'Firefox',self.team,sleep=lambda _:None,now=lambda:10000)
            kill.assert_not_called()


if __name__ == '__main__':unittest.main()
