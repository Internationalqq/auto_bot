'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../autobot/static/estimate_review.js'),'utf8');
const fields=['name','basis_code','type','unit','qty','unit_price','total'];
const values={name:'Бетон',basis_code:'',type:'material',unit:'м3',qty:2,unit_price:100,total:200};
const response=(data,status=200)=>({ok:status<400,status,json:async()=>data});
const current=(version='c'.repeat(64),row=values)=>({ok:true,version,revision:1,row,history:[],next_before:null});
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
function harness(storage=new Map(),initial=[]) {
  function node() {return {value:'',textContent:'',dataset:{},hidden:false,disabled:false,children:[],classList:{toggle(){}},
    addEventListener(name,callback){this[name]=callback;},append(...children){this.children.push(...children);},appendChild(child){this.children.push(child);},replaceChildren(){this.children=[];}};}
  const ids=Object.fromEntries(['correctionForm','correctionSave','correctionRefresh','correctionStatus','correctionMore','correctionHistory',
    'correction-reason',...fields.map(name=>'correction-'+name)].map(id=>[id,node()]));
  fields.forEach(name=>ids['correction-'+name].value=String(values[name]));
  ids['correction-reason'].value='Сверено с файлом';ids.correctionRefresh.hidden=true;
  ids.correctionConfig={textContent:JSON.stringify({estimateId:'a'.repeat(16),positionId:'pdf:1:2',version:'a'.repeat(64),fields,labels:{},values})};
  const requests=[],responses=[...initial],label=node();
  const context={console,AbortController,crypto:require('node:crypto').webcrypto,
    document:{getElementById:id=>ids[id],createElement:node,querySelector:()=>label,querySelectorAll:()=>[]},
    sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    setTimeout:()=>1,clearTimeout(){},fetch:async(url,options)=>{requests.push({url,options});assert.ok(responses.length,'Unexpected request '+url);
      let value=responses.shift();if(typeof value==='function')value=await value();if(value instanceof Error)throw value;return value;}};
  vm.runInNewContext(source,context);
  return {ids,storage,requests,responses,label,submit:()=>ids.correctionForm.submit({preventDefault(){}})};
}
(async()=>{
  const lost=harness();lost.ids['correction-total'].value='250.01';lost.responses.push(new Error('lost response'));
  await lost.submit();assert.equal(lost.requests.length,1);assert.equal(lost.ids['correction-total'].value,'250.01');assert.equal(lost.storage.size,1);
  const first=JSON.parse(lost.requests[0].options.body);assert.match(first.operation_id,/^[a-f0-9]{32}$/);
  const restored=harness(lost.storage,[response({ok:false,message:'Not found'},404)]);await flush();
  assert.equal(restored.ids['correction-total'].value,'250.01');assert.equal(restored.ids.correctionSave.disabled,false);
  restored.responses.push(response({ok:true,version:'c'.repeat(64),revision:1,duplicate:true}),response(current('c'.repeat(64),{...values,total:'250.01'})));
  await restored.submit();const repeated=JSON.parse(restored.requests[1].options.body);
  assert.deepEqual(repeated,first);assert.equal(restored.storage.size,0);assert.match(restored.ids.correctionStatus.textContent,/Сохранена редакция 1/);
  assert.equal(restored.ids['correction-reason'].value,'');

  const pending=harness();pending.responses.push(new Error('lost'));await pending.submit();
  const acknowledged=harness(pending.storage,[response({ok:true,version:'c'.repeat(64),revision:1}),response(current())]);await flush();
  assert.equal(acknowledged.requests.length,2);assert.equal(acknowledged.storage.size,0);assert.match(acknowledged.ids.correctionStatus.textContent,/сохранение найдено/);

  const conflict=harness();conflict.ids['correction-name'].value='Введённое название';
  conflict.responses.push(response({ok:false,message:'Уже исправлено'},409));await conflict.submit();
  assert.equal(conflict.ids['correction-name'].value,'Введённое название');assert.equal(conflict.ids.correctionSave.disabled,true);
  assert.equal(conflict.ids.correctionRefresh.hidden,false);assert.equal(conflict.storage.size,0);
  conflict.responses.push(response(current('d'.repeat(64),{...values,name:'Из другой вкладки'})));await conflict.ids.correctionRefresh.click();
  assert.equal(conflict.ids['correction-name'].value,'Из другой вкладки');assert.equal(conflict.ids.correctionSave.disabled,false);

  const double=harness();let resolve;const gate=new Promise(done=>resolve=done);
  double.responses.push(()=>gate,response(current()));const saving=double.submit();await double.submit();assert.equal(double.requests.length,1);
  resolve(response({ok:true,version:'c'.repeat(64),revision:1}));await saving;assert.equal(double.requests.length,2);

  const missing=harness();missing.ids['correction-reason'].value=' ';await missing.submit();assert.equal(missing.requests.length,0);
  const invalid=harness();invalid.responses.push(response({ok:false,message:'Неверное число'},400));await invalid.submit();assert.equal(invalid.storage.size,0);
  assert.equal(invalid.ids.correctionSave.disabled,false);assert.match(invalid.ids.correctionStatus.textContent,/Неверное число/);
  console.log('Estimate review: saved revision, lost acknowledgement, reload recovery, version conflict, input preservation, validation and double submit passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
