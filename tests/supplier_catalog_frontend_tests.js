const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

async function main(){
  const fields=Object.fromEntries(['data-item-count','data-last-import','data-job-status'].map(k=>[k,{textContent:''}]));
  const button={dataset:{},textContent:'Загрузить',disabled:false};
  const row={dataset:{sourceId:'source'},querySelector(selector){return selector==='[data-import-source]'?button:fields[selector.slice(1,-1)];}};
  const listeners={};
  let snapshot={sources:[{id:'source',item_count:0,last_import_at:null}],jobs:[]};
  const document={hidden:false,getElementById:()=>({dataset:{}}),
    querySelector:()=>row,querySelectorAll:selector=>selector==='[data-source-id]'?[row]:[],
    addEventListener:(name,handler)=>{listeners[name]=handler;}};
  const context=vm.createContext({document,Intl,Date,URL,clearTimeout,setTimeout,
    fetch:async()=>({ok:true,json:async()=>snapshot})});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../autobot/static/supplier_catalog.js'),'utf8'),context);
  await new Promise(setImmediate);
  assert.equal(fields['data-last-import'].textContent,'Ещё не загружен');
  snapshot={sources:[{id:'source',item_count:12,last_import_at:Date.UTC(2026,8,20)/1000}],
    jobs:[{source_id:'source',id:'job',status:'completed',processed:3,discovered:3,errors:0}]};
  listeners.visibilitychange();await new Promise(setImmediate);
  assert.equal(fields['data-item-count'].textContent,12);
  assert.equal(fields['data-last-import'].textContent,'20.09.2026');
  assert.match(fields['data-job-status'].textContent,/Обход завершён: 3 из 3/);
  assert.equal(button.textContent,'Обновить');
  snapshot.sources[0].last_import_at=Date.UTC(2026,8,21)/1000;
  listeners.visibilitychange();await new Promise(setImmediate);
  assert.equal(fields['data-last-import'].textContent,'21.09.2026');
  console.log('Supplier catalogue: initial import, completed progress and refreshed date remain consistent.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
