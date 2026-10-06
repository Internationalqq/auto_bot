"""Opt-in Mac smoke test: remote stream input, no accounts or external sites."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import quote
import websockets

spec=importlib.util.spec_from_file_location('headless_control','/Users/egor/.hermes/team-browser-access/headless_control.py')
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)

async def check(info):
    result=c.run(info,'stream',['status'],True)
    port=result['data']['port']
    async with websockets.connect(f'ws://127.0.0.1:{port}',origin='http://localhost:4848',max_size=8*1024*1024) as ws:
        while True:
            frame=json.loads(await asyncio.wait_for(ws.recv(),10))
            if frame.get('type')=='frame':break
        assert frame['data']
        for event in ['mousePressed','mouseReleased']:
            await ws.send(json.dumps(dict(type='input_mouse',eventType=event,x=90,y=30,button='left',clickCount=1,modifiers=0)))
        await asyncio.sleep(.3)
        for char in 'stream-ok':
            await ws.send(json.dumps(dict(type='input_keyboard',eventType='keyDown',key=char,code=('Minus' if char=='-' else 'Key'+char.upper()),text=char,windowsVirtualKeyCode=ord(char.upper()),modifiers=0)))
            await ws.send(json.dumps(dict(type='input_keyboard',eventType='keyUp',key=char,code=('Minus' if char=='-' else 'Key'+char.upper()),text='',windowsVirtualKeyCode=ord(char.upper()),modifiers=0)))
        for _ in range(10):
            try:
                response=json.loads(await asyncio.wait_for(ws.recv(),.2))
                if response.get('type')!='frame':print('STREAM',response)
            except asyncio.TimeoutError:pass
        for _ in range(20):
            await asyncio.sleep(.1)
            value=c.run(info,'eval',["document.querySelector('input').value"],True)
            if value.get('data',{}).get('result')=='stream-ok':break
        else:raise AssertionError('Stream keyboard input did not reach the page: '+json.dumps(value))
        print('PASS: frame delivery, remote click, keyboard input')

with tempfile.TemporaryDirectory(prefix='hs-',dir='/tmp') as tmp:
    root=Path(tmp)
    info=dict(session='stream_smoke_'+str(os.getpid()),socket_dir=str(root/'socket'),team_root=str(root))
    Path(info['socket_dir']).mkdir(mode=0o700)
    html='<input style="position:absolute;left:10px;top:10px;width:200px;height:40px" aria-label="Test input">'
    try:
        opened=c.run(info,'open',['data:text/html,'+quote(html)],True)
        assert opened['success'],opened
        asyncio.run(check(info))
    finally:
        if c.daemon_alive(info):c.run(info,'close',human=True)
