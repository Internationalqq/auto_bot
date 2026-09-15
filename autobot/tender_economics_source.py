"""Read-only reference for CRM scenarios; no private assumptions live in AutoBot."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from autobot.market_contract import kopecks, relative_market_total_kopecks


def fingerprints(reports: Path, tender_id: str) -> dict:
    names = [f'ОТЧЕТ_ПО_СМЕТАМ_{tender_id}.xlsx', f'СВОДКА_РЫНОК_{tender_id}.xlsx',
             f'ESTIMATE_PARSE_{tender_id}.json', f'ARCHIVES_{tender_id}.json']
    names.extend(path.name for path in sorted(reports.glob(f'РЫНОК_ИСТОЧНИКИ_*{tender_id}*.xlsx')))
    if len(names) > 64:
        raise ValueError('too_many_sources')
    result = {}
    for name in names:
        path = reports / name
        if not path.is_file():
            result[name] = None
            continue
        if path.stat().st_size > 128 * 1024 * 1024:
            raise ValueError('source_too_large')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        result[name] = digest.hexdigest()
    from autobot.document_bundle import read_bundle
    documents = read_bundle(reports, tender_id)
    # Retry timestamps do not create a new economic source when bytes and state
    # stayed the same. Changes to the current set or an incomplete attempt do.
    if documents is not None:
        result['current_documents'] = {'state': documents.get('state'),
            'files': sorted(documents.get('files', []), key=lambda row: row.get('saved_name', ''))}
    from autobot.estimate_publication import read_status
    parsing = read_status(reports, tender_id)
    if parsing is not None:
        result['document_parse'] = {'state': parsing.get('state'), 'sources': parsing.get('sources', [])}
    return result


def build_source(tender_id, metadata, reports, build_detail):
    from autobot.estimate_publication_recovery import consistent_report
    with consistent_report(reports, tender_id):
        return _consistent_build_source(tender_id, metadata, reports, build_detail)


def _consistent_build_source(tender_id, metadata, reports, build_detail):
    before = fingerprints(reports, tender_id)
    detail = build_detail(tender_id, metadata, {})
    if before != fingerprints(reports, tender_id):
        raise ValueError('source_changed')
    priced, market_total, verified = 0, 0, []
    for row in detail['positions']:
        if not row.get('verified_count'):
            continue
        amount = relative_market_total_kopecks(row.get('estimate_total'), row.get('estimate_unit'), row.get('market_unit'))
        if amount is not None:
            priced += 1
            market_total += amount
            verified.append([row['position_key'], amount])
    source = {'tender_id': tender_id, 'title': str(metadata.get('title') or '')[:1000],
              'initial_price_kopecks': kopecks(metadata.get('price_rub')),
              'rows_total': len(detail['positions']), 'rows_priced': priced,
              'known_market_kopecks': market_total if priced else None,
              'source_warning': detail.get('estimate_check_detail', '')[:4000]}
    material = {'summary': source, 'files': before, 'verified_positions': verified, 'region': metadata.get('region')}
    source['version'] = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return source
