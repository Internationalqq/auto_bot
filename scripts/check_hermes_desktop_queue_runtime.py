"""Exercise real Hermes plugin discovery/dispatch/hooks in a temporary profile.

Run inside the installed Hermes venv with its repository as working directory.
The noop backend avoids all real apps; no model requests are made.
"""
import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--plugin', type=Path, required=True)
args = parser.parse_args()
with tempfile.TemporaryDirectory(prefix='hermes-desktop-check-') as temp:
    root = Path(temp)
    shutil.copytree(args.plugin, root/'plugins/desktop-queue')
    (root/'config.yaml').write_text('plugins:\n  enabled: [desktop-queue]\n  entries:\n    desktop-queue:\n      lock_path: '+str(root/'desktop.lock')+'\n')
    os.environ['HERMES_HOME'] = str(root)
    os.environ['HERMES_COMPUTER_USE_BACKEND'] = 'noop'
    sys.path.insert(0, str(Path.cwd()))
    # Actual gateway startup: plugins first, builtin tool discovery lazily later.
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    import model_tools
    from tools.registry import registry
    from tools.computer_use import tool
    from hermes_cli.plugins import invoke_hook
    entry = registry._tools['computer_use']
    assert entry.handler.__self__.__class__.__name__ == 'DesktopQueue', 'Plugin was not loaded'
    queue = entry.handler.__self__
    for task in ('first', 'second'):
        result = registry.dispatch('computer_use', {'action':'list_apps'}, task_id=task)
        assert queue.owner == task, result
        assert tool._backend is not None
        invoke_hook('on_session_end', task_id=task, session_id=task, completed=True, interrupted=False)
        assert queue.owner is None
        assert tool._backend is None
    print('PASS: actual Hermes discovery, tool override, two tasks, finalizer hook and backend teardown')
