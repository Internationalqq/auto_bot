(() => {
  'use strict';
  const api='/api/tenders/suppliers';
  const feedback=document.getElementById('catalogFeedback');
  let timer=null;
  function message(text,error=false){feedback.textContent=text;feedback.dataset.error=String(error);feedback.hidden=false;}
  async function request(path,method='GET'){
    const response=await fetch(api+path,{method,credentials:'same-origin',headers:{Accept:'application/json'}});
    const data=await response.json().catch(()=>({message:'Не удалось прочитать ответ сервера'}));
    if(!response.ok||data.ok===false)throw new Error(data.message||'Операция не выполнена. Повторите попытку.');
    return data;
  }
  async function refresh(){
    if(!document.querySelector('[data-source-id]'))return;
    try{
      const data=await request('/sources');
      const states={queued:'В очереди',running:'Загружаем',paused:'Приостановлен на лимите',completed:'Обход завершён',partial:'Обход завершён с пропусками',canceled:'Остановлен'};
      for(const row of document.querySelectorAll('[data-source-id]')){
        const source=data.sources.find(s=>s.id===row.dataset.sourceId);
        const job=data.jobs.find(j=>j.source_id===row.dataset.sourceId);
        if(source){
          row.querySelector('[data-item-count]').textContent=source.item_count;
          row.querySelector('[data-last-import]').textContent=source.last_import_at
            ?new Intl.DateTimeFormat('ru-RU',{timeZone:'UTC'}).format(new Date(source.last_import_at*1000)):'Ещё не загружен';
        }
        if(!job)continue;
        row.querySelector('[data-job-status]').textContent=`${states[job.status]||job.status}: ${job.processed} из ${job.discovered} страниц${job.errors?`; ошибок: ${job.errors}`:''}${job.error?`. ${job.error}`:''}`;
        const button=row.querySelector('[data-import-source]');
        if(!button)continue;
        const active=['queued','running'].includes(job.status);
        button.dataset.cancelJob=active?job.id:'';
        button.textContent=active?(job.cancel_requested?'Останавливаем…':'Остановить'):job.status==='paused'?'Продолжить':'Обновить';
        button.disabled=Boolean(active&&job.cancel_requested);
      }
      clearTimeout(timer);
      if(!document.hidden&&data.jobs.some(j=>['queued','running'].includes(j.status)))timer=setTimeout(refresh,5000);
    }catch(error){message(error.message,true);}
  }
  document.addEventListener('click',async event=>{
    const paging=event.target.closest('[data-page-offset]');
    if(paging){const url=new URL(location.href);url.searchParams.set('offset',paging.dataset.pageOffset);location.assign(url);return;}
    const button=event.target.closest('[data-import-source],[data-import-all]');
    if(!button||button.disabled)return;
    button.disabled=true;
    try{
      if(button.dataset.cancelJob){await request(`/jobs/${encodeURIComponent(button.dataset.cancelJob)}/cancel`,'POST');message('Останавливаем импорт. Уже загруженные предложения останутся в базе.');}
      else{await request(button.hasAttribute('data-import-all')?'/import':`/sources/${encodeURIComponent(button.dataset.importSource)}/import`,'POST');message('Импорт поставлен в очередь. Страницу можно закрыть.');}
      await refresh();
    }catch(error){message(error.message,true);}
    finally{if(button.textContent!=='Останавливаем…')button.disabled=false;}
  });
  for(const detail of document.querySelectorAll('[data-item-id]'))detail.addEventListener('toggle',async()=>{
    if(!detail.open||detail.dataset.loaded==='true')return;
    const box=detail.querySelector('[data-history]');box.textContent='Загружаем историю…';
    try{
      const data=await request(`/items/${encodeURIComponent(detail.dataset.itemId)}/history`);
      const list=document.createElement('ul');
      for(const item of data.history){
        const li=document.createElement('li');
        const price=item.record.price;
        const display=price==null?'Цена не указана':new Intl.NumberFormat('ru-RU',{style:'currency',currency:'RUB'}).format(Number(price));
        const prefix=item.record.details?.price_prefix?'от ':'';
        const unit=item.record.unit?' / '+item.record.unit:'';
        li.textContent=new Date(item.observed_at*1000).toLocaleDateString('ru-RU')+' — '+prefix+display+(price==null?'':unit);
        list.append(li);
      }
      box.replaceChildren(list);detail.dataset.loaded='true';
    }catch(error){box.textContent=error.message;}
  });
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();else clearTimeout(timer);});
  refresh();
})();
