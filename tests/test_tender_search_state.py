from argparse import Namespace
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from unittest.mock import MagicMock
import hashlib
import json
from pathlib import Path

import pytest

from autobot import main
from autobot import tender_search_state as state


def arguments(**changes):
    values = dict(max_pages=2, max_tenders=15, days_back=30, catalog_only=False,
                  resume_downloads=False, from_tender_id='', from_tender_url='',
                  from_downloaded_tender_id='', emit_new_ids_to='')
    return Namespace(**dict(values, **changes))


def tender(tid='12345678', **changes):
    item = main.Tender(tid, 'Благоустройство территории', 'https://zakupki.gov.ru/notice/' + tid,
                       'Ярославская область', 'Подача заявок', 50_000_000, '15.09.2026')
    return replace(item, **changes)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    paths = {key: tmp_path / key for key in ('downloads', 'extracted', 'reports')}
    paths['root'] = tmp_path
    for path in paths.values():
        path.mkdir(exist_ok=True)
    monkeypatch.setattr(main, 'ensure_dirs', lambda: paths)
    monkeypatch.setattr(main, 'telegram_config', lambda: None)
    monkeypatch.setattr(main, 'configure_rar_backend', lambda: True)
    monkeypatch.setattr(main, 'REGIONS', ['Ярославская область'])
    monkeypatch.setattr(main, 'KEYWORDS', ['благоустройство'])
    monkeypatch.setattr(main.business_time, 'today_iso', lambda: '2026-09-15')
    monkeypatch.setenv('SEARCH_RESUME', '1')
    monkeypatch.setenv('REFRESH_CACHED_STAGES', '0')
    return paths


def save_checkpoint(paths, *, done=(), items=None):
    args = arguments()
    main._save_search_checkpoint(paths, args, filtered=items or [tender(), tender('23456789')],
                                completed_ids=set(done), new_ids={'12345678'}, search_total=2)
    path = paths['root'] / 'search_resume_checkpoint.json'
    return path, json.loads(path.read_text(encoding='utf-8'))


def test_default_search_does_not_load_even_a_valid_unfinished_checkpoint(isolated):
    path, _ = save_checkpoint(isolated)
    before = path.read_bytes()
    assert main._load_search_checkpoint(isolated, arguments()) is None
    assert path.read_bytes() == before


def test_catalog_search_gets_fresh_cards_without_changing_download_progress(isolated, monkeypatch):
    path, _ = save_checkpoint(isolated)
    before = path.read_bytes()
    monkeypatch.setattr(main, 'parse_args', lambda: arguments(catalog_only=True))
    called = []
    def search(*args, **kwargs):
        called.append(args)
        kwargs['diagnostics']['cards_seen'] += 1
        return [tender('34567890')]
    monkeypatch.setattr(main, 'search_tenders', search)
    # Catalog search must not launch a second scan of all historical statuses.
    monkeypatch.setenv('REFRESH_CACHED_STAGES', '1')
    monkeypatch.setattr(main, 'refresh_cached_open_tender_stages', lambda *a: pytest.fail('old status scan'))
    main.main()
    assert len(called) == 1 and path.read_bytes() == before
    summary = state.read_state(isolated['root'] / 'last_search_run.json')
    assert summary['counts']['new'] == 1 and summary['mode'] == 'fresh'
    assert json.loads((isolated['root'] / 'tenders.json').read_text(encoding='utf-8'))[0]['tender_id'] == '34567890'


def test_explicit_resume_skips_completed_ids_and_retains_failed_download(isolated, monkeypatch):
    path, original = save_checkpoint(isolated, done=['12345678'])
    monkeypatch.setattr(main, 'parse_args', lambda: arguments(resume_downloads=True))
    monkeypatch.setattr(main, 'search_tenders', lambda *a, **kw: pytest.fail('resume searched EIS'))
    downloads = []
    monkeypatch.setattr(main, 'open_tender_and_download_archives', lambda item, *_: downloads.append(item.tender_id) or [])
    main.main()
    assert downloads == ['23456789']
    current = state.checkpoint_for_resume(path)
    assert current['run_id'] == original['run_id'] and current['completed_ids'] == ['12345678']
    summary = state.read_state(isolated['root'] / 'last_search_run.json')
    assert summary['state'] == 'awaiting_resume' and summary['counts']['download_failed'] == 1
    assert state.public_resume(isolated['root'])['remaining'] == 1


def test_bad_archive_preserves_previous_report_and_resume_moves_to_other_tenders(isolated, monkeypatch):
    path, _ = save_checkpoint(isolated)
    monkeypatch.setattr(main, 'parse_args', lambda: arguments(resume_downloads=True))
    monkeypatch.setattr(main, 'search_tenders', lambda *a, **kw: pytest.fail('resume searched EIS'))
    first = isolated['downloads'] / '12345678' / 'bad.zip'
    first.parent.mkdir()
    first.write_bytes(b'broken archive')
    previous = isolated['reports'] / 'ОТЧЕТ_ПО_СМЕТАМ_12345678.xlsx'
    previous.write_bytes(b'previous report')
    visited = []
    monkeypatch.setattr(main, 'open_tender_and_download_archives',
        lambda item, *_: visited.append(item.tender_id) or ([first] if item.tender_id == '12345678' else []))
    main.main()
    assert visited == ['12345678', '23456789']
    assert previous.read_bytes() == b'previous report'
    assert json.loads((isolated['reports'] / 'ARCHIVES_12345678.json').read_text(encoding='utf-8'))['failed_count'] == 1
    assert state.checkpoint_for_resume(path)['completed_ids'] == []
    summary = state.read_state(isolated['root'] / 'last_search_run.json')
    assert summary['state'] == 'awaiting_resume' and summary['counts']['document_failed'] == 1


@pytest.mark.parametrize('case', ['old', 'future', 'naive', 'missing_date', 'signature', 'completed', 'bad_rows', 'bad_parameters'])
def test_invalid_resume_fails_closed_and_preserves_original(isolated, case):
    path, value = save_checkpoint(isolated)
    if case == 'old':
        value['started_at'] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    elif case == 'future':
        value['started_at'] = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    elif case == 'naive':
        value['started_at'] = '2026-09-15T00:00:00'
    elif case == 'missing_date':
        del value['started_at']
    elif case == 'signature':
        value['signature'] = 'other'
    elif case == 'completed':
        value['completed'] = True
    elif case == 'bad_rows':
        value['filtered_tenders'] = ['broken']
    else:
        value['parameters']['max_pages'] = []
    state.atomic_json(path, value)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        main._load_search_checkpoint(isolated, arguments(resume_downloads=True))
    assert path.read_bytes() == before


def test_legacy_checkpoint_is_preserved_before_new_bulk_search(isolated):
    path = isolated['root'] / 'search_resume_checkpoint.json'
    original = b'{"saved_at":"2026-08-24","completed":false}'
    path.write_bytes(original)
    save_checkpoint(isolated)
    archived = isolated['root'] / 'search_runs' / ('checkpoint-' + hashlib.sha256(original).hexdigest() + '.json')
    assert archived.read_bytes() == original
    assert state.checkpoint_for_resume(path)['schema_version'] == 2
    before = path.read_bytes()
    main._clear_search_checkpoint(isolated)
    assert not path.exists()
    assert any(p.read_bytes() == before for p in archived.parent.glob('checkpoint-*.json'))


def test_interrupted_json_replace_does_not_damage_previous_state(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    state.atomic_json(path, {'value': 1})
    before = path.read_bytes()
    def fail(*args):
        raise OSError('interrupted')
    monkeypatch.setattr(state.os, 'replace', fail)
    with pytest.raises(OSError):
        state.atomic_json(path, {'value': 2})
    assert path.read_bytes() == before
    assert list(tmp_path.glob('*.tmp')) == []


@pytest.mark.parametrize('changes,reason', [
    ({'price_rub': None}, 'price_unknown'), ({'price_rub': float('nan')}, 'price_unknown'),
    ({'price_rub': 10_000}, 'price'), ({'stage': ''}, 'stage_unknown'),
    ({'stage': 'Работа комиссии'}, 'stage'), ({'publish_date': None}, 'date_unknown'),
    ({'publish_date': '???'}, 'date_unknown'), ({'publish_date': '15.08.2026'}, 'date_old'),
    ({'publish_date': '16.09.2026'}, 'date_future'),
])
def test_filter_reasons_are_explicit(isolated, changes, reason):
    assert reason in main.tender_filter_reasons(tender(**changes), 30)
    assert not main.tender_matches_filters(tender(**changes), 30)


def test_date_boundary_includes_whole_business_day_and_reevaluates_clock(isolated, monkeypatch):
    assert main.is_recent('16.08.2026', 30)
    assert main.tender_matches_filters(tender(publish_date='16.08.2026'), 30)
    monkeypatch.setattr(main.business_time, 'today_iso', lambda: '2026-09-16')
    assert not main.is_recent('16.08.2026', 30)
    assert not main.tender_matches_filters(tender(publish_date='16.08.2026'), 30)


def test_funnel_counts_before_limit_and_keeps_rejection_reasons(isolated, monkeypatch):
    main.merge_and_save_tenders(isolated, [tender()])
    monkeypatch.setattr(main, 'parse_args', lambda: arguments(catalog_only=True, max_tenders=1))
    monkeypatch.setattr(main, 'search_tenders', lambda *a, **kw: [tender(), tender('23456789'), tender('34567890', price_rub=None), tender('45678901', publish_date='01.01.2020')])
    main.main()
    summary = state.read_state(isolated['root'] / 'last_search_run.json')
    assert summary['counts'] == dict(found=4, unique=4, matched=2, limited=1, selected=1, known=1, new=0, processed=0)
    assert summary['rejections'] == {'price_unknown': 1, 'date_old': 1}


def test_unavailable_source_is_not_successful_empty_search(isolated, monkeypatch):
    path, _ = save_checkpoint(isolated)
    before = path.read_bytes()
    monkeypatch.setattr(main, 'parse_args', lambda: arguments(catalog_only=True))
    def search(*args, diagnostics, **kwargs):
        state.record_source_error(diagnostics, 'source unavailable')
        return []
    monkeypatch.setattr(main, 'search_tenders', search)
    with pytest.raises(RuntimeError, match='не означает отсутствие'):
        main.main()
    assert state.read_state(isolated['root'] / 'last_search_run.json')['state'] == 'failed'
    assert path.read_bytes() == before


def fake_browser(monkeypatch, *, text='', cards=0, go=None):
    playwright = MagicMock()
    browser = playwright.__enter__.return_value.chromium.launch.return_value
    page = MagicMock()
    card_list = MagicMock()
    card_list.count.return_value = cards
    card_list.nth.side_effect = lambda i: i
    page.locator.side_effect = lambda selector: card_list if selector.startswith('div.') else MagicMock(inner_text=lambda **kw: text)
    if go:
        page.goto.side_effect = go
    monkeypatch.setattr(main, 'sync_playwright', lambda: playwright)
    monkeypatch.setattr(main, '_new_eis_page', lambda _: page)
    return browser, page


@pytest.mark.parametrize('text,field', [('По вашему запросу ничего не найдено', 'empty_pages'), ('Подтвердите, что вы не робот', 'unknown_pages')])
def test_empty_and_unrecognized_page_are_different(monkeypatch, text, field):
    browser, page = fake_browser(monkeypatch, text=text)
    summary = state.start_summary('fresh')
    assert main.search_tenders('region', 'keyword', 10, diagnostics=summary['source']) == []
    assert summary['source'][field] == 1 and page.goto.call_count == 1
    summary['counts'] = {'unique': 0}
    state.finish_discovery(summary)
    assert summary['state'] == ('completed' if field == 'empty_pages' else 'unavailable')
    browser.close.assert_called_once()


def test_two_page_errors_stop_requests_and_partial_cards_survive(monkeypatch):
    browser, page = fake_browser(monkeypatch, cards=1, go=[None, main.PlaywrightError('timeout'), main.PlaywrightError('timeout')])
    monkeypatch.setattr(main, '_tender_from_search_card', lambda *a: tender())
    stats = state.start_summary('fresh')['source']
    assert len(main.search_tenders('region', 'keyword', 10, diagnostics=stats)) == 1
    assert stats['page_errors'] == 2 and page.goto.call_count == 3
    browser.close.assert_called_once()


def test_expired_budget_does_not_launch_browser(monkeypatch):
    monkeypatch.setattr(main, 'sync_playwright', lambda: pytest.fail('browser launched'))
    stats = state.start_summary('fresh')['source']
    assert main.search_tenders('region', 'keyword', diagnostics=stats, deadline=0) == []
    assert stats['budget_exhausted'] and stats['pages_requested'] == 0


def test_loading_page_waits_for_cards_before_declaring_unknown(monkeypatch):
    browser, page = fake_browser(monkeypatch, text='Загрузка результатов')
    cards = page.locator('div.search-registry-entry-block, div.registry-entry__form')
    cards.count.side_effect = [0, 1]
    monkeypatch.setattr(main, '_tender_from_search_card', lambda *args: tender())
    stats = state.start_summary('fresh')['source']
    assert len(main.search_tenders('region', 'keyword', 1, diagnostics=stats)) == 1
    cards.first.wait_for.assert_called_once_with(state='attached', timeout=10000)
    assert stats['unknown_pages'] == 0


def test_unreadable_card_does_not_erase_other_card(monkeypatch):
    browser, page = fake_browser(monkeypatch, cards=2)
    def parse(index, region):
        if index == 0:
            raise main.PlaywrightError('card unavailable')
        return tender()
    monkeypatch.setattr(main, '_tender_from_search_card', parse)
    stats = state.start_summary('fresh')['source']
    assert len(main.search_tenders('region', 'keyword', 1, diagnostics=stats)) == 1
    assert stats['card_errors'] == 1 and stats['cards_seen'] == 2


def test_second_search_cannot_overwrite_running_state(isolated, monkeypatch):
    from autobot.atomic_output import output_lock
    monkeypatch.setattr(main, 'parse_args', lambda: arguments(catalog_only=True))
    with output_lock(isolated['root'] / 'eis_search'):
        with pytest.raises(SystemExit, match='другом процессе'):
            main.main()
    assert not (isolated['root'] / 'last_search_run.json').exists()


def test_status_survives_restart_and_resume_api_uses_saved_parameters(isolated, monkeypatch):
    from autobot import web_ui
    save_checkpoint(isolated, done=['12345678'])
    summary = state.start_summary('fresh')
    summary.update(state='awaiting_resume', message='Документы ожидают продолжения')
    state.save_summary(isolated['root'], summary)
    monkeypatch.setattr(web_ui, 'DATA_DIR', isolated['root'])
    monkeypatch.setattr(web_ui, '_merge_site_busy', lambda: False)
    monkeypatch.setattr(web_ui, 'parse_state', dict(web_ui.parse_state, running=False))
    calls = []
    class Thread:
        def __init__(self, *, target, kwargs, daemon):
            calls.append(kwargs)
        def start(self):
            pass
    monkeypatch.setattr(web_ui.threading, 'Thread', Thread)
    client = web_ui.app.test_client()
    result = client.get('/api/parse-status').get_json()
    assert result['search_summary']['message'] == summary['message']
    assert result['search_resume']['remaining'] == 1
    response = client.post('/api/start-parse', json={'search_mode': 'resume', 'max_pages': 99})
    assert response.status_code == 200
    assert '--resume-downloads' in calls[0]['cli_args'] and '99' not in calls[0]['cli_args']
    assert '--catalog-only' not in calls[0]['cli_args']


def test_resume_api_without_valid_checkpoint_starts_no_thread(isolated, monkeypatch):
    from autobot import web_ui
    monkeypatch.setattr(web_ui, 'DATA_DIR', isolated['root'])
    monkeypatch.setattr(web_ui, '_merge_site_busy', lambda: False)
    monkeypatch.setattr(web_ui, 'parse_state', dict(web_ui.parse_state, running=False))
    monkeypatch.setattr(web_ui.threading, 'Thread', lambda **kwargs: pytest.fail('started'))
    client = web_ui.app.test_client()
    assert client.post('/api/start-parse', json={'search_mode': 'resume'}).status_code == 409
    assert client.post('/api/start-parse', json={'search_mode': 'unknown'}).status_code == 400


def test_saved_running_status_is_interrupted_without_a_live_process(isolated):
    from autobot.atomic_output import output_lock
    summary = state.start_summary('fresh')
    state.save_summary(isolated['root'], summary)
    assert state.public_summary(isolated['root'])['state'] == 'interrupted'
    with output_lock(isolated['root'] / 'eis_search'):
        assert state.public_summary(isolated['root'])['state'] == 'searching'
    assert state.public_summary(isolated['root'])['state'] == 'interrupted'
    # A read does not rewrite the historic facts.
    assert state.read_state(isolated['root'] / 'last_search_run.json')['state'] == 'searching'


def test_disabled_resume_is_not_advertised(isolated, monkeypatch):
    save_checkpoint(isolated)
    monkeypatch.setenv('SEARCH_RESUME', '0')
    assert not state.public_resume(isolated['root'])['available']
    with pytest.raises(ValueError, match='отключено'):
        main._load_search_checkpoint(isolated, arguments(resume_downloads=True))


@pytest.mark.parametrize('args', [
    ['--resume-downloads', '--catalog-only'], ['--from-tender-id', '12345678'],
    ['--from-downloaded-tender-id', '../other'], ['--max-pages', '999'],
])
def test_cli_rejects_ambiguous_or_invalid_targets(monkeypatch, args):
    monkeypatch.setattr(main.sys, 'argv', ['autobot.main', *args])
    with pytest.raises(SystemExit) as error:
        main.parse_args()
    assert error.value.code == 2
