from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import threading
from dataclasses import replace

import pandas as pd
import pytest

from autobot import agent_market_queue as queue
from autobot import market_web_worker as worker
from autobot import real_market_scraper as market
from autobot.market_contract import position_identity, confirmed_prices, BUNDLE_COLUMN


def quote(*, price=2500, ago=0, verification='verified', url='https://supplier.example/stone'):
    return market.MarketOffer(
        'Интернет', 'Щебень гранитный 20-40', price, url, verification=verification,
        matched_unit='м3', search_region='Ярославль', region_evidence='Доставка по Ярославлю',
        evidence=f'Щебень гранитный 20-40 — {price} руб/м3', page_checked=True, extractor='price-block',
        observed_at=(datetime.now(timezone.utc) - timedelta(seconds=ago)).isoformat(),
    )


@pytest.fixture
def job_context(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, 'DEFAULT_DB_PATH', tmp_path / 'queue.sqlite3')
    estimate, output = tmp_path / 'estimate.xlsx', tmp_path / 'market.xlsx'
    row = {market.COL_NAME: 'Щебень гранитный 20-40', 'Ед. изм.': 'м3',
           market.COL_QTY: 2, market.COL_UNIT_PRICE: 3000, market.COL_SUM: 6000, '№ п/п': 1}
    pd.DataFrame([row]).to_excel(estimate, index=False)
    monkeypatch.setattr(market, 'estimate_path_for_tender', lambda tid: estimate)
    monkeypatch.setattr(market, 'output_path_for_tender', lambda tid: output)
    monkeypatch.setattr(market, 'output_path_for_estimate', lambda path: output)
    monkeypatch.setattr(market, 'load_tender_metadata', lambda: {'12345678': {'region': 'Ярославль'}})
    monkeypatch.setattr(market, '_store_verified_offers_in_index', lambda *a, **kw: 0)
    monkeypatch.setattr(market, '_MARKET_SEARCH_LOG_PATH', tmp_path / 'search.jsonl')
    monkeypatch.setattr(market, 'REPORTS_DIR', tmp_path)
    payload = {'position_key': position_identity(row), 'name': row[market.COL_NAME],
               'unit': 'м3', 'max_attempts': 2, 'region': 'Ярославль'}
    created = queue.enqueue_jobs('12345678', [payload])['created'][0]
    return row, payload, created['id'], estimate, output


def test_server_claim_filters_avito_and_does_not_expire_its_lease(job_context, monkeypatch):
    row, payload, job_id, _, _ = job_context
    queue.enqueue_jobs('12345678', [dict(payload, job_mode='avito')], priority=1)
    avito = queue.claim_job('external', mode='avito')
    clock = [queue._now() + 400]
    monkeypatch.setattr(queue, '_now', lambda: clock[0])
    web = queue.claim_job('server', mode='web')
    assert web['id'] == job_id
    assert queue.get_job(avito['id'])['status'] == 'leased'
    # Unfiltered legacy clients still recover and claim either kind.
    again = queue.claim_job('legacy')
    assert again['id'] == avito['id'] and again['attempts'] == 2


@pytest.mark.parametrize('mode', ['other', [], {}])
def test_invalid_claim_modes_are_validation_errors(job_context, mode):
    with pytest.raises(ValueError, match='mode'):
        queue.claim_job('worker', mode=mode)


def test_server_job_searches_web_and_publishes_exact_input_without_external_worker(job_context, monkeypatch):
    row, payload, job_id, _, output = job_context
    requests = []
    def search(src, plan, **kwargs):
        requests.append((src.to_dict(), plan.queries, kwargs))
        return [quote()], ''
    monkeypatch.setattr(market, '_research_row_market', search)
    result = worker.run_once('server-test')
    assert result['id'] == job_id and result['status'] == 'completed'
    assert result['result']['executor'] == 'server'
    assert result['result']['import']['verified'] == 1
    assert confirmed_prices(pd.read_excel(output).iloc[0]) == [2500]
    assert len(requests) == 1 and requests[0][2]['sources'] == ['web']
    assert isinstance(requests[0][2]['browser_fetcher'], market.WebBrowserFetcher)
    assert requests[0][2]['browser_fetcher']._playwright is None
    assert requests[0][0][market.COL_QTY] == 2
    assert requests[0][0]['Регион поиска'] == 'Ярославль'
    assert queue.pending_deliveries() == []
    assert worker.run_once('server-test') is None


def test_payload_region_is_used_by_query_plan_when_metadata_has_none(job_context, monkeypatch):
    row, payload, *_ = job_context
    monkeypatch.setattr(market, 'load_tender_metadata', lambda: {})
    context = market._agent_import_context('12345678', payload)
    assert context[3]['Регион поиска'] == 'Ярославль'
    assert any('Ярославль' in query for query in context[6].queries)


def test_server_queue_uses_catalogue_before_web_discovery(job_context,monkeypatch):
    _,_,job_id,_,output=job_context
    calls=[]
    monkeypatch.setattr(market,'_offers_from_local_index',lambda row,**kw:calls.append(kw) or [quote()])
    def search(row,plan,*,initial_offers,**kwargs):
        assert len(initial_offers)==1 and initial_offers[0].price==2500
        return initial_offers,''
    monkeypatch.setattr(market,'_research_row_market',search)
    assert worker.run_once('catalogue-test')['status']=='completed'
    assert calls==[{'max_results':3,'region':'Ярославль'}]
    assert confirmed_prices(pd.read_excel(output).iloc[0])==[2500]


@pytest.mark.parametrize('change', ['cancel', 'expire', 'estimate', 'region'])
def test_changed_attempt_or_input_never_publishes(job_context, monkeypatch, change):
    row, payload, job_id, estimate, output = job_context
    def search(*args, **kwargs):
        if change == 'cancel':
            queue.cancel_job(job_id, '12345678')
        elif change == 'expire':
            now = queue._now() + 400
            monkeypatch.setattr(queue, '_now', lambda: now)
        elif change == 'estimate':
            pd.DataFrame([dict(row, **{market.COL_QTY: 3})]).to_excel(estimate, index=False)
        else:
            monkeypatch.setattr(market, 'load_tender_metadata', lambda: {'12345678': {'region': 'Миасс'}})
        return [quote()], ''
    monkeypatch.setattr(market, '_research_row_market', search)
    worker.run_once('server-test')
    assert not output.exists()
    assert queue.get_job(job_id)['status'] != 'completed'


def test_accepted_file_failure_recovers_without_another_search(job_context, monkeypatch):
    _, _, job_id, _, output = job_context
    calls = []
    monkeypatch.setattr(market, '_research_row_market', lambda *a, **kw: (calls.append(1) or [quote()], ''))
    def unavailable(*args):
        raise OSError('temporarily locked')
    monkeypatch.setattr(worker, 'publish_agent_market_result', unavailable)
    assert worker.run_once('server-test')['delivery_pending']
    assert not output.exists()
    assert worker.run_once('server-test') is None
    assert queue.apply_accepted_result(job_id, market.publish_agent_market_result)['status'] == 'completed'
    assert len(calls) == 1 and confirmed_prices(pd.read_excel(output).iloc[0]) == [2500]


def test_heartbeat_notices_cancellation_while_search_is_running(job_context, monkeypatch):
    _, _, job_id, _, output = job_context
    started, seen_cancel = threading.Event(), threading.Event()
    monkeypatch.setattr(worker, 'HEARTBEAT_SECONDS', 0.01)
    def search(*args, cancelled, **kwargs):
        started.set()
        for _ in range(300):
            if cancelled():
                seen_cancel.set()
                break
            seen_cancel.wait(0.01)
        return [quote()], ''
    monkeypatch.setattr(market, '_research_row_market', search)
    thread = threading.Thread(target=worker.run_once, args=('server-test',))
    thread.start()
    try:
        assert started.wait(3)
        assert queue.cancel_job(job_id, '12345678')
        assert seen_cancel.wait(3)
    finally:
        thread.join(4)
    assert not thread.is_alive() and not output.exists()


def test_cancel_stops_before_next_page_and_avito_results_are_not_opened(job_context, monkeypatch):
    row, *_ = job_context
    stop = threading.Event()
    opened = []
    monkeypatch.setattr(market, 'search_market', lambda *a, **kw: (
        [quote(url='https://www.avito.ru/city/ad_1234567'), quote(), quote(url='https://other.example/stone')], ''))
    def verify(src, offers, plan, **kw):
        opened.extend(offer.url for offer in offers)
        stop.set()
        return offers
    monkeypatch.setattr(market, '_verify_offers', verify)
    plan = market.build_search_plan(row[market.COL_NAME], 'м3', '', '', 'Ярославль')
    offers, error = market._research_row_market(pd.Series(row), plan, sources=['web'], max_results=3, cancelled=stop.is_set)
    assert opened == ['https://supplier.example/stone']
    assert 'отменён' in error and len(offers) == 1
    assert market._SEARCH_CANCELLED.get() is None
    assert market._SEARCH_DEADLINE.get() is None


def test_two_server_threads_have_only_one_leader(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, 'LEADER_PATH', tmp_path / 'leader')
    entered, entered_twice = threading.Event(), threading.Event()
    stops = [threading.Event(), threading.Event()]
    def leader(stop):
        if entered.is_set():
            entered_twice.set()
        entered.set()
        stop.wait(4)
    monkeypatch.setattr(worker, '_work_as_leader', leader)
    threads = [threading.Thread(target=worker._run, args=(stop,)) for stop in stops]
    for thread in threads:
        thread.start()
    try:
        assert entered.wait(2)
        assert not entered_twice.wait(0.3)
    finally:
        for stop in stops:
            stop.set()
        for thread in threads:
            thread.join(2)
    assert all(not thread.is_alive() for thread in threads)


def test_server_startup_is_idempotent_and_can_be_disabled(monkeypatch):
    entered = threading.Event()
    monkeypatch.setattr(worker, '_thread', None)
    monkeypatch.setattr(worker, '_stop', threading.Event())
    def run(stop):
        entered.set()
        stop.wait(3)
    monkeypatch.setattr(worker, '_run', run)
    monkeypatch.setenv('MARKET_WEB_WORKER', '0')
    assert worker.start_web_worker() is None and not entered.is_set()
    monkeypatch.setenv('MARKET_WEB_WORKER', '1')
    thread = worker.start_web_worker()
    try:
        assert entered.wait(2)
        assert worker.start_web_worker() is thread
    finally:
        worker._stop.set()
        thread.join(2)


def test_late_whole_search_preserves_fresh_same_position_quote(job_context, monkeypatch):
    row, payload, job_id, estimate, output = job_context
    monkeypatch.setattr(market, '_offers_from_local_index', lambda *a, **kw: [])
    monkeypatch.setattr(market, 'append_market_web_event', lambda *a, **kw: None)
    monkeypatch.setattr(market, 'record_parser_run', lambda **kw: {})
    monkeypatch.setenv('MARKET_AVITO_BROWSER', '0')
    monkeypatch.setenv('MARKET_INDEX_BACKFILL', '0')
    def search(src, plan, **kw):
        fresh_row = market._build_output_row(src, offers=[quote()], query='test', err='', plan=plan)
        pd.DataFrame([fresh_row]).to_excel(output, index=False)
        return [quote(price=2200, ago=60)], ''
    monkeypatch.setattr(market, '_research_row_market', search)
    market.run_tender('12345678', max_rows=1, sources=['web'], pause=0)
    assert confirmed_prices(pd.read_excel(output).iloc[0]) == [2500]


@pytest.mark.parametrize('incoming', [[], [quote(price=2400, ago=60)]])
def test_empty_or_older_observation_does_not_erase_saved_price(job_context, incoming):
    row, *_ = job_context
    row['Регион поиска'] = 'Ярославль'
    merged = market._latest_offers_for_row(row, [quote()], incoming)
    assert len(merged) == 1 and merged[0].price == 2500 and merged[0].verification == 'verified'


def test_new_candidate_replaces_old_exact_price_in_same_page(job_context):
    row, *_ = job_context
    row['Регион поиска'] = 'Ярославль'
    fresh = quote(price=2400, verification='candidate')
    fresh.evidence = 'Щебень гранитный 20-40 от 2400 руб/м3'
    merged = market._latest_offers_for_row(row, [quote(ago=60)], [fresh])
    assert len(merged) == 1 and merged[0].verification == 'candidate' and merged[0].price == 2400


def test_failed_page_open_does_not_erase_saved_exact_quote(job_context):
    row, *_ = job_context
    row['Регион поиска'] = 'Ярославль'
    unchecked = replace(quote(price=2400, verification='candidate'), page_checked=False)
    merged = market._latest_offers_for_row(row, [quote(ago=60)], [unchecked])
    assert len(merged) == 1 and merged[0].price == 2500 and merged[0].verification == 'verified'


def test_stale_region_cannot_override_current_region(job_context):
    row, *_ = job_context
    row['Регион поиска'] = 'Ярославль'
    wrong = replace(quote(price=2000), search_region='Москва', region_evidence='Москва')
    merged = market._latest_offers_for_row(row, [quote(ago=60)], [wrong])
    assert len(merged) == 1 and merged[0].price == 2500 and merged[0].verification == 'verified'


def test_api_reports_executor_and_filters_external_claim(job_context, monkeypatch):
    from autobot import web_ui
    client = web_ui.app.test_client()
    monkeypatch.setenv('MARKET_AGENT_TOKEN', 'fixture-token')
    monkeypatch.setenv('MARKET_WEB_WORKER', '1')
    assert client.get('/api/tenders/12345678/agent-market/jobs').get_json()['executor'] == 'server'
    assert client.get('/api/tenders/12345678/agent-market/jobs?mode=avito').get_json()['executor'] == 'external'
    response = client.post('/api/agent-market/v1/claim', json={'worker_id': 'avito', 'mode': 'avito'},
                           headers={'Authorization': 'Bearer fixture-token'})
    assert response.status_code == 200 and response.get_json()['job'] is None
    response = client.post('/api/agent-market/v1/claim', json={'worker_id': 'invalid', 'mode': []},
                           headers={'Authorization': 'Bearer fixture-token'})
    assert response.status_code == 400
    monkeypatch.setenv('MARKET_WEB_WORKER', '0')
    assert client.get('/api/tenders/12345678/agent-market/jobs').get_json()['executor'] == 'external'


def test_network_failures_stop_at_attempt_limit(job_context, monkeypatch):
    _, _, job_id, _, output = job_context
    def fail(*args, **kwargs):
        raise OSError('connection timeout')
    monkeypatch.setattr(worker, 'prepare_builtin_market_result', fail)
    assert worker.run_once('server')['status'] == 'queued'
    assert worker.run_once('server')['status'] == 'failed'
    assert worker.run_once('server') is None
    assert queue.get_job(job_id)['attempts'] == 2 and not output.exists()


@pytest.mark.parametrize('requested_mode', [None, 'web', 'avito'])
def test_external_api_cannot_take_server_web_jobs(job_context, monkeypatch, requested_mode):
    from autobot import web_ui
    _, payload, job_id, _, _ = job_context
    monkeypatch.setenv('MARKET_AGENT_TOKEN', 'fixture-token')
    monkeypatch.setenv('MARKET_WEB_WORKER', '1')
    client = web_ui.app.test_client()
    headers = {'Authorization': 'Bearer fixture-token'}
    data = {'worker_id': 'mac-mini-hermes'}
    if requested_mode is not None:
        data['mode'] = requested_mode
    assert client.post('/api/agent-market/v1/claim', json=data, headers=headers).get_json()['job'] is None
    assert queue.get_job(job_id)['status'] == 'queued'
    assert queue.get_job(job_id)['attempts'] == 0
    avito = queue.enqueue_jobs('12345678', [dict(payload, job_mode='avito')])['created'][0]
    claimed = client.post('/api/agent-market/v1/claim', json=data, headers=headers).get_json()['job']
    if requested_mode == 'web':
        assert claimed is None
        assert queue.get_job(avito['id'])['status'] == 'queued'
    else:
        assert claimed['id'] == avito['id']
    assert queue.claim_job('server', mode='web')['id'] == job_id


def test_external_web_claim_still_works_when_server_executor_disabled(job_context, monkeypatch):
    from autobot import web_ui
    monkeypatch.setenv('MARKET_AGENT_TOKEN', 'fixture-token')
    monkeypatch.setenv('MARKET_WEB_WORKER', '0')
    result = web_ui.app.test_client().post('/api/agent-market/v1/claim',
        json={'worker_id': 'external', 'mode': 'web'},
        headers={'Authorization': 'Bearer fixture-token'})
    assert result.status_code == 200 and result.get_json()['job']['id'] == job_context[2]


def test_fresh_rejected_page_invalidates_cached_quote_and_blocks_late_old_write(tmp_path, monkeypatch):
    from autobot import market_price_index as index
    monkeypatch.setattr(index, 'REPO_ROOT', tmp_path)
    monkeypatch.setattr(index, 'INDEX_ROOT', tmp_path / 'index')
    monkeypatch.setattr(index, 'INDEX_DB', tmp_path / 'index' / 'prices.sqlite3')
    monkeypatch.setattr(index, 'AUDIT_ROOT', tmp_path / 'index' / 'audit')
    context = dict(tender_id='12345678', name='Щебень гранитный 20-40', unit='м3', region='Ярославль')
    search = {k: v for k, v in context.items() if k != 'tender_id'}
    old = vars(quote(ago=60)).copy()
    assert index.record_verified_offers(**context, offers=[old]) == 1
    assert len(index.lookup_verified_offers(**search)) == 1
    audit_before = set(index.AUDIT_ROOT.rglob('*.json'))
    candidate = vars(quote(verification='candidate')).copy()
    candidate['evidence'] = 'Щебень гранитный 20-40 от 2500 руб/м3'
    index.record_verified_offers(**context, offers=[candidate], record_candidates=True)
    assert index.lookup_verified_offers(**search) == []
    assert index.record_verified_offers(**context, offers=[old]) == 0
    assert index.lookup_verified_offers(**search) == []
    assert audit_before <= set(index.AUDIT_ROOT.rglob('*.json'))
    # A genuinely newer exact observation can be used again.
    assert index.record_verified_offers(**context, offers=[vars(quote(price=2600))]) == 1
    assert index.lookup_verified_offers(**search)[0]['price'] == 2600


def test_unopened_candidate_does_not_invalidate_cached_quote(tmp_path, monkeypatch):
    from autobot import market_price_index as index
    monkeypatch.setattr(index, 'REPO_ROOT', tmp_path)
    monkeypatch.setattr(index, 'INDEX_ROOT', tmp_path / 'index')
    monkeypatch.setattr(index, 'INDEX_DB', tmp_path / 'index' / 'prices.sqlite3')
    monkeypatch.setattr(index, 'AUDIT_ROOT', tmp_path / 'index' / 'audit')
    context = dict(tender_id='12345678', name='Щебень гранитный 20-40', unit='м3', region='Ярославль')
    assert index.record_verified_offers(**context, offers=[vars(quote(ago=60))]) == 1
    index.record_verified_offers(**context, offers=[vars(replace(quote(verification='candidate'), page_checked=False))], record_candidates=True)
    assert len(index.lookup_verified_offers(name=context['name'], unit='м3', region='Ярославль')) == 1


@pytest.mark.parametrize('change', ['estimate', 'region'])
def test_whole_search_does_not_publish_after_input_changed(job_context, monkeypatch, change):
    row, payload, job_id, estimate, output = job_context
    monkeypatch.setattr(market, '_offers_from_local_index', lambda *a, **kw: [])
    monkeypatch.setattr(market, 'append_market_web_event', lambda *a, **kw: None)
    monkeypatch.setenv('MARKET_AVITO_BROWSER', '0')
    monkeypatch.setenv('MARKET_INDEX_BACKFILL', '0')
    def search(*args, **kwargs):
        if change == 'estimate':
            pd.DataFrame([dict(row, **{market.COL_QTY: 3})]).to_excel(estimate, index=False)
        else:
            monkeypatch.setattr(market, 'load_tender_metadata', lambda: {'12345678': {'region': 'Миасс'}})
        return [quote()], ''
    monkeypatch.setattr(market, '_research_row_market', search)
    with pytest.raises(ValueError, match='изменились'):
        market.run_tender('12345678', max_rows=1, sources=['web'], pause=0)
    assert not output.exists()
