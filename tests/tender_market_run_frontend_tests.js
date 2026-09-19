const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const template = fs.readFileSync(path.join(__dirname, '../autobot/templates/tender_detail.html'), 'utf8');
const start = template.indexOf('    function agentMarketRunView(data) {');
const end = template.indexOf('    async function refreshAgentJobs()', start);
assert.ok(start >= 0 && end > start);
const context = vm.createContext({});
vm.runInContext(template.slice(start, end), context);
const history = { progress: { total: 121, processed: 117, percent: 97 },
  results: [{ title: 'Old accepted quote' }], result_totals: { verified: 1 } };
const offer = { title: 'Current candidate', verification: 'candidate' };
const run = { id: 'current', total: 4, processed: 2, percent: 50, status: 'running',
  verified_offers: 0, candidate_offers: 1, offers_found: 1,
  results: [offer], positions: [{status:'completed', updated_at:2, reason:'No exact match'},
    {status:'leased',updated_at:3}, {status:'queued',updated_at:1}] };
const view = context.agentMarketRunView({ ...history, latest_run: run });
assert.equal(view.progress.total, 4);
assert.equal(view.progress.percent, 50);
assert.equal(view.progress.remaining, 2);
assert.equal(view.progress.running, true);
assert.equal(view.progress.recent.length, 1);
assert.equal(view.progress.recent[0].error, 'No exact match');
assert.equal(view.results[0], offer);
assert.equal(view.totals.verified, 0);
const reopened = context.agentMarketRunView({ ...history,
  latest_run: { ...run, status:'completed', processed:4, percent:100, canceled:1 } });
assert.equal(reopened.progress.running, false);
assert.equal(reopened.progress.remaining, 0);
assert.equal(reopened.progress.canceled, 1);
assert.equal(context.agentMarketRunView(history).progress.total, 121);
console.log('Current run stays separate from prior attempts; finished/reopened and legacy views passed.');
