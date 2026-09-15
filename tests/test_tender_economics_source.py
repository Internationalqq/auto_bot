from pathlib import Path

import pytest

from autobot.tender_economics_source import build_source


def test_legacy_context_keeps_its_version_until_documents_are_refreshed(tmp_path):
    source = build_source('12345678', {}, tmp_path, lambda *args: {'positions': []})
    # Recorded from af110f3: installing the bundle reader must not invalidate
    # private conditions before any real source or document-state change.
    assert source['version'] == 'cfb4c00b3aea801dc8217b224841ca9ec6e709fbbb2b24794e117d732843872e'


def test_reference_only_counts_confirmed_prices_and_tracks_real_market_files(tmp_path):
    tid = '12345678'
    source_file = tmp_path / f'РЫНОК_ИСТОЧНИКИ_ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx'
    source_file.write_bytes(b'first')
    detail = {'positions': [
        {'position_key': 'a', 'estimate_total': '100.01', 'estimate_unit': '10', 'market_unit': '5', 'verified_count': 1},
        {'position_key': 'b', 'estimate_total': '100', 'estimate_unit': '10', 'market_unit': '1', 'verified_count': 0}],
        'estimate_check_detail': 'Не все источники проверены'}
    build = lambda *args: detail
    first = build_source(tid, {'price_rub': '1000.01'}, tmp_path, build)
    assert first['known_market_kopecks'] == 5001
    assert first['rows_priced'] == 1 and first['rows_total'] == 2
    assert first['initial_price_kopecks'] == 100001
    assert not {'profit', 'conditions', 'actor_id'} & first.keys()
    source_file.write_bytes(b'second')
    assert build_source(tid, {'price_rub': '1000.01'}, tmp_path, build)['version'] != first['version']
    second = build_source(tid, {}, tmp_path, build)
    detail['positions'][0]['verified_count'] = 0
    expired = build_source(tid, {}, tmp_path, build)
    assert expired['version'] != second['version'] and expired['known_market_kopecks'] is None


def test_change_during_read_is_not_published_as_consistent_context(tmp_path):
    def build(*args):
        (tmp_path / 'ESTIMATE_PARSE_12345678.json').write_text('{}')
        return {'positions': []}
    with pytest.raises(ValueError, match='source_changed'):
        build_source('12345678', {}, tmp_path, build)


def test_source_route_is_read_only_and_has_no_private_scenario_state(monkeypatch, tmp_path):
    from autobot import web_ui
    monkeypatch.setattr(web_ui, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(web_ui, 'load_tender_metadata', lambda: {'12345678': {'title': 'Test'}})
    monkeypatch.setattr(web_ui, 'build_tender_detail', lambda *args: {'positions': [], 'estimate_check_detail': ''})
    client = web_ui.app.test_client()
    response = client.get('/api/tenders/12345678/economics-source')
    assert response.status_code == 200
    assert response.json['known_market_kopecks'] is None
    assert response.headers['Cache-Control'] == 'no-store'
    assert client.post('/api/tenders/12345678/economics-source', json={'profit': 100}).status_code == 405
    assert client.get('/api/tenders/123456789/economics-source').status_code == 404
