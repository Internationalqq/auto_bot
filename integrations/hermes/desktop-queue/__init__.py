"""One desktop owner per turn, shared by explicitly configured profiles.

Uses Hermes' supported tool override and turn-end hook. Does not modify core,
relax CUA targeting/approval guards, close browser windows or replay input.
"""
import atexit
import fcntl
import json
import os
from pathlib import Path
import threading
import time


class DesktopQueue:
    def __init__(self, path, handler, reset, profile, wait_seconds=30):
        self.path = Path(path)
        self.handler, self.reset, self.profile = handler, reset, profile
        self.wait_seconds = wait_seconds
        self.guard = threading.RLock()
        self.owner = None
        self.handle = None

    def _event(self, event, task_id):
        # No screen, messages, recipient addresses or credentials in this log.
        row = dict(event=event, task_id=task_id, profile=self.profile,
                   pid=os.getpid(), time=time.time())
        with self.path.with_suffix('.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps(row) + '\n')

    def _acquire(self, task_id):
        if self.owner == task_id:
            return True
        if self.owner is not None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.path.open('a+')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self.handle, self.owner = handle, task_id
        try:
            # Compatibility adapter: installed Hermes exposes its teardown
            # under this legacy helper name. Called only with desktop ownership.
            self.reset()
            self._event('acquired', task_id)
        except Exception:
            self._unlock()
            raise
        return True

    def _unlock(self):
        handle, self.handle = self.handle, None
        self.owner = None
        if handle is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def dispatch(self, args, **kwargs):
        task_id = str(kwargs.get('task_id') or '')
        if not task_id:
            return json.dumps({'error': 'desktop_task_id_required'})
        deadline = time.monotonic() + self.wait_seconds
        while True:
            with self.guard:
                try:
                    acquired = self._acquire(task_id)
                except Exception:
                    return json.dumps({'error': 'desktop_queue_unavailable',
                                       'hint': 'Desktop was not touched. Check the desktop-queue configuration.'})
                if acquired:
                    # Serializes concurrent tool calls in the same turn too.
                    # The original handler keeps all validation and approvals.
                    return self.handler(args, **kwargs)
            if time.monotonic() >= deadline:
                return json.dumps({'error': 'desktop_busy', 'retryable': True,
                    'hint': 'Another Hermes task owns the desktop. No action was executed. '
                            'Retry computer_use later; do not use another tool to bypass the queue.'})
            time.sleep(min(.25, max(0, deadline - time.monotonic())))

    def finish(self, task_id='', **kwargs):
        with self.guard:
            if self.owner is None or self.owner != task_id:
                return
            try:
                self.reset()
                self._event('released', task_id)
            finally:
                self._unlock()

    def close(self):
        if self.owner is not None:
            self.finish(task_id=self.owner)


def register(ctx):
    from hermes_cli.config import load_config
    # Gateway discovers plugins before run_agent/model_tools. Import the shim
    # now so later lazy builtin discovery cannot overwrite our registration.
    import tools.computer_use_tool  # noqa: F401
    from tools.computer_use.tool import (
        handle_computer_use, reset_backend_for_tests,
        check_computer_use_requirements, get_computer_use_schema,
    )
    settings = load_config().get('plugins', {}).get('entries', {}).get('desktop-queue', {})
    path = Path(settings['lock_path']).expanduser()
    if not path.is_absolute():
        raise ValueError('desktop-queue.lock_path must be absolute and shared by participating profiles')
    queue = DesktopQueue(path, handle_computer_use, reset_backend_for_tests,
                         ctx.profile_name, wait_seconds=30)
    ctx.register_tool(name='computer_use', toolset='computer_use',
                      schema=get_computer_use_schema(), handler=queue.dispatch,
                      check_fn=check_computer_use_requirements, override=True)
    ctx.register_hook('on_session_end', queue.finish)
    queue.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    queue._event('registered', '')
    atexit.register(queue.close)
