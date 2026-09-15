"""Execute one saved plan in a process whose lifetime is independent of Flask."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import traceback

from autobot import main_jobs as jobs
from autobot.paths import REPO_ROOT


class JobLog(io.TextIOBase):
    def __init__(self, path, run_id, initial):
        self.path, self.run_id = path, run_id
        self.lines, self.pending = list(initial), ''
        self._guard = threading.RLock()

    @property
    def encoding(self):
        return 'utf-8'

    def writable(self):
        return True

    def write(self, text):
        size = len(text)
        with self._guard:
            parts = (self.pending + text[-jobs.MAX_LOG_LINE * jobs.MAX_LOG_LINES:]).replace('\r', '\n').split('\n')
            self.pending = parts.pop()[:jobs.MAX_LOG_LINE]
            if parts:
                self.lines.extend(line[:jobs.MAX_LOG_LINE] for line in parts)
                self.lines = self.lines[-jobs.MAX_LOG_LINES:]
                jobs.progress(self.path, self.run_id, self.lines)
        return size

    def flush(self):
        with self._guard:
            if self.pending:
                self.lines.append(self.pending)
                self.lines = self.lines[-jobs.MAX_LOG_LINES:]
                self.pending = ''
            jobs.progress(self.path, self.run_id, self.lines)


def _run_cli(argv):
    previous = sys.argv
    try:
        sys.argv = ['autobot.main', *argv]
        runpy.run_module('autobot.main', run_name='__main__')
        return 0
    except SystemExit as error:
        if error.code is None:
            return 0
        if isinstance(error.code, int):
            return error.code
        print(str(error.code))
        return 1
    finally:
        sys.argv = previous


def run_job(path, run_id, *, execute=None):
    """The execution lock remains in this process while main.py and its children run."""
    execute = execute or _run_cli
    try:
        with jobs.execution_lock(path, timeout=1):
            current = jobs.get(path, run_id)
            if current is None or current['status'] != 'queued':
                return 0
            try:
                plan = jobs.validate_plan(current['plan'])
            except ValueError:
                jobs.launch_failed(path, run_id)
                return 1
            row = jobs.claim(path, run_id)
            if row is None:
                return 0
            log = JobLog(path, run_id, ['Запуск: ' + row['task']])
            code = 1
            with redirect_stdout(log), redirect_stderr(log):
                try:
                    plans = ([['--from-downloaded-tender-id', tid] for tid in plan['tender_ids']]
                             if plan['kind'] == 'batch' else [plan['argv']])
                    failures = 0
                    for index, argv in enumerate(plans, 1):
                        if plan['kind'] == 'batch':
                            print(f'--- [{index}/{len(plans)}] {argv[-1]} ---')
                        try:
                            item_code = execute(argv)
                        except Exception:
                            traceback.print_exc(limit=8)
                            item_code = 1
                        failures += item_code != 0
                        print('--- Завершено, код ' + str(item_code) + ' ---')
                        log.flush()
                        jobs.progress(path, run_id, log.lines, done=index)
                    code = item_code if plan['kind'] == 'main' else (0 if failures == 0 else 1)
                    print('Задание завершено.' if not failures else 'Есть ошибки. Проверьте журнал и исходные файлы.')
                finally:
                    log.flush()
                    jobs.finish(path, run_id, code)
                    log.close()
            return code
    except TimeoutError:
        # Another child owns execution; never mark its job failed or repeat its work.
        return 75


def launch(path, run_id, *, env=None):
    command = [sys.executable, '-u', str(REPO_ROOT / 'tools' / 'run_module.py'),
               'autobot.main_job_runtime', str(Path(path).resolve()), run_id]
    try:
        child = subprocess.Popen(command, cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            start_new_session=sys.platform != 'win32',
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
    except OSError:
        jobs.launch_failed(path, run_id)
        return -1
    child.wait()
    row = jobs.get(path, run_id)
    if row and row['status'] == 'queued':
        jobs.launch_failed(path, run_id)
    if child.returncode not in (0, 75):
        jobs.recover_interrupted(path)
    return child.returncode


def recover_and_launch(path, *, env=None):
    jobs.recover_interrupted(path)
    row = jobs.latest(path)
    if row and row['status'] == 'queued':
        return launch(path, row['run_id'], env=env)
    return None


def start_recovery(path, *, env=None):
    if not Path(path).exists():
        return
    threading.Thread(target=recover_and_launch, kwargs={'path': path, 'env': env},
                     name='autobot-main-job-recovery', daemon=True).start()


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: main_job_runtime <store> <run_id>')
    raise SystemExit(run_job(Path(sys.argv[1]), sys.argv[2]))
