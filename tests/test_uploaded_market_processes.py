"""Real process exits around admission, leases and accepted evidence publication."""
import json
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

from autobot import uploaded_estimates as store, uploaded_market as flow, agent_market_queue as queue

SOURCE=Path(__file__).resolve().parents[1]
EID='a'*16
RUN='b'*32
BOOT='''import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from autobot import uploaded_market as flow, agent_market_queue as queue, real_market_scraper as market
root=Path(sys.argv[2]);mode=sys.argv[3];marker=root/sys.argv[4]
queue.DEFAULT_DB_PATH=root/'queue.sqlite3';flow.ESTIMATES_ROOT=root/'estimates'
if mode=='parallel':
    marker.write_text('ready')
    until=time.monotonic()+20
    while not (root/'go').exists():
        if time.monotonic()>until:raise RuntimeError('gate timeout')
        time.sleep(.02)
original=queue.enqueue_in_transaction
def pause_admission(*args,**kwargs):
    result=original(*args,**kwargs)
    marker.write_text('before commit');time.sleep(180)
    return result
if mode=='admission':queue.enqueue_in_transaction=pause_admission
run,duplicate=flow.enqueue('a'*16,city='Ярославль',operation_id='b'*32)
if mode=='parallel':
    print(str(duplicate),flush=True);raise SystemExit(0)
if mode=='receipt':marker.write_text('accepted');time.sleep(180)
job=queue.claim_job('killed-server',mode='web',include_uploaded=True)
if mode=='lease':marker.write_text('leased');time.sleep(180)
_,_,key,row,_,_,_,digest=flow.import_context(job['tender_id'],job['payload'])
prepared={'schema_version':1,'position_key':key,'estimate_digest':digest,'region':'Ярославль','offers':[]}
queue.accept_job_result(job['id'],'killed-server',{'offers':[],'notes':'QA: no offers'},prepared,lease_token=job['lease_token'])
if mode=='accepted':marker.write_text('accepted evidence');time.sleep(180)
def publish(*args):
    result=market.publish_agent_market_result(*args)
    marker.write_text('files saved');time.sleep(180)
    return result
queue.apply_accepted_result(job['id'],publish)
'''


def seed(root):
    folder=root/'estimates'/EID
    store.write_json(folder/'meta.json',{'id':EID,'title':'Process QA','row_count':1})
    store.write_json(folder/'rows.json',[{'name':'Щебень гранитный 20-40','unit':'м3','qty':2,'unit_price':3000,'total':6000,
        'type':'material','type_label':'Материал','position_id':'pdf:1:20','estimate_version':'c'*64}])


def start(root,mode,marker='ready'):
    return subprocess.Popen([sys.executable,'-X','utf8','-c',BOOT,str(SOURCE),str(root),mode,marker],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')


def wait_marker(path):
    until=time.monotonic()+25
    while not path.exists():
        if time.monotonic()>until:raise AssertionError('Market process did not reach boundary')
        time.sleep(.03)


def kill(process):
    if process.poll() is None:process.kill()
    process.communicate(timeout=10)


def test_exit_before_admission_commit_leaves_no_partial_run(tmp_path):
    seed(tmp_path)
    process=start(tmp_path,'admission')
    try:wait_marker(tmp_path/'ready')
    finally:kill(process)
    database=tmp_path/'queue.sqlite3'
    assert flow.latest(EID,path=database) is None
    assert queue.list_jobs(flow.subject(EID),path=database)==[]
    run,duplicate=flow.enqueue(EID,city='Ярославль',operation_id=RUN,root=tmp_path/'estimates',path=database)
    assert not duplicate and run['run_id']==RUN


def test_exit_after_admission_reuses_the_saved_run(tmp_path):
    seed(tmp_path)
    process=start(tmp_path,'receipt')
    try:wait_marker(tmp_path/'ready')
    finally:kill(process)
    database=tmp_path/'queue.sqlite3'
    before=queue.list_jobs(flow.subject(EID),path=database)
    run,duplicate=flow.enqueue(EID,city='Ярославль',operation_id=RUN,root=tmp_path/'estimates',path=database)
    assert duplicate and run['run_id']==RUN and queue.list_jobs(flow.subject(EID),path=database)==before


def test_dead_worker_lease_is_reclaimed_by_the_server(tmp_path):
    seed(tmp_path)
    process=start(tmp_path,'lease')
    try:wait_marker(tmp_path/'ready')
    finally:kill(process)
    database=tmp_path/'queue.sqlite3'
    job=queue.list_jobs(flow.subject(EID),path=database)[0]
    assert job['status']=='leased' and job['attempts']==1
    with patch.object(queue,'_now',return_value=time.time()+400):
        assert queue.claim_job('external',mode='web',path=database) is None
        again=queue.claim_job('restarted-server',mode='web',include_uploaded=True,path=database)
    assert again['id']==job['id'] and again['attempts']==2


def recover(root):
    from autobot import agent_market_delivery as delivery, real_market_scraper as market
    with patch.object(queue,'DEFAULT_DB_PATH',root/'queue.sqlite3'),patch.object(flow,'ESTIMATES_ROOT',root/'estimates'), \
         patch.object(market,'_research_row_market',side_effect=AssertionError('Repeated supplier request')):
        assert delivery.recover_accepted_results()['completed']==1
        assert flow.status(EID)['ok']
        assert delivery.recover_accepted_results()['completed']==0
    import pandas as pd
    saved=pd.read_excel(root/'estimates'/EID/'market_sources.xlsx')
    assert len(saved)==1 and saved.iloc[0]['position_id']=='pdf:1:20'


def test_exit_after_acceptance_publishes_without_another_search(tmp_path):
    seed(tmp_path)
    process=start(tmp_path,'accepted')
    try:wait_marker(tmp_path/'ready')
    finally:kill(process)
    assert not (tmp_path/'estimates'/EID/'market_sources.xlsx').exists()
    recover(tmp_path)


def test_exit_after_file_publication_replays_without_duplicate_rows(tmp_path):
    seed(tmp_path)
    process=start(tmp_path,'published')
    try:wait_marker(tmp_path/'ready')
    finally:kill(process)
    assert (tmp_path/'estimates'/EID/'market_sources.xlsx').exists()
    recover(tmp_path)


def test_two_processes_admit_only_one_run(tmp_path):
    seed(tmp_path)
    processes=[start(tmp_path,'parallel','ready'+str(index)) for index in (1,2)]
    try:
        for index in (1,2):wait_marker(tmp_path/('ready'+str(index)))
        (tmp_path/'go').write_text('go')
        results=[process.communicate(timeout=25) for process in processes]
        assert all(process.returncode==0 for process in processes),results
        assert sorted(result[0].strip() for result in results)==['False','True']
    finally:
        for process in processes:kill(process)
    assert len(queue.list_jobs(flow.subject(EID),path=tmp_path/'queue.sqlite3'))==1
