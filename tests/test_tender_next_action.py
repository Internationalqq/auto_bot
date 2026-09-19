"""User actions follow actual data and preserve recoverable source failures."""
import json
from html.parser import HTMLParser

import pandas as pd
import pytest

from autobot import tender_detail, web_ui
from autobot.market_analytics import COL_NAME, COL_QTY, COL_SUM, COL_UNIT, COL_UNIT_PRICE
from autobot.workflow_overview import build_workflow_payload

TID = '12345678901'


@pytest.mark.parametrize('changes,key', [
    ({'has_downloads': False}, 'download'),
    ({'download_blocked': True, 'parse_blocked': True}, 'download'),
    ({'parse_blocked': True}, 'extract'),
    ({'archive_failed': True}, 'extract'),
    ({'total_positions': 0}, 'extract'),
    ({'needs_review': True}, 'positions'),
    ({}, 'market'),
    ({'verified': 2}, 'positions'),
])
def test_next_action_precedence(changes, key):
    state = dict(has_downloads=True, download_blocked=False, parse_blocked=False, archive_failed=False,
                 total_positions=2, verified=1, needs_review=False)
    result = tender_detail._primary_action(**(state | changes))
    assert result['key'] == key
    assert result['label'] and result['detail']


class ActionElements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def test_real_detail_promotes_download_parse_review_and_prices(tmp_path, monkeypatch):
    monkeypatch.setattr(tender_detail, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(tender_detail, 'latest_parser_health', lambda _: {})
    rows = [{COL_NAME: 'Поставка бетона', COL_UNIT: 'м3', COL_QTY: 2, COL_SUM: 200, COL_UNIT_PRICE: 100}]
    report = tmp_path / f'ОТЧЕТ_ПО_СМЕТАМ_{TID}.xlsx'
    pd.DataFrame(rows).to_excel(report, index=False)
    metadata = {'title': 'Длинное название <script>alert(1)</script>', 'tender_id': TID}
    workflow = {'has_downloads': True}

    def render(expected, **updates):
        result = tender_detail.build_tender_detail(TID, metadata, workflow | updates)
        assert result['primary_action']['key'] == expected
        result.update(active_tab='overview', documents={'count': 0, 'items': []})
        with web_ui.app.test_request_context('/tenders/' + TID):
            html = web_ui.render_template('tender_detail.html', tender=result)
        panel = html.split('id="nextAction"', 1)[1].split('</section>', 1)[0]
        parser = ActionElements()
        parser.feed(panel)
        primary = [(tag, attrs) for tag, attrs in parser.elements if 'primary' in attrs.get('class', '').split()]
        assert len(primary) == 1
        tag, attrs = primary[0]
        if expected == 'market':
            assert attrs['id'] == 'runMarketBtn'
        elif expected == 'positions':
            assert tag == 'a' and attrs['href'] == '#positions'
        else:
            assert ('data-refresh-documents' if expected == 'download' else 'data-rebuild-report') in attrs
        assert html.count('id="runMarketBtn"') == 1  # Manual search remains in More.
        assert '<script>alert(1)</script>' not in html
        assert 'data-tender-economics' in html and 'hidden' in html
        return result

    render('download', has_downloads=False)
    render('market')
    parse = tmp_path / f'PARSE_RUN_{TID}.json'
    parse.write_text(json.dumps({'schema_version': 1, 'tender_id': TID, 'state': 'failed', 'error': 'bad source'}))
    render('extract')
    parse.write_text(json.dumps({'schema_version': 1, 'tender_id': TID, 'state': 'complete', 'warnings': ['Check unit']}))
    render('positions')
    parse.unlink()
    rows[0][COL_QTY] = None
    pd.DataFrame(rows).to_excel(report, index=False)
    render('positions')


def test_catalogue_reopens_failed_jobs_even_when_old_reports_exist(tmp_path):
    reports = tmp_path / 'reports'
    reports.mkdir()
    downloads = tmp_path / 'downloads' / TID
    downloads.mkdir(parents=True)
    (downloads / 'doc.xlsx').write_bytes(b'original')
    for name in [f'ОТЧЕТ_ПО_СМЕТАМ_{TID}.xlsx', f'РЫНОК_ИСТОЧНИКИ_ОТЧЕТ_ПО_СМЕТАМ_{TID}.xlsx', f'СВОДКА_РЫНОК_{TID}.xlsx']:
        (reports / name).write_bytes(b'old')
    site = tmp_path / 'reports_site' / TID
    site.mkdir(parents=True)
    (site / 'index.html').write_text('old')
    (tmp_path / 'tenders.json').write_text(json.dumps([{'tender_id': TID}]))

    def state():
        return build_workflow_payload(data_dir=tmp_path, include_storage=False)['tenders'][0]

    assert state()['next_action'] == 'review'
    parse = reports / f'PARSE_RUN_{TID}.json'
    parse.write_text(json.dumps({'schema_version': 1, 'tender_id': TID, 'state': 'failed', 'error': 'bad source'}))
    assert state()['next_action'] == 'extract_estimate'
    assert state()['has_estimate']  # Previous export remains available.
    assert not state()['is_ready']
    bundle = reports / f'DOCUMENTS_{TID}.json'
    bundle.write_text(json.dumps({'schema_version': 1, 'tender_id': TID, 'state': 'failed', 'files': [], 'errors': ['missing']}))
    assert state()['next_action'] == 'download_documents'
    bundle.write_text('{broken')
    assert state()['document_download_blocked']
    bundle.unlink()
    parse.unlink()
    assert state()['next_action'] == 'review'


def test_download_log_alone_is_not_a_source_document(tmp_path):
    folder = tmp_path / 'downloads' / TID
    folder.mkdir(parents=True)
    (folder / 'download_log.json').write_text('[]')
    (folder / '.autobot-incoming-part').write_text('incomplete')
    (tmp_path / 'tenders.json').write_text(json.dumps([{'tender_id': TID}]))
    item = build_workflow_payload(data_dir=tmp_path, include_storage=False)['tenders'][0]
    assert item['next_action'] == 'download_documents' and not item['has_downloads']


def test_canonical_routes_keep_handlers_and_csrf(monkeypatch):
    client = web_ui.app.test_client()
    rules = {rule.rule: rule.endpoint for rule in web_ui.app.url_map.iter_rules()}
    for alias, canonical in [('/api/rebuild-report', '/api/reports/rebuild'),
                             ('/api/rebuild-all-reports', '/api/reports/rebuild-all'),
                             ('/api/storage-overview', '/api/tenders/storage-overview'),
                             ('/api/crm/projects', '/api/tenders/crm/projects'),
                             ('/api/research-items', '/research/items'),
                             ('/market-audit', '/tenders/market-audit')]:
        assert rules[alias] == rules[canonical]
    assert client.post('/api/reports/rebuild', json={'tender_id': TID}, headers={'Origin': 'https://untrusted.example'}).status_code == 403
    monkeypatch.setattr(web_ui, 'load_tender_metadata', lambda: {})
    assert client.post('/api/reports/rebuild', json={'tender_id': TID}).status_code == 404
    assert client.post('/research/items', json={'queries': ''}).status_code == 400
    assert client.get('/tenders/market-audit?record=../../private').status_code == 404
    script = client.get('/research/client.js')
    assert script.status_code == 200
    assert script.get_data() == (web_ui.REPO_ROOT / 'autobot/static/research.js').read_bytes()
    monkeypatch.setattr(web_ui, 'crm_projects_for_picker', lambda: [{'id': 1, 'name': 'Allowed project'}])
    assert client.get('/api/tenders/crm/projects').json['projects'] == [{'id': 1, 'name': 'Allowed project'}]


def test_catalogue_buttons_have_correct_job_routes(monkeypatch):
    states = ['download_documents', 'extract_estimate', 'find_market_prices', 'review']
    items = [{'tender_id': str(12345678 + i), 'next_action': state, 'has_downloads': i > 0,
              'has_estimate': i > 1, 'has_market_sources': i > 2, 'has_comparison': i > 2} for i, state in enumerate(states)]
    monkeypatch.setattr(web_ui, 'build_workflow_payload', lambda **_: {'tenders': items, 'counts': {state: 1 for state in states}})
    monkeypatch.setattr(web_ui, 'load_tender_metadata', lambda: {})
    html = web_ui.app.test_client().get('/tenders').get_data(as_text=True)
    assert 'Готовы к решению' not in html and 'Есть сравнение' in html
    assert 'data-tender-action="download"' in html and 'data-tender-action="rebuild"' in html
    assert 'data-tender-action="continue"' in html
    for required in ['data-tender-delete', 'data-tender-select', 'tenderSort', 'exactTenderSearch', 'loadStorageBtn']:
        assert required in html
