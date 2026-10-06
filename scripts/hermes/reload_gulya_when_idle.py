"""Apply updated tools to Gulya's gateway after its current turn ends."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time


def main():
    home=Path('/Users/egor/.hermes/profiles/gulya')
    team=Path('/Users/egor/.hermes/team-browser-access')
    report=team/'gulya-recovery-reload.json'
    initial=json.loads((home/'gateway.pid').read_text())['pid']
    for _ in range(120):
        pid=json.loads((home/'gateway.pid').read_text())['pid']
        if pid!=initial:
            report.write_text(json.dumps({'status':'reloaded','pid':pid,'updated_at':time.time()}));return
        active=team/'state/active.json'
        busy=active.exists() and json.loads(active.read_text()).get('owner')=='gulya'
        with sqlite3.connect('file:'+str(home/'state.db')+'?mode=ro',uri=True) as db:
            sessions=db.execute('select distinct session_id from messages where timestamp>?',(time.time()-1200,)).fetchall()
            for (sid,) in sessions:
                last=db.execute('select role,tool_calls from messages where session_id=? order by id desc limit 1',(sid,)).fetchone()
                if last and (last[0]!='assistant' or last[1] not in (None,'','[]')):busy=True
        if not busy:
            service=subprocess.run(['launchctl','print',f'gui/{os.getuid()}/ai.hermes.gateway-gulya'],capture_output=True,text=True)
            if service.returncode or f'pid = {pid}' not in service.stdout:
                report.write_text(json.dumps({'status':'unverified_service','updated_at':time.time()}));return
            os.kill(pid,signal.SIGUSR1)
            report.write_text(json.dumps({'status':'reload_requested','old_pid':pid,'updated_at':time.time()}))
            # No repeated signal if graceful draining takes time.
            for _ in range(60):
                time.sleep(5)
                new=json.loads((home/'gateway.pid').read_text())['pid']
                if new!=pid:
                    report.write_text(json.dumps({'status':'reloaded','pid':new,'updated_at':time.time()}));return
            return
        report.write_text(json.dumps({'status':'waiting_for_current_task','pid':pid,'updated_at':time.time()}))
        time.sleep(30)
    report.write_text(json.dumps({'status':'deferred_active_task','updated_at':time.time()}))


if __name__=='__main__':main()
