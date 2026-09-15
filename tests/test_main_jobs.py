import concurrent.futures
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from autobot import main_jobs as jobs
from autobot import main_job_runtime as runtime


PLAN = {'kind': 'main', 'argv': ['--from-downloaded-tender-id', '12345678']}


def test_no_job_read_does_not_create_a_store(tmp_path):
    path = tmp_path / 'jobs.sqlite3'
    assert jobs.latest(path) is None
    assert jobs.get(path, 'a' * 32) is None
    assert not jobs.recover_interrupted(path)
    assert not path.exists()


def test_job_is_immutable_and_duplicate_is_idempotent(tmp_path):
    path, run_id = tmp_path / 'jobs.sqlite3', 'a' * 32
    plan = copy.deepcopy(PLAN)
    saved, duplicate = jobs.enqueue(path, plan, 'Разбор', '12345678', run_id=run_id)
    assert not duplicate and saved['status'] == 'queued'
    plan['argv'][1] = '87654321'
    retry, duplicate = jobs.enqueue(path, PLAN, 'Разбор', '12345678', run_id=run_id)
    assert duplicate and retry == saved
    with pytest.raises(jobs.JobConflict):
        jobs.enqueue(path, plan, 'Разбор', '87654321', run_id=run_id)
    assert jobs.get(path, run_id) == saved
    assert jobs.public_status(saved)['running']
    assert 'plan' not in jobs.public_status(saved) and 'argv' not in json.dumps(jobs.public_status(saved))


def test_concurrent_admission_keeps_one_active_job(tmp_path):
    path = tmp_path / 'jobs.sqlite3'
    barrier = threading.Barrier(2)
    def attempt(index):
        barrier.wait()
        try:
            jobs.enqueue(path, PLAN, 'Разбор', run_id=str(index) * 32)
            return 'accepted'
        except jobs.JobBusy:
            return 'busy'
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(attempt, (1, 2))) == ['accepted', 'busy']


def test_completion_next_job_and_late_updates_keep_history(tmp_path):
    path = tmp_path / 'jobs.sqlite3'
    row, _ = jobs.enqueue(path, PLAN, 'Разбор')
    with jobs.execution_lock(path):
        assert jobs.claim(path, row['run_id'])['status'] == 'running'
        assert jobs.claim(path, row['run_id']) is None
        with pytest.raises(jobs.JobBusy):
            jobs.enqueue(path, PLAN, 'Другой разбор')
        jobs.progress(path, row['run_id'], ['Проверено'], done=1)
        jobs.finish(path, row['run_id'], 0)
    saved = jobs.get(path, row['run_id'])
    assert saved['status'] == 'completed' and saved['done'] == 1
    jobs.progress(path, row['run_id'], ['Поздний старый поток'])
    jobs.finish(path, row['run_id'], 1)
    assert jobs.get(path, row['run_id']) == saved
    following, _ = jobs.enqueue(path, PLAN, 'Новый разбор')
    assert following['sequence'] > saved['sequence']
    assert jobs.get(path, row['run_id']) == saved
    jobs.launch_failed(path, following['run_id'])
    assert jobs.latest(path)['status'] == 'failed'


def test_log_is_bounded_and_queued_job_is_not_marked_interrupted(tmp_path):
    path = tmp_path / 'jobs.sqlite3'
    row, _ = jobs.enqueue(path, PLAN, 'Разбор')
    assert not jobs.recover_interrupted(path)
    with jobs.execution_lock(path):
        jobs.claim(path, row['run_id'])
        jobs.progress(path, row['run_id'], ['x' * 10000] * 1000, done=900)
        assert not jobs.recover_interrupted(path)
    row = jobs.latest(path)
    assert len(row['logs']) == 300 and max(map(len, row['logs'])) == 1500
    assert row['done'] == row['total'] == 1
    public = jobs.public_status(row)
    assert len(public['log_tail']) == 80
    assert jobs.recover_interrupted(path)
    saved = jobs.latest(path)
    assert saved['status'] == 'interrupted' and not jobs.public_status(saved)['running']
    assert not jobs.recover_interrupted(path)
    assert jobs.latest(path) == saved


@pytest.mark.parametrize('plan', [
    {'kind': 'shell', 'argv': ['echo', 'bad']},
    {'kind': 'main', 'argv': ['--emit-new-ids-to', '../private']},
    {'kind': 'main', 'argv': ['--from-downloaded-tender-id', '../private']},
    {'kind': 'main', 'argv': ['--from-tender-id', '12345678', '--from-tender-url', 'http://zakupki.gov.ru/x']},
    {'kind': 'main', 'argv': ['--from-tender-id', '12345678', '--from-tender-url', 'https://zakupki.gov.ru.evil.example/x']},
    {'kind': 'main', 'argv': ['--from-tender-id', '12345678', '--from-tender-url', 'https://u:p@zakupki.gov.ru/x']},
    {'kind': 'main', 'argv': ['--max-pages', '21', '--max-tenders', '1', '--days-back', '1']},
    {'kind': 'main', 'argv': ['--max-pages', '1', '--max-tenders', '1', '--days-back', '1', '--catalog-only', '--resume-downloads']},
    {'kind': 'main', 'argv': ['--from-downloaded-tender-id', '12345678', '--catalog-only']},
    {'kind': 'main', 'argv': ['--from-downloaded-tender-id']},
    {'kind': 'batch', 'tender_ids': ['12345678', '12345678']},
    {'kind': 'batch', 'tender_ids': []},
    {'kind': 'batch', 'tender_ids': ['12345678'], 'command': 'wrong'},
])
def test_invalid_plans_do_not_create_records(tmp_path, plan):
    path = tmp_path / 'jobs.sqlite3'
    with pytest.raises(ValueError):
        jobs.enqueue(path, plan, 'Проверка')
    assert not path.exists()


def test_batch_snapshot_and_direct_download_are_supported(tmp_path):
    ids = ['12345678', '87654321']
    row, _ = jobs.enqueue(tmp_path / 'jobs.sqlite3', {'kind': 'batch', 'tender_ids': ids}, 'Все отчёты')
    ids.clear()
    assert row['plan']['tender_ids'] == ['12345678', '87654321'] and row['total'] == 2
    direct = {'kind': 'main', 'argv': ['--from-tender-id', '12345678', '--from-tender-url', 'https://zakupki.gov.ru/notice?regNumber=12345678']}
    assert jobs.validate_plan(direct) == direct


def test_real_executor_lock_survives_observer_and_detects_process_death(tmp_path):
    path, ready = tmp_path / 'jobs.sqlite3', tmp_path / 'ready'
    row, _ = jobs.enqueue(path, PLAN, 'Разбор')
    root = str(Path(__file__).resolve().parents[1])
    code = '''import sys, time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from autobot import main_jobs as jobs
with jobs.execution_lock(sys.argv[2]):
    assert jobs.claim(sys.argv[2],sys.argv[3])
    Path(sys.argv[4]).write_text('ready')
    time.sleep(60)
'''
    child = subprocess.Popen([sys.executable, '-c', code, root, str(path), row['run_id'], str(ready)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.03)
        assert ready.exists(), child.poll()
        # A brand-new observer must not turn the live child into an interrupted job.
        probe = "import sys;sys.path.insert(0,sys.argv[1]);from autobot import main_jobs as j;print(j.recover_interrupted(sys.argv[2]))"
        args = [sys.executable, '-c', probe, root, str(path)]
        assert subprocess.check_output(args, text=True).strip() == 'False'
        child.kill()
        child.wait(timeout=10)
        assert subprocess.check_output(args, text=True).strip() == 'True'
        assert jobs.latest(path)['status'] == 'interrupted'
        assert subprocess.check_output(args, text=True).strip() == 'False'
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def test_runtime_consumes_saved_plan_once_and_preserves_cli_exit_code(tmp_path):
    path = tmp_path / 'jobs.sqlite3'
    row, _ = jobs.enqueue(path, PLAN, 'Разбор')
    calls = []
    def execute(argv):
        calls.append(argv)
        print('Проверяем исходный файл')
        return 2
    assert runtime.run_job(path, row['run_id'], execute=execute) == 2
    saved = jobs.get(path, row['run_id'])
    assert saved['status'] == 'failed' and saved['exit_code'] == 2
    assert 'Проверяем исходный файл' in saved['logs']
    assert runtime.run_job(path, row['run_id'], execute=execute) == 0
    assert calls == [PLAN['argv']]
    assert jobs.get(path, row['run_id']) == saved


def test_runtime_batch_uses_admitted_order_and_reports_each_failure(tmp_path):
    path = tmp_path / 'jobs.sqlite3'
    row, _ = jobs.enqueue(path, {'kind': 'batch', 'tender_ids': ['87654321', '12345678']}, 'Все отчёты')
    calls = []
    def execute(argv):
        calls.append(argv[-1])
        if argv[-1] == '87654321':
            raise RuntimeError('Повреждённый исходный файл')
        return 0
    assert runtime.run_job(path, row['run_id'], execute=execute) == 1
    saved = jobs.get(path, row['run_id'])
    assert calls == ['87654321', '12345678']
    assert saved['status'] == 'failed' and saved['done'] == saved['total'] == 2
    assert any('Повреждённый исходный файл' in line for line in saved['logs'])


def test_runtime_keeps_bounded_partial_output_and_restores_python_streams(tmp_path):
    path = tmp_path / 'jobs.sqlite3'
    row, _ = jobs.enqueue(path, PLAN, 'Разбор')
    before = sys.stdout, sys.stderr
    def execute(argv):
        sys.stdout.write('x' * 1000000)
        sys.stdout.write('\n')
        print('Результат сохранён', end='')
        return 0
    assert runtime.run_job(path, row['run_id'], execute=execute) == 0
    saved = jobs.get(path, row['run_id'])
    assert saved['status'] == 'completed'
    assert max(map(len, saved['logs'])) <= jobs.MAX_LOG_LINE
    assert any('Результат сохранён' in line for line in saved['logs'])
    assert (sys.stdout, sys.stderr) == before
