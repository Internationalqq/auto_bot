"""Click-grid regression through the actual panel at three viewport widths."""
import importlib.util,tempfile,os,time,json,math
from pathlib import Path
from urllib.parse import quote
spec=importlib.util.spec_from_file_location('c','/Users/egor/.hermes/team-browser-access/headless_control.py');c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
def call(info,command,args):
 r=c.run(info,command,args,True);assert r['success'],r.get('error');return r['data']
with tempfile.TemporaryDirectory(prefix='xy-',dir='/tmp') as tmp:
 sessions=[]
 try:
  for name in ['xy_target','xy_viewer']:
   root=Path(tmp)/name;root.mkdir();(root/'sock').mkdir()
   sessions.append(dict(session=name+str(os.getpid()),team_root=str(root),socket_dir=str(root/'sock')))
  target,viewer=sessions;target['socket_dir']='/tmp/agent-browser-team-manual'
  html='<body style="margin:0;background:#eef"><script>window.last=null;document.addEventListener("mousedown",e=>window.last={x:e.clientX,y:e.clientY})</script><p>Coordinate fixture</p></body>'
  call(target,'open',['data:text/html,'+quote(html)])
  call(target,'set',['viewport','1280','720'])
  port=call(target,'stream',['status'])['port']
  call(viewer,'open',[f'http://localhost:4850/?port={port}'])
  time.sleep(1)
  for width in [390,768,1280]:
   call(viewer,'set',['viewport',str(width),'900'])
   time.sleep(.3)
   for x,y in [(25,25),(640,360),(1240,670)]:
    expression='''(()=>{const c=document.querySelector('canvas');c.style.width='100%';c.style.height='100%';const r=c.getBoundingClientRect(),scale=Math.min(r.width/c.width,r.height/c.height),w=c.width*scale,h=c.height*scale;const clientX=r.left+(r.width-w)/2+X*w/1280,clientY=r.top+(r.height-h)/2+Y*h/720;c.focus();for(const type of ['mousedown','mouseup'])c.dispatchEvent(new MouseEvent(type,{bubbles:true,clientX,clientY,button:0}));return {w:r.width,h:r.height}})()'''.replace('+X*','+'+str(x)+'*').replace('+Y*','+'+str(y)+'*')
    geometry=call(viewer,'eval',[expression])['result']
    time.sleep(.15)
    actual=call(target,'eval',['window.last'])['result']
    tolerance=math.ceil(max(1280/geometry['w'],720/geometry['h']))+1
    assert actual and abs(actual['x']-x)<=tolerance and abs(actual['y']-y)<=tolerance,(width,x,y,actual,geometry)
   print('PASS coordinate grid',width)
 finally:
  for info in sessions:
   if c.daemon_alive(info):c.run(info,'close',human=True)
