"""Actual process termination at the correction's existing publication boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if __name__ != '__main__':
    import pytest
    from test_tender_corrections import context, data, ACTOR, TID


def child(config_path, stop_at):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    config=json.loads(Path(config_path).read_text(encoding='utf-8'))
    from autobot import main,tender_corrections as edit,estimate_publication_recovery as recovery
    paths={name:Path(value) for name,value in config['paths'].items()}
    original=recovery.os.replace
    def replace(source,target):
        result=original(source,target)
        target=Path(target)
        reached=(stop_at=='before-commit' and target.parent==paths['reports'] and target.name==recovery.output_names(config['tid'])[1])
        if stop_at=='after-commit' and target.name=='PUBLICATION_'+config['tid']+'.json':
            reached=json.loads(target.read_text(encoding='utf-8'))['phase']=='committed'
        if reached:
            Path(config['ready']).write_text('ready')
            while True:time.sleep(.1)
        return result
    recovery.os.replace=replace
    tender=main.Tender(config['tid'],'Монтаж окон','','','',100000,None)
    try:
        result,duplicate=edit.apply(tender,paths,actor=config['actor'],**config['body'])
        payload={'revision':result['revision'],'version':result['version'],'duplicate':duplicate}
    except edit.CorrectionError as error:payload={'error':error.status}
    except TimeoutError:payload={'error':503}
    print(json.dumps(payload),flush=True)


if __name__=='__main__':
    child(sys.argv[1],sys.argv[2])
else:
    @pytest.mark.parametrize('stop_at',['before-commit','after-commit'])
    def test_killed_correction_recovers_one_publication(context,tmp_path,stop_at):
        from autobot import tender_corrections as edit
        paths,source,tender=context
        body=data(context)
        before=edit.snapshot(paths['reports'],TID)
        original=source.read_bytes()
        ready=tmp_path/'ready'
        config=tmp_path/'process.json'
        config.write_text(json.dumps({'paths':{name:str(path) for name,path in paths.items()},
            'tid':TID,'actor':ACTOR,'body':body,'ready':str(ready)},ensure_ascii=False),encoding='utf-8')
        process=subprocess.Popen([sys.executable,'-X','utf8',__file__,str(config),stop_at],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+30
            while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.02)
            assert ready.exists(),process.communicate(timeout=2)
            process.kill();process.communicate(timeout=10)
            current=edit.snapshot(paths['reports'],TID)
            if stop_at=='before-commit':
                assert current['rows']==before['rows'] and current['revision']==0
            else:
                assert current['rows'][0]['total']=='25.01' and current['revision']==1
            saved,duplicate=edit.apply(tender,paths,actor=ACTOR,**body)
            assert saved['revision']==1 and duplicate==(stop_at=='after-commit')
            assert edit.snapshot(paths['reports'],TID)['revision']==1
            assert source.read_bytes()==original
            assert not list(paths['reports'].glob('PUBLICATION_*.json'))
            assert not list(paths['reports'].glob('.autobot-parse-*'))
        finally:
            if process.poll() is None:process.kill()
            process.communicate(timeout=10)


    def test_two_processes_with_one_operation_leave_one_revision(context,tmp_path):
        from autobot import tender_corrections as edit
        paths,_,tender=context
        body=data(context)
        config=tmp_path/'concurrent.json'
        config.write_text(json.dumps({'paths':{name:str(path) for name,path in paths.items()},
            'tid':TID,'actor':ACTOR,'body':body,'ready':str(tmp_path/'unused')},ensure_ascii=False),encoding='utf-8')
        processes=[subprocess.Popen([sys.executable,'-X','utf8',__file__,str(config),'none'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                   for _ in range(2)]
        try:
            results=[]
            for process in processes:
                output,error=process.communicate(timeout=30)
                assert process.returncode==0,error.decode('utf-8','replace')
                results.append(json.loads(output.decode('utf-8').strip().splitlines()[-1]))
            assert sum(result.get('duplicate') is False for result in results)==1
            assert all(result.get('revision')==1 or result.get('error')==503 for result in results)
            result,duplicate=edit.apply(tender,paths,actor=ACTOR,**body)
            assert result['revision']==1 and duplicate
            assert len(edit.history(edit.snapshot(paths['reports'],TID)))==1
        finally:
            for process in processes:
                if process.poll() is None:process.kill()
                process.communicate(timeout=10)
