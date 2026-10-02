"""Current-row procurement evidence, projected without changing money or history."""
from contextlib import closing
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import sqlite3
import time

from autobot import buyer_jobs, buyer_outbox, buyer_store
from autobot.buyer_needs import snapshot
from autobot.buyer_replies import comparison_amount, parse_price
from autobot.hermes_buyer import BuyerError

STAGES = ('candidates', 'comparable', 'contacts', 'sent', 'replied', 'confirmed')


def money(value):
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and amount > 0:
            return int((amount * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        pass
    return None


def need(row, tid, region):
    try:
        if 'requirements' in row:
            row = {**row, 'specification': {'requirements': row['requirements']}}
        return snapshot({'tender_id': tid, 'region': region, 'positions': [row]})['positions'][0]
    except (BuyerError, IndexError):
        return None


def mail_status(path=None, *, now=None):
    """Read only the deliberately public worker heartbeat, never its config."""
    if path is None:
        from autobot.buyer_mail_status import read
        local = read(now=now)
        if local is not None: return local
    path = path or buyer_outbox.DB_PATH.parent / 'mail-transport' / 'service-status.json'
    now = time.time() if now is None else now
    try:
        if path.stat().st_size > 16000:
            raise ValueError('oversized status')
        data = json.loads(path.read_text(encoding='utf-8'))
        stamp = float(data['checked_at'])
        if not 0 <= now - stamp < 1800:
            return {'state': 'offline', 'checked_at': stamp, 'label': 'Почтовый сервис давно не выходил на связь'}
        if data.get('ok') is True and data.get('receiving') is True and data.get('sending') is False:
            return {'state':'receiving','checked_at':stamp,'label':'Ответы проверяются; отправка приостановлена'}
        return {'state': 'ready' if data.get('ok') is True else 'blocked', 'checked_at': stamp,
                'label': 'Почта подключена' if data.get('ok') is True else 'Почта недоступна: требуется проверить подключение'}
    except (OSError, ValueError, KeyError, TypeError):
        return {'state': 'unknown', 'checked_at': None, 'label': 'Нет подтверждения подключения почты'}


def stored(tid):
    """A tender-scoped, read-only snapshot; no schema creation on this GET."""
    result = {'runs': [], 'outbox': [], 'replies': [], 'campaigns': []}
    path = buyer_outbox.DB_PATH.resolve()
    if not path.is_file():
        return result
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'buyer_search_runs' in tables:
            for record in db.execute('SELECT * FROM buyer_search_runs WHERE tender_id=? ORDER BY created_at', (tid,)):
                run = dict(record)
                run['payload'] = json.loads(run['payload'])
                run['candidates'] = [json.loads(r[0]) for r in db.execute('SELECT data FROM buyer_search_candidates WHERE run_id=?', (run['id'],))]
                run['errors'] = [dict(r) for r in db.execute('SELECT payload,error,status FROM buyer_search_steps WHERE run_id=? AND kind<>?', (run['id'], 'prepare'))]
                result['runs'].append(run)
        if 'outbound' in tables:
            result['outbox'] = [dict(r) for r in db.execute('SELECT * FROM outbound WHERE tender_id=? ORDER BY created_at', (tid,))]
        if 'buyer_replies' in tables:
            for record in db.execute('SELECT r.* FROM buyer_replies r JOIN outbound o ON o.id=r.outbound_id WHERE o.tender_id=? ORDER BY r.received_at', (tid,)):
                reply = dict(record)
                reply['prices'] = [dict(r) for r in db.execute('SELECT * FROM buyer_reply_prices WHERE reply_id=?', (reply['id'],))]
                result['replies'].append(reply)
        if 'buyer_campaigns' in tables:
            for record in db.execute('SELECT * FROM buyer_campaigns WHERE tender_id=?', (tid,)):
                campaign = dict(record)
                campaign['contacts'] = [dict(r) for r in db.execute('SELECT * FROM buyer_campaign_contacts WHERE campaign_id=?', (campaign['id'],))]
                result['campaigns'].append(campaign)
    return result


def stored_drafts(tid):
    """Keep older correspondence linked without migrating or limiting the queue."""
    path = buyer_jobs.DB_PATH.resolve()
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_market_jobs'").fetchone():
            return []
        return [{'id': key, 'payload': json.loads(payload), 'result': json.loads(result) if result else None}
                for key, payload, result in db.execute(
                    'SELECT id,payload_json,result_json FROM agent_market_jobs WHERE tender_id=?', (tid,))]


def project(tid, region, rows, *, drafts=(), runs=(), outbox=(), replies=(), campaigns=(), mail=None):
    """Flags are independent evidence, not a fabricated monotonic funnel."""
    current, result = {}, {}
    for row in rows:
        key = row['position_key']
        current[key] = need(row, tid, region) if row.get('price_state') != 'excluded' else None
        result[key] = {'position_key': key, 'name': row['name'], 'item_no': row.get('item_no', ''),
                       'unit': row.get('unit', ''), 'quantity': row.get('quantity'), 'type_label': row.get('type_label', ''),
                       'eligible': current[key] is not None, 'flags': {stage: False for stage in STAGES},
                       'offers': [], 'contacts': [], 'messages': [], 'price_checks': [], 'search_state': 'not_started',
                       'search_error': '', 'stale': False, 'updated_at': None, 'last_reply_at': None}
        entry = result[key]
        if not entry['eligible']:
            entry['reason'] = row.get('classification_reason') or 'Отдельная закупка не требуется или нужно уточнить строку'
            continue
        for source in row.get('sources', []):
            entry['offers'].append({'origin': 'website', 'supplier': source.get('source', ''),
                'url': source.get('url', ''), 'evidence': source.get('evidence', ''),
                'price_kopecks': money(source.get('comparison_price')), 'unit': row.get('unit', ''),
                'comparable': bool(source.get('verified') and row.get('verified_count') and money(source.get('comparison_price'))),
                'reason': source.get('reason', ''), 'observed_at': source.get('observed_at')})
        if row.get('market_processed'):
            entry['search_state'] = 'checked'

    def matching(payload):
        if str(payload.get('tender_id')) != str(tid) or str(payload.get('region', '')).strip() != str(region).strip():
            return set()
        return {p['position_key'] for p in payload.get('positions', []) if current.get(p.get('position_key')) is not None
                and need(p, tid, region) == current[p['position_key']]}

    from autobot.buyer_discovery import directory_source
    for run in runs:
        payload = run['payload']
        keys = matching(payload) if payload.get('discovery_version') == buyer_store.DISCOVERY_VERSION else set()
        for row in payload.get('positions', []):
            if row['position_key'] in result and row['position_key'] not in keys:
                result[row['position_key']]['stale'] = True
        for key in keys:
            result[key]['search_state'] = run['status']
            result[key]['search_error'] = ''
            result[key]['stale'] = False
            result[key]['updated_at'] = run.get('updated_at')
        for step in run.get('errors', []):
            data = json.loads(step['payload']) if isinstance(step['payload'], str) else step['payload']
            for key in keys.intersection(data.get('position_keys', [])):
                if step.get('error'):
                    result[key]['search_error'] = step['error']
        for company in run.get('candidates', []):
            if directory_source(company.get('url', '')):
                continue
            for key in keys.intersection(company.get('position_keys', [])):
                entry = result[key]
                entry['flags']['candidates'] = True
                entry['price_checks'].extend(p for p in company.get('price_checks',[]) if p.get('position_key')==key)
                contacts = ([{'channel': 'email', 'address': company['email'], 'source_url': company['url']}]
                            if company.get('email') else []) + company.get('channels', [])
                entry['contacts'].extend({**c, 'company': company['company'], 'checked_at': run.get('updated_at')} for c in contacts)
                for price in company.get('prices', []):
                    if price['position_key'] != key:
                        continue
                    from autobot.market_requirements import technical_conflict
                    amount = comparison_amount(price.get('price_kopecks'), price.get('unit'), entry['unit'])
                    reason = technical_conflict(entry['name'], price.get('evidence', ''))
                    if amount is None:
                        reason = 'Единица цены не соответствует смете'
                    entry['offers'].append({'origin': 'website', 'supplier': company['company'],
                        'url': price.get('source_url', ''), 'evidence': price.get('evidence', ''),
                        'price_kopecks': amount, 'unit': entry['unit'],
                        'comparable': bool(not reason and amount and price.get('state') == 'published' and price.get('source_url')),
                        'reason': reason or 'Опубликованная цена; наличие и доставку уточняем', 'observed_at': price.get('observed_at')})

    jobmap = {j['id']: j for j in drafts}
    jobkeys = {j['id']: matching(j['payload']['draft_task']) for j in drafts}

    def draft_keys(record):
        job = jobmap.get(record['draft_job_id'])
        if not job:
            return set()
        parts = (job.get('result') or {}).get('drafts', [])
        index = record['draft_index']
        if not isinstance(index, int) or not 0 <= index < len(parts):
            return set()
        return jobkeys[job['id']].intersection(parts[index]['position_keys'])

    messagekeys = {}
    for message in outbox:
        keys = draft_keys(message)
        messagekeys[message['id']] = keys
        for key in keys:
            entry = result[key]
            entry['messages'].append({k: message.get(k) for k in ('id', 'recipient', 'status', 'updated_at', 'created_at')})
            if message['status'] == 'sent':
                entry['flags']['sent'] = True
                entry['contacts'].append({'company': message['recipient'], 'channel': 'email', 'address': message['recipient'], 'source_url': '', 'checked_at': message['updated_at']})

    for campaign in campaigns:
        for key in draft_keys(campaign):
            for contact in campaign.get('contacts', []):
                if contact.get('contact') and not contact.get('error'):
                    result[key]['contacts'].append({'company': contact['company'], 'address': contact['contact'],
                        'channel': contact['channel'], 'source_url': contact['source_url'], 'checked_at': contact['checked_at']})

    # The latest reply price from each sender supersedes their older quote,
    # including a later ambiguous offer. A reply without price retains history.
    reply_offers = {}
    for reply in sorted(replies, key=lambda r: r.get('received_at') or 0):
        keys = messagekeys.get(reply['outbound_id'], set())
        for key in keys:
            result[key]['flags']['replied'] = True
            result[key]['last_reply_at'] = reply.get('received_at')
        for price in reply.get('prices', []):
            key = price['position_key']
            if key not in keys:
                continue
            original = json.loads(price['snapshot']) if isinstance(price['snapshot'], str) else price['snapshot']
            if need(original, tid, region) != current[key] or original.get('_request_region', region) != region:
                continue
            amount = comparison_amount(price.get('price_kopecks'), price.get('unit'), result[key]['unit'])
            check = parse_price({'quote': price['quote'], 'price': str(Decimal(price['price_kopecks']) / 100) if price.get('price_kopecks') is not None else '',
                                 **{k: price.get(k, '') for k in ('unit', 'vat', 'availability', 'delivery')}}, original, reply['raw_text'])
            comparable = price['state'] == 'comparable' and check[-2] == 'comparable' and amount is not None
            reply_offers[(key, reply['sender'].lower())] = {'origin': 'reply', 'supplier': reply['sender'], 'reply_id': reply['id'],
                'outbox_id': reply['outbound_id'], 'price_kopecks': amount, 'unit': result[key]['unit'],
                'comparable': comparable, 'evidence': price['quote'], 'reason': check[-1] or price['reason'],
                'vat': price['vat'], 'delivery': price['delivery'], 'availability': price['availability'], 'observed_at': reply['received_at']}
    for (key, _), price in reply_offers.items():
        result[key]['offers'].append(price)

    for entry in result.values():
        if not entry['eligible']:
            entry['state'], entry['label'] = 'excluded', 'Не участвует в подборе'
            continue
        # Deduplicate evidence and contacts across repeated searches.
        entry['contacts'] = list({(c['channel'], c['address'].casefold()): c for c in entry['contacts']}.values())
        entry['offers'] = list({(o['origin'], o.get('url'), o.get('reply_id'), o['price_kopecks']): o for o in entry['offers']}.values())
        flags = entry['flags']
        flags['candidates'] |= bool(entry['offers'])
        flags['contacts'] = bool(entry['contacts'])
        flags['comparable'] = any(o['comparable'] for o in entry['offers'])
        flags['confirmed'] = any(o['comparable'] and o['origin'] == 'reply' for o in entry['offers'])
        latest = max(entry['messages'], key=lambda m: m.get('updated_at') or 0, default={})
        if flags['confirmed']:
            state, label, reason = 'confirmed', 'Цена из ответа проверена', 'Товар и единица совпадают; условия поставки — в ответе'
        elif flags['replied'] and (entry['last_reply_at'] or 0) >= (latest.get('updated_at') or 0):
            state, label, reason = 'replied', 'Ответ получен', 'Подходящая цена в ответе пока не подтверждена'
        elif latest.get('status') in ('blocked', 'uncertain'):
            state, label, reason = latest['status'], 'Отправка требует внимания', 'Откройте переписку: автоматический повтор не выполняется'
        elif latest.get('status') in ('queued', 'sending'):
            state, label, reason = latest['status'], 'В очереди' if latest['status'] == 'queued' else 'Отправляется', 'Подтверждения отправки пока нет'
        elif flags['sent']:
            state, label, reason = 'sent', 'Ожидаем ответ', 'Отправлено уточнение после ответа поставщика' if flags['replied'] else 'Отправка подтверждена'
        elif flags['replied']:
            state, label, reason = 'replied', 'Ответ получен', 'Подходящая цена в ответе пока не подтверждена'
        elif flags['comparable']:
            state, label, reason = 'comparable', 'Цена подходит', 'Цена опубликована на сайте; поставщик её ещё не подтвердил'
        elif flags['contacts']:
            state, label, reason = 'contacts', 'Контакт найден', 'Можно подготовить запрос по этой позиции'
        elif flags['candidates']:
            failures=[p['reason'] for p in entry['price_checks'] if not p.get('accepted') and p.get('reason')]
            state, label, reason = 'candidates', 'Есть варианты', (failures[-1] if failures else 'Нужно проверить характеристики, единицу или цену')
        elif entry['search_state'] == 'searching':
            state, label, reason = 'searching', 'Идёт поиск', 'Результаты появятся автоматически'
        elif entry['search_error']:
            state, label, reason = 'error', 'Поиск завершён не полностью', entry['search_error']
        elif entry['stale']:
            state, label, reason = 'stale', 'Нужен новый подбор', 'Прежние результаты относятся к другой версии запроса'
        elif entry['search_state'] in ('checked', 'completed', 'partial', 'canceled'):
            state, label, reason = 'missing', 'Пока не найдено', 'Подходящих предложений нет в сохранённых результатах'
        else:
            state, label, reason = 'pending', 'Поиск не запускался', 'Выберите позицию для подбора'
        entry.update(state=state, label=label, reason=reason)
        entry['updated_at'] = max([entry['updated_at'] or 0, entry['last_reply_at'] or 0] + [m.get('updated_at') or 0 for m in entry['messages']] +
                                  [o['observed_at'] for o in entry['offers'] if isinstance(o.get('observed_at'), (int, float))]) or None
    positions = list(result.values())
    eligible = [p for p in positions if p['eligible']]
    return {'schema_version': 1, 'tender_id': tid, 'positions': positions,
            'coverage': {'denominator': len(eligible),
                         **{stage: round(100 * sum(p['flags'][stage] for p in eligible) / len(eligible), 1)
                            if eligible else 0 for stage in STAGES}},
            'summary': {'total': len(positions), 'eligible': len(eligible), 'excluded': len(positions) - len(eligible),
                        **{stage: sum(p['flags'][stage] for p in eligible) for stage in STAGES}},
            'mail': mail or {'state': 'unknown', 'label': 'Состояние почты не проверено', 'checked_at': None}}


def build(tid):
    from autobot import web_ui
    from autobot.estimate_publication_recovery import consistent_report
    with consistent_report(web_ui.REPORTS_DIR, tid):
        if not (web_ui.REPORTS_DIR / f'ОТЧЕТ_ПО_СМЕТАМ_{tid}.xlsx').is_file():
            raise BuyerError('Сначала загрузите и разберите смету')
        metadata = web_ui.load_tender_metadata().get(tid, {})
        tender = web_ui.build_tender_detail(tid, metadata, {})
        drafts = stored_drafts(tid)
        return project(tid, tender.get('region', ''), tender['positions'], drafts=drafts,
                       **stored(tid), mail=mail_status())
