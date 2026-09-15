'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'../autobot/static/estimate_upload.js'),'utf8');
const STORAGE='autobot:estimate-upload-operation-v1';
const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>data});
async function flush(){for(let i=0;i<15;i++)await Promise.resolve();}
let sequence=0;
function harness(storage=new Map(),initial=[]) {
  const ids={},xhrs=[],fetches=[],timers=[],responses=[...initial];
  const node=()=>({hidden:true,value:'',textContent:'',disabled:false,style:{},files:[],
    classList:{toggle(){},remove(){}},addEventListener(event,fn){this[event]=fn;}});
  for(const name of ['Form','File','Title','Submit','Progress','Original','RetryStatus','New','FileName','Fill','Pct','Stage','Detail','Error','LogDetails','Logs'])ids['estimateUpload'+name]=node();
  ids.uploadStatus=node();
  ids.estimateUploadFile.files=[{name:'source.xlsx',size:4,lastModified:1}];
  ids.estimateUploadTitle.value='Школа';
  class FormData {
    constructor(){this.values={file:ids.estimateUploadFile.disabled?null:ids.estimateUploadFile.files[0],title:ids.estimateUploadTitle.value};}
    set(key,value){this.values[key]=value;}
  }
  class XMLHttpRequest {
    constructor(){this.upload={addEventListener:(name,fn)=>{this.progress=fn;}};xhrs.push(this);}
    open(method,url){this.method=method;this.url=url;}
    send(body){assert.ok(storage.get(STORAGE),'Operation must be remembered before network send');this.body=body;}
  }
  const context={AbortController,FormData,XMLHttpRequest,document:{getElementById:id=>ids[id]},
    window:{sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
      crypto:{randomUUID:()=>String(++sequence).padStart(32,'0')},location:{href:''}},
    fetch:async(url,options)=>{fetches.push({url,options});assert.ok(responses.length,'Unexpected fetch '+url);const result=responses.shift();if(result instanceof Error)throw result;return result;},
    setTimeout(fn,delay){timers.push({fn,delay,active:true});return timers.length-1;},clearTimeout(id){if(timers[id])timers[id].active=false;}};
  vm.runInNewContext(source,context);
  return {ids,xhrs,fetches,responses,storage,context,timers,
    submit(){ids.estimateUploadForm.submit({preventDefault(){}});},
    runTimer(delay){const item=timers.find(item=>item.active&&(delay===undefined?item.delay<10000:item.delay===delay));assert.ok(item,'Missing timer');item.active=false;item.fn();}};
}

(async()=>{
  const h=harness();h.submit();h.submit();
  assert.equal(h.xhrs.length,1,'Double submit cannot start a second upload');
  const key=JSON.parse(h.storage.get(STORAGE)).operation_id;
  assert.equal(h.xhrs[0].body.values.operation_id,key);
  assert.equal(h.xhrs[0].body.values.file.name,'source.xlsx','FormData captured before disabling the input');
  h.responses.push(response({ok:true,running:true,progress:30,stage:'Разбор'}));
  h.xhrs[0].status=0;h.xhrs[0].onerror();await flush();
  assert.ok(h.fetches[0].url.endsWith(key));
  assert.equal(JSON.parse(h.storage.get(STORAGE)).confirmed,true);
  assert.equal(h.ids.estimateUploadSubmit.disabled,true);
  const restored=harness(h.storage,[response({ok:true,running:false,result_ok:true,estimate_id:key,progress:100})]);
  await flush();
  assert.equal(restored.fetches.length,1);
  assert.equal(restored.xhrs.length,0,'Reload resumes status without reuploading the file');
  restored.runTimer(450);
  assert.equal(restored.context.window.location.href,'/estimates/'+key);
  assert.equal(h.storage.has(STORAGE),false);

  const pending=harness();pending.submit();
  const originalKey=JSON.parse(pending.storage.get(STORAGE)).operation_id;
  pending.responses.push(new Error('lost status'));
  pending.xhrs[0].status=0;pending.xhrs[0].onerror();await flush();
  assert.equal(pending.ids.estimateUploadSubmit.disabled,false);
  pending.submit();
  assert.equal(pending.xhrs[1].body.values.operation_id,originalKey);
  pending.responses.push(response({ok:true,running:false,result_ok:false,error:'Не распознано',progress:50,
    original_url:'/estimates/uploads/'+originalKey+'/original',log_tail:['Файл получен','Ошибка разбора']}));
  pending.xhrs[1].status=200;pending.xhrs[1].responseText=JSON.stringify({ok:true,job_id:originalKey,duplicate:true});pending.xhrs[1].onload();await flush();
  assert.equal(pending.ids.estimateUploadOriginal.hidden,false);
  assert.equal(pending.ids.estimateUploadOriginal.href,'/estimates/uploads/'+originalKey+'/original');
  assert.equal(pending.ids.estimateUploadLogs.textContent,'Файл получен\nОшибка разбора');
  assert.equal(pending.ids.estimateUploadSubmit.textContent,'Загрузить ещё раз');
  pending.submit();
  assert.notEqual(pending.xhrs[2].body.values.operation_id,originalKey,'Explicit retry of a terminal failure is a new operation');
  pending.xhrs[2].status=409;pending.xhrs[2].responseText=JSON.stringify({ok:false,message:'Другой файл'});pending.xhrs[2].onload();await flush();
  assert.equal(pending.ids.estimateUploadError.textContent,'Другой файл');
  assert.equal(pending.ids.estimateUploadSubmit.disabled,false);
  const previous=pending.xhrs[2].body.values.operation_id;
  pending.ids.estimateUploadTitle.value='Новая смета';pending.submit();
  assert.notEqual(pending.xhrs[3].body.values.operation_id,previous);

  const unknown=harness(new Map([[STORAGE,JSON.stringify({job_id:'a'.repeat(32),operation_id:'a'.repeat(32),confirmed:false,file_name:'source.xlsx'})]]),
    [response({ok:false,retry_upload:true,message:'Передача не завершена'},409)]);
  await flush();
  assert.equal(unknown.ids.estimateUploadSubmit.disabled,false);
  assert.match(unknown.ids.uploadStatus.textContent,/Передача не завершена/);
  assert.equal(unknown.storage.has(STORAGE),true);
  unknown.ids.estimateUploadNew.click();
  assert.equal(unknown.storage.has(STORAGE),false);
  assert.equal(unknown.ids.estimateUploadProgress.hidden,true);
  assert.equal(unknown.ids.estimateUploadSubmit.disabled,false);
  console.log('Estimate upload: lost response, persistent key, double submit, reload, same-key retry, terminal retry, conflict and failed original passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
