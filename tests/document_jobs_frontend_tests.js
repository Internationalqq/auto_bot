'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../autobot/static/document_jobs.js'), 'utf8');
const reply = (data, status = 200) => ({ok:status >= 200 && status < 300, status, json:async () => data});
async function flush() {for (let i=0;i<10;i++) await Promise.resolve();}
function harness(initial = {}) {
  const requests = [], timers = [], responses = [reply(initial)];
  const status = {hidden:true, textContent:'', focus() {}};
  const log = {textContent:''}, details = {hidden:true}, panel = {hidden:true};
  const button = rebuild => ({disabled:false, hasAttribute:name => rebuild && name === 'data-rebuild-report',
    addEventListener(name, handler) {this.click=handler;}});
  const buttons = [button(false),button(true)];
  let generated = 0, reloads = 0;
  const context = {
    document:{currentScript:{dataset:{documentJobs:'12345678'}}, querySelectorAll:() => buttons,
      getElementById:id => ({documentRefreshStatus:status,documentJobPanel:panel,documentJobLog:log,documentJobDetails:details})[id]},
    crypto:{randomUUID:() => `00000000-0000-4000-8000-${String(++generated).padStart(12,'0')}`},
    fetch:async (url, options) => {
      requests.push({url, options});
      assert.ok(responses.length, 'Unexpected fetch: '+url);
      const result = responses.shift();
      if (result instanceof Error) throw result;
      return result;
    },
    window:{location:{reload() {reloads++;}},setTimeout(callback,delay) {timers.push({callback,delay});}}
  };
  vm.runInNewContext(source,context);
  return {requests,timers,responses,status,buttons,log,details,panel,reloads:()=>reloads};
}

(async () => {
  const h = harness();
  await flush();
  h.responses.push(new Error('Connection lost'));
  await h.buttons[0].click();
  const first = JSON.parse(h.requests[1].options.body);
  assert.equal(h.buttons[0].disabled,false);
  const run = first.operation_id.replaceAll('-','');
  h.responses.push(reply({ok:true,run_id:run,duplicate:true}),reply({run_id:run,running:true,job_status:'queued'}));
  await h.buttons[0].click();
  await flush();
  assert.equal(JSON.parse(h.requests[2].options.body).operation_id,first.operation_id);
  assert.ok(h.requests[3].url.endsWith('&run_id='+run));
  assert.ok(h.status.textContent.includes('Задание сохранено'));
  assert.equal(h.buttons[1].disabled,true);
  const count=h.requests.length;
  await h.buttons[1].click();
  assert.equal(h.requests.length,count,'A second action cannot overlap the current job');
  h.responses.push(reply({run_id:run,running:false,job_status:'completed',exit_code:0}));
  await h.timers.shift().callback();
  await flush();
  assert.equal(h.reloads(),1);

  const stopped=harness({run_id:run,tender_id:'12345678',running:false,job_status:'interrupted',exit_code:-1,
    log_tail:['<img src=x onerror=alert(1)>','Документы сохранены']});
  await flush();
  assert.ok(stopped.status.textContent.includes('Исполнитель остановился'));
  assert.equal(stopped.status.hidden,false);
  assert.equal(stopped.buttons[0].disabled,false);
  assert.equal(stopped.reloads(),0);
  assert.equal(stopped.requests.length,1);
  assert.equal(stopped.panel.hidden,false);
  assert.equal(stopped.details.hidden,false);
  assert.equal(stopped.log.textContent,'<img src=x onerror=alert(1)>\nДокументы сохранены');

  const running=harness({run_id:run,tender_id:'12345678',running:true,job_status:'running'});
  running.responses.push(reply({run_id:run,running:true,job_status:'running'}));
  await flush();
  assert.ok(running.requests[1].url.endsWith('&run_id='+run));
  assert.equal(running.buttons[0].disabled,true);
  running.responses.push(reply({},503));
  await running.timers.shift().callback();
  assert.equal(running.timers[0].delay,5000);
  running.responses.push(reply({},404));
  await running.timers.shift().callback();
  assert.equal(running.buttons[0].disabled,false);
  assert.ok(running.status.textContent.includes('не найден'));
  assert.equal(running.timers.length,0);

  const rejected=harness();
  await flush();
  rejected.responses.push(reply({ok:false,message:'Busy'},409));
  await rejected.buttons[1].click();
  const rejectedId=JSON.parse(rejected.requests[1].options.body).operation_id;
  rejected.responses.push(new Error('Connection lost'));
  await rejected.buttons[1].click();
  const next=JSON.parse(rejected.requests[2].options.body);
  assert.notEqual(next.operation_id,rejectedId);
  assert.equal(next.tender_id,'12345678');
  assert.equal(rejected.requests[2].url,'/api/reports/rebuild');
  console.log('Document jobs: stable retry, run-specific polling, reopening, interruption, duplicate action, backoff and explicit rejection passed');
})().catch(error => {console.error(error);process.exitCode=1;});
