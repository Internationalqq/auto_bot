(() => {
  'use strict';
  const dialog = document.getElementById('searchProfileDialog');
  if (!dialog) return;
  const form = document.getElementById('searchProfileForm');
  const select = document.getElementById('searchProfileSelect');
  const status = document.getElementById('searchProfileStatus');
  const reload = document.getElementById('reloadSearchProfiles');
  const save = document.getElementById('saveSearchProfile');
  const run = document.getElementById('runProfileSearch');
  const create = document.getElementById('newSearchProfile');
  const field = name => document.getElementById('searchProfile' + name);
  let catalogue = null, selectedId = '', busy = false;
  const storageKey = 'autobot.search-profile.v1';

  function message(text, error = false) {
    status.textContent = text;
    status.classList.toggle('is-error', error);
  }
  function lock(value) {
    busy = value;
    [select, save, run, create].forEach(el => { el.disabled = value || !catalogue; });
    form.querySelectorAll('input, textarea').forEach(el => { el.disabled = value || !catalogue; });
  }
  function remember() { try { localStorage.setItem(storageKey, selectedId); } catch (_) {} }
  function limitsSummary() {
    field('LimitsSummary').textContent = '· ' + field('Days').value + ' дней · до ' + field('Count').value + ' закупок';
  }
  function options() {
    select.replaceChildren();
    catalogue.profiles.forEach(profile => {
      const option = document.createElement('option');
      option.value = profile.id; option.textContent = profile.name;
      select.append(option);
    });
    if (!catalogue.profiles.some(profile => profile.id === selectedId)) {
      const option = document.createElement('option');
      option.value = selectedId; option.textContent = 'Новый профиль · не сохранён';
      select.append(option);
    }
    select.value = selectedId;
  }
  function fill(profile) {
    selectedId = profile.id;
    const data = profile.filters;
    field('Name').value = profile.name;
    field('Regions').value = data.regions.join('\n');
    field('Keywords').value = data.keywords.join('\n');
    field('Min').value = data.price_min_kopecks === null ? '' : (data.price_min_kopecks / 100).toFixed(2);
    field('Max').value = data.price_max_kopecks === null ? '' : (data.price_max_kopecks / 100).toFixed(2);
    field('Days').value = data.days_back; field('Pages').value = data.max_pages; field('Count').value = data.max_tenders;
    options(); limitsSummary();
  }
  async function load() {
    lock(true); message('Загружаем профили…'); reload.hidden = true;
    try {
      const response = await fetch('/api/search-profiles', {cache: 'no-store'});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || 'Не удалось прочитать профили.');
      const firstLoad = !catalogue;
      catalogue = data;
      if (firstLoad) {
        let remembered = ''; try { remembered = localStorage.getItem(storageKey) || ''; } catch (_) {}
        fill(catalogue.profiles.find(profile => profile.id === remembered) || catalogue.profiles[0]);
      } else options();
      message('Условия можно изменить для одного запуска или сохранить в профиле.');
    } catch (error) { message(error.message, true); reload.hidden = false; }
    finally { lock(false); }
  }
  function money(input) {
    const value = input.value.replace(/[\s\u00a0]/g, '').replace(',', '.');
    if (!value) return null;
    if (!/^\d+(?:\.\d{1,2})?$/.test(value)) throw new Error('Сумма: укажите рубли и не более двух знаков копеек.');
    const [rubles, cents = ''] = value.split('.');
    const result = Number(rubles) * 100 + Number(cents.padEnd(2, '0'));
    if (!Number.isSafeInteger(result) || result > 100000000000000) throw new Error('Граница суммы не может превышать 1 трлн рублей.');
    return result;
  }
  function read() {
    const numbers = ['Days', 'Pages', 'Count'];
    if (numbers.some(name => !field(name).checkValidity())) {
      form.querySelector('.search-profile-limits').open = true;
    }
    if (!form.reportValidity()) return null;
    const terms = name => field(name).value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    const data = {regions: terms('Regions'), keywords: terms('Keywords'),
      price_min_kopecks: money(field('Min')), price_max_kopecks: money(field('Max')),
      days_back: Number(field('Days').value), max_pages: Number(field('Pages').value), max_tenders: Number(field('Count').value),
      needed_stage: 'Подача заявок'};
    if (data.price_min_kopecks !== null && data.price_max_kopecks !== null && data.price_min_kopecks > data.price_max_kopecks)
      throw new Error('Минимальная сумма не может превышать максимальную.');
    return data;
  }
  document.querySelector('[data-global-action="start-search"]')?.addEventListener('click', () => {
    if (!dialog.open) dialog.showModal();
    if (!catalogue) load();
  });
  dialog.querySelector('[data-search-profile-close]').addEventListener('click', () => { if (!busy) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  reload.addEventListener('click', load);
  select.addEventListener('change', () => {
    const profile = catalogue.profiles.find(row => row.id === select.value);
    if (profile) { fill(profile); remember(); message('Профиль загружен.'); }
  });
  create.addEventListener('click', () => {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    selectedId = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    field('Name').value = ''; options(); field('Name').focus();
    message('Назовите новый профиль. Текущие условия скопированы в форму.');
  });
  ['Days', 'Count'].forEach(name => field(name).addEventListener('input', limitsSummary));
  form.addEventListener('invalid', event => {
    const limits = event.target.closest('.search-profile-limits');
    if (limits) limits.open = true;
  }, true);
  save.addEventListener('click', async () => {
    if (busy) return;
    try {
      const filters = read(); if (!filters) return;
      const profile = {id: selectedId, name: field('Name').value.trim(), filters};
      lock(true); message('Сохраняем профиль…');
      const response = await fetch('/api/search-profiles', {method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({revision: catalogue.revision, profile})});
      const data = await response.json();
      if (!response.ok || !data.ok) { reload.hidden = response.status !== 409; throw new Error(data.message || 'Не удалось сохранить профиль.'); }
      catalogue = data; options(); remember(); reload.hidden = true;
      message('Профиль сохранён. Его можно выбрать при следующем поиске.');
    } catch (error) { message(error.message, true); }
    finally { lock(false); }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    try {
      const filters = read(); if (!filters) return;
      lock(true); message('Запускаем поиск…');
      const started = await window.startCatalogueSearch('fresh', filters);
      if (started) { remember(); dialog.close(); }
      else message('Сейчас выполняется другая задача. Дождитесь её завершения.', true);
    } catch (error) { message(error.message, true); }
    finally { lock(false); }
  });
})();
