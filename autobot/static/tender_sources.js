(() => {
  'use strict';
  const dialog = document.getElementById('sourcePreviewDrawer');
  if (!dialog) return;
  const get = id => document.getElementById(id);
  const title = get('sourcePreviewTitle'), back = get('sourcePreviewBack');
  const offers = get('sourceOffersBody'), list = get('sourceOffersList');
  const detail = get('sourcePreviewBody'), footer = get('sourcePreviewFooter');
  let trigger = null, selected = null, listTitle = '';
  function open() {
    if (!dialog.open) dialog.showModal();
    title.focus();
  }
  function showList() {
    title.textContent = listTitle;
    offers.hidden = false; detail.hidden = true; footer.hidden = true; back.hidden = true;
    if (selected) selected.focus();
  }
  document.querySelectorAll('[data-position-offers]').forEach(button => {
    button.addEventListener('click', () => {
      trigger = button; selected = null;
      const sources = button.parentElement.querySelector('.source-list');
      list.replaceChildren(...Array.from(sources.children, source => source.cloneNode(true)));
      listTitle = `Предложения · позиция ${button.dataset.positionNumber}`;
      get('sourceOffersPosition').textContent = button.dataset.positionTitle;
      showList(); open();
    });
  });
  // Delegation also covers the list copied into the dialog. All supplier text
  // stays plain text, and the original source evidence/links remain available.
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-source-preview]');
    if (!button) return;
    const inList = list.contains(button);
    if (inList) selected = button;
    else { trigger = button; selected = null; listTitle = ''; }
    const data = button.dataset;
    title.textContent = data.sourceTitle || 'Источник цены';
    get('sourcePreviewPrice').textContent = data.sourcePrice || '—';
    get('sourcePreviewBasis').textContent = data.sourcePriceBasis || 'Цена источника';
    get('sourcePreviewCompare').textContent = data.sourceComparisonPrice && data.sourceComparisonPrice !== data.sourcePrice ? `В единице сметы: ${data.sourceComparisonPrice}` : '';
    get('sourcePreviewRatio').textContent = data.sourceRatio && data.sourceRatio !== '—' ? `${data.sourceRatio} к смете` : 'Сравнение недоступно';
    get('sourcePreviewReason').textContent = (data.sourceReason || '').startsWith('Страница не открылась') ? 'Страница источника недоступна. Цена требует проверки.' : data.sourceReason || '';
    get('sourcePreviewEvidence').textContent = data.sourceEvidence || 'Нет сохранённого фрагмента страницы.';
    const observed = data.sourceObserved || '';
    const instant = observed ? new Date(/^\d+(?:\.\d+)?$/.test(observed) ? Number(observed) * 1000 : observed) : null;
    const checkedAt = instant && Number.isFinite(instant.getTime()) ? `Проверено: ${instant.toLocaleString('ru-RU')}` : 'Дата проверки неизвестна';
    get('sourcePreviewMeta').textContent = [data.sourceLocation, data.sourcePublished, checkedAt].filter(Boolean).join(' · ');
    get('sourcePreviewRegionSection').hidden = !(data.sourceRegionproof || data.sourceDelivery || data.sourceSupplier);
    get('sourcePreviewRegionEvidence').textContent = [data.sourceSupplier, data.sourceRegionproof, data.sourceDelivery, data.sourceRegionUrl].filter(Boolean).join(' · ');
    get('sourcePreviewOpen').href = data.sourceUrl || '#';
    const audit = get('sourcePreviewAudit');
    audit.hidden = !data.sourceAudit;
    audit.href = data.sourceAudit ? `/tenders/market-audit?record=${encodeURIComponent(data.sourceAudit)}` : '#';
    offers.hidden = true; detail.hidden = false; footer.hidden = false; back.hidden = !inList;
    detail.scrollTop = 0;
    open();
  });
  back.addEventListener('click', showList);
  dialog.querySelector('[data-source-preview-close]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener('close', () => { trigger?.focus({preventScroll:true}); });
})();
