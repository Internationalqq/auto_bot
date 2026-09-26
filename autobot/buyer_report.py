"""Small machine-readable result and its plain-text projection. No accounting data."""
import json
from urllib.parse import quote
from decimal import Decimal
from autobot import buyer_store as store, buyer_jobs as jobs, buyer_outbox as outbox, buyer_replies as replies
from autobot.buyer_needs import revision


def build(tid, run_id=None):
    runs = store.listing(tid)
    if run_id is None and runs: run_id = runs[0]['id']
    if not run_id:
        return {'schema_version': 1, 'tender_id': tid, 'run_id': None, 'status': 'not_started', 'companies': [], 'errors': [], 'uncovered': []}
    run = store.source(tid, run_id)
    request_current, version_note = None, ''
    from autobot.buyer_workflow import current_source
    from autobot.hermes_buyer import BuyerError
    try:
        with current_source(tid, [p['position_key'] for p in run['payload']['positions']]) as current:
            request_current = revision(current) == revision(run['payload'])
            if not request_current: version_note = 'Смета изменилась после запуска; цены относятся к прежнему запросу'
    except (BuyerError, OSError):
        version_note = 'Не удалось проверить актуальность сметы; цены относятся к сохранённому запросу'
    candidates = store.candidates(tid, run_id)
    prepared = json.loads(run['prepared']) if run['prepared'] else {}
    drafts = jobs.jobs(tid)
    outgoing = outbox.listing(tid)
    incoming = replies.listing(tid)
    companies = []
    for supplier in candidates:
        matching = [j for j in drafts if j['id'] in prepared.get('job_ids', []) and
                    (j['payload']['draft_task'].get('supplier', {}).get('id') == supplier['id'] or
                     supplier.get('email') and j['payload']['draft_task'].get('supplier', {}).get('email') == supplier['email'])]
        job_ids = {j['id'] for j in matching}
        sent = [m for m in outgoing if m['draft_job_id'] in job_ids and m['recipient'] == supplier.get('email')]
        out_ids = {m['id'] for m in sent}
        answers = [r for r in incoming['messages'] if r['outbound_id'] in out_ids]
        prices = [dict(p, origin='website') for p in supplier.get('prices', [])]
        for answer in answers:
            for p in answer['prices']:
                prices.append({k: p[k] for k in ('position_key','price_kopecks','unit','vat','availability','delivery','state','reason')} |
                              {'origin':'reply', 'reply_id':answer['id'], 'received_at':answer['received_at']})
        if request_current is not True:
            prices = [p | {'state':'review','reason':'; '.join(filter(None,[p.get('reason'),version_note]))} for p in prices]
        messages = [{'subject': d['subject'], 'body': d['body'], 'position_keys': d['position_keys']}
                    for j in matching for d in (j['result'] or {}).get('drafts', [])]
        contacts = ([{'channel':'email', 'address':supplier['email'], 'source_url':supplier['url']}] if supplier.get('email') else []) + supplier.get('channels', [])
        status = 'answered' if answers else sent[-1]['status'] if sent else 'prepared' if messages and supplier.get('email') else 'contact_required'
        companies.append({'id':supplier['id'], 'name':supplier['company'], 'source_url':supplier['url'],
                          'draft_job_ids':[j['id'] for j in matching],
                          'image_url':f'/api/tenders/{tid}/buyer/image/{quote(supplier["id"], safe="")}?run_id={quote(run_id, safe="")}' if supplier.get('image') else '',
                          'contacts':contacts, 'prices':prices, 'messages':messages, 'status':status,
                          'position_keys':supplier['position_keys'], 'region_note':supplier['region_note'],
                          'outbox_ids':sorted(out_ids), 'reply_count':len(answers),
                          'send_details':[m['receipt']['detail'] for m in sent if m.get('receipt')],
                          'inbox':[incoming['checks'][key] for key in out_ids if key in incoming['checks']]})
    from contextlib import closing
    with closing(outbox.connect()) as db:
        errors = list(dict.fromkeys(r[0] for r in db.execute("SELECT error FROM buyer_search_steps WHERE run_id=? AND error<>''", (run_id,))))
    return {'schema_version':1, 'tender_id':tid, 'run_id':run_id, 'status':run['status'],
            'delivery':run['payload'].get('delivery','draft'), 'companies':companies, 'errors':errors,
            'request_revision':revision(run['payload']), 'request_current':request_current,'version_note':version_note,
            'positions':run['payload']['positions'],
            'uncovered':run['payload'].get('rejected', []) + prepared.get('uncovered', [])}


def plain(data):
    states = {'searching':'поиск', 'completed':'готово', 'partial':'частичный результат', 'canceled':'остановлено',
              'not_started':'не запускалось', 'prepared':'сообщение подготовлено', 'contact_required':'нужно подключение канала',
              'queued':'в очереди', 'sending':'отправляется', 'sent':'отправлено', 'uncertain':'отправка уточняется',
              'blocked':'не отправлено', 'answered':'получен ответ'}
    lines = [f"Подбор: {states.get(data['status'],data['status'])}. Компаний: {len(data['companies'])}."]
    if data.get('version_note'): lines.append(data['version_note'])
    position_names = {p['position_key']:p['name'] for p in data.get('positions',[])}
    for company in data['companies']:
        lines.extend(['', f"{company['name']} — {states.get(company['status'],company['status'])}",
                      'Контакт: '+('; '.join(c['channel']+': '+c['address'] for c in company['contacts']) or 'не найден')])
        if not company['prices']: lines.append('Цена: пока нет подтверждённого предложения')
        for price in company['prices']:
            value = format(Decimal(price['price_kopecks'])/100, '.2f') if price['price_kopecks'] is not None else 'не определена'
            lines.append(f"Цена: {value} ₽ / {price['unit']} · {position_names.get(price['position_key'],price['position_key'])} · "
                         + ('с сайта, требует подтверждения' if price['origin']=='website' else f"ответ, {price['state']}"))
        for message in company['messages']: lines.extend(['Сообщение: '+message['subject'], message['body']])
        lines.extend(company['send_details'])
        lines.append('Источник: '+company['source_url'])
    if data['uncovered']: lines += ['', 'Не покрыто:'] + [p['name']+' — '+p['reason'] for p in data['uncovered']]
    if data['errors']: lines += ['', 'Не удалось проверить:'] + data['errors']
    return '\n'.join(lines)+'\n'
