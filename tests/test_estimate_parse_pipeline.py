from dataclasses import replace
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from autobot import main, estimate_parse_worker as worker, estimate_publication as publication


TID = '12345678'


def excel_bytes():
    cells = [[None] * 16 for _ in range(5)]
    cells[0][0] = 'Раздел 1. Окна'
    cells[1][0:3] = [1, 'ГЭСН15-01-050-04', 'Демонтаж облицовки оконных откосов']
    cells[1][7:9] = ['100 м2', 0.0316]
    cells[2][2], cells[2][13], cells[2][15] = 'Всего по позиции', 136210.13, 4304.24
    cells[3][0:3] = [2, 'ФСБЦ-11.3.02.04-0027', 'Блок оконный из ПВХ-профилей']
    cells[3][7:9] = ['м2', 17.1]
    cells[3][13], cells[3][15] = 5062.25, 86564.48
    cells[4][2], cells[4][15] = 'Всего по позиции', 86564.48
    stream = io.BytesIO()
    pd.DataFrame(cells).to_excel(stream, index=False, header=False)
    return stream.getvalue()


def setup(tmp_path):
    paths = {'root': tmp_path, **{key: tmp_path / key for key in ('downloads', 'extracted', 'reports')}}
    for path in paths.values():
        path.mkdir(exist_ok=True)
    source = paths['downloads'] / TID / 'ЛСР.xlsx'
    source.parent.mkdir()
    source.write_bytes(excel_bytes())
    tender = main.Tender(TID, 'Монтаж окон', '', '', '', 100_000, None)
    return paths, source, tender


def saved_reports(paths):
    return {path.name: path.read_bytes() for path in paths['reports'].iterdir()
            if path.is_file() and path.suffix in {'.xlsx', '.html', '.json'} and not path.name.startswith('PARSE_RUN_')}


def test_real_child_parser_and_publication_preserve_values_on_next_request(tmp_path):
    paths, source, tender = setup(tmp_path)
    first = publication.parse_and_publish(tender, [source], [], [source], paths)
    assert first[2]['Сумма, руб'].tolist() == [4304.24, 86564.48]
    assert first[2]['Кол-во'].tolist() == [0.0316, 17.1]
    assert first[2]['Ед. изм.'].tolist() == ['100 м2', 'м2']
    second = publication.parse_and_publish(tender, [source], [], [source], paths)
    assert first[2].equals(second[2])
    assert publication.read_status(paths['reports'], TID)['state'] == 'complete'
    control = json.loads((paths['reports'] / f'ESTIMATE_PARSE_{TID}.json').read_text(encoding='utf-8'))
    assert control['parsed_row_count'] == 2 and control['parse_sources'] == worker.snapshot([source])
    assert not list(paths['reports'].glob('.autobot-parse-*'))


def test_strict_excel_read_distinguishes_error_and_keeps_old_helper_contract(tmp_path):
    paths, source, tender = setup(tmp_path)
    source.write_bytes(b'broken')
    assert main.extract_rows_from_excel(source, tender) == []
    with pytest.raises(worker.EstimateParseRejected, match='прочитать Excel'):
        main.extract_rows_from_excel(source, tender, strict=True)


@pytest.mark.parametrize('limits', [worker.ParseLimits(sheet_rows=2), worker.ParseLimits(cells=10),
                                  worker.ParseLimits(columns=3), worker.ParseLimits(sheets=0)])
def test_excel_limits_refuse_partial_table(tmp_path, limits):
    _, source, tender = setup(tmp_path)
    with pytest.raises(worker.EstimateParseRejected):
        worker.parse_files([source], [], tender, limits)


def test_broken_or_empty_source_does_not_overwrite_previous_report_and_retry_works(tmp_path):
    paths, source, tender = setup(tmp_path)
    publication.parse_and_publish(tender, [source], [], [source], paths)
    before = saved_reports(paths)
    for content in (b'broken',):
        source.write_bytes(content)
        with pytest.raises(worker.EstimateParseRejected):
            publication.parse_and_publish(tender, [source], [], [source], paths)
        assert saved_reports(paths) == before
    pd.DataFrame([['Документ без сметных позиций']]).to_excel(source, index=False, header=False)
    with pytest.raises(worker.EstimateParseRejected, match='не найдены позиции'):
        publication.parse_and_publish(tender, [source], [], [source], paths)
    assert saved_reports(paths) == before
    source.write_bytes(excel_bytes())
    assert len(publication.parse_and_publish(tender, [source], [], [source], paths)[2]) == 2


def test_one_unreadable_source_prevents_partial_tender_replacement(tmp_path):
    paths, source, tender = setup(tmp_path)
    publication.parse_and_publish(tender, [source], [], [source], paths)
    before = saved_reports(paths)
    second = source.with_name('ЛСР2.xlsx'); second.write_bytes(b'broken')
    with pytest.raises(worker.EstimateParseRejected, match='ЛСР2.xlsx'):
        publication.parse_and_publish(tender, [source, second], [], [source, second], paths)
    assert saved_reports(paths) == before


def test_changed_original_during_staging_preserves_all_previous_outputs(tmp_path, monkeypatch):
    paths, source, tender = setup(tmp_path)
    publication.parse_and_publish(tender, [source], [], [source], paths)
    before = saved_reports(paths)
    original = main.write_tender_estimate_html
    def change(*args):
        result = original(*args)
        source.write_bytes(b'changed after parse')
        return result
    monkeypatch.setattr(main, 'write_tender_estimate_html', change)
    with pytest.raises(worker.EstimateParseRejected, match='изменились'):
        publication.parse_and_publish(tender, [source], [], [source], paths)
    assert saved_reports(paths) == before
    assert not list(paths['reports'].glob('.autobot-parse-*'))


def test_second_output_replace_error_rolls_back_first_output(tmp_path, monkeypatch):
    paths, source, tender = setup(tmp_path)
    publication.parse_and_publish(tender, [source], [], [source], paths)
    before = saved_reports(paths)
    original = publication.os.replace
    failed = []
    def replace_file(origin, destination):
        if Path(destination) == paths['reports'] / f'ОТЧЕТ_ПО_СМЕТАМ_{TID}.xlsx' and not failed:
            failed.append(True)
            raise OSError('fixture disk failure')
        return original(origin, destination)
    monkeypatch.setattr(publication.os, 'replace', replace_file)
    with pytest.raises(worker.EstimateParseRejected, match='сохранить'):
        publication.parse_and_publish(tender, [source], [], [source], paths)
    assert failed and saved_reports(paths) == before


def test_restore_failure_keeps_private_recovery_copies(tmp_path, monkeypatch):
    paths, source, tender = setup(tmp_path)
    publication.parse_and_publish(tender, [source], [], [source], paths)
    before = saved_reports(paths)
    # Make the new HTML different so recovery really has a file to restore.
    cells = pd.read_excel(source, header=None)
    cells.iloc[1, 2] = 'Демонтаж облицовки дверных откосов'
    cells.to_excel(source, index=False, header=False)
    original = publication.os.replace
    def fail(origin, destination):
        origin, destination = Path(origin), Path(destination)
        if destination.parent == paths['reports'] and (destination.suffix == '.xlsx' or origin.parent.name == 'previous'):
            raise OSError('fixture repeated disk failure')
        return original(origin, destination)
    monkeypatch.setattr(publication.os, 'replace', fail)
    with pytest.raises(worker.EstimateParseRejected, match='Резервная копия'):
        publication.parse_and_publish(tender, [source], [], [source], paths)
    recovery = list(paths['reports'].glob('.autobot-parse-*'))
    assert len(recovery) == 1
    assert all((recovery[0] / 'previous' / name).read_bytes() == data for name, data in before.items())
    assert publication.display_status(paths['reports'], TID)['blocked']
    monkeypatch.setattr(publication.os, 'replace', original)
    assert len(publication.parse_and_publish(tender, [source], [], [source], paths)[2]) == 2
    assert publication.read_status(paths['reports'], TID)['state'] == 'complete'
    assert not list(paths['reports'].glob('PUBLICATION_*.json'))
    assert not list(paths['reports'].glob('.autobot-parse-*'))


def test_parent_deadline_cleans_child_and_next_parse_succeeds(tmp_path):
    _, source, tender = setup(tmp_path)
    with pytest.raises(worker.EstimateParseRejected, match='Время разбора'):
        worker.run_parser([source], [], tender, limits=worker.ParseLimits(seconds=.001))
    assert len(worker.run_parser([source], [], tender)['rows']) == 2


def test_non_estimate_workbook_remains_visible_in_parse_warnings(tmp_path):
    paths, source, tender = setup(tmp_path)
    other = source.with_name('Пояснения.xlsx')
    pd.DataFrame([['Общие сведения о закупке']]).to_excel(other, index=False, header=False)
    result = publication.parse_and_publish(tender, [source, other], [], [source, other], paths)
    assert len(result[2]) == 2
    status = publication.display_status(paths['reports'], TID)
    assert not status['blocked'] and 'Пояснения.xlsx' in status['warnings'][0]


def test_pdf_page_and_render_limits_without_allocating_pixels(tmp_path, monkeypatch):
    class Document:
        needs_pass = False
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __len__(self): return 2
        def __iter__(self): return iter([SimpleNamespace(rect=SimpleNamespace(width=1000, height=1000))])
    monkeypatch.setitem(sys.modules, 'pymupdf', SimpleNamespace(open=lambda _: Document()))
    with pytest.raises(worker.EstimateParseRejected, match='много страниц'):
        worker.inspect_pdf(tmp_path / 'large.pdf', worker.ParseLimits(pdf_pages=1))
    with pytest.raises(worker.EstimateParseRejected, match='Размер страницы'):
        worker.inspect_pdf(tmp_path / 'large.pdf', worker.ParseLimits(page_pixels=10))


def test_rebuild_api_reserves_once_retains_200_and_exposes_run_id(tmp_path, monkeypatch):
    import copy
    from autobot import web_ui
    monkeypatch.setattr(web_ui, 'parse_state', dict(copy.deepcopy(web_ui.parse_state), running=False))
    monkeypatch.setattr(web_ui, '_merge_site_busy', lambda: False)
    monkeypatch.setattr(web_ui, 'load_tender_metadata', lambda: {TID: {}})
    monkeypatch.setattr(web_ui, 'REPORTS_DIR', tmp_path)
    started = []
    class Thread:
        def __init__(self, **kwargs): self.kwargs = kwargs
        def start(self): started.append(self.kwargs)
    monkeypatch.setattr(web_ui.threading, 'Thread', Thread)
    client = web_ui.app.test_client()
    assert client.post('/api/rebuild-report', json={'tender_id':TID}, headers={'Origin':'https://evil.example'}).status_code == 403
    response = client.post('/api/rebuild-report', json={'tender_id':TID})
    assert response.status_code == 200 and response.json['run_id']
    assert started[0]['kwargs']['cli_args'] == ['--from-downloaded-tender-id', TID]
    assert client.post('/api/rebuild-report', json={'tender_id':TID}).status_code == 409
    assert len(started) == 1
    assert client.get('/api/parse-status?tender_id=' + TID).json['run_id'] == response.json['run_id']


def test_failed_parse_visible_before_first_report_on_both_tabs(tmp_path, monkeypatch):
    from autobot import web_ui, tender_detail
    monkeypatch.setattr(web_ui, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(tender_detail, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(web_ui, 'load_tender_metadata', lambda: {TID: {'title':'Смета'}})
    publication.atomic_json(publication.status_path(tmp_path, TID),
        {'schema_version':1, 'tender_id':TID, 'state':'failed', 'error':'ЛСР.xlsx: файл не прочитан'})
    for suffix in ('', '?tab=files'):
        response = web_ui.app.test_client().get('/tenders/' + TID + suffix)
        assert response.status_code == 200
        assert 'ЛСР.xlsx: файл не прочитан' in response.get_data(as_text=True)
        assert 'data-rebuild-report' in response.get_data(as_text=True)


def test_empty_file_selection_records_failed_state_and_keeps_report(tmp_path):
    paths, source, tender = setup(tmp_path)
    publication.parse_and_publish(tender, [source], [], [source], paths)
    before = saved_reports(paths)
    with pytest.raises(worker.EstimateParseRejected, match='не найдены позиции'):
        publication.parse_and_publish(tender, [], [], [source], paths)
    assert saved_reports(paths) == before
    assert publication.display_status(paths['reports'], TID)['blocked']


def test_busy_document_set_cannot_overwrite_other_run_state(tmp_path):
    from autobot.atomic_output import output_lock
    from autobot.document_bundle import bundle_path
    paths, source, tender = setup(tmp_path)
    status = publication.status_path(paths['reports'], TID)
    publication.atomic_json(status, {'schema_version':1, 'tender_id':TID, 'state':'running', 'run_id':'other'})
    before = status.read_bytes()
    with output_lock(bundle_path(paths['reports'], TID)):
        with pytest.raises(worker.EstimateParseRejected, match='другим процессом'):
            publication.parse_and_publish(tender, [source], [], [source], paths)
    assert status.read_bytes() == before


def test_bulk_parse_continues_after_unreadable_tender_and_records_failure(tmp_path, monkeypatch):
    from argparse import Namespace
    paths = {'root':tmp_path, **{key:tmp_path/key for key in ('downloads','extracted','reports')}}
    for path in paths.values(): path.mkdir(exist_ok=True)
    tenders = [main.Tender(tid, 'Строительство и благоустройство', 'https://zakupki.gov.ru/notice/' + tid,
                          'Ярославская область', 'Подача заявок', 50_000_000, main.datetime.now().strftime('%d.%m.%Y'))
               for tid in (TID, '23456789')]
    args = Namespace(max_pages=2, max_tenders=15, days_back=30, catalog_only=False, resume_downloads=False,
                     from_tender_id='', from_tender_url='', from_downloaded_tender_id='', emit_new_ids_to='')
    monkeypatch.setattr(main, 'ensure_dirs', lambda: paths)
    monkeypatch.setattr(main, 'parse_args', lambda: args)
    monkeypatch.setattr(main, 'telegram_config', lambda: None)
    monkeypatch.setattr(main, 'configure_rar_backend', lambda: True)
    monkeypatch.setattr(main, 'REGIONS', ['Ярославская область'])
    monkeypatch.setattr(main, 'KEYWORDS', ['строительство'])
    monkeypatch.setattr(main, 'search_tenders', lambda *args, **kwargs: tenders)
    def download(tender, folder):
        path = folder / tender.tender_id / 'ЛСР.xlsx'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'broken' if tender.tender_id == TID else excel_bytes())
        return [path]
    monkeypatch.setattr(main, 'open_tender_and_download_archives', download)
    main.main()
    assert not (paths['reports'] / f'ОТЧЕТ_ПО_СМЕТАМ_{TID}.xlsx').exists()
    assert (paths['reports'] / 'ОТЧЕТ_ПО_СМЕТАМ_23456789.xlsx').is_file()
    assert publication.read_status(paths['reports'], TID)['state'] == 'failed'
    assert publication.read_status(paths['reports'], '23456789')['state'] == 'complete'
    summary = json.loads((tmp_path / 'last_search_run.json').read_text(encoding='utf-8'))
    assert summary['counts']['document_failed'] == 1
