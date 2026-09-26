"""Headless sourcing orchestration. The same pipeline serves CLI and CRM."""
from contextlib import contextmanager

from autobot import buyer_store as store, buyer_suppliers as suppliers, buyer_jobs as jobs
from autobot.buyer_needs import snapshot, revision
from autobot.hermes_buyer import BuyerError


@contextmanager
def current_source(tid, keys=None):
    from autobot import web_ui as web
    from autobot.estimate_publication_recovery import consistent_report
    with consistent_report(web.REPORTS_DIR, tid):
        if not (web.REPORTS_DIR / f'ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx').is_file():
            raise BuyerError('Сначала загрузите и разберите смету')
        tender = web.build_tender_detail(tid, web.load_tender_metadata().get(tid, {}), {})
        rows = [p for p in tender['positions'] if p.get('price_state') != 'excluded']
        if keys is not None:
            wanted = set(keys)
            rows = [p for p in rows if p['position_key'] in wanted]
            if {p['position_key'] for p in rows} != wanted:
                raise BuyerError('Часть выбранных строк изменилась. Запустите подбор заново')
        else:
            rows = [p for p in rows if not p.get('verified_count')]
        yield {'tender_id': tid, 'region': tender.get('region'), 'positions': [
            {**p, 'specification': {'requirements': p.get('requirements'),
                'resource_scope': p.get('resource_scope'), 'section_note': p.get('section_note')}} for p in rows]}


@contextmanager
def current_draft(payload):
    """Lock publication before the buyer DB. Legacy draft contracts stay unchanged."""
    supplier = payload.get('supplier') or {}
    if not supplier.get('discovered'):
        yield
        return
    with current_source(payload['tender_id'], [p['position_key'] for p in payload['positions']]) as source:
        run_id = supplier.get('source_run_id')
        if run_id:
            run = store.source(payload['tender_id'], run_id)
            if run['status'] == 'canceled':
                raise BuyerError('Подбор остановлен; запрос не отправлен')
            if run['payload'].get('discovery_version') != store.DISCOVERY_VERSION:
                raise BuyerError('Правила проверки источников обновились; запустите подбор заново')
        if revision(source) != revision(payload):
            raise BuyerError('Смета изменилась. Старое обращение не отправлено; запустите подбор заново')
        yield


def prepare_run(tid, run_id):
    run = store.source(tid, run_id)
    if run['status'] == 'canceled': raise BuyerError('Подбор остановлен')
    payload = run['payload']
    if payload.get('discovery_version') != store.DISCOVERY_VERSION:
        raise BuyerError('Правила проверки источников обновились; запустите подбор заново')
    candidates = store.candidates(tid, run_id)
    with current_source(tid, [p['position_key'] for p in payload['positions']]) as source:
        if revision(source) != revision(payload):
            raise BuyerError('Смета изменилась после поиска. Запустите подбор для актуальных позиций')
        result = suppliers.prepare(snapshot(source), discovered=[c | {'source_run_id': run_id} for c in candidates])
    result['campaign_ids'] = []
    if payload.get('delivery') == 'email':
        from autobot import buyer_campaigns as campaigns
        for job in jobs.jobs(tid):
            if job['id'] not in result['job_ids']: continue
            supplier = job['payload']['draft_task'].get('supplier', {})
            if supplier.get('source_run_id') != run_id or not supplier.get('email'): continue
            for index in range(len(job['result']['drafts'])):
                result['campaign_ids'].append(campaigns.start(tid, job['id'], index))
    return result
