"""Real process termination around a correction's commit and concurrent save."""
from pathlib import Path
import subprocess
import sys
import time
from autobot import uploaded_corrections as edit, uploaded_estimates as store

SOURCE=Path(__file__).resolve().parents[1]
EID='a'*16
BOOT='''import sys,time
from pathlib import Path
from contextlib import contextmanager
sys.path.insert(0,sys.argv[1])
from autobot import uploaded_corrections as edit
root=Path(sys.argv[2]);mode=sys.argv[3];marker=root/sys.argv[4]
original=edit.connection
@contextmanager
def pause(*args,**kwargs):
    with original(*args,**kwargs) as con:
        yield con
        if kwargs.get('write'):marker.write_text('before commit');time.sleep(180)
if mode=='before':edit.connection=pause
value=edit.snapshot(root,'a'*16)
if mode=='parallel':
    marker.write_text('ready');until=time.monotonic()+20
    while not (root/'go').exists():
        if time.monotonic()>until:raise RuntimeError('gate timeout')
        time.sleep(.02)
try:
    saved,duplicate=edit.apply(root,'a'*16,position_id='source:1',changes={'total':'25.01'},expected_version=value['version'],
        operation_id='b'*32,reason='Verified source',actor={'id':7,'name':'Author'})
    if mode=='after':marker.write_text('after commit');time.sleep(180)
    print(str(duplicate),flush=True)
except edit.CorrectionError as error:
    print(str(error.status),flush=True)
'''

def seed(root):
    store.write_json(root/EID/'meta.json',{'id':EID,'title':'Process fixture','row_count':1})
    store.write_json(root/EID/'rows.json',[{'name':'Concrete','position_id':'source:1','type':'material','unit':'m3','qty':2,'unit_price':10,'total':20}])

def start(root,mode,marker='ready'):
    return subprocess.Popen([sys.executable,'-X','utf8','-c',BOOT,str(SOURCE),str(root),mode,marker],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')

def wait(path):
    until=time.monotonic()+25
    while not path.exists():
        if time.monotonic()>until:raise AssertionError('Correction process did not reach boundary')
        time.sleep(.02)

def kill(process):
    if process.poll() is None:process.kill()
    process.communicate(timeout=10)

def test_exit_before_commit_leaves_the_original_revision(tmp_path):
    seed(tmp_path);process=start(tmp_path,'before')
    try:wait(tmp_path/'ready')
    finally:kill(process)
    current=edit.snapshot(tmp_path,EID)
    assert current['revision']==0 and current['rows'][0]['total']==20 and edit.history(tmp_path,EID)==[]

def test_exit_after_commit_keeps_one_confirmed_correction(tmp_path):
    seed(tmp_path);before=edit.snapshot(tmp_path,EID)['version'];process=start(tmp_path,'after')
    try:wait(tmp_path/'ready')
    finally:kill(process)
    current=edit.snapshot(tmp_path,EID)
    assert current['revision']==1 and current['rows'][0]['total_kopecks']==2501
    result,duplicate=edit.apply(tmp_path,EID,position_id='source:1',changes={'total':'25.01'},expected_version=before,
        operation_id='b'*32,reason='Verified source',actor={'id':7,'name':'Author'})
    assert duplicate and result['version']==current['version'] and len(edit.history(tmp_path,EID))==1

def test_two_processes_retry_one_correction_without_duplicates(tmp_path):
    seed(tmp_path);one=start(tmp_path,'parallel','one');two=start(tmp_path,'parallel','two')
    try:
        wait(tmp_path/'one');wait(tmp_path/'two');(tmp_path/'go').write_text('go')
        outputs=[process.communicate(timeout=20)[0].strip() for process in (one,two)]
        assert sorted(outputs)==['False','True'] and one.returncode==two.returncode==0
    finally:kill(one);kill(two)
    assert edit.snapshot(tmp_path,EID)['revision']==1 and len(edit.history(tmp_path,EID))==1
