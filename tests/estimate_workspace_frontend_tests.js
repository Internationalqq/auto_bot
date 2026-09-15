'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../autobot/static/estimate_workspace.js'), 'utf8');
const reply = data => ({ok:true, json:async () => data});
async function flush() {for (let i=0; i<12; i++) await Promise.resolve();}
function harness(initial = {}, config = {}, bridge = null, storage = new Map()) {
  const requests = [], responses = [reply(initial)], redirects = [], timers = [];
  function node(attrs = {}) {return {attrs, dataset:{}, textContent:'', value:'', hidden:false, disabled:false,
    classList:{toggle(){}}, getAttribute(name) {return this.attrs[name];}, setAttribute(name,value){this.attrs[name]=value;},
    addEventListener(name,fn){this[name]=fn;}, focus(){this.focused=true;}};}
  const buttons = ['estimate','compare','sources'].map(key => node({'data-estimate-view-btn':key,
    'data-download-href':'/download/'+key+'?q=бетон&types=material','data-download-label':key}));
  const panels = ['estimate','compare','sources'].map(key => node({'data-estimate-view-panel':key}));
  const ids = Object.fromEntries(['marketStatusMain','marketStatusDetail','marketLogs','marketLogDetails',
    'marketStartBtn','marketCityInput','estimateTableViewInput','activeTableDownloadBtn'].map(id=>[id,node()]));
  ids.marketCityInput.value='Ярославль';
  ids.estimatePageConfig={textContent:JSON.stringify({estimateId:'aabbcc',title:'Смета',marketRevision:'old',activeTableView:'estimate',crmPrefill:{},...config})};
  const context = {
    URL, AbortController, console,
    document:{getElementById:id=>ids[id] || null, querySelectorAll:selector=>selector.includes('view-btn')?buttons:
      selector.includes('view-panel')?panels:selector.includes('types')?[{value:'material'}]:[]},
    window:{AutoBotCrmBridge:bridge,crypto:require('node:crypto').webcrypto,
      sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
      location:{href:'http://localhost/estimates/aabbcc?q=бетон&types=material',replace(url){redirects.push(url);}}},
    fetch:async (url,options)=> {requests.push({url,options}); assert.ok(responses.length,'Unexpected fetch '+url);
      const result=responses.shift(); if (result instanceof Error) throw result; return result;},
    setTimeout(fn,ms){timers.push({fn,ms});return timers.length;},clearTimeout(){},setInterval(fn){context.poll=fn;},alert(message){context.lastAlert=message;}
  };
  context.window.setTimeout=context.setTimeout;
  vm.runInNewContext(source,context);
  return {context,ids,requests,responses,redirects,buttons,panels,timers,storage};
}

(async()=>{
  const first=harness({running:false,has_raw:false,has_merged:false,market_revision:'old'});
  await flush();
  assert.equal(first.redirects.length,0);
  first.responses.push(reply({running:true,has_raw:true,market_revision:'new',done:1,total:3}));
  await first.context.poll();
  assert.equal(first.redirects.length,0);
  assert.equal(first.ids.marketStartBtn.textContent,'Остановить поиск');
  first.responses.push(reply({running:false,has_raw:true,has_merged:true,market_revision:'new'}));
  await first.context.poll();
  assert.equal(first.redirects.length,1);
  const url=new URL(first.redirects[0]);
  assert.equal(url.searchParams.get('q'),'бетон');
  assert.equal(url.searchParams.get('types'),'material');
  assert.equal(url.searchParams.get('table_view'),'compare');
  first.responses.push(reply({running:false,has_raw:true,market_revision:'new'}));
  await first.context.poll();
  assert.equal(first.redirects.length,1,'Only one navigation for a published revision');

  const stored=harness({has_raw:true,market_revision:'old'});
  await flush();
  assert.equal(stored.redirects.length,0);
  assert.match(stored.ids.marketStatusMain.textContent,/Сохранённые цены/);
  stored.responses.push(reply({error:'Источник недоступен',has_raw:true,market_revision:'old',log_tail:['Одна','Две']}));
  await stored.context.poll();
  assert.equal(stored.redirects.length,0,'A failed repeat with old files cannot reload');
  assert.match(stored.ids.marketStatusDetail.textContent,/Источник недоступен/);
  assert.equal(stored.ids.marketLogs.textContent,'Одна\nДве');
  assert.equal(stored.ids.marketLogDetails.hidden,false);
  stored.responses.push(new Error('offline'));
  await stored.context.poll();
  assert.equal(stored.ids.marketStartBtn.disabled,true);
  stored.responses.push(reply({has_raw:true,market_revision:'old'}));
  await stored.context.poll();
  assert.equal(stored.ids.marketStartBtn.disabled,false);
  assert.equal(stored.ids.marketLogDetails.hidden,true);

  stored.buttons[1].click();
  assert.equal(stored.ids.estimateTableViewInput.value,'compare');
  assert.equal(stored.panels[1].hidden,false);
  assert.equal(stored.panels[0].hidden,true);
  assert.equal(stored.buttons[1].attrs['aria-selected'],'true');
  assert.ok(stored.ids.activeTableDownloadBtn.href.includes('/compare?q='));
  stored.buttons[2].disabled=true;
  stored.buttons[1].keydown({key:'ArrowRight',preventDefault(){}});
  assert.equal(stored.ids.estimateTableViewInput.value,'estimate');
  assert.equal(stored.buttons[0].focused,true);
  stored.responses.push(reply({ok:true}),reply({running:true,market_revision:'old'}));
  await stored.context.startEstimateMarket(); await flush();
  const start=stored.requests.find(request=>request.url.endsWith('/market-start'));
  const startBody=JSON.parse(start.options.body);
  assert.match(startBody.operation_id,/^[a-f0-9]{32}$/);
  assert.deepEqual({...startBody,operation_id:undefined},{city:'Ярославль',selected_types:['material'],operation_id:undefined});
  stored.responses.push(reply({ok:true}),reply({running:false,market_revision:'old'}));
  await stored.context.toggleEstimateMarket(); await flush();
  assert.ok(stored.requests.some(request=>request.url.endsWith('/market-stop')));
  stored.ids.estimateCrmProject={value:'',options:[{}]};
  stored.ids.estimateCrmSubmitBtn={};
  stored.ids.estimateCrmNewProjectFields={};
  stored.ids.estimateCrmTitle={};
  stored.context.syncEstimateCrmMode();
  assert.equal(stored.ids.estimateCrmSubmitBtn.disabled,true);
  assert.equal(stored.ids.estimateCrmNewProjectFields.hidden,true);
  assert.equal(stored.ids.estimateCrmTitle.required,false);
  stored.ids.estimateCrmProject.value='17';
  stored.context.syncEstimateCrmMode();
  assert.equal(stored.ids.estimateCrmSubmitBtn.disabled,false);
  const imports=[];
  const embedded=harness({}, {importCapability:'scoped-fixture'}, {embedded:true,available:true,
    async importEstimate(project,payload){imports.push({project,payload});return {result:{materials_sent:1}};},navigate(){return true;}});
  await flush();
  embedded.ids.estimateCrmProject={value:'17',options:[{}]};
  embedded.ids.estimateCrmSubmitBtn={};
  embedded.ids.estimateCrmStatus={classList:{toggle(){}},textContent:''};
  embedded.responses.push(reply({ok:true,items:[{name:'Бетон',planned_qty:10}],source:{id:'aabbcc'},sourceLabel:'Смета'}));
  await embedded.context.window.submitEstimateCrmForm({preventDefault(){}});
  assert.equal(imports.length,1);
  assert.equal(imports[0].project,'17');
  assert.equal(imports[0].payload.items[0].name,'Бетон');
  const prepared=embedded.requests.filter(request=>request.url.endsWith('/crm-import-payload'));
  assert.equal(prepared.length,1);
  assert.equal(prepared[0].options.method,'GET');
  assert.equal(prepared[0].options.headers['X-AutoBot-Estimate-Capability'],'scoped-fixture');
  assert.equal(prepared[0].options.credentials,'same-origin');
  assert.equal(embedded.requests.some(request=>request.options?.method==='POST'),false);
  assert.match(embedded.ids.estimateCrmStatus.textContent,/Готово: смета добавлена/);
  const lost=harness({running:false,market_revision:'old'});
  await flush();
  lost.responses.push(new Error('lost acknowledgement'),reply({running:false,market_revision:'old'}));
  await lost.context.startEstimateMarket();await flush();
  const firstKey=JSON.parse(lost.requests.find(r=>r.url.endsWith('/market-start')).options.body).operation_id;
  assert.equal(lost.storage.size,1);
  const restored=harness({running:false,market_revision:'old'},{},null,lost.storage);
  await flush();
  let release;
  restored.responses.push(new Promise(resolve=>{release=resolve;}),reply({running:true,run_id:firstKey,market_revision:'old'}));
  const pending=restored.context.startEstimateMarket();
  await restored.context.startEstimateMarket();
  assert.equal(restored.requests.filter(r=>r.url.endsWith('/market-start')).length,1,'Double click cannot add another launch');
  assert.equal(JSON.parse(restored.requests.at(-1).options.body).operation_id,firstKey,'A page reload reuses the lost request key');
  release(reply({ok:true,accepted:true,run_id:firstKey}));
  await pending;await flush();
  assert.equal(restored.storage.size,0);
  restored.responses.push(reply({ok:true}),reply({running:false,canceled:2,run_id:firstKey,market_revision:'old'}));
  await restored.context.stopEstimateMarket();await flush();
  assert.deepEqual(JSON.parse(restored.requests.find(r=>r.url.endsWith('/market-stop')).options.body),{run_id:firstKey});
  assert.match(restored.ids.marketStatusMain.textContent,/Поиск остановлен/);
  restored.responses.push(reply({ok:true}),reply({running:true,run_id:'f'.repeat(32),market_revision:'old'}));
  await restored.context.startEstimateMarket();await flush();
  const nextKey=JSON.parse(restored.requests.filter(r=>r.url.endsWith('/market-start')).at(-1).options.body).operation_id;
  assert.notEqual(nextKey,firstKey,'An explicit new search after cancellation has its own operation');
  console.log('Estimate workspace: revision, failures, tabs, CRM import, saved launch retry, reload, double submit and exact cancellation passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
