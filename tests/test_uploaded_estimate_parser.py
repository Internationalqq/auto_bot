from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from autobot import estimate_excel_analysis as analysis, estimate_parse_worker as parser, web_ui
from autobot.market_analytics import COL_ITEM, COL_NAME, COL_UNIT, COL_QTY, COL_SUM, COL_UNIT_PRICE


def workbook(path):
    pd.DataFrame([{COL_ITEM:'1',COL_NAME:'Устройство покрытия',COL_UNIT:'100 м2',COL_QTY:.2,
                   COL_UNIT_PRICE:100.05,COL_SUM:20.01}]).to_excel(path,index=False)
    return path


def test_real_uploaded_excel_process_preserves_source_scale_and_reports_progress(tmp_path):
    path=workbook(tmp_path/'estimate.xlsx')
    before=path.read_bytes()
    events=[]
    result=parser.run_uploaded_parser(path,progress_cb=lambda *event:events.append(event))
    row=result['rows'][0]
    assert row['unit']=='100 м2' and row['qty']==.2 and row['total']==20.01
    assert result['sources'][0]['sha256']==hashlib.sha256(before).hexdigest()
    assert path.read_bytes()==before and events
    assert all(0<=event[0]<=96 for event in events)


def test_plain_named_excel_is_not_misread_as_positional_lsr(tmp_path):
    path=tmp_path/'plain.xlsx'
    table=pd.DataFrame([{'Наименование':'Устройство покрытия с НДС','Ед. изм.':'100 м2','Количество':.2,
                         'Цена, руб':100.05,'Сумма, руб':20.01}])
    with pd.ExcelWriter(path) as book:
        table.to_excel(book,sheet_name='Первая смета',index=False)
        table.to_excel(book,sheet_name='Вторая смета',index=False)
    result=parser.run_uploaded_parser(path)
    assert len(result['rows'])==2
    for row in result['rows']:
        assert row['unit']=='100 м2' and row['qty']==.2
        assert row['unit_price']==100.05 and row['total']==20.01
    assert len({row['position_id'] for row in result['rows']})==2


def test_named_reader_does_not_accept_ambiguous_price_headers_or_partial_workbook(tmp_path):
    headers=['Наименование','Ед. изм.','Количество','Цена','Цена, руб','Сумма, руб']
    assert analysis._labeled_columns(headers) is None
    path=tmp_path/'mixed.xlsx'
    with pd.ExcelWriter(path) as book:
        pd.DataFrame([{'Наименование':'Работа','Ед. изм.':'м2','Количество':2,'Цена':100,'Сумма, руб':200}]).to_excel(book,sheet_name='Таблица',index=False)
        pd.DataFrame([['Нестандартная смета, которую нельзя потерять']]).to_excel(book,sheet_name='Другой формат',index=False,header=False)
    with pytest.raises(parser.EstimateParseRejected,match='Не распознан формат листов'):
        parser.run_uploaded_parser(path)


def test_named_reader_keeps_short_material_name_and_skips_repeated_header(tmp_path):
    path=tmp_path/'repeated-header.xlsx'
    headers=['№','Наименование','Ед. изм.','Количество','Цена','Сумма, руб']
    pd.DataFrame([headers,[11,'Люк','шт',2,100,200],headers,[15,'Кран','шт',1,100,100],
                  ['', 'Итого','','','',300]]).to_excel(path,index=False,header=False)
    result=parser.run_uploaded_parser(path)
    assert [row['name'] for row in result['rows']]==['Люк','Кран']
    assert [row['item_no'] for row in result['rows']]==['11','15']
    assert sum(row['total'] for row in result['rows'])==300


def test_uploaded_timeout_cleans_scratch_and_next_process_succeeds(tmp_path, monkeypatch):
    path=workbook(tmp_path/'estimate.xlsx')
    scratch=tmp_path/'scratch';scratch.mkdir()
    monkeypatch.setattr(parser.tempfile,'tempdir',str(scratch))
    with pytest.raises(parser.EstimateParseRejected,match='Время разбора'):
        parser.run_uploaded_parser(path,limits=replace(parser.ParseLimits(),seconds=.001),progress_cb=lambda *args:None)
    assert not list(scratch.iterdir())
    assert len(parser.run_uploaded_parser(path)['rows'])==1
    assert not list(scratch.iterdir())


@pytest.mark.parametrize('limits',[
    replace(parser.ParseLimits(),file_bytes=10),
    replace(parser.ParseLimits(),sheet_rows=1),
    replace(parser.ParseLimits(),columns=2),
    replace(parser.ParseLimits(),cells=2),
    replace(parser.ParseLimits(),rows=0),
])
def test_uploaded_reader_enforces_shared_limits(tmp_path,limits):
    path=workbook(tmp_path/'estimate.xlsx')
    with pytest.raises(parser.EstimateParseRejected):
        parser.parse_uploaded_file(path,limits)


def test_uploaded_pdf_page_limit_is_checked_before_ocr(tmp_path):
    import fitz
    path=tmp_path/'two-pages.pdf'
    with fitz.open() as document:
        document.new_page();document.new_page();document.save(path)
    with pytest.raises(parser.EstimateParseRejected,match='слишком много страниц'):
        parser.parse_uploaded_file(path,replace(parser.ParseLimits(),pdf_pages=1))


def test_uploaded_coordinates_unknown_values_and_diagnostics_survive_serialization(tmp_path,monkeypatch):
    path=workbook(tmp_path/'estimate.xlsx')
    rows=[analysis.EstimateRow(idx=1,name='Одинаковая работа',unit='',qty=None,total=20.01,
          basis_code='ГЭСН01-01-001',sheet='PDF, стр. 1',position_id='pdf:ocr:1:100:200'),
          analysis.EstimateRow(idx=2,name='Одинаковая работа',unit='',qty=None,total=20.01,
          basis_code='ГЭСН01-01-001',sheet='PDF, стр. 1',position_id='pdf:ocr:1:500:200')]
    diagnostics={'declared_total':40.02,'unallocated_total':0}
    monkeypatch.setattr(analysis,'load_estimate_session',lambda *args,**kwargs:
                        analysis.EstimateSession(path,rows,[],diagnostics))
    result=parser.parse_uploaded_file(path,parser.ParseLimits())
    saved=[web_ui._estimate_row_to_dict(SimpleNamespace(**row)) for row in result['rows']]
    assert [row['position_id'] for row in saved]==[row.position_id for row in rows]
    assert all(row['qty'] is None and row['unit']=='' for row in saved)
    assert result['diagnostics']==diagnostics
    assert web_ui._estimate_rows_to_report_df(saved)['position_id'].nunique()==2


def test_uploaded_source_change_during_reader_is_rejected(tmp_path,monkeypatch):
    path=workbook(tmp_path/'estimate.xlsx')
    def changed(*args,**kwargs):
        path.write_bytes(b'changed source')
        return analysis.EstimateSession(path,[analysis.EstimateRow(idx=1,name='Работа')],[])
    monkeypatch.setattr(analysis,'load_estimate_session',changed)
    with pytest.raises(parser.EstimateParseRejected,match='изменились'):
        parser.parse_uploaded_file(path,parser.ParseLimits())


def upload_job(tmp_path,monkeypatch):
    estimate_id,job_id='d'*16,'c'*16
    root=tmp_path/'data/user_estimates'
    (root/estimate_id).mkdir(parents=True)
    monkeypatch.setattr(web_ui,'REPO_ROOT',tmp_path)
    monkeypatch.setattr(web_ui,'USER_ESTIMATES_DIR',root)
    monkeypatch.setattr(web_ui,'USER_ESTIMATES_INDEX',root/'index.json')
    monkeypatch.setattr(web_ui,'ESTIMATE_UPLOAD_JOBS_DIR',root/'.upload_jobs')
    monkeypatch.setattr(web_ui,'estimate_upload_jobs',{job_id:{'job_id':job_id,'running':True,'progress':26,'log_lines':[]}})
    monkeypatch.setattr(web_ui,'estimate_upload_workers',{job_id})
    return root,estimate_id,job_id


def test_failed_uploaded_parser_does_not_publish_a_successful_card(tmp_path,monkeypatch):
    root,estimate_id,job_id=upload_job(tmp_path,monkeypatch)
    path=root/estimate_id/'broken.xlsx';path.write_bytes(b'broken')
    web_ui._run_estimate_upload_worker(job_id,estimate_id=estimate_id,title_raw='Проверка',original_name=path.name,src_path=path)
    state=web_ui.estimate_upload_jobs[job_id]
    assert not state['ok'] and not state['running'] and state['error']
    assert path.read_bytes()==b'broken'
    assert not (root/'index.json').exists() and not (path.parent/'meta.json').exists()
    assert job_id not in web_ui.estimate_upload_workers


def test_upload_parent_checks_source_before_publishing_and_persists_version(tmp_path,monkeypatch):
    root,estimate_id,job_id=upload_job(tmp_path,monkeypatch)
    path=workbook(root/estimate_id/'estimate.xlsx')
    row=analysis.EstimateRow(idx=1,name='Работа',qty=None,total=20.01,position_id='pdf:ocr:1:100:200')
    parsed={'rows':[asdict(row)],'diagnostics':{},'sources':parser.snapshot([path])}
    monkeypatch.setattr(parser,'run_uploaded_parser',lambda *args,**kwargs:parsed)
    web_ui._run_estimate_upload_worker(job_id,estimate_id=estimate_id,title_raw='Проверка',original_name=path.name,src_path=path)
    stored=web_ui._load_estimate_rows(estimate_id)
    meta=web_ui._load_estimate_meta(estimate_id)
    assert stored[0]['position_id']==row.position_id
    assert stored[0]['estimate_version']==meta['source_sha256']==parsed['sources'][0]['sha256']
    before=stored
    path.write_bytes(b'changed after parsing')
    web_ui.estimate_upload_jobs[job_id].update(running=True,ok=False)
    web_ui._estimate_upload_persist_locked(web_ui.estimate_upload_jobs[job_id],strict=True)
    web_ui._run_estimate_upload_worker(job_id,estimate_id=estimate_id,title_raw='Проверка',original_name=path.name,src_path=path)
    assert not web_ui.estimate_upload_jobs[job_id]['ok']
    assert web_ui._load_estimate_rows(estimate_id)==before
