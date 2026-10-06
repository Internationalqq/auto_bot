"""Run in the Mac Hermes checkout with pytest; no model calls or messages."""
import json
import subprocess
import sys
from unittest.mock import patch
import pytest
from tools import team_headless as t


def test_profile_excludes_other_task_and_release_allows_reopen(tmp_path):
    with patch.object(t,'location',return_value=tmp_path):
        first=t.attach({'session_name':'one'},'task1',tmp_path,True)
        try:
            with pytest.raises(RuntimeError,match='agent_browser_busy'):
                t.attach({'session_name':'two'},'task2',tmp_path,True)
            t.release(dict(first,session_name='not-owner'))
            with pytest.raises(RuntimeError):t.attach({'session_name':'three'},'task3',tmp_path,True)
            # The OS lease also excludes another process.
            probe='import fcntl,sys; f=open(sys.argv[1],"a+"); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'
            assert subprocess.run([sys.executable,'-c',probe,str(tmp_path/'profile.lock')],capture_output=True).returncode != 0
        finally:t.release(first)
        second=t.attach({'session_name':'two'},'task2',tmp_path,True)
        t.release(second)


def test_disabled_mode_unchanged():
    info={'session_name':'legacy'}
    assert t.attach(info,'task','not-a-profile',False) is info
    assert t.flags(info)==[]


def test_unknown_profile_rejected():
    with pytest.raises(ValueError):t.location('/tmp/foreign-hermes')


def test_monitor_does_not_store_form_text_or_query_tokens(tmp_path):
    with patch.object(t,'location',return_value=tmp_path):
        info=t.attach({'session_name':'one'},'task1',tmp_path,True)
        try:
            t.record(info,'open',{'success':True,'data':{'url':'https://mail.example/inbox?token=secret#private','text':'message body'}},'/tmp/socket')
            state=json.loads((tmp_path/'status.json').read_text())
            assert state['url']=='https://mail.example/inbox'
            assert 'secret' not in json.dumps(state) and 'message body' not in json.dumps(state)
            assert t.flags(info)==['--profile',str(tmp_path/'profile'),'--headed','false']
        finally:t.release(info)
