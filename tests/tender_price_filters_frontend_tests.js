const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('autobot/templates/tender_detail.html', 'utf8');
const script = html.slice(html.indexOf('    function filterRows()'), html.indexOf('    function selectedAgentKeys()'));

function element(dataset = {}) {
  const classes = new Set();
  return {dataset, hidden: false, value: '', textContent: '', attrs: {}, listeners: {},
    classList: {toggle(name, on) {if (on) classes.add(name); else classes.delete(name);}},
    setAttribute(k, v) {this.attrs[k] = v;}, focus() {this.focused = true;},
    addEventListener(name, callback) {this.listeners[name] = callback;},
    fire(name = 'click') {this.listeners[name]();}};
}
function fixture(search = '') {
  const rows = [
    ['verified', 'materials', 'кабель', 'a', 'a1', '1', '1'],
    ['candidate', 'materials', 'краска', 'a', 'a2', '1', '1'],
    ['verified', 'works', 'монтаж', 'b', 'b1', '1', '1'],
    ['no_quote', 'works', 'демонтаж', 'b', 'b2', '0', '1'],
    ['excluded', 'other', 'надбавка', 'c', 'c1', '0', '0'],
    ['pending', 'materials', 'песок', 'c', 'c2', '0', '0'],
    ['candidate', 'materials', 'цена по запросу', 'c', 'c2', '0', '1'],
  ].map(([priceState, bucket, search, fileKey, sectionKey, marketFound, marketProcessed], i) =>
    element({priceState, bucket, search, fileKey, sectionKey, marketFound, marketProcessed, positionKey: String(i)}));
  const sections = ['a1', 'a2', 'b1', 'b2', 'c1', 'c2'].map(sectionKey => element({sectionKey}));
  const files = ['a', 'b', 'c'].map(fileKey => element({fileKey}));
  const prices = ['all', 'found', 'verified'].map(priceFilter => element({priceFilter}));
  const buckets = ['all', 'materials', 'works', 'other', 'processed'].map(bucketFilter => element({bucketFilter}));
  const reset = element(), emptyReset = element();
  const ids = Object.fromEntries(['positionSearch', 'priceStateFilter', 'shownCount', 'noPositionResults', 'priceFilterHint'].map(id => [id, element()]));
  ids.priceStateFilter.value = 'all';
  ids.priceStateFilter.options = ['all', 'found', 'verified', 'candidate', 'missing', 'pending', 'no_quote', 'excluded', 'blocked', 'needs_details'].map(value => ({value}));
  const selectors = {'[data-position-row]': rows, '[data-section-row]': sections,
    '[data-file-row]': files, '[data-price-filter]': prices, '[data-bucket-filter]': buckets,
    '[data-reset-position-filters]': [reset, emptyReset]};
  const location = new URL('https://app.example/tenders/test?embed=1' + search + '#positions');
  const context = {activeBucket: 'all', URL, URLSearchParams, document: {
    getElementById: id => ids[id], querySelectorAll: selector => selectors[selector] || [],
    querySelector: selector => selector === '.reset-position-filters' ? reset : prices[0]},
    window: {location, history: {replaceState(_, title, url) {
      context.window.location = new URL(url, context.window.location.href);
    }}}};
  vm.createContext(context); vm.runInContext(script, context);
  return {rows, sections, files, prices, buckets, reset, emptyReset, ids, context,
    visible: () => rows.filter(r => !r.hidden).map(r => r.dataset.positionKey),
    price(value) {prices.find(p => p.dataset.priceFilter === value).fire();},
    bucket(value) {buckets.find(p => p.dataset.bucketFilter === value).fire();},
    search(value) {ids.positionSearch.value = value; ids.positionSearch.fire('input');},
    state(value) {ids.priceStateFilter.value = value; ids.priceStateFilter.fire('change');}};
}
const f = fixture();
assert.equal(f.visible().length, 7);
assert.equal(f.reset.hidden, true);
f.price('found');
assert.deepEqual(f.visible(), ['0', '1', '2'], 'Found prices omit requests without a numeric price');
assert.equal(f.ids.shownCount.textContent, '3');
assert.equal(f.ids.priceStateFilter.value, 'found');
assert.equal(f.prices[1].attrs['aria-pressed'], 'true');
assert.deepEqual(f.files.map(x => x.hidden), [false, false, true]);
assert.deepEqual(f.sections.map(x => x.hidden), [false, false, false, true, true, true]);
f.price('verified');
assert.deepEqual(f.visible(), ['0', '2']);
assert.equal(f.prices[1].attrs['aria-pressed'], 'false');
f.bucket('materials');
assert.deepEqual(f.visible(), ['0']);
f.search(' КАБЕЛЬ ');
assert.deepEqual(f.visible(), ['0'], 'Search combines with both filters and normalizes Russian case');
f.search('несуществующая позиция');
assert.deepEqual(f.visible(), []);
assert.equal(f.ids.noPositionResults.hidden, false);
assert.ok(f.files.every(x => x.hidden) && f.sections.every(x => x.hidden));
f.emptyReset.fire();
assert.equal(f.visible().length, 7);
assert.equal(f.ids.positionSearch.value, '');
assert.equal(f.ids.priceStateFilter.value, 'all');
assert.equal(f.buckets[0].attrs['aria-pressed'], 'true');
assert.equal(f.prices[0].focused, true);
assert.equal(f.ids.noPositionResults.hidden, true);
assert.equal(f.context.window.location.search, '?embed=1');
f.state('candidate');
assert.deepEqual(f.visible(), ['1', '6']);
assert.ok(f.prices.every(p => p.attrs['aria-pressed'] === 'false'), 'Advanced statuses cannot leave a contradictory quick filter selected');
f.state('missing');
assert.deepEqual(f.visible(), ['1', '3', '5', '6']);
f.bucket('processed');
assert.deepEqual(f.visible(), ['1', '3', '6']);
f.reset.fire();
f.price('found');
assert.equal(f.context.window.location.search, '?embed=1&prices=found');
assert.equal(f.context.window.location.hash, '#positions');
assert.deepEqual(fixture('&prices=found').visible(), ['0', '1', '2']);
assert.deepEqual(fixture('&prices=verified').visible(), ['0', '2']);
assert.equal(fixture('&prices=unknown').visible().length, 7);
const empty = fixture(); empty.rows.length = 0; empty.context.filterRows();
assert.equal(empty.ids.shownCount.textContent, '0');
assert.equal(empty.ids.noPositionResults.hidden, false);
console.log('Tender price filters: found/verified evidence, combined search/type/status, empty groups, reset, links and reload passed.');
