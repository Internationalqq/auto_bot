"""Mail bridge checks use fixture data and never authenticate a real account."""
import sys,asyncio,importlib.util,json,tempfile,os
from pathlib import Path
from urllib.parse import quote
sys.path.insert(0,'/Users/egor/.hermes/team-browser-access')
import mail_login_bridge as b
import headless_control as c

def test_external_page_rejected():
 old=b.command
 try:
  b.command=lambda s,args:{'url':'https://example.com/fake-login'}
  try:b.inspect({})
  except RuntimeError:pass
  else:raise AssertionError('external page accepted')
 finally:b.command=old

def test_no_message_buttons():
 assert b.BUTTONS.fullmatch('Войти')
 assert not b.BUTTONS.fullmatch('Отправить сообщение')
 assert not b.BUTTONS.fullmatch('Удалить')

def live():
 with tempfile.TemporaryDirectory(prefix='ml-',dir='/tmp') as tmp:
  root=Path(tmp);(root/'sock').mkdir()
  state=dict(session='mail_fixture'+str(os.getpid()),team_root=str(root),socket_dir=str(root/'sock'))
  try:
   assert c.run(state,'open',['data:text/html,'+quote('<input id="login" value="old-value">')],True)['success']
   state['stream_port']=c.run(state,'stream',['status'],True)['data']['port']
   for value in ['fixture-user','replacement-123']:
    assert c.run(state,'fill',['#login',''],True)['success']
    asyncio.run(b.enter(state,value))
    actual=c.run(state,'eval',["document.querySelector('#login').value"],True)['data']['result']
    assert actual==value,repr(actual)
   print('PASS: field replacement via private stream; external origin and non-login actions rejected')
  finally:
   if c.daemon_alive(state):c.run(state,'close',human=True)

if __name__=='__main__':
 test_external_page_rejected();test_no_message_buttons();live()
