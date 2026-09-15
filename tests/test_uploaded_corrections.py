from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
import urllib.error

import pandas as pd
import pytest
from autobot import uploaded_corrections as edit, uploaded_estimates as store, web_ui as web
from autobot import crm_actor, uploaded_market as market, agent_market_queue as queue
from autobot.market_contract import match_market_rows
from test_uploaded_estimate_store import bind, record


@pytest.fixture
def context(tmp_path, monkeypatch):
    root=bind(tmp_path,monkeypatch)
    source,meta,rows,_=record(root)
    rows.append(dict(rows[0],position_id='excel:Sheet1:row3',idx=2))
    meta.update(row_count=2,total_sum=40.02,reconciliation={'declared_total':50,'signed_position_total':40.02,'unallocated_total':9.98})
    store.publish(root,meta,rows)
    monkeypatch.setattr(crm_actor,'resolve',lambda _: {'id':7,'name':'Автор проверки'})
    return root,meta,rows,source,web.app.test_client()


def request_data(root, meta, rows, **updates):
    data={'position_id':rows[0]['position_id'],'changes':{'unit_price':'125.05','total':'25.01'},
          'expected_version':edit.snapshot(root,meta['id'])['version'],'operation_id':'b'*32,
          'reason':'Сверено с исходной строкой'}
    data.update(updates)
    return data


def apply(context, **updates):
    root,meta,rows,_,_=context
    return edit.apply(root,meta['id'],actor={'id':7,'name':'Автор проверки'},**request_data(root,meta,rows,**updates))


def test_read_only_and_exact_money_with_immutable_original(context):
    root,meta,rows,source,_=context
    before=source.read_bytes()
    original_meta=store.meta(root,meta['id'])
    first=edit.snapshot(root,meta['id'])
    assert first['revision']==0 and not (root/'.corrections.sqlite3').exists()
    event,duplicate=apply(context)
    assert not duplicate and event['actor']['id']==7
    current=edit.snapshot(root,meta['id'])
    row=current['rows'][0]
    assert row['total']=='25.01' and row['total_kopecks']==2501 and row['unit_price_kopecks']==12505
    assert row['qty']==.2 and row['unit']=='100 м2'
    assert current['rows'][1]==rows[1]
    assert current['meta']['total_sum_kopecks']==4502
    assert current['meta']['reconciliation']['unallocated_total']==4.98
    assert current['revision']==1 and current['version']!=first['version']
    assert current['original_rows']==rows and store.rows(root,meta['id'])==rows and store.meta(root,meta['id'])==original_meta
    assert source.read_bytes()==before
    history=edit.history(root,meta['id'])
    assert history[0]['changes']['total']=={'before':20.01,'after':'25.01'}
    assert 'overrides' not in history[0] and 'request_fingerprint' not in history[0]


def test_duplicate_receipt_survives_later_edits_and_conflicts(context):
    root,meta,rows,_,_=context
    data=request_data(root,meta,rows)
    first,_=edit.apply(root,meta['id'],actor={'id':7,'name':'Автор'},**data)
    apply(context,operation_id='c'*32,changes={'qty':'0.3'})
    again,duplicate=edit.apply(root,meta['id'],actor={'id':7,'name':'Переименованный автор'},**data)
    assert duplicate and again['version']==first['version'] and edit.snapshot(root,meta['id'])['revision']==2
    for extra in [{'reason':'другая причина'},{'changes':{'total':'30'}}]:
        with pytest.raises(edit.CorrectionError) as error:
            edit.apply(root,meta['id'],actor={'id':7,'name':'Автор'},**dict(data,**extra))
        assert error.value.status==409
    with pytest.raises(edit.CorrectionError):
        edit.apply(root,meta['id'],actor={'id':8,'name':'Другой'},**data)


def test_concurrent_edit_returns_one_version_and_one_conflict(context):
    root,meta,rows,_,_=context
    data=request_data(root,meta,rows)
    def save(key):
        try:return edit.apply(root,meta['id'],actor={'id':7,'name':'Автор'},**dict(data,operation_id=key))[0]['revision']
        except edit.CorrectionError as error:return error.status
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(save,['b'*32,'c'*32]))
    assert sorted(results)==[1,409] and len(edit.history(root,meta['id']))==1


def test_failed_commit_preserves_previous_values_and_can_retry(context,monkeypatch):
    root,meta,rows,_,_=context
    data=request_data(root,meta,rows)
    original=edit.connection
    @contextmanager
    def fail(root,**kwargs):
        with original(root,**kwargs) as con:
            yield con
            if kwargs.get('write'):raise OSError('Disk unavailable before commit')
    with monkeypatch.context() as patch:
        patch.setattr(edit,'connection',fail)
        with pytest.raises(edit.CorrectionError) as error:edit.apply(root,meta['id'],actor={'id':7,'name':'Автор'},**data)
        assert error.value.status==503
    assert edit.snapshot(root,meta['id'])['revision']==0 and store.load_rows(root,meta['id'])==rows
    assert not edit.apply(root,meta['id'],actor={'id':7,'name':'Автор'},**data)[1]


@pytest.mark.parametrize('changes',[{'qty':True},{'qty':'NaN'},{'total':'Infinity'},{'total':'1.005'},
    {'qty':'0.0000001'},{'unit_price':'1e40'},{'total':'1e999999999'},{'qty':'1e-999999999'},
    {'position_id':'new'},{'type':'admin'},{'name':''},{'qty':{}},{}])
def test_invalid_changes_do_not_publish(context,changes):
    root,meta,*_=context
    with pytest.raises(edit.CorrectionError) as error:apply(context,changes=changes)
    assert error.value.status==400 and edit.snapshot(root,meta['id'])['revision']==0


def test_zero_with_large_exponent_is_bounded_and_remains_explicit(context):
    root,meta,*_=context
    apply(context,changes={'total':'0e999999999'})
    row=edit.snapshot(root,meta['id'])['rows'][0]
    assert row['total']=='0' and row['total_kopecks']==0


def test_review_opens_the_original_pdf_page_and_offers_bounded_preview(context):
    import hashlib
    import pymupdf
    root,_,rows,_,client=context
    eid='e'*16
    folder=root/eid;folder.mkdir()
    source=folder/'two-pages.pdf'
    with pymupdf.open() as document:
        for number in range(2):document.new_page().insert_text((40,60),'Page '+str(number+1))
        document.save(source)
    raw=source.read_bytes()
    store.publish(root,{'id':eid,'title':'PDF QA','original_filename':source.name,'source_path':str(source),
        'source_sha256':hashlib.sha256(raw).hexdigest(),'row_count':1},
        [dict(rows[0],sheet='PDF, стр. 2',position_id='pdf:ocr:2:row1')])
    review=client.get('/estimates/'+eid+'/review',query_string={'position_id':'pdf:ocr:2:row1'})
    assert review.status_code==200 and '/source-preview?page=2' in review.get_data(as_text=True)
    preview=client.get('/estimates/'+eid+'/source-preview?page=2&zoom=1')
    html=preview.get_data(as_text=True)
    assert preview.status_code==200 and 'data:image/png;base64,' in html and 'Страница 2 исходного PDF' in html
    assert 'actual-size' in html and "img-src data:" in preview.headers['Content-Security-Policy']
    assert client.get('/estimates/'+eid+'/source-preview?page=251').status_code==400
    assert source.read_bytes()==raw


def test_unknown_numbers_remain_unknown_and_sum_is_not_invented(context):
    root,meta,*_=context
    apply(context,changes={'qty':'','unit_price':None,'total':None})
    current=edit.snapshot(root,meta['id'])
    assert current['rows'][0]['qty'] is None and current['rows'][0]['total_kopecks'] is None
    assert current['meta']['reconciliation']['missing_total_count']==1
    assert current['meta']['reconciliation']['unallocated_total'] is None


def test_corrected_position_never_uses_legacy_market_and_other_rows_keep_it(context):
    root,meta,rows,_,_=context
    legacy=store.report_frame(rows)
    apply(context,changes={'type':'material'})
    current=store.report_frame(store.load_rows(root,meta['id']))
    matched=match_market_rows(current,legacy)
    assert matched[0] is None and matched[1] is not None
    legacy.loc[0,'estimate_version']=''
    assert match_market_rows(current,legacy)[0] is None
    fresh=current.iloc[[0]].copy()
    assert match_market_rows(current,fresh)[0] is not None


def test_correction_invalidates_an_already_leased_market_position(context):
    root,meta,rows,_,_=context
    market.enqueue(meta['id'],city='Ярославль',operation_id='d'*32)
    job=queue.claim_job('server',mode='web',include_uploaded=True)
    apply(context)
    with pytest.raises(market.MarketError,match='изменилась'):
        market.import_context(job['tender_id'],job['payload'])


def test_legacy_source_change_or_corrupt_history_fails_closed(context):
    root,meta,rows,_,_=context
    apply(context)
    with sqlite3.connect(root/'estimates.sqlite3') as con:
        con.execute('UPDATE uploaded_estimates SET rows_json=? WHERE id=?',(json.dumps([dict(rows[0],qty=5),rows[1]]),meta['id']))
    with pytest.raises(edit.CorrectionError,match='Исходная смета изменилась'):store.load_rows(root,meta['id'])
    with sqlite3.connect(root/'estimates.sqlite3') as con:con.execute('UPDATE uploaded_estimates SET rows_json=? WHERE id=?',(json.dumps(rows),meta['id']))
    with sqlite3.connect(root/'.corrections.sqlite3') as con:con.execute("UPDATE estimate_revisions SET event_json='{}'")
    with pytest.raises(edit.CorrectionError) as error:store.load_rows(root,meta['id'])
    assert error.value.status==503


def test_api_rejects_forged_author_and_recovers_exact_save(context):
    root,meta,rows,_,client=context
    path='/api/estimates/'+meta['id']+'/corrections'
    data=request_data(root,meta,rows)
    assert client.post(path,json=dict(data,actor={'id':1})).status_code==400
    result=client.post(path,json=data)
    assert result.status_code==200 and result.json['revision']==1
    assert client.post(path,json=data).json['duplicate']
    saved=client.get(path+'?operation_id='+data['operation_id'])
    assert saved.json['version']==result.json['version']
    assert client.get(path+'?operation_id='+'f'*32).status_code==404
    history=client.get(path).json['history']
    assert history[0]['actor']=={'id':7,'name':'Автор проверки'}
    assert 'source_path' not in saved.get_data(as_text=True)
    assert saved.headers['Cache-Control']=='private, no-store'


@pytest.mark.parametrize('role',['guest','customer','accountant','worker','unrecognized'])
def test_roles_without_autobot_access_cannot_edit(context,monkeypatch,role):
    root,meta,rows,_,client=context
    monkeypatch.setattr(crm_actor,'resolve',lambda _:edit.actor_from_user({'id':9,'name':'Denied','roles':[role]}))
    assert client.post('/api/estimates/'+meta['id']+'/corrections',json=request_data(root,meta,rows)).status_code==403
    assert edit.history(root,meta['id'])==[]


def test_review_escapes_content_and_keeps_original_values(context):
    root,meta,rows,_,client=context
    apply(context,changes={'name':'<script>alert(1)</script>'})
    response=client.get('/estimates/'+meta['id']+'/review',query_string={'position_id':rows[0]['position_id']})
    html=response.get_data(as_text=True)
    assert response.status_code==200 and '<script>alert(1)</script>' not in html
    assert 'Устройство покрытия' in html and 'Первое распознавание' in html
    assert '/source-preview' in html and str(root) not in html


def test_crm_session_is_forwarded_only_to_configured_origin_without_redirect(monkeypatch):
    monkeypatch.setenv('PMBI_CRM_URL','http://crm:8080')
    seen=[]
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,limit):
            assert limit==65537
            return json.dumps({'user':{'id':5,'name':'Foreman','roles':['foreman'],'private':'not retained'}}).encode()
    class Opener:
        def open(self,request,timeout):
            seen.append(request);assert timeout==8;return Response()
    def opener(*handlers):
        assert any(isinstance(h,crm_actor.NoRedirect) for h in handlers)
        return Opener()
    monkeypatch.setattr(crm_actor.urllib.request,'build_opener',opener)
    actor=crm_actor.resolve({'Cookie':'session=qa','X-User-Id':'1','Authorization':'Bearer qa'})
    assert actor=={'id':5,'name':'Foreman'}
    assert seen[0].full_url=='http://crm:8080/api/auth/me'
    assert set(dict(seen[0].header_items()))=={'Cookie','Authorization','Accept'}
    assert crm_actor.NoRedirect().redirect_request(None,None,302,'',{},'https://elsewhere.example') is None


def test_crm_missing_session_or_unavailable_never_uses_a_service_account(monkeypatch):
    monkeypatch.setenv('PMBI_CRM_URL','http://crm:8080')
    monkeypatch.setenv('PMBI_CRM_LOGIN','unused')
    with pytest.raises(edit.CorrectionError) as error:crm_actor.resolve({})
    assert error.value.status==401
    class Opener:
        def open(self,*args,**kwargs):raise urllib.error.HTTPError('http://crm:8080',302,'Moved',{},None)
    monkeypatch.setattr(crm_actor.urllib.request,'build_opener',lambda *args:Opener())
    with pytest.raises(edit.CorrectionError) as error:crm_actor.resolve({'Cookie':'qa'})
    assert error.value.status==503


def test_corrected_import_preserves_zero_totals_and_stable_source_key(context):
    root,meta,rows,_,_=context
    before=web._build_estimate_crm_import_payload(meta['id'])
    apply(context,changes={'name':'НГ','type':'service','unit_price':'0','total':'0'})
    payload=web._build_estimate_crm_import_payload(meta['id'])
    item=payload['items'][0]
    assert len(payload['items'])==2 and item['title']=='НГ' and item['type']=='service'
    assert item['planned_qty']==.2 and item['unit']=='100 м2' and item['planned_price']==item['planned_total']==0
    assert item['source_item_key']==before['items'][0]['source_item_key']
    assert payload['source']['metadata']['normalization']['version']==edit.snapshot(root,meta['id'])['version']


def test_corrected_incomplete_import_is_rejected_instead_of_guessing(context):
    root,meta,rows,_,client=context
    apply(context,changes={'qty':None})
    with pytest.raises(edit.CorrectionError) as error:web._build_estimate_crm_import_payload(meta['id'])
    assert error.value.status==422
    with web.app.test_request_context(): token=web._issue_estimate_import_capability(meta['id'])
    response=client.get('/api/estimates/'+meta['id']+'/crm-import-payload',headers={'X-AutoBot-Estimate-Capability':token})
    assert response.status_code==422 and 'items' not in response.json


def test_next_card_catalogue_and_excel_read_the_saved_revision(context):
    import io
    root,meta,rows,_,client=context
    apply(context)
    html=client.get('/estimates/'+meta['id']).get_data(as_text=True)
    assert 'редакция 1' in html and '25,01 ₽' in html and 'Исправлена вручную' in html
    catalogue=web._read_estimates_index()
    assert catalogue[0]['total_sum_kopecks']==4502
    response=client.get('/estimates/'+meta['id']+'/download.xlsx')
    assert response.status_code==200
    sheets=pd.read_excel(io.BytesIO(response.data),sheet_name=None)
    assert any('25.01' in str(frame.values) for frame in sheets.values())


def test_invalid_document_id_preserves_not_found_response(context):
    *_,client=context
    assert client.get('/estimates/ZZZ').status_code==404
    assert client.get('/estimates/ZZZ/download.xlsx').status_code==404


def test_original_is_downloadable_if_correction_history_is_corrupt(context):
    root,meta,_,source,client=context
    apply(context)
    with sqlite3.connect(root/'.corrections.sqlite3') as con:con.execute("UPDATE estimate_revisions SET event_json='{}'")
    assert client.get('/api/estimates/'+meta['id']+'/corrections').status_code==503
    response=client.get('/estimates/'+meta['id']+'/original')
    assert response.status_code==200 and response.data==source.read_bytes()
    response.close()


def test_changed_original_file_cannot_receive_new_corrections(context):
    root,meta,rows,source,_=context
    source.write_bytes(b'changed after parsing')
    with pytest.raises(edit.CorrectionError,match='файл изменился'):apply(context)
    assert edit.history(root,meta['id'])==[]


def test_legacy_rows_without_ids_can_be_corrected_and_history_paginates(context):
    root,meta,rows,_,_=context
    eid='d'*16
    legacy=[dict(rows[0])]
    legacy[0].pop('position_id')
    store.write_json(root/eid/'meta.json',{'id':eid,'title':'Legacy','row_count':1})
    store.write_json(root/eid/'rows.json',legacy)
    for number in range(21):
        value=edit.snapshot(root,eid)
        edit.apply(root,eid,position_id=value['rows'][0]['position_id'],changes={'name':'Название '+str(number)},
            expected_version=value['version'],operation_id=f'{number:032x}',reason='QA',actor={'id':7,'name':'Автор'})
    current=edit.snapshot(root,eid)
    assert current['rows'][0]['position_id']=='upload:'+eid+':1' and current['revision']==21
    assert len(edit.history(root,eid))==20 and len(edit.history(root,eid,before=2))==1
    assert json.loads((root/eid/'rows.json').read_text(encoding='utf-8'))==legacy
