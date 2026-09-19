from pathlib import Path

import pytest

from autobot import agent_market_queue as queue
from autobot import web_ui


@pytest.fixture
def whole_estimate(monkeypatch, tmp_path):
    monkeypatch.setattr(queue, 'DEFAULT_DB_PATH', tmp_path / 'queue.sqlite3')
    monkeypatch.setattr(web_ui, 'REPORTS_DIR', tmp_path)
    (tmp_path / 'ОТЧЕТ_ПО_СМЕТАМ_12345678.xlsx').touch()
    positions = [{'position_key': f'row-{n}', 'name': f'Кабель ВВГнг-LS 3х2,5, позиция {n}',
                  'unit': 'м', 'quantity': 5, 'type_slug': 'material', 'can_auto_price': True,
                  'verified_count': 0, 'queries': ['Кабель ВВГнг-LS 3х2,5 Ярославль цена за м']}
                 for n in range(120)]
    positions += [{'position_key': 'clarify', 'name': 'Материал без единицы', 'unit': '',
                   'can_auto_price': False, 'warning': 'Нет единицы измерения'},
                  {'position_key': 'priced', 'name': 'Уже проверено', 'verified_count': 1}]
    monkeypatch.setattr(web_ui, 'load_tender_metadata', lambda: {'12345678': {}})
    monkeypatch.setattr(web_ui, '_tenders_items', lambda: ([], {}))
    monkeypatch.setattr(web_ui, 'build_tender_detail', lambda *args: {'region': 'Ярославль', 'positions': positions})
    return web_ui.app.test_client(), positions


def test_whole_estimate_survives_next_request_and_process_reopen(whole_estimate):
    client, positions = whole_estimate
    response = client.post('/api/tenders/12345678/agent-market/jobs',
                           json={'mode': 'web', 'scope': 'all_without_verified'})
    assert response.status_code == 200
    result = response.get_json()
    assert result['created'] == 120
    assert result['estimate_plan'] == {'scope': 'all_without_verified', 'total_positions': 122,
        'already_verified': 1, 'needs_details': 1, 'searchable_positions': 120}
    assert result['skipped_ineligible'][0]['position_key'] == 'clarify'
    assert queue.job_progress('12345678', mode='web')['total'] == 120
    # A fresh connection reads the whole run and refuses duplicate admission.
    repeated = client.post('/api/tenders/12345678/agent-market/jobs',
                           json={'mode': 'web', 'scope': 'all_without_verified'}).get_json()
    assert repeated['created'] == 0 and repeated['skipped_active'] == 120
    for number in range(120):
        job = queue.claim_job('test-worker', mode='web')
        assert job is not None
        assert queue.complete_job(job['id'], 'test-worker', {'offers': []}, lease_token=job['lease_token'])
    assert queue.claim_job('test-worker', mode='web') is None
    final = client.get('/api/tenders/12345678/agent-market/jobs').get_json()
    assert final['progress']['total'] == final['progress']['processed'] == 120
    assert final['latest_run']['estimate_plan']['needs_details'] == 1
    assert final['latest_run']['total'] == 120


def test_explicit_selection_does_not_silently_stop_at_fifty(whole_estimate):
    client, positions = whole_estimate
    response = client.post('/api/tenders/12345678/agent-market/jobs', json={
        'mode': 'web', 'scope': 'selected', 'position_keys': [p['position_key'] for p in positions[:90]]})
    assert response.get_json()['created'] == 90


def test_legacy_small_batch_contract_and_avito_limit_are_retained(whole_estimate):
    client, _ = whole_estimate
    result = client.post('/api/tenders/12345678/agent-market/jobs', json={'mode': 'web'}).get_json()
    assert result['created'] == 20
    result = client.post('/api/tenders/12345678/agent-market/jobs',
                         json={'mode': 'avito', 'scope': 'all_without_verified'}).get_json()
    assert result['created'] == 5


def test_progress_counts_positions_beyond_history_display_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(queue, 'DEFAULT_DB_PATH', tmp_path / 'queue.sqlite3')
    positions = [{'position_key': f'row-{n}', 'name': f'Позиция {n}', 'batch_id': 'whole',
                  'estimate_plan': {'scope': 'all_without_verified', 'total_positions': 1205}}
                 for n in range(1205)]
    queue.enqueue_jobs('12345678', positions)
    assert queue.job_summary('12345678')['total'] == 1205
    assert queue.job_progress('12345678')['total'] == 1205
    response = web_ui.app.test_client().get('/api/tenders/12345678/agent-market/jobs').get_json()
    assert response['latest_run']['total'] == response['progress']['total'] == 1205
    assert len(response['jobs']) == 250
    assert response['latest_run']['positions_truncated']


def test_unsearchable_whole_estimate_returns_explanation(whole_estimate):
    client, positions = whole_estimate
    positions[:] = [positions[-2]]
    response = client.post('/api/tenders/12345678/agent-market/jobs',
                           json={'mode': 'web', 'scope': 'all_without_verified'})
    assert response.status_code == 200
    assert response.get_json()['created'] == 0
    assert response.get_json()['estimate_plan']['needs_details'] == 1


def test_stop_is_scoped_idempotent_and_does_not_lose_accepted_result(whole_estimate):
    client, _ = whole_estimate
    url = '/api/tenders/12345678/agent-market/jobs'
    client.post(url, json={'scope': 'all_without_verified'})
    accepted = queue.claim_job('saving', mode='web')
    queue.accept_job_result(accepted['id'], 'saving', {'offers': []}, {'rows': []},
                            lease_token=accepted['lease_token'])
    searching = queue.claim_job('searching', mode='web')
    queue.enqueue_jobs('87654321', [{'position_key': 'other', 'name': 'Другой тендер'}])
    queue.enqueue_jobs('12345678', [{'position_key': 'avito', 'name': 'Авито', 'job_mode': 'avito'}])
    result = client.post(url, json={'action': 'cancel_pending', 'mode': 'web'}).get_json()
    assert result['canceled'] == 119 and result['progress']['running']
    assert queue.job_summary('12345678', mode='web')['leased'] == 1
    assert not queue.complete_job(searching['id'], 'searching', {'offers': []}, lease_token=searching['lease_token'])
    assert accepted['id'] in queue.pending_deliveries()
    assert queue.apply_accepted_result(accepted['id'], lambda *args: {'saved': 1})['status'] == 'completed'
    assert client.post(url, json={'action': 'cancel_pending'}).get_json()['canceled'] == 0
    assert queue.job_progress('87654321')['queued'] == 1
    assert queue.job_progress('12345678', mode='avito')['queued'] == 1
    assert not queue.job_progress('12345678', mode='web')['running']


def test_whole_estimate_prioritizes_expensive_positions_within_type(whole_estimate):
    client, positions = whole_estimate
    positions[51]['estimate_total'] = 900000
    positions[100]['estimate_total'] = 800000
    positions[99]['estimate_total'] = float('inf')
    client.post('/api/tenders/12345678/agent-market/jobs', json={'scope': 'all_without_verified'})
    assert queue.claim_job('worker')['position_key'] == 'row-51'
    assert queue.claim_job('worker2')['position_key'] == 'row-100'
