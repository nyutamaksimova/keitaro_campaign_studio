'use strict';

const search = document.querySelector('#campaign-search');
const list = document.querySelector('#campaign-list');
const status = document.querySelector('#campaign-list-status');
const previous = document.querySelector('#previous-page');
const next = document.querySelector('#next-page');
let offset = 0;
const limit = 12;
let controller;
let timer;

async function loadCampaigns() {
  controller?.abort();
  controller = new AbortController();
  const current = controller;
  status.textContent = 'Загружаем кампании…';
  previous.disabled = next.disabled = true;
  list.replaceChildren();
  try {
    const params = new URLSearchParams({offset, limit, q: search.value.trim()});
    const response = await fetch(`/api/campaigns?${params}`, {signal: current.signal});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Ошибка загрузки.');
    if (current.signal.aborted) return;
    for (const campaign of data.items) {
      const card = document.createElement('a');
      card.href = `/campaigns/${campaign.id}`;
      card.className = 'card campaign-card';
      const meta = document.createElement('span');
      meta.className = 'eyebrow';
      meta.textContent = `КАМПАНИЯ #${campaign.id}`;
      const heading = document.createElement('h2');
      heading.textContent = campaign.name;
      const state = document.createElement('span');
      state.className = 'field-help';
      const stateLabel = {active: 'Включена', disabled: 'Выключена', deleted: 'Удалена'}[campaign.state] || 'Неизвестное состояние';
      state.textContent = `${stateLabel} · Открыть →`;
      card.append(meta, heading, state);
      list.append(card);
    }
    status.textContent = data.items.length ? 'Кампании загружены из Keitaro.' : 'Кампании не найдены.';
    previous.disabled = offset === 0;
    next.disabled = !data.has_more;
    document.querySelector('#page-number').textContent = `Страница ${offset / limit + 1}`;
  } catch (error) {
    if (!current.signal.aborted) status.textContent = error.message;
  }
}

search.addEventListener('input', () => {
  clearTimeout(timer);
  controller?.abort();
  offset = 0;
  timer = setTimeout(loadCampaigns, 400);
});
previous.addEventListener('click', () => { offset = Math.max(0, offset - limit); loadCampaigns(); });
next.addEventListener('click', () => { offset += limit; loadCampaigns(); });
document.querySelector('#refresh-list').addEventListener('click', loadCampaigns);
loadCampaigns();
