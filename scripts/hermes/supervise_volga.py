"""Resume the existing Volga runner after recoverable browser/process failures.

Bound to its original deadline and existing queue. API/access denials stop the
supervisor; they are never treated as browser failures or retried here.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

BASE=Path('/Users/egor/.hermes/profiles/commercial/workspace/volga-chrome-pilot-20261004')
ROOT=BASE/'full-tender-20261004'


def decision(state, alive, now):
    if now >= state['deadline'] or state.get('status')=='finished':return 'complete'
    if alive:return 'running'
    batches=state.get('batches',[])
    last=batches[-1] if batches else {}
    if last.get('stop_reason')=='access_challenge':return 'needs_attention'
    if state.get('status') in ('running','starting','waiting_for_browser','interrupted'):return 'preflight'
    if state.get('status')=='needs_attention' and last.get('exit_code')==0:
        if any(i.get('outcome')=='browser_error' for i in last.get('result',{}).get('items',[])):
            return 'preflight'
    return 'needs_attention'


def alive(pid):
    if not pid:return False
    try:
        line=subprocess.check_output(['ps','-p',str(pid),'-o','command='],text=True).strip()
        return 'run_volga_full.py' in line
    except subprocess.CalledProcessError:return False


def main():
    import fcntl
    os.environ['HERMES_HOME']='/Users/egor/.hermes/profiles/commercial'
    os.environ['PATH']='/Users/egor/.local/bin:/opt/homebrew/bin:'+os.environ.get('PATH','')
    sys.path[:0]=['/Users/egor/.hermes/hermes-agent','/Users/egor/.hermes/team-browser-access']
    from browser_lock import operation
    from tools.computer_use.cua_backend import CuaDriverBackend
    from tools.computer_use.team_runtime import recover_browser, retryable_window_error
    mutex=(ROOT/'supervisor.lock').open('a+')
    fcntl.flock(mutex,fcntl.LOCK_EX|fcntl.LOCK_NB)
    statusfile=ROOT/'supervisor-state.json'
    def report(status, **kw):
        tmp=statusfile.with_suffix('.tmp')
        tmp.write_text(json.dumps(dict(status=status,updated_at=time.time(),pid=os.getpid(),**kw)))
        tmp.replace(statusfile)
    while True:
        state=json.loads((ROOT/'run-state.json').read_text())
        if (ROOT/'stop-request').exists():report('stopped');return
        action=decision(state,alive(state.get('pid')),time.time())
        if action in ('complete','needs_attention'):
            report(action,completed=state.get('completed'));return
        if action=='running':
            report('running',current=state.get('current'),completed=state.get('completed'))
            time.sleep(30);continue
        headless=(ROOT/'headless-enabled.json').exists()
        lock_state=ROOT/'headless-queue-lock' if headless else Path('/Users/egor/.hermes/team-browser-access/state')
        held=operation(lock_state,'acquire','commercial')
        if held['status']!='acquired':
            report('waiting_for_browser',owner=held.get('owner'));time.sleep(60);continue
        b=None
        try:
            if headless:
                from tools import browser_tool
                try:
                    check=browser_tool._run_browser_command('volga-preflight','open',['about:blank'])
                    if not check.get('success'):raise RuntimeError(check.get('error','headless preflight failed'))
                finally:
                    browser_tool.cleanup_browser('volga-preflight')
            else:
                b=CuaDriverBackend(allowed_apps=['Google Chrome'])
                b.start()
                recover_browser(b,'Google Chrome')
        except Exception as e:
            message=str(e)
            retryable=('agent_browser_busy' in message) if headless else (retryable_window_error(message) or 'cooldown' in message)
            report('waiting_for_browser' if retryable else 'needs_attention',reason=message[:500])
            if not retryable:return
        else:
            # Recheck after recovery; never start a second runner/child.
            procs=subprocess.check_output(['ps','-axo','command'],text=True)
            if not any('run_volga_full.py' in x or 'ivan_pilot_session.py' in x or 'volga_headless_session.py' in x for x in procs.splitlines()):
                with (ROOT/'supervised-runner.log').open('a') as log:
                    p=subprocess.Popen([sys.executable,str(BASE/'run_volga_full.py')],cwd=BASE,
                        stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                report('resumed',runner_pid=p.pid,completed=state.get('completed'))
        finally:
            if b:b.stop()
            operation(lock_state,'release','commercial',held['ticket'])
        time.sleep(30)


if __name__=='__main__':main()
