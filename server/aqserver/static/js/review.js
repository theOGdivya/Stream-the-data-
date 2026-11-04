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
  const input = el('input', { type: 'number', step: 'any', inputmode: 'decimal', value: item.value ?? '', placeholder: 'Right value', 'aria-label': 'Correct value' });
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
      el('button', { type: 'button', class: 'danger', onclick: () => act('reject') }, 'Unreadable'),
      el('button', { type: 'button', class: 'link', onclick: () => openFrame(item) }, 'Full picture')),
    el('div', { class: 'actions fix' }, el('span', { class: 'small muted' }, 'Wrong?'), input,
      el('button', { type: 'button', onclick: save }, 'Save the right value')),
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

// --- spot check -------------------------------------------------------------------------------
// Random automatically accepted readings, checked by a person: the basis of the accuracy figure.

function spotParams() {
  const v = $('#spot-run').value;
  return v ? new URLSearchParams({ run_id: v }) : new URLSearchParams();
}

async function loadSpot() {
  const params = spotParams();
  params.set('limit', 10);
  const res = await api(`/api/spotcheck?${params}`);
  $('#spot-target').textContent = res.target;
  renderSpotProgress(res);
  const list = $('#spot-list');
  list.replaceChildren(...(res.items.length ? res.items.map(spotCard)
    : [el('div', { class: 'panel muted' }, 'No automatically accepted readings left to check in this period.')]));
  list.querySelector('button.primary')?.focus();
}

let spotProgress = null; // last progress table, updated in place after each answer

// 95% Wilson interval, as on the server (accuracy.wilson), in percent with one decimal.
function wilson(k, n, z = 1.96) {
  const p = k / n, den = 1 + (z * z) / n;
  const centre = (p + (z * z) / (2 * n)) / den;
  const half = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / den;
  const r = v => Math.round(1000 * v) / 10;
  return [r(Math.max(0, centre - half)), r(Math.min(1, centre + half))];
}

// Count one answer without asking the server to re-aggregate the whole run.
function countSpot(item, status) {
  const row = spotProgress?.progress.find(r => r.device_name === item.device_name && r.metric === item.metric);
  if (!row || (status !== 'verified' && status !== 'corrected')) return;
  row.spot_checked += 1;
  row.spot_right += status === 'verified' ? 1 : 0;
  row.accuracy_pct = Math.round((1000 * row.spot_right) / row.spot_checked) / 10;
  [row.ci_low, row.ci_high] = wilson(row.spot_right, row.spot_checked);
  renderSpotProgress(spotProgress);
}

function renderSpotProgress(res) {
  spotProgress = res;
  const rows = res.progress;
  $('#spot-progress').replaceChildren(
    el('thead', {}, el('tr', {}, ['Monitor', 'Reading', 'Checked', 'Accuracy so far'].map((h, i) => el('th', { class: i > 1 ? 'numcell' : null }, h)))),
    el('tbody', {}, rows.length ? rows.map(r => el('tr', {},
      el('td', {}, r.device_name), el('td', {}, metricLabel(r.metric, catalog)),
      el('td', { class: 'numcell' }, `${r.spot_checked} of ${res.target}`),
      el('td', { class: 'numcell' }, r.accuracy_pct === null ? '–' : `${r.accuracy_pct}% (${r.ci_low}–${r.ci_high}%)`)))
      : [el('tr', {}, el('td', { colspan: 4, class: 'muted' }, 'No readings in this period yet.'))]));
}

function spotCard(item) {
  const input = el('input', { type: 'number', step: 'any', inputmode: 'decimal', placeholder: 'Right value', 'aria-label': 'If it is wrong: the value shown in the picture' });
  const node = el('article', { class: 'review-card' });
  const act = async (action, value) => {
    try {
      const saved = await api(`/api/readings/${item.id}/spotcheck`, { method: 'POST', body: { action, value } });
      countSpot(item, saved.status);
      node.classList.add('done');
      node.querySelectorAll('button, input').forEach(b => { b.disabled = true; });
      node.nextElementSibling?.querySelector('button.primary')?.focus();
      setTimeout(() => {
        node.remove();
        if (!$('#spot-list .review-card:not(.done)')) loadSpot();
      }, 300);
    } catch (e) { toast(e.message, 'error'); }
  };
  const wrong = () => {
    if (input.value === '') { input.focus(); toast('Type the value shown in the picture', 'error'); return; }
    act('wrong', Number(input.value));
  };
  input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); wrong(); } });
  const label = metricLabel(item.metric, catalog);
  node.append(
    el('img', { src: `/api/readings/${item.id}/crop.png`, alt: `${label} on ${item.device_name}`, loading: 'lazy' }),
    el('div', { class: 'meta' }, el('strong', {}, `${item.device_name} · ${label}`), el('span', {}, fmtDateTime(item.t_ms))),
    el('div', { class: 'ocr' }, 'Machine read: ', el('strong', { class: 'num' }, item.raw_text), item.unit ? ` ${item.unit}` : ''),
    el('div', { class: 'actions' },
      el('button', { type: 'button', class: 'primary', onclick: () => act('right') }, 'Matches the picture'),
      el('button', { type: 'button', onclick: () => act('unclear') }, 'Can’t tell'),
      el('button', { type: 'button', class: 'link', onclick: () => openFrame(item) }, 'Full picture')),
    el('div', { class: 'actions fix' }, el('span', { class: 'small muted' }, 'Wrong?'), input,
      el('button', { type: 'button', onclick: wrong }, 'Save the right value')),
  );
  return node;
}

function setMode(mode) {
  const spot = mode === 'spot';
  for (const [id, on] of [['#tab-queue', !spot], ['#tab-spot', spot]]) {
    $(id).setAttribute('aria-selected', String(on));
    $(id).setAttribute('aria-pressed', String(on));
  }
  $('#spot').classList.toggle('hidden', !spot);
  for (const id of ['#queue-head', '#list']) $(id).classList.toggle('hidden', spot);
  if (spot) $('#more').classList.add('hidden');
  history.replaceState(null, '', spot ? '#spot' : '#');
  if (spot) loadSpot().catch(e => toast(e.message, 'error'));
  else load();
}
$('#tab-queue').addEventListener('click', () => setMode('queue'));
$('#tab-spot').addEventListener('click', () => setMode('spot'));
$('#spot-run').addEventListener('change', () => loadSpot().catch(e => toast(e.message, 'error')));

(async () => {
  catalog = await getCatalog();
  await loadFilters();
  const runs = await api('/api/runs');
  $('#spot-run').replaceChildren(...runs.map(r => el('option', { value: r.id }, `${r.name}${r.ended_ms ? '' : ' (running)'}`)),
    el('option', { value: '' }, 'All time'));
  await loadStats();
  setMode(location.hash === '#spot' ? 'spot' : 'queue');
})();
