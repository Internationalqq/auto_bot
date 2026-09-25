"""Targeted compatibility fix for the installed Hermes/cua-driver adapter.

Run on Mac only after inspecting the installed source. Keeps all prior local
changes and a byte-for-byte backup; does not change approvals or permissions.
"""
from pathlib import Path
import datetime
import hashlib


def patched(source):
    start = source.index('    def click(\n', source.index('class CuaDriverBackend'))
    end = source.index('    # ── Keyboard', start)
    block = source[start:end]
    marker = '# Preserve the captured window for coordinate actions too.'
    if marker in block:
        return source
    for name in ('click', 'drag', 'scroll'):
        old = f'''        if pid is None:
            return ActionResult(ok=False, action="{name}",
                                message="No active window — call capture() first.")'''
        new = old.replace('if pid is None:', 'if pid is None or self._active_window_id is None:')
        assert block.count(old) == 1, name + ': unexpected installed source'
        block = block.replace(old, new)
    for name in ('click', 'drag', 'scroll'):
        old = ('        return self._action(tool, args)' if name == 'click'
               else f'        return self._action("{name}", args)')
        assert block.count(old) == 1
        block = block.replace(old, '        ' + marker + '\n'
                              '        args["window_id"] = self._active_window_id\n' + old)
    return source[:start] + block + source[end:]


if __name__ == '__main__':
    target = Path.home()/'.hermes/hermes-agent/tools/computer_use/cua_backend.py'
    before = target.read_bytes()
    after = patched(before.decode()).encode()
    if after != before:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = target.with_name(target.name + '.autobot-pointer-' + stamp)
        backup.write_bytes(before)
        target.write_bytes(after)
        print('Backup:', backup)
        print('Before sha256:', hashlib.sha256(before).hexdigest())
        print('After sha256:', hashlib.sha256(after).hexdigest())
    else:
        print('Already patched')
