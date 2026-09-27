(() => {
  'use strict';
  const current = location.pathname.match(/^\/(?:autobot\/)?tenders\/(\d{8,25})$/)?.[1];
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
  const waiting = [], states = new Map(); let running = false;
  function enqueue(el, reset = false) {
    const state = states.get(el);
    if (!state || state.queued || state.busy) return;
    clearTimeout(state.timer);
    if (reset) state.failures = 0;
    state.queued = true;
    waiting.push(el);
    drain();
  }
  async function drain() {
    if (running) return; running = true;
    try {
      while (waiting.length) {
        const el = waiting.shift();
        const state = states.get(el), bar = el.querySelector('progress'), retry = el.querySelector('[data-progress-retry]');
        state.queued = false; state.busy = true;
        el.setAttribute('aria-busy','true');
        if (retry) { retry.disabled = true; retry.textContent = 'Загружаем…'; }
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 10000);
        try {
          const response = await fetch(`/api/tenders/${el.dataset.progressTender}/work-progress`,{cache:'no-store',signal:controller.signal});
          const data = await response.json();
          if (!response.ok || !data.ok || ![data.total,data.processed,data.verified].every(n => Number.isInteger(n) && n >= 0) || data.processed > data.total || data.verified > data.processed) throw new Error();
          const total = data.total, processed = data.processed;
          el.querySelector('strong').textContent = total ? `Проанализировано ${processed} из ${total} позиций` : 'Смета ещё не разобрана';
          bar.max = Math.max(1,total); bar.value = processed;
          bar.hidden = !total;
          bar.setAttribute('aria-label', 'Прогресс анализа сметы');
          el.querySelector('[data-progress-prices]').textContent = `С подтверждённой ценой: ${data.verified}`;
          state.failures = 0; state.loaded = true;
          if (retry) retry.hidden = true;
        } catch (_) {
          el.querySelector('strong').textContent = 'Прогресс временно недоступен';
          bar.hidden = true;
          el.querySelector('[data-progress-prices]').textContent = '';
          state.failures += 1;
          if (retry) retry.hidden = false;
          // A short connection failure must not leave the card broken until F5.
          if (state.failures < 3) state.timer = setTimeout(() => {
            if (!document.hidden) enqueue(el);
          }, state.failures * 10000);
        } finally {
          clearTimeout(timeout);
          state.busy = false;
          el.setAttribute('aria-busy','false');
          if (retry) { retry.disabled = false; retry.textContent = 'Повторить'; }
        }
      }
    } finally { running = false; }
  }
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) { observer.unobserve(entry.target); enqueue(entry.target); }
  }, {rootMargin:'100px'});
  document.querySelectorAll('[data-progress-tender]').forEach(el => {
    states.set(el,{failures:0,loaded:false,queued:false,busy:false,timer:null});
    el.querySelector('[data-progress-retry]')?.addEventListener('click',() => enqueue(el,true));
    observer.observe(el);
  });
  document.addEventListener('visibilitychange',() => {
    if (!document.hidden) states.forEach((state,el) => {
      if (state.failures > 0 && state.failures < 3) enqueue(el);
    });
  });
})();
