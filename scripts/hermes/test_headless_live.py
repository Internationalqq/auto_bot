"""Mac integration: two real browsers, persistence, per-agent exclusion."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys

BASE=Path('/Users/egor/.hermes')
PYTHON=BASE/'hermes-agent/venv/bin/python'
CHILD=r'''
import os,sys,json,time
sys.path.insert(0,'/Users/egor/.hermes/hermes-agent')
from tools import browser_tool as b
name=sys.argv[1]; phase=sys.argv[2]
task='parallel-'+name
try:
    opened=b._run_browser_command(task,'open',['https://example.com'])
    assert opened['success'],opened
    value=b._run_browser_command(task,'eval',["localStorage.getItem('pm-headless-test')"])
    assert value['success'],value
    expected=None if phase=='first' else name
    assert value['data']['result']==expected, value
    assert b._run_browser_command(task,'eval',["localStorage.setItem('pm-headless-test', '"+name+"')"])['success']
    busy=b._run_browser_command('other-task','open',['https://example.org'])
    assert not busy['success'] and 'agent_browser_busy' in busy['error'],busy
    assert b._run_browser_command(task,'get',['url'])['data']
    time.sleep(3)
    print(json.dumps(dict(agent=name,phase=phase,storage=value['data']['result'],busy_rejected=True)))
finally:b.cleanup_all_browsers()
'''


def check(name,phase):
    env=dict(os.environ,HERMES_HOME=str(BASE/'profiles'/name))
    p=subprocess.run([str(PYTHON),'-c',CHILD,name,phase],env=env,capture_output=True,text=True,timeout=100)
    assert p.returncode==0, p.stdout+'\n'+p.stderr
    return p.stdout.strip()


if __name__=='__main__':
    for phase in ('first','reopen'):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            for out in pool.map(lambda name:check(name,phase),['headless_smoke_a','headless_smoke_b']):print(out)
