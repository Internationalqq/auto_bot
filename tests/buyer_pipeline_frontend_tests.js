const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let focused;
class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.dataset={}; this.events={}; this.attrs={}; this.hidden=false; this.ownText=''; this.value=''; }
  set textContent(value) { this.ownText=String(value); this.children=[]; }
  get textContent() { return this.ownText+this.children.map(c=>typeof c==='string'?c:c.textContent).join(' '); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; this.ownText=''; }
  addEventListener(type, fn) { this.events[type]=fn; }
  setAttribute(name,value) { this.attrs[name]=value; }
  focus() { focused=this; }
  querySelectorAll(selector) { return this.children.flatMap(c=>typeof c==='string'?[]:[...(c.tag===selector || (selector.startsWith('.') && c.className===selector.slice(1)) ? [c]:[]),...c.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
const host=new Element(), calls=[];
const context={window:{}, document:{createElement:tag=>new Element(tag)}, URL, Date, console};
vm.runInNewContext(fs.readFileSync('autobot/static/buyer_pipeline.js','utf8'), context);
const view=context.window.createBuyerPipeline(host,{onSearch:async key=>calls.push(['search',key]),onChat:id=>{calls.push(['chat',id]);return true;}});
const flags={candidates:true,comparable:false,contacts:true,sent:true,replied:true,confirmed:false};
const row={position_key:'r1',name:'Кабель <script>alert(1)</script>',item_no:'1',quantity:5,unit:'м',eligible:true,flags,state:'replied',label:'Ответ получен',reason:'Нужно уточнить цену',contacts:[{company:'Компания',address:'a@example.org'}],messages:[{id:'m1'}],offers:[{origin:'website',price_kopecks:12050,unit:'м',evidence:'<img src=x>',url:'javascript:alert(1)',comparable:false}]};
const data={positions:[row],summary:{total:1,eligible:1,excluded:0,...Object.fromEntries(Object.keys(flags).map(k=>[k,flags[k]?1:0]))}};
const findButton = label => host.querySelectorAll('button').find(b=>b.textContent===label);
(async () => {
  view.update(data);
  findButton('1 Получен ответ').events.click();
  assert.equal(host.querySelector('article').textContent.includes('Ответ получен'),true);
  assert.equal(host.querySelectorAll('script').length,0);
  assert.equal(host.querySelectorAll('img').length,0);
  assert.equal(host.querySelectorAll('a').length,0,'Untrusted URL must not become a link');
  findButton('Переписка').events.click();
  await findButton('Подобрать поставщика').events.click();
  assert.deepEqual(calls,[['chat','m1'],['search','r1']]);
  assert.equal(findButton('Подобрать поставщика').disabled,false);
  const input=host.querySelector('input'); input.value='несуществующая'; input.events.input();
  assert.match(host.textContent,/По этому запросу позиций нет/);
  view.update(data); assert.equal(input.value,'несуществующая','Polling preserves search text');
  input.value=''; input.events.input();
  findButton('0 Цена из ответа').events.click();
  assert.match(host.textContent,/На этом этапе пока нет позиций/);
  view.fail('Ошибка загрузки.'); assert.match(host.textContent,/последние загруженные данные/);
  findButton('Свернуть').events.click(); assert.equal(focused,findButton('Все позиции'));
  const confirmed=structuredClone(data); confirmed.positions[0].flags.confirmed=true;confirmed.summary.confirmed=1;
  confirmed.positions[0].offers=[{origin:'reply',price_kopecks:12050,unit:'м',comparable:true,outbox_id:'m1'}];
  view.update(confirmed);findButton('1 Цена из ответа').events.click();
  assert.match(host.textContent,/120,5 ₽ \/ м/);
  assert.equal(findButton('Подобрать поставщика'),undefined,'Confirmed quote does not invite a duplicate search');
  assert.equal(host.querySelectorAll('button').some(b=>b.textContent==='Отправить'),false,'Evidence view cannot send a message');
  console.log('Buyer pipeline: stage filters, empty/error states, exact actions, safe evidence, polling and keyboard focus passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
