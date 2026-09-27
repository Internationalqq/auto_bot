const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let focused;
class Element {
  constructor(dataset={}) { this.dataset=dataset; this.events={}; this.children=[]; this.hidden=false; }
  addEventListener(name, fn) { (this.events[name] ||= []).push(fn); }
  emit(name, event={target:this}) { (this.events[name] || []).forEach(fn=>fn(event)); }
  focus() { focused=this; }
  contains(element) { return this === element || this.children.some(child=>child.contains(element)); }
  closest(selector) { return selector === '[data-source-preview]' && this.dataset.sourcePreview != null ? this : null; }
  cloneNode() { return new Element({...this.dataset}); }
  replaceChildren(...children) { this.children=children; }
  showModal() { this.open=true; }
  close() { this.open=false; this.emit('close'); }
}
const ids=['sourcePreviewDrawer','sourcePreviewTitle','sourcePreviewBack','sourceOffersBody','sourceOffersList','sourceOffersPosition','sourcePreviewBody','sourcePreviewFooter','sourcePreviewPrice','sourcePreviewBasis','sourcePreviewCompare','sourcePreviewRatio','sourcePreviewReason','sourcePreviewEvidence','sourcePreviewMeta','sourcePreviewRegionSection','sourcePreviewRegionEvidence','sourcePreviewOpen','sourcePreviewAudit'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element()]));
const dialog=elements.sourcePreviewDrawer, close=new Element();
dialog.querySelector=()=>close;
const source=new Element({sourcePreview:'',sourceTitle:'Поставщик <script>',sourcePrice:'200 ₽',sourceComparisonPrice:'100 ₽',sourcePriceBasis:'За упаковку',sourceRatio:'1,2',sourceReason:'Нужно уточнить доставку',sourceEvidence:'<img src=x onerror=bad()>',sourceObserved:'1760000000',sourceLocation:'Ярославль',sourceSupplier:'Поставщик',sourceDelivery:'Доставка по области',sourceUrl:'https://example.org/offer',sourceAudit:'records/check one.json'});
const trigger=new Element({positionTitle:'Щебень, объём 57,859 м³',positionNumber:'5'});
trigger.parentElement={querySelector:()=>({children:[source]})};
const document=new Element();
document.getElementById=id=>elements[id];
document.querySelectorAll=()=>[trigger];
vm.runInNewContext(fs.readFileSync(require.resolve('../autobot/static/tender_sources.js'),'utf8'),{document,Date});

trigger.emit('click');
assert.equal(dialog.open,true);
assert.equal(elements.sourceOffersPosition.textContent,trigger.dataset.positionTitle);
assert.equal(elements.sourceOffersList.children.length,1);
assert.equal(elements.sourceOffersBody.hidden,false);
assert.equal(elements.sourcePreviewBody.hidden,true);
assert.equal(focused,elements.sourcePreviewTitle);
const offer=elements.sourceOffersList.children[0];
assert.notEqual(offer,source,'Opening the list must preserve the original row data');
document.emit('click',{target:offer});
assert.equal(elements.sourceOffersBody.hidden,true);
assert.equal(elements.sourcePreviewBody.hidden,false);
assert.equal(elements.sourcePreviewTitle.textContent,'Поставщик <script>');
assert.equal(elements.sourcePreviewEvidence.textContent,'<img src=x onerror=bad()>','Supplier evidence is text, not HTML');
assert.equal(elements.sourcePreviewCompare.textContent,'В единице сметы: 100 ₽');
assert.equal(elements.sourcePreviewRegionSection.hidden,false);
assert.equal(elements.sourcePreviewOpen.href,'https://example.org/offer');
assert.equal(elements.sourcePreviewAudit.href,'/tenders/market-audit?record=records%2Fcheck%20one.json');
assert.equal(elements.sourcePreviewBack.hidden,false);
elements.sourcePreviewBack.emit('click');
assert.equal(elements.sourceOffersBody.hidden,false);
assert.equal(focused,offer,'Back returns keyboard focus to the chosen offer');
dialog.emit('click',{target:offer});
assert.equal(dialog.open,true,'Clicks inside the panel must not dismiss it');
close.emit('click');
assert.equal(dialog.open,false);
assert.equal(focused,trigger,'Closing returns focus to the estimate row');

trigger.emit('click');
dialog.emit('click');
assert.equal(dialog.open,false,'Backdrop dismisses the panel');
const minimal=new Element({sourcePreview:'',sourceTitle:'Другой источник',sourceReason:'Страница не открылась: HTTPError 403; Playwright: Error: traceback'});
document.emit('click',{target:minimal});
assert.equal(dialog.open,true);
assert.equal(elements.sourcePreviewAudit.hidden,true,'Old audit links never leak into another offer');
assert.equal(elements.sourcePreviewRegionSection.hidden,true);
assert.equal(elements.sourcePreviewCompare.textContent,'');
assert.equal(elements.sourcePreviewBack.hidden,true);
assert.equal(elements.sourcePreviewReason.textContent,'Страница источника недоступна. Цена требует проверки.');
dialog.close();
assert.equal(focused,minimal);
console.log('OK: source list, evidence, navigation and focus');
