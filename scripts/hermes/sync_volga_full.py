"""Windows relay for Mac observations through existing SSH access; no AI."""
import argparse
import json
from pathlib import Path
import subprocess
import time

REMOTE='/Users/egor/.hermes/profiles/commercial/workspace/volga-chrome-pilot-20261004/full-tender-20261004/run-state.json'


def save(path,value):
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(path)


def receipt_key(entry):
    # Preserve existing first-pass receipts; retries have their own delivery identity.
    key = entry['position_key']
    return key if entry.get('attempt', 1) == 1 else f"{key}:attempt-{entry['attempt']}"


def groups(state,source,synced):
    rows={r['position_key']:r for r in source['source']['positions']}
    ready=[b for b in state['batches'] if b.get('finished_at') and receipt_key(b) not in synced]
    batches, batch, keys = [], [], set()
    for entry in ready:
        if len(batch) == 10 or entry['position_key'] in keys:
            batches.append(batch)
            batch, keys = [], set()
        batch.append(entry)
        keys.add(entry['position_key'])
    if batch:
        batches.append(batch)
    for batch in batches:
        wanted=[b for b in batch if b.get('links')]
        payload={'source':{'tender_id':source['source']['tender_id'],'region':source['source']['region'],
                           'positions':[rows[b['position_key']] for b in wanted]},
                 'links':[link for b in wanted for link in b['links']]}
        yield batch,payload


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--key',type=Path,required=True)
    args=parser.parse_args()
    root=args.directory.resolve()
    source=json.loads((root/'volga-full-input-20261004.json').read_text(encoding='utf-8'))
    statusfile=root/'full-tender-sync.json'
    sync=json.loads(statusfile.read_text(encoding='utf-8')) if statusfile.exists() else {'synced':{},'started_at':time.time()}
    # One relay per output directory, including after a reconnect/restart.
    import msvcrt
    mutex=(root/'full-tender-sync.lock').open('a+b')
    if mutex.tell()==0:mutex.write(b'0');mutex.flush()
    mutex.seek(0)
    msvcrt.locking(mutex.fileno(),msvcrt.LK_NBLCK,1)
    mac=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=15','mac-mini-hermes']
    server=['ssh','-i',str(args.key),'-o','BatchMode=yes','-o','ConnectTimeout=15','root@193.187.94.165',
            'docker exec -i -w /app -e PYTHONPATH=/app -e BUYER_DISCOVERY_WORKER=0 pmbi-autobot python /app/data/buyer-pilot-evidence/import_volga_observations.py']
    deadline=sync.get('deadline',sync['started_at']+38*3600)
    while time.time()<deadline and not (root/'full-tender-sync-stop').exists():
        try:
            result=subprocess.run(mac+['cat '+REMOTE],capture_output=True,text=True,encoding='utf-8',timeout=40,check=True)
            state=json.loads(result.stdout)
            deadline=state.get('deadline',deadline-7200)+7200
            sync['deadline']=deadline
            save(root/'full-tender-state.json',state)
            for batch,payload in groups(state,source,sync['synced']):
                receipt={'reason':'Наблюдаемые URL отсутствуют'}
                if payload['links']:
                    result=subprocess.run(server,input=json.dumps(payload,ensure_ascii=False),capture_output=True,
                                          text=True,encoding='utf-8',timeout=100,check=True)
                    receipt=json.loads(result.stdout)
                for item in batch:sync['synced'][receipt_key(item)]=receipt
                save(statusfile,sync)
            sync.update(status='synced',updated_at=time.time(),mac_status=state['status'],completed=state['completed'],total=state['total'])
            sync.update(phase=state.get('phase','first_pass'),retry_completed=state.get('retry_completed',0),
                        retry_total=state.get('retry_total',0),attempts_completed=state.get('attempts_completed',state['completed']))
            sync.pop('error',None)
            save(statusfile,sync)
            if state['status']=='finished':
                sync['status']='finished';save(statusfile,sync);return
            if state['status'] in ('needs_attention','stopped','interrupted','time_limit'):
                sync['status']='needs_attention';save(statusfile,sync);return
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            sync.update(status='retrying',updated_at=time.time(),error=str(exc)[:400])
            save(statusfile,sync)
        time.sleep(90)
    sync['status']='stopped';save(statusfile,sync)


if __name__=='__main__':main()
