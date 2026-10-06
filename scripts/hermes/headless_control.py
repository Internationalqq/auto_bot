"""Operator controls for a single agent: inspect, human login, finish login.

Manual control uses the bundled loopback-only dashboard and stream.
Browser processes remain headless, including during human login.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import time
import uuid

BASE = Path('/Users/egor/.hermes')
sys.path.insert(0, str(BASE / 'hermes-agent'))
from tools.team_headless import attach, release, write_state


def run(info, command, args=(), human=False):
    env = dict(os.environ, AGENT_BROWSER_SOCKET_DIR=info['socket_dir'],
               AGENT_BROWSER_IDLE_TIMEOUT_MS='0' if human else '1800000')
    cmd = [str(BASE/'node/bin/agent-browser'), '--session', info['session'],
           '--profile', str(Path(info['team_root'])/'profile'),
           '--headed', 'false', '--json', command, *args]
    # Daemon inherits descriptors: regular files, not pipes, avoid an EOF hang.
    root = Path(info['team_root'])
    with (root/'operator-output.json').open('w+') as out, (root/'operator-error.log').open('w') as err:
        p = subprocess.run(cmd, env=env, stdout=out, stderr=err, timeout=45)
        out.seek(0)
        try: result = json.load(out)
        except ValueError: result = {'success': False, 'error': f'CLI exit {p.returncode}'}
    return result


def daemon_alive(info):
    """Inspect process state only; never start a daemon just to check it."""
    try:
        pidfile=Path(info['socket_dir'])/(info['session']+'.pid')
        pid=int(pidfile.read_text().strip())
        process=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True)
        return process.returncode==0 and 'agent-browser' in process.stdout
    except (OSError,ValueError,KeyError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['peek', 'login', 'finish-login'])
    parser.add_argument('profile')
    parser.add_argument('--url', default='https://mail.ru/')
    args = parser.parse_args()
    if not args.profile.replace('_','').replace('-','').isalnum():
        raise SystemExit('Invalid profile')
    home = BASE if args.profile == 'default' else BASE/'profiles'/args.profile
    root = home/'headless-browser'
    if args.action == 'finish-login':
        state = json.loads((root/'status.json').read_text())
        if state.get('status') != 'human_login':
            raise SystemExit('No human login session')
        (root/'finish-login').touch()
        print('Manual browser session will close; profile will be retained.')
        return
    if args.action == 'peek':
        state = json.loads((root/'status.json').read_text())
        if state['status'] not in ('ready', 'human_login', 'error'):
            raise SystemExit('No active browser; latest metadata: '+json.dumps(state))
        # Never silently create a replacement session just to inspect it.
        if not daemon_alive(state):
            raise SystemExit('Browser daemon ended; stale metadata retained')
        info = dict(state, team_root=str(root))
        output = root/'latest.png'
        result = run(info, 'screenshot', [str(output)], state['status']=='human_login')
        print(json.dumps({'screenshot':str(output), 'result':result},ensure_ascii=False))
        return
    from urllib.parse import urlsplit
    if urlsplit(args.url).scheme != 'https':
        raise SystemExit('Login requires an HTTPS URL')
    session = 'login_'+args.profile+'_'+uuid.uuid4().hex[:8]
    info = attach({'session_name':session}, 'human-login', home, True)
    from tools.browser_tool import _socket_safe_tmpdir
    socket = Path(_socket_safe_tmpdir())/'agent-browser-team-manual'
    socket.mkdir(mode=0o700, exist_ok=True)
    state = dict(session=session, team_root=str(root),socket_dir=str(socket),
                 status='human_login',pid=os.getpid(),updated_at=time.time(),headless=True)
    stop = root/'finish-login'
    stop.unlink(missing_ok=True)
    try:
        dashboard = run(state, 'dashboard', ['start'], True)
        if not dashboard.get('success'):
            raise RuntimeError('Manual browser dashboard is unavailable')
        result = run(state,'open',[args.url], True)
        if not result.get('success'):
            # Navigation can exceed the page timeout while the login page is
            # already rendered. Inspect instead of repeatedly opening windows.
            observed = run(state,'get',['url'],True)
            if not observed.get('success'):
                raise RuntimeError(result.get('error'))
            state['navigation_warning'] = str(result.get('error'))[:200]
        viewport = run(state, 'set', ['viewport', '1280', '720'], True)
        if not viewport.get('success'):
            raise RuntimeError('Could not establish the browser viewport')
        stream = run(state, 'stream', ['status'], True)
        port = (stream.get('data') or {}).get('port')
        if not stream.get('success') or not isinstance(port, int):
            raise RuntimeError('Manual browser stream is unavailable')
        state['stream_port'] = port
        state['viewer_url'] = f'http://localhost:4850/?port={port}'
        write_state(root,state)
        print('Headless manual session ready: '+state['viewer_url'],flush=True)
        def stop_login(signum, frame):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM,stop_login)
        while not stop.exists():
            time.sleep(2)
            if not daemon_alive(state):
                print('Login browser ended; releasing this profile.',flush=True)
                break
    finally:
        if daemon_alive(state):
            run(state,'close',human=True)
        release(info)
        stop.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
