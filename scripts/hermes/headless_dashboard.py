"""Loopback-only compatibility front for the bundled dashboard. No credential logs."""
from pathlib import Path
from urllib.parse import urlsplit
from aiohttp import web, ClientSession, ClientTimeout

PORT=4850
UPSTREAM='http://127.0.0.1:4848'
FIX=Path(__file__).with_name('dashboard_input_fix.js')
OLD_COORDINATES='let i=n.getBoundingClientRect(),a=t/i.width,o=r/i.height;return{x:Math.round((e.clientX-i.left)*a),y:Math.round((e.clientY-i.top)*o)}'
NEW_COORDINATES='return window.__pmViewportPoint(n,e,t,r)'


def allowed(request):
    if request.host not in (f'localhost:{PORT}',f'127.0.0.1:{PORT}'):
        return False
    origin=request.headers.get('Origin')
    return not origin or origin in (f'http://localhost:{PORT}',f'http://127.0.0.1:{PORT}')

async def serve(request):
    if not allowed(request):raise web.HTTPForbidden()
    if request.path=='/input-fix.js':
        return web.Response(body=FIX.read_bytes(),content_type='application/javascript',headers={'Cache-Control':'no-store'})
    async with request.app['client'].get(UPSTREAM+request.rel_url.path_qs,allow_redirects=False) as response:
        body=await response.read()
        kind=response.headers.get('Content-Type','application/octet-stream')
        if request.path=='/' and body.startswith(b'<!DOCTYPE html>'):kind='text/html; charset=utf-8'
        if request.path.endswith('.js'):
            body=body.replace(OLD_COORDINATES.encode(),NEW_COORDINATES.encode())
        if 'text/html' in kind:
            body=body.replace(b'<head>',b'<head><script src="/input-fix.js"></script>',1)
        return web.Response(body=body,status=response.status,headers={'Content-Type':kind,'Cache-Control':'no-store'})

async def resources(app):
    async with ClientSession(timeout=ClientTimeout(total=20)) as client:
        app['client']=client
        yield

app=web.Application()
app.cleanup_ctx.append(resources)
app.router.add_get('/{tail:.*}',serve)
if __name__=='__main__':web.run_app(app,host='127.0.0.1',port=PORT,access_log=None,print=None)
