import { $, api, el, fmtDateTime, getCatalog, metricLabel, renderTopbar, toast, updateReviewCount } from './common.js';

renderTopbar('review.html');

let catalog;

async function loadStats() {
  const s = await api('/api/review/stats');
  const t = s.totals;
  const total = Object.values(t).reduce((a, b) => a + b, 0);
  if (!total) { $('#stats').textContent = ''; return; }
  const pct = n => `${((100 * (n || 0)) / total).toFixed(1)}%`;
  $('#stats').textContent = `${total.toLocaleString()} readings so far: ${pct(t.ok)} accepted automatically, ` +
    `${pct(t.verified)} confirmed by a person, ${pct(t.corrected)} corrected, ${pct(t.rejected)} unreadable, ${pct(t.review)} waiting.`;
}

async function loadFilters() {
  const cams = await api('/api/cameras');
  $('#f-camera').append(...cams.map(c => el('option', { value: c.id }, c.name || c.id)));
  const metrics = await api('/api/metrics');
  $('#f-metric').append(...metrics.map(m => el('option', { value: m.metric }, m.label)));
}

async function load(append = false) {
  const params = new URLSearchParams({ limit: 30, order: $('#f-order').value });
  if ($('#f-camera').value) params.set('camera_id', $('#f-camera').value);
  if ($('#f-metric').value) params.set('metric', $('#f-metric').value);
  const res = await api(`/api/review?${params}`);
  $('#total').textContent = `${res.total} waiting`;
  $('#total').className = `pill ${res.total ? 'warn' : 'good'}`;
  updateReviewCount(res.total);
  const list = $('#list');
  if (!append) list.replaceChildren();
  if (!res.items.length && !append) {
    list.append(el('div', { class: 'panel muted' }, 'Nothing to review. Readings that need a person will appear here.'));
  }
  for (const item of res.items) list.append(card(item));
  $('#more').textContent = `Showing ${res.items.length} of ${res.total}. The next ones appear as you finish these.`;
  $('#more').classList.toggle('hidden', res.total <= res.items.length);
}

function card(item) {
  const input = el('input', { type: 'number', step: 'any', inputmode: 'decimal', value: item.value ?? '', 'aria-label': 'Correct value' });
  const node = el('article', { class: 'review-card' });
  const act = async (action, value) => {
    try {
      await api(`/api/readings/${item.id}/review`, { method: 'POST', body: { action, value } });
      node.classList.add('done');
      node.querySelectorAll('button, input').forEach(b => { b.disabled = true; });
      setTimeout(() => {
        node.remove();
        if (!$('#list .review-card:not(.done)')) load();
      }, 400);
      loadStats();
      const next = node.nextElementSibling?.querySelector('input');
      next?.focus();
    } catch (e) { toast(e.message, 'error'); }
  };
  const save = () => {
    if (input.value === '') { input.focus(); toast('Type the value shown in the picture, or mark it unreadable', 'error'); return; }
    act('correct', Number(input.value));
  };
  input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); save(); } });
  const label = metricLabel(item.metric, catalog);
  node.append(
    el('img', { src: `/api/readings/${item.id}/crop.png`, alt: `${label} on ${item.device_name}`, loading: 'lazy' }),
    el('div', { class: 'meta' }, el('strong', {}, `${item.device_name} · ${label}`), el('span', {}, fmtDateTime(item.t_ms))),
    el('div', { class: 'ocr' }, 'Machine read: ', el('strong', { class: 'num' }, item.raw_text || '(nothing)'),
      item.unit ? ` ${item.unit}` : '', el('span', { class: 'muted' }, ` · confidence ${Math.round(item.confidence * 100)}%`),
      item.note ? el('div', { class: 'flag' }, item.note) : null),
    el('div', { class: 'actions' },
      el('button', { type: 'button', class: 'primary', disabled: item.value === null, onclick: () => act('accept') }, 'Reading is right'),
      input,
      el('button', { type: 'button', onclick: save }, 'Save value'),
      el('button', { type: 'button', class: 'danger', onclick: () => act('reject') }, 'Unreadable'),
      el('button', { type: 'button', onclick: () => openFrame(item) }, 'Full picture')),
  );
  return node;
}

function openFrame(item) {
  $('#frame-title').textContent = `${item.device_name} · ${fmtDateTime(item.t_ms)}`;
  $('#frame-img').src = `/api/frames/${item.frame_id}/image.jpg`;
  $('#frame-dialog').showModal();
}
$('#frame-close').addEventListener('click', () => $('#frame-dialog').close());

for (const id of ['#f-camera', '#f-metric', '#f-order']) $(id).addEventListener('change', () => load());

(async () => {
  catalog = await getCatalog();
  await loadFilters();
  await Promise.all([load(), loadStats()]);
})();
