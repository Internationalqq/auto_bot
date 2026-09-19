const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('autobot/templates/tender_detail.html', 'utf8');
const functionText = html.slice(html.indexOf('    function openWorkspace('), html.indexOf('    if (document.querySelector("[data-workspace-panel]"))'));
function node(name) {
  return {dataset:{workspacePanel:name,workspaceLink:name}, hidden:false, attrs:{}, classes:new Set(),
    setAttribute(k,v){this.attrs[k]=v;}, removeAttribute(k){delete this.attrs[k];},
    focus(){this.focused=true;}, scrollIntoView(){this.scrolled=true;},
    classList:{toggle(){}}};
}
const panels=['positions','search','economics'].map(node);
const links=['positions','search','economics'].map(node);
const positionContext=[node('summary')];
const changes=[];
const context={document:{querySelectorAll(selector){return selector==='[data-workspace-panel]'?panels:selector==='[data-position-context]'?positionContext:links;}},
  window:{location:{hash:''},history:{replaceState(a,b,hash){changes.push(hash);context.window.location.hash=hash;}}}};
vm.createContext(context);vm.runInContext(functionText,context);
context.openWorkspace('',false);
assert.deepEqual(panels.map(p=>p.hidden),[false,true,true]);
assert.equal(changes.length,0,'Opening the tender must not scroll past its header');
context.openWorkspace('search',true);
assert.deepEqual(panels.map(p=>p.hidden),[true,false,true]);
assert.equal(positionContext[0].hidden,true,'Position guidance must not repeat on the search tab');
assert.equal(links[1].attrs['aria-current'],'page');
assert.equal(links[0].attrs['aria-current'],undefined);
assert.equal(panels[1].focused,true);assert.equal(panels[1].scrolled,true);
assert.equal(context.window.location.hash,'#search');
context.openWorkspace('economics',true);
assert.deepEqual(panels.map(p=>p.hidden),[true,true,false]);
context.openWorkspace('unknown-fragment',false);
assert.deepEqual(panels.map(p=>p.hidden),[false,true,true]);
assert.equal(context.window.location.hash,'#positions');
assert.equal(positionContext[0].hidden,false);
console.log('Tender workspace: initial view, direct navigation, focus, hidden panels and unknown fragments passed.');
