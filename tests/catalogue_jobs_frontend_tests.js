'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../autobot/templates/tenders.html'), 'utf8');
// Exercise the actual request boundary without reproducing the catalogue DOM.
const start = html.indexOf('    async function postJson(');
const end = html.indexOf('    function closeTenderDeleteDialog(', start);
assert.ok(start >= 0 && end > start);
const runId = 'a'.repeat(32);
const reply = (data, status=200) => ({ok:status >= 200 && status < 300, status, json:async () => data});
function harness(legacy=false) {
  const requests=[], responses=[];
  let next=0;
  const context=vm.createContext({Uint8Array,
    crypto: legacy ? {getRandomValues: bytes => bytes.fill(++next)} : {randomUUID: () => String(++next).padStart(32,'0')},
    fetch: async (url, options) => {
      requests.push({url, body:JSON.parse(options.body)});
      const response=responses.shift();
      if (response instanceof Error) throw response;
      assert.ok(response, 'Unexpected request');
      return response;
    }
  });
  vm.runInContext(html.slice(start,end),context);
  return {requests,responses,post:context.postMainJob,summary:context.finishedWorkSummary};
}
(async () => {
  const h=harness();
  const url='/api/parse-tenders', body={max_pages:2,max_tenders:3,days_back:7};
  h.responses.push(new Error('Connection lost'),reply({ok:true,run_id:runId}));
  await assert.rejects(h.post(url,body),/Connection lost/);
  await h.post(url,body);
  assert.equal(h.requests[0].body.operation_id,h.requests[1].body.operation_id);
  assert.equal(body.operation_id,undefined);

  h.responses.push(reply({message:'Unavailable'},503),reply({ok:true}),reply({ok:true,run_id:runId}));
  await assert.rejects(h.post(url,body),/Unavailable/);
  await assert.rejects(h.post(url,body),/подтвердить запуск/);
  await h.post(url,body);
  assert.notEqual(h.requests[2].body.operation_id,h.requests[0].body.operation_id);
  assert.equal(h.requests[2].body.operation_id,h.requests[3].body.operation_id);
  assert.equal(h.requests[2].body.operation_id,h.requests[4].body.operation_id);

  h.responses.push(reply({message:'Busy'},409),reply({ok:true,run_id:runId}));
  await assert.rejects(h.post(url,body),/Busy/);
  await h.post(url,body);
  assert.notEqual(h.requests[5].body.operation_id,h.requests[6].body.operation_id);

  h.responses.push(new Error('Connection lost'),reply({ok:true,run_id:runId}));
  await assert.rejects(h.post(url,body));
  await h.post('/api/reports/rebuild',{tender_id:'12345678'});
  assert.notEqual(h.requests[7].body.operation_id,h.requests[8].body.operation_id);
  assert.equal(h.requests[8].body.tender_id,'12345678');

  const fallback=harness(true);
  fallback.responses.push(reply({ok:true,run_id:runId}));
  await fallback.post('/api/tenders/12345678/refresh-documents',{});
  assert.match(fallback.requests[0].body.operation_id,/^[0-9a-f]{32}$/);
  const state=h.summary({exit_code:-1,job_status:'interrupted',task:'Разбор',log_tail:['Файл сохранён']},
    {last_summary:'Сравнение завершено',log_tail:[]});
  assert.equal(state.failed,true);
  assert.match(state.summary,/исполнитель остановился/);
  assert.match(state.summary,/Сравнение завершено/);
  assert.ok(state.logs.includes('\nФайл сохранён'));
  assert.equal(h.summary({exit_code:0,job_status:'completed'},{}).failed,false);
  assert.equal(h.summary({},{}).failed,false);
  console.log('Catalogue jobs: lost response, ambiguous failure, confirmed retry, conflict, changed action and UUID fallback passed');
})().catch(error => {console.error(error);process.exitCode=1;});
