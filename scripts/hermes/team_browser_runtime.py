"""Operator-configured browser approvals and bounded process recovery for Hermes.

No model calls, mail sends, account changes, or global approval changes.
Only the assigned application under the shared browser lock may be recovered.
"""
import json
import os
from pathlib import Path
import signal
import time

TEAM = Path('/Users/egor/.hermes/team-browser-access')
ACTIONS = {'focus_app', 'click', 'double_click', 'right_click', 'middle_click',
           'scroll', 'drag', 'type', 'key', 'set_value'}
BROWSERS = {'Firefox', 'Safari', 'Google Chrome'}


def profile_name():
    home = Path(os.environ.get('HERMES_HOME', str(Path.home()/'.hermes'))).resolve()
    root = Path('/Users/egor/.hermes').resolve()
    if home == root:return 'default'
    return home.name if home.parent == root/'profiles' else ''


def save_state(path, state):
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def policy_context(app, team=TEAM):
    try:
        policy = json.loads((team/'unattended-policy.json').read_text())
        name = profile_name()
        apps = policy['profiles'].get(name, [])
        if app not in apps:
            return None
        active = json.loads((team/'state/active.json').read_text())
        if active.get('owner') != name:
            return None
        return policy
    except (OSError, ValueError, KeyError, TypeError):
        return None


def approval(action, args, team=TEAM):
    """An explicit per-app grant, never an approval for shell or another app."""
    app = args.get('app')
    if action in ACTIONS and policy_context(app, team):
        return 'approve_once'
    try:
        policy = json.loads((team/'unattended-policy.json').read_text())
        if profile_name() in policy['profiles']:
            return 'deny:assigned_app_and_browser_lock_required'
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def retryable_window_error(message):
    s = str(message).lower()
    if any(x in s for x in ['permission', 'denied', 'unauthorized', 'revoked', 'session has ended']):
        return False
    return any(x in s for x in ['no on-screen window', 'no application content window',
                               'no verified content window', 'window not found',
                               'no usable browser content', 'not verified as frontmost and focused',
                               'target visibility is not confirmed'])


def usable_capture(backend, app):
    cap = backend.capture(mode='ax', app=app)
    if not cap.elements:
        raise RuntimeError('No usable browser content')
    return cap


def recover_browser(backend, app, team=TEAM, sleep=time.sleep, now=time.time):
    """Recover visibility, then gracefully restart only the assigned browser.

    Never replay the failed click/type/send. Caller must capture and inspect again.
    Real permission denials are propagated. One restart per five minutes, at most
    three per hour; exhausted recovery requires operator attention.
    """
    if app not in BROWSERS or not policy_context(app, team):
        raise RuntimeError('Browser recovery needs an assigned browser and owned browser lock')
    try:
        return usable_capture(backend, app)
    except RuntimeError as e:
        if not retryable_window_error(e):
            raise
    focus = backend.focus_app(app, raise_window=True)
    if not focus.ok and not retryable_window_error(focus.message):
        raise RuntimeError(focus.message)
    if focus.ok:
        try:
            return usable_capture(backend, app)
        except RuntimeError as e:
            if not retryable_window_error(e):
                raise
    # Idempotent open: no URLs/extra arguments, no second browser instance.
    backend.launch_app(name=app, creates_new_application_instance=False)
    sleep(2)
    try:
        return usable_capture(backend, app)
    except RuntimeError as e:
        if not retryable_window_error(e):
            raise
    statefile = team/('recovery-'+profile_name()+'.json')
    state = json.loads(statefile.read_text()) if statefile.exists() else {}
    recent = [t for t in state.get('restarts', []) if now()-t < 3600]
    if recent and (now()-recent[-1] < 300 or len(recent) >= 3):
        raise RuntimeError('Browser recovery cooldown: waiting before another restart')
    apps = backend.list_apps()
    matches = [a for a in apps if (a.get('name') or a.get('app_name')) == app]
    if len(matches) != 1 or not isinstance(matches[0].get('pid'), int):
        raise RuntimeError('Cannot verify one exact browser process for recovery')
    pid = matches[0]['pid']
    recent.append(now())
    state.update(app=app, pid=pid, restarts=recent, status='restarting', at=now())
    save_state(statefile,state)
    # User authorized browser close/reopen on 6 October. SIGTERM preserves the
    # browser's normal shutdown; never SIGKILL or delete cookies/profile files.
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        sleep(1)
        current = backend.list_apps()
        if not any(a.get('pid') == pid for a in current):
            break
    else:
        state.update(status='needs_attention', reason='Browser did not exit gracefully')
        save_state(statefile,state)
        raise RuntimeError(state['reason'])
    backend.launch_app(name=app, creates_new_application_instance=False)
    for _ in range(10):
        sleep(2)
        backend.focus_app(app, raise_window=True)
        try:
            cap = usable_capture(backend, app)
            state.update(status='ready', at=now())
            save_state(statefile,state)
            return cap
        except RuntimeError as e:
            if not retryable_window_error(e):
                raise
    state.update(status='needs_attention', reason='Browser reopened without usable content')
    save_state(statefile,state)
    raise RuntimeError(state['reason'])
