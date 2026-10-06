// No real credentials: exercise exactly the patch installed ahead of dashboard JS.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const sent=[],listeners={};
class WS { static OPEN=1; readyState=1; send(v){sent.push(JSON.parse(v));} }
const context={WebSocket:WS,document:{activeElement:{tagName:'CANVAS'}},window:{addEventListener:(name,cb)=>listeners[name]=cb}};
vm.runInNewContext(fs.readFileSync('scripts/hermes/dashboard_input_fix.js','utf8'),context);
const ws=new WS();
for(const key of ['Backspace','Delete','Tab','Enter','ArrowLeft','Shift']) {
 ws.send(JSON.stringify({type:'input_keyboard',eventType:'keyUp',key,code:key}));
 const event=sent.at(-1);assert.equal(event.text,'');assert.equal(event.key,key);
}
ws.send(JSON.stringify({type:'input_keyboard',eventType:'keyDown',key:'я',code:'KeyZ',text:'я'}));
assert.equal(sent.at(-1).text,'я');
let prevented=false;
listeners.paste({clipboardData:{getData:()=> 'Тест@почта.ру'},preventDefault:()=>prevented=true,stopImmediatePropagation(){}});
assert.equal(sent.slice(7).filter(e=>e.eventType==='keyDown').map(e=>e.text).join(''),'Тест@почта.ру');assert.ok(prevented);
context.document.activeElement.tagName='INPUT';const count=sent.length;
listeners.paste({});assert.equal(sent.length,count);
console.log('PASS: special keys, Cyrillic, paste, dashboard input isolation');
// Corners, scaled image and letterboxed margins must map to the same page point.
const point=context.window.__pmViewportPoint;
for (const size of [[800,450],[800,700],[500,200]]) {
 const canvas={width:1280,height:720,getBoundingClientRect:()=>({left:35,top:81,width:size[0],height:size[1]})};
 const scale=Math.min(size[0]/1280,size[1]/720),w=1280*scale,h=720*scale;
 for(const [x,y] of [[10,10],[640,360],[1260,700]]) {
  const result=point(canvas,{clientX:35+(size[0]-w)/2+x*scale,clientY:81+(size[1]-h)/2+y*scale},1280,720);
  assert.ok(Math.abs(result.x-x)<=1 && Math.abs(result.y-y)<=1);
 }
 if(size[1]>h)assert.equal(point(canvas,{clientX:36,clientY:82},1280,720),null);
}
console.log('PASS: scaled coordinates and ignored letterbox margins');
