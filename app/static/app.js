'use strict';

const form = document.querySelector('#campaign-form');
const createButton = document.querySelector('#create-button');
const geo = document.querySelector('#geo');
const offerId = document.querySelector('#offer-id');
const search = document.querySelector('#offer-search');
const results = document.querySelector('#offer-results');
const searchStatus = document.querySelector('#search-status');
const result = document.querySelector('#result');
const offerStatus = document.querySelector('#offer-status');
const configured = form.dataset.keitaroConfigured === 'true';
let requestId = crypto.randomUUID();
let busy = false;
let searchController;
let searchTimer;
let offerController;
let offerTimer;
let verifiedOfferId = null;
const labels = {completed: 'Создана', pending: 'В процессе / проверить',
  failed: 'Ошибка', cleanup_failed: 'Проверьте удаление', uncertain: 'Результат неизвестен'};

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let data;
  try { data = await response.json(); } catch { throw new Error('Сервер вернул некорректный ответ.'); }
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map(item => `${item.loc.slice(1).join('.')}: ${item.msg}`).join('; ')
      : data.detail;
    throw new Error(detail || 'Не удалось выполнить запрос.');
  }
  return data;
}

function safeLink(url, text) {
  try {
    const parsed = new URL(url);
    if (!['http:', 'https:'].includes(parsed.protocol)) return document.createTextNode(text);
    const link = document.createElement('a');
    link.href = parsed.href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = text;
    return link;
  } catch { return document.createTextNode(text); }
}

function updatePreview() {
  document.querySelector('#preview-geo').textContent = geo.value.trim().toUpperCase() || 'страны';
  document.querySelector('#preview-offer').textContent = offerId.value ? `Оффер #${offerId.value}` : 'Оффер не выбран';
}

form.addEventListener('input', () => { if (!busy) requestId = crypto.randomUUID(); updatePreview(); });

function syncCreateButton() {
  createButton.disabled = busy || !configured || verifiedOfferId !== Number(offerId.value);
}

function cancelOfferCheck() {
  clearTimeout(offerTimer);
  offerController?.abort();
  verifiedOfferId = null;
  offerId.setCustomValidity('');
  offerId.removeAttribute('aria-invalid');
  offerStatus.textContent = '';
  syncCreateButton();
}

offerId.addEventListener('input', () => {
  // Manual ID entry invalidates the name and any pending autocomplete results.
  clearTimeout(searchTimer);
  searchController?.abort();
  search.value = '';
  results.replaceChildren();
  results.hidden = true;
  searchStatus.textContent = '';
  cancelOfferCheck();
  if (!offerId.value) return;
  if (!offerId.validity.valid || !Number.isInteger(Number(offerId.value))) {
    offerStatus.textContent = 'Введите целое положительное ID оффера.';
    offerId.setAttribute('aria-invalid', 'true');
    return;
  }
  const id = Number(offerId.value);
  const controller = new AbortController();
  offerController = controller;
  offerStatus.textContent = 'Проверяем оффер…';
  offerId.setCustomValidity('Дождитесь проверки оффера.');
  offerTimer = setTimeout(async () => {
    try {
      const offer = await api(`/api/offers/${id}`, {signal: controller.signal});
      if (controller.signal.aborted || Number(offerId.value) !== id) return;
      verifiedOfferId = id;
      search.value = offer.name;
      offerId.setCustomValidity('');
      offerId.removeAttribute('aria-invalid');
      offerStatus.textContent = `Оффер #${id} найден.`;
    } catch (error) {
      if (controller.signal.aborted || Number(offerId.value) !== id) return;
      offerId.setCustomValidity(error.message);
      offerId.setAttribute('aria-invalid', 'true');
      offerStatus.textContent = error.message;
    } finally {
      if (!controller.signal.aborted) syncCreateButton();
    }
  }, 350);
});

search.addEventListener('input', () => {
  cancelOfferCheck();
  offerId.value = '';
  updatePreview();
  clearTimeout(searchTimer);
  searchController?.abort();
  results.replaceChildren();
  results.hidden = true;
  searchStatus.textContent = '';
  const query = search.value.trim();
  if (!query) return;
  searchController = new AbortController();
  const controller = searchController;
  searchTimer = setTimeout(async () => {
    searchStatus.textContent = 'Ищем офферы…';
    try {
      const offers = await api(`/api/offers?q=${encodeURIComponent(query)}`, {signal: controller.signal});
      if (controller.signal.aborted) return;
      searchStatus.textContent = offers.length ? 'Выберите оффер из списка.' : 'Офферы не найдены.';
      results.replaceChildren();
      for (const offer of offers) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'offer-option';
        button.textContent = `#${offer.id} · ${offer.name}`;
        button.addEventListener('click', () => {
          cancelOfferCheck();
          searchController?.abort();
          offerId.value = offer.id;
          search.value = offer.name;
          verifiedOfferId = Number(offer.id);
          offerStatus.textContent = `Оффер #${offer.id} выбран.`;
          syncCreateButton();
          requestId = crypto.randomUUID();
          results.hidden = true;
          searchStatus.textContent = `Выбран оффер #${offer.id}`;
          updatePreview();
        });
        results.append(button);
      }
      results.hidden = !offers.length;
    } catch (error) { if (!controller.signal.aborted) searchStatus.textContent = error.message; }
  }, 300);
});

async function loadHistory() {
  if (document.querySelector('.history').hidden) return;
  const message = document.querySelector('#history-message');
  try {
    const operations = await api('/api/operations');
    message.textContent = operations.length ? 'Показаны последние 20 операций.' : 'Созданные кампании появятся здесь.';
    const tbody = document.querySelector('#history-body');
    tbody.replaceChildren();
    for (const operation of operations) {
      const row = document.createElement('tr');
      const name = row.insertCell();
      name.textContent = operation.name;
      const date = document.createElement('span');
      date.className = 'row-details';
      date.textContent = new Date(operation.created_at).toLocaleString('ru-RU');
      name.append(date);
      row.insertCell().textContent = `${operation.geo} / #${operation.offer_id}`;
      const status = row.insertCell();
      const badge = document.createElement('span');
      badge.className = `badge ${operation.status}`;
      badge.textContent = labels[operation.status] || operation.status;
      status.append(badge);
      if (operation.error || operation.status !== 'completed') {
        const details = document.createElement('span');
        details.className = 'row-details';
        details.textContent = [operation.error, `alias: ${operation.alias}`].filter(Boolean).join(' ');
        status.append(details);
      }
      const campaign = row.insertCell();
      campaign.textContent = operation.campaign_id ? `#${operation.campaign_id}` : '—';
      if (operation.status === 'completed' && operation.campaign_url) {
        campaign.replaceChildren(safeLink(operation.campaign_url, `#${operation.campaign_id} ↗`));
      }
      tbody.append(row);
    }
  } catch (error) { message.textContent = error.message; }
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !configured || verifiedOfferId !== Number(offerId.value)
      || !form.reportValidity()) return;
  busy = true;
  for (const element of form.elements) element.disabled = true;
  createButton.textContent = 'Создаём кампанию…';
  result.hidden = true;
  try {
    const operation = await api('/api/campaigns', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: document.querySelector('#campaign-name').value,
        geo: geo.value, offer_id: Number(offerId.value), request_id: requestId}),
    });
    result.className = 'result';
    result.textContent = `Кампания #${operation.campaign_id} создана. Оба потока настроены, кампания включена.`;
    // Retain UUID until input changes: a second click returns the same operation.
  } catch (error) {
    result.className = 'result error';
    result.textContent = `${error.message} Перед повтором проверьте кампанию в Keitaro. Для нового запроса измените параметры формы.`;
  } finally {
    busy = false;
    for (const element of form.elements) element.disabled = false;
    syncCreateButton();
    createButton.textContent = 'Создать кампанию ↗';
    result.hidden = false;
    result.focus();
    await loadHistory();
  }
});

document.querySelector('#refresh-history').addEventListener('click', loadHistory);
updatePreview();
syncCreateButton();
loadHistory();
