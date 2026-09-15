"""Actual process exits and concurrent HTTP-receipt boundaries, without providers."""
import io
import json
from pathlib import Path
import subprocess
import sys
import time

from autobot import upload_admission as admission

SOURCE = Path(__file__).resolve().parents[1]
KEY = 'b' * 32
BOOT = '''import io,json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from autobot import upload_admission as admission, uploaded_estimates as store
root=Path(sys.argv[2]); mode=sys.argv[3]; marker=root/sys.argv[4]
key='b'*32; jobs=root/'jobs'
write=store.write_json
def paused(path,value):
    is_receipt=path.parent.name=='admissions'
    is_job=path.parent==jobs
    if mode=='source' and is_job:
        marker.write_text('source');time.sleep(180)
    write(path,value)
    if (mode=='receipt' and is_receipt) or (mode=='job' and is_job):
        marker.write_text(mode);time.sleep(180)
store.write_json=paused
replace=admission.os.replace
def paused_replace(source,target):
    if mode=='partial' and Path(source).name.startswith('.receiving-'):
        marker.write_text('partial');time.sleep(180)
    replace(source,target)
admission.os.replace=paused_replace
if mode=='parallel':
    marker.write_text('ready')
    until=time.monotonic()+20
    while not (root/'go').exists():
        if time.monotonic()>until: raise RuntimeError('gate timeout')
        time.sleep(.02)
job,duplicate=admission.receive(io.BytesIO(b'one source'),key=key,original_name='source.xlsx',title='School',
    source_root=root/'estimates',jobs_dir=jobs,repo_root=root,max_bytes=1024)
print(json.dumps({'job_id':job['job_id'],'duplicate':duplicate}),flush=True)
'''


def start(root, mode, marker='ready'):
    return subprocess.Popen([sys.executable,'-X','utf8','-c',BOOT,str(SOURCE),str(root),mode,marker],
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')


def wait_marker(path):
    deadline = time.monotonic() + 20
    while not path.exists():
        if time.monotonic() > deadline: raise AssertionError('Admission process did not reach boundary')
        time.sleep(.03)


def stop(process):
    if process.poll() is None: process.kill()
    process.communicate(timeout=10)


def receive(root):
    return admission.receive(io.BytesIO(b'one source'),key=KEY,original_name='source.xlsx',title='School',
                             source_root=root/'estimates',jobs_dir=root/'jobs',repo_root=root,max_bytes=1024)


def test_kill_after_reservation_requires_same_file(tmp_path):
    process=start(tmp_path,'receipt')
    try: wait_marker(tmp_path/'ready')
    finally: stop(process)
    assert not (tmp_path/'estimates'/KEY/'source.xlsx').exists()
    try: admission.restore(tmp_path/'jobs',tmp_path/'estimates',tmp_path,KEY)
    except admission.AdmissionError as error: assert error.retry_upload
    else: raise AssertionError('Missing source was accepted')
    job,duplicate=receive(tmp_path)
    assert duplicate and job['job_id']==KEY


def test_kill_after_source_recovers_job_without_second_upload(tmp_path):
    process=start(tmp_path,'source')
    try: wait_marker(tmp_path/'ready')
    finally: stop(process)
    assert not (tmp_path/'jobs'/(KEY+'.json')).exists()
    job=admission.restore(tmp_path/'jobs',tmp_path/'estimates',tmp_path,KEY)
    assert job['job_id']==KEY and job['running']
    assert (tmp_path/'estimates'/KEY/'source.xlsx').read_bytes()==b'one source'


def test_kill_before_source_rename_cleans_only_own_temporary_file(tmp_path):
    process=start(tmp_path,'partial')
    try: wait_marker(tmp_path/'ready')
    finally: stop(process)
    folder=tmp_path/'estimates'/KEY
    assert len(list(folder.glob('.receiving-*.tmp')))==1
    unrelated=folder/'keep.txt';unrelated.write_text('keep')
    job,duplicate=receive(tmp_path)
    assert duplicate and job['job_id']==KEY
    assert not list(folder.glob('.receiving-*.tmp')) and unrelated.read_text()=='keep'


def test_kill_before_response_reuses_complete_job(tmp_path):
    process=start(tmp_path,'job')
    try: wait_marker(tmp_path/'ready')
    finally: stop(process)
    path=tmp_path/'jobs'/(KEY+'.json'); before=path.read_bytes()
    job,duplicate=receive(tmp_path)
    assert duplicate and job['job_id']==KEY and path.read_bytes()==before


def test_two_processes_share_one_receipt_source_and_job(tmp_path):
    processes=[start(tmp_path,'parallel','first'),start(tmp_path,'parallel','second')]
    try:
        wait_marker(tmp_path/'first');wait_marker(tmp_path/'second')
        (tmp_path/'go').write_text('go')
        replies=[]
        for process in processes:
            output,error=process.communicate(timeout=25)
            assert process.returncode==0,error
            replies.append(json.loads(output))
        assert sorted(row['duplicate'] for row in replies)==[False,True]
        assert {row['job_id'] for row in replies}=={KEY}
        assert len(list((tmp_path/'estimates').glob('*/source.xlsx')))==1
        assert len(list((tmp_path/'jobs').glob('*.json')))==1
    finally:
        for process in processes: stop(process)
