"""Real supervisor/child boundaries with a local stand-in for the main operation."""
from pathlib import Path
import shutil
import subprocess
import sys
import time

from autobot import main_jobs as jobs


SOURCE = Path(__file__).resolve().parents[1]


def fixture(tmp_path):
    root = tmp_path / 'runtime'
    (root / 'autobot').mkdir(parents=True)
    (root / 'tools').mkdir()
    (root / 'data').mkdir()
    (root / 'autobot/__init__.py').write_text('')
    for name in ('main_jobs.py', 'main_job_runtime.py', 'atomic_output.py', 'paths.py'):
        shutil.copyfile(SOURCE / 'autobot' / name, root / 'autobot' / name)
    for name in ('run_module.py', 'runtime_paths.py'):
        shutil.copyfile(SOURCE / 'tools' / name, root / 'tools' / name)
    # The dispatcher and launcher are the real code. Only the external operation is a fixture.
    (root / 'autobot/main.py').write_text('''from pathlib import Path
import os,time
root=Path(__file__).resolve().parents[1]/'data'
with (root/'calls').open('a') as stream: stream.write('run\\n')
(root/'started').write_text(str(os.getpid()))
deadline=time.monotonic()+20
while not (root/'release').exists() and time.monotonic()<deadline:
    if (root/'kill').exists(): os._exit(91)
    time.sleep(.02)
if not (root/'release').exists(): raise RuntimeError('Fixture timed out')
(root/'artifact').write_bytes(b'published result')
os.write(1,b'Native output after supervisor death\\n')
os.write(2,b'Native stderr after supervisor death\\n')
if (root/'crash_after_publish').exists(): os._exit(91)
print('Fixture result published')
''', encoding='utf-8')
    path = root / 'data/main_jobs.sqlite3'
    row, _ = jobs.enqueue(path, {'kind': 'main', 'argv': ['--from-downloaded-tender-id', '12345678']}, 'Process fixture')
    return root, path, row


def launch_supervisor(root, path):
    code = "import sys;sys.path.insert(0,sys.argv[1]);from autobot.main_job_runtime import recover_and_launch;recover_and_launch(sys.argv[2])"
    return subprocess.Popen([sys.executable, '-c', code, str(root), str(path)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def wait_for(condition, *, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.03)
    raise AssertionError('Process fixture did not reach the expected state')


def release(root):
    (root / 'data/release').touch()


def close_supervisor(child):
    if child.poll() is None:
        child.kill()
    child.communicate(timeout=10)


def test_queued_job_starts_after_reopening_and_child_survives_supervisor_death(tmp_path):
    root, path, row = fixture(tmp_path)
    supervisor = launch_supervisor(root, path)
    try:
        wait_for(lambda: (root / 'data/started').exists())
        assert jobs.get(path, row['run_id'])['status'] == 'running'
        supervisor.kill()
        supervisor.communicate(timeout=10)
        assert not jobs.recover_interrupted(path)
        # A replacement web supervisor sees the existing child and starts no new main operation.
        replacement = launch_supervisor(root, path)
        stdout, _ = replacement.communicate(timeout=10)
        assert replacement.returncode == 0, stdout
        assert (root / 'data/calls').read_text().splitlines() == ['run']
        release(root)
        wait_for(lambda: jobs.latest(path)['status'] == 'completed')
        assert (root / 'data/artifact').read_bytes() == b'published result'
        before = jobs.latest(path)
        again = launch_supervisor(root, path)
        assert again.communicate(timeout=10)[0] == b'' and again.returncode == 0
        assert jobs.latest(path) == before
    finally:
        release(root)
        close_supervisor(supervisor)


def test_two_supervisors_cannot_execute_the_same_saved_job_twice(tmp_path):
    root, path, row = fixture(tmp_path)
    first, second = launch_supervisor(root, path), launch_supervisor(root, path)
    try:
        wait_for(lambda: (root / 'data/started').exists())
        release(root)
        for child in (first, second):
            output, _ = child.communicate(timeout=10)
            assert child.returncode == 0, output
        assert jobs.latest(path)['status'] == 'completed'
        assert (root / 'data/calls').read_text().splitlines() == ['run']
    finally:
        release(root)
        for child in (first, second):
            close_supervisor(child)


def test_executor_death_is_interrupted_and_is_not_automatically_replayed(tmp_path):
    root, path, row = fixture(tmp_path)
    supervisor = launch_supervisor(root, path)
    try:
        wait_for(lambda: (root / 'data/started').exists())
        (root / 'data/kill').touch()
        output, _ = supervisor.communicate(timeout=10)
        assert supervisor.returncode == 0, output
        assert jobs.latest(path)['status'] == 'interrupted'
        before = jobs.latest(path)
        replacement = launch_supervisor(root, path)
        replacement.communicate(timeout=10)
        assert replacement.returncode == 0 and jobs.latest(path) == before
        assert (root / 'data/calls').read_text().splitlines() == ['run']
    finally:
        release(root)
        close_supervisor(supervisor)


def test_death_after_output_preserves_artifact_without_faking_completion(tmp_path):
    root, path, row = fixture(tmp_path)
    (root / 'data/crash_after_publish').touch()
    release(root)
    supervisor = launch_supervisor(root, path)
    try:
        output, _ = supervisor.communicate(timeout=10)
        assert supervisor.returncode == 0, output
        assert jobs.latest(path)['status'] == 'interrupted'
        assert (root / 'data/artifact').read_bytes() == b'published result'
        next_supervisor = launch_supervisor(root, path)
        next_supervisor.communicate(timeout=10)
        assert next_supervisor.returncode == 0
        assert (root / 'data/calls').read_text().splitlines() == ['run']
        assert (root / 'data/artifact').read_bytes() == b'published result'
    finally:
        close_supervisor(supervisor)
