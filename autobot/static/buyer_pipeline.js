/* A read-only view of the current estimate; chat and search use existing actions. */
(() => {
  'use strict';
  const stages = [
    ['candidates', 'Найдены варианты'], ['comparable', 'Цена подходит'],
    ['contacts', 'Есть контакты'], ['sent', 'Запрос отправлен'],
    ['replied', 'Получен ответ'], ['confirmed', 'Цена из ответа']
  ];
  const descriptions = {
    candidates: 'Сайты и предложения, найденные для позиции. Совпадение товара ещё нужно проверить.',
    comparable: 'Цены с подходящими характеристиками и единицей. Цена с сайта ещё не подтверждена поставщиком.',
    contacts: 'Контакты с проверенных страниц компаний или из подтверждённой отправки.',
    sent: 'Есть подтверждение отправки хотя бы одной компании. Очередь сюда не входит.',
    replied: 'Компания ответила. Это может быть вопрос или предложение без цены.',
    confirmed: 'В ответе совпадают товар, характеристики и единица; указаны цена, валюта и НДС.'
  };
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function button(text, action, className = 'buyer-pipeline-link') {
    const element = node('button', text, className); element.type = 'button';
    element.addEventListener('click', action); return element;
  }
  function link(url, label) {
    try {
      const parsed = new URL(url);
      if (!['https:', 'http:'].includes(parsed.protocol)) return null;
      const element = node('a', label || parsed.hostname); element.href = parsed.href;
      element.target = '_blank'; element.rel = 'noopener noreferrer'; return element;
    } catch (_) { return null; }
  }
  function ago(stamp) {
    const minutes = Math.max(0, Math.floor((Date.now() / 1000 - stamp) / 60));
    if (minutes < 1) return 'только что';
    if (minutes < 60) return `${minutes} мин назад`;
    if (minutes < 1440) return `${Math.floor(minutes / 60)} ч ${minutes % 60} мин назад`;
    return `${Math.floor(minutes / 1440)} д ${Math.floor(minutes % 1440 / 60)} ч назад`;
  }
  const amount = offer => offer.price_kopecks == null ? 'Цена не указана' :
    `${(offer.price_kopecks / 100).toLocaleString('ru-RU')} ₽ / ${offer.unit || 'ед.'}`;

  window.createBuyerPipeline = (host, {onSearch, onChat}) => {
    if (!host) return null;
    let data = null, selected = '', signature = '', limit = 20, search = '';
    const expanded = new Set();
    const note = node('p', 'Загружаем состояние позиций…', 'buyer-pipeline-note');
    note.setAttribute('role', 'status');
    const steps = node('div', null, 'buyer-pipeline-stages');
    steps.setAttribute('role', 'group'); steps.setAttribute('aria-label', 'Этапы снабжения по позициям');
    const stageButtons = stages.map(([key, label]) => {
      const control = button('', () => choose(selected === key ? '' : key), 'buyer-pipeline-stage');
      control.title = descriptions[key]; control.dataset.stage = key;
      control.setAttribute('aria-pressed', 'false'); control.setAttribute('aria-controls', 'buyerPipelinePositions');
      control.append(node('strong', '—'), node('span', label)); steps.append(control); return control;
    });
    const footer = node('div', null, 'buyer-pipeline-footer');
    const scope = node('span');
    const toggle = button('Все позиции', () => choose(selected === 'all' ? '' : 'all'));
    toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-controls', 'buyerPipelinePositions');
    footer.append(scope, toggle);
    const panel = node('section', null, 'buyer-pipeline-positions'); panel.id = 'buyerPipelinePositions'; panel.hidden = true;
    const toolbar = node('div', null, 'buyer-pipeline-toolbar');
    const title = node('h3'), input = node('input'); input.type = 'search'; input.placeholder = 'Найти позицию';
    input.setAttribute('aria-label', 'Найти позицию в цепочке снабжения');
    toolbar.append(title, input, button('Свернуть', () => choose('')));
    const hint = node('p', null, 'buyer-pipeline-hint');
    const rows = node('div', null, 'buyer-pipeline-rows');
    const more = button('Показать ещё', () => { limit += 20; renderRows(); }, 'btn ghost');
    panel.append(toolbar, hint, rows, more);
    host.replaceChildren(note, steps, footer, panel);

    function choose(key) {
      selected = key; limit = 20; renderRows();
      if (!key) toggle.focus({preventScroll: true});
    }
    function chat(message) {
      if (!onChat(message.id || message.outbox_id)) {
        note.hidden = false;
        note.textContent = 'Переписка пока не загрузилась. Нажмите «Обновить» и повторите.';
      }
    }
    function evidence(position) {
      const block = node('div', null, 'buyer-pipeline-evidence');
      for (const offer of position.offers) {
        const item = node('div', null, 'buyer-pipeline-offer');
        const origin = offer.origin === 'reply' ? 'Ответ поставщика' : 'Сайт компании';
        item.append(node('strong', amount(offer)), node('span', `${origin} · ${offer.supplier || 'Источник'}`));
        item.append(node('span', offer.comparable ? 'Подходит для сравнения' : 'Требует уточнения', offer.comparable ? 'buyer-pipeline-ok' : 'buyer-pipeline-warn'));
        if (offer.reason) item.append(node('p', offer.reason));
        if (offer.evidence) item.append(node('blockquote', offer.evidence));
        const terms = [offer.vat, offer.availability, offer.delivery].filter(Boolean);
        if (terms.length) item.append(node('p', terms.join(' · ')));
        const source = link(offer.url, 'Открыть источник'); if (source) item.append(source);
        if (offer.outbox_id) item.append(button('Открыть ответ', () => chat(offer)));
        block.append(item);
      }
      if (position.contacts.length) {
        const contacts = node('div', null, 'buyer-pipeline-contacts');
        contacts.append(node('strong', 'Контакты'));
        for (const contact of position.contacts) {
          const line = node('p', `${contact.company} · ${contact.address}`);
          const source = link(contact.source_url, 'Сайт'); if (source) line.append(' · ', source);
          contacts.append(line);
        }
        block.append(contacts);
      }
      return block;
    }
    function renderRows() {
      panel.hidden = !selected;
      toggle.textContent = selected === 'all' ? 'Свернуть позиции' : 'Все позиции';
      toggle.setAttribute('aria-expanded', String(!!selected));
      stageButtons.forEach(control => control.setAttribute('aria-pressed', String(control.dataset.stage === selected)));
      if (!selected || !data) return;
      const matches = data.positions.filter(p => (selected === 'all' || p.flags[selected]) &&
        `${p.name} ${p.item_no}`.toLocaleLowerCase('ru-RU').includes(search));
      title.textContent = `${stages.find(([key]) => key === selected)?.[1] || 'Все позиции'} · ${matches.length}`;
      hint.textContent = descriptions[selected] || 'Состояние каждой строки актуальной сметы. Расчётные строки показаны отдельно от закупки.';
      rows.replaceChildren();
      if (!matches.length) rows.append(node('p', search ? 'По этому запросу позиций нет.' : 'На этом этапе пока нет позиций.', 'buyer-pipeline-empty'));
      for (const position of matches.slice(0, limit)) {
        const row = node('article', null, 'buyer-pipeline-row'); row.dataset.position = position.position_key;
        const identity = node('div', null, 'buyer-pipeline-identity');
        identity.append(node('strong', position.name));
        const quantity = position.quantity == null ? 'Объём не указан' : `${Number(position.quantity).toLocaleString('ru-RU')} ${position.unit}`;
        identity.append(node('span', [position.item_no && `№ ${position.item_no}`, quantity, position.type_label].filter(Boolean).join(' · ')));
        const state = node('div', null, 'buyer-pipeline-state'); state.dataset.state = position.state;
        state.append(node('strong', position.label), node('p', position.reason));
        if (position.updated_at) { const time = node('time', ago(position.updated_at)); time.dataset.stamp = position.updated_at; time.dateTime = new Date(position.updated_at * 1000).toISOString(); time.title = new Date(position.updated_at * 1000).toLocaleString('ru-RU'); state.append(time); }
        const actions = node('div', null, 'buyer-pipeline-actions');
        const offer = position.offers.find(o => o.comparable && o.origin === 'reply') || position.offers.find(o => o.comparable);
        if (offer) actions.append(node('b', amount(offer)), node('span', offer.origin === 'reply' ? 'Из ответа' : 'С сайта'));
        const lastMessage = position.messages.at(-1);
        if (lastMessage) actions.append(button('Переписка', () => chat(lastMessage)));
        if (position.eligible && !position.flags.confirmed) {
          const find = button(position.state === 'searching' ? 'Поиск идёт…' : 'Подобрать поставщика', async () => {
            find.disabled = true; find.textContent = 'Запускаем подбор…';
            try {
              const result = await onSearch(position.position_key);
              if (result?.error) { state.querySelector('p').textContent = result.error; }
            } finally { find.disabled = false; find.textContent = 'Подобрать поставщика'; }
          });
          find.disabled = position.state === 'searching'; actions.append(find);
        }
        row.append(identity, state, actions);
        if (position.offers.length || position.contacts.length) {
          const details = node('details'); details.dataset.key = position.position_key; details.open = expanded.has(position.position_key);
          details.append(node('summary', `Предложения: ${position.offers.length} · контакты: ${position.contacts.length}`), evidence(position));
          details.addEventListener('toggle', () => { if (details.open) expanded.add(position.position_key); else expanded.delete(position.position_key); });
          row.append(details);
        }
        rows.append(row);
      }
      more.hidden = matches.length <= limit;
      more.textContent = `Показать ещё · осталось ${Math.max(0, matches.length - limit)}`;
    }
    input.addEventListener('input', () => { search = input.value.trim().toLocaleLowerCase('ru-RU'); limit = 20; renderRows(); });
    return {
      update(next) {
        data = next; note.hidden = true;
        const {summary} = next;
        scope.textContent = `${summary.eligible} поз. для закупки${summary.excluded ? ` · ${summary.excluded} вне подбора` : ''}`;
        stageButtons.forEach(control => { control.querySelector('strong').textContent = summary[control.dataset.stage]; });
        const nextSignature = JSON.stringify(next.positions);
        if (signature !== nextSignature) { signature = nextSignature; renderRows(); }
        else rows.querySelectorAll('time[data-stamp]').forEach(time => { time.textContent = ago(Number(time.dataset.stamp)); });
      },
      fail(message) { note.hidden = false; note.textContent = message + (data ? ' Показаны последние загруженные данные.' : ' Чаты доступны ниже.'); }
    };
  };
})();
