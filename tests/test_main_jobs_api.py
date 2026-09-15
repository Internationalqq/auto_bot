import copy
import uuid

import pytest

from autobot import main_jobs as jobs
from autobot import web_ui


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(web_ui, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web_ui, 'REPORTS_DIR', tmp_path / 'reports')
    monkeypatch.setattr(web_ui, '_merge_site_busy', lambda: False)
    monkeypatch.setattr(web_ui, 'parse_state', dict(copy.deepcopy(web_ui.parse_state), running=False, run_id=None))
    monkeypatch.setattr(web_ui, 'load_tender_metadata', lambda: {
        '12345678': {'url': 'https://zakupki.gov.ru/notice?regNumber=12345678'}, '87654321': {}})
    launches = []
    class Thread:
        def __init__(self, **kwargs): self.kwargs = kwargs
        def start(self): launches.append(self.kwargs)
    monkeypatch.setattr(web_ui.threading, 'Thread', Thread)
    return web_ui.app.test_client(), tmp_path / 'main_jobs.sqlite3', launches


def test_admission_survives_web_memory_reset_and_repeated_http_request(api, monkeypatch):
    client, path, launches = api
    operation = str(uuid.uuid4())
    route = '/api/tenders/12345678/refresh-documents'
    response = client.post(route, json={'operation_id': operation})
    assert response.status_code == 202 and not response.json['duplicate']
    run_id = response.json['run_id']
    assert run_id == uuid.UUID(operation).hex and jobs.latest(path)['run_id'] == run_id
    assert len(launches) == 1
    # A fresh web process has none of the previous in-memory reservation.
    monkeypatch.setattr(web_ui, 'parse_state', dict(web_ui.parse_state, running=False, run_id=None, log_lines=[]))
    saved = client.get('/api/parse-status').json
    assert saved['running'] and saved['job_status'] == 'queued' and saved['run_id'] == run_id
    duplicate = client.post(route, json={'operation_id': operation})
    assert duplicate.status_code == 202 and duplicate.json['duplicate'] and len(launches) == 1
    assert client.post('/api/reports/rebuild', json={'tender_id': '87654321'}).status_code == 409
    assert client.post('/api/reports/rebuild', json={'tender_id': '12345678', 'operation_id': operation}).status_code == 409


def test_live_executor_status_and_finished_job_remain_addressable(api):
    client, path, launches = api
    operation = uuid.uuid4().hex
    first = client.post('/api/reports/rebuild', json={'tender_id': '12345678', 'operation_id': operation})
    assert first.status_code == 200
    with jobs.execution_lock(path):
        jobs.claim(path, operation)
        status = client.get('/api/parse-status?run_id=' + operation).json
        assert status['job_status'] == 'running'
        jobs.finish(path, operation, 0)
    second = client.post('/api/reports/rebuild', json={'tender_id': '87654321'})
    assert second.status_code == 200 and len(launches) == 2
    # Retrying a completed request cannot replace a different active job.
    retry = client.post('/api/reports/rebuild', json={'tender_id': '12345678', 'operation_id': operation})
    assert retry.status_code == 200 and retry.json['duplicate'] and len(launches) == 2
    assert client.get('/api/parse-status').json['run_id'] == second.json['run_id']
    old = client.get('/api/parse-status?run_id=' + operation).json
    assert old['job_status'] == 'completed' and not old['running'] and old['exit_code'] == 0
    assert client.get('/api/parse-status?run_id=' + 'f'*32).status_code == 404
    assert client.get('/api/parse-status?run_id=../private').status_code == 400


def test_failed_launch_is_saved_and_allows_a_new_attempt(api, monkeypatch):
    client, path, launches = api
    class BrokenThread:
        def __init__(self, **kwargs): pass
        def start(self): raise RuntimeError('no thread')
    monkeypatch.setattr(web_ui.threading, 'Thread', BrokenThread)
    response = client.post('/api/reports/rebuild', json={'tender_id': '12345678'})
    assert response.status_code == 500
    saved = jobs.latest(path)
    assert saved['status'] == 'failed' and saved['exit_code'] == -1
    assert not client.get('/api/parse-status').json['running']


def test_invalid_request_or_failed_store_never_launches_a_process(api, monkeypatch):
    client, path, launches = api
    assert client.post('/api/reports/rebuild', json={'tender_id': '12345678', 'operation_id': ['bad']}).status_code == 400
    assert client.post('/api/tenders/12345678/refresh-documents', headers={'Origin': 'https://other.example'}).status_code == 403
    def failed(*args, **kwargs): raise OSError('disk unavailable')
    monkeypatch.setattr(jobs, 'enqueue', failed)
    assert client.post('/api/reports/rebuild', json={'tender_id': '12345678'}).status_code == 503
    assert not launches and not path.exists()


def test_interrupted_executor_status_requires_explicit_retry(api):
    client, path, launches = api
    response = client.post('/api/reports/rebuild', json={'tender_id': '12345678'})
    jobs.claim(path, response.json['run_id'])
    status = client.get('/api/parse-status').json
    assert status['job_status'] == 'interrupted' and status['exit_code'] == -1
    assert not status['running'] and len(launches) == 1
    assert client.post('/api/reports/rebuild', json={'tender_id': '12345678'}).status_code == 200
    assert len(launches) == 2


@pytest.mark.parametrize('endpoint', [
    'generate-merge-site-all', 'generate-merge-site-missing', 'generate-merge-site-selected',
    'generate-merge-site-one', 'generate-merge-site-one-rerun-market', 'generate-merge-site-one-sample-market',
    'generate-avito-safe-sample', 'generate-merge-site-by-link',
])
def test_neighbour_workflows_refuse_saved_job_after_web_restart(api, monkeypatch, endpoint):
    client, path, launches = api
    jobs.enqueue(path, {'kind': 'main', 'argv': ['--from-downloaded-tender-id', '12345678']}, 'Разбор')
    assert not web_ui.parse_state['running']
    assert client.post('/api/' + endpoint, json={}).status_code == 409
    assert not launches


def test_delete_cannot_remove_documents_of_surviving_job_and_next_request_recovers(api, monkeypatch):
    client, path, launches = api
    row, _ = jobs.enqueue(path, {'kind': 'main', 'argv': ['--from-downloaded-tender-id', '12345678']}, 'Разбор')
    deleted = []
    monkeypatch.setattr(web_ui, 'delete_tender_data', lambda tid: deleted.append(tid) or {})
    route = '/api/tenders/12345678/delete'
    with jobs.execution_lock(path):
        jobs.claim(path, row['run_id'])
        assert client.post(route, json={'confirm_tender_id':'12345678'}).status_code == 409
        assert not deleted
        jobs.finish(path, row['run_id'], 0)
    assert client.post(route, json={'confirm_tender_id':'12345678'}).status_code == 200
    assert deleted == ['12345678'] and not launches


def test_unavailable_job_store_does_not_allow_neighbour_mutation(api, monkeypatch):
    client, path, launches = api
    def unavailable(*args): raise OSError('cannot read store')
    monkeypatch.setattr(jobs, 'latest', unavailable)
    assert client.post('/api/generate-merge-site-all', json={}).status_code == 409
    assert client.post('/api/tenders/12345678/delete', json={'confirm_tender_id':'12345678'}).status_code == 409
    assert not launches
