"""Mac integration tests for human login lifecycle, with no real login."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json

spec=importlib.util.spec_from_file_location('headless_control',
    '/Users/egor/.hermes/team-browser-access/headless_control.py')
c=importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def test_missing_daemon_check_does_not_start_browser(tmp_path,monkeypatch):
    command=Mock()
    monkeypatch.setattr(c.subprocess,'run',command)
    assert not c.daemon_alive({'socket_dir':str(tmp_path),'session':'gone'})
    command.assert_not_called()


def test_stale_pid_of_other_process_is_not_browser(tmp_path,monkeypatch):
    (tmp_path/'old.pid').write_text('123')
    monkeypatch.setattr(c.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout='/usr/bin/python'))
    assert not c.daemon_alive({'socket_dir':str(tmp_path),'session':'old'})


def test_human_login_does_not_expire_after_thirty_minutes(tmp_path,monkeypatch):
    seen=[]
    def run(cmd,**kwargs):
        seen.append((cmd,kwargs['env']))
        kwargs['stdout'].write(json.dumps({'success':True}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(c.subprocess,'run',run)
    info={'socket_dir':str(tmp_path),'team_root':str(tmp_path),'session':'human'}
    assert c.run(info,'open',['https://example.com'],True)['success']
    assert seen[0][1]['AGENT_BROWSER_IDLE_TIMEOUT_MS']=='0'
    assert seen[0][0][seen[0][0].index('--headed')+1]=='false'


def test_closed_login_window_releases_lease_without_reopening(tmp_path,monkeypatch):
    root=tmp_path/'profiles/agent/headless-browser';root.mkdir(parents=True)
    monkeypatch.setattr(c,'BASE',tmp_path)
    monkeypatch.setattr(c.sys,'argv',['control','login','agent'])
    monkeypatch.setattr(c,'attach',lambda info,*a:dict(info,team_root=str(root)))
    run=Mock(side_effect=[{'success':True},{'success':True},{'success':True},{'success':True,'data':{'port':12345}}]);release=Mock()
    monkeypatch.setattr(c,'run',run)
    monkeypatch.setattr(c,'release',release)
    monkeypatch.setattr(c,'daemon_alive',lambda info:False)
    monkeypatch.setattr(c.time,'sleep',lambda x:None)
    monkeypatch.setattr(c.signal,'signal',lambda *a:None)
    c.main()
    assert run.call_count==4
    assert run.call_args_list[1].args[1]=='open'
    assert json.loads((root/'status.json').read_text())['headless'] is True
    assert json.loads((root/'status.json').read_text())['stream_port']==12345
    release.assert_called_once()
