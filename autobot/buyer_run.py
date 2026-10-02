"""Server-side CLI: one launch, durable background work, JSON/text results."""
import argparse
import json
import os
import re
from autobot.hermes_buyer import BuyerError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start','report','work','catalog','sources','status'))
    parser.add_argument('--tender-id')
    parser.add_argument('--position', action='append', dest='positions')
    parser.add_argument('--run-id')
    parser.add_argument('--send', action='store_true', help='Authorize one grouped email per found company')
    parser.add_argument('--format', choices=('json','text'), default='json')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--sources-file', help='JSON list of observed URLs and position_keys; draft-only')
    parser.add_argument('--missing', action='store_true', help='Only positions without a comparable price')
    args = parser.parse_args(argv)
    # Importing the server to read the estimate must not spawn a second worker here.
    os.environ['BUYER_DISCOVERY_WORKER'] = '0'
    if args.action != 'work' and not re.fullmatch(r'\d{8,25}', args.tender_id or ''):
        parser.error('--tender-id must be an 8–25 digit identifier')
    try:
        if args.missing:
            if args.action != 'start' or args.positions:
                raise BuyerError('--missing применим только к start без --position')
            from autobot.buyer_pipeline import build
            args.positions = [p['position_key'] for p in build(args.tender_id)['positions']
                              if p['eligible'] and not p['flags']['comparable']]
            if not args.positions:
                print(json.dumps({'ok':True,'run_id':None,'position_count':0}))
                return 0
        if args.action == 'start':
            from autobot.buyer_workflow import current_source
            from autobot.buyer_store import enqueue
            with current_source(args.tender_id, args.positions) as source:
                run_id = enqueue(source, delivery='email' if args.send else 'draft')
            print(json.dumps({'ok':True,'run_id':run_id,'delivery':'email' if args.send else 'draft'}))
        elif args.action == 'status':
            from autobot.buyer_pipeline import build
            result = build(args.tender_id)
            print(json.dumps({k:result[k] for k in ('tender_id','summary','coverage','mail')}, ensure_ascii=False))
        elif args.action in ('catalog','sources'):
            from autobot.buyer_workflow import current_source
            from autobot.buyer_catalog import catalog
            from autobot.buyer_store import enqueue_sources
            from pathlib import Path
            if args.action == 'sources' and (not args.sources_file or args.send):
                raise BuyerError('sources требует --sources-file и не поддерживает --send')
            try:
                links = json.loads(Path(args.sources_file).read_text(encoding='utf-8-sig')) if args.action == 'sources' else None
            except (OSError, ValueError) as error:
                raise BuyerError('Не удалось прочитать JSON со ссылками: ' + str(error)) from None
            keys = args.positions
            if links is not None:
                if not isinstance(links, list) or any(not isinstance(p, dict) or not isinstance(p.get('position_keys'), list) for p in links):
                    raise BuyerError('Неверный формат источников')
                keys = list(dict.fromkeys(k for p in links for k in p['position_keys']))
            if args.action == 'catalog' and keys is None:
                from autobot.buyer_pipeline import build
                keys = [p['position_key'] for p in build(args.tender_id)['positions'] if p['eligible']]
            with current_source(args.tender_id, keys) as source:
                result = catalog(source) if args.action == 'catalog' else {'run_id': enqueue_sources(source, links), 'delivery':'draft'}
            print(json.dumps({'ok':True, **result}, ensure_ascii=False))
        elif args.action == 'report':
            from autobot.buyer_report import build, plain
            result = build(args.tender_id, args.run_id)
            print(plain(result) if args.format == 'text' else json.dumps(result, ensure_ascii=False))
        else:
            import time
            from autobot.buyer_discovery import run_once
            while True:
                run_once()
                if args.once: break
                time.sleep(1)
        return 0
    except BuyerError as error:
        print(json.dumps({'ok':False,'error':str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__': raise SystemExit(main())
