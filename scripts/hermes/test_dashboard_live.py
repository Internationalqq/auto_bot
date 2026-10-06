"""Real browser test of the patched dashboard against an isolated form."""
import importlib.util,tempfile,json,time,os
from pathlib import Path
from urllib.parse import quote
spec=importlib.util.spec_from_file_location('c','/Users/egor/.hermes/team-browser-access/headless_control.py');c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)

def call(info,command,args):
 r=c.run(info,command,args,True)
 assert r['success'],r.get('error')
 return r['data']

with tempfile.TemporaryDirectory(prefix='ui-',dir='/tmp') as tmp:
 sessions=[]
 try:
  for name in ['target','viewer']:
   root=Path(tmp)/name;root.mkdir();(root/'sock').mkdir()
   sessions.append(dict(session=name+str(os.getpid()),team_root=str(root),socket_dir=str(root/'sock')))
  target,viewer=sessions
  target['socket_dir']='/tmp/agent-browser-team-manual'
  call(target,'open',['data:text/html,'+quote('<input id="a" style="position:absolute;left:10px;top:10px;width:200px;height:40px"><input id="b" style="position:absolute;left:10px;top:80px">')])
  call(target,'set',['viewport','1280','720'])
  port=call(target,'stream',['status'])['port']
  opened=c.run(viewer,'open',[f'http://localhost:4850/?port={port}'],True)
  if not opened['success']:print('NAV',opened,call(viewer,'get',['url']))
  for _ in range(15):
   ready=call(viewer,'eval',["Boolean(document.querySelector('canvas')?.width)"])['result']
   if ready:break
   time.sleep(.2)
  time.sleep(1)
  call(viewer,'eval',["(()=>{const c=document.querySelector('canvas'),r=c.getBoundingClientRect();c.focus();for(const type of ['mousedown','mouseup'])c.dispatchEvent(new MouseEvent(type,{bubbles:true,clientX:r.left+90*r.width/c.width,clientY:r.top+30*r.height/c.height,button:0}));return true})()"])
  call(viewer,'press',['a']);call(viewer,'press',['b']);call(viewer,'press',['Backspace'])
  time.sleep(.3)
  observed=call(target,'eval',["({value:document.querySelector('#a').value,focus:document.activeElement.id})"])['result']
  assert observed['value']=='a',observed
  call(viewer,'press',['Tab']);time.sleep(.2)
  assert call(target,'eval',["document.activeElement.id"])['result']=='b'
  call(viewer,'eval',["window.dispatchEvent(new ClipboardEvent('paste',{clipboardData:(()=>{const d=new DataTransfer();d.setData('text/plain','Привет@тест.ру');return d})()}));true"])
  time.sleep(.3)
  assert call(target,'eval',["document.querySelector('#b').value"])['result']=='Привет@тест.ру'
  print('PASS: actual dashboard typing, Backspace, Tab, Cyrillic paste')
 finally:
  for info in sessions:
   if c.daemon_alive(info):c.run(info,'close',human=True)
