"""Server-side import of observed URLs, never agent-generated prices or mail."""
import json
import os
import sys

os.environ['BUYER_DISCOVERY_WORKER']='0'


def main():
    from autobot.buyer_needs import revision
    from autobot.buyer_workflow import current_source
    from autobot import buyer_store
    payload=json.load(sys.stdin)
    expected=payload['source']
    assert expected['tender_id']=='0171200001926000664'
    assert 1<=len(expected['positions'])<=10
    keys=[row['position_key'] for row in expected['positions']]
    with current_source(expected['tender_id'],keys) as actual:
        if revision(actual)!=revision(expected):
            raise ValueError('Смета изменилась: старые наблюдения не импортированы')
        run_id=buyer_store.enqueue_sources(actual,payload['links'])
    print(json.dumps({'run_id':run_id,'delivery':'draft'}))


if __name__=='__main__':main()
