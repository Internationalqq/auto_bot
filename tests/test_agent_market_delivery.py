from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

from autobot import agent_market_queue as queue
from autobot import agent_market_delivery as delivery
from autobot import real_market_scraper as market
from autobot.market_contract import position_identity, BUNDLE_COLUMN


@pytest.fixture
def isolated_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, 'DEFAULT_DB_PATH', tmp_path / 'jobs.sqlite3')
    clock = [1000.0]
    monkeypatch.setattr(queue, '_now', lambda: clock[0])
    queue.enqueue_jobs('12345678', [{'position_key':'p1', 'name':'Щебень 20-40', 'max_attempts':2}])
    return clock


def test_claim_token_is_private_and_old_attempt_cannot_change_reissued_job(isolated_queue):
    first = queue.claim_job('same-worker', lease_seconds=60)
    assert first['lease_token']
    assert first['lease_token'] not in json.dumps(queue.list_jobs('12345678'))
    assert '_lease_token' not in json.dumps(queue.get_job(first['id']))
    isolated_queue[0] += 61
    assert not queue.heartbeat_job(first['id'], 'same-worker', lease_token=first['lease_token'])
    assert queue.complete_job(first['id'], 'same-worker', {}, lease_token=first['lease_token']) is None
    assert not queue.fail_job(first['id'], 'same-worker', 'timeout', lease_token=first['lease_token'])
    second = queue.claim_job('same-worker')
    assert second['id'] == first['id'] and second['lease_token'] != first['lease_token']
    assert not queue.heartbeat_job(first['id'], 'same-worker', lease_token=first['lease_token'])
    assert not queue.fail_job(second['id'], 'same-worker', 'timeout')
    assert queue.complete_job(second['id'], 'same-worker', {}, lease_token=first['lease_token']) is None
    assert queue.complete_job(second['id'], 'same-worker', {}, lease_token=second['lease_token'])['status'] == 'completed'


def test_expiration_during_page_verification_never_publishes(isolated_queue, monkeypatch):
    job = queue.claim_job('worker', lease_seconds=60)
    calls = []
    def prepare(*args):
        isolated_queue[0] += 61
        return {'verified':True}
    monkeypatch.setattr(delivery, 'prepare_agent_market_result', prepare)
    monkeypatch.setattr(delivery, 'publish_agent_market_result', lambda *args: calls.append(args))
    with pytest.raises(delivery.DeliveryConflict):
        delivery.complete_agent_result(job['id'], 'worker', {'offers':[]}, lease_token=job['lease_token'])
    assert calls == []
    assert queue.get_job(job['id'])['result'] is None


def test_identical_completion_does_not_repeat_verification_or_publication(isolated_queue, monkeypatch):
    job = queue.claim_job('worker')
    calls = []
    monkeypatch.setattr(delivery, 'prepare_agent_market_result', lambda *args: calls.append('prepare') or {'ok':True})
    monkeypatch.setattr(delivery, 'publish_agent_market_result', lambda *args: calls.append('publish') or {'imported':1})
    for _ in range(2):
        completed = delivery.complete_agent_result(job['id'], 'worker', {'offers':[]}, lease_token=job['lease_token'])
        assert completed['status'] == 'completed'
    assert calls == ['prepare', 'publish']
    with pytest.raises(delivery.DeliveryConflict):
        delivery.complete_agent_result(job['id'], 'worker', {'offers':[], 'notes':'different'}, lease_token=job['lease_token'])


def test_cancel_after_acceptance_prevents_report_write(isolated_queue):
    job = queue.claim_job('worker')
    accepted = queue.accept_job_result(job['id'], 'worker', {'offers':[]}, {'ok':True}, lease_token=job['lease_token'])
    assert accepted['delivery_pending']
    assert '_delivery' not in json.dumps(accepted)
    assert queue.enqueue_jobs('12345678', [{'position_key':'p1','name':'Щебень 20-40'}])['created'] == []
    assert queue.cancel_job(job['id'], '12345678')
    calls = []
    assert queue.apply_accepted_result(job['id'], lambda *args: calls.append(args)) is None
    assert calls == [] and queue.pending_deliveries() == []


def test_temporary_publication_failure_is_accepted_and_recovers(isolated_queue, monkeypatch):
    job = queue.claim_job('worker')
    prepares = []
    monkeypatch.setattr(delivery, 'prepare_agent_market_result', lambda *args: prepares.append(1) or {'ok':True})
    def fail(*args):
        raise OSError('temporarily locked')
    monkeypatch.setattr(delivery, 'publish_agent_market_result', fail)
    pending = delivery.complete_agent_result(job['id'], 'worker', {'offers':[]}, lease_token=job['lease_token'])
    assert pending['delivery_pending']
    assert not queue.fail_job(job['id'], 'worker', 'late worker timeout')
    monkeypatch.setattr(delivery, 'publish_agent_market_result', lambda *args: {'imported':1})
    assert delivery.recover_accepted_results()['completed'] == 1
    assert prepares == [1]
    assert queue.get_job(job['id'])['status'] == 'completed'


@pytest.fixture
def prepared_market(tmp_path, monkeypatch):
    estimate_path = tmp_path / 'estimate.xlsx'
    market_path = tmp_path / 'market.xlsx'
    row = {market.COL_NAME:'Щебень гранитный 20-40', 'Ед. изм.':'м3', market.COL_QTY:2,
           market.COL_UNIT_PRICE:3000, market.COL_SUM:6000, 'basis_code':'ФСБЦ-02', '№ п/п':1}
    pd.DataFrame([row]).to_excel(estimate_path, index=False)
    monkeypatch.setattr(market, 'estimate_path_for_tender', lambda tid: estimate_path)
    monkeypatch.setattr(market, 'output_path_for_tender', lambda tid: market_path)
    monkeypatch.setattr(market, 'load_tender_metadata', lambda: {'12345678':{'region':'Ярославль'}})
    monkeypatch.setattr(market, '_store_verified_offers_in_index', lambda *a, **kw: 0)
    def verify(src, offers, plan, **kwargs):
        for offer in offers:
            offer.verification = 'verified'
            offer.observed_at = datetime.now(timezone.utc).isoformat()
            offer.matched_unit = 'м3'
            offer.search_region = 'Ярославль'
            offer.region_evidence = 'Доставка по Ярославлю'
        return offers
    monkeypatch.setattr(market, '_verify_offers', verify)
    payload = {'name':row[market.COL_NAME], 'unit':'м3', 'position_key':position_identity(row)}
    result = {'offers':[{'title':row[market.COL_NAME], 'price':2500, 'unit':'м3',
                        'url':'https://supplier.example/stone', 'evidence':'Щебень гранитный 20-40 — 2500 руб/м3'}]}
    prepared = market.prepare_agent_market_result('12345678', payload, result)
    assert not market_path.exists()
    return estimate_path, market_path, payload, result, prepared


@pytest.mark.parametrize('write_before_crash', [False, True])
def test_new_process_recovers_before_or_after_file_publication(isolated_queue, prepared_market, write_before_crash):
    estimate_path, market_path, payload, result, prepared = prepared_market
    # Use the exact estimate position as the queued input.
    queue.patch_queued_job_payloads('12345678', payload)
    job = queue.claim_job('worker')
    assert queue.accept_job_result(job['id'], 'worker', result, prepared, lease_token=job['lease_token'])
    code = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[4])
from autobot import agent_market_queue as q, real_market_scraper as m
from autobot.agent_market_delivery import recover_accepted_results
q.DEFAULT_DB_PATH=Path(sys.argv[1])
m.estimate_path_for_tender=lambda tid:Path(sys.argv[2])
m.output_path_for_tender=lambda tid:Path(sys.argv[3])
m.load_tender_metadata=lambda:{'12345678':{'region':'Ярославль'}}
m._store_verified_offers_in_index=lambda *a,**kw:0
assert recover_accepted_results()['completed']==1
assert q.pending_deliveries()==[]
'''
    if write_before_crash:
        crash_code = code[:code.index("assert recover_accepted_results()")] + '''
import os
def publish_then_crash(*args):
    m.publish_agent_market_result(*args)
    os._exit(23)
q.apply_accepted_result(q.pending_deliveries()[0], publish_then_crash)
'''
        crashed = subprocess.run([sys.executable, '-X', 'utf8', '-c', crash_code, str(queue.DEFAULT_DB_PATH), str(estimate_path), str(market_path), str(Path(market.__file__).resolve().parent.parent)],
                                 capture_output=True, text=True, encoding='utf-8', timeout=30, env=os.environ.copy())
        assert crashed.returncode == 23, crashed.stdout + crashed.stderr
        assert market_path.exists() and queue.get_job(job['id'])['delivery_pending']
    process = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, str(queue.DEFAULT_DB_PATH), str(estimate_path), str(market_path), str(Path(market.__file__).resolve().parent.parent)],
                             capture_output=True, text=True, encoding='utf-8', timeout=30, env=os.environ.copy())
    assert process.returncode == 0, process.stdout + process.stderr
    frame = pd.read_excel(market_path)
    assert len(frame) == 1
    assert len(json.loads(frame.iloc[0][BUNDLE_COLUMN])) == 1
    assert queue.get_job(job['id'])['status'] == 'completed'


def test_changed_estimate_invalidates_accepted_package(isolated_queue, prepared_market):
    estimate_path, market_path, payload, result, prepared = prepared_market
    queue.patch_queued_job_payloads('12345678', payload)
    job = queue.claim_job('worker')
    queue.accept_job_result(job['id'], 'worker', result, prepared, lease_token=job['lease_token'])
    frame = pd.read_excel(estimate_path)
    frame.loc[0, market.COL_QTY] = 3
    frame.to_excel(estimate_path, index=False)
    with pytest.raises(ValueError, match='изменилась'):
        queue.apply_accepted_result(job['id'], market.publish_agent_market_result)
    assert not market_path.exists()
    assert queue.get_job(job['id'])['status'] == 'failed'


def test_empty_completed_search_is_published_and_repeat_keeps_existing_quote(prepared_market):
    estimate_path, market_path, payload, result, prepared = prepared_market
    original = estimate_path.read_bytes()
    empty = dict(prepared, offers=[], notes='Лимит времени поиска исчерпан')
    assert market.publish_agent_market_result('12345678', payload, empty)['imported'] == 0
    frame = pd.read_excel(market_path)
    from autobot.market_contract import merge_market_frames
    merged = merge_market_frames(pd.read_excel(estimate_path), frame)
    assert len(frame) == 1 and merged.iloc[0]['Рынок обработано'] == 'Да'
    assert 'времени' in frame.iloc[0]['Ошибка / статус']
    market.publish_agent_market_result('12345678', payload, prepared)
    market.publish_agent_market_result('12345678', payload, empty)
    frame = pd.read_excel(market_path)
    assert len(frame) == 1 and len(json.loads(frame.iloc[0][BUNDLE_COLUMN])) == 1
    assert estimate_path.read_bytes() == original


def test_operating_system_releases_file_lock_when_process_exits(tmp_path):
    from autobot.atomic_output import output_lock
    destination = tmp_path / 'result.xlsx'
    code = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from autobot.atomic_output import output_lock
with output_lock(Path(sys.argv[2])):
    os._exit(23)
'''
    process = subprocess.run([sys.executable, '-c', code, str(Path(market.__file__).resolve().parent.parent), str(destination)],
                             capture_output=True, timeout=15)
    assert process.returncode == 23, process.stderr
    with output_lock(destination, timeout=1):
        destination.write_text('recovered', encoding='utf-8')
    assert destination.read_text(encoding='utf-8') == 'recovered'


def test_api_acknowledges_durable_pending_result_without_exposing_internal_package(isolated_queue, monkeypatch):
    from autobot.web_ui import app
    monkeypatch.setenv('MARKET_AGENT_TOKEN', 'delivery-test-token')
    headers = {'Authorization':'Bearer delivery-test-token'}
    client = app.test_client()
    job = queue.claim_job('worker')
    monkeypatch.setattr(delivery, 'prepare_agent_market_result', lambda *args: {'private_package':'only in durable storage'})
    def unavailable(*args):
        raise OSError('temporary output lock')
    monkeypatch.setattr(delivery, 'publish_agent_market_result', unavailable)
    response = client.post(f"/api/agent-market/v1/jobs/{job['id']}/complete", headers=headers,
                           json={'worker_id':'worker', 'lease_token':job['lease_token'], 'result':{'offers':[]}})
    assert response.status_code == 202 and response.get_json()['delivery_pending']
    public = client.get('/api/tenders/12345678/agent-market/jobs').get_data(as_text=True)
    assert 'private_package' not in public and job['lease_token'] not in public
    assert '_delivery' not in public and '_lease_token' not in public


def test_api_rejects_expired_completion_before_verifying_pages(isolated_queue, monkeypatch):
    from autobot.web_ui import app
    monkeypatch.setenv('MARKET_AGENT_TOKEN', 'delivery-test-token')
    client = app.test_client()
    job = queue.claim_job('worker', lease_seconds=60)
    isolated_queue[0] += 61
    calls = []
    monkeypatch.setattr(delivery, 'prepare_agent_market_result', lambda *args: calls.append(args))
    response = client.post(f"/api/agent-market/v1/jobs/{job['id']}/complete", headers={'Authorization':'Bearer delivery-test-token'},
                           json={'worker_id':'worker','lease_token':job['lease_token'],'result':{'offers':[]}})
    assert response.status_code == 409 and calls == []
