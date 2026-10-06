"""Mail-only human login bridge. Request via stdin, credentials never argv/files."""
import asyncio,hashlib,json,os,re,subprocess,sys
from urllib.parse import urlsplit
import websockets
import headless_control as c
HOSTS={'mail.ru','www.mail.ru','id.vk.ru','account.mail.ru','auth.mail.ru','e.mail.ru'}
BUTTONS=re.compile(r'^(Войти|Продолжить|Далее|Подтвердить|Войти по паролю|Использовать пароль|Вход по паролю|Другой способ|Получить код|Войти другим способом)$',re.I)
FIELDS=re.compile(r'ящик|парол|код|телефон|email|логин',re.I)

def command(state,args):
    env=dict(os.environ,AGENT_BROWSER_SOCKET_DIR=state['socket_dir'],AGENT_BROWSER_IDLE_TIMEOUT_MS='0')
    p=subprocess.run([str(c.BASE/'node/bin/agent-browser'),'--session',state['session'],'--json',*args],env=env,capture_output=True,text=True,timeout=25)
    r=json.loads(p.stdout)
    if not r.get('success'):raise RuntimeError('Browser action failed; refresh the login step.')
    return r.get('data') or {}

def inspect(state):
    url=command(state,['get','url'])['url'];u=urlsplit(url)
    if u.scheme!='https' or u.hostname not in HOSTS:raise RuntimeError('Not on an allowed Mail login page.')
    if u.hostname=='e.mail.ru' and u.path.startswith('/inbox'):
        return dict(logged_in=True,fields=[],buttons=[],token='inbox')
    data=command(state,['snapshot','-i']);refs=data.get('refs',{})
    fields=[dict(ref=k,label=v.get('name','')) for k,v in refs.items() if v.get('role')=='textbox' and FIELDS.search(v.get('name',''))]
    buttons=[dict(ref=k,label=v.get('name','')) for k,v in refs.items() if v.get('role')=='button' and BUTTONS.fullmatch(v.get('name',''))]
    token=hashlib.sha256(json.dumps([url,fields,buttons],sort_keys=True).encode()).hexdigest()
    return dict(logged_in=False,fields=fields,buttons=buttons,token=token)

async def enter(state,text):
    async with websockets.connect('ws://127.0.0.1:'+str(state['stream_port']),max_size=8000000) as ws:
        for character in text:
            for event in ['keyDown','keyUp']:
                await ws.send(json.dumps(dict(type='input_keyboard',eventType=event,key=character,code='KeyA',text=character if event=='keyDown' else '',windowsVirtualKeyCode=65,modifiers=0)))
        await asyncio.sleep(.25)

def handle(request):
    state=json.loads((c.BASE/'profiles/gulya/headless-browser/status.json').read_text())
    if state.get('status')!='human_login' or not c.daemon_alive(state):raise RuntimeError('Gulya manual login session is not active.')
    form=inspect(state)
    action=request.get('action','inspect')
    if action=='inspect':return form
    if request.get('token')!=form['token']:raise RuntimeError('Login form changed. Refresh and try this step again.')
    if action=='finish':
        if not form['logged_in']:raise RuntimeError('Login has not been verified yet.')
        (c.BASE/'profiles/gulya/headless-browser/finish-login').touch()
        return {'closing_and_saving':True}
    if form['logged_in']:return form
    if action=='fill':
        fields=form['fields'];index=request.get('index');value=request.get('value')
        if type(index)!=int or not 0<=index<len(fields) or not isinstance(value,str) or not 0<len(value)<=500 or '\n' in value or '\r' in value:raise RuntimeError('Invalid input')
        command(state,['fill','@'+fields[index]['ref'],''])
        asyncio.run(enter(state,value))
        return {'filled':True}
    if action=='click':
        index=request.get('index')
        if type(index)!=int or not 0<=index<len(form['buttons']):raise RuntimeError('Invalid button')
        command(state,['click','@'+form['buttons'][index]['ref']]);return {'clicked':True}
    raise RuntimeError('Unknown action')

if __name__=='__main__':
    try:
        request=json.loads(sys.stdin.readline(8192));response=handle(request)
        print(json.dumps({'ok':True,'data':response},ensure_ascii=False))
    except Exception as error:
        # Never emit provider/tool output or request data in an exception.
        message=str(error) if type(error) is RuntimeError else 'Login step failed. Refresh or ask for help.'
        print(json.dumps({'ok':False,'error':message},ensure_ascii=False))
