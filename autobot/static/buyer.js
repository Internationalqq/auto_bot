(() => {
  'use strict';
  const root = document.querySelector('[data-buyer]');
  if (!root) return;
  const url = `/api/tenders/${encodeURIComponent(root.dataset.buyer)}/buyer/jobs`;
  const start = root.querySelector('[data-buyer-start]');
  const cancel = root.querySelector('[data-buyer-cancel]');
  const status = root.querySelector('[data-buyer-status]');
  const list = root.querySelector('[data-buyer-list]');
  const refresh = root.querySelector('[data-buyer-refresh]');
  let busy = false, last = '', active = false;
  const recipients = new Map();
  const messages = new Map();
  const campaignLabels = {queued:'Проверка ожидает запуска', checking:'Проверяем сайты поставщиков', completed:'Проверка сайтов завершена', failed:'Проверка остановлена'};
  const sendLabels = {queued: 'Ожидает Mac', sending: 'Агент отправляет', sent: 'Агент подтвердил отправку', blocked: 'Не отправлено', uncertain: 'Нужна проверка отправки'};
  const labels = {queued: 'В очереди', leased: 'Готовится', completed: 'Черновики готовы', failed: 'Не удалось подготовить', canceled: 'Отменено'};
  const errors = {submission_uncertain: 'Связь прервалась при запуске. Нужна проверка агента, повтор заблокирован.', invalid_result: 'Агент вернул неполный ответ. Нужна проверка задания.'};
  function node(tag, text, className) {
    const el = document.createElement(tag);
    if (text != null) el.textContent = text;
    if (className) el.className = className;
    return el;
  }
  async function api(body) {
    const response = await fetch(url, body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || 'Не удалось получить задания. Нажмите «Обновить».');
    return data;
  }
  function render(jobs, outbox = [], campaigns = []) {
    active = jobs.some(j => j.status === 'queued' || j.status === 'leased');
    cancel.hidden = !active;
    const queued = jobs.filter(j => j.status === 'queued').length;
    const running = jobs.filter(j => j.status === 'leased').length;
    const ready = jobs.filter(j => j.status === 'completed').length;
    const failed = jobs.filter(j => j.status === 'failed').length;
    status.textContent = jobs.length ? `Групп в очереди: ${queued} · В работе: ${running} · Готово: ${ready}${failed ? ` · Ошибки: ${failed}` : ''}${queued && !running ? '. Ожидаем закупщика на Mac.' : ''}` : 'Заданий пока нет. Выберите строки в смете или подготовьте запросы по всем позициям без подтверждённой цены.';
    const signature = JSON.stringify([jobs.map(j => [j.id, j.status, j.error, j.result, j.can_send]), outbox, campaigns]);
    if (signature === last) return;
    last = signature;
    const open = new Set(Array.from(list.querySelectorAll('details[open]')).map(el => el.dataset.key));
    const focusedKey = list.contains(document.activeElement) ? document.activeElement.closest('details')?.dataset.key : null;
    const focusedInput = document.activeElement?.dataset?.recipientKey;
    const selection = focusedInput ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
    list.replaceChildren();
    jobs.forEach(job => {
      const group = node('details', null, 'buyer-group');
      group.dataset.key = job.id;
      group.open = open.has(job.id);
      const summary = node('summary');
      summary.append(node('strong', job.position_name), node('span', `${labels[job.status] || 'Неизвестное состояние'} · Позиций: ${job.positions.length}`));
      group.append(summary);
      if (job.result) {
        job.result.drafts.forEach((draft, index) => {
          const key = `${job.id}-${index}`;
          const message = messages.get(key) || {subject:draft.subject, body:draft.body};
          const article = node('article', null, 'buyer-draft');
          const heading = node('h3', message.subject), preview = node('p', message.body, 'buyer-body');
          article.append(heading, preview);
          if (job.can_send) {
            const edit = node('details', null, 'buyer-edit'); edit.dataset.key = `edit-${key}`;
            edit.open = open.has(edit.dataset.key);
            edit.append(node('summary', 'Изменить текст перед отправкой'));
            ['subject','body'].forEach(field => {
              const label = node('label', field === 'subject' ? 'Тема письма' : 'Текст письма');
              const input = node(field === 'body' ? 'textarea' : 'input');
              input.id = `buyer-${field}-${key}`; label.htmlFor = input.id;
              input.value = message[field]; input.maxLength = field === 'subject' ? 300 : 20000;
              if (field === 'body') input.rows = 7;
              input.dataset.recipientKey = `${key}-${field}`;
              input.addEventListener('input', () => {
                messages.set(key, {...(messages.get(key) || message), [field]:input.value});
                (field === 'subject' ? heading : preview).textContent = input.value;
              });
              edit.append(label, input);
            });
            article.append(edit);
            const automatic = node('button', 'Найти поставщиков и отправить', 'btn primary');
            automatic.type = 'button';
            const autoRows = job.positions.filter(p => draft.position_keys.includes(p.position_key));
            const supported = /ярослав/i.test(job.region || '') && autoRows.length > 0 && autoRows.every(p => /щебень/i.test(p.name) && ['material','product'].includes(p.type_slug));
            automatic.hidden = !supported;
            const autoFeedback = node('p', 'Автоподбор: щебень · Ярославская область · 3 сайта · Email. Другие направления и мессенджеры пока не подключены.', 'buyer-send-feedback');
            if (!supported) autoFeedback.textContent = 'Автоподбор для этого направления пока не подключён. Можно отправить письмо по указанному контакту.';
            autoFeedback.setAttribute('role','status');
            automatic.addEventListener('click', async () => {
              if (busy) return;
              busy = true; automatic.disabled = true; autoFeedback.textContent = 'Запускаем проверку сайтов и контактов…';
              try {
                const response = await fetch(url.replace(/jobs$/, 'outbox'), {method:'POST', headers:{'Content-Type':'application/json'},
                  body:JSON.stringify({action:'find_and_send',draft_job_id:job.id,draft_index:index,message:messages.get(key) || message})});
                const data = await response.json();
                if (!response.ok || !data.ok) throw new Error(data.message || 'Не удалось запустить проверку');
                autoFeedback.textContent = 'Запрос сохранён. Проверка сайтов и результаты отправки — в журнале ниже.';
                await load();
              } catch(error) { autoFeedback.textContent = error.message; }
              finally { busy = false; automatic.disabled = false; }
            });
            article.append(automatic, autoFeedback);
            const form = node('form', null, 'buyer-send');
            const label = node('label', 'Email поставщика');
            const input = node('input');
            input.type = 'email'; input.required = true; input.maxLength = 254;
            input.id = `buyer-email-${key}`; label.htmlFor = input.id;
            input.dataset.recipientKey = key; input.value = recipients.get(key) || '';
            input.placeholder = 'sales@company.ru';
            input.addEventListener('input', () => recipients.set(key, input.value));
            const button = node('button', 'Отправить по указанной почте', 'btn ghost');
            button.type = 'submit';
            const feedback = node('p', '', 'buyer-send-feedback');
            feedback.setAttribute('role', 'status');
            form.append(label, input, button, feedback);
            form.addEventListener('submit', async event => {
              event.preventDefault();
              if (busy || !form.reportValidity()) return;
              busy = true; button.disabled = true;
              feedback.textContent = 'Передаём на Mac…';
              try {
                const response = await fetch(url.replace(/jobs$/, 'outbox'), {method: 'POST', headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({draft_job_id: job.id, draft_index: index, recipient: input.value.trim(), message:messages.get(key) || message})});
                const data = await response.json();
                if (!response.ok || !data.ok) throw new Error(data.message || 'Не удалось передать письмо. Нажмите «Обновить», чтобы проверить статус.');
                feedback.textContent = 'Статус отправки — ниже. Повторное нажатие не создаёт второе письмо.';
                await load();
              } catch (error) { feedback.textContent = error.message; }
              finally { busy = false; button.disabled = false; }
            });
            article.append(form);
            const ownCampaigns = campaigns.filter(c => c.draft_job_id === job.id && c.draft_index === index);
            ownCampaigns.forEach(c => {
              article.append(node('p', `${campaignLabels[c.status] || c.status} · ${c.region}${c.error ? '. '+c.error : ''}`, 'buyer-send-feedback'));
              c.contacts.filter(contact => !contact.outbox_id).forEach(contact => {
                article.append(node('p', `${contact.company} · Email · Не поставлено в очередь: ${contact.error}`, 'buyer-send-feedback'));
              });
            });
            const sentItems = outbox.filter(item => item.draft_job_id === job.id && item.draft_index === index);
            if (sentItems.length) article.append(node('h3', 'Журнал обращений'));
            sentItems.forEach(item => {
              const contact = ownCampaigns.flatMap(c => c.contacts).find(c => c.outbox_id === item.id);
              const entry = node('details', null, 'buyer-delivery'); entry.dataset.key = `mail-${item.id}`;
              entry.open = open.has(entry.dataset.key);
              const head = node('summary');
              head.append(node('strong', contact?.company || item.recipient), node('span', `Email · ${item.recipient}`, 'buyer-delivery-contact'),
                node('span', sendLabels[item.status] || item.status, `buyer-delivery-state buyer-state-${item.status}`));
              entry.append(head);
              entry.append(node('p', `Создано: ${new Date(item.created_at*1000).toLocaleString('ru-RU')} · Обновлено: ${new Date(item.updated_at*1000).toLocaleString('ru-RU')}`));
              if (contact) {
                const source = node('a', 'Контакт на сайте поставщика'); source.href = contact.source_url;
                source.target = '_blank'; source.rel = 'noopener noreferrer'; entry.append(source);
              }
              entry.append(node('h4', item.subject), node('p', item.body, 'buyer-body'));
              const receipt = node('p', item.receipt?.detail || 'Результат появится после проверки агентом на Mac.', 'buyer-send-feedback');
              entry.append(receipt);
              (item.attempts || []).forEach(attempt => entry.append(node('p', `Предыдущая попытка: ${attempt.receipt.detail} · Повтор ${new Date(attempt.retried_at*1000).toLocaleString('ru-RU')}`)));
              if (item.status === 'blocked') {
                const retry = node('button', `Повторить для ${item.recipient}`, 'btn ghost');
                retry.type = 'button'; retry.title = 'После устранения причины. Предыдущая попытка не отправляла письмо.';
                retry.addEventListener('click', async () => {
                  if (busy) return;
                  busy = true; retry.disabled = true;
                  try {
                    const response = await fetch(url.replace(/jobs$/, 'outbox'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'retry_blocked',id:item.id})});
                    const data = await response.json();
                    if (!response.ok || !data.ok) throw new Error(data.message || 'Не удалось повторить. Обновите статус отправки.');
                    await load();
                  } catch (error) { receipt.textContent = error.message; }
                  finally { busy = false; retry.disabled = false; }
                });
                entry.append(retry);
              }
              article.append(entry);
            });
          } else {
            article.append(node('p', 'Старый формат. Выберите эти позиции и подготовьте новое обращение перед отправкой.'));
          }
          group.append(article);
        });
        if (job.result.questions.length) {
          group.append(node('h3', 'Что нужно уточнить'));
          const questions = node('ul');
          job.result.questions.forEach(q => questions.append(node('li', q)));
          group.append(questions);
        }
      } else {
        const explanation = job.status === 'failed' ? (errors[job.error] || 'Агент не завершил задание. Проверьте подключение и повторите подготовку.') : job.status === 'canceled' ? 'Приём результата отменён. Текущий черновик может завершиться на Mac, но сюда не попадёт.' : 'Закупщик подготовит отдельные обращения по материалам, работам и оборудованию. Можно закрыть страницу.';
        group.append(node('p', explanation));
        const positions = node('ul');
        job.positions.forEach(p => positions.append(node('li', `${p.name} — ${p.quantity ?? 'объём не указан'} ${p.unit || ''}`)));
        group.append(positions);
      }
      list.append(group);
    });
    if (focusedKey) {
      const group = Array.from(list.querySelectorAll('details')).find(el => el.dataset.key === focusedKey);
      const input = focusedInput ? Array.from(group?.querySelectorAll('input, textarea') || []).find(el => el.dataset.recipientKey === focusedInput) : null;
      (input || group?.querySelector('summary'))?.focus({preventScroll: true});
      // Email inputs do not support setSelectionRange in all browsers.
      if (input && selection?.[0] != null && ['text','textarea'].includes(input.type)) input.setSelectionRange(...selection);
    }
  }
  async function load() {
    try { const data = await api(); render(data.jobs, data.outbox, data.campaigns); }
    catch (error) { status.textContent = error.message; }
  }
  async function mutate(body) {
    if (busy) return;
    busy = true; start.disabled = cancel.disabled = true;
    status.textContent = body.action === 'cancel' ? 'Отменяем задания…' : 'Собираем позиции по направлениям…';
    try { await api(body); await load(); }
    catch (error) { status.textContent = error.message; }
    finally { busy = false; start.disabled = cancel.disabled = false; }
  }
  start.addEventListener('click', () => {
    const keys = Array.from(document.querySelectorAll('[data-agent-position]:checked')).map(el => el.value);
    mutate(keys.length ? {position_keys: keys} : {});
  });
  cancel.addEventListener('click', () => mutate({action: 'cancel'}));
  refresh.addEventListener('click', load);
  function syncSelection() {
    const count = document.querySelectorAll('[data-agent-position]:checked').length;
    start.textContent = count ? `Подготовить по выбранным (${count})` : 'Подготовить обращения';
  }
  document.addEventListener('change', syncSelection);
  // Existing bulk actions (and the price search) update this shared count
  // without firing checkbox change events. Keep displayed and submitted scope equal.
  const selectionCount = document.querySelector('#agentSelectedCount');
  if (selectionCount) new MutationObserver(syncSelection).observe(selectionCount, {childList: true, characterData: true, subtree: true});
  syncSelection();
  load();
  setInterval(() => { if (!document.hidden && !busy) load(); }, 15000);
})();
