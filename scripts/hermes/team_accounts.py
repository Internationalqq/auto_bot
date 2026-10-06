"""Named web accounts for Hermes; passwords enter only via getpass -> vault stdin."""
import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import headless_control as control

BASE=control.BASE
CONFIG=BASE/'team-browser-access/accounts.json'
ACCOUNTS={
 'mail': {'url':'https://mail.ru/','username':'pm.build.team@mail.ru','vault':'pm_work_mail','method':'password_or_qr'},
 'avito': {'url':'https://www.avito.ru/','username':'','vault':'pm_work_avito','method':'phone_confirmation'},
 'whatsapp': {'url':'https://web.whatsapp.com/','username':'','method':'qr'},
 'telegram': {'url':'https://web.telegram.org/a/','username':'','method':'qr_or_phone'},
 'max': {'url':'https://web.max.ru/','username':'','method':'qr_or_phone'},
}

def config():
    existing=json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    accounts={**ACCOUNTS,**existing}
    if not CONFIG.exists() or accounts!=existing:
        CONFIG.write_text(json.dumps(accounts,ensure_ascii=False,indent=2))
        CONFIG.chmod(0o600)
    return accounts

def vault(args,password=None):
    result=subprocess.run([str(BASE/'node/bin/agent-browser'),'--json','auth',*args],
        input=password,text=True,capture_output=True,timeout=45)
    try:return json.loads(result.stdout)
    except ValueError:return {'success':False}

def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=['status','configure','connect'])
    p.add_argument('account',nargs='?',choices=list(ACCOUNTS))
    p.add_argument('--profile')
    args=p.parse_args();accounts=config()
    if args.action=='status':
        result=vault(['list'])
        saved={x['name'] for x in (result.get('data') or {}).get('profiles',[])}
        print(json.dumps({k:{'method':v['method'],'credentials_saved':v.get('vault') in saved,'login_verified':False} for k,v in accounts.items()},ensure_ascii=False))
        return
    if not args.account:p.error('Choose an account')
    account=accounts[args.account]
    if args.action=='configure':
        if account['method']!='password_or_qr':
            raise SystemExit('This channel requires interactive QR/phone confirmation; use connect.')
        if not sys.stdin.isatty():raise SystemExit('Run configure in an interactive terminal; do not pass passwords in chat or arguments.')
        password=getpass.getpass('Web account password (not the mail app password): ')
        if not password:raise SystemExit('No password entered')
        result=vault(['save',account['vault'],'--url',account['url'],'--username',account['username'],'--password-stdin'],password+'\n')
        del password
        print('Saved in local authentication vault.' if result.get('success') else 'Could not save credentials; nothing was sent to a website.')
        return
    if not args.profile:p.error('connect needs --profile')
    home=BASE if args.profile=='default' else BASE/'profiles'/args.profile
    from tools.team_headless import location
    root=location(home)
    state=json.loads((root/'status.json').read_text())
    own_task = state.get('status') in ('ready','error') and Path(os.environ.get('HERMES_HOME','/invalid')).resolve()==home.resolve()
    if not (state.get('status')=='human_login' or own_task) or not control.daemon_alive(state):
        raise SystemExit('Start a manual headless session with headless_control.py login PROFILE first. Active agent tasks are not interrupted.')
    result=vault(['list']);saved={x['name'] for x in (result.get('data') or {}).get('profiles',[])}
    if account.get('vault') in saved:
        result=control.run(state,'auth',['login',account['vault']],True)
        print('Login submitted; verify account page or complete confirmation.' if result.get('success') else 'Automatic login did not finish. Inspect the page; multi-step/QR confirmation may be required.')
    else:
        result=control.run(state,'open',[account['url']],True)
        print('Account page opened; saved web credentials are missing. Verify existing login or complete QR/login in the panel.' if result.get('success') else 'Account page could not be opened.')

if __name__=='__main__':main()
