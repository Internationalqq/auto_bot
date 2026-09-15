"""Real process exits around SQLite commit and the uploaded-job OS lock."""
import json
from pathlib import Path
import subprocess
import sys
import time

from autobot import uploaded_estimates as store

SOURCE=Path(__file__).resolve().parents[1]
BOOT='''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
root=Path(sys.argv[2]); mode=sys.argv[3]; eid=sys.argv[4]
'''
PUBLISH='''import sqlite3,time
from autobot import uploaded_estimates as store
connect=sqlite3.connect
class Gate(sqlite3.Connection):
    def commit(self):
        if mode=='before':
            (root/'entered').write_text('before')
            while not (root/'release').exists():time.sleep(.03)
        super().commit()
        if mode=='after':
            (root/'entered').write_text('after')
            while not (root/'release').exists():time.sleep(.03)
store.sqlite3.connect=lambda *args,**kwargs:connect(*args,**dict(kwargs,factory=Gate))
store.publish(root,{'id':eid,'title':'Process fixture','row_count':1,'source_sha256':'f'*64},[{'name':'Work','total':20.01}])
'''
WORKER='''import time
from dataclasses import asdict
from autobot import web_ui, estimate_parse_worker as parser
from autobot.estimate_excel_analysis import EstimateRow
web_ui.REPO_ROOT=root
web_ui.USER_ESTIMATES_DIR=root/'uploads'
web_ui.USER_ESTIMATES_INDEX=root/'uploads/index.json'
web_ui.ESTIMATE_UPLOAD_JOBS_DIR=root/'uploads/.upload_jobs'
web_ui.estimate_upload_jobs={};web_ui.estimate_upload_workers=set()
source=root/'uploads'/eid/'source.xlsx';jid='b'*16
def fake_parse(*args,**kwargs):
    with (root/'calls').open('a') as output:output.write('parse\\n')
    if mode=='hold':
        (root/'entered').write_text('parse')
        while not (root/'release').exists():time.sleep(.03)
    return {'rows':[asdict(EstimateRow(idx=1,name='Process work',qty=1,unit='m2',total=20.01,position_id='excel:row2'))],
            'diagnostics':{},'sources':parser.snapshot([source])}
parser.run_uploaded_parser=fake_parse
if mode=='committed':
    def stop_after_commit(*args,**kwargs):
        (root/'entered').write_text('committed')
        while not (root/'release').exists():time.sleep(.03)
    web_ui._estimate_upload_complete=stop_after_commit
web_ui._run_estimate_upload_worker(jid,estimate_id=eid,title_raw='Process fixture',original_name=source.name,src_path=source)
'''


def start(code,root,mode='normal',eid='a'*16):
    return subprocess.Popen([sys.executable,'-c',BOOT+code,str(SOURCE),str(root),mode,eid],
                            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)


def wait_file(root,process):
    deadline=time.monotonic()+25
    while not (root/'entered').exists():
        assert process.poll() is None, process.communicate()
        assert time.monotonic()<deadline, 'Process did not reach checkpoint'
        time.sleep(.03)


def done(process):
    stdout,stderr=process.communicate(timeout=25)
    assert process.returncode==0,(stdout,stderr)


def stop(process):
    if process.poll() is None:process.kill()
    process.communicate(timeout=10)


def seed_job(root):
    eid='a'*16;jid='b'*16
    path=root/'uploads'/eid/'source.xlsx'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'local parser fixture')
    value={'job_id':jid,'target_estimate_id':eid,'source_path':str(path),'running':True,'ok':False,
           'progress':26,'attempts':0,'log_lines':[],'original_name':path.name,'title_raw':'Process fixture'}
    store.write_json(root/'uploads/.upload_jobs'/f'{jid}.json',value)


def status(root):
    return json.loads((root/'uploads/.upload_jobs'/('b'*16+'.json')).read_text(encoding='utf-8'))


def test_kill_before_commit_preserves_previous_estimates(tmp_path):
    store.publish(tmp_path,{'id':'c'*16,'title':'Previous','row_count':1,'source_sha256':'f'*64},[{'name':'Previous'}])
    process=start(PUBLISH,tmp_path,'before')
    try:
        wait_file(tmp_path,process);stop(process)
        assert store.meta(tmp_path,'a'*16) is None
        assert store.meta(tmp_path,'c'*16)['title']=='Previous'
        done(start(PUBLISH,tmp_path))
        assert len(store.catalogue(tmp_path))==2
    finally:stop(process)


def test_kill_after_commit_keeps_whole_result(tmp_path):
    process=start(PUBLISH,tmp_path,'after')
    try:
        wait_file(tmp_path,process);stop(process)
        assert store.meta(tmp_path,'a'*16)['row_count']==1
        assert store.rows(tmp_path,'a'*16)[0]['total']==20.01
        done(start(PUBLISH,tmp_path))
        assert len(store.catalogue(tmp_path))==1
    finally:stop(process)


def test_two_publications_do_not_lose_catalogue_entries(tmp_path):
    first=start(PUBLISH,tmp_path,eid='a'*16)
    second=start(PUBLISH,tmp_path,eid='c'*16)
    try:
        done(first);done(second)
        assert {row['id'] for row in store.catalogue(tmp_path)}=={'a'*16,'c'*16}
    finally:stop(first);stop(second)


def test_two_upload_workers_execute_one_parser(tmp_path):
    seed_job(tmp_path)
    first=start(WORKER,tmp_path,'hold')
    second=None
    try:
        wait_file(tmp_path,first)
        second=start(WORKER,tmp_path);done(second)
        assert (tmp_path/'calls').read_text().splitlines()==['parse']
        assert status(tmp_path)['running'] and status(tmp_path)['attempts']==1
        (tmp_path/'release').write_text('continue');done(first)
        assert status(tmp_path)['ok'] and len(store.catalogue(tmp_path/'uploads'))==1
    finally:
        stop(first)
        if second is not None:stop(second)


def test_interrupted_upload_can_resume_once_with_same_source(tmp_path):
    seed_job(tmp_path)
    first=start(WORKER,tmp_path,'hold')
    try:
        wait_file(tmp_path,first);stop(first)
        done(start(WORKER,tmp_path))
        assert status(tmp_path)['ok'] and status(tmp_path)['attempts']==2
        assert (tmp_path/'calls').read_text().splitlines()==['parse','parse']
        assert len(store.catalogue(tmp_path/'uploads'))==1
    finally:stop(first)


def test_committed_upload_recovers_status_without_reparsing(tmp_path):
    seed_job(tmp_path)
    first=start(WORKER,tmp_path,'committed')
    try:
        wait_file(tmp_path,first);stop(first)
        assert status(tmp_path)['running'] and len(store.catalogue(tmp_path/'uploads'))==1
        done(start(WORKER,tmp_path))
        assert status(tmp_path)['ok'] and not status(tmp_path)['running']
        assert status(tmp_path)['attempts']==1
        assert (tmp_path/'calls').read_text().splitlines()==['parse']
    finally:stop(first)
