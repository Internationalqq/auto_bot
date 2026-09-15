"""Shared isolated process fixture for pytest and final Linux-image checks."""
import json
import os
import subprocess
import sys
from pathlib import Path
from autobot import estimate_publication_recovery as recovery

TID = '12345678901'
RUN = 'a' * 32


def prepare(root, *, previous=True):
    reports = root / 'reports'
    reports.mkdir()
    stage = reports / '.autobot-parse-fixture1'
    stage.mkdir()
    old, new = {}, {}
    for i, name in enumerate(recovery.output_names(TID)):
        old[name] = f'previous-{i}'.encode()
        new[name] = f'next-{i}'.encode()
        if name.startswith('PARSE_RUN_'):
            old[name] = json.dumps({'schema_version': 1, 'tender_id': TID, 'state': 'running', 'run_id': RUN}).encode()
            new[name] = json.dumps({'schema_version': 1, 'tender_id': TID, 'state': 'complete', 'run_id': RUN}).encode()
        if previous:
            (reports / name).write_bytes(old[name])
        (stage / name).write_bytes(new[name])
    return reports, stage, old, new


def child(root, action, crash=''):
    code = '''
from pathlib import Path
import os, signal, sys
sys.path.insert(0,sys.argv[4])
from autobot import estimate_publication_recovery as r
from autobot.atomic_output import output_lock
root=Path(sys.argv[1]); action=sys.argv[2]; crash=sys.argv[3]
reports=root/'reports'; tid='12345678901'; stage=reports/'.autobot-parse-fixture1'
replace=r.os.replace
counter=[0]
def crash_now():
    if os.name != 'nt': os.kill(os.getpid(), signal.SIGKILL)
    os._exit(83)
def replaced(source,target):
    result=replace(source,target)
    if Path(target).parent==reports and Path(target).name in r.output_names(tid):
        counter[0]+=1
        if str(counter[0])==crash:crash_now()
    return result
r.os.replace=replaced
save=r._save
def saved(path,data):
    save(path,data)
    if crash=='committed' and data.get('phase')=='committed':crash_now()
    if crash=='recovered_status' and data.get('publication_recovered'):crash_now()
r._save=saved
if action=='publish':
    with output_lock(reports/r.output_names(tid)[1]):
        r.activate([stage/name for name in r.output_names(tid)],reports,stage)
else:
    r.recover_publication(reports,tid)
'''
    result = subprocess.run([sys.executable, '-c', code, str(root), action, crash, str(Path(__file__).resolve().parents[1])], capture_output=True, text=True)
    assert result.returncode == ((83 if os.name == 'nt' else -9) if crash else 0), result.stdout + result.stderr
