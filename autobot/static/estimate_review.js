(() => {
  'use strict';
  const config = JSON.parse(document.getElementById('correctionConfig').textContent);
  const form = document.getElementById('correctionForm');
  const save = document.getElementById('correctionSave');
  const refresh = document.getElementById('correctionRefresh');
  const status = document.getElementById('correctionStatus');
  const more = document.getElementById('correctionMore');
  const history = document.getElementById('correctionHistory');
  const isTender = config.kind === 'tender';
  const workspace = (isTender ? '/tenders/' : '/estimates/') + encodeURIComponent(config.estimateId);
  const endpoint = (isTender ? '/api/tender/' : '/api/estimates/') + encodeURIComponent(config.estimateId) + '/corrections';
  const storageKey = 'autobot:correction:' + (isTender ? 'tender:' : '') + config.estimateId + ':' + config.positionId;
  let version = config.version, pending = null, busy = false, conflict = false;
  const text = value => value == null || value === '' ? '—' : String(value);
  const date = value => { const parsed=new Date(value); return Number.isNaN(parsed.getTime())?value:parsed.toLocaleString('ru-RU'); };
  document.querySelectorAll('.review-history time').forEach(node=>{node.textContent=date(node.dateTime);});
  function message(value, error=false) { status.textContent=value; status.classList.toggle('is-error',error); }
  function remember(value) {
    pending=value;
    try { if(value) sessionStorage.setItem(storageKey,JSON.stringify(value)); else sessionStorage.removeItem(storageKey); } catch (_) {}
  }
  function buttons() { save.disabled=busy || conflict; refresh.disabled=busy; more.disabled=busy; }
  async function request(url, payload) {
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),15000);
    try {
      const response=await fetch(url,{method:payload?'POST':'GET',cache:'no-store',credentials:'same-origin',
        headers:{Accept:'application/json',...(payload?{'Content-Type':'application/json'}:{})},
        ...(payload?{body:JSON.stringify(payload)}:{}),signal:controller.signal});
      const data=await response.json().catch(()=>({message:'Сервер вернул неполный ответ.'}));
      if(!response.ok || !data.ok) { const error=new Error(data.message || 'Не удалось прочитать ответ.'); error.status=response.status; throw error; }
      return data;
    } finally { clearTimeout(timeout); }
  }
  function fill(values, reason='') {
    config.fields.forEach(name=>{document.getElementById('correction-'+name).value=values[name] == null?'':String(values[name]);});
    document.getElementById('correction-reason').value=reason;
  }
  function renderHistory(items, append=false) {
    if(!append) history.replaceChildren();
    for(const item of items) {
      const article=document.createElement('article');
      const title=document.createElement('strong'); title.textContent='Редакция '+item.revision+' · '+item.actor.name;
      const time=document.createElement('time'); time.dateTime=item.created_at;time.textContent=date(item.created_at);
      const position=document.createElement('p'), link=document.createElement('a');
      link.textContent=item.position_name || 'Проверить позицию';
      link.href=workspace+'/review?position_id='+encodeURIComponent(item.position_id);
      position.appendChild(link);
      const reason=document.createElement('p'); reason.textContent=item.reason;
      const list=document.createElement('ul');
      for(const [name,change] of Object.entries(item.changes)) {
        const li=document.createElement('li');li.textContent=(config.labels[name] || name)+': '+text(change.before)+' → '+text(change.after);list.appendChild(li);
      }
      article.append(title,time,position,reason,list); history.appendChild(article);
    }
  }
  async function loadCurrent() {
    const data=await request(endpoint+'?position_id='+encodeURIComponent(config.positionId));
    version=data.version; fill(data.row); conflict=false;
    renderHistory(data.history); more.dataset.before=data.next_before || '';more.hidden=!data.next_before;
    const label=document.querySelector('.review-version'); if(label) label.textContent='Редакция '+data.revision;
    refresh.hidden=true;
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault(); if(busy || conflict) return;
    const changes=Object.fromEntries(config.fields.map(name=>[name,document.getElementById('correction-'+name).value.trim()]));
    const reason=document.getElementById('correction-reason').value.trim();
    if(!reason) { message('Укажите причину исправления.',true); return; }
    const payload={position_id:config.positionId,changes,expected_version:version,reason};
    const fingerprint=JSON.stringify(payload);
    if(!pending || pending.fingerprint!==fingerprint) remember({fingerprint,payload:{...payload,operation_id:crypto.randomUUID().replaceAll('-','')}});
    busy=true;buttons();message('Сохраняю исправление…');
    try {
      const data=await request(endpoint,pending.payload);
      remember(null);version=data.version;
      try { await loadCurrent(); message('Сохранена редакция '+data.revision+'. Исходный файл не изменён.'); }
      catch (_) { conflict=true;refresh.hidden=false;message('Редакция '+data.revision+' сохранена. Не удалось обновить строку — загрузите текущую редакцию.',true); }
    } catch(error) {
      if([400,401,403,404,409,413].includes(error.status)) remember(null);
      if([401,403,404,409].includes(error.status)) { conflict=true;refresh.hidden=false; }
      message(error.status && error.status<500 ? error.message : 'Подтверждение не получено. Повторное нажатие проверит то же сохранение; введённые значения остались в форме.',true);
    } finally { busy=false;buttons(); }
  });
  refresh.addEventListener('click',async()=>{
    if(busy) return;busy=true;buttons();
    try { await loadCurrent();remember(null);message('Текущая редакция загружена. Сравните значения перед исправлением.'); }
    catch(error) { message(error.message,true); }
    finally { busy=false;buttons(); }
  });
  more.addEventListener('click',async()=>{
    if(busy || !more.dataset.before) return;busy=true;buttons();
    try { const data=await request(endpoint+'?before='+encodeURIComponent(more.dataset.before));renderHistory(data.history,true);more.dataset.before=data.next_before || '';more.hidden=!data.next_before; }
    catch(error) { message(error.message,true); }
    finally { busy=false;buttons(); }
  });
  async function recover() {
    try { const saved=JSON.parse(sessionStorage.getItem(storageKey) || 'null');
      if(saved && /^[a-f0-9]{32}$/.test(saved.payload?.operation_id || '') && saved.payload.position_id===config.positionId
          && typeof saved.fingerprint==='string' && saved.payload.changes && typeof saved.payload.reason==='string') pending=saved;
    } catch (_) {}
    if(!pending) return;
    busy=true;buttons();message('Проверяю последнее сохранение…');
    try {
      const data=await request(endpoint+'?operation_id='+encodeURIComponent(pending.payload.operation_id));
      remember(null);await loadCurrent();message('Предыдущее сохранение найдено: редакция '+data.revision+'.');
    } catch(error) {
      if(pending) {
        version=pending.payload.expected_version;fill(pending.payload.changes,pending.payload.reason);
        conflict=error.status!==404;refresh.hidden=!conflict;
        message(error.status===404?'Предыдущее сохранение ещё не найдено. Повторите отправку — ключ сохранён.':'Не удалось проверить сохранение. Введённые значения восстановлены; повторите загрузку текущей строки.',true);
      } else { conflict=true;refresh.hidden=false;message('Сохранение найдено, но текущая строка недоступна. Повторите загрузку.',true); }
    } finally { busy=false;buttons(); }
  }
  recover();
})();
