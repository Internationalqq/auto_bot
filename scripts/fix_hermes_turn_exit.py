"""Compatibility patch: emit a generic cleanup hook on every synchronous exit.

The installed Hermes skips on_session_end on early provider failures. Existing
hooks keep their semantics; on_turn_exit is an additional finally hook. No
plugin-specific behavior, prompt changes, retry or approval changes in core.
"""
import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path


def patch_forwarder(source):
    start = source.index('    def run_conversation(\n')
    end = source.index('\n    def chat(', start)
    block = source[start:end]
    if '"on_turn_exit"' in block:
        return source
    anchor = '        return run_conversation(\n'
    assert block.count(anchor) == 1, 'Unexpected Hermes forwarder'
    offset = block.index(anchor)
    call = block[offset:].rstrip()
    replacement = '        try:\n' + '\n'.join('    '+line for line in call.splitlines())
    replacement += '''
        finally:
            # Resource owners need cleanup on early returns/exceptions too.
            # The normal finalizer's on_session_end may not have been reached.
            try:
                from hermes_cli.plugins import invoke_hook
                invoke_hook(
                    "on_turn_exit",
                    session_id=getattr(self, "session_id", ""),
                    task_id=task_id or getattr(self, "_current_task_id", ""),
                )
            except Exception:
                logger.warning("on_turn_exit hook failed", exc_info=True)
'''
    return source[:start] + block[:offset] + replacement + source[end:]


def patch_hooks(source):
    if '    "on_turn_exit",' in source:
        return source
    anchor = '    "on_session_end",\n'
    assert source.count(anchor) == 1, 'Unexpected Hermes hook catalog'
    return source.replace(anchor, anchor + '    "on_turn_exit",\n')


def install(root):
    pending = []
    for relative, patch in [('run_agent.py', patch_forwarder), ('hermes_cli/plugins.py', patch_hooks)]:
        path = root/relative
        before = path.read_bytes()
        after = patch(before.decode('utf-8')).encode('utf-8')
        compile(after, str(path), 'exec')
        if before != after:
            pending.append((path, before, after))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    for path, before, after in pending:
        backup = path.with_name(path.name + '.turn-exit-' + stamp)
        backup.write_bytes(before)
        path.write_bytes(after)
        print(path.name, 'backup', backup, 'sha256', hashlib.sha256(after).hexdigest())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    install(parser.parse_args().root)
