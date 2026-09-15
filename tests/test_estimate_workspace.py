import json
import os
import re

import pandas as pd
import pytest

from autobot import web_ui, uploaded_estimates as store
from test_uploaded_estimate_store import bind, record


def fixture(tmp_path, monkeypatch):
    root = bind(tmp_path, monkeypatch)
    source, meta, rows, _ = record(root)
    meta['title'] = '</script><script>alert(1)</script>'
    store.publish(root, meta, rows)
    return source, meta, rows, web_ui.app.test_client()


def page_config(html):
    return json.loads(re.search(r'<script id="estimatePageConfig" type="application/json">(.*?)</script>', html, re.S)[1])


def test_card_has_no_ready_market_before_search_and_escapes_config(tmp_path, monkeypatch):
    source, meta, _, client = fixture(tmp_path, monkeypatch)
    html = client.get('/estimates/' + meta['id'] + '?table_view=compare').get_data(as_text=True)
    config = page_config(html)
    assert config['activeTableView'] == 'estimate'
    assert config['title'] == meta['title'] and meta['title'] not in html
    assert 'data-estimate-view-btn="compare"' in html
    assert 'disabled title="Сначала найдите рыночные цены"' in html
    assert '/estimates/' + meta['id'] + '/original' in html
    assert str(source) not in html
    assert config['marketRevision'] == client.get('/api/estimates/' + meta['id'] + '/market-status').json['market_revision']


def test_partial_market_retains_unpriced_positions_and_source_link(tmp_path, monkeypatch):
    _, meta, rows, client = fixture(tmp_path, monkeypatch)
    market = web_ui._estimate_rows_to_report_df(rows)
    market['Рыночные источники'] = 'https://example.org/offer/test?one=1&two=2'
    market['Цены за ед. (рынок, руб)'] = '150'
    market.to_excel(web_ui._estimate_market_raw_path(meta['id']), index=False)
    html = client.get('/estimates/' + meta['id'] + '?table_view=sources').get_data(as_text=True)
    assert page_config(html)['activeTableView'] == 'sources'
    assert 'href="https://example.org/offer/test?one=1&amp;two=2"' in html
    assert 'Найденная цена' in html and 'Устройство покрытия' in html
    assert '>150<' in html
    # Candidate prices remain inspectable, while the profitability input stays empty.
    verified = web_ui._estimate_market_df_for_rows(web_ui._estimate_market_raw_path(meta['id']), rows)
    assert web_ui._estimate_compare_rows(rows, verified)[0]['market_price'] == '—'
    exported = pd.read_excel(__import__('io').BytesIO(client.get('/estimates/' + meta['id'] + '/market-sources.xlsx').data))
    assert str(exported.iloc[0]['Цена рынка']) == '150'
    no_match = client.get('/estimates/' + meta['id'] + '?q=несуществующая+строка&table_view=compare').get_data(as_text=True)
    assert page_config(no_match)['activeTableView'] == 'estimate'
    assert 'По фильтру ничего не найдено.' in no_match
    assert page_config(no_match)['marketRevision'] == page_config(html)['marketRevision']


def test_revision_changes_only_for_saved_reports(tmp_path, monkeypatch):
    _, meta, _, client = fixture(tmp_path, monkeypatch)
    status = lambda: client.get('/api/estimates/' + meta['id'] + '/market-status').json
    initial = status()['market_revision']
    path = web_ui._estimate_market_raw_path(meta['id'])
    path.write_bytes(b'first version')
    first = status()['market_revision']
    assert initial != first and first == status()['market_revision']
    web_ui.estimate_market_jobs[meta['id']] = {'running': False, 'error': 'Network timeout'}
    assert status()['market_revision'] == first
    previous = path.stat()
    path.write_bytes(b'other version')
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1000000))
    assert status()['market_revision'] != first
    path.unlink()
    assert status()['market_revision'] == initial


def test_missing_source_nan_is_not_reported_as_an_offer():
    rows = [{'name':'Бетон', 'unit':'м3'}]
    frame = web_ui._estimate_rows_to_report_df(rows)
    frame['Рыночные источники'] = float('nan')
    frame['Ошибка / статус'] = float('nan')
    frame['Цены за ед. (рынок, руб)'] = float('nan')
    result = web_ui._estimate_source_rows(rows, frame)[0]
    assert result['status'] == 'Нет источников' and result['market_price'] == '—'
    assert not result['site_url']


def test_original_download_preserves_bytes_and_safe_headers(tmp_path, monkeypatch):
    source, meta, _, client = fixture(tmp_path, monkeypatch)
    response = client.get('/estimates/' + meta['id'] + '/original')
    assert response.status_code == 200 and response.data == source.read_bytes()
    assert 'attachment;' in response.headers['Content-Disposition']
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    response.close()
    source.unlink()
    assert client.get('/estimates/' + meta['id'] + '/original').status_code == 404


@pytest.mark.parametrize('kind', ['outside', 'sibling', 'unsupported', 'missing', 'relative'])
def test_original_never_serves_other_paths(tmp_path, monkeypatch, kind):
    source, meta, _, client = fixture(tmp_path, monkeypatch)
    target = {'outside': tmp_path/'outside.pdf', 'sibling': source.parent.parent/('c'*16)/'other.pdf',
              'unsupported': source.parent/'config.json', 'missing': source.parent/'missing.pdf',
              'relative': tmp_path/'outside.pdf'}[kind]
    if kind != 'missing':
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'never disclose')
    corrupted = dict(meta, source_path=str(target.relative_to(tmp_path)) if kind == 'relative' else str(target))
    monkeypatch.setattr(web_ui, '_load_estimate_meta', lambda _: corrupted)
    assert client.get('/estimates/' + meta['id'] + '/original').status_code == 404
    assert client.get('/estimates/invalid-id/original').status_code == 404


def test_original_refuses_symlink(tmp_path, monkeypatch):
    source, meta, _, client = fixture(tmp_path, monkeypatch)
    link = source.parent/'link.pdf'
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip('Host does not permit creating symlinks')
    monkeypatch.setattr(web_ui, '_load_estimate_meta', lambda _: dict(meta, source_path=str(link)))
    assert client.get('/estimates/' + meta['id'] + '/original').status_code == 404


def test_component_routes_keep_canonical_estimates_prefix():
    client = web_ui.app.test_client()
    for url, mime in [('/estimates/workspace.css','text/css'),('/estimates/workspace.js','javascript')]:
        response = client.get(url)
        assert response.status_code == 200 and mime in response.content_type
        response.close()
