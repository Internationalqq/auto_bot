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
