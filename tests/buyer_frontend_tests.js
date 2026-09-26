const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node() {
  return {textContent: '', hidden: false, disabled: false, events: {}, children:[],
    append(...values) {this.children.push(...values);},
    querySelectorAll() {return [];},
    setAttribute() {},
    addEventListener(name, fn) { this.events[name] = fn; }};
}
const start = node(), cancel = node(), refresh = node(), status = node();
const mode = {...node(),value:'draft'};
const list = {...node(), children: [], querySelectorAll() { return []; },
  contains() { return false; }, replaceChildren() { this.children = []; }};
const elements = {'[data-buyer-start]':start, '[data-buyer-cancel]':cancel,
  '[data-buyer-refresh]':refresh, '[data-buyer-status]':status, '[data-buyer-list]':list, '[data-buyer-mode]':mode};
const root = {dataset: {buyer:'123456789012345'}, querySelector(s) { return elements[s]; }};
const rows = [{value:'cable', checked:false}, {value:'work', checked:false}];
const events = {}, posts = [];
let selectionObserver;
const counter = {};
Object.defineProperty(counter, 'textContent', {set() { queueMicrotask(() => selectionObserver?.()); }});
const document = {hidden:false, activeElement:null,
  createElement() {return node();},
  querySelector(s) { return s === '[data-buyer]' ? root : s === '#agentSelectedCount' ? counter : null; },
  querySelectorAll(s) { return s === '[data-agent-position]:checked' ? rows.filter(r => r.checked) : []; },
  addEventListener(name, fn) { events[name] = fn; }};
const context = {document, console, setInterval() {},
  MutationObserver: class {constructor(fn) { selectionObserver = fn; } observe() {}},
  async fetch(url, options) {
    if (options.method === 'POST') posts.push(JSON.parse(options.body));
    return {ok:true, async json() {return {ok:true, jobs:[]};}};
  }};
vm.runInNewContext(fs.readFileSync('autobot/static/buyer.js','utf8'), context);
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await flush();
  rows[0].checked = true; events.change();
  assert.match(start.textContent, /\(1\)/);
  start.events.click(); await flush();
  assert.deepEqual(posts.at(-1), {action:'search_suppliers',position_keys:['cable']});
  // Bulk clear / successful price search change checkboxes without change events.
  rows.forEach(r => r.checked = false); counter.textContent = '0'; await flush();
  assert.equal(start.textContent, 'Подобрать поставщиков');
  start.events.click(); await flush();
  assert.deepEqual(posts.at(-1), {action:'search_suppliers'});
  rows.forEach(r => r.checked = true); counter.textContent = '2'; await flush();
  assert.match(start.textContent, /\(2\)/);
  start.events.click(); await flush();
  assert.deepEqual(posts.at(-1), {action:'search_suppliers',position_keys:['cable','work']});
  assert.equal(start.disabled, false);
  assert.equal(list.children.length,1,'Empty workspace should render a useful starting state');
  mode.value='email';mode.events.change();
  assert.match(start.textContent,/Найти и отправить запросы/);
  start.events.click(); start.events.click(); await flush();
  assert.deepEqual(posts.at(-1),{action:'search_suppliers',position_keys:['cable','work'],delivery:'email'});
  assert.equal(posts.length,4,'A second click during a pending launch must not send another request');
  console.log('Buyer scope: manual/bulk selection, external clear, caption and POST stay consistent.');
})().catch(error => { console.error(error); process.exitCode = 1; });
