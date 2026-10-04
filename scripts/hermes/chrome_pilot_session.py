"""Supply the user's renewed, time-limited Chrome consent to one Hermes session.

Uses the CLI's existing approval callback. Does not edit Hermes or grant shell,
mail, other-app, permanent, or session-wide approval.
"""
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit


def chrome_help_strip(window, state):
    sc = state.get('structuredContent') or {}
    bounds = window.get('bounds') or sc.get('window_bounds') or {}
    return (window.get('app_name') == 'Google Chrome' and not window.get('title')
            and sc.get('elements') == []
            and 0 < bounds.get('height', 0) <= 24
            and bounds.get('width', 0) > 0)


def chrome_omnibox_popup(window, state):
    elements = (state.get('structuredContent') or {}).get('elements') or []
    return (window.get('app_name') == 'Google Chrome' and not window.get('title')
            and any(e.get('role') == 'AXWebArea' and e.get('label') == 'Omnibox Popup' for e in elements)
            and not any(e.get('role') in ('AXDialog', 'AXSheet') for e in elements))


def select_chrome_content(windows, select):
    """Skip at most two observed Chrome overlays, proving tooltip identity.

    Screenshot pixel sizes change with display scaling. The empty hover
    strip must instead match an AXHelpTag's logical frame in the main tree.
    Never skip an unknown overlay or select another process's window.
    """
    selected, state = select(windows)
    pid = selected.get('pid')
    skipped = []
    strips = []
    for _ in range(3):
        strip = chrome_help_strip(selected, state)
        omnibox = chrome_omnibox_popup(selected, state)
        if not strip and not omnibox:
            if not skipped:
                return selected, state
            elements = (state.get('structuredContent') or {}).get('elements') or []
            main = any(e.get('role') == 'AXWindow' and 'Google Chrome' in e.get('label', '') for e in elements)
            help_frames = [e.get('frame') or {} for e in elements if e.get('role') == 'AXHelpTag']
            def matches(bounds, frame):
                return all(abs(bounds.get(a, -9999) - frame.get(b, 9999)) <= 1
                           for a,b in [('x','x'),('y','y'),('width','w'),('height','h')])
            if not main or any(not any(matches(bounds,frame) for frame in help_frames) for bounds in strips):
                raise RuntimeError('Unrecognized Chrome overlay; content selection stopped')
            return selected, state
        skipped.append(selected['window_id'])
        if strip:
            strips.append(selected.get('bounds') or (state.get('structuredContent') or {}).get('window_bounds') or {})
        remaining = [w for w in windows if w['window_id'] not in skipped and w['pid'] == pid]
        if not remaining:
            break
        selected, state = select(remaining)
    raise RuntimeError('Chrome overlays have no verified content window')


def decide(consent, action, args, now):
    if consent.get('scope') != 'volga-google-public-product-search':
        return 'deny'
    if not consent.get('starts_at', 0) <= now < consent.get('expires_at', 0):
        return 'deny'
    if args.get('app') != 'Google Chrome':
        return 'deny'
    if action in ('type', 'set_value'):
        value = args.get('text', args.get('value', ''))
        if not isinstance(value, str) or not value.strip() or len(value) > 1000 or any(ord(c) < 32 for c in value):
            return 'deny'
        if re.match(r'^[a-z][a-z0-9+.-]*:', value, re.I):
            u = urlsplit(value)
            if u.scheme != 'https' or not u.hostname or u.username or u.password:
                return 'deny'
            if any(s in u.hostname for s in ('mail.', 'accounts.', 'account.', 'web.telegram.', 'web.whatsapp.')):
                return 'deny'
        elif any(c in value for c in (';', '|', '`', '\\')):
            return 'deny'
    elif action == 'key':
        key = args.get('keys', '').lower().replace('command', 'cmd').replace('control', 'ctrl').replace(' ', '')
        if key not in {'cmd+l', 'cmd+a', 'cmd+t', 'cmd+r', 'cmd+[', 'cmd+]', 'enter', 'return', 'escape', 'tab', 'shift+tab', 'backspace', 'down', 'up', 'pagedown', 'pageup'}:
            return 'deny'
    elif action not in {'click', 'scroll', 'focus_app'}:
        return 'deny'
    return 'approve_once'


def main():
    batch = Path(sys.argv[1]).resolve()
    root = batch.parent
    consent_path = root / 'chrome-consent.json'
    consent = json.loads(consent_path.read_text())
    if not consent['starts_at'] <= time.time() < consent['expires_at']:
        raise SystemExit('Chrome consent expired')
    if consent.get('mode') == 'full_tender':
        manifest = json.loads((root / 'input.json').read_text())['source']
        rows = manifest['positions']
        assert manifest['tender_id'] == '0171200001926000664'
        assert 1 <= len(rows) <= 2000
        assert re.fullmatch(r'batch-[1-9][0-9]*', batch.name)
        index = int(batch.name.split('-')[1]) - 1
        assert 0 <= index < len(rows)
        assert json.loads((batch / 'positions.json').read_text()) == [rows[index]]
    else:
        assert batch.name in {f'batch-{i}' for i in range(1, 11)}
    assert os.environ['HERMES_HOME'] == '/Users/egor/.hermes/profiles/commercial'
    import cli
    expected_path = root / 'expected-model.json'
    if expected_path.exists():
        expected = json.loads(expected_path.read_text())
        from hermes_cli.config import load_config
        configured = load_config()
        assert configured['model']['default'] == expected['model']
        assert cli.CLI_CONFIG['agent']['reasoning_effort'] == expected['reasoning_effort']
        original_cli_init = cli.HermesCLI.__init__
        def checked_cli_init(self, *args, **kwargs):
            original_cli_init(self, *args, **kwargs)
            assert self.reasoning_config.get('effort') == expected['reasoning_effort']
            (batch/'runtime-model.json').write_text(json.dumps({
                'model':configured['model']['default'], 'reasoning':self.reasoning_config}))
        cli.HermesCLI.__init__ = checked_cli_init
    from tools.computer_use.cua_backend import CuaDriverBackend
    original_select = CuaDriverBackend._select_content_window
    original_init = CuaDriverBackend.__init__

    def init_chrome(self, *args, **kwargs):
        if 'Google Chrome' in (kwargs.get('allowed_apps') or []):
            # Keep this price-search pilot narrower than the profile's general
            # permissions (which may also permit Telegram for other tasks).
            kwargs['allowed_apps'] = ['Google Chrome']
            # Supported exact-window foreground delivery, process-local only.
            kwargs['keyboard_delivery_mode'] = 'foreground'
        original_init(self, *args, **kwargs)

    CuaDriverBackend.__init__ = init_chrome

    def select_content(self, windows):
        return select_chrome_content(windows, lambda candidates: original_select(self, candidates))

    CuaDriverBackend._select_content_window = select_content

    def approved(self, action, args, summary):
        verdict = decide(consent, action, args, time.time())
        with (batch / 'approval-audit.jsonl').open('a') as log:
            log.write(json.dumps({'at': time.time(), 'action': action, 'app': args.get('app'), 'verdict': verdict}) + '\n')
        return verdict

    cli.HermesCLI._computer_use_approval_callback = approved
    prompt = (batch / 'prompt.txt').read_text() + '''
Уточнение по проверенному поведению Chrome: переходы по результатам Google
делай click по element индексу ссылки из свежего capture, не по координатам
уменьшенной картинки (они могут попасть мимо). Если клик не перешёл, бери
наблюдаемый URL ссылки из дерева и открывай через cmd+l/type/return. Не
придумывай URL. После одной карточки сразу сохрани результат; незакрытая
модель или сайт с ошибкой не должны съесть время второй позиции.
'''
    cli.main(query=prompt, quiet=True,
             toolsets='computer_use,file', max_turns=24)


if __name__ == '__main__':
    main()
