import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from autobot.atomic_output import output_lock
from autobot import buyer_local as local, buyer_mail_service as service
from autobot import buyer_mail_status as heartbeat, buyer_outbox, buyer_pipeline, buyer_routes
from autobot.hermes_buyer import BuyerError
from flask import Flask


class LocalMailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'outbox_dir':str(self.root), 'local_control':True}

    def test_process_lock_excludes_another_process_and_releases_on_exit(self):
        code = ('from pathlib import Path; from autobot.atomic_output import output_lock; '
                'import sys; ctx=output_lock(Path(sys.argv[1]), timeout=0); ctx.__enter__(); print("locked", flush=True); sys.stdin.readline(); ctx.__exit__(None,None,None)')
        child = subprocess.Popen([sys.executable,'-c',code,str(self.root/'sender')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual(child.stdout.readline().strip(), 'locked')
        self.assertTrue(local.running(self.root))
        child.communicate('\n', timeout=10)
        self.assertEqual(child.returncode, 0)
        self.assertFalse(local.running(self.root))

    def test_stopped_worker_never_reports_old_connection_as_live(self):
        (self.root/'service-status.json').write_text(json.dumps({'ok':True,'checked_at':time.time(), 'receiving':True,'sending':True}))
        state = local.status(self.config)
        self.assertFalse(state['running']); self.assertFalse(state['connected']); self.assertFalse(state['sending'])

    def test_stop_is_cooperative_and_prevents_the_next_claim(self):
        with output_lock(self.root/'sender', timeout=0):
            self.assertTrue(local.stop(self.config)['stopping'])
        runner = service.Service(self.config, Mock(), Mock())
        with patch.object(runner, 'step') as step:
            self.assertEqual(runner.run(), 0)
        step.assert_not_called()
        self.assertFalse(json.loads((self.root/'service-status.json').read_text())['running'])

    def test_repeated_start_preserves_running_process_and_stop_request(self):
        with output_lock(self.root/'sender', timeout=0):
            (self.root/'stop-request').touch()
            with patch.object(local,'load_config',return_value=self.config), patch.object(local.subprocess,'Popen') as launch:
                state = local.start(self.root/'config.json')
                self.assertTrue(state['already_running']);self.assertTrue(state['stopping'])
                launch.assert_not_called()
        self.assertTrue((self.root/'stop-request').exists())

    def test_start_is_explicit_smtp_worker_without_browser_and_no_secret_arguments(self):
        password=self.root/'mail_password';password.write_text('private-password')
        token=self.root/'queue_token';token.write_text('private-token')
        config=self.config | {'mail_password_file':str(password),'queue_token_file':str(token)}
        with patch.object(local,'load_config',return_value=config), patch.object(local,'running',side_effect=[False,True,True]), patch.object(local.subprocess,'Popen') as launch:
            self.assertTrue(local.start(self.root/'config.json')['started'])
        args=launch.call_args.args[0]
        self.assertEqual(args[1:6],['-X','utf8','-m','autobot.buyer_mail_service','run'])
        self.assertNotIn('private-password',str(launch.call_args))
        self.assertNotIn('private-token',str(launch.call_args))
        if os.name=='nt':self.assertEqual(launch.call_args.kwargs['creationflags'],subprocess.CREATE_NO_WINDOW)
        else:self.assertTrue(launch.call_args.kwargs['start_new_session'])

    def test_status_api_requires_worker_key_and_stores_only_bools_with_server_time(self):
        app = Flask(__name__)
        with patch('autobot.buyer_discovery.start_worker'), patch('autobot.buyer_campaigns.launch'):
            app.register_blueprint(buyer_routes.blueprint)
        url = buyer_routes.WORKER_API + '/service/status'
        state = dict.fromkeys(heartbeat.FIELDS, True) | {'checked_at':99999999999, 'detail':'secret'}
        body = {'worker_id':'laptop', 'state':state}
        with patch.object(buyer_outbox,'DB_PATH',self.root/'outbox.db'), patch.dict(os.environ,BUYER_WORKER_TOKEN='a'*48), patch.object(heartbeat.time,'time',return_value=1000):
            client = app.test_client()
            self.assertEqual(client.post(url,json=body).status_code,401)
            self.assertFalse(heartbeat.path().exists())
            headers = {'Authorization':'Bearer '+'a'*48}
            self.assertEqual(client.post(url,json=body,headers=headers).status_code,200)
            stored = heartbeat.records()['laptop']
            self.assertEqual(stored,dict.fromkeys(heartbeat.FIELDS,True)|{'checked_at':1000})
            self.assertEqual(buyer_pipeline.mail_status(now=1001)['state'],'ready')
            self.assertEqual(buyer_pipeline.mail_status(now=1301)['state'],'offline')
            self.assertNotIn('secret',heartbeat.path().read_text())
            body['state']['sending']='true'
            self.assertEqual(client.post(url,json=body,headers=headers).status_code,422)

    def test_stopped_device_does_not_hide_another_active_device_and_records_are_bounded(self):
        active = dict.fromkeys(heartbeat.FIELDS,True)
        with patch.object(buyer_outbox,'DB_PATH',self.root/'outbox.db'), patch.object(heartbeat.time,'time',return_value=1000):
            for i in range(12):heartbeat.record('old-'+str(i), active|{'running':False})
            self.assertLessEqual(len(heartbeat.records()),8)
        with patch.object(buyer_outbox,'DB_PATH',self.root/'outbox.db'):
            with patch.object(heartbeat.time,'time',return_value=1001):heartbeat.record('current',active)
            with patch.object(heartbeat.time,'time',return_value=1002):heartbeat.record('other',active|{'running':False})
            self.assertEqual(heartbeat.read(now=1003)['state'],'ready')
            self.assertFalse(heartbeat.records()['other']['sending'])
            with patch.object(heartbeat.time,'time',return_value=1004):heartbeat.record('current',active|{'running':False})
            self.assertEqual(heartbeat.read(now=1005)['state'],'offline')

    def test_local_status_publication_omits_mail_and_errors_and_network_failure_is_recoverable(self):
        remote = Mock()
        runner = service.Service(self.config,remote,Mock())
        runner.publish({'running':True,'ok':False,'detail':'private-error','body':'private-mail'})
        remote.request.assert_called_once_with('/service/status',state={'running':True,'ok':False,'receiving':False,'sending':False})
        remote.request.side_effect = BuyerError('network down')
        runner.publish({'running':True,'ok':True})
        self.assertFalse(json.loads((self.root/'service-status.json').read_text())['queue_connected'])


if __name__ == '__main__':unittest.main()
