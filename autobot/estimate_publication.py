"""One publication boundary for all tender parse entry points."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from autobot.atomic_output import output_lock
from autobot.document_bundle import bundle_path, current_files
from autobot.estimate_parse_worker import EstimateParseRejected, run_parser, snapshot, validate_snapshot
from autobot.estimate_publication_recovery import (
    PublicationRecoveryRequired, activate as _activate, publication_path, recover_publication,
)
from autobot.tender_search_state import atomic_json


@contextmanager
def _staging(reports):
    path = Path(tempfile.mkdtemp(prefix='.autobot-parse-', dir=reports))
    preserve = False
    try:
        yield path
    except PublicationRecoveryRequired:
        preserve = True
        raise
    finally:
        if not preserve:
            if path.resolve().parent == reports.resolve() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)


def status_path(reports, tender_id):
    return bundle_path(reports, tender_id).with_name(f'PARSE_RUN_{tender_id}.json')


def read_status(reports, tender_id):
    path = status_path(reports, tender_id)
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('oversized status')
        data = json.loads(path.read_text(encoding='utf-8'))
        if (data.get('schema_version') != 1 or data.get('tender_id') != str(tender_id)
                or data.get('state') not in {'running', 'complete', 'failed'}):
            raise ValueError('invalid status')
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        return {'state': 'failed', 'error': 'Не удалось прочитать состояние разбора. Повторите разбор документов.'}


def display_status(reports, tender_id):
    try:
        recover_publication(reports, tender_id, timeout=.01)
    except (EstimateParseRejected, OSError, TimeoutError) as error:
        return {'checked': True, 'blocked': True, 'state': 'failed',
                'errors': [str(error)[:1000]], 'warnings': [], 'publication_blocked': True}
    data = read_status(reports, tender_id)
    if data is None:
        return {'checked': False, 'blocked': False, 'state': 'legacy', 'errors': [], 'warnings': []}
    state = data['state']
    error = ('Разбор не завершён. Если обработка уже остановилась, повторите её.'
             if state == 'running' else str(data.get('error') or '')[:1000])
    warnings = [str(value)[:500] for value in data.get('warnings', [])[:5]] if isinstance(data.get('warnings', []), list) else []
    return {'checked': True, 'blocked': state != 'complete', 'state': state,
            'errors': [error] if error else [], 'warnings': warnings}


def parse_and_publish(tender, excel_files, pdf_files, downloaded_files, out_paths):
    from autobot import main
    reports = Path(out_paths['reports'])
    reports.mkdir(parents=True, exist_ok=True)
    journal = status_path(reports, tender.tender_id)
    state = {'schema_version': 1, 'tender_id': str(tender.tender_id), 'run_id': uuid.uuid4().hex,
             'started_at': datetime.now(timezone.utc).isoformat(), 'state': 'running', 'warnings': []}
    try:
        with output_lock(bundle_path(reports, tender.tender_id), timeout=.1):
            recover_publication(reports, tender.tender_id)
            atomic_json(journal, state)
            try:
                selected = [Path(p).absolute() for p in downloaded_files]
                actual = [p.absolute() for p in current_files(out_paths['downloads'], tender.tender_id)]
                if set(actual) != set(selected):
                    raise EstimateParseRejected('Состав документов изменился перед разбором. Повторите его по текущим источникам.')
                originals = snapshot(selected)
                result = run_parser(excel_files, pdf_files, tender)
                with _staging(reports) as staging:
                    preview_paths = {**out_paths, 'reports': staging}
                    report, frame = main.write_tender_estimate_report(tender, result['rows'], preview_paths)
                    if frame.empty or not frame['Сумма, руб'].notna().any():
                        raise EstimateParseRejected('После проверки не осталось позиций с определённой суммой. Предыдущий отчёт сохранён.')
                    html = main.write_tender_estimate_html(tender, frame, preview_paths)
                    control = main.write_estimate_parse_manifest(tender.tender_id, pdf_files, result['rows'], preview_paths, result['official_totals'])
                    payload = json.loads(control.read_text(encoding='utf-8'))
                    payload.update(parse_sources=result['sources'], parse_documents=result['documents'],
                                   parse_resources=[{'source_file':row['source_file'], 'position_id':row.get('position_id'),
                                       'resources':row['resources']} for row in result['rows'] if row.get('resources')],
                                   unresolved_amount_rows=[row for row in result['rows'] if row.get('price_from_estimate_rub') is None],
                                   selected_excel_count=len(excel_files), parsed_row_count=len(frame))
                    atomic_json(control, payload)
                    warnings = []
                    for document in result['documents']:
                        name = Path(document['source_file']).name
                        if document['state'] == 'unrecognized':
                            warnings.append(name + ': файл прочитан, сметные строки не определены.')
                        elif document['state'] == 'fallback':
                            warnings.append(name + ': использован запасной текстовый разбор PDF; строки требуют проверки.')
                        elif any(document.get(key) for key in ('missing_quantity_rows', 'missing_unit_rows', 'missing_amount_rows')):
                            warnings.append(name + ': не определены количество, единица или сумма части позиций; проверьте исходную смету.')
                    state.update(state='complete', warnings=warnings, rows=len(frame),
                                 finished_at=datetime.now(timezone.utc).isoformat(), sources=result['sources'])
                    completed = staging / journal.name
                    atomic_json(completed, state)
                    validate_snapshot(originals)
                    validate_snapshot(result['sources'])
                    # Synchronize replacement of the authoritative Excel with
                    # other writers using the existing output lock contract.
                    with output_lock(reports / report.name):
                        _activate([html, report, control, completed], reports, staging)
                return result['rows'], reports / report.name, frame, reports / html.name
            except Exception as error:
                message = str(error) if isinstance(error, (EstimateParseRejected, ValueError)) else 'Не удалось сохранить новую смету (' + type(error).__name__ + ').'
                state.update(state='failed', error=message[:2000], finished_at=datetime.now(timezone.utc).isoformat())
                # An unresolved publication owns its journal and recovery copies.
                if not publication_path(reports, tender.tender_id).exists():
                    atomic_json(journal, state)
                raise EstimateParseRejected(message) from None
    except TimeoutError:
        raise EstimateParseRejected('Документы или отчёт уже обрабатываются другим процессом. Повторите позже.') from None
