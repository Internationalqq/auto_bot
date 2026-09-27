const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync('autobot/static/tender_progress.js','utf8');
const flush = async () => { for (let n=0;n<8;n++) await Promise.resolve(); };

function setup(responses) {
  const listeners={}, timers=new Map(); let sequence=0, calls=0, observer;
  const label={textContent:''}, prices={textContent:''};
  const bar={hidden:false,setAttribute(){}};
  const retry={hidden:true,addEventListener(type,fn){this[type]=fn;}};
  const card={dataset:{progressTender:'0171200001926000664'},setAttribute(){},querySelector(selector){return {'strong':label,'progress':bar,'[data-progress-prices]':prices,'[data-progress-retry]':retry}[selector];}};
  const document={hidden:false,addEventListener(type,fn){listeners[type]=fn;},querySelectorAll(selector){return selector==='[data-progress-tender]'?[card]:[];}};
  vm.runInNewContext(script,{location:{pathname:'/tenders'},document,AbortController,Date,
    fetch:async () => { calls++; const value=responses.shift(); if(value instanceof Error) throw value; return {ok:true,json:async()=>value}; },
    setInterval(){},setTimeout(fn,ms){timers.set(++sequence,{fn,ms});return sequence;},clearTimeout(id){timers.delete(id);},
    IntersectionObserver:class {constructor(fn){observer=fn;} observe(){} unobserve(){}}
  });
  return {label,prices,bar,retry,listeners,document,timers,get calls(){return calls;},show(){observer([{isIntersecting:true,target:card}]);}};
}
(async()=>{
  const good={ok:true,total:259,processed:214,verified:28};
  const s=setup([new Error('temporary outage'),good]);
  s.show(); await flush();
  assert.equal(s.bar.hidden,true); assert.equal(s.retry.hidden,false);
  assert.equal(s.timers.size,1,'Only the retry timer survives a failed request');
  [...s.timers.values()][0].fn(); await flush();
  assert.equal(s.calls,2); assert.equal(s.bar.hidden,false,'Successful retry restores the actual progress bar');
  assert.equal(s.bar.max,259); assert.equal(s.bar.value,214); assert.equal(s.retry.hidden,true);
  assert(s.prices.textContent.includes('28')); assert(s.label.textContent.includes('214 из 259'));

  const capped=setup([new Error(),new Error(),new Error(),good]);
  capped.show(); await flush();
  for(let n=0;n<2;n++){const next=[...capped.timers.entries()][0];capped.timers.delete(next[0]);next[1].fn();await flush();}
  assert.equal(capped.calls,3); assert.equal(capped.timers.size,0,'Background retries are bounded');
  capped.retry.click(); await flush();
  assert.equal(capped.calls,4); assert.equal(capped.bar.value,214,'Manual recovery remains available');

  const invalid=setup([{ok:true,total:2,processed:3,verified:0}]);
  invalid.show(); await flush();
  assert.equal(invalid.bar.hidden,true,'Invalid totals must not render a made-up percentage');
  console.log('Tender progress: temporary failure recovery, retry cap, manual retry and valid counts passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
