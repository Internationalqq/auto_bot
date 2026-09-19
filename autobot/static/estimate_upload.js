/* One browser operation survives a lost upload response and a page reload. */
(function () {
  'use strict';
  const form = document.getElementById('estimateUploadForm');
  if (!form) return;
  const byId = id => document.getElementById(id);
  const fileInput = byId('estimateUploadFile');
  const titleInput = byId('estimateUploadTitle');
  const submit = byId('estimateUploadSubmit');
  const status = byId('uploadStatus');
  const panel = byId('estimateUploadProgress');
  const original = byId('estimateUploadOriginal');
  const retryStatus = byId('estimateUploadRetryStatus');
  const newUpload = byId('estimateUploadNew');
  const storageKey = 'autobot:estimate-upload-operation-v1';
  const legacyKey = 'autobot:estimate-upload-job';
  let operation = null;
  let busy = false;
  let generation = 0;
  let timer = null;

  function persist() {
    try {
      if (operation) window.sessionStorage.setItem(storageKey, JSON.stringify(operation));
      else window.sessionStorage.removeItem(storageKey);
      window.sessionStorage.removeItem(legacyKey);
    } catch (_) { /* The in-memory key still protects retries in this page. */ }
  }
  function restore() {
    try {
      const value = JSON.parse(window.sessionStorage.getItem(storageKey) || 'null');
      if (value && /^[a-f0-9]{16,40}$/.test(value.job_id || '')
          && (!value.operation_id || /^[a-f0-9]{32}$/.test(value.operation_id))) return value;
      const legacy = window.sessionStorage.getItem(legacyKey) || '';
      if (/^[a-fA-F0-9]{16,40}$/.test(legacy)) return {job_id: legacy, confirmed: true};
    } catch (_) {}
    return null;
  }
  function setBusy(value) {
    busy = value;
    submit.disabled = value;
    fileInput.disabled = value;
    titleInput.disabled = value;
    submit.textContent = value ? 'Смета обрабатывается…' : operation?.terminal && fileInput.files?.length ? 'Загрузить ещё раз' : 'Разобрать смету';
  }
  function chosenFile() {
    const file = fileInput.files?.[0];
    const name = byId('estimateUploadFileName');
    name.textContent = file ? file.name : operation?.file_name ? 'Последний файл: ' + operation.file_name : '';
    name.hidden = !name.textContent;
    if (!busy) setBusy(false);
  }
  function render(data) {
    panel.hidden = false;
    const disclosure = panel.closest?.("details.upload-card");
    if (disclosure) disclosure.open = true;
    const value = Math.max(0, Math.min(data.result_ok ? 100 : 99, Number(data.progress) || 0));
    byId('estimateUploadFill').style.width = value + '%';
    byId('estimateUploadPct').textContent = (data.progress_estimated ? '≈ ' : '') + value + '%';
    panel.classList.toggle('is-running', !!data.running);
    byId('estimateUploadStage').textContent = data.stage || 'Проверяю загрузку';
    const elapsed = Math.max(0, Number(data.elapsed_seconds) || 0);
    const elapsedText = elapsed >= 60 ? Math.floor(elapsed / 60) + ' мин ' + elapsed % 60 + ' с' : elapsed + ' с';
    byId('estimateUploadDetail').textContent = [data.detail, data.running && elapsed ? 'Прошло ' + elapsedText : ''].filter(Boolean).join(' · ');
    const error = byId('estimateUploadError');
    error.hidden = !data.error;
    const rawError = String(data.error || '');
    error.textContent = rawError.includes('(BadZipFile)')
      ? 'Excel-файл повреждён или имеет неверный формат. Откройте его в редакторе, сохраните новую копию и загрузите её.' : rawError;
    const logs = Array.isArray(data.log_tail) ? data.log_tail : [];
    byId('estimateUploadLogDetails').hidden = !logs.length;
    byId('estimateUploadLogs').textContent = logs.join('\n');
    const expected = '/estimates/uploads/' + encodeURIComponent(operation?.job_id || '') + '/original';
    original.hidden = data.original_url !== expected;
    if (!original.hidden) original.href = expected;
  }
  function later(token, failures) {
    timer = setTimeout(() => poll(token, failures), Math.min(5000, 1500 + failures * 500));
  }
  async function poll(token, failures = 0) {
    if (!operation || token !== generation) return;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/api/estimates/upload-status/' + encodeURIComponent(operation.job_id),
        {cache: 'no-store', signal: controller.signal});
      const data = await response.json();
      if (token !== generation) return;
      if (!response.ok || !data.ok) {
        if (data.retry_upload || (response.status === 404 && !operation.confirmed && failures >= 2)) {
          setBusy(false);
          panel.classList.remove('is-running');
          status.textContent = data.retry_upload ? data.message : 'Сервер ещё не подтвердил приём. Прикрепите тот же файл и повторите отправку.';
          retryStatus.hidden = false;
          newUpload.hidden = false;
          return;
        }
        throw new Error(data.message || 'Не удалось проверить загрузку.');
      }
      operation.confirmed = true;
      operation.terminal = !data.running;
      persist();
      retryStatus.hidden = true;
      newUpload.hidden = !!data.running;
      render(data);
      setBusy(!!data.running);
      if (data.running) {
        status.textContent = 'Файл принят. Разбираю позиции — страницу можно обновить.';
        later(token, 0);
      } else if (data.result_ok && data.estimate_id) {
        status.textContent = 'Смета готова. Открываю позиции…';
        operation = null;
        persist();
        timer = setTimeout(() => { if (token === generation) window.location.href = '/estimates/' + encodeURIComponent(data.estimate_id); }, 450);
      } else {
        status.textContent = 'Разбор не завершён. Исходник можно скачать ниже или загрузить исправленный файл.';
      }
    } catch (error) {
      if (token !== generation) return;
      retryStatus.hidden = false;
      newUpload.hidden = false;
      status.textContent = operation?.confirmed
        ? 'Нет связи со статусом. Принятое задание сохранено; проверяем снова…'
        : 'Проверяю, успел ли сервер принять файл. Повтор использует ту же загрузку.';
      if (!operation?.confirmed) setBusy(false);
      if (failures < 7) later(token, failures + 1);
      else status.textContent = 'Статус пока недоступен. Нажмите «Проверить статус» после восстановления связи.';
    } finally {
      clearTimeout(timeout);
    }
  }
  function checkStatus() {
    if (!operation) return;
    clearTimeout(timer);
    const token = ++generation;
    retryStatus.hidden = true;
    newUpload.hidden = true;
    status.textContent = 'Проверяю принятую загрузку…';
    poll(token);
  }
  function newOperation(file, fingerprint) {
    const key = window.crypto.randomUUID().replaceAll('-', '');
    return {operation_id: key, job_id: key, fingerprint, file_name: file.name,
      title: titleInput.value.trim().slice(0,160), confirmed: false, terminal: false};
  }
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (busy) return;
    const file = fileInput.files?.[0];
    if (!file) { status.textContent = 'Прикрепите Excel или PDF со сметой.'; return; }
    const fingerprint = JSON.stringify([file.name, file.size, file.lastModified, titleInput.value.trim().slice(0,160)]);
    if (!operation?.operation_id || operation.terminal || operation.fingerprint !== fingerprint) operation = newOperation(file, fingerprint);
    persist(); // Before the request, including before a response containing job_id exists.
    const body = new FormData(form);
    body.set('operation_id', operation.operation_id);
    clearTimeout(timer);
    const token = ++generation;
    setBusy(true);
    retryStatus.hidden = true;
    newUpload.hidden = true;
    render({progress: 0, stage: 'Отправляю файл', detail: 'После приёма сервер начнёт разбор', running: true});
    status.textContent = 'Передаю смету на сервер…';
    const xhr = new XMLHttpRequest();
    let settled = false;
    xhr.open('POST', '/api/estimates/upload');
    xhr.timeout = 300000;
    xhr.upload.addEventListener('progress', event => {
      if (token !== generation || !event.lengthComputable) return;
      const transferred = Math.round(event.loaded / event.total * 100);
      render({progress: Math.min(24, Math.round(transferred * .24)), running: true,
        stage: 'Отправляю файл', detail: 'Передано ' + transferred + '% файла'});
    });
    function complete() {
      if (settled || token !== generation) return;
      settled = true;
      let data = {};
      try { data = JSON.parse(xhr.responseText || '{}'); } catch (_) {}
      if ((data.ok || data.accepted) && data.job_id === operation.job_id) {
        operation.confirmed = true;
        persist();
        poll(token);
      } else if ([400, 409, 410, 413].includes(xhr.status)) {
        operation.terminal = true;
        persist();
        render({stage: 'Файл не принят', error: data.message || 'Проверьте файл и повторите загрузку.'});
        status.textContent = 'Исправьте файл или начните новую загрузку.';
        setBusy(false);
      } else {
        setBusy(false);
        status.textContent = 'Ответ о приёме не получен. Проверяю сохранённую загрузку…';
        poll(token);
      }
    }
    xhr.onload = complete;
    xhr.onerror = complete;
    xhr.ontimeout = complete;
    xhr.send(body);
  });
  fileInput.addEventListener('change', chosenFile);
  retryStatus.addEventListener('click', checkStatus);
  newUpload.addEventListener('click', () => {
    const continuing = operation?.confirmed && !operation?.terminal;
    ++generation;
    clearTimeout(timer);
    operation = null;
    persist();
    setBusy(false);
    panel.hidden = true;
    fileInput.value = '';
    titleInput.value = '';
    chosenFile();
    status.textContent = continuing ? 'Принятое задание продолжит обработку. Можно загрузить другую смету.' : 'Прикрепите следующую смету.';
  });
  operation = restore();
  if (operation?.title && !titleInput.value) titleInput.value = operation.title;
  chosenFile();
  if (operation) {
    panel.hidden = false;
    setBusy(!!operation.confirmed && !operation.terminal);
    checkStatus();
  }
})();
