const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.dataset={}; this.events={}; this.value=''; this.hidden=false; this.attrs={}; this.className=''; this.ownText=''; this.scrollTop=0; this.clientHeight=200; this.scrollHeight=1000; this.classList={add:value=>{this.className+=' '+value;}}; }
  get tagName() { return this.tag.toUpperCase(); }
  setSelectionRange(start,end) { this.selectionStart=start; this.selectionEnd=end; }
  set textContent(text) { this.ownText=String(text); this.children=[]; }
  get textContent() { return this.ownText+this.children.map(child=>child.textContent).join(' '); }
  append(...children) { children.forEach(child=>{ if(child.parent) child.parent.children=child.parent.children.filter(c=>c!==child); child.parent=this; this.children.push(child); }); }
  replaceChildren(...children) { this.ownText=''; this.children=children; }
  setAttribute(key,value) { this.attrs[key]=value; }
  addEventListener(name,fn) { this.events[name]=fn; }
  contains(el) { return el && (el===this || this.children.some(c=>c.contains(el))); }
  closest(selector) { return this.tag===selector ? this : this.parent?.closest(selector); }
  focus() { document.activeElement=this; }
  scrollIntoView(options) { this.lastScroll=options; }
  querySelectorAll(selector) {
    const matches=el=>selector.startsWith('.') ? el.className.split(' ').includes(selector.slice(1)) : selector==='[data-buyer-count]' ? el.count : selector==='[data-buyer-chat-control]' ? el.dataset.buyerChatControl != null : selector==='[data-buyer-sent-at]' ? el.dataset.buyerSentAt != null : selector==='details[open]' ? el.tag==='details' && el.open : el.tag===selector;
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
const source=fs.readFileSync('autobot/static/buyer.js','utf8').replace(/  load\(\);\r?\n  setInterval/,'  capture({companyList, correspondence, renderCompanies, relativeAge, render, conversationEvents, chatState, draftState});\n  setInterval');
assert.ok(source.includes('capture({companyList'));
const storage=new Map();
const context={document,console,URL,Date:Clock,sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},setInterval:(fn,ms)=>intervals.push({fn,ms}),fetch(){throw new Error('Offline test');},capture:value=>api=value};
context.crypto=require('node:crypto').webcrypto;
vm.runInNewContext(source,context);
const position={position_key:'cable',name:'Кабель 4×150',quantity:351.9,unit:'пм'};
const company={id:'supplier',name:'Поставщик',contacts:[{channel:'email',address:'old@example.org'}],prices:[],position_keys:['cable'],draft_job_ids:['current'],status:'sent'};
const report={companies:[company],positions:[position]};
const job=(id,email)=>({id,positions:[position],supplier:{id,email,company:id},result:{drafts:[]},status:'completed'});
const outbox=[{id:'old-mail',draft_job_id:'old',recipient:'new@example.org',created_at:100,updated_at:110,status:'sent'},
  {id:'new-mail',draft_job_id:'current',recipient:'new@example.org',created_at:200,updated_at:210,status:'sent'},
  {id:'legacy-mail',draft_job_id:'legacy',recipient:'legacy@example.org',created_at:50,updated_at:60,status:'uncertain'}];
const reply={id:'reply',outbound_id:'new-mail',received_at:300,sender:'manager@example.org',raw_text:'Уточните адрес доставки. <img src=x onerror=alert(1)>',prices:[]};
const replies={checks:{'new-mail':{status:'checked',checked_at:310}},messages:[reply]};
const archived={...job('archived','sales@example.org'),can_send:true,supplier:{id:'found',email:'sales@example.org',url:'https://regional.example.org/'},result:{drafts:[{position_keys:['cable']}],questions:[]}};
assert.match(api.draftState(archived,[archived],[],[],{messages:[]}).label,/отправка не запускалась/);
assert.match(api.draftState({...archived,supplier:{url:'https://2gis.ru/city',email:''}},[],[],[],{messages:[]}).label,/Справочник/);
assert.match(api.draftState({...archived,supplier:{email:''}},[],[],[],{messages:[]}).label,/Нужен email/);
assert.match(api.draftState(archived,[],[],[{draft_job_id:'archived',status:'completed',contacts:[{error:'Email не подтверждается'}]}],{messages:[]}).detail,/Email не подтверждается/);
const previous={...archived,id:'previous',supplier:{id:'old',url:'https://example.org/',email:'sales+old@example.org'}};
const previousSend={id:'sent-request',draft_job_id:'previous',draft_index:0,status:'sent',recipient:'sales+old@example.org'};
assert.equal(api.draftState(archived,[archived,previous],[previousSend],[],{messages:[]}).related,'sent-request','A regional website and rotating email alias do not disguise the existing request');
assert.equal(api.draftState({...archived,positions:[{position_key:'other'}]},[previous],[previousSend],[],{messages:[]}).related,undefined,'A different position remains a new draft');
assert.equal(api.draftState(archived,[{...previous,supplier:{id:'different',url:'https://another.org'}}],[previousSend],[],{messages:[]}).related,undefined,'Different company must not be marked a duplicate');
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
assert.match(card.attrs['aria-label'],/new@example.org/);
assert.doesNotMatch(card.attrs['aria-label'],/old@example.org/);
assert.match(card.textContent,/Ответ получен/);
assert.match(card.textContent,/Уточните адрес доставки/,'Contact shows a bounded last-message preview');
assert.equal(list.querySelectorAll('img').length,0,'Email markup is literal text, never HTML');
card.events.click();
assert.equal(storage.get('autobot:buyer-chat:123456789012345'),'supplier');
assert.equal(list.querySelector('.buyer-chats').dataset.open,'true','Opening a contact also opens the mobile conversation');
let panel=list.querySelector('.buyer-chat');
assert.equal(panel.hidden,false);
assert.match(panel.querySelector('.buyer-chat-timeline').textContent,/Уточните адрес доставки/);
assert.equal(document.activeElement,panel.querySelector('h3'),'Keyboard focus follows the opened contact');
panel.querySelector('.buyer-chat-info-button').events.click();
assert.equal(panel.querySelector('.buyer-chat-info').hidden,false);
assert.equal(panel.querySelector('.buyer-chat-timeline').hidden,true);
panel.querySelector('.buyer-chat-info-button').events.click();
assert.equal(card.dataset.answered,'true');
filters.find(f=>f.dataset.buyerFilter==='answered').events.click();
assert.equal(card.hidden,false);
filters.find(f=>f.dataset.buyerFilter==='sent').events.click();
assert.equal(card.hidden,false);
find.value='new@example.org'; find.events.input(); assert.equal(card.hidden,false);
find.value='missing'; find.events.input(); assert.equal(card.hidden,true); assert.equal(list.querySelector('.buyer-chat-no-results').hidden,false); assert.equal(panel.hidden,true);
assert.equal(list.querySelector('.buyer-chats').dataset.open,'false','Mobile search returns to the contact list, including no-result feedback');
find.value=''; find.events.input();
assert.equal(panel.hidden,false);
card=render({checks:{'new-mail':{status:'blocked',checked_at:320,error:'Нужна проверка входа'}},messages:[]});
assert.match(card.textContent,/Проверка почты недоступна/);
assert.doesNotMatch(card.textContent,/Ожидаем|Проверено/);
assert.match(mailNote.textContent,/Часть ответов не удалось проверить/);
assert.equal(card.dataset.attention,'true');
filters.find(f=>f.dataset.buyerFilter==='attention').events.click(); assert.equal(card.hidden,false);
card=render({checks:{},messages:[]});
assert.match(card.textContent,/Ожидаем/);
assert.equal(card.hidden,true,'The selected filter survives a refresh');
card=render({checks:{'new-mail':{status:'checking'}},messages:[]});
assert.match(card.textContent,/Проверяем/);
card=render({...replies,checks:{'new-mail':{status:'blocked'}}});
assert.match(card.textContent,/Ответ получен/);
assert.match(mailNote.textContent,/не удалось проверить/);
const priced={...report,companies:[{...company,prices:[{price_kopecks:12550,unit:'м',position_key:'cable',origin:'reply',state:'review'}]}]};
card=render({...replies,messages:[{...reply,raw_text:'Ответ '.repeat(1000)}]},priced);
assert.ok(card.attrs['aria-label'].length<300,'A long reply cannot inflate the accessible contact name');
assert.ok(card.textContent.length<400,'Last-message preview is bounded');
assert.ok(list.querySelector('.buyer-chat-timeline').textContent.length>5000,'Full text is retained in the conversation');
assert.match(card.textContent,/125,5 ₽ \/ м/);
assert.match(card.textContent,/из ответа · уточнить/);
assert.match(list.querySelector('.buyer-chat-info').textContent,/нужно уточнение/);
assert.equal(card.dataset.priced,'true');
card=render({checks:{},messages:[]},report,[{...outbox[1],status:'uncertain'}]);
assert.match(card.textContent,/Нужна проверка отправки/);
assert.equal(card.querySelector('time'),null,'Creation time must never be shown as a send confirmation');
assert.equal(mailNote.hidden,true);
card=render({checks:{},messages:[]},report,[{...outbox[1],updated_at:null}]);
assert.equal(card.querySelector('time'),null,'Missing confirmation time must not produce an invented age');
assert.match(list.querySelector('.buyer-chat-timeline').textContent,/Отправка подтверждена/);

const stamp=now/1000;
[[0,'только что'],[59,'только что'],[60,'1 мин назад'],[3599,'59 мин назад'],[3600,'1 ч назад'],[9180,'2 ч 33 мин назад'],[86400,'1 д назад'],[93600,'1 д 2 ч назад'],[259200,'3 д назад']].forEach(([seconds,label])=>assert.equal(api.relativeAge(stamp-seconds),label));
assert.equal(api.relativeAge(stamp+60),'только что','Small clock skew never displays a negative duration');
[null,0,undefined,'bad',Infinity].forEach(value=>assert.equal(api.relativeAge(value),''));
const timedOutbox=[{...outbox[1],updated_at:stamp-120}];
api.render([],timedOutbox,[],{checks:{},messages:[]},report);
filters.find(f=>f.dataset.buyerFilter==='all').events.click();
const retained=list.querySelector('.buyer-company'); retained.events.click();
const time=retained.querySelector('time');
assert.equal(time.textContent,'2 мин назад');
assert.match(time.title,/Отправка подтверждена/);
assert.equal(time.dateTime,new Date((stamp-120)*1000).toISOString());
now+=60000;
api.render([],timedOutbox,[],{checks:{},messages:[]},report);
assert.equal(list.querySelector('.buyer-company'),retained,'Identical API data must not rebuild the row');
assert.equal(time.textContent,'3 мин назад','Age advances even when API data did not change');
assert.equal(retained.attrs['aria-pressed'],'true');
now+=60000;
assert.equal(intervals[0].ms,15000);
intervals[0].fn();
assert.equal(time.textContent,'4 мин назад','Scheduled age updates also work while API requests fail');
now+=120000;
events.visibilitychange();
assert.equal(time.textContent,'6 мин назад','Returning from a hidden tab refreshes elapsed time immediately');
assert.equal(retained.attrs['aria-pressed'],'true');
// Sorting uses confirmed send time; follow-ups appear once in the same conversation.
const sent1={...outbox[1],body:'Первый запрос',created_at:10,updated_at:100};
const sent2={...sent1,id:'followup',body:'Адрес объекта',created_at:20,updated_at:400};
const addressReply={...reply,received_at:300};
const addressReplies={checks:{},messages:[addressReply],followups:[{reply_id:reply.id,outbound_id:'followup',source:{document:'Контракт.docx'}}]};
const addressThread=api.correspondence(company,[sent2,sent1],addressReplies);
assert.equal(api.conversationEvents(addressThread,addressReplies).map(e=>e.id).join(','),'out-new-mail,in-reply,out-followup');
assert.equal(api.chatState(addressThread,company),'Ожидаем ответ','An answer followed by our reply returns to waiting');
api.render([], [sent2,sent1], [],addressReplies,report);
panel=list.querySelector('.buyer-chat');
assert.equal(panel.querySelectorAll('.buyer-message').length,3);
assert.match(panel.textContent,/Автобот · адрес объекта/);
assert.match(panel.textContent,/Контракт.docx/);
assert.equal(panel.querySelectorAll('.buyer-message-in').length,1);
panel.querySelector('.buyer-chat-info-button').focus();
list.querySelector('.buyer-chat-list').scrollTop=90;
const scroller=panel.querySelector('.buyer-chat-timeline');
scroller.scrollTop=120;
api.render([], [sent2,sent1], [],{...addressReplies,checks:{'new-mail':{status:'checked',checked_at:410}}},report);
assert.equal(list.querySelector('.buyer-chat-timeline').scrollTop,120,'Polling preserves position while reading earlier messages');
assert.equal(list.querySelector('.buyer-chat-list').scrollTop,90,'Polling preserves the contact list scroll');
assert.equal(document.activeElement,list.querySelector('.buyer-chat-info-button'),'Polling preserves focused conversation controls');
list.querySelector('.buyer-chat-timeline').scrollTop=800;
api.render([], [sent2,sent1], [],{...addressReplies,checks:{'new-mail':{status:'checked',checked_at:420}}},report);
assert.equal(list.querySelector('.buyer-chat-timeline').scrollTop,1000,'A reader at the bottom continues to the newest message');
// A fresh page instance restores the selected conversation from per-tender storage.
const multipleReport={...report,companies:[company,{...company,id:'second',name:'Другая компания',draft_job_ids:['second-job']}]};
const multipleOutbox=[sent2,sent1,{...sent1,id:'second-mail',draft_job_id:'second-job',recipient:'second@example.org',updated_at:500}];
api.render([],multipleOutbox,[],addressReplies,multipleReport);
const chosen=list.querySelectorAll('.buyer-company').find(c=>c.dataset.key==='company-supplier'); chosen.events.click();
assert.notEqual(list.querySelector('.buyer-company'),chosen,'Chosen conversation is not the default first row');
vm.runInNewContext(source,context);
api.render([], multipleOutbox, [],addressReplies,multipleReport);
assert.equal(list.querySelectorAll('.buyer-company').find(c=>c.dataset.key==='company-supplier').attrs['aria-pressed'],'true');
assert.equal(list.querySelector('.buyer-company').attrs['aria-pressed'],'false');
assert.equal(list.querySelector('.buyer-chats').dataset.open,'true');
list.querySelectorAll('.buyer-chat').find(p=>!p.hidden).querySelector('.buyer-chat-back').events.click();
assert.equal(list.querySelector('.buyer-chats').dataset.open,'false');
assert.equal(storage.has('autobot:buyer-chat:123456789012345'),false);
render({checks:{},messages:[]},{companies:[]},[]);
assert.equal(toolbar.hidden,true); assert.equal(mailNote.hidden,true);
assert.match(list.textContent,/Компании пока не найдены/);
(async () => {
  const parent={...outbox[1],body:'Исходный запрос',subject:'Кабель [AB-RFQ-ABCDE]',updated_at:stamp};
  const draftJob=job('current','new@example.org');
  draftJob.result.questions=[];
  let outgoing=[parent], responses={checks:{},messages:[]}, requests=[], pending;
  let handler=() => new Promise(resolve=>pending=resolve);
  context.fetch=async (url,options) => {
    if (options?.method==='POST') { requests.push(JSON.parse(options.body)); return handler(); }
    return {ok:true,json:async()=>url.includes('/report') ? report : {ok:true,jobs:[draftJob],outbox:outgoing,replies:responses,campaigns:[],searches:[]}};
  };
  const paint=()=>api.render([draftJob],outgoing,[],responses,report);
  const form=()=>list.querySelector('.buyer-chat-composer');
  const input=()=>form().querySelector('textarea');
  const button=()=>form().querySelector('button');
  const submit=()=>form().events.submit({preventDefault(){}});
  paint();
  assert.equal(button().disabled,true);
  input().value='Ручной текст\nСо следующей строкой'; input().events.input(); input().focus(); input().setSelectionRange(3,8);
  responses={checks:{'new-mail':{status:'checked',checked_at:stamp+10}},messages:[]}; paint();
  assert.equal(input().value,'Ручной текст\nСо следующей строкой');
  assert.equal(document.activeElement,input()); assert.equal(input().selectionStart,3);
  vm.runInNewContext(source,context); paint();
  assert.equal(input().value,'Ручной текст\nСо следующей строкой','F5 restores draft');
  const sending=submit(); await submit();
  assert.equal(requests.length,1,'Double click cannot queue another operation');
  responses={checks:{'new-mail':{status:'checked',checked_at:stamp+20}},messages:[]}; paint();
  assert.equal(button().disabled,true); assert.equal(input().readOnly,true,'Polling retains pending state');
  outgoing.push({...parent,id:'manual',manual:true,parent_outbound_id:parent.id,request_id:requests[0].request_id,status:'queued',body:requests[0].body,created_at:stamp+25});
  pending({ok:true,status:202,json:async()=>({ok:true,id:'manual'})}); await sending;
  assert.equal(input().value,''); assert.equal(input().readOnly,false);
  assert.match(list.querySelector('.buyer-chat-timeline').textContent,/Ручной текст/);
  assert.match(list.querySelector('.buyer-chat-timeline').textContent,/В очереди отправки/);
  assert.equal(requests[0].parent_id,parent.id); assert.equal(requests[0].recipient,undefined);
  input().value='Текст при потере связи'; input().events.input();
  handler=async()=>{ throw new Error('Lost acknowledgment'); }; await submit();
  assert.equal(input().value,'Текст при потере связи'); assert.equal(input().readOnly,true);
  assert.equal(button().textContent,'Проверить отправку');
  const lost=requests.at(-1).request_id;
  vm.runInNewContext(source,context); paint();
  assert.equal(button().textContent,'Проверить отправку','Reload never creates a new intent after an uncertain response');
  handler=async()=>({ok:true,status:202,json:async()=>({ok:true,id:'recovered'})}); await submit();
  assert.equal(requests.at(-1).request_id,lost); assert.equal(input().value,'');
  input().value='Попробую после входа'; input().events.input();
  handler=async()=>({ok:false,status:401,json:async()=>({ok:false,message:'Сессия истекла'})}); await submit();
  assert.equal(input().readOnly,false); assert.equal(input().value,'Попробую после входа');
  assert.match(form().textContent,/Сессия истекла/);
  console.log('Buyer chats and manual messages: isolation, chronology, safe text, statuses, persistence, polling, focus, duplicate submit, lost acknowledgment, restart and rejected request passed.');
})().catch(error=>{console.error(error); process.exitCode=1;});
