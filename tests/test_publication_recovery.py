"""Kill real processes at publication boundaries; never touch working reports."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from autobot import estimate_publication_recovery as recovery

from publication_process_fixture import TID, RUN, prepare, child


@pytest.mark.parametrize('boundary', ['1', '2', '3', '4'])
def test_next_process_restores_all_previous_outputs_after_crash(tmp_path, boundary):
    reports, stage, old, _ = prepare(tmp_path)
    child(tmp_path, 'publish', boundary)
    assert recovery.publication_path(reports, TID).exists()
    assert stage.is_dir()
    child(tmp_path, 'recover')
    for name, content in list(old.items())[:3]:
        assert (reports / name).read_bytes() == content
    status = json.loads((reports / recovery.output_names(TID)[-1]).read_text())
    assert status['state'] == 'failed' and status['publication_recovered'] == RUN
    assert not stage.exists() and not recovery.publication_path(reports, TID).exists()
    before = {path.name: path.read_bytes() for path in reports.iterdir() if path.suffix != '.lock'}
    child(tmp_path, 'recover')
    assert {path.name: path.read_bytes() for path in reports.iterdir() if path.suffix != '.lock'} == before


@pytest.mark.parametrize('boundary', ['1', 'recovered_status'])
def test_recovery_itself_can_be_killed_and_repeated(tmp_path, boundary):
    reports, stage, old, _ = prepare(tmp_path)
    child(tmp_path, 'publish', '3')
    child(tmp_path, 'recover', boundary)
    assert all((stage / 'previous' / name).read_bytes() == content for name, content in old.items())
    child(tmp_path, 'recover')
    assert [(reports / name).read_bytes() for name in recovery.output_names(TID)[:3]] == list(old.values())[:3]


def test_committed_generation_survives_crash_before_cleanup(tmp_path):
    reports, stage, _, new = prepare(tmp_path)
    child(tmp_path, 'publish', 'committed')
    assert recovery.publication_path(reports, TID).exists()
    child(tmp_path, 'recover')
    assert all((reports / name).read_bytes() == content for name, content in new.items())
    assert not stage.exists()


def test_first_publication_rolls_back_only_its_new_outputs(tmp_path):
    reports, _, _, _ = prepare(tmp_path, previous=False)
    unrelated = reports / 'another-report.xlsx'
    unrelated.write_bytes(b'unrelated')
    child(tmp_path, 'publish', '3')
    child(tmp_path, 'recover')
    assert all(not (reports / name).exists() for name in recovery.output_names(TID)[:3])
    assert unrelated.read_bytes() == b'unrelated'


@pytest.mark.parametrize('conflict', ['target', 'backup', 'journal'])
def test_unknown_change_blocks_recovery_before_any_restoration(tmp_path, conflict):
    reports, stage, _, _ = prepare(tmp_path)
    child(tmp_path, 'publish', '2')
    if conflict == 'target':
        path = reports / recovery.output_names(TID)[2]
    elif conflict == 'backup':
        path = stage / 'previous' / recovery.output_names(TID)[1]
    else:
        path = recovery.publication_path(reports, TID)
    path.write_bytes(b'changed outside this publication')
    before = {p.relative_to(reports): p.read_bytes() for p in reports.rglob('*') if p.is_file() and p.suffix != '.lock'}
    with pytest.raises(recovery.PublicationRecoveryRequired):
        recovery.recover_publication(reports, TID)
    assert {p.relative_to(reports): p.read_bytes() for p in reports.rglob('*') if p.is_file() and p.suffix != '.lock'} == before


def test_reader_holds_existing_writer_lock_and_is_reentrant(tmp_path):
    reports, _, _, _ = prepare(tmp_path)
    with recovery.consistent_report(reports, TID):
        with recovery.consistent_report(reports, TID):
            code = '''
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from autobot.estimate_publication_recovery import consistent_report
try:
    with consistent_report(Path(sys.argv[1]),'12345678901',timeout=.01):pass
except TimeoutError:sys.exit(0)
sys.exit(1)
'''
            result = subprocess.run([sys.executable, '-c', code, str(reports), str(Path(__file__).resolve().parents[1])], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
    recovery.recover_publication(reports, TID)


def test_legacy_scratch_without_journal_is_not_guessed_or_removed(tmp_path):
    reports, stage, old, _ = prepare(tmp_path)
    recovery.recover_publication(reports, TID)
    assert stage.is_dir()
    assert all((reports / name).read_bytes() == content for name, content in old.items())


def test_commit_sync_error_does_not_mark_completed_generation_failed(tmp_path, monkeypatch):
    reports, stage, _, new = prepare(tmp_path)
    original = recovery._save
    failed = []
    def fail_once(path, data):
        original(path, data)
        if data.get('phase') == 'committed' and not failed:
            failed.append(True)
            raise OSError('sync after commit rename')
    monkeypatch.setattr(recovery, '_save', fail_once)
    with recovery.consistent_report(reports, TID):
        recovery.activate([stage / name for name in recovery.output_names(TID)], reports, stage)
    assert failed and all((reports / name).read_bytes() == value for name, value in new.items())
    assert not recovery.publication_path(reports, TID).exists()


def test_cleanup_sync_failure_keeps_committed_marker_for_next_recovery(tmp_path, monkeypatch):
    reports, stage, _, new = prepare(tmp_path)
    sync = recovery._sync_directory
    failed = []
    def fail_once(path):
        if path == reports and not stage.exists() and not recovery.publication_path(reports, TID).exists() and not failed:
            failed.append(True)
            raise OSError('cleanup directory sync failed')
        sync(path)
    monkeypatch.setattr(recovery, '_sync_directory', fail_once)
    with recovery.consistent_report(reports, TID):
        with pytest.raises(recovery.PublicationRecoveryRequired, match='очистка'):
            recovery.activate([stage / name for name in recovery.output_names(TID)], reports, stage)
    assert json.loads(recovery.publication_path(reports, TID).read_text())['phase'] == 'committed'
    child(tmp_path, 'recover')
    assert failed and all((reports / name).read_bytes() == value for name, value in new.items())
    assert not recovery.publication_path(reports, TID).exists()


def test_change_after_preflight_is_not_overwritten_by_later_restore(tmp_path, monkeypatch):
    reports, stage, _, _ = prepare(tmp_path)
    child(tmp_path, 'publish', '3')
    restore = recovery._restore_copy
    modified = reports / recovery.output_names(TID)[2]
    def changed(backup, target):
        restore(backup, target)
        modified.write_bytes(b'outside change during recovery')
    monkeypatch.setattr(recovery, '_restore_copy', changed)
    with pytest.raises(recovery.PublicationRecoveryRequired, match='во время восстановления'):
        recovery.recover_publication(reports, TID)
    assert modified.read_bytes() == b'outside change during recovery'
    assert stage.is_dir() and recovery.publication_path(reports, TID).exists()


@pytest.mark.parametrize('damage', ['stage_path', 'filename', 'digest', 'schema', 'run_id'])
def test_corrupt_journal_identity_never_modifies_files(tmp_path, damage):
    reports, _, _, _ = prepare(tmp_path)
    child(tmp_path, 'publish', '2')
    marker = recovery.publication_path(reports, TID)
    data = json.loads(marker.read_text())
    if damage == 'stage_path':
        data['stage_dir'] = '../outside'
    elif damage == 'filename':
        data['files'][0]['name'] = '../outside'
    elif damage == 'digest':
        data['files'][0]['new_sha256'] = 'invalid'
    elif damage == 'schema':
        data['schema_version'] = 0
    else:
        data['run_id'] = None
    marker.write_text(json.dumps(data))
    before = {p.relative_to(reports): p.read_bytes() for p in reports.rglob('*') if p.is_file()}
    with pytest.raises(recovery.PublicationRecoveryRequired):
        recovery.recover_publication(reports, TID)
    assert {p.relative_to(reports): p.read_bytes() for p in reports.rglob('*') if p.is_file()} == before


def test_startup_recovery_is_repeatable_and_skips_one_blocked_tender(tmp_path, caplog):
    reports, _, old, _ = prepare(tmp_path)
    child(tmp_path, 'publish', '2')
    other = '12345678902'
    corrupt = recovery.publication_path(reports, other)
    corrupt.write_bytes(b'invalid')
    unknown = reports / 'PUBLICATION_unknown.json'
    unknown.write_bytes(b'legacy')
    result = recovery.recover_pending_publications(reports)
    assert result == {'recovered': [TID], 'blocked': [other]}
    assert other in caplog.text
    assert [(reports / name).read_bytes() for name in recovery.output_names(TID)[:3]] == list(old.values())[:3]
    assert recovery.recover_pending_publications(reports) == {'recovered': [], 'blocked': [other]}
    assert corrupt.read_bytes() == b'invalid' and unknown.read_bytes() == b'legacy'


def test_report_routes_recover_and_block_conflicts_but_keep_originals(tmp_path, monkeypatch):
    from autobot import web_ui
    reports, _, old, _ = prepare(tmp_path)
    child(tmp_path, 'publish', '2')
    monkeypatch.setattr(web_ui, 'REPORTS_DIR', reports)
    client = web_ui.app.test_client()
    name = recovery.output_names(TID)[0]
    result = client.get('/reports/' + name)
    assert result.status_code == 200 and result.data == old[name]
    result.close()  # close send_file's handle before a second publication on Windows
    # Block another attempt after a manual change to one of its output files.
    stage = reports / '.autobot-parse-fixture1'
    stage.mkdir()
    for i, name in enumerate(recovery.output_names(TID)):
        (stage / name).write_bytes(json.dumps({'run_id': RUN, 'schema_version': 1,
            'tender_id': TID, 'state': 'complete'}).encode() if i == 3 else b'next version')
    child(tmp_path, 'publish', '2')
    (reports / recovery.output_names(TID)[2]).write_bytes(b'external change')
    monkeypatch.setattr(web_ui, 'list_tender_source_files', lambda tid: {'files': [
        {'name': '<source>.xlsx', 'token': 'original', 'kind': 'excel', 'extension': 'xlsx', 'type_label': 'Excel', 'size_fmt': '2 КБ'}]})
    original = tmp_path / 'original.xlsx'
    original.write_bytes(b'original unchanged')
    monkeypatch.setattr(web_ui, 'resolve_tender_source_file', lambda *args: original)
    for path in (f'/tenders/{TID}', f'/tenders/{TID}?tab=files', f'/tenders/{TID}/estimate.xlsx',
                 f'/tenders/{TID}/market-sources.xlsx', f'/tenders/{TID}/svodka.xlsx',
                 '/reports/' + recovery.output_names(TID)[0], f'/merge-report/{TID}/'):
        result = client.get(path)
        assert result.status_code == 409, path
        html = result.get_data(as_text=True)
        assert 'Отчёт требует проверки' in html and '&lt;source&gt;' in html
        assert 'Скачать оригинал' in html and 'next version' not in html
    for path, method, payload in [(f'/api/tenders/{TID}/economics-source', 'get', None),
            (f'/api/tenders/{TID}/agent-market/jobs', 'post', {}),
            ('/api/export-to-crm', 'post', {'tender_id': TID})]:
        result = getattr(client, method)(path, json=payload)
        assert result.status_code == 409 and result.json['error'] == 'publication_recovery_required'
    result = client.get(f'/tenders/{TID}/source-files/original/download')
    assert result.status_code == 200 and result.data == b'original unchanged'
    result.close()
    assert client.get('/reports/' + recovery.publication_path(reports, TID).name).status_code == 404


def test_consumer_and_market_helpers_refuse_inconsistent_generation(tmp_path, monkeypatch):
    from autobot import tender_detail, tender_economics_source, real_market_scraper, merge_estimate_market, report_merge_html
    reports, _, _, _ = prepare(tmp_path)
    child(tmp_path, 'publish', '2')
    (reports / recovery.output_names(TID)[2]).write_bytes(b'external change')
    for module in (tender_detail, real_market_scraper, merge_estimate_market, report_merge_html):
        monkeypatch.setattr(module, 'REPORTS_DIR', reports)
    calls = [lambda: tender_detail.build_tender_detail(TID, {}, {}),
             lambda: tender_economics_source.build_source(TID, {}, reports, lambda *args: pytest.fail('must not read data')),
             lambda: real_market_scraper._estimate_bytes(TID),
             lambda: real_market_scraper.publish_agent_market_result(TID, {}, {}),
             lambda: merge_estimate_market.merge_estimate_and_market(TID),
             lambda: merge_estimate_market.refresh_svodka_if_market_newer(TID),
             lambda: report_merge_html.write_tender_report_site(TID)]
    for call in calls:
        with pytest.raises(recovery.PublicationRecoveryRequired):
            call()


def test_active_publication_returns_retry_and_releases_request_lock(tmp_path, monkeypatch):
    import threading
    from autobot import web_ui
    from autobot.atomic_output import output_lock
    reports, _, _, _ = prepare(tmp_path)
    monkeypatch.setattr(web_ui, 'REPORTS_DIR', reports)
    ready, stop = threading.Event(), threading.Event()
    def hold():
        with output_lock(reports / recovery.output_names(TID)[1]):
            ready.set()
            stop.wait(5)
    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(2)
        result = web_ui.app.test_client().get(f'/api/tenders/{TID}/economics-source')
        assert result.status_code == 503 and result.headers['Retry-After'] == '2'
        assert result.json['error'] == 'report_busy'
    finally:
        stop.set()
        thread.join(5)
    result = web_ui.app.test_client().get('/reports/' + recovery.output_names(TID)[0])
    assert result.status_code == 200
    result.close()
    with recovery.consistent_report(reports, TID, timeout=.01):
        pass
