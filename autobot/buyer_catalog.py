"""Shared public supplier observations, derived from immutable discovery history.

Private correspondence, buyer quantities and tender IDs never enter this view.
Only source URLs are reused by discovery; every new request inspects them again.
"""
from contextlib import closing
import json
import re
import sqlite3
import time
from urllib.parse import urlsplit

from autobot import buyer_outbox
from autobot.buyer_needs import digest

FRESH_SECONDS = 7 * 86400
HISTORY_LIMIT = 5000


def identity(row, region=''):
    def normalize(value):
        return re.sub(r'\s+', ' ', str(value or '').casefold().replace('ё', 'е').replace('×', 'х')).strip()
    kind = row.get('type_slug')
    return digest([normalize(row.get('name')), normalize(row.get('unit')), kind,
                   normalize(region) if kind in ('work', 'service') else ''])


def catalog(source, *, now=None):
    from autobot.buyer_discovery import public_url, directory_source, checked_candidate
    from autobot.hermes_buyer import BuyerError
    now = time.time() if now is None else now
    wanted = {identity(p, source.get('region')) for p in source['positions']}
    path = buyer_outbox.DB_PATH.resolve()
    empty = {'items': [], 'truncated': False, 'fresh_seconds': FRESH_SECONDS}
    if not path.is_file():
        return empty
    observations = {}
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='buyer_search_candidates'").fetchone():
            return empty
        rows = db.execute('''SELECT r.payload,c.data FROM buyer_search_candidates c
            JOIN buyer_search_runs r ON r.id=c.run_id
            ORDER BY r.updated_at DESC,c.party_id LIMIT ?''', (HISTORY_LIMIT + 1,)).fetchall()
    for payload_json, candidate_json in rows[:HISTORY_LIMIT]:
        payload, raw = json.loads(payload_json), json.loads(candidate_json)
        candidate = checked_candidate(raw, payload['positions'])
        if directory_source(candidate.get('url', '')):
            continue
        positions = {p['position_key']: p for p in payload['positions']}
        pages = {p['url']: p for p in candidate.get('evidence_pages', [])}
        checks = candidate.get('price_checks', [])
        for key in candidate.get('position_keys', []):
            row = positions.get(key)
            if row is None or identity(row, payload.get('region')) not in wanted:
                continue
            # Keep even a rejected price source: it can still identify a supplier
            # to recheck. Never elevate it to a usable price.
            related = [p for p in checks if p.get('position_key') == key]
            if not related and candidate.get('url') in pages:
                related = [{'source_url': candidate['url'], 'observed_at': pages[candidate['url']].get('checked_at'),
                            'accepted': False, 'reason': 'Цена на странице не подтверждена'}]
            for check in related:
                try:
                    url = public_url(check['source_url'])
                except (BuyerError, KeyError):
                    continue
                page = pages.get(url)
                stamp = check.get('observed_at') or (page or {}).get('checked_at')
                if not page or not isinstance(stamp, (float, int)) or not 0 < stamp <= now + 60 or directory_source(url):
                    continue
                price = next((p for p in candidate.get('prices', []) if p.get('position_key') == key and p.get('source_url') == url), {})
                public_contacts = []
                for contact in candidate.get('channels', []):
                    if contact.get('source_url') in pages and contact.get('channel') in ('phone', 'email', 'telegram', 'whatsapp', 'max', 'avito'):
                        public_contacts.append({k: contact.get(k, '') for k in ('channel', 'address', 'source_url')})
                # Email must occur in the retained public-page excerpt. A stored
                # preferred recipient alone is not proof of public provenance.
                for email in candidate.get('emails', []):
                    evidence = next((p for p in pages.values() if email in p.get('excerpt', '')), None)
                    if evidence:
                        public_contacts.append({'channel': 'email', 'address': email, 'source_url': evidence['url']})
                observation = {'checked_at': stamp,
                    'price_kopecks': price.get('price_kopecks') if check.get('accepted') else None,
                    'unit': price.get('unit', check.get('unit', '')), 'vat': price.get('vat', ''),
                    'state': 'website' if price and check.get('accepted') else 'review',
                    'reason': check.get('reason', ''), 'evidence': check.get('evidence', '')}
                product = identity(row, payload.get('region'))
                token = (product, url)
                item = observations.setdefault(token, {'product_key': product, 'name': row['name'],
                    'type_slug': row['type_slug'], 'requested_unit': row['unit'],
                    'supplier': urlsplit(url).hostname, 'url': url, 'observations': []})
                if observation not in item['observations']:
                    item['observations'].append(observation)
                if stamp >= item.get('checked_at', 0):
                    item.update(checked_at=stamp, contacts=public_contacts)
    items = []
    for item in observations.values():
        item['observations'].sort(key=lambda x: x['checked_at'], reverse=True)
        item['latest'] = item['observations'][0]
        item['freshness'] = 'recently_checked' if now - item['checked_at'] <= FRESH_SECONDS else 'needs_refresh'
        items.append(item)
    items.sort(key=lambda x: (-x['checked_at'], x['url']))
    return {'items': items, 'truncated': len(rows) > HISTORY_LIMIT, 'fresh_seconds': FRESH_SECONDS}


def links(task, source):
    from autobot.buyer_needs import SOURCES_PER_QUERY
    from autobot.supplier_catalog_match import lookup
    selected = {p for p in task.get('position_keys', [])}
    subset = {**source, 'positions': [p for p in source['positions'] if p['position_key'] in selected]}
    found = {}
    for row in subset['positions']:
        for offer in lookup(name=row['name'], unit=row['unit'], region=source.get('region', ''),
                            quantity=row.get('quantity'), include_candidates=True, limit=SOURCES_PER_QUERY):
            found.setdefault(offer['url'], {'url': offer['url'], 'title': offer.get('title', ''), 'reused': True})
    for item in catalog(subset)['items']:
        found.setdefault(item['url'], {'url': item['url'], 'title': item['supplier'], 'reused': True})
    return list(found.values())[:SOURCES_PER_QUERY]


def publish_pages(candidate, captures, task, *, path=None):
    """Feed proven public captures into the existing price base, never buyer text.

    Called only after a valid discovery lease commits. Catalogue unavailability
    must not discard the already persisted supplier discovery result.
    """
    from autobot import supplier_catalog_store as store
    from autobot.supplier_catalog_extract import extract
    from autobot.buyer_discovery import page_facts, public_url, directory_source
    from autobot.market_source_adapters import source_region_evidence
    if not store.ready(path):
        return 0
    root = public_url(candidate['url'])
    if directory_source(root) or root not in captures:
        return 0
    body, stamp = captures[root]
    facts = page_facts(root, body)
    if not facts['heading'] or facts['editorial']:
        return 0
    # Actual public heading, not the requested estimate name: M1400 must never
    # enter the common base under a buyer's requested M1200 label.
    config = {'name': facts['name'], 'url': root,
              'catalog': {'adapter': 'product', 'bucket': task['bucket']}}
    records = extract(body, root, 'product', facts['heading'], config)
    source_id = store.digest(root)
    with store.connect(path, write=True) as db:
        db.execute('''INSERT OR IGNORE INTO supplier_catalog_sources
            (id,supplier_id,name,url,bucket,region_label,config_json,enabled,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (source_id, urlsplit(root).hostname.removeprefix('www.'), facts['name'], root,
             task['bucket'], 'Регион и доставка уточняются', store.json_text(config), 1, stamp))
        source_id = db.execute('SELECT id FROM supplier_catalog_sources WHERE url=?', (root,)).fetchone()[0]
    store.save_page(source_id, root, body, stamp, records, kind='product', path=path)
    proven = {page['url'] for page in candidate.get('evidence_pages', [])}
    for url, (html, observed) in captures.items():
        if url not in proven:
            continue
        public = page_facts(url, html)
        contacts = ' · '.join(dict.fromkeys([*public['emails'], *[c['address'] for c in public['channels']]]))
        if contacts:
            store.save_contact(source_id, contacts, url, observed, path)
        proof = source_region_evidence(html, '', task['bucket'])
        store.save_coverage(source_id, proof, url, observed, path=path)
        if url != root:
            store.save_page(source_id, url, html, observed, [], kind='context', path=path)
    return len(records)
