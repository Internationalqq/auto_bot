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
  function render(jobs, outbox = [], campaigns = [], replies = {checks:{},messages:[]}, report = null) {
    const signature = JSON.stringify([jobs, outbox, campaigns, replies, report]);
    if (signature === last) return;
    const open = new Set(Array.from(list.querySelectorAll('details[open]')).map(el => el.dataset.key));
    const focusedKey = list.contains(document.activeElement) ? document.activeElement.closest('details')?.dataset.key : null;
    const focusedInput = document.activeElement?.dataset?.recipientKey;
    const selection = focusedInput ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
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
      const hasReply = (replies.messages || []).some(reply => ownOutbox.some(item => item.id === reply.outbound_id));
      const supplierState = hasReply ? 'Ответ получен' : latestSend ? sendLabels[latestSend.status] : 'Запрос готов';
      summary.append(node('strong', job.position_name), node('span', `${job.supplier ? supplierState+' · Email · '+(latestSend?.recipient || job.supplier.email) : labels[job.status] || 'Неизвестное состояние'} · Позиций: ${job.positions.length}`));
      group.append(summary);
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
                autoFeedback.textContent = 'Запрос сохранён. Проверка сайтов и результаты отправки — в журнале ниже.';
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
    renderCompanies(report, jobs, outbox, replies, jobNodes, open, mailNodes);
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
    const outgoing = outbox.filter(item => company.correspondence_ids ? company.correspondence_ids.includes(item.id) : ids.has(item.draft_job_id) && (!company.correspondence_recipient || item.recipient === company.correspondence_recipient)).sort((a,b) => b.created_at-a.created_at);
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
  function renderCompanies(report, jobs, outbox, replies, jobNodes, expanded, mailNodes = new Map()) {
    const companies = companyList(report, jobs, outbox, replies);
    const positionMap = new Map([...jobs.flatMap(j => j.positions), ...(report?.positions || [])].map(p => [p.position_key,p]));
    const used = new Set();
    const companyStates = {...sendLabels, sent:'Отправлено', answered:'Отправлено', prepared:'Готово к отправке', contact_required:'Нужен контакт'};
    companies.forEach(company => {
      const card = node('details', null, 'buyer-company'); card.dataset.key = `company-${company.id}`; card.open = expanded.has(card.dataset.key);
      const positions = company.position_keys.map(key => positionMap.get(key)).filter(Boolean);
      const prices = company.prices || [], priced = prices.some(p => p.price_kopecks != null);
      const thread = correspondence(company, outbox, replies);
      const attention = thread.attention || ['blocked','uncertain','contact_required'].includes(company.status) || prices.some(p => p.state === 'review') || !!report?.version_note;
      card.dataset.priced = String(priced); card.dataset.answered = String(!!thread.answer); card.dataset.sent = String(thread.sent); card.dataset.attention = String(attention);
      card.dataset.search = [company.name,...thread.outgoing.map(item => item.recipient),...company.contacts.map(c => c.address),...positions.map(p => p.name)].join(' ').toLocaleLowerCase('ru-RU');
      const summary = node('summary', null, 'buyer-company-summary');
      const media = node('span', null, 'buyer-company-media'); media.setAttribute('aria-hidden','true');
      media.append(node('span', (company.name || '?').split(/\s+/).slice(0,2).map(s => s[0]).join('').toUpperCase()));
      if (company.image_url?.startsWith(`/api/tenders/${encodeURIComponent(root.dataset.buyer)}/buyer/image/`)) {
        const img = node('img'); img.src = company.image_url; img.alt = ''; img.loading = 'lazy'; img.width = 72; img.height = 72;
        img.addEventListener('error', () => img.remove()); media.append(img);
      }
      const identity = node('span', null, 'buyer-company-identity');
      identity.append(node('strong', company.name));
      const recipient = thread.latest?.recipient || company.contacts[0]?.address || 'Контакт пока не найден';
      if (recipient !== company.name) identity.append(node('span', recipient, 'buyer-company-contact'));
      identity.append(node('span', `${company.previous ? 'Ранее по тендеру · ' : ''}${company.position_keys.length} поз. · ${positions.slice(0,2).map(p => p.name).join(' · ') || 'Состав запроса уточняется'}`, 'buyer-company-scope'));
      const activity = node('span',null,'buyer-company-activity');
      const sendState = thread.latest?.status || company.status;
      activity.append(node('span',companyStates[sendState] || 'Готовим обращение',`buyer-company-state buyer-state-${sendState}`));
      if (thread.latest) activity.append(node('span',shortDate(thread.latest.status === 'sent' ? thread.latest.updated_at : thread.latest.created_at),'buyer-company-date'));
      if (thread.outgoing.length > 1) activity.append(node('span',`Обращений: ${thread.outgoing.length}`,'buyer-company-date'));
      const offer = node('span', null, 'buyer-company-offer');
      if (thread.answer) {
        offer.append(node('span',`Ответ · ${shortDate(thread.answer.received_at)}`,'buyer-reply-label'));
        const excerpt = thread.answer.raw_text?.trim() || 'Получен ответ без текста';
        offer.append(node('span',excerpt.length > 240 ? excerpt.slice(0,237).trimEnd()+'…' : excerpt,'buyer-reply-preview'));
      } else {
        const waiting = thread.blocked ? 'Не удалось проверить ответы' : thread.checking ? 'Проверяем ответы…' : thread.sent ? 'Ожидаем ответ' : thread.latest?.status === 'uncertain' ? 'Ответ пока не отслеживается' : 'Ответа пока нет';
        offer.append(node('span',waiting,thread.blocked ? 'buyer-inbox-warning' : 'buyer-reply-empty'));
      }
      if (thread.blocked) offer.append(node('span',thread.answer ? 'Новые ответы не проверены' : 'Откройте переписку, чтобы повторить проверку','buyer-inbox-warning'));
      else if (!thread.answer && thread.checkedAt) offer.append(node('span',`Проверено ${shortDate(thread.checkedAt)}`,'buyer-company-date'));
      const price = prices.find(p => p.origin === 'reply' && p.price_kopecks != null) || prices.find(p => p.price_kopecks != null);
      if (price) {
        offer.append(node('strong', `${(price.price_kopecks/100).toLocaleString('ru-RU')} ₽ / ${price.unit || 'ед.'}`));
        if (company.position_keys.length > 1) offer.append(node('span', positionMap.get(price.position_key)?.name || 'Позиция сохранённого запроса', 'buyer-offer-position'));
        offer.append(node('span', `${price.origin === 'website' ? 'Цена с сайта' : 'Из ответа'}${price.state === 'review' ? ' · уточнить' : price.origin === 'website' ? ' · подтвердить' : ''}${prices.length > 1 ? ` · ещё ${prices.length-1}` : ''}`));
      }
      summary.append(media, identity, activity, offer); card.append(summary);
      const body = node('div', null, 'buyer-company-body');
      if (thread.answer) {
        const answer = node('section',null,'buyer-latest-reply');
        answer.append(node('h3','Последний ответ'),node('p',`${thread.answer.sender || thread.latest?.recipient || company.name} · ${shortDate(thread.answer.received_at)}`,'buyer-company-date'),node('p',thread.answer.raw_text || 'Ответ без текста','buyer-body'));
        body.append(answer);
      }
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
      card.append(body); list.append(card);
    });
    const remaining = [...jobNodes.entries()].filter(([id]) => !used.has(id));
    if (remaining.length) {
      const archive = node('details',null,'buyer-archive'); archive.dataset.key = 'previous'; archive.open = expanded.has('previous');
      archive.append(node('summary',`${report ? 'Другие обращения по тендеру' : 'Обращения без выбранной компании'} (${remaining.length})`));
      remaining.forEach(([,group]) => archive.append(group)); list.append(archive);
    }
    if (!companies.length && !remaining.length) {
      const empty = node('div',null,'buyer-empty');
      empty.append(node('h3', report?.status === 'searching' ? 'Ищем подходящие компании' : report ? 'Компании пока не найдены' : 'От сметы — к предложениям'));
      empty.append(node('p', report ? 'Результаты проверок появятся здесь. Если источник недоступен, можно повторить подбор.' : 'Материалы сгруппируем по поставщикам, работы — по исполнителям. Одна компания получит общий запрос по подходящим позициям.'));
      list.append(empty);
    }
    const toolbar = root.querySelector('[data-buyer-toolbar]'); if (toolbar) toolbar.hidden = !companies.length;
    const mailNote = root.querySelector('[data-buyer-mail-note]'); if (mailNote) mailNote.hidden = !outbox.some(item => item.status === 'sent');
    applyFilter();
  }
  function applyFilter() {
    const cards = Array.from(list.querySelectorAll('.buyer-company'));
    const query = (find?.value || '').trim().toLocaleLowerCase('ru-RU');
    let visible = 0;
    cards.forEach(card => { card.hidden = !(filter === 'all' || card.dataset[filter] === 'true') || !card.dataset.search.includes(query); if (!card.hidden) visible++; });
    root.querySelectorAll?.('[data-buyer-filter]').forEach(button => {
      const kind = button.dataset.buyerFilter; button.setAttribute('aria-pressed', String(kind === filter));
      button.querySelector('[data-buyer-count]').textContent = cards.filter(card => kind === 'all' || card.dataset[kind] === 'true').length;
    });
    const empty = root.querySelector('[data-buyer-no-results]'); if (empty) empty.hidden = !cards.length || visible > 0;
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
  find?.addEventListener('input', applyFilter);
  root.querySelectorAll?.('[data-buyer-filter]').forEach(button => button.addEventListener('click', () => { filter = button.dataset.buyerFilter; applyFilter(); }));
  document.addEventListener('change', syncSelection);
  // Existing bulk actions (and the price search) update this shared count
  // without firing checkbox change events. Keep displayed and submitted scope equal.
  const selectionCount = document.querySelector('#agentSelectedCount');
  if (selectionCount) new MutationObserver(syncSelection).observe(selectionCount, {childList: true, characterData: true, subtree: true});
  syncSelection();
  load();
  setInterval(() => { if (!document.hidden && !busy) load(); }, 15000);
})();
