from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path

import pytest

from autobot import uploaded_estimates as store, web_ui, estimate_parse_worker as parser
from autobot.estimate_excel_analysis import EstimateRow


def record(root, eid='a'*16):
    source=root/eid/'source.xlsx'
    source.parent.mkdir(parents=True,exist_ok=True)
    source.write_bytes(b'local source fixture')
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    row=EstimateRow(idx=1,name='Устройство покрытия',unit='100 м2',qty=.2,unit_price=100.05,total=20.01,position_id='excel:Sheet1:row2')
    positions=[dict(web_ui._estimate_row_to_dict(row),estimate_version=digest)]
    meta={'id':eid,'title':'Контрольная смета','source_sha256':digest,'source_path':str(source),
          'row_count':1,'total_sum':20.01,'original_filename':source.name,'created_at':'15.09.2026 12:00'}
    return source,meta,positions,row


def bind(tmp_path,monkeypatch):
    root=tmp_path/'data/user_estimates'
    monkeypatch.setattr(web_ui,'REPO_ROOT',tmp_path)
    monkeypatch.setattr(web_ui,'USER_ESTIMATES_DIR',root)
    monkeypatch.setattr(web_ui,'USER_ESTIMATES_INDEX',root/'index.json')
    monkeypatch.setattr(web_ui,'ESTIMATE_UPLOAD_JOBS_DIR',root/'.upload_jobs')
    monkeypatch.setattr(web_ui,'estimate_upload_jobs',{})
    monkeypatch.setattr(web_ui,'estimate_upload_workers',set())
    monkeypatch.setattr(web_ui,'estimate_market_jobs',{})
    return root


def job(root,source,eid,*,attempts=0):
    jid='b'*16
    value={'job_id':jid,'target_estimate_id':eid,'source_path':str(source),'running':True,'ok':False,
           'attempts':attempts,'progress':26,'log_lines':[],'original_name':source.name,'title_raw':'Контрольная смета'}
    web_ui.estimate_upload_jobs[jid]=value
    web_ui._estimate_upload_persist_locked(value,strict=True)
    return jid


def run(jid,source,meta):
    web_ui._run_estimate_upload_worker(jid,estimate_id=meta['id'],title_raw=meta['title'],original_name=source.name,src_path=source)


def test_empty_read_does_not_create_storage(tmp_path):
    assert store.catalogue(tmp_path)==[] and store.meta(tmp_path,'a'*16) is None
    assert store.rows(tmp_path,'a'*16) is None and not list(tmp_path.iterdir())


def test_whole_publication_retry_conflict_and_metadata_update(tmp_path):
    source,meta,rows,_=record(tmp_path)
    assert store.publish(tmp_path,meta,rows)
    assert not store.publish(tmp_path,meta,rows)
    assert store.catalogue(tmp_path)==[meta] and store.rows(tmp_path,meta['id'])==rows
    with pytest.raises(store.StoreError,match='другая версия'):
        store.publish(tmp_path,dict(meta,title='Другой документ'),rows)
    assert store.meta(tmp_path,meta['id'])==meta
    assert store.update_market_meta(tmp_path,meta['id'],{'market_city':'Ярославль'})
    assert store.meta(tmp_path,meta['id'])['market_city']=='Ярославль'
    with pytest.raises(store.StoreError):
        store.update_market_meta(tmp_path,meta['id'],{'total_sum':0})
    assert store.rows(tmp_path,meta['id'])==rows and source.read_bytes()==b'local source fixture'


def test_invalid_or_nonfinite_result_never_publishes(tmp_path):
    _,meta,rows,_=record(tmp_path)
    for changed in [dict(meta,row_count=2),dict(meta,source_sha256='bad'),dict(meta,total_sum=float('nan'))]:
        with pytest.raises(store.StoreError):store.publish(tmp_path,changed,rows)
    assert store.catalogue(tmp_path)==[]


def test_legacy_and_new_estimates_share_existing_consumers_and_delete(tmp_path,monkeypatch):
    root=bind(tmp_path,monkeypatch)
    _,legacy,old_rows,_=record(root,'c'*16)
    files={root/'index.json':[legacy],root/legacy['id']/'meta.json':legacy,root/legacy['id']/'rows.json':old_rows}
    for path,value in files.items():store.write_json(path,value)
    before={path:path.read_bytes() for path in files}
    source,current,rows,_=record(root)
    store.publish(root,current,rows)
    assert len(web_ui._read_estimates_index())==2
    for meta in (legacy,current):
        assert web_ui._load_estimate_meta(meta['id'])==meta
        assert web_ui._load_estimate_rows(meta['id'])==(old_rows if meta is legacy else rows)
        client=web_ui.app.test_client()
        assert client.get('/estimates/'+meta['id']).status_code==200
        assert client.get('/estimates/'+meta['id']+'/download.xlsx').status_code==200
        endpoint='/api/estimates/'+meta['id']+'/crm-import-payload'
        assert client.get(endpoint).status_code==403
        capability=web_ui._issue_estimate_import_capability(meta['id'])
        response=client.get(endpoint,headers={'X-AutoBot-Estimate-Capability':capability,'Sec-Fetch-Site':'same-origin'})
        assert response.status_code==200 and len(response.json['items'])==1
    web_ui.delete_estimate(current['id'])
    assert web_ui._read_estimates_index()==[legacy]
    assert not source.parent.exists()
    assert all(path.read_bytes()==content for path,content in before.items())


def test_corrupt_database_fails_closed_instead_of_showing_empty_catalogue(tmp_path,monkeypatch):
    root=bind(tmp_path,monkeypatch);root.mkdir(parents=True)
    (root/'estimates.sqlite3').write_bytes(b'broken SQLite')
    assert web_ui.app.test_client().get('/estimates').status_code==503


def test_saved_result_recovers_without_repeating_parser(tmp_path,monkeypatch):
    root=bind(tmp_path,monkeypatch)
    source,meta,rows,_=record(root)
    jid=job(root,source,meta['id'],attempts=3)
    store.publish(root,meta,rows)
    monkeypatch.setattr(parser,'run_uploaded_parser',lambda *a,**k:pytest.fail('Completed parse repeated'))
    run(jid,source,meta)
    status=json.loads((root/'.upload_jobs'/f'{jid}.json').read_text(encoding='utf-8'))
    assert status['ok'] and not status['running'] and status['estimate_id']==meta['id']
    assert status['attempts']==3 and len(store.catalogue(root))==1


def test_three_interrupted_attempts_stop_automatic_recovery(tmp_path,monkeypatch):
    root=bind(tmp_path,monkeypatch)
    source,meta,_,_=record(root)
    jid=job(root,source,meta['id'],attempts=3)
    monkeypatch.setattr(parser,'run_uploaded_parser',lambda *a,**k:pytest.fail('Recovery budget ignored'))
    run(jid,source,meta)
    assert not web_ui.estimate_upload_jobs[jid]['running']
    assert 'Три попытки' in web_ui.estimate_upload_jobs[jid]['error']
    assert not store.catalogue(root) and source.is_file()


def test_legacy_partial_files_are_not_a_success_proof(tmp_path,monkeypatch):
    root=bind(tmp_path,monkeypatch)
    source,meta,_,row=record(root)
    jid=job(root,source,meta['id'])
    (source.parent/'meta.json').write_text('{}')
    (source.parent/'rows.json').write_text('[]')
    parsed={'rows':[asdict(row)],'diagnostics':{},'sources':parser.snapshot([source])}
    calls=[]
    def parse(*a,**k):calls.append(1);return parsed
    monkeypatch.setattr(parser,'run_uploaded_parser',parse)
    run(jid,source,meta)
    assert calls==[1] and web_ui.estimate_upload_jobs[jid]['ok']
    assert len(store.rows(root,meta['id']))==1 and len(web_ui._read_estimates_index())==1


def test_source_change_before_first_publication_cannot_create_result(tmp_path,monkeypatch):
    root=bind(tmp_path,monkeypatch)
    source,meta,_,row=record(root)
    jid=job(root,source,meta['id'])
    parsed={'rows':[asdict(row)],'diagnostics':{},'sources':parser.snapshot([source])}
    def parse(*a,**k):source.write_bytes(b'changed while parsing');return parsed
    monkeypatch.setattr(parser,'run_uploaded_parser',parse)
    run(jid,source,meta)
    assert not store.catalogue(root) and not web_ui.estimate_upload_jobs[jid]['ok']


def test_admission_requires_persisted_job_before_start(tmp_path,monkeypatch):
    root=bind(tmp_path,monkeypatch)
    monkeypatch.setattr(store,'write_json',lambda *a,**k:(_ for _ in ()).throw(OSError('disk unavailable')))
    monkeypatch.setattr(web_ui,'_start_estimate_upload_worker',lambda *a,**k:pytest.fail('Non-durable job started'))
    response=web_ui.app.test_client().post('/api/estimates/upload',data={'file':(io.BytesIO(b'fixture'),'source.xlsx')})
    assert response.status_code==503 and not response.json['ok']
    # An immutable receipt must be durable even before the source is published.
    assert not web_ui.estimate_upload_jobs and not list(root.glob('*/source.xlsx'))


def test_status_reads_new_progress_from_other_process(tmp_path,monkeypatch):
    root=bind(tmp_path,monkeypatch)
    source,meta,_,_=record(root);jid=job(root,source,meta['id'])
    disk=dict(web_ui.estimate_upload_jobs[jid],progress=91,running=False,ok=False)
    store.write_json(root/'.upload_jobs'/f'{jid}.json',disk)
    assert web_ui.estimate_upload_jobs[jid]['progress']==26
    response=web_ui.app.test_client().get('/api/estimates/upload-status/'+jid)
    assert response.status_code==200 and response.json['progress']==91


def test_cache_cleanup_keeps_every_running_job(tmp_path,monkeypatch):
    bind(tmp_path,monkeypatch)
    web_ui.estimate_upload_jobs.update({f'{i:016x}':{'running':True,'started_at':str(i)} for i in range(20)})
    web_ui._estimate_upload_cleanup()
    assert len(web_ui.estimate_upload_jobs)==20


def test_failed_observer_thread_cannot_overwrite_an_active_owner(tmp_path,monkeypatch):
    from autobot.atomic_output import output_lock
    root=bind(tmp_path,monkeypatch)
    source,meta,_,_=record(root);jid=job(root,source,meta['id'],attempts=1)
    class BrokenThread:
        def __init__(self,**kwargs):pass
        def start(self):raise RuntimeError('Thread unavailable')
    monkeypatch.setattr(web_ui.threading,'Thread',BrokenThread)
    with output_lock(web_ui._estimate_upload_job_path(jid).with_suffix('.run')):
        assert not web_ui._start_estimate_upload_worker(jid,recovering=True)
    saved=json.loads((root/'.upload_jobs'/f'{jid}.json').read_text(encoding='utf-8'))
    assert saved['running'] and saved['attempts']==1 and not saved.get('error')


def test_rollback_export_preserves_new_rows_and_legacy_catalogue_without_writing_live_data(tmp_path):
    root=tmp_path/'live'
    _,meta,rows,_=record(root)
    legacy={'id':'c'*16,'title':'Legacy estimate'}
    store.write_json(root/'index.json',[legacy])
    store.publish(root,meta,rows)
    before={path:path.read_bytes() for path in root.rglob('*') if path.is_file()}
    destination=tmp_path/'rollback-artifact'
    assert store.export_legacy_snapshot(root,destination)==1
    assert json.loads((destination/meta['id']/'rows.json').read_text(encoding='utf-8'))==rows
    assert json.loads((destination/meta['id']/'meta.json').read_text(encoding='utf-8'))==meta
    assert json.loads((destination/'index.json').read_text(encoding='utf-8'))==[meta,legacy]
    assert all(path.read_bytes()==value for path,value in before.items())


@pytest.mark.parametrize('inside',[False,True])
def test_rollback_export_rejects_existing_or_live_destination(tmp_path,inside):
    root=tmp_path/'live';root.mkdir()
    destination=root/'nested' if inside else tmp_path/'existing'
    if not inside:destination.mkdir()
    with pytest.raises(store.StoreError):store.export_legacy_snapshot(root,destination)
