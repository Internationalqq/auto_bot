"""Credential wrapper tests: no real credentials or websites."""
import importlib.util,sys,json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
sys.path.insert(0,'/Users/egor/.hermes/team-browser-access')
import team_accounts as a

def test_password_goes_only_to_stdin(monkeypatch):
    seen=[]
    def run(command,**kwargs):
        seen.append((command,kwargs))
        return SimpleNamespace(stdout=json.dumps({'success':True}))
    monkeypatch.setattr(a.subprocess,'run',run)
    a.vault(['save','test','--password-stdin'],'fixture-secret\n')
    assert 'fixture-secret' not in str(seen[0][0])
    assert seen[0][1]['input']=='fixture-secret\n'
    assert 'shell' not in seen[0][1]

@pytest.mark.parametrize('channel',['whatsapp','telegram','max'])
def test_qr_channel_does_not_ask_for_password(monkeypatch,channel):
    monkeypatch.setattr(a,'config',lambda:a.ACCOUNTS)
    monkeypatch.setattr(sys,'argv',['accounts','configure',channel])
    prompt=Mock();monkeypatch.setattr(a.getpass,'getpass',prompt)
    with pytest.raises(SystemExit,match='QR'):a.main()
    prompt.assert_not_called()

def test_registry_adds_channels_without_overwriting_accounts(tmp_path,monkeypatch):
    path=tmp_path/'accounts.json'
    existing={'mail':{'username':'kept@example.test','url':'https://mail.ru/'}}
    path.write_text(json.dumps(existing))
    monkeypatch.setattr(a,'CONFIG',path)
    result=a.config()
    assert result['mail']==existing['mail']
    assert result['max']['url']=='https://web.max.ru/'
    assert json.loads(path.read_text())==result
    assert a.config()==result

def test_saved_password_does_not_claim_logged_in(monkeypatch,capsys):
    monkeypatch.setattr(a,'config',lambda:a.ACCOUNTS)
    monkeypatch.setattr(sys,'argv',['accounts','status'])
    monkeypatch.setattr(a,'vault',lambda args:{'data':{'profiles':[{'name':'pm_work_mail'}]}})
    a.main();result=json.loads(capsys.readouterr().out)
    assert result['mail']['credentials_saved'] is True
    assert result['mail']['login_verified'] is False

@pytest.mark.parametrize('own',[False,True])
def test_agent_connect_uses_only_its_active_profile(tmp_path,monkeypatch,own):
    import tools.team_headless as headless
    home=tmp_path/'profiles/gulya';root=home/'headless-browser';root.mkdir(parents=True)
    (root/'status.json').write_text(json.dumps({'status':'ready'}))
    monkeypatch.setattr(a,'BASE',tmp_path)
    monkeypatch.setattr(a,'config',lambda:a.ACCOUNTS)
    monkeypatch.setattr(headless,'location',lambda p:root)
    monkeypatch.setattr(a.control,'daemon_alive',lambda s:True)
    monkeypatch.setenv('HERMES_HOME',str(home if own else tmp_path/'profiles/commercial'))
    monkeypatch.setattr(sys,'argv',['accounts','connect','mail','--profile','gulya'])
    monkeypatch.setattr(a,'vault',lambda args:{'data':{'profiles':[]}})
    command=Mock(return_value={'success':True});monkeypatch.setattr(a.control,'run',command)
    if own:
        a.main();command.assert_called_once()
    else:
        with pytest.raises(SystemExit):a.main()
        command.assert_not_called()
