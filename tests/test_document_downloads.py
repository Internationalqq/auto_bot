from argparse import Namespace
import hashlib
import io
import json
from pathlib import Path
import tempfile
import zipfile

import pytest

from autobot import document_bundle as bundle, document_download as transfer, main, source_documents as documents
from autobot.tender_search_state import atomic_json


class Response:
    def __init__(self, chunks, headers=None, status=200):
        self.chunks, self.headers, self.status_code = chunks, headers or {}, status

    def raise_for_status(self):
        pass

    @property
    def content(self):
        raise AssertionError('Whole response was loaded into memory')

    def iter_content(self, chunk_size):
        assert chunk_size <= 64 * 1024
        yield from self.chunks


def zipped(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return stream.getvalue()


@pytest.mark.parametrize('payload,headers,status,limit,reason', [
    ([], {}, 200, 100, 'пустой'),
    ([b'<html>captcha</html>'], {}, 200, 100, 'страницу'),
    ([b'login'], {'content-type': 'text/html'}, 200, 100, 'страницу'),
    ([b'123'], {'content-length': '5'}, 200, 100, 'не полностью'),
    ([b'123'], {'content-length': '101'}, 200, 100, 'лимит'),
    ([b'12', b'34'], {}, 200, 3, 'лимит'),
    ([b'abc'], {}, 206, 100, 'целый документ'),
    ([b'broken archive'], {'content-disposition': 'attachment; filename="estimate.zip"'}, 200, 100, 'повреждён'),
])
def test_stream_rejects_incomplete_or_wrong_response(tmp_path, payload, headers, status, limit, reason):
    with pytest.raises(transfer.DownloadRejected, match=reason):
        transfer.stream_response(Response(payload, headers, status), tmp_path/'output', limit=limit)


def test_stream_preserves_exact_bytes_and_detects_office_file(tmp_path):
    data = zipped([('xl/workbook.xml', b'<workbook/>')])
    path = tmp_path/'document'
    result = transfer.stream_response(Response([data[:12], data[12:]], {'content-length': str(len(data))}), path)
    assert path.read_bytes() == data and result['sha256'] == hashlib.sha256(data).hexdigest()
    assert result['extension'] == '.xlsx' and result['size_bytes'] == len(data)


def test_timeout_keeps_previous_file_and_removes_private_scratch(monkeypatch, tmp_path):
    scratch = tmp_path/'scratch'; scratch.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(scratch))
    destination = tmp_path/'existing.zip'
    destination.write_bytes(b'previous file')
    with monkeypatch.context() as patch:
        patch.setattr(transfer, 'TIMEOUT', .00001)
        with pytest.raises(transfer.DownloadRejected, match='Время загрузки'):
            transfer.fetch_document('https://source.invalid/file', destination)
    assert destination.read_bytes() == b'previous file'
    assert not list(scratch.glob('autobot-download-*')) and not list(tmp_path.glob('.autobot-*'))


def test_download_detects_extension_even_when_endpoint_ends_in_html(monkeypatch, tmp_path):
    payload = zipped([('xl/workbook.xml', b'<workbook/>')])
    def fetch(url, path, **kwargs):
        path.write_bytes(payload)
        return {'extension': '.xlsx', 'headers': {}}
    monkeypatch.setattr(transfer, 'fetch_document', fetch)
    path, name = main.download_file('https://source.example/download.html', tmp_path/'incoming.bin')
    assert path.suffix == '.xlsx' and name == 'download.xlsx' and path.read_bytes() == payload


def run_batch(tmp_path, entries):
    def download(url, destination, **kwargs):
        name, data = entries[url]
        assert 0 < kwargs['timeout'] <= bundle.MAX_SECONDS
        if data is None:
            kwargs['diagnostics'].append('Документ не загрузился')
            return None
        destination.write_bytes(data)
        return destination, name
    return bundle.download_batch('12345678', [(url, url) for url in entries], tmp_path/'downloads',
        cookies={}, download=download, sanitize=main._sanitize_filename_for_windows)


def test_failed_batch_keeps_old_sources_log_and_report_then_retry_succeeds(tmp_path):
    first = run_batch(tmp_path, {'url1': ('estimate.xlsx', b'old')})[0]
    log = first.parent/'download_log.json'
    before_log = log.read_bytes()
    report = tmp_path/'reports'/'ОТЧЕТ_ПО_СМЕТАМ_12345678.xlsx'
    report.write_bytes(b'previous report')
    assert run_batch(tmp_path, {'url1': ('estimate.xlsx', b'new'), 'url2': ('note.pdf', None)}) == []
    assert first.read_bytes() == b'old' and log.read_bytes() == before_log and report.read_bytes() == b'previous report'
    assert bundle.display_status(tmp_path/'reports', '12345678')['blocked']
    with pytest.raises(bundle.DocumentBundleRejected, match='не завершена'):
        bundle.current_files(tmp_path/'downloads', '12345678')
    assert not list(first.parent.glob('.autobot-incoming-*'))
    paths = run_batch(tmp_path, {'url1': ('estimate.xlsx', b'new'), 'url2': ('note.pdf', b'pdf')})
    assert {path.name for path in paths} == {'estimate.xlsx', 'note.pdf'}
    assert first.read_bytes() == b'new' and bundle.current_files(tmp_path/'downloads', '12345678') == paths
    assert any(path.read_bytes() == b'old' for path in (tmp_path/'trash').rglob('estimate.xlsx'))


def test_repeated_bytes_do_not_duplicate_and_current_set_excludes_retained_old_archive(tmp_path):
    initial = run_batch(tmp_path, {'old': ('old.zip', b'old archive'), 'current': ('estimate.xlsx', b'current')})
    current = run_batch(tmp_path, {'current': ('estimate.xlsx', b'current')})
    assert [path.name for path in current] == ['estimate.xlsx'] and initial[0].is_file()
    assert len(list(current[0].parent.glob('*.xlsx'))) == 1
    assert bundle.current_files(tmp_path/'downloads', '12345678') == current
    current[0].write_bytes(b'changed')
    with pytest.raises(bundle.DocumentBundleRejected, match='изменился'):
        bundle.current_files(tmp_path/'downloads', '12345678')


def test_same_name_distinct_documents_remain_distinct(tmp_path):
    paths = run_batch(tmp_path, {'first': ('Документ.pdf', b'first document'), 'second': ('Документ.pdf', b'second document')})
    assert len(paths) == 2 and len(set(path.name for path in paths)) == 2
    assert {path.read_bytes() for path in paths} == {b'first document', b'second document'}
    assert bundle.current_files(tmp_path/'downloads', '12345678') == paths


def test_unfinished_incoming_is_not_listed_or_downloadable(monkeypatch, tmp_path):
    folder = tmp_path/'12345678'; folder.mkdir()
    path = folder/'.autobot-incoming-secret.bin'; path.write_bytes(b'partial')
    monkeypatch.setattr(documents, 'DOWNLOADS_DIR', tmp_path)
    assert documents.list_tender_source_files('12345678')['count'] == 0
    with pytest.raises(FileNotFoundError):
        documents.resolve_tender_source_file('12345678', documents.make_file_token(path.name))


def test_reparse_uses_current_manifest_and_preserves_old_report_if_document_changed(monkeypatch, tmp_path):
    from test_estimate_parse_pipeline import excel_bytes
    files = run_batch(tmp_path, {'current': ('estimate.xlsx', excel_bytes())})
    (files[0].parent/'old.zip').write_bytes(b'broken old archive')
    paths = {key: tmp_path/key for key in ('downloads', 'extracted', 'reports')}
    paths['root'] = tmp_path
    for path in paths.values():path.mkdir(exist_ok=True)
    args = Namespace(max_pages=2, max_tenders=15, days_back=30, catalog_only=False, resume_downloads=False,
                     from_tender_id='', from_tender_url='', from_downloaded_tender_id='12345678', emit_new_ids_to='')
    monkeypatch.setattr(main, 'ensure_dirs', lambda: paths)
    monkeypatch.setattr(main, 'parse_args', lambda: args)
    monkeypatch.setattr(main, 'telegram_config', lambda: None)
    monkeypatch.setattr(main, 'configure_rar_backend', lambda: True)
    def extract(archives, *args, **kwargs):
        assert archives == []
        return []
    monkeypatch.setattr(main, 'extract_archives_nested', extract)
    main.main()
    parsed = json.loads((paths['reports']/'ESTIMATE_PARSE_12345678.json').read_text())
    assert [row['path'] for row in parsed['parse_sources']] == [str(p) for p in files]
    report = paths['reports']/'ОТЧЕТ_ПО_СМЕТАМ_12345678.xlsx'
    before = report.read_bytes()
    files[0].write_bytes(b'changed')
    with pytest.raises(bundle.DocumentBundleRejected):
        main.main()
    assert report.read_bytes() == before


def test_partial_download_visible_on_both_detail_tabs(monkeypatch, tmp_path):
    from autobot import web_ui, tender_detail
    monkeypatch.setattr(tender_detail, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(web_ui, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(web_ui, 'load_tender_metadata', lambda: {'12345678': {'title': 'Тест'}})
    atomic_json(bundle.bundle_path(tmp_path, '12345678'), {'schema_version': 1, 'tender_id': '12345678',
                'state': 'failed', 'files': [], 'errors': ['Документ не загрузился']})
    for suffix in ('', '?tab=files'):
        response = web_ui.app.test_client().get('/tenders/12345678' + suffix)
        assert response.status_code == 200
        assert 'Комплект документов не загружен' in response.get_data(as_text=True)


def test_economic_source_changes_with_document_set_but_not_retry_timestamp(tmp_path):
    from autobot.tender_economics_source import build_source
    path = bundle.bundle_path(tmp_path, '12345678')
    payload = {'schema_version': 1, 'tender_id': '12345678', 'state': 'complete',
               'files': [{'saved_name': 'a.xlsx', 'sha256': 'a'*64, 'size_bytes': 1}], 'started_at': 'first'}
    build = lambda: build_source('12345678', {}, tmp_path, lambda *args: {'positions': []})
    atomic_json(path, payload); first = build()['version']
    payload['started_at'] = 'second'; atomic_json(path, payload)
    assert build()['version'] == first
    payload['files'][0]['sha256'] = 'b'*64; atomic_json(path, payload)
    assert build()['version'] != first


def test_navigation_retries_cannot_reset_document_discovery_deadline(monkeypatch):
    now, calls = [100.0], []
    monkeypatch.setattr(main.time, 'monotonic', lambda: now[0])
    class Page:
        def goto(self, url, **kwargs):
            calls.append(kwargs)
            assert kwargs['timeout'] == 2000
            now[0] = 103
            raise main.PlaywrightTimeoutError('inaccessible')
    ok, errors = main._goto_with_retries(Page(), 'https://source.example', retries=5, deadline=102)
    assert not ok and len(calls) == 1 and 'истекло' in errors[-1]


def test_refresh_api_reserves_once_and_uses_catalogue_url_not_request_body(monkeypatch, tmp_path):
    from autobot import web_ui
    import copy
    monkeypatch.setattr(web_ui, 'parse_state', dict(copy.deepcopy(web_ui.parse_state), running=False))
    monkeypatch.setattr(web_ui, '_merge_site_busy', lambda: False)
    monkeypatch.setattr(web_ui, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(web_ui, 'load_tender_metadata', lambda: {'12345678': {'url': 'https://zakupki.gov.ru/notice?regNumber=12345678'}})
    started = []
    class Thread:
        def __init__(self, **kwargs):self.kwargs=kwargs
        def start(self):started.append(self.kwargs)
    monkeypatch.setattr(web_ui.threading, 'Thread', Thread)
    client = web_ui.app.test_client()
    route = '/api/tenders/12345678/refresh-documents'
    assert client.post(route, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert not started
    response = client.post(route, json={'url': 'http://127.0.0.1/private'})
    assert response.status_code == 202 and len(started) == 1
    assert started[0]['kwargs']['cli_args'] == ['--from-tender-id','12345678','--from-tender-url','https://zakupki.gov.ru/notice?regNumber=12345678']
    assert client.post(route).status_code == 409 and len(started) == 1
    status = client.get('/api/parse-status?tender_id=12345678').json
    assert status['running'] and status['run_id'] == response.json['run_id'] and status['tender_id'] == '12345678'
    assert 'document_status' in status
    assert client.post('/api/tenders/87654321/refresh-documents').status_code == 404


def test_refresh_launch_failure_releases_reservation(monkeypatch):
    from autobot import web_ui
    import copy
    monkeypatch.setattr(web_ui, 'parse_state', dict(copy.deepcopy(web_ui.parse_state), running=False))
    monkeypatch.setattr(web_ui, '_merge_site_busy', lambda: False)
    monkeypatch.setattr(web_ui, 'load_tender_metadata', lambda: {'12345678': {}})
    class Thread:
        def __init__(self, **kwargs):pass
        def start(self):raise RuntimeError('cannot create thread')
    monkeypatch.setattr(web_ui.threading, 'Thread', Thread)
    assert web_ui.app.test_client().post('/api/tenders/12345678/refresh-documents').status_code == 500
    assert not web_ui.parse_state['running'] and web_ui.parse_state['exit_code'] == -1


def test_duplicate_cleanup_preserves_current_manifest_references(tmp_path):
    import os
    from autobot.source_file_versions import cleanup_existing_source_duplicates
    files = run_batch(tmp_path, {'current': ('estimate.zip', b'current')})
    duplicate = files[0].with_name('estimate (2).zip')
    duplicate.write_bytes(b'current')
    os.utime(files[0], (100, 100)); os.utime(duplicate, (200, 200))
    result = cleanup_existing_source_duplicates('12345678', data_dir=tmp_path, include_extracted=False)
    assert result['files_moved'] == 1 and files[0].is_file() and not duplicate.exists()
    assert bundle.current_files(tmp_path/'downloads', '12345678') == files
    run_batch(tmp_path, {'failed': ('bad.pdf', None)})
    duplicate.write_bytes(b'current')
    result = cleanup_existing_source_duplicates('12345678', data_dir=tmp_path, include_extracted=False)
    assert result['errors'] and duplicate.exists() and files[0].exists()


def test_cleanup_does_not_race_with_active_document_download(tmp_path):
    from autobot.atomic_output import output_lock
    from autobot.source_file_versions import cleanup_existing_source_duplicates
    files = run_batch(tmp_path, {'current': ('estimate.zip', b'current')})
    with output_lock(bundle.bundle_path(tmp_path/'reports', '12345678')):
        result = cleanup_existing_source_duplicates('12345678', data_dir=tmp_path)
    assert result['errors'] and result['files_moved'] == 0 and files[0].is_file()
