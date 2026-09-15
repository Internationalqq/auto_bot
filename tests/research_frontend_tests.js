const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element() {
  return { value:'', textContent:'', disabled:false, children:[], focused:false,
    classList:{add(){},remove(){}}, addEventListener(){}, focus(){this.focused=true;},
    replaceChildren(){this.children=[];}, appendChild(child){this.children.push(child);},
    querySelector(){return this.label || null;} };
}

async function run() {
  const ids = Object.fromEntries(['researchQueries','researchCity','researchRunBtn','researchStatus','researchResults'].map(id=>[id,element()]));
  ids.researchRunBtn.label=element();
  const calls=[];
  let finish;
  const context = vm.createContext({URL, Intl, console, document:{getElementById:id=>ids[id], createElement:element, body:element()},
    fetch:(url, options)=>{calls.push({url,options}); return new Promise(resolve=>{finish=resolve;});}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../autobot/static/research.js'),'utf8'),context);
  context.fillExample();
  assert.equal(ids.researchQueries.value.split('\n').length,2);
  assert.ok(ids.researchQueries.value.includes(' | м\n'));
  ids.researchQueries.value=' ';
  await context.runResearch();
  assert.equal(calls.length,0);
  assert.equal(ids.researchQueries.focused,true);
  context.fillExample();
  ids.researchCity.value='Ярославль';
  const first=context.runResearch();
  await context.runResearch();
  assert.equal(calls.length,1); // Keyboard repeats cannot create a concurrent search.
  assert.equal(ids.researchRunBtn.disabled,true);
  assert.equal(calls[0].url,'/research/items');
  assert.equal(JSON.parse(calls[0].options.body).queries,ids.researchQueries.value);
  assert.equal(JSON.parse(calls[0].options.body).city,'Ярославль');
  finish({ok:false,json:async()=>({ok:false,message:'Источник временно недоступен'})});
  await first;
  assert.equal(ids.researchRunBtn.disabled,false);
  assert.equal(ids.researchStatus.textContent,'Источник временно недоступен');
  const retry=context.runResearch();
  finish({ok:true,json:async()=>({ok:true,results:[{query:'<script>bad()</script>', offers:[{price:100,verified:false,url:'javascript:bad()',title:'<b>candidate</b>'}]}]})});
  await retry;
  assert.equal(calls.length,2);
  const card=ids.researchResults.children[0];
  assert.equal(card.children[0].textContent,'<script>bad()</script>');
  const offer=card.children.at(-1).children[0];
  assert.equal(offer.children[1].href,'#');
  assert.equal(offer.children[1].textContent,'<b>candidate</b>');
  assert.equal(offer.children[0].children[1].textContent,'не принято в расчёт');
  assert.equal(context.safeSourceUrl('https://supplier.example/item'),'https://supplier.example/item');
  console.log('Research frontend: example, empty input, duplicate, error/retry, candidates and safe links passed');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
