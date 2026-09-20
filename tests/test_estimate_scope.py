import json
import pandas as pd
import pytest
from autobot.estimate_scope import RESOURCES,PARENT,expand_resources,financial_scope
from autobot.market_analytics import COL_NAME,COL_QTY,COL_SUM,COL_UNIT,COL_UNIT_PRICE


def primary():
    return {'position_id':'pdf:native:1:10','Файл ЛСР':'a.pdf','№ п/п':'10',
        COL_NAME:'Укладка геотекстиля',COL_QTY:2,COL_UNIT:'1000 м2',COL_SUM:90000,COL_UNIT_PRICE:45000,
        RESOURCES:json.dumps([{'position_id':'pdf:native:1:10.1','parent_position_id':'pdf:native:1:10',
            'position':'10.1','page':1,'code':'ФСБЦ-01-0053','name':'Геотекстиль иглопробивной 200 г/м2',
            'qty':2200,'unit':'м2','total':74272,'unit_price':33.76}],ensure_ascii=False)}


def test_resources_are_searchable_but_budget_is_counted_once():
    frame=expand_resources(pd.DataFrame([primary()]))
    assert len(frame)==2 and frame.iloc[1][PARENT]=='pdf:native:1:10'
    assert frame.iloc[1]['№ п/п']=='10.1' and frame.iloc[1][COL_QTY]==2200
    financial=financial_scope(frame)
    assert list(financial[COL_SUM])==[15728,74272]
    assert financial[COL_SUM].sum()==90000
    assert len(expand_resources(frame))==2
    assert frame.iloc[0][COL_SUM]==90000


def test_incomplete_resource_amount_never_creates_partial_budget():
    row=primary();resources=json.loads(row[RESOURCES]);resources[0]['total']=None
    row[RESOURCES]=json.dumps(resources)
    financial=financial_scope(expand_resources(pd.DataFrame([row])))
    assert len(financial)==1 and financial.iloc[0][COL_SUM]==90000


def test_resource_from_another_parent_cannot_be_published():
    row=primary();row['position_id']='different'
    with pytest.raises(ValueError):expand_resources(pd.DataFrame([row]))


def test_resource_quotes_survive_report_reopening_and_input_identity(tmp_path,monkeypatch):
    from autobot import real_market_scraper as market
    from autobot.market_contract import position_identity
    path=tmp_path/'estimate.xlsx';pd.DataFrame([primary()]).to_excel(path,index=False)
    child=expand_resources(pd.read_excel(path)).iloc[1]
    monkeypatch.setattr(market,'REPORTS_DIR',tmp_path)
    monkeypatch.setattr(market,'estimate_path_for_tender',lambda _:path)
    monkeypatch.setattr(market,'load_tender_metadata',lambda:{'12345678':{'region':'Ярославль'}})
    context=market._agent_import_context('12345678',{'name':child[COL_NAME],'position_key':position_identity(child)})
    assert context[3][COL_QTY]==2200 and context[3][COL_SUM]==74272


def test_source_resource_scope_cannot_be_overwritten_by_saved_market_fields():
    from autobot.market_contract import merge_market_frames
    frame=expand_resources(pd.DataFrame([primary()]))
    market=frame.copy();market['has_resources']=False;market[PARENT]='forged-parent'
    merged=merge_market_frames(frame,market)
    assert merged.iloc[0]['has_resources'] and merged.iloc[0][PARENT]==''
    assert merged.iloc[1][PARENT]=='pdf:native:1:10'


def test_summary_export_keeps_resources_and_a_budget_that_can_be_summed(tmp_path,monkeypatch):
    from autobot import merge_estimate_market as merge
    monkeypatch.setattr(merge,'REPORTS_DIR',tmp_path)
    monkeypatch.setattr(merge,'load_tender_metadata',lambda:{})
    tid='12345678';name=f'ОТЧЕТ_ПО_СМЕТАМ_{tid}'
    original=pd.DataFrame([primary()]);original.to_excel(tmp_path/(name+'.xlsx'),index=False)
    expand_resources(original).to_excel(tmp_path/('РЫНОК_ИСТОЧНИКИ_'+name+'.xlsx'),index=False)
    result=pd.read_excel(merge.merge_estimate_and_market(tid))
    assert len(result)==2 and result.iloc[0][COL_SUM]==90000
    assert result['Бюджет без повторного учёта ресурсов, руб'].sum()==90000
