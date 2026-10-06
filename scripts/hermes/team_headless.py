"""Persistent, agent-scoped local browsers. No shared desktop or shared cookies.

Loaded by Hermes' browser_tool through three small integration hooks.
The lease covers a complete browser session, not just an individual click.
"""
import json
import os
from pathlib import Path
import threading
import time

_leases = {}
_guard = threading.RLock()


def location(home):
    home = Path(home).resolve()
    base = Path('/Users/egor/.hermes').resolve()
    if home != base and not (home.parent == base / 'profiles' and home.is_dir()):
        raise ValueError('Unknown Hermes profile')
    return home / 'headless-browser'


def write_state(root, data):
    temp = root / f'.status-{os.getpid()}-{threading.get_ident()}.tmp'
    temp.write_text(json.dumps(data, ensure_ascii=False))
    temp.chmod(0o600)
    temp.replace(root / 'status.json')


def attach(info, task_id, home, enabled):
    if not enabled:
        return info
    import fcntl
    root = location(home)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    profile = root / 'profile'
    profile.mkdir(mode=0o700, exist_ok=True)
    with _guard:
        if str(root) in _leases:
            raise RuntimeError('agent_browser_busy: another task of this agent owns its browser; retry later')
        lease = (root / 'profile.lock').open('a+')
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lease.close()
            raise RuntimeError('agent_browser_busy: browser belongs to another task or human login; retry later')
        _leases[str(root)] = (info['session_name'], lease)
        info = dict(info, team_root=str(root), team_profile=str(profile))
        write_state(root, dict(status='starting', pid=os.getpid(), task_id=task_id,
                              session=info['session_name'], updated_at=time.time()))
        return info


def flags(info):
    if not info.get('team_profile'):
        return []
    return ['--profile', info['team_profile'], '--headed', 'false']


def record(info, command, result, socket_dir):
    if not info.get('team_root'):
        return
    root = Path(info['team_root'])
    try:
        state = json.loads((root / 'status.json').read_text())
        state.update(status='ready' if result.get('success') else 'error', command=command,
                     updated_at=time.time(), socket_dir=socket_dir)
        # No form values, cookie data, or message bodies in monitoring metadata.
        data = result.get('data') or {}
        if command == 'open' and isinstance(data, dict):
            from urllib.parse import urlsplit, urlunsplit
            u = urlsplit(data.get('url', ''))
            state['url'] = urlunsplit((u.scheme, u.netloc, u.path, '', ''))
        if not result.get('success'):
            state['error'] = str(result.get('error', 'browser command failed'))[:300]
        else:
            state.pop('error', None)
        write_state(root, state)
    except (OSError, ValueError):
        pass


def release(info):
    root = info.get('team_root') if info else None
    if not root:
        return
    with _guard:
        held = _leases.get(root)
        if held and held[0] == info['session_name']:
            try:
                p = Path(root)
                state = json.loads((p / 'status.json').read_text())
                state.update(status='closed', updated_at=time.time())
                write_state(p, state)
            finally:
                held[1].close()
                del _leases[root]
