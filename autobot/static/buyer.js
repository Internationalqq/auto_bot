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
  const legacyDrafts = root.querySelector('[data-buyer-drafts]');
  const coverage = root.querySelector('[data-buyer-coverage]');
  const discovery = root.querySelector('[data-buyer-discovery]');
  const mode = root.querySelector('[data-buyer-mode]');
  const runSelect = root.querySelector('[data-buyer-run]');
  const find = root.querySelector('[data-buyer-find]');
  const setup = root.querySelector('[data-buyer-setup]');
  let setupInitialized = false;
  let selectedRun = '', filter = 'all', loading = false, loadAgain = false;
  let busy = false, last = '', active = false;
  const recipients = new Map();
  const messages = new Map();
  const chatStorageKey = `autobot:buyer-chat:${root.dataset.buyer}`;
  let selectedChat = '', chatOpened = false, chatEntries = [], chatShell, chatHost;
  const chatScroll = new Map(), chatInfo = new Set();
  const chatDrafts = new Map();
  try { selectedChat = sessionStorage.getItem(chatStorageKey) || ''; chatOpened = !!selectedChat; } catch (_) { /* Storage may be disabled. */ }
  const campaignLabels = {queued:'Проверка ожидает запуска', checking:'Проверяем сайты поставщиков', completed:'Проверка сайтов завершена', failed:'Проверка остановлена'};
  const sendLabels = {queued: 'В очереди отправки', sending: 'Отправляем', sent: 'Отправка подтверждена', blocked: 'Не отправлено', uncertain: 'Нужна проверка отправки'};
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
  function draftState(job, jobs, outbox, campaigns, replies) {
    const own = outbox.filter(item => item.draft_job_id === job.id);
    const latest = own.at(-1);
    if ((replies.messages || []).some(reply => own.some(item => item.id === reply.outbound_id))) return {label:'Ответ получен'};
    if (latest) return {label:sendLabels[latest.status] || 'Статус уточняется', detail:latest.receipt?.detail || ''};
    if (!job.result) return {label:labels[job.status] || 'Обращение не подготовлено'};
    const campaign = campaigns.find(item => item.draft_job_id === job.id);
    if (campaign && ['queued','checking'].includes(campaign.status)) return {label:'Проверяем контакт…'};
    const failure = campaign?.contacts?.find(contact => !contact.outbox_id && contact.error)?.error || campaign?.error;
    if (failure) return {label:'Контакт не прошёл проверку', detail:failure};
    if (!job.can_send) return {label:'Старый формат обращения', detail:'Выберите позиции и подготовьте обращение заново.'};
    const supplier = job.supplier || {};
    const host = address => { try { return new URL(address).hostname.toLowerCase().replace(/^www\./,''); } catch (_) { return ''; } };
    const domain = host(supplier.url);
    const directory = ['2gis.ru','spravker.ru','orgsprav.com','rusprofile.ru','optsbyt.ru','metaprom.ru','vsem-podryad.ru','ruscable.ru'].some(value => domain === value || domain.endsWith('.'+value));
    if (directory) return {label:'Справочник · нужен контакт компании', detail:'Сохранена страница справочника. Для отправки нужен email самого поставщика.'};
    const keys = job.positions.map(p => p.position_key);
    const duplicate = outbox.find(item => {
      if (!['queued','sending','sent','uncertain'].includes(item.status)) return false;
      const other = jobs.find(candidate => candidate.id === item.draft_job_id);
      const otherSupplier = other?.supplier || {}, otherDomain = host(otherSupplier.url);
      const same = supplier.email && supplier.email === item.recipient || supplier.id && supplier.id === otherSupplier.id || domain && otherDomain && (domain === otherDomain || domain.endsWith('.'+otherDomain) || otherDomain.endsWith('.'+domain));
      const requested = other?.result?.drafts?.[item.draft_index]?.position_keys || [];
      return same && keys.length && keys.every(key => requested.includes(key));
    });
    if (duplicate) return {label:duplicate.status === 'sent' ? 'Такие позиции уже отправлены' : 'Уже есть запрос · '+sendLabels[duplicate.status], detail:'Для этой компании есть запрос по всем позициям этого текста. Проверьте существующую переписку.', related:duplicate.id};
    if (job.supplier && !supplier.email) return {label:'Нужен email поставщика', detail:'Текст подготовлен, но адрес для отправки не найден.'};
    return {label:'Черновик · отправка не запускалась', detail:'Текст сохранён. Проверка контакта и отправка запускаются кнопкой внутри обращения.'};
  }
  function render(jobs, outbox = [], campaigns = [], replies = {checks:{},messages:[]}, report = null) {
    const signature = JSON.stringify([jobs, outbox, campaigns, replies, report]);
    if (signature === last) { updateRelativeTimes(); return; }
    const open = new Set(Array.from(list.querySelectorAll('details[open]')).map(el => el.dataset.key));
    const focusedKey = list.contains(document.activeElement) ? document.activeElement.closest('details')?.dataset.key : null;
    const focusedInput = document.activeElement?.dataset?.recipientKey;
    const selection = focusedInput ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
    const focusedChatControl = document.activeElement?.dataset?.buyerChatControl;
    const chatSelection = document.activeElement?.tagName === 'TEXTAREA' ? [document.activeElement.selectionStart,document.activeElement.selectionEnd] : null;
    const contactScroll = list.querySelector('.buyer-chat-list')?.scrollTop || 0;
    rememberChatScroll();
    list.replaceChildren();
    const jobNodes = new Map();
    const mailNodes = new Map();
    jobs.forEach(job => {
      const group = node('details', null, 'buyer-group');
      group.dataset.key = job.id;
      group.open = open.has(job.id);
      const summary = node('summary');
      const ownOutbox = outbox.filter(item => item.draft_job_id === job.id);
      const latestSend = ownOutbox.at(-1);
      const readiness = draftState(job,jobs,outbox,campaigns,replies);
      const contact = latestSend?.recipient || job.supplier?.email;
      summary.append(node('strong', job.position_name), node('span', [readiness.label,contact,`Позиций: ${job.positions.length}`].filter(Boolean).join(' · ')));
      group.append(summary);
      if (readiness.detail) group.append(node('p',readiness.detail,'buyer-send-feedback'));
      if (readiness.related) {
        const existing = node('button','Открыть переписку','btn ghost'); existing.type = 'button';
        existing.addEventListener('click', () => {
          const entry = chatEntries.find(item => item.company.correspondence_ids?.includes(readiness.related));
          if (entry) { selectChat(entry.company.id,{open:true,focus:true}); chatShell.scrollIntoView({block:'start',behavior:'instant'}); }
        });
        group.append(existing);
      }
      if (job.result) {
        job.result.drafts.forEach((draft, index) => {
          const key = `${job.id}-${index}`;
          const message = messages.get(key) || {subject:draft.subject, body:draft.body};
          const article = node('article', null, 'buyer-draft');
          const heading = node('h3', message.subject), preview = node('p', message.body, 'buyer-body');
          if (job.supplier) {
            const requestText = node('details',null,'buyer-edit'); requestText.dataset.key = `text-${key}`;
            requestText.open = open.has(requestText.dataset.key);
            requestText.append(node('summary',`Позиции и текст запроса (${job.positions.length})`),heading,preview);
            article.append(requestText);
          } else article.append(heading, preview);
          if (job.supplier) {
            const source = node('a','Поставщик и ассортимент'); source.href = job.supplier.url;
            source.target = '_blank'; source.rel = 'noopener noreferrer'; article.append(source);
          }
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
            const automatic = node('button', job.supplier ? 'Отправить запрос поставщику' : 'Найти поставщиков и отправить', 'btn primary');
            automatic.type = 'button';
            const stale = !!report?.version_note && report.companies.some(c => c.draft_job_ids?.includes(job.id));
            automatic.disabled = stale;
            const autoRows = job.positions.filter(p => draft.position_keys.includes(p.position_key));
            const supported = job.supplier ? !!job.supplier.email : (/ярослав/i.test(job.region || '') && autoRows.length > 0 && autoRows.every(p => /щебень/i.test(p.name) && ['material','product'].includes(p.type_slug)));
            automatic.hidden = !supported;
            const autoFeedback = node('p', 'Автоподбор: щебень · Ярославская область · 3 сайта · Email. Другие направления и мессенджеры пока не подключены.', 'buyer-send-feedback');
            if (!supported) autoFeedback.textContent = 'Можно скопировать обращение и связаться по контакту компании или указать email ниже.';
            if (job.supplier?.email) autoFeedback.textContent = `Один запрос на ${job.positions.length} поз. · ${job.region}. Email будет проверен на сайте перед отправкой.`;
            if (stale) autoFeedback.textContent = 'Сначала запустите подбор для актуальной сметы. Сохранённый текст доступен для просмотра и копирования.';
            if (ownOutbox.some(item => item.draft_index === index)) automatic.hidden = true;
            autoFeedback.setAttribute('role','status');
            automatic.addEventListener('click', async () => {
              if (busy) return;
              busy = true; automatic.disabled = true; autoFeedback.textContent = 'Запускаем проверку сайтов и контактов…';
              try {
                const response = await fetch(url.replace(/jobs$/, 'outbox'), {method:'POST', headers:{'Content-Type':'application/json'},
                  body:JSON.stringify({action:'find_and_send',draft_job_id:job.id,draft_index:index,message:messages.get(key) || message})});
                const data = await response.json();
                if (!response.ok || !data.ok) throw new Error(data.message || 'Не удалось запустить проверку');
                autoFeedback.textContent = 'Проверяем контакт. После проверки здесь появится отправка или причина остановки.';
                group.open = true;
                await load();
              } catch(error) { autoFeedback.textContent = error.message; }
              finally { busy = false; automatic.disabled = false; }
            });
            article.append(automatic, autoFeedback);
            const copy = node('button', 'Скопировать обращение', 'btn ghost'); copy.type = 'button';
            copy.addEventListener('click', async () => {
              const value = messages.get(key) || message;
              try { await navigator.clipboard.writeText(`${value.subject}\n\n${value.body}`); copy.textContent = 'Скопировано'; }
              catch (_) { autoFeedback.textContent = 'Не удалось скопировать. Выделите текст обращения и скопируйте его вручную.'; }
            });
            article.append(copy);
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
            button.disabled = stale;
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
            if (!job.supplier?.email) article.append(form);
            const ownCampaigns = campaigns.filter(c => c.draft_job_id === job.id && c.draft_index === index);
            const latestContactFailure = ownCampaigns[0]?.contacts?.find(c => !c.outbox_id && c.error);
            if (latestContactFailure && !ownOutbox.length) {
              autoFeedback.textContent = `Не отправлено: ${latestContactFailure.error}. Можно указать проверенный email вручную.`;
              automatic.textContent = 'Повторить проверку контакта';
              article.append(form);
            } else if (ownCampaigns.some(c => ['queued','checking'].includes(c.status))) {
              autoFeedback.textContent = 'Проверяем контакт на сайте…';
              automatic.disabled = true;
            }
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
              const sentText = node('details',null,'buyer-edit'); sentText.dataset.key = `sent-text-${item.id}`;
              sentText.open = open.has(sentText.dataset.key);
              sentText.append(node('summary','Точный текст обращения'),node('h4',item.subject),node('p',item.body,'buyer-body'));
              entry.append(sentText);
              const receipt = node('p', item.receipt?.detail || 'Результат появится после проверки агентом на Mac.', 'buyer-send-feedback');
              entry.append(receipt);
              const check = replies.checks?.[item.id];
              if (item.status === 'sent') {
                const checkStatus = node('p', check?.status === 'checking' ? 'Проверяем ответы на Mac…' : check?.status === 'blocked' ? `Проверка ответов остановлена: ${check.error}` : check?.checked_at ? `Ответы проверены ${new Date(check.checked_at*1000).toLocaleString('ru-RU')}. ${check.error || ''}` : 'Ждём ответ. Автобот проверит переписку через несколько минут.', 'buyer-send-feedback');
                const checkButton = node('button','Проверить ответы','btn ghost'); checkButton.type = 'button';
                checkButton.disabled = check?.status === 'checking';
                checkButton.addEventListener('click',async () => {
                  if (busy) return; busy = true; checkButton.disabled = true;
                  try {
                    const response = await fetch(url.replace(/jobs$/,'outbox'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'check_replies',id:item.id})});
                    const data = await response.json();
                    if (!response.ok || !data.ok) throw new Error(data.message || 'Не удалось проверить ответы');
                    checkStatus.textContent = 'Проверка поставлена в очередь Mac.';
                  } catch(error) { checkStatus.textContent = error.message; }
                  finally { busy = false; checkButton.disabled = false; }
                });
                entry.append(checkStatus, checkButton);
              }
              (replies.messages || []).filter(reply => reply.outbound_id === item.id).forEach(reply => {
                entry.append(node('h4',`Ответ ${new Date(reply.received_at*1000).toLocaleString('ru-RU')}`),node('p',reply.raw_text,'buyer-body'));
                const followup = replies.followups?.find(f => f.reply_id === reply.id);
                if (followup) {
                  const sent = outbox.find(o => o.id === followup.outbound_id);
                  entry.append(node('p', sent?.status === 'sent' ? 'Автобот ответил: адрес объекта из документов отправлен.' : sent ? `Ответ с адресом: ${sendLabels[sent.status] || sent.status}` : followup.reason, 'buyer-send-feedback'));
                  if (followup.source?.address) entry.append(node('p',`${followup.source.address} · Источник: ${followup.source.document}`, 'buyer-send-feedback'));
                }
                if (!reply.prices.length) entry.append(node('p','В ответе пока нет построчных цен.'));
                reply.prices.forEach(price => {
                  const position = job.positions.find(p => p.position_key === price.position_key);
                  const amount = price.price_kopecks == null ? 'Цена не указана' : `${(price.price_kopecks/100).toLocaleString('ru-RU')} ₽ / ${price.unit}`;
                  const row = node('div',null,'buyer-quote');
                  row.append(node('strong',position?.name || 'Позиция запроса'),node('p',`${amount} · ${price.vat || 'НДС не указан'}`),node('p',price.state === 'comparable' ? 'Цена привязана к позиции сметы. Условия доставки учитываются отдельно.' : `Нужно уточнить: ${price.reason}`));
                  if (price.state === 'comparable' && price.comparison_kopecks != null && price.estimate_unit !== price.unit) row.append(node('p',`В единице сметы: ${(price.comparison_kopecks/100).toLocaleString('ru-RU')} ₽ / ${price.estimate_unit}`));
                  if (price.availability || price.delivery) row.append(node('p',[price.availability,price.delivery].filter(Boolean).join(' · ')));
                  entry.append(row);
                });
              });
              (item.attempts || []).forEach(attempt => entry.append(node('p', `Предыдущий результат: ${attempt.receipt.detail} · Зафиксирован ${new Date(attempt.retried_at*1000).toLocaleString('ru-RU')}`)));
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
              mailNodes.set(item.id, entry);
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
      jobNodes.set(job.id, group);
    });
    renderCompanies(report, jobs, outbox, replies, jobNodes, open, mailNodes, campaigns);
    const contacts = list.querySelector('.buyer-chat-list'); if (contacts) contacts.scrollTop = contactScroll;
    if (focusedChatControl) {
      const control = Array.from(list.querySelectorAll('[data-buyer-chat-control]')).find(el => el.dataset.buyerChatControl === focusedChatControl);
      control?.focus({preventScroll:true});
      if (control?.tagName === 'TEXTAREA' && chatSelection?.[0] != null) control.setSelectionRange(...chatSelection);
    }
    if (focusedKey) {
      const group = Array.from(list.querySelectorAll('details')).find(el => el.dataset.key === focusedKey);
      const input = focusedInput ? Array.from(group?.querySelectorAll('input, textarea') || []).find(el => el.dataset.recipientKey === focusedInput) : null;
      (input || group?.querySelector('summary'))?.focus({preventScroll: true});
      // Email inputs do not support setSelectionRange in all browsers.
      if (input && selection?.[0] != null && ['text','textarea'].includes(input.type)) input.setSelectionRange(...selection);
    }
    last = signature;
  }
  function safeLink(address, label, channel = 'web') {
    const a = node('a', label);
    if (channel === 'email' && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(address)) a.href = `mailto:${address}`;
    else if (channel === 'phone' && /^[+\d ()-]+$/.test(address)) a.href = `tel:${address.replace(/[^+\d]/g,'')}`;
    else if (/^https?:\/\//i.test(address)) { a.href = address; a.target = '_blank'; a.rel = 'noopener noreferrer'; }
    return a;
  }
  function correspondence(company, outbox, replies) {
    const ids = new Set(company.draft_job_ids || []);
    const eventTime = item => item.status === 'sent' ? item.updated_at || 0 : item.created_at || 0;
    const outgoing = outbox.filter(item => company.correspondence_ids ? company.correspondence_ids.includes(item.id) : ids.has(item.draft_job_id) && (!company.correspondence_recipient || item.recipient === company.correspondence_recipient)).sort((a,b) => eventTime(b)-eventTime(a));
    const outgoingIds = new Set(outgoing.map(item => item.id));
    const answers = (replies.messages || []).filter(reply => outgoingIds.has(reply.outbound_id)).sort((a,b) => b.received_at-a.received_at);
    const checks = outgoing.filter(item => item.status === 'sent').map(item => replies.checks?.[item.id]).filter(Boolean);
    return {outgoing, latest:outgoing[0], answers, answer:answers[0],
      blocked:checks.some(check => check.status === 'blocked'),
      checking:checks.some(check => check.status === 'checking'),
      checkedAt:Math.max(0,...checks.map(check => check.checked_at || 0)),
      sent:outgoing.some(item => item.status === 'sent'),
      attention:outgoing.some(item => ['blocked','uncertain'].includes(item.status)) || checks.some(check => check.status === 'blocked')};
  }
  function companyList(report, jobs, outbox, replies) {
    const companies = (report?.companies || []).map(c => ({...c, contacts:[...(c.contacts || [])], prices:[...(c.prices || [])], position_keys:[...c.position_keys], draft_job_ids:[...(c.draft_job_ids || [])], current_job_ids:[...(c.draft_job_ids || [])], correspondence_ids:outbox.filter(item => c.draft_job_ids?.includes(item.draft_job_id)).map(item => item.id), history_outbox_ids:[]}));
    const assigned = new Set(companies.flatMap(c => c.draft_job_ids));
    // Sent requests stay visible when a newer sourcing run replaces its results.
    jobs.filter(job => !assigned.has(job.id) && (!report && job.supplier || outbox.some(item => item.draft_job_id === job.id))).forEach(job => {
        const supplier = job.supplier || {}, sent = outbox.filter(item => item.draft_job_id === job.id);
        const addresses = sent.length ? [...new Set(sent.map(item => item.recipient))] : [supplier.email || ''];
        addresses.forEach(address => {
        const id = address || supplier.id || job.id;
        let company = companies.find(c => address ? c.correspondence_recipient === address || c.contacts.some(contact => contact.channel === 'email' && address === contact.address) : c.id === id);
        if (!company) {
          company = {id, name:addresses.length === 1 && supplier.company || address || job.position_name, source_url:supplier.url, contacts:address ? [{channel:'email',address}] : supplier.channels || [], prices:[], status:job.result ? 'prepared' : job.status, position_keys:[], draft_job_ids:[], current_job_ids:[], region_note:supplier.region_note, previous:!!report, correspondence_recipient:address, correspondence_ids:[], history_outbox_ids:[]};
          companies.push(company);
        }
        if (!company.draft_job_ids.includes(job.id)) company.draft_job_ids.push(job.id);
        if (!sent.length) company.current_job_ids.push(job.id);
        company.position_keys = [...new Set([...company.position_keys, ...job.positions.map(p => p.position_key)])];
        const own = sent.filter(item => item.recipient === address);
        company.correspondence_ids.push(...own.map(item => item.id));
        company.history_outbox_ids.push(...own.map(item => item.id));
        const answers = (replies.messages || []).filter(r => own.some(o => o.id === r.outbound_id));
        company.prices.push(...answers.flatMap(r => (r.prices || []).map(p => ({...p,origin:'reply'}))));
        if (answers.length) company.status = 'answered';
        else if (company.status !== 'answered' && own.length) company.status = own.at(-1).status;
        });
    });
    return companies;
  }
  function shortDate(timestamp) {
    return timestamp ? new Date(timestamp*1000).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
  }
  function relativeAge(timestamp, now = Date.now()) {
    if (!Number.isFinite(Number(timestamp)) || Number(timestamp) <= 0) return '';
    const minutes = Math.max(0, Math.floor((now - Number(timestamp)*1000) / 60000));
    if (!minutes) return 'только что';
    if (minutes < 60) return `${minutes} мин назад`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} ч${minutes % 60 ? ` ${minutes % 60} мин` : ''} назад`;
    return `${Math.floor(hours / 24)} д${hours % 24 ? ` ${hours % 24} ч` : ''} назад`;
  }
  function updateRelativeTimes() {
    list.querySelectorAll('[data-buyer-sent-at]').forEach(time => {
      const text = relativeAge(time.dataset.buyerSentAt);
      if (time.textContent !== text) time.textContent = text;
    });
  }
  function conversationEvents(thread, replies) {
    const followups = new Map((replies.followups || []).map(item => [item.outbound_id,item]));
    const outgoing = thread.outgoing.map(item => ({id:`out-${item.id}`, direction:'out', item,
      timestamp:item.status === 'sent' ? item.updated_at : item.created_at, followup:followups.get(item.id)}));
    const incoming = thread.answers.map(item => ({id:`in-${item.id}`, direction:'in', item, timestamp:item.received_at}));
    return [...outgoing,...incoming].sort((a,b) => (a.timestamp || 0)-(b.timestamp || 0) || a.id.localeCompare(b.id));
  }
  function chatState(thread, company) {
    if (thread.latest && thread.latest.status !== 'sent') return sendLabels[thread.latest.status] || 'Не отправлено';
    if (thread.answer && (!thread.latest || thread.answer.received_at > (thread.latest.updated_at || 0))) return 'Ответ получен';
    if (thread.blocked) return 'Проверка почты недоступна';
    if (thread.checking) return 'Проверяем ответы…';
    if (thread.sent) return 'Ожидаем ответ';
    return {prepared:'Запрос готов',contact_required:'Нужен контакт',queued:'Готовим запрос',leased:'Готовим запрос'}[company.status] || 'Готовим обращение';
  }
  function messagePreview(event) {
    if (!event) return 'Переписка ещё не началась';
    const body = event.direction === 'in' ? event.item.raw_text : event.item.body;
    const text = (body || event.item.subject || 'Сообщение без текста').replace(/\s+/g,' ').replace(/^\s*(?:добрый (?:день|вечер)|доброе утро|здравствуйте)[!,.\s]+/i,'').trim();
    return `${event.direction === 'out' ? event.followup ? 'Автобот: ' : 'Вы: ' : ''}${text.slice(0,130)}`;
  }
  function rememberChatScroll() {
    const entry = chatEntries.find(item => item.company.id === selectedChat);
    if (!entry || entry.panel.hidden || entry.timeline.hidden || !entry.timeline.clientHeight) return;
    const el = entry.timeline;
    chatScroll.set(selectedChat,{top:el.scrollTop, bottom:el.scrollHeight-el.clientHeight-el.scrollTop < 32});
  }
  function selectChat(id, {open = false, focus = false} = {}) {
    rememberChatScroll();
    selectedChat = id;
    if (open) {
      chatOpened = true;
      try { sessionStorage.setItem(chatStorageKey,id); } catch (_) { /* In-memory selection still works. */ }
    }
    if (chatShell) chatShell.dataset.open = String(chatOpened && !!id);
    chatEntries.forEach(entry => {
      const selected = entry.company.id === id;
      entry.button.setAttribute('aria-pressed',String(selected)); entry.panel.hidden = !selected;
      if (selected) {
        const saved = chatScroll.get(id);
        entry.timeline.scrollTop = !saved || saved.bottom ? entry.timeline.scrollHeight : saved.top;
        if (focus) entry.title.focus({preventScroll:true});
      }
    });
    const empty = chatHost?.querySelector('.buyer-chat-placeholder'); if (empty) empty.hidden = !!id;
    if (open && typeof matchMedia !== 'undefined' && matchMedia('(max-width: 700px)').matches) chatShell.scrollIntoView({block:'start',behavior:'instant'});
  }
  function renderTimeline(thread, replies, name, expanded) {
    const timeline = node('div',null,'buyer-chat-timeline');
    timeline.setAttribute('role','log'); timeline.setAttribute('aria-label',`История сообщений: ${name}`);
    timeline.tabIndex = 0;
    const events = conversationEvents(thread,replies);
    let previousDay = '';
    events.forEach(event => {
      const stamp = Number(event.timestamp), validTime = Number.isFinite(stamp) && stamp > 0;
      const date = validTime ? new Date(stamp*1000) : null;
      const day = date ? date.toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:'numeric'}) : 'Дата не указана';
      if (day !== previousDay) { timeline.append(node('p',day,'buyer-chat-day')); previousDay = day; }
      const incoming = event.direction === 'in', sent = event.item.status === 'sent';
      const bubble = node('article',null,`buyer-message buyer-message-${event.direction}${!incoming && !sent ? ' buyer-message-pending' : ''}`);
      bubble.dataset.messageId = event.id;
      const sender = incoming && event.item.sender && event.item.sender !== thread.latest?.recipient ? `${name} · ${event.item.sender}` : name;
      bubble.append(node('span',incoming ? sender : event.followup ? 'Автобот · адрес объекта' : 'Вы','buyer-message-author'));
      if (event.item.subject && !event.followup && !event.item.manual) bubble.append(node('h4',event.item.subject,'buyer-message-subject'));
      bubble.append(node('p',(incoming ? event.item.raw_text : event.item.body) || 'Сообщение без текста','buyer-message-text'));
      const meta = node('footer',null,'buyer-message-meta');
      if (date) {
        const time = node('time',date.toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}));
        time.dateTime = date.toISOString(); time.title = `${incoming ? 'Получено' : sent ? 'Отправка подтверждена' : 'Создано'} ${date.toLocaleString('ru-RU')}`;
        meta.append(time);
      }
      meta.append(node('span',incoming ? 'Получено' : sendLabels[event.item.status] || 'Не отправлено'));
      if (!incoming && sent && validTime) {
        const age = node('span',relativeAge(stamp)); age.dataset.buyerSentAt = String(stamp); meta.append(age);
      }
      bubble.append(meta);
      if (!incoming && !sent && event.item.receipt?.detail) bubble.append(node('p',event.item.manual ? event.item.receipt.detail : 'Подробности отправки и доступные действия — в сведениях.','buyer-message-reason'));
      if (event.followup?.source?.document) {
        const source = node('details',null,'buyer-message-source'); source.dataset.key = `source-${event.id}`; source.open = expanded.has(source.dataset.key);
        source.append(node('summary','Адрес из документа'),node('p',event.followup.source.document)); bubble.append(source);
      }
      timeline.append(bubble);
    });
    if (!events.length) {
      const empty = node('div',null,'buyer-chat-placeholder');
      empty.append(node('h3','Переписка ещё не началась'),node('p','Подготовленный запрос и действия отправки — в сведениях о компании.'));
      timeline.append(empty);
    }
    return timeline;
  }
  function chatDraft(parentId) {
    if (!chatDrafts.has(parentId)) {
      let saved;
      try { saved = JSON.parse(sessionStorage.getItem(`${chatStorageKey}:draft:${parentId}`)); } catch (_) {}
      const valid = saved && typeof saved.text === 'string' && saved.text.length <= 10000;
      const draft = valid ? saved : {text:''};
      draft.busy = false;
      chatDrafts.set(parentId,draft);
    }
    return chatDrafts.get(parentId);
  }
  function saveChatDraft(parentId,draft) {
    try { sessionStorage.setItem(`${chatStorageKey}:draft:${parentId}`,JSON.stringify({text:draft.text,attempt:draft.attempt,feedback:draft.feedback,queuedId:draft.queuedId})); } catch (_) {}
  }
  function renderComposer(thread,replies,companyId) {
    const form = node('form',null,'buyer-chat-composer');
    const latest = thread.outgoing.find(item => item.status === 'sent');
    if (!latest) {
      form.append(node('p','Написать сюда можно после подтверждения отправки первого запроса. Первый запрос — в «Сведениях».','buyer-composer-note'));
      return form;
    }
    const parentId = latest.parent_outbound_id || replies.followups?.find(item => item.outbound_id === latest.id)?.parent_outbound_id || latest.id;
    const draft = chatDraft(parentId);
    if (!draft.busy && draft.attempt && thread.outgoing.some(item => item.request_id === draft.attempt.request_id)) {
      draft.text = ''; draft.attempt = null; draft.feedback = ''; saveChatDraft(parentId,draft);
    }
    if (draft.queuedId && thread.outgoing.some(item => item.id === draft.queuedId)) {
      draft.queuedId = null; draft.feedback = ''; saveChatDraft(parentId,draft);
    }
    const label = node('label',`Сообщение для ${latest.recipient}`,'sr-only');
    const input = node('textarea'); input.id = `buyer-message-input-${companyId}`; label.htmlFor = input.id;
    input.rows = 2; input.maxLength = 10000; input.placeholder = 'Написать сообщение…'; input.value = draft.text;
    input.dataset.buyerChatControl = `compose-${companyId}`;
    const actions = node('div',null,'buyer-composer-actions');
    const hint = node('span','Ctrl / ⌘ + Enter — отправить','buyer-composer-hint');
    const send = node('button','Отправить','btn primary buyer-message-send'); send.type = 'submit';
    send.dataset.buyerChatControl = `send-${companyId}`;
    const feedback = node('p',draft.feedback || '', 'buyer-composer-feedback'); feedback.setAttribute('role','status');
    function sync() {
      if (input.value !== draft.text) input.value = draft.text;
      input.readOnly = draft.busy || !!draft.attempt;
      send.disabled = draft.busy || !draft.text.trim();
      send.textContent = draft.busy ? 'Отправляем…' : draft.attempt ? 'Проверить отправку' : 'Отправить';
      feedback.textContent = draft.feedback || ''; feedback.hidden = !draft.feedback;
    }
    draft.sync = sync;
    input.addEventListener('input',() => { draft.text = input.value; draft.feedback = ''; saveChatDraft(parentId,draft); sync(); });
    async function submit(event) {
      event.preventDefault();
      if (draft.busy || !draft.text.trim()) return;
      if (!draft.attempt) {
        draft.attempt = {action:'message',parent_id:parentId,body:draft.text,request_id:crypto.randomUUID()};
      }
      draft.busy = true; draft.feedback = ''; saveChatDraft(parentId,draft); sync();
      let rejected = false;
      try {
        const response = await fetch(url.replace(/jobs$/,'outbox'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(draft.attempt)});
        const data = await response.json();
        if (!response.ok || !data.ok) {
          rejected = response.status >= 400 && response.status < 500;
          throw new Error(data.message || 'Не удалось подтвердить отправку');
        }
        draft.text = ''; draft.attempt = null; draft.queuedId = data.id; draft.feedback = 'В очереди отправки. Подтверждение появится в истории.';
        input.value = ''; chatScroll.set(companyId,{top:0,bottom:true});
      } catch (error) {
        if (rejected) draft.attempt = null;
        draft.feedback = rejected ? error.message : 'Нет подтверждения от сервера. Нажмите «Проверить отправку» — повторного письма не будет.';
      } finally {
        draft.busy = false; saveChatDraft(parentId,draft); draft.sync();
      }
      await load();
    }
    form.addEventListener('submit',submit);
    input.addEventListener('keydown',event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) submit(event); });
    actions.append(hint,send); form.append(label,input,actions,feedback); sync();
    return form;
  }
  function renderCompanies(report, jobs, outbox, replies, jobNodes, expanded, mailNodes = new Map(), campaigns = []) {
    chatEntries = [];
    const stopped = campaigns[0]?.contacts?.filter(c => !c.outbox_id && c.error) || [];
    if (stopped.length) list.append(node('p', `Последняя попытка — не отправлено. ${stopped.map(c => `${c.company}: ${c.error}`).join(' · ')}`, 'buyer-send-feedback buyer-last-attempt'));
    const companies = companyList(report, jobs, outbox, replies);
    const positionMap = new Map([...jobs.flatMap(j => j.positions), ...(report?.positions || [])].map(p => [p.position_key,p]));
    const used = new Set();
    chatShell = node('div',null,'buyer-chats');
    const sidebar = node('nav',null,'buyer-chat-list'); sidebar.setAttribute('aria-label','Чаты поставщиков');
    const noMatches = node('p','По этому фильтру компаний нет. Измените поиск или выберите «Все».','buyer-chat-placeholder buyer-chat-no-results'); noMatches.hidden = true; sidebar.append(noMatches);
    chatHost = node('div',null,'buyer-chat-host');
    const placeholder = node('div',null,'buyer-chat-placeholder');
    placeholder.append(node('h3','Выберите компанию'),node('p','Здесь будет история запросов и ответов.'));
    chatHost.append(placeholder); chatShell.append(sidebar,chatHost);
    if (companies.length) list.append(chatShell);
    const byActivity = companies.map(company => ({company,thread:correspondence(company,outbox,replies)}));
    byActivity.sort((a,b) => (conversationEvents(b.thread,replies).at(-1)?.timestamp || 0)-(conversationEvents(a.thread,replies).at(-1)?.timestamp || 0));
    byActivity.forEach(({company,thread}, index) => {
      const card = node('button', null, 'buyer-company'); card.type = 'button'; card.dataset.key = `company-${company.id}`;
      const positions = company.position_keys.map(key => positionMap.get(key)).filter(Boolean);
      const prices = company.prices || [], priced = prices.some(p => p.price_kopecks != null);
      const attention = thread.attention || ['blocked','uncertain','contact_required'].includes(company.status) || prices.some(p => p.state === 'review') || !!report?.version_note;
      card.dataset.priced = String(priced); card.dataset.answered = String(!!thread.answer); card.dataset.sent = String(thread.sent); card.dataset.attention = String(attention);
      card.dataset.search = [company.name,...thread.outgoing.map(item => item.recipient),...company.contacts.map(c => c.address),...positions.map(p => p.name)].join(' ').toLocaleLowerCase('ru-RU');
      const knownContact = campaigns.flatMap(c => c.contacts || []).find(c => thread.outgoing.some(item => item.id === c.outbox_id));
      const name = company.name.includes('@') && knownContact?.company ? knownContact.company : company.name;
      card.dataset.search += ` ${name.toLocaleLowerCase('ru-RU')}`;
      const recipient = thread.latest?.recipient || company.contacts[0]?.address || 'Контакт пока не найден';
      const state = chatState(thread,company);
      const lastEvent = conversationEvents(thread,replies).at(-1);
      const avatar = node('span',name.replace(/^(?:ООО|АО|ИП)\s+/i,'').split(/[\s@.-]+/).filter(Boolean).slice(0,2).map(part => part[0]).join('').toLocaleUpperCase('ru-RU'),'buyer-chat-avatar');
      avatar.setAttribute('aria-hidden','true');
      const identity = node('span',null,'buyer-chat-identity'), top = node('span',null,'buyer-chat-top');
      top.append(node('strong',name));
      const tone = state === 'Ответ получен' ? 'answered' : state === 'Ожидаем ответ' ? 'waiting' : thread.latest?.status === 'blocked' ? 'error' : thread.blocked || thread.latest?.status === 'uncertain' || company.status === 'contact_required' ? 'warning' : thread.checking || ['queued','sending'].includes(thread.latest?.status) ? 'active' : 'neutral';
      const stateLabel = node('span',state,`buyer-chat-state buyer-chat-${tone}`);
      const statusLine = node('span',null,'buyer-chat-status'); statusLine.append(stateLabel);
      if (lastEvent?.timestamp && (lastEvent.direction === 'in' || lastEvent.item.status === 'sent')) {
        const time = node('time',relativeAge(lastEvent.timestamp),'buyer-chat-age');
        time.dataset.buyerSentAt = String(lastEvent.timestamp); time.dateTime = new Date(lastEvent.timestamp*1000).toISOString();
        time.title = `${lastEvent.direction === 'in' ? 'Ответ получен' : 'Отправка подтверждена'} ${shortDate(lastEvent.timestamp)}`; statusLine.append(time);
      }
      identity.append(top,node('span',messagePreview(lastEvent),'buyer-chat-preview'),statusLine);
      const price = prices.find(p => p.origin === 'reply' && p.price_kopecks != null) || prices.find(p => p.price_kopecks != null);
      if (price) {
        identity.append(node('span',`${(price.price_kopecks/100).toLocaleString('ru-RU')} ₽ / ${price.unit || 'ед.'} · ${price.origin === 'website' ? 'с сайта' : 'из ответа'}${price.state === 'review' || price.origin === 'website' ? ' · уточнить' : ''}`,'buyer-chat-price'));
      }
      card.setAttribute('aria-label',`${name} · ${recipient} · ${state}`); card.setAttribute('aria-controls',`buyer-chat-${index}`);
      card.dataset.buyerChatControl = `contact-${company.id}`;
      card.append(avatar,identity); sidebar.append(card);
      const panel = node('section',null,'buyer-chat'); panel.id = `buyer-chat-${index}`; panel.hidden = true;
      const header = node('header',null,'buyer-chat-header');
      const back = node('button',null,'buyer-chat-back'); back.type = 'button'; back.append(node('span','Чаты','sr-only'));
      back.dataset.buyerChatControl = `back-${company.id}`;
      back.addEventListener('click',() => { chatOpened = false; chatShell.dataset.open = 'false'; try { sessionStorage.removeItem(chatStorageKey); } catch (_) {} card.focus({preventScroll:true}); chatShell.scrollIntoView({block:'start',behavior:'instant'}); });
      const heading = node('div',null,'buyer-chat-heading'), title = node('h3',name); title.tabIndex = -1;
      title.dataset.buyerChatControl = `title-${company.id}`;
      heading.append(title,node('span',recipient,'buyer-chat-contact'));
      const info = node('button','Сведения','btn ghost buyer-chat-info-button'); info.type = 'button';
      info.dataset.buyerChatControl = `info-${company.id}`;
      header.append(back,heading,info);
      const timeline = renderTimeline(thread,replies,name,expanded);
      timeline.dataset.buyerChatControl = `history-${company.id}`;
      const body = node('div', null, 'buyer-chat-info'); body.id = `buyer-chat-info-${index}`;
      const composer = renderComposer(thread,replies,company.id);
      body.hidden = !chatInfo.has(company.id); timeline.hidden = !body.hidden;
      composer.hidden = !body.hidden;
      info.setAttribute('aria-expanded',String(!body.hidden)); info.setAttribute('aria-controls',body.id);
      info.textContent = body.hidden ? 'Сведения' : 'Переписка';
      info.addEventListener('click',() => {
        if (body.hidden) { rememberChatScroll(); chatInfo.add(company.id); } else chatInfo.delete(company.id);
        body.hidden = !body.hidden; timeline.hidden = !body.hidden;
        composer.hidden = !body.hidden;
        info.textContent = body.hidden ? 'Сведения' : 'Переписка'; info.setAttribute('aria-expanded',String(!body.hidden));
        if (body.hidden) { const saved = chatScroll.get(company.id); timeline.scrollTop = !saved || saved.bottom ? timeline.scrollHeight : saved.top; }
      });
      const footer = node('div',null,'buyer-chat-footer');
      footer.append(node('span',state,thread.blocked ? 'buyer-inbox-warning' : ''));
      if (thread.blocked) footer.append(node('span','Причина и повтор проверки — в сведениях.'));
      else footer.append(node('span',thread.checkedAt ? `Проверено ${shortDate(thread.checkedAt)} · Email` : 'Переписка по email'));
      const bottom = node('div',null,'buyer-chat-bottom'); bottom.append(footer,composer);
      panel.append(header,timeline,body,bottom); chatHost.append(panel);
      chatEntries.push({company,button:card,panel,timeline,title});
      card.addEventListener('click',() => selectChat(company.id,{open:true,focus:true}));
      const contactDetails = node('details',null,'buyer-contact-details'); contactDetails.dataset.key = `contacts-${company.id}`; contactDetails.open = expanded.has(contactDetails.dataset.key);
      contactDetails.append(node('summary','Контакты и сайт компании'));
      const contacts = node('div', null, 'buyer-contacts');
      if (company.source_url) contacts.append(safeLink(company.source_url,'Сайт компании'));
      const channelNames = {email:'Email',phone:'Телефон',telegram:'Telegram',whatsapp:'WhatsApp',max:'MAX',avito:'Авито'};
      const seen = new Set();
      company.contacts.forEach(c => {
        const address = c.channel === 'phone' ? c.address.replace(/\D/g,'').replace(/^8(?=\d{10}$)/,'7') : c.address;
        const key = `${c.channel}:${address}`; if (seen.has(key)) return; seen.add(key);
        contacts.append(safeLink(c.address, `${channelNames[c.channel] || c.channel}: ${c.address}`, c.channel));
      });
      contactDetails.append(contacts);
      if (company.region_note) contactDetails.append(node('p', company.region_note, 'buyer-region-note'));
      body.append(contactDetails);
      const scope = node('details', null, 'buyer-scope-details'); scope.dataset.key = `scope-${company.id}`; scope.open = expanded.has(scope.dataset.key);
      scope.append(node('summary',`Позиции сметы (${positions.length})`));
      const rows = node('ul'); positions.forEach(p => rows.append(node('li', `${p.name} — ${p.quantity ?? 'уточнить объём'} ${p.unit || ''}`))); scope.append(rows); body.append(scope);
      if (prices.length) {
        const priceList = node('div', null, 'buyer-price-list'); priceList.append(node('h3','Предложения по позициям'));
        prices.forEach(p => {
          const row = node('div', null, 'buyer-price-row');
          row.append(node('strong', positionMap.get(p.position_key)?.name || 'Позиция запроса'));
          row.append(node('b', p.price_kopecks == null ? 'Цена не указана' : `${(p.price_kopecks/100).toLocaleString('ru-RU')} ₽ / ${p.unit}`));
          row.append(node('span', [p.origin === 'website' ? 'С сайта · требуется подтверждение' : p.state === 'comparable' ? 'Ответ поставщика · сопоставимая единица' : 'Ответ поставщика · нужно уточнение', p.vat || 'НДС не указан', p.availability, p.delivery, p.reason].filter(Boolean).join(' · ')));
          if (p.source_url) row.append(safeLink(p.source_url,'Источник цены'));
          priceList.append(row);
        }); body.append(priceList);
      }
      thread.outgoing.filter(item => company.history_outbox_ids.includes(item.id)).forEach(item => {
        const entry = mailNodes.get(item.id); if (entry) body.append(entry);
      });
      company.current_job_ids.forEach(id => {
        const group = jobNodes.get(id); if (!group) return;
        used.add(id); group.classList.add('buyer-request'); group.open = expanded.has(id);
        const heading = group.querySelector('summary strong'); if (heading) heading.textContent = 'Обращение и переписка';
        body.append(group);
      });
      if (!company.draft_job_ids?.length) body.append(node('p', report?.status === 'searching' ? 'Компания найдена. Обращение появится после завершения подбора.' : 'Обращение пока не подготовлено. Повторите незавершённые проверки.'));
    });
    const remaining = [...jobNodes.entries()].filter(([id]) => !used.has(id));
    if (remaining.length) {
      const archive = node('details',null,'buyer-archive'); archive.dataset.key = 'previous'; archive.open = expanded.has('previous');
      archive.append(node('summary',`Архив подготовленных обращений (${remaining.length})`));
      archive.append(node('p','Тексты из прошлых подборов. У каждого указано, отправлялся ли запрос и что нужно для отправки.', 'buyer-send-feedback'));
      remaining.forEach(([,group]) => archive.append(group)); list.append(archive);
    }
    if (!companies.length && !remaining.length) {
      const empty = node('div',null,'buyer-empty');
      empty.append(node('h3', report?.status === 'searching' ? 'Ищем подходящие компании' : report ? 'Компании пока не найдены' : 'От сметы — к предложениям'));
      empty.append(node('p', report ? 'Результаты проверок появятся здесь. Если источник недоступен, можно повторить подбор.' : 'Материалы сгруппируем по поставщикам, работы — по исполнителям. Одна компания получит общий запрос по подходящим позициям.'));
      list.append(empty);
    }
    const toolbar = root.querySelector('[data-buyer-toolbar]'); if (toolbar) toolbar.hidden = !companies.length;
    const mailNote = root.querySelector('[data-buyer-mail-note]');
    if (mailNote) {
      const sent = outbox.filter(item => item.status === 'sent');
      const blocked = sent.filter(item => replies.checks?.[item.id]?.status === 'blocked').length;
      mailNote.hidden = !sent.length;
      mailNote.textContent = blocked ? `${blocked === sent.length ? 'Проверка ответов недоступна' : 'Часть ответов не удалось проверить'}. В списке — только сохранённые ответы.` : 'Ответы проверяются каждые 10 минут.';
    }
    applyFilter();
  }
  function applyFilter(browse = false) {
    if (browse) chatOpened = false;
    const cards = Array.from(list.querySelectorAll('.buyer-company'));
    const query = (find?.value || '').trim().toLocaleLowerCase('ru-RU');
    let visible = 0;
    cards.forEach(card => { card.hidden = !(filter === 'all' || card.dataset[filter] === 'true') || !card.dataset.search.includes(query); if (!card.hidden) visible++; });
    root.querySelectorAll?.('[data-buyer-filter]').forEach(button => {
      const kind = button.dataset.buyerFilter; button.setAttribute('aria-pressed', String(kind === filter));
      button.querySelector('[data-buyer-count]').textContent = cards.filter(card => kind === 'all' || card.dataset[kind] === 'true').length;
    });
    const empty = list.querySelector('.buyer-chat-no-results'); if (empty) empty.hidden = visible > 0;
    const available = chatEntries.filter(entry => !entry.button.hidden);
    selectChat(available.some(entry => entry.company.id === selectedChat) ? selectedChat : available[0]?.company.id || '');
  }
  function renderCoverage(result, report) {
    if (!coverage) return;
    const expanded = coverage.querySelector('details')?.open;
    coverage.replaceChildren();
    const uncovered = report?.uncovered || result?.uncovered || [];
    if (uncovered.length) {
      const details = node('details'); details.open = !!expanded;
      details.append(node('summary',`Ещё нужен поставщик: ${uncovered.length} поз.`));
      const rows = node('ul'); uncovered.forEach(p => rows.append(node('li',`${p.name} — ${p.reason}`)));
      details.append(rows); coverage.append(details);
    }
  }
  async function load() {
    if (loading) { loadAgain = true; return; }
    loading = true; refresh.disabled = true;
    try {
      const data = await api(), runs = data.searches || [];
      let report = null;
      if (runs.length) {
        if (selectedRun && !runs.some(run => run.id === selectedRun)) selectedRun = '';
        const response = await fetch(url.replace(/jobs$/, 'report') + (selectedRun ? `?run_id=${encodeURIComponent(selectedRun)}` : ''), {cache:'no-store'});
        report = await response.json();
        if (!response.ok || !report.ok) throw new Error(report.message || 'Не удалось загрузить компании. Нажмите «Обновить».');
      }
      render(data.jobs, data.outbox, data.campaigns, data.replies, report);
      renderCoverage(data.coverage, report); renderSearches(runs, data.jobs, report);
      status.classList?.remove('buyer-error');
    }
    catch (error) {
      status.textContent = error.message; status.classList?.add('buyer-error');
      const bar = root.querySelector('[data-buyer-statusbar]'); if (bar) bar.hidden = false;
    }
    finally { loading = false; refresh.disabled = false; if (loadAgain) { loadAgain = false; await load(); } }
  }
  let lastSearches = '';
  function renderSearches(runs, jobs, report) {
    if (!discovery) return;
    active = runs.some(run => run.status === 'searching') || jobs.some(job => ['queued','leased'].includes(job.status));
    if (setup && !setupInitialized) {
      setup.open = active || !list.querySelectorAll('.buyer-company').length;
      setupInitialized = true;
    }
    const bar = root.querySelector('[data-buyer-statusbar]'); if (bar) bar.hidden = !active && !!list.querySelectorAll('.buyer-company').length;
    cancel.hidden = !active;
    const run = runs.find(r => r.id === report?.run_id) || runs[0];
    const names = {searching:'Подбор выполняется',completed:'Подбор завершён',partial:'Есть результат · часть сайтов недоступна',canceled:'Подбор остановлен'};
    status.textContent = run ? `${names[run.status]} · Позиций: ${run.position_count} · Компаний: ${report?.companies.length || 0}` : jobs.length ? `Обращений по этому тендеру: ${jobs.length}${active ? ' · Подготовка выполняется' : ''}` : 'Запустите подбор — компании и обращения появятся здесь. Можно закрыть страницу, работа продолжится.';
    const signature = JSON.stringify([runs.map(r => [r.id,r.status,r.updated_at,r.steps]), selectedRun, report?.version_note]);
    if (signature === lastSearches) return;
    lastSearches = signature;
    discovery.replaceChildren();
    if (run) {
      const section = node('div', null, 'buyer-progress');
      const ready = run.steps.filter(s => s.status === 'completed').length;
      if (run.status === 'searching') section.append(node('p', `Проверено источников: ${ready} из ${run.steps.length}. Список дополняется автоматически.`));
      if (report?.version_note) section.append(node('p', report.version_note + '. Запустите подбор для актуальной сметы.', 'buyer-warning'));
      if (['partial','canceled'].includes(run.status)) {
        const retry = node('button','Повторить незавершённые проверки','btn ghost'); retry.type = 'button';
        retry.addEventListener('click', () => mutate({action:'retry_search',run_id:run.id})); section.append(retry);
      }
      const failures = [...new Set(run.steps.map(step => step.error).filter(Boolean))];
      if (failures.length) {
        const details = node('details'); details.append(node('summary', `Недоступные источники (${failures.length})`));
        const rows = node('ul'); failures.forEach(error => rows.append(node('li', error))); details.append(rows); section.append(details);
      }
      discovery.append(section);
    }
    if (runSelect) {
      runSelect.replaceChildren(node('option','Последний подбор')); runSelect.firstChild.value = '';
      runs.forEach(r => { const option = node('option', `${new Date(r.created_at*1000).toLocaleString('ru-RU')} · ${r.position_count} поз. · ${names[r.status]}`); option.value = r.id; runSelect.append(option); });
      runSelect.value = selectedRun; runSelect.disabled = !runs.length;
    }
    const exports = root.querySelector('[data-buyer-exports]');
    if (exports) {
      exports.replaceChildren();
      if (run) ['text','json'].forEach(format => {
        const link = node('a', format === 'text' ? 'Скачать текст' : 'JSON', 'btn ghost');
        link.href = url.replace(/jobs$/, 'report') + `?run_id=${encodeURIComponent(run.id)}&format=${format}`;
        link.target = '_blank'; link.rel = 'noopener noreferrer'; exports.append(link);
      });
    }
  }
  async function mutate(body) {
    if (busy) return;
    busy = true; start.disabled = cancel.disabled = true;
    status.textContent = body.action === 'cancel' ? 'Отменяем задания…' : 'Собираем позиции по направлениям…';
    try {
      await api(body); if (body.action === 'search_suppliers') selectedRun = ''; await load();
    }
    catch (error) { status.textContent = error.message; }
    finally { busy = false; start.disabled = cancel.disabled = false; }
  }
  start.addEventListener('click', () => {
    const keys = Array.from(document.querySelectorAll('[data-agent-position]:checked')).map(el => el.value);
    const body = keys.length ? {action:'search_suppliers',position_keys: keys} : {action:'search_suppliers'};
    if (mode?.value === 'email') body.delivery = 'email';
    mutate(body);
  });
  legacyDrafts?.addEventListener('click',() => {
    const keys = Array.from(document.querySelectorAll('[data-agent-position]:checked')).map(el => el.value);
    mutate(keys.length ? {position_keys:keys} : {});
  });
  cancel.addEventListener('click', () => mutate({action: 'cancel'}));
  refresh.addEventListener('click', load);
  function syncSelection() {
    const count = document.querySelectorAll('[data-agent-position]:checked').length;
    const action = mode?.value === 'email' ? 'Найти и отправить запросы' : 'Подобрать поставщиков';
    start.textContent = count ? `${action} (${count})` : action;
    const scope = root.querySelector('[data-buyer-scope]');
    if (count && setup) setup.open = true;
    if (scope) scope.textContent = count ? `Выбрано позиций: ${count}` : 'Все позиции без подтверждённой цены';
    const hint = root.querySelector('[data-buyer-mode-hint]');
    if (hint) hint.textContent = mode?.value === 'email' ? 'Автобот найдёт компании и отправит каждой общий запрос по выбранным позициям. Ответы появятся здесь. Мессенджеры доступны для ручного обращения.' : 'Автобот найдёт компании и соберёт общий запрос для каждой. Вы сможете проверить текст перед отправкой.';
  }
  mode?.addEventListener('change', syncSelection);
  runSelect?.addEventListener('change', () => { selectedRun = runSelect.value; load(); });
  find?.addEventListener('input', () => applyFilter(true));
  root.querySelectorAll?.('[data-buyer-filter]').forEach(button => button.addEventListener('click', () => { filter = button.dataset.buyerFilter; applyFilter(true); }));
  document.addEventListener('change', syncSelection);
  // Existing bulk actions (and the price search) update this shared count
  // without firing checkbox change events. Keep displayed and submitted scope equal.
  const selectionCount = document.querySelector('#agentSelectedCount');
  if (selectionCount) new MutationObserver(syncSelection).observe(selectionCount, {childList: true, characterData: true, subtree: true});
  syncSelection();
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(() => {
    const entry = chatEntries.find(item => item.company.id === selectedChat);
    if (entry && !entry.timeline.hidden && (!chatScroll.has(selectedChat) || chatScroll.get(selectedChat).bottom)) entry.timeline.scrollTop = entry.timeline.scrollHeight;
  }).observe(root);
  load();
  setInterval(() => { if (!document.hidden) { updateRelativeTimes(); if (!busy) load(); } }, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) updateRelativeTimes(); });
})();
