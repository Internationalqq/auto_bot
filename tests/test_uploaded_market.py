from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from autobot import agent_market_queue as queue, uploaded_estimates as store, uploaded_market as flow
from autobot import real_market_scraper as market, market_web_worker as worker, web_ui as web
from autobot.market_contract import confirmed_prices, position_identity
from test_uploaded_estimate_store import bind

EID='a'*16
RUN='b'*32


@pytest.fixture
def context(tmp_path,monkeypatch):
    root=bind(tmp_path,monkeypatch)
    rows=[{'idx':n,'position_id':f'pdf:page1:row{n}','estimate_version':'c'*64,
           'name':'Щебень гранитный 20-40','unit':'м3','qty':n,'unit_price':3000,'total':3000*n,
           'type':'material','type_label':'Материал','sheet':'Смета','source':'pdf'} for n in (1,2)]
    metadata={'id':EID,'title':'Контрольная смета','original_filename':'source.pdf','source_path':str(root/EID/'source.pdf'),
              'row_count':len(rows),'created_at':'15.09.2026 10:00','source_sha256':'d'*64}
    store.write_json(root/EID/'rows.json',rows)
    store.write_json(root/EID/'meta.json',metadata)
    store.write_json(root/'index.json',[metadata])
    (root/EID/'source.pdf').write_bytes(b'original QA bytes')
    monkeypatch.setattr(market,'_MARKET_SEARCH_LOG_PATH',tmp_path/'search.jsonl')
    return root,rows,web.app.test_client()


def start(client,key=RUN,**changes):
    return client.post('/api/estimates/'+EID+'/market-start',json=dict(city='Ярославль',operation_id=key,selected_types=[],**changes))


def quote(price=2500,unit='м3',city='Ярославль'):
    return market.MarketOffer('Интернет','Щебень гранитный 20-40',price,'https://supplier.example/stone',
        verification='verified',matched_unit=unit,search_region=city,region_evidence='Доставка: '+city,
        evidence=f'Щебень гранитный 20-40, {price} руб/{unit}',page_checked=True,
        observed_at=datetime.now(timezone.utc).isoformat())


def test_read_does_not_create_queue_and_repeat_keeps_exact_run(context):
    root,rows,client=context
    assert client.get('/api/estimates/'+EID+'/market-status').json['running'] is False
    assert not queue.DEFAULT_DB_PATH.exists()
    first=start(client)
    assert first.status_code==200 and first.json['accepted'] and first.json['run_id']==RUN
    again=start(client)
    assert again.json['duplicate'] and again.json['run_id']==RUN
    assert len(queue.list_jobs(flow.subject(EID)))==2
    assert flow.status(EID)['total']==2 and flow.status(EID)['running']
    assert len({job['position_key'] for job in queue.list_jobs(flow.subject(EID))})==2
    # Old clients without a key still receive a run; the active guard is global.
    assert client.post('/api/estimates/'+EID+'/market-start',json={}).status_code==409
    assert queue.claim_job('external-browser',mode='web') is None


def test_conflicting_parameters_and_source_are_not_new_jobs(context):
    root,rows,client=context
    start(client)
    conflict=client.post('/api/estimates/'+EID+'/market-start',json={'city':'Казань','operation_id':RUN})
    assert conflict.status_code==409
    store.write_json(root/EID/'rows.json',[dict(rows[0],name='Другая марка'),rows[1]])
    assert start(client).status_code==409
    assert len(queue.list_jobs(flow.subject(EID)))==2


@pytest.mark.parametrize('payload',[[],None,{'city':{}},{'selected_types':['unrecognized']},{'selected_types':'material'},
                                     {'sources':['web','avito']},{'operation_id':'../no'}])
def test_invalid_launch_input(context,payload):
    _,_,client=context
    response=client.post('/api/estimates/'+EID+'/market-start',json=payload)
    assert response.status_code==400


def test_two_admissions_and_database_failure_are_atomic(context,monkeypatch):
    root,_,client=context
    original=queue.enqueue_in_transaction
    def interrupted(connection,*a,**kw):
        original(connection,*a,**kw)
        raise OSError('Interrupted before run association')
    with monkeypatch.context() as patch:
        patch.setattr(queue,'enqueue_in_transaction',interrupted)
        assert start(client).status_code==503
    assert flow.latest(EID) is None and not queue.list_jobs(flow.subject(EID))
    def enqueue(_):return flow.enqueue(EID,city='Ярославль',operation_id=RUN,root=root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(enqueue,range(2)))
    assert sorted(duplicate for _,duplicate in results)==[False,True]
    assert len(queue.list_jobs(flow.subject(EID)))==2


def test_queue_worker_preserves_both_physical_rows_and_next_start(context,monkeypatch):
    root,rows,client=context
    calls=[]
    def search(row,plan,**kwargs):
        calls.append((position_identity(row),kwargs['sources'],row['Регион поиска']))
        return [quote()],''
    monkeypatch.setattr(market,'_research_row_market',search)
    start(client)
    assert worker.run_once('server')['status']=='completed'
    assert worker.run_once('server')['status']=='completed'
    assert worker.run_once('server') is None
    saved=pd.read_excel(root/EID/'market_sources.xlsx')
    assert len(saved)==2 and all(confirmed_prices(row)==[2500] for _,row in saved.iterrows())
    assert len({call[0] for call in calls})==2 and all(call[1:]==(['web'],'Ярославль') for call in calls)
    result=client.get('/api/estimates/'+EID+'/market-status').json
    assert result['run_id']==RUN and result['result_ok'] and result['done']==result['total']==2
    assert 'lease_token' not in json.dumps(result) and '_delivery' not in json.dumps(result)
    # A late repeated POST keeps a completed operation and performs no search.
    assert start(client).json['duplicate'] and worker.run_once('server') is None
    assert start(client,key='f'*32).status_code==200
    assert flow.status(EID)['run_id']=='f'*32
    assert flow.status(EID,run_id=RUN)['total']==2


def test_cancel_is_exact_and_late_accepted_result_cannot_publish(context):
    root,_,client=context
    start(client)
    job=queue.claim_job('server',include_uploaded=True,mode='web')
    _,_,key,row,_,_,_,digest=flow.import_context(job['tender_id'],job['payload'])
    prepared={'schema_version':1,'position_key':key,'estimate_digest':digest,'region':'Ярославль','offers':[vars(quote())]}
    assert queue.accept_job_result(job['id'],'server',{'offers':[]},prepared,lease_token=job['lease_token'])
    canceled=client.post('/api/estimates/'+EID+'/market-stop',json={'run_id':RUN})
    assert canceled.status_code==200 and canceled.json['canceled']==2
    assert queue.apply_accepted_result(job['id'],market.publish_agent_market_result) is None
    assert not (root/EID/'market_sources.xlsx').exists()
    assert flow.status(EID)['stage']=='Остановлено' and not flow.status(EID)['ok']
    assert start(client,key='f'*32).status_code==200
    assert client.post('/api/estimates/'+EID+'/market-stop',json={'run_id':RUN}).status_code==409
    assert flow.status(EID)['running']


@pytest.mark.parametrize('change',['source','deleted','new_run'])
def test_input_changes_during_search_reject_old_result(context,monkeypatch,change):
    root,rows,client=context
    start(client)
    def search(*args,**kwargs):
        if change=='source': store.write_json(root/EID/'rows.json',[dict(rows[0],qty=5),rows[1]])
        elif change=='deleted': (root/EID/'meta.json').unlink()
        else:
            flow.cancel(EID,run_id=RUN)
            flow.enqueue(EID,city='Казань',operation_id='f'*32)
        return [quote()],''
    monkeypatch.setattr(market,'_research_row_market',search)
    result=worker.run_once('server')
    assert result['status'] in {'failed','canceled'}
    assert not (root/EID/'market_sources.xlsx').exists()


def test_interrupted_publication_replays_without_supplier_request(context,monkeypatch):
    root,_,client=context
    from autobot import atomic_output
    calls=[]
    monkeypatch.setattr(market,'_research_row_market',lambda *a,**kw:(calls.append(1) or [quote()],''))
    start(client)
    write=atomic_output.write_excel
    def fail_raw(frame,path):
        if path.name=='market_sources.xlsx':raise OSError('disk temporarily unavailable')
        write(frame,path)
    with monkeypatch.context() as patch:
        patch.setattr(atomic_output,'write_excel',fail_raw)
        job=worker.run_once('server')
    assert job['delivery_pending'] and (root/EID/'market_compare.xlsx').exists()
    assert not (root/EID/'market_sources.xlsx').exists()
    assert web._estimate_market_df_for_rows(root/EID/'market_compare.xlsx',store.load_rows(root,EID)).empty
    assert queue.apply_accepted_result(job['id'],market.publish_agent_market_result)['status']=='completed'
    assert len(calls)==1 and len(pd.read_excel(root/EID/'market_sources.xlsx'))==1
    again=queue.apply_accepted_result(job['id'],market.publish_agent_market_result)
    assert again['status']=='completed' and len(calls)==1


def test_no_offers_is_finished_research_and_keeps_diagnostic(context,monkeypatch):
    root,_,client=context
    monkeypatch.setattr(market,'_research_row_market',lambda *a,**kw:([],'Подходящих предложений не найдено'))
    start(client)
    worker.run_once('server');worker.run_once('server')
    assert flow.status(EID)['ok']
    frame=pd.read_excel(root/EID/'market_sources.xlsx')
    assert len(frame)==2 and not any(confirmed_prices(row) for _,row in frame.iterrows())
    assert any('Подходящих предложений' in line for line in flow.status(EID)['log_lines'])


def test_city_change_disqualifies_saved_evidence_and_delete_respects_queue(context,monkeypatch):
    root,rows,client=context
    monkeypatch.setattr(market,'_research_row_market',lambda *a,**kw:([quote()],''))
    start(client)
    assert client.post('/api/estimates/'+EID+'/delete').status_code==409
    worker.run_once('server');worker.run_once('server')
    changed=client.post('/api/estimates/'+EID+'/market-start',json={'city':'Казань','operation_id':'f'*32})
    assert changed.status_code==200 and web._load_estimate_meta(EID)['market_city']=='Казань'
    checked=web._estimate_market_df_for_rows(root/EID/'market_sources.xlsx',rows)
    assert not any(confirmed_prices(row) for _,row in checked.iterrows())
    flow.cancel(EID)
    assert client.post('/api/estimates/'+EID+'/delete').status_code==200
    assert not (root/EID).exists()
    with closing(sqlite3.connect(queue.DEFAULT_DB_PATH)) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert connection.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_legacy_missing_ids_are_stable_before_and_after_filter(context):
    root,rows,client=context
    legacy=[{key:value for key,value in row.items() if key!='position_id'} for row in rows]
    store.write_json(root/EID/'rows.json',legacy)
    first=store.load_rows(root,EID)
    assert first==store.load_rows(root,EID)
    assert len({row['position_id'] for row in first})==2
    assert start(client).status_code==200
    for job in queue.list_jobs(flow.subject(EID)):
        assert flow.import_context(job['tender_id'],job['payload'])[2]==job['position_key']


def test_api_storage_failure_is_explicit_without_legacy_empty_fallback(context,monkeypatch):
    root,rows,client=context
    start(client)
    with closing(sqlite3.connect(queue.DEFAULT_DB_PATH)) as connection,connection:
        connection.execute('DELETE FROM uploaded_market_run_jobs WHERE job_id=(SELECT job_id FROM uploaded_market_run_jobs LIMIT 1)')
    assert client.get('/api/estimates/'+EID+'/market-status').status_code==503


def test_unreadable_saved_evidence_is_preserved(context,monkeypatch):
    root,_,client=context
    raw=root/EID/'market_sources.xlsx'
    raw.write_bytes(b'corrupt but retained for recovery')
    monkeypatch.setattr(market,'_research_row_market',lambda *a,**kw:([quote()],''))
    start(client)
    result=worker.run_once('server')
    assert result['status']=='failed'
    assert raw.read_bytes()==b'corrupt but retained for recovery'


def test_empty_json_list_cannot_stop_current_search(context):
    _,_,client=context
    start(client)
    assert client.post('/api/estimates/'+EID+'/market-stop',json=[]).status_code==400
    assert flow.status(EID)['running']
