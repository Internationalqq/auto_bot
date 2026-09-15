import json
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from autobot import web_ui as web, crm_actor, tender_review as review, tender_detail as detail
from autobot import tender_corrections as edit, real_market_scraper as market, tender_economics_source as economics
from autobot import estimate_publication as publication, estimate_parse_worker as worker
from autobot.market_contract import position_identity
from test_tender_corrections import context, data, save, ACTOR, TID


@pytest.fixture
def browser(context, monkeypatch):
    paths, _, tender = context
    monkeypatch.setattr(web,'DATA_DIR',paths['root'])
    monkeypatch.setattr(web,'REPORTS_DIR',paths['reports'])
    monkeypatch.setattr(detail,'REPORTS_DIR',paths['reports'])
    monkeypatch.setattr(market,'REPORTS_DIR',paths['reports'])
    metadata = {TID:{'title':tender.title,'price_rub':tender.price_rub,'region':'Ярославль'}}
    monkeypatch.setattr(web,'load_tender_metadata',lambda: metadata)
    monkeypatch.setattr(market,'load_tender_metadata',lambda: metadata)
    monkeypatch.setattr(web,'_tenders_items',lambda: ([],{}))
    monkeypatch.setattr(web,'list_tender_source_files',lambda _: {'files':[],'count':0})
    monkeypatch.setattr(crm_actor,'resolve',lambda _: ACTOR)
    return web.app.test_client()


def api(): return '/api/tender/'+TID+'/corrections'
def page(): return '/tenders/'+TID+'/review'


def test_api_author_identity_exact_repeat_and_tamper(context, browser):
    body = data(context)
    response = browser.post(api(),json=body)
    assert response.status_code == 200 and response.json['revision'] == 1
    again = browser.post(api(),json=body)
    assert again.json['duplicate'] and again.json['version'] == response.json['version']
    history = browser.get(api()).json
    assert history['history'][0]['actor'] == ACTOR and history['revision'] == 1
    assert 'private' in browser.get(api()).headers['Cache-Control']
    assert browser.post(api(),json=dict(body,actor={'id':1,'name':'Подмена'})).status_code == 400
    assert browser.get(api(),query_string={'operation_id':body['operation_id']}).json['revision'] == 1
    stale = browser.post(api(),json=dict(body,operation_id='d'*32))
    assert stale.status_code == 409


def test_denied_actor_cannot_read_write_or_preview(context, browser, monkeypatch):
    def denied(_):
        raise edit.CorrectionError('Нет доступа',403)
    monkeypatch.setattr(crm_actor,'resolve',denied)
    body = data(context)
    for url in (api(),page(),'/tenders/'+TID+'/review-source'):
        response = browser.get(url,query_string={'position_id':body['position_id']})
        assert response.status_code == 403 and 'Монтаж' not in response.get_data(as_text=True)
    assert browser.post(api(),json=body).status_code == 403
    assert edit.snapshot(context[0]['reports'],TID)['revision'] == 0


def test_page_uses_same_editor_original_download_and_card_link(context,browser):
    body = data(context)
    response = browser.get(page(),query_string={'position_id':body['position_id']})
    assert response.status_code == 200
    soup = BeautifulSoup(response.data,'html.parser')
    config = json.loads(soup.select_one('#correctionConfig').text)
    assert config['kind']=='tender' and config['estimateId']==TID and 'type' not in config['fields']
    assert soup.select_one('.review-back')['href']=='/tenders/'+TID
    original = soup.select_one('.review-source a')['href']
    response = browser.get(original)
    assert response.data == context[1].read_bytes()
    response.close()
    preview = browser.get(soup.iframe['src'])
    assert preview.status_code == 200 and 'Блок оконный' in preview.get_data(as_text=True)
    card = browser.get('/tenders/'+TID)
    assert card.status_code == 200 and 'Сверить с исходником' in card.get_data(as_text=True)
    card_soup = BeautifulSoup(card.data,'html.parser')
    link = card_soup.find('a',string='Сверить с исходником')['href']
    assert parse_qs(urlparse(link).query)['position_id'][0] == body['position_id']


def test_source_changed_and_legacy_review_show_actionable_error(context,browser):
    body = data(context)
    context[1].write_bytes(b'changed')
    response = browser.get(page(),query_string={'position_id':body['position_id']})
    assert response.status_code==503 and 'Открыть документы' in response.get_data(as_text=True)
    manifest = context[0]['reports']/publication.status_path(context[0]['reports'],TID).name.replace('PARSE_RUN_','ESTIMATE_PARSE_')
    value = json.loads(manifest.read_text(encoding='utf-8'));value.pop('parse_sources')
    manifest.write_text(json.dumps(value),encoding='utf-8')
    response = browser.get(page(),query_string={'position_id':body['position_id']})
    assert response.status_code==409 and 'Сначала выполните разбор' in response.get_data(as_text=True)


def test_current_import_and_economics_change_without_fabricating_missing_values(context,browser):
    paths, _, tender = context
    before_import = web._tender_estimate_materials_for_crm(TID)
    before = economics.build_source(TID,{'title':tender.title},paths['reports'],detail.build_tender_detail)
    save(context,changes={'name':'ПВХ','basis_code':'Исправленный шифр','total':'0','unit_price':'0','qty':'0.2'})
    current_import = web._tender_estimate_materials_for_crm(TID)
    assert current_import[0]['planned_total']==0 and current_import[0]['planned_price']==0
    assert current_import[0]['source_item_key']==before_import[0]['source_item_key']
    after = economics.build_source(TID,{'title':tender.title},paths['reports'],detail.build_tender_detail)
    assert after['version']!=before['version'] and after['known_market_kopecks'] is None
    save(context,changes={'qty':''},operation_id='c'*32)
    with pytest.raises(edit.CorrectionError) as error:
        web._tender_estimate_materials_for_crm(TID)
    assert error.value.status==422
    result = browser.post('/api/export-to-crm',json={'tender_id':TID})
    assert result.status_code==422 and 'уточните' in result.json['message']


def test_old_accepted_market_delivery_cannot_overwrite_correction(context,browser,monkeypatch):
    paths, _, _ = context
    value = edit.snapshot(paths['reports'],TID)
    row = value['frame'].iloc[0]
    payload = {'name':row[edit.COLUMNS['name']],'position_key':position_identity(row)}
    monkeypatch.setattr(market,'_research_row_market',lambda *a,**kw: ([],''))
    _,prepared = market.prepare_builtin_market_result(TID,payload)
    save(context)
    with pytest.raises(ValueError):
        market.publish_agent_market_result(TID,payload,prepared)
    assert edit.snapshot(paths['reports'],TID)['rows'][0]['total']=='25.01'
    assert not market.output_path_for_tender(TID).exists()


def test_extracted_source_and_pdf_navigation_keep_position(context,browser,monkeypatch):
    paths, source, tender = context
    extracted = paths['extracted']/TID/'archive'/'ЛСР.xlsx'
    extracted.parent.mkdir(parents=True);extracted.write_bytes(source.read_bytes())
    publication.parse_and_publish(tender,[extracted],[],[source],paths)
    body = data(context)
    response = browser.get(page(),query_string={'position_id':body['position_id']})
    soup = BeautifulSoup(response.data,'html.parser')
    response = browser.get(soup.select_one('.review-source a')['href'])
    assert response.data==extracted.read_bytes();response.close()
    save(context)
    # Render the shared PDF navigation without an OCR dependency; the real PDF child is checked separately.
    from flask import render_template
    with web.app.test_request_context('/'):
        html = render_template('estimate_source_preview.html',preview={'kind':'pdf-page','pages':3,'page':2,
            'image':'data:image/png;base64,AA==','width':100,'height':100},source_position_id='pdf:2:3',
            original_url='/original',actual_size=False,title='Смета')
    soup = BeautifulSoup(html,'html.parser')
    assert soup.select_one('input[name="position_id"]')['value']=='pdf:2:3'
    for link in soup.select('.review-pdf-nav a'):
        assert parse_qs(urlparse(link['href']).query)['position_id']==['pdf:2:3']
