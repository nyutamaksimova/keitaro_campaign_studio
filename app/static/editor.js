'use strict';

const root = document.querySelector('#editor');
const campaignId = Number(root.dataset.campaignId);
const endpoint = `/api/editor/campaigns/${campaignId}`;
const streamsNode = document.querySelector('#streams');
const message = document.querySelector('#editor-message');
const draftStatus = document.querySelector('#draft-status');
const publishButton = document.querySelector('#publish');
const discardButton = document.querySelector('#discard');
const refreshButton = document.querySelector('#refresh-remote');
const campaignToggle = document.querySelector('#campaign-toggle');
let state = null;
let busy = false;
let localUnsaved = false;
let pendingShareKey = null;
let pageVersion = 0;
const searches = new Set();

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function button(text, handler, className = 'secondary') {
  const element = node('button', text, className);
  element.type = 'button';
  element.addEventListener('click', handler);
  return element;
}

function notify(text, error = false) {
  message.textContent = text;
  message.hidden = !text;
  message.className = error ? 'notice editor-message-error' : 'notice';
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let data;
  try { data = await response.json(); } catch { throw new Error('Сервер вернул некорректный ответ.'); }
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail
      : Array.isArray(data.detail) ? data.detail.map(item => item.msg).join('; ') : 'Ошибка запроса.';
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return data;
}

function updateControls() {
  for (const input of streamsNode.querySelectorAll('.share-input')) {
    const pending = localUnsaved && input.dataset.offerKey === pendingShareKey;
    input.classList.toggle('pending-input', pending);
    input.closest('tr').classList.toggle('pending-input-row', pending);
    const label = input.parentElement.querySelector('.input-pending-label');
    if (label) label.hidden = !pending;
  }
  document.querySelector('#draft-legend').hidden = !localUnsaved && !state?.dirty;
  for (const element of streamsNode.querySelectorAll('[data-offer-key]')) {
    element.disabled = busy || element.dataset.permanentlyDisabled === 'true'
      || (localUnsaved && element.dataset.offerKey !== pendingShareKey);
  }
  publishButton.disabled = !state || busy || localUnsaved || !state.dirty
    || state.conflicts.length > 0 || state.errors.length > 0
    || state.status === 'uncertain' || Boolean(state.publish_result.uncertain);
  discardButton.disabled = !state || busy || (!state.dirty && !state.conflicts.length && !localUnsaved);
  refreshButton.disabled = !state || busy || localUnsaved;
  campaignToggle.disabled = !state || busy || localUnsaved
    || !['active', 'disabled'].includes(state.draft.campaign.state)
    || state.conflicts.some(c => c.kind === 'campaign');
  document.querySelector('#reload-draft').disabled = busy;
  draftStatus.textContent = busy ? 'Выполняем действие…'
    : localUnsaved ? 'Изменение доли не сохранено. Исправьте значение или отмените изменения.'
    : !state ? 'Загружаем черновик…'
    : state.conflicts.length ? `Нужно разрешить конфликтов: ${state.conflicts.length}`
    : state.dirty ? 'Черновик сохранён · есть неотправленные изменения'
    : 'Изменения не ожидают отправки';
}

async function loadDraft() {
  if (busy) return;
  if (localUnsaved && !window.confirm('Отбросить несохранённый ввод и загрузить черновик из базы?')) return;
  busy = true;
  updateControls();
  try {
    state = await api(endpoint);
    localUnsaved = false;
    pendingShareKey = null;
    render();
    notify(state.error || '', Boolean(state.error));
  } catch (error) { notify(error.message, true); }
  finally { busy = false; updateControls(); }
}

async function act(action, values = {}) {
  if (busy || !state) return;
  if (localUnsaved && !['pin', 'discard'].includes(action)) {
    notify('Сначала сохраните исправленную долю или отмените изменения.', true);
    return;
  }
  if (localUnsaved && action === 'pin'
      && `${values.stream_id}:${values.offer_id}` !== pendingShareKey) {
    notify('Сначала сохраните долю редактируемого оффера или отмените изменения.', true);
    return;
  }
  busy = true;
  updateControls();
  for (const element of streamsNode.querySelectorAll('button,input')) element.disabled = true;
  notify('');
  try {
    state = await api(`${endpoint}/actions`, {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({revision: state.revision, action, ...values})});
    localUnsaved = false;
    pendingShareKey = null;
    render();
    const texts = {publish: 'Изменения подтверждены в Keitaro.',
      discard: 'Локальные изменения отменены.',
      refresh: state.changes.length ? 'Данные Keitaro подгружены и объединены с черновиком.'
        : 'Проверка завершена. Новых изменений в Keitaro нет.'};
    notify(texts[action] || '');
  } catch (error) {
    if (action === 'publish') {
      try { state = await api(endpoint); render(); } catch { /* Keep the last visible draft. */ }
    }
    if (action === 'pin') {
      localUnsaved = true;
      pendingShareKey = `${values.stream_id}:${values.offer_id}`;
    }
    notify(error.message, true);
  } finally {
    busy = false;
    // Restores disabled rules (disabled offers, missing streams, unresolved conflicts).
    for (const element of streamsNode.querySelectorAll('button,input')) {
      element.disabled = element.dataset.permanentlyDisabled === 'true';
    }
    updateControls();
  }
}

function render() {
  pageVersion++;
  for (const controller of searches) controller.abort();
  searches.clear();
  document.querySelector('#campaign-title').textContent = state.draft.campaign.name;
  document.title = `${state.draft.campaign.name} · Редактор Keitaro`;
  const desiredState = state.draft.campaign.state;
  const changedState = desiredState !== state.base.campaign.state;
  const statusLabel = document.querySelector('#campaign-state');
  const stateText = {active: 'Включена', disabled: 'Выключена', deleted: 'Удалена'}[desiredState] || 'Неизвестное состояние';
  statusLabel.textContent = changedState ? `После отправки: ${stateText}` : stateText;
  statusLabel.classList.toggle('campaign-state-changed', changedState);
  campaignToggle.textContent = desiredState === 'active' ? 'Выключить кампанию' : 'Включить кампанию';
  renderChanges();
  renderConflicts();
  streamsNode.replaceChildren();
  for (const stream of state.draft.streams) renderStream(stream);
  updateControls();
}

function renderChanges() {
  const changes = document.querySelector('#remote-changes');
  changes.replaceChildren();
  const confirmed = state.publish_result.confirmed || [];
  if (confirmed.length && (state.publish_result.remaining || []).length) {
    changes.append(node('p', `Подтверждены потоки: ${confirmed.map(id => '#' + id).join(', ')}. `
      + 'Остальные изменения остаются в черновике.'));
  }
  if (state.publish_result.uncertain) {
    changes.append(node('p', 'Результат отправки одного из потоков не подтверждён. Обновите данные из Keitaro.'));
  }
  if (state.changes.length) {
    changes.append(node('strong', 'Изменения из Keitaro'));
    const list = node('ul');
    for (const change of state.changes) list.append(node('li', change.text));
    changes.append(list);
  }
  changes.hidden = !changes.childNodes.length;
}

function describe(value) {
  if (!value) return 'Удалён';
  if ('id' in value && !('offers' in value)) return {active: 'Включена', disabled: 'Выключена', deleted: 'Удалена'}[value.state] || 'Неизвестное состояние';
  if ('offers' in value) return `Поток: ${value.schema}; офферов: ${value.offers.length}`;
  return `#${value.offer_id} · ${value.share}% · ${value.state === 'active' ? 'активен' : 'отключён'}${value.locked ? ' · закреплён' : ''}`;
}

function renderConflicts() {
  const area = document.querySelector('#editor-conflicts');
  area.replaceChildren();
  area.hidden = !state.conflicts.length;
  if (!state.conflicts.length) return;
  area.append(node('h2', 'Разрешите конфликты перед отправкой'));
  for (const conflict of state.conflicts) {
    const card = node('div', undefined, 'conflict-card');
    card.append(node('strong', conflict.kind === 'campaign' ? 'Состояние кампании'
      : `Поток #${conflict.stream_id}${conflict.offer_id ? ' · оффер #' + conflict.offer_id : ''}`));
    card.append(node('p', `Раньше: ${describe(conflict.base)}`));
    card.append(node('p', `Ваш вариант: ${describe(conflict.local)}`));
    card.append(node('p', `Keitaro: ${describe(conflict.remote)}`));
    const controls = node('div', undefined, 'conflict-actions');
    if (conflict.kind !== 'stream' || (conflict.remote && conflict.remote.schema === 'landings')) {
      controls.append(button('Оставить мой вариант', () => act('resolve', {conflict_id: conflict.id, choice: 'local'})));
    }
    controls.append(button('Принять Keitaro', () => act('resolve', {conflict_id: conflict.id, choice: 'remote'})));
    card.append(controls);
    if (conflict.kind === 'stream' && (!conflict.remote || conflict.remote.schema !== 'landings')) {
      card.append(node('p', 'Удалённый или сменивший схему поток не восстанавливается автоматически. Офферы сохранены в архиве.'));
    }
    area.append(card);
  }
}

function permanentDisable(element, disabled) {
  element.disabled = disabled;
  element.dataset.permanentlyDisabled = String(disabled);
}

function baselineOffer(streamId, offerId) {
  return state.base.streams.find(s => s.id === streamId)?.offers.find(o => o.offer_id === offerId);
}

function offerChange(stream, offer) {
  const before = baselineOffer(stream.id, offer.offer_id);
  if (!before) return {kind: 'added', label: offer.persisted ? 'Восстановлен' : 'Добавлен'};
  const wasLocked = Object.hasOwn(state.base_pins?.[String(stream.id)] || {}, String(offer.offer_id));
  const details = [];
  if (before.share !== offer.share) details.push(`Доля: ${before.share}% → ${offer.share}%`);
  if (wasLocked !== Boolean(offer.locked)) details.push(offer.locked ? 'Доля закреплена' : 'Фиксация снята');
  if (before.state !== offer.state) details.push('Изменён статус');
  return details.length ? {kind: 'modified', label: 'Изменён', details: details.join('. ')} : null;
}

function renderStream(stream) {
  const canEdit = stream.schema === 'landings' && !stream.missing;
  const hasConflict = state.conflicts.some(c => c.stream_id === stream.id);
  const card = node('section', undefined, canEdit ? 'card editor-stream' : 'stream-readonly editor-stream');
  card.dataset.streamId = stream.id;
  const heading = node('div', undefined, 'stream-heading');
  const headingText = node('div');
  headingText.append(node('h2', stream.name || `Поток #${stream.id}`));
  const hasLocalChanges = stream.offers.some(o => offerChange(stream, o))
    || state.archive.some(o => o.stream_id === stream.id && baselineOffer(stream.id, o.offer_id));
  if (canEdit && hasLocalChanges) {
    card.classList.add('stream-has-changes');
    headingText.append(node('span', 'Есть неотправленные изменения', 'change-badge change-modified'));
  }
  if (stream.missing) headingText.append(node('span', 'Удалён в Keitaro', 'stream-meta'));
  heading.append(headingText);
  if (canEdit) {
    const equalize = button('Распределить поровну', () => act('equalize', {stream_id: stream.id}));
    permanentDisable(equalize, hasConflict);
    heading.append(equalize);
  }
  card.append(heading);
  if (canEdit) {
    renderOffers(card, stream, hasConflict);
    const active = stream.offers.filter(o => o.state === 'active');
    const sum = active.reduce((total, o) => total + o.share, 0);
    card.append(node('p', `Активных офферов: ${active.length} · Сумма: ${sum}% · `
      + `Закреплено: ${active.filter(o => o.locked).reduce((total, o) => total + o.share, 0)}%`, 'stream-summary'));
    const error = state.errors.find(e => e.stream_id === stream.id);
    if (error) card.append(node('p', error.message, 'stream-error'));
    renderPicker(card, stream, hasConflict);
  } else {
    card.append(node('p', stream.missing ? 'Поток удалён. Его офферы доступны для просмотра в архиве.'
      : stream.action_payload ? `Перенаправление: ${stream.action_payload}` : 'Этот поток доступен только для просмотра.'));
    if (!stream.missing && stream.state !== 'active') card.append(node('p', stream.state === 'disabled' ? 'Выключен' : 'Недоступен'));
  }
  renderArchive(card, stream, canEdit && !hasConflict);
  streamsNode.append(card);
}

function renderOffers(card, stream, hasConflict) {
  if (!stream.offers.length) {
    card.append(node('p', 'Нет офферов. Добавьте оффер через поиск или восстановите из архива.', 'empty-stream'));
    return;
  }
  const wrap = node('div', undefined, 'table-wrap');
  const table = node('table', undefined, 'stream-offers');
  const head = node('thead');
  const heading = node('tr');
  for (const title of ['Оффер', 'Доля, %', 'Фиксация', '']) heading.append(node('th', title));
  head.append(heading);
  const body = node('tbody');
  for (const offer of stream.offers) {
    const row = node('tr');
    if (state.changes.some(c => c.stream_id === stream.id && c.offer_id === offer.offer_id)) row.classList.add('external-change');
    const change = offerChange(stream, offer);
    if (change) row.classList.add(`draft-${change.kind}`);
    const name = node('td');
    name.append(node('span', offer.name, 'offer-name'), node('span', `#${offer.offer_id}${offer.state === 'disabled' ? ' · отключён в Keitaro' : ''}`, 'row-details'));
    if (change) {
      const badge = node('span', change.label, `change-badge change-${change.kind}`);
      if (change.details) badge.title = change.details;
      name.append(badge);
    }
    const shareCell = node('td');
    const share = node('input', undefined, 'share-input');
    share.type = 'number'; share.min = 0; share.max = 100; share.step = 1; share.value = offer.share;
    share.dataset.offerKey = `${stream.id}:${offer.offer_id}`;
    share.setAttribute('aria-label', `Доля оффера #${offer.offer_id} в потоке #${stream.id}`);
    permanentDisable(share, hasConflict || offer.state !== 'active');
    const applyShare = () => {
      if (!share.checkValidity() || !Number.isInteger(share.valueAsNumber)) {
        localUnsaved = true;
        notify('Доля должна быть целым числом от 0 до 100.', true);
        updateControls();
        return;
      }
      act('pin', {stream_id: stream.id, offer_id: offer.offer_id, locked: true, share: share.valueAsNumber});
    };
    share.addEventListener('input', () => {
      localUnsaved = true;
      pendingShareKey = `${stream.id}:${offer.offer_id}`;
      updateControls();
    });
    share.addEventListener('change', applyShare);
    share.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); applyShare(); }
    });
    shareCell.append(share);
    const before = baselineOffer(stream.id, offer.offer_id);
    if (before && before.share !== offer.share) {
      share.classList.add('changed-share');
      shareCell.append(node('span', `Было: ${before.share}%`, 'previous-share'));
    }
    const pendingLabel = node('span', 'Не сохранено в черновике', 'input-pending-label');
    pendingLabel.hidden = true;
    shareCell.append(pendingLabel);
    const apply = button('Применить', applyShare);
    apply.dataset.offerKey = `${stream.id}:${offer.offer_id}`;
    apply.setAttribute('aria-label', `Применить долю оффера #${offer.offer_id} в потоке #${stream.id}`);
    permanentDisable(apply, hasConflict || offer.state !== 'active');
    shareCell.append(apply);
    const pinCell = node('td');
    const label = node('label', undefined, 'pin-control');
    const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = offer.locked;
    checkbox.dataset.offerKey = `${stream.id}:${offer.offer_id}`;
    checkbox.setAttribute('aria-label', `Зафиксировать оффер #${offer.offer_id} в потоке #${stream.id}`);
    permanentDisable(checkbox, hasConflict || offer.state !== 'active');
    checkbox.addEventListener('change', () => act('pin', {stream_id: stream.id,
      offer_id: offer.offer_id, locked: checkbox.checked, share: Number(share.value)}));
    label.append(checkbox, node('span', offer.locked ? 'Закреплено' : 'Авто'));
    pinCell.append(label);
    const removeCell = node('td');
    const remove = button('Убрать', () => act('remove', {stream_id: stream.id, offer_id: offer.offer_id}), 'secondary remove-offer');
    remove.setAttribute('aria-label', `Убрать оффер #${offer.offer_id} из потока #${stream.id}`);
    permanentDisable(remove, hasConflict);
    removeCell.append(remove);
    row.append(name, shareCell, pinCell, removeCell);
    body.append(row);
  }
  table.append(head, body); wrap.append(table); card.append(wrap);
}

function renderPicker(card, stream, hasConflict) {
  const picker = node('div', undefined, 'offer-picker');
  const id = `search-offer-${stream.id}`;
  const label = node('label', 'Добавить оффер'); label.htmlFor = id;
  const input = node('input'); input.id = id; input.type = 'search'; input.autocomplete = 'off';
  input.placeholder = 'Поиск по названию или ID'; input.maxLength = 255;
  permanentDisable(input, hasConflict);
  const results = node('div', undefined, 'offer-results'); results.hidden = true;
  const status = node('p', '', 'field-help'); status.setAttribute('role', 'status');
  let timer, controller;
  const version = pageVersion;
  input.addEventListener('input', () => {
    clearTimeout(timer); controller?.abort(); results.replaceChildren(); results.hidden = true;
    status.textContent = '';
    const query = input.value.trim();
    if (!query) return;
    controller = new AbortController(); const current = controller; searches.add(current);
    timer = setTimeout(async () => {
      if (version !== pageVersion || current.signal.aborted) return;
      status.textContent = 'Ищем офферы…';
      try {
        const offers = await api(`/api/offers?q=${encodeURIComponent(query)}`, {signal: current.signal});
        if (current.signal.aborted || version !== pageVersion || busy) return;
        const existing = new Set(stream.offers.map(o => o.offer_id));
        const available = offers.filter(o => !existing.has(o.id));
        status.textContent = available.length ? 'Нажмите на оффер, чтобы добавить его в черновик.' : 'Новых офферов по запросу нет.';
        for (const offer of available) {
          results.append(button(`Добавить #${offer.id} · ${offer.name}`, () => act('add',
            {stream_id: stream.id, offer_id: offer.id}), 'offer-option'));
        }
        results.hidden = !available.length;
      } catch (error) { if (!current.signal.aborted && version === pageVersion) status.textContent = error.message; }
      finally { searches.delete(current); }
    }, 300);
  });
  picker.append(label, input, results, status); card.append(picker);
}

function renderArchive(card, stream, canRestore) {
  const archived = state.archive.filter(o => o.stream_id === stream.id);
  if (!archived.length) return;
  const details = node('details', undefined, 'archive');
  details.open = true;
  details.append(node('summary', `Архив (${archived.length})`));
  const reasons = {local_removed: 'Удалён в черновике', remote_removed: 'Удалён в Keitaro', stream_removed: 'Поток удалён в Keitaro'};
  for (const offer of archived) {
    const row = node('div', undefined, 'archive-row');
    const pendingRemoval = Boolean(baselineOffer(stream.id, offer.offer_id));
    if (pendingRemoval) row.classList.add('draft-removed');
    const description = node('div');
    description.append(node('span', `#${offer.offer_id} · ${offer.name}`));
    if (pendingRemoval) description.append(node('span', 'Будет убран', 'change-badge change-removed'));
    description.append(node('p', `${reasons[offer.reason] || 'Удалён из потока'} · прежняя доля ${offer.share}%`));
    const restore = button('Вернуть в поток', () => act('restore', {stream_id: stream.id, offer_id: offer.offer_id}));
    restore.setAttribute('aria-label', `Вернуть оффер #${offer.offer_id} в поток #${stream.id}`);
    permanentDisable(restore, !canRestore);
    row.append(description, restore); details.append(row);
  }
  card.append(details);
}

publishButton.addEventListener('click', () => act('publish'));
campaignToggle.addEventListener('click', () => act(state.draft.campaign.state === 'active' ? 'disable' : 'enable'));
refreshButton.addEventListener('click', () => act('refresh'));
discardButton.addEventListener('click', () => {
  if (window.confirm('Отменить все локальные изменения и вернуться к последним данным Keitaro?')) act('discard');
});
document.querySelector('#reload-draft').addEventListener('click', loadDraft);
window.addEventListener('beforeunload', event => {
  if (busy || localUnsaved) { event.preventDefault(); event.returnValue = ''; }
});
document.addEventListener('click', event => {
  const link = event.target.closest('a[href]');
  if (!link) return;
  if (busy) { event.preventDefault(); notify('Дождитесь завершения сохранения черновика.'); }
  else if (localUnsaved && !window.confirm('Введённая доля не сохранена. Покинуть страницу?')) event.preventDefault();
});
loadDraft();
