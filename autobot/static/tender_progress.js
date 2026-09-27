(() => {
  'use strict';
  const current = location.pathname.match(/^\/tenders\/(\d{8,25})$/)?.[1];
  function record(tid, action) {
    fetch(`/api/tenders/${tid}/activity`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action}),keepalive:true}).catch(() => {});
  }
  if (current) record(current, 'open');
  document.addEventListener('click', event => {
    const target = event.target.closest('[data-tender-action], [data-refresh-documents], [data-rebuild-report], #runMarketBtn, #runMarketSampleBtn');
    if (!target) return;
    const tid = target.dataset.tenderId || current;
    const action = /download|rebuild/.test(target.dataset.tenderAction || '') || target.hasAttribute('data-refresh-documents') || target.hasAttribute('data-rebuild-report') ? 'documents' : 'analysis';
    if (tid && /^\d{8,25}$/.test(tid)) record(tid, action);
  });
  function ages() {
    document.querySelectorAll('[data-activity-time]').forEach(el => {
      const stamp = Number(el.dataset.activityTime);
      if (!Number.isFinite(stamp) || stamp <= 0) return;
      const minutes = Math.max(0, Math.floor((Date.now()/1000-stamp)/60));
      el.textContent = minutes < 1 ? 'только что' : minutes < 60 ? `${minutes} мин назад` : minutes < 1440 ? `${Math.floor(minutes/60)} ч ${minutes%60} мин назад` : `${Math.floor(minutes/1440)} дн назад`;
      el.title = new Date(stamp*1000).toLocaleString('ru-RU');
      el.dateTime = new Date(stamp*1000).toISOString();
    });
  }
  ages(); setInterval(ages, 60000);
  const waiting = []; let running = false;
  async function drain() {
    if (running) return; running = true;
    try {
      while (waiting.length) {
        const el = waiting.shift();
        try {
          const response = await fetch(`/api/tenders/${el.dataset.progressTender}/work-progress`,{cache:'no-store'});
          const data = await response.json();
          if (!response.ok || !data.ok) throw new Error();
          const total = Math.max(0, Number(data.total)), processed = Math.min(total, Math.max(0, Number(data.processed)));
          el.querySelector('strong').textContent = total ? `Проанализировано ${processed} из ${total} позиций` : 'Смета ещё не разобрана';
          const bar = el.querySelector('progress');
          bar.max = Math.max(1,total); bar.value = processed;
          bar.setAttribute('aria-label', 'Прогресс анализа сметы');
          el.querySelector('[data-progress-prices]').textContent = `С подтверждённой ценой: ${data.verified}`;
        } catch (_) {
          el.querySelector('strong').textContent = 'Прогресс временно недоступен';
          el.querySelector('progress').hidden = true;
        }
      }
    } finally { running = false; }
  }
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) { observer.unobserve(entry.target); waiting.push(entry.target); }
    drain();
  }, {rootMargin:'100px'});
  document.querySelectorAll('[data-progress-tender]').forEach(el => observer.observe(el));
})();
