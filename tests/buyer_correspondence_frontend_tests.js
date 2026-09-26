const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.dataset={}; this.events={}; this.value=''; this.hidden=false; this.attrs={}; this.className=''; this.ownText=''; }
  set textContent(text) { this.ownText=String(text); this.children=[]; }
  get textContent() { return this.ownText+this.children.map(child=>child.textContent).join(' '); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText=''; this.children=children; }
  setAttribute(key,value) { this.attrs[key]=value; }
  addEventListener(name,fn) { this.events[name]=fn; }
  contains() { return false; }
  querySelectorAll(selector) {
    const matches=el=>selector.startsWith('.') ? el.className.split(' ').includes(selector.slice(1)) : selector==='[data-buyer-count]' ? el.count : selector==='[data-buyer-sent-at]' ? el.dataset.buyerSentAt != null : el.tag===selector;
    return this.children.flatMap(child=>[...(matches(child)?[child]:[]),...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
const list=new Element(), find=new Element('input'), noResults=new Element(), mailNote=new Element(), toolbar=new Element(), head=new Element();
const filters=['all','sent','priced','answered','attention'].map(kind=>{
  const button=new Element('button'); button.dataset.buyerFilter=kind;
  const count=new Element('span'); count.count=true; button.append(count); return button;
});
const elements={'[data-buyer-list]':list,'[data-buyer-find]':find,'[data-buyer-no-results]':noResults,'[data-buyer-mail-note]':mailNote,'[data-buyer-toolbar]':toolbar,'[data-buyer-head]':head};
['start','cancel','refresh','status'].forEach(key=>elements[`[data-buyer-${key}]`]=new Element());
const root={dataset:{buyer:'123456789012345'},querySelector:s=>elements[s],querySelectorAll:()=>filters};
const events={}, intervals=[];
const document={hidden:false,createElement:tag=>new Element(tag),querySelector:s=>s==='[data-buyer]'?root:null,querySelectorAll:()=>[],addEventListener:(name,fn)=>events[name]=fn};
let api;
let now=1700000000000;
class Clock extends Date { static now() { return now; } }
const source=fs.readFileSync('autobot/static/buyer.js','utf8').replace(/  load\(\);\r?\n  setInterval/,'  capture({companyList, correspondence, renderCompanies, relativeAge, render});\n  setInterval');
assert.ok(source.includes('capture({companyList'));
vm.runInNewContext(source,{document,console,Date:Clock,setInterval:(fn,ms)=>intervals.push({fn,ms}),fetch(){throw new Error('Offline test');},capture:value=>api=value});
const position={position_key:'cable',name:'Кабель 4×150',quantity:351.9,unit:'пм'};
const company={id:'supplier',name:'Поставщик',contacts:[{channel:'email',address:'old@example.org'}],prices:[],position_keys:['cable'],draft_job_ids:['current'],status:'sent'};
const report={companies:[company],positions:[position]};
const job=(id,email)=>({id,positions:[position],supplier:{id,email,company:id},result:{drafts:[]},status:'completed'});
const outbox=[{id:'old-mail',draft_job_id:'old',recipient:'new@example.org',created_at:100,updated_at:110,status:'sent'},
  {id:'new-mail',draft_job_id:'current',recipient:'new@example.org',created_at:200,updated_at:210,status:'sent'},
  {id:'legacy-mail',draft_job_id:'legacy',recipient:'legacy@example.org',created_at:50,updated_at:60,status:'uncertain'}];
const reply={id:'reply',outbound_id:'new-mail',received_at:300,sender:'manager@example.org',raw_text:'Уточните адрес доставки. <img src=x onerror=alert(1)>',prices:[]};
const replies={checks:{'new-mail':{status:'checked',checked_at:310}},messages:[reply]};
// Matching uses the sent recipient, not a stale contact from the supplier website.
const actualReport={...report,companies:[{...company,contacts:[{channel:'email',address:'new@example.org'}]}]};
const original=JSON.stringify(actualReport);
const companies=api.companyList(actualReport,[job('current','old@example.org'),job('old','new@example.org'),job('legacy','legacy@example.org'),job('unsent','unsent@example.org')],outbox,replies);
assert.equal(companies.length,2,'Historical sends remain in the list; unrelated old drafts do not clutter it');
assert.equal(companies[0].draft_job_ids.length,2,'Same actual recipient is grouped across runs');
assert.equal(companies[1].previous,true);
assert.equal(JSON.stringify(actualReport),original,'Rendering must not mutate the API payload');
const shared=[...outbox,{id:'other-recipient',draft_job_id:'old',recipient:'different@example.org',created_at:150,status:'uncertain'}];
const split=api.companyList(actualReport,[job('old','new@example.org')],shared,replies);
assert.equal(split.length,2);
assert.equal(api.correspondence(split[0],shared,replies).latest.recipient,'new@example.org');
assert.equal(api.correspondence(split[1],shared,replies).answer,undefined,'One draft sent to several companies must never mix their replies');
assert.equal(api.correspondence(split[1],shared,replies).sent,false);
assert.equal(api.companyList(null,[job('no-email-a',''),job('no-email-b','')],[],{messages:[]}).length,2,'Missing email does not identify a company');
let thread=api.correspondence(companies[0],outbox,replies);
assert.equal(thread.latest.id,'new-mail');
assert.equal(thread.answer.id,'reply');
assert.equal(thread.sent,true);
assert.equal(thread.attention,false);
assert.equal(api.correspondence(companies[1],outbox,replies).sent,false,'Uncertain is never presented as sent');
assert.equal(api.correspondence(companies[1],outbox,replies).attention,true);
function render(data=replies, rep=report, outgoing=outbox) {
  list.replaceChildren(); api.renderCompanies(rep,[],outgoing,data,new Map(),new Set());
  return list.querySelector('.buyer-company');
}
let card=render();
let summary=card.querySelector('summary');
assert.match(summary.textContent,/new@example.org/);
assert.doesNotMatch(summary.textContent,/old@example.org/);
assert.match(summary.textContent,/Отправлено/);
assert.match(summary.textContent,/Получен/);
assert.doesNotMatch(summary.textContent,/Уточните адрес доставки/,'Full reply is one disclosure away, not repeated in every row');
assert.match(card.querySelector('.buyer-latest-reply').textContent,/manager@example.org/);
assert.equal(card.querySelectorAll('img').length,0,'Email markup is literal text, never HTML');
assert.equal(card.dataset.answered,'true');
filters.find(f=>f.dataset.buyerFilter==='answered').events.click();
assert.equal(card.hidden,false);
filters.find(f=>f.dataset.buyerFilter==='sent').events.click();
assert.equal(card.hidden,false);
find.value='new@example.org'; find.events.input(); assert.equal(card.hidden,false);
find.value='missing'; find.events.input(); assert.equal(card.hidden,true); assert.equal(noResults.hidden,false); assert.equal(head.hidden,true);
find.value=''; find.events.input();
assert.equal(head.hidden,false);
card=render({checks:{'new-mail':{status:'blocked',checked_at:320,error:'Нужна проверка входа'}},messages:[]});
assert.match(card.querySelector('summary').textContent,/Не зафиксирован/);
assert.doesNotMatch(card.querySelector('summary').textContent,/Ожидаем|Проверено/);
assert.match(mailNote.textContent,/Часть ответов не удалось проверить/);
assert.equal(card.dataset.attention,'true');
filters.find(f=>f.dataset.buyerFilter==='attention').events.click(); assert.equal(card.hidden,false);
card=render({checks:{},messages:[]});
assert.match(card.querySelector('summary').textContent,/Ожидаем/);
assert.equal(card.hidden,true,'The selected filter survives a refresh');
card=render({checks:{'new-mail':{status:'checking'}},messages:[]});
assert.match(card.querySelector('summary').textContent,/Проверяем/);
card=render({...replies,checks:{'new-mail':{status:'blocked'}}});
assert.match(card.querySelector('summary').textContent,/Получен/);
assert.match(card.querySelector('.buyer-inbox-warning').textContent,/Не удалось проверить новые ответы/);
const priced={...report,companies:[{...company,prices:[{price_kopecks:12550,unit:'м',position_key:'cable',origin:'reply',state:'review'}]}]};
card=render({...replies,messages:[{...reply,raw_text:'Ответ '.repeat(1000)}]},priced);
assert.ok(card.querySelector('summary').textContent.length<300,'A long reply cannot inflate the accessible row name');
assert.ok(card.querySelector('.buyer-latest-reply').textContent.length>5000);
assert.match(card.querySelector('summary').textContent,/125,5 ₽ \/ м/);
assert.match(card.querySelector('summary').textContent,/Из ответа · уточнить/);
assert.equal(card.dataset.priced,'true');
card=render({checks:{},messages:[]},report,[{...outbox[1],status:'uncertain'}]);
assert.match(card.querySelector('summary').textContent,/Не подтверждено/);
assert.equal(card.querySelector('time'),null,'Creation time must never be shown as a send confirmation');
assert.equal(mailNote.hidden,true);
card=render({checks:{},messages:[]},report,[{...outbox[1],updated_at:null}]);
assert.equal(card.querySelector('time'),null,'Missing confirmation time must not produce an invented age');
assert.match(card.querySelector('summary').textContent,/Отправка подтверждена/);

const stamp=now/1000;
[[0,'только что'],[59,'только что'],[60,'1 мин назад'],[3599,'59 мин назад'],[3600,'1 ч назад'],[9180,'2 ч 33 мин назад'],[86400,'1 д назад'],[93600,'1 д 2 ч назад'],[259200,'3 д назад']].forEach(([seconds,label])=>assert.equal(api.relativeAge(stamp-seconds),label));
assert.equal(api.relativeAge(stamp+60),'только что','Small clock skew never displays a negative duration');
[null,0,undefined,'bad',Infinity].forEach(value=>assert.equal(api.relativeAge(value),''));
const timedOutbox=[{...outbox[1],updated_at:stamp-120}];
api.render([],timedOutbox,[],{checks:{},messages:[]},report);
const retained=list.querySelector('.buyer-company'); retained.open=true;
const time=retained.querySelector('time');
assert.equal(time.textContent,'2 мин назад');
assert.match(time.title,/Отправка подтверждена/);
assert.equal(time.dateTime,new Date((stamp-120)*1000).toISOString());
now+=60000;
api.render([],timedOutbox,[],{checks:{},messages:[]},report);
assert.equal(list.querySelector('.buyer-company'),retained,'Identical API data must not rebuild the row');
assert.equal(time.textContent,'3 мин назад','Age advances even when API data did not change');
assert.equal(retained.open,true);
now+=60000;
assert.equal(intervals[0].ms,15000);
intervals[0].fn();
assert.equal(time.textContent,'4 мин назад','Scheduled age updates also work while API requests fail');
now+=120000;
events.visibilitychange();
assert.equal(time.textContent,'6 мин назад','Returning from a hidden tab refreshes elapsed time immediately');
assert.equal(retained.open,true);
render({checks:{},messages:[]},{companies:[]},[]);
assert.equal(toolbar.hidden,true); assert.equal(mailNote.hidden,true); assert.equal(head.hidden,true);
assert.match(list.textContent,/Компании пока не найдены/);
console.log('Buyer correspondence: recipient isolation, compact statuses, safe replies, elapsed time, unchanged/offline refresh, filters and empty state passed.');
