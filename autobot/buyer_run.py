"""Server-side CLI: one launch, durable background work, JSON/text results."""
import argparse
import json
import os
import re
from autobot.hermes_buyer import BuyerError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start','report','work'))
    parser.add_argument('--tender-id')
    parser.add_argument('--position', action='append', dest='positions')
    parser.add_argument('--run-id')
    parser.add_argument('--send', action='store_true', help='Authorize one grouped email per found company')
    parser.add_argument('--format', choices=('json','text'), default='json')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    # Importing the server to read the estimate must not spawn a second worker here.
    os.environ['BUYER_DISCOVERY_WORKER'] = '0'
    if args.action != 'work' and not re.fullmatch(r'\d{8,25}', args.tender_id or ''):
        parser.error('--tender-id must be an 8–25 digit identifier')
    try:
        if args.action == 'start':
            from autobot.buyer_workflow import current_source
            from autobot.buyer_store import enqueue
            with current_source(args.tender_id, args.positions) as source:
                run_id = enqueue(source, delivery='email' if args.send else 'draft')
            print(json.dumps({'ok':True,'run_id':run_id,'delivery':'email' if args.send else 'draft'}))
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
