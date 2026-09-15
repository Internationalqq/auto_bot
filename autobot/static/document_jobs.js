(() => {
  'use strict';
  const tenderId = document.currentScript?.dataset.documentJobs;
  const buttons = [...document.querySelectorAll('[data-refresh-documents], [data-rebuild-report]')];
  const status = document.getElementById('documentRefreshStatus');
  const panel = document.getElementById('documentJobPanel');
  const details = document.getElementById('documentJobDetails');
  const log = document.getElementById('documentJobLog');
  if (!tenderId || !status || !buttons.length) return;
  const statusUrl = '/api/parse-status?tender_id=' + encodeURIComponent(tenderId);
  let runId = null, pending = null, busy = false, failures = 0;
  const operationId = () => typeof crypto.randomUUID === 'function' ? crypto.randomUUID()
    : [...crypto.getRandomValues(new Uint8Array(16))].map(value => value.toString(16).padStart(2,'0')).join('');
  const enable = enabled => buttons.forEach(button => {button.disabled = !enabled;});
  const show = text => {if (panel) panel.hidden = false; status.hidden = false; status.textContent = text;};
  const showLog = state => {
    if (!log || !details) return;
    const lines = Array.isArray(state.log_tail) ? state.log_tail.slice(-80) : [];
    log.textContent = lines.join('\n');
    details.hidden = !lines.length;
  };
  const failureText = state => state.job_status === 'interrupted'
    ? 'Исполнитель остановился до подтверждения результата. Проверьте сохранённые файлы и повторите нужное действие.'
    : 'Загрузка или разбор не завершились. ' + ([...(state.document_status?.errors || []), ...(state.document_parse?.errors || [])].join(' · ') || 'Подробности доступны в журнале обработки. Повторите попытку позже.');

  async function poll(expectedRun) {
    if (runId !== expectedRun) return;
    try {
      const response = await fetch(statusUrl + '&run_id=' + encodeURIComponent(expectedRun), {cache:'no-store'});
      if (runId !== expectedRun) return;
      if (response.status === 404) {
        runId = null; enable(true);
        show('Сохранённый запуск не найден. Обновите карточку и проверьте документы.');
        return;
      }
      if (!response.ok) throw new Error('status');
      const state = await response.json();
      if (runId !== expectedRun) return;
      if (state.run_id !== expectedRun) {
        runId = null; enable(true);
        show('Состояние запуска изменилось. Обновите карточку и проверьте документы.');
        return;
      }
      failures = 0;
      showLog(state);
      if (!state.running) {
        runId = null; enable(true);
        if (state.exit_code === 0) {
          show('Документы обработаны. Обновляю карточку…');
          window.location.reload();
        } else show(failureText(state));
        return;
      }
      show(state.job_status === 'queued' ? 'Задание сохранено. Ожидает начала обработки.' : 'Обрабатываю документы и разбираю смету. Страницу можно закрыть.');
    } catch {
      if (runId !== expectedRun) return;
      failures += 1;
      show('Не удалось получить состояние обработки. Проверяю повторно…');
    }
    if (runId === expectedRun) window.setTimeout(() => poll(expectedRun), Math.min(30000, 2500 * 2 ** Math.min(failures, 4)));
  }

  buttons.forEach(button => button.addEventListener('click', async () => {
    if (busy || runId) return;
    busy = true; enable(false);
    const rebuild = button.hasAttribute('data-rebuild-report');
    const kind = rebuild ? 'rebuild' : 'download';
    showLog({});
    show(rebuild ? 'Запускаю разбор сохранённых документов…' : 'Запускаю загрузку документов…');
    status.focus();
    try {
      if (!pending || pending.kind !== kind) pending = {kind, id:operationId()};
      const response = await fetch(rebuild ? '/api/reports/rebuild' : '/api/tenders/' + encodeURIComponent(tenderId) + '/refresh-documents', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({...(rebuild ? {tender_id:tenderId} : {}), operation_id:pending.id})
      });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        if (response.status < 500) pending = null;
        throw Object.assign(new Error(data.message || 'Не удалось запустить обработку.'), {server:true});
      }
      if (!/^[0-9a-f]{32}$/.test(data.run_id || '')) throw new Error('unconfirmed');
      runId = data.run_id; pending = null; failures = 0;
      poll(runId);
    } catch (error) {
      show(error.server ? error.message : 'Не удалось подтвердить запуск. Повторите действие — проверим тот же запуск.');
      enable(true);
    } finally {busy = false;}
  }));

  fetch(statusUrl, {cache:'no-store'}).then(response => response.ok ? response.json() : null).then(state => {
    if (!state || busy || runId || state.tender_id !== tenderId || !state.run_id) return;
    if (state.running) {
      runId = state.run_id; enable(false); poll(runId);
    } else if (state.job_status === 'interrupted' || state.job_status === 'failed') {
      showLog(state);
      show(failureText(state));
    }
  }).catch(() => {});
})();
