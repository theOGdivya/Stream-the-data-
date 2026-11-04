import { $, $$, api, duration, el, fmtDateTime, fmtValue, renderTopbar, toast } from './common.js';

renderTopbar('lined.html');

const state = { mode: 'live', times: [], idx: 0, playing: false, data: null, liveTimer: null, busy: false };
const tiles = new Map(); // device_id -> {node, img, ...}

function preload(img, url) {
  if (img.dataset.src === url) return Promise.resolve();
  return new Promise(resolve => {
    const pre = new Image();
    pre.onload = () => { img.src = url; img.dataset.src = url; resolve(); };
    pre.onerror = () => resolve();
    pre.src = url;
  });
}

function makeTile(t) {
  const img = el('img', { alt: `Screen of ${t.name}` });
  const frameBox = el('div', { class: 'tile-img', style: `aspect-ratio:${t.warp_w} / ${t.warp_h}` }, img);
  const name = el('div', { class: 'device-name' });
  const readings = el('div', { class: 'readings' });
  const meta = el('div', { class: 'small muted' });
  const left = el('button', { type: 'button', class: 'iconbtn', 'aria-label': `Move ${t.name} left`, onclick: () => move(t.device_id, -1) }, '◀');
  const right = el('button', { type: 'button', class: 'iconbtn', 'aria-label': `Move ${t.name} right`, onclick: () => move(t.device_id, 1) }, '▶');
  const node = el('article', { class: 'tile' }, frameBox, el('div', { class: 'tile-body' }, name, readings, meta,
    el('div', { class: 'row', style: 'justify-content:flex-end' }, left, right)));
  return { node, img, frameBox, name, readings, meta };
}

function valueNode(v) {
  const review = v.status === 'review';
  const shown = review ? (v.raw_text || '?') : v.value === null ? '–' : fmtValue(v.value, v.metric);
  return el('div', { class: `reading${review ? ' review' : ''}` },
    el('div', { class: 'm' }, v.label),
    el('div', { class: 'v num' }, shown, v.unit ? el('span', { class: 'u' }, v.unit) : null),
    review ? el('div', { class: 'flag' }, '⚠ to review') : null);
}

async function render(data) {
  state.data = data;
  const host = $('#tiles');
  if (!data.tiles.length) {
    host.replaceChildren(el('div', { class: 'panel muted' }, 'No monitors marked yet. Mark them on the ', el('a', { href: 'calibrate.html' }, 'Calibrate'), ' page.'));
    tiles.clear();
    return;
  }
  const order = data.tiles.map(t => t.device_id);
  for (const id of [...tiles.keys()]) if (!order.includes(id)) { tiles.get(id).node.remove(); tiles.delete(id); }
  const loads = [];
  data.tiles.forEach((t, i) => {
    let tile = tiles.get(t.device_id);
    if (!tile) { tile = makeTile(t); tiles.set(t.device_id, tile); }
    if (host.children[i] !== tile.node) host.insertBefore(tile.node, host.children[i] || null);
    tile.name.replaceChildren(...[t.name, t.model && el('span', { class: 'tag' }, t.model), t.is_own && el('span', { class: 'tag' }, 'Ours')].filter(Boolean));
    tile.readings.replaceChildren(...t.values.map(valueNode));
    const notes = [];
    if (t.captured_ms) notes.push(`Picture ${fmtDateTime(t.captured_ms)}`);
    else notes.push('No picture near this time');
    if (t.alignment?.status === 'adjusted' && t.alignment.shift_px >= 8) notes.push(`view shifted ${Math.round(t.alignment.shift_px)} px, auto-corrected`);
    if (t.alignment?.status === 'lost') notes.push('monitor not found in this picture');
    if (t.alignment?.status === 'kept') notes.push('picture changed; read at the last known spot');
    if (t.ocr_status === 'pending') notes.push('not read yet');
    tile.meta.textContent = notes.join(' · ');
    if (t.frame_id) {
      tile.frameBox.classList.remove('empty');
      loads.push(preload(tile.img, `/api/frames/${t.frame_id}/screen/${t.device_id}.jpg?h=420`));
    } else {
      tile.frameBox.classList.add('empty');
      tile.img.removeAttribute('src');
      delete tile.img.dataset.src;
    }
  });
  await Promise.all(loads);
  // Pictures from different cameras are a few seconds apart at most when all are recording; a large
  // gap means one camera has stopped, so its tile shows an older moment.
  const far = data.spread_ms > 120_000;
  const spread = data.spread_ms > 1500
    ? ` · cameras ${data.spread_ms < 60_000 ? `${Math.round(data.spread_ms / 1000)} s` : duration(data.spread_ms)} apart${far ? ' (a camera is behind: check the times under each screen)' : ''}`
    : '';
  $('#when').textContent = data.t_ms ? `${state.mode === 'live' ? 'Newest: ' : ''}${fmtDateTime(data.t_ms)}${spread}` : '';
  $('#when').classList.toggle('flag', far);
  $('#download').href = `/api/strip.png${data.t_ms ? `?t_ms=${data.t_ms}` : ''}`;
}

async function move(deviceId, dir) {
  const ids = state.data.tiles.map(t => t.device_id);
  const i = ids.indexOf(deviceId);
  const j = i + dir;
  if (j < 0 || j >= ids.length) return;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  await api('/api/strip/order', { method: 'POST', body: { device_ids: ids } });
  await refresh();
}

async function refresh() {
  if (state.busy) return;
  state.busy = true;
  try {
    const t = state.mode === 'time' && state.times.length ? state.times[state.idx] : null;
    await render(await api(`/api/strip${t ? `?t_ms=${t}` : ''}`));
    if (state.mode === 'time') updatePos();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    state.busy = false;
  }
}

// --- live mode ------------------------------------------------------------------------------
function startLive() {
  stopLive();
  refresh();
  state.liveTimer = setInterval(() => { if (!document.hidden) refresh(); }, 4000);
}
function stopLive() { clearInterval(state.liveTimer); state.liveTimer = null; }

// --- time travel ----------------------------------------------------------------------------
async function loadPeriods() {
  const runs = await api('/api/runs');
  const now = Date.now();
  const opts = [...runs.map(r => ({ label: `Run: ${r.name}`, since: r.started_ms, until: r.ended_ms || now })),
    { label: 'Last hour', since: now - 3600e3, until: now }, { label: 'Last 24 hours', since: now - 86400e3, until: now }];
  $('#period').replaceChildren(...opts.map((o, i) => el('option', { value: i, dataset: { since: o.since, until: o.until } }, o.label)));
}

async function loadTimes() {
  const opt = $('#period').selectedOptions[0];
  if (!opt) return;
  const res = await api(`/api/strip/times?since_ms=${opt.dataset.since}&until_ms=${opt.dataset.until}`);
  state.times = res.times;
  const slider = $('#slider');
  slider.max = Math.max(0, state.times.length - 1);
  state.idx = Math.max(0, state.times.length - 1);
  slider.value = state.idx;
  if (!state.times.length) { $('#pos').textContent = 'No pictures in this period'; return; }
  await refresh();
}

function updatePos() {
  $('#slider').value = state.idx;
  $('#pos').textContent = state.times.length ? `${state.idx + 1} of ${state.times.length}` : '';
}

async function step(d) {
  const n = state.idx + d;
  if (n < 0 || n >= state.times.length) return false;
  state.idx = n;
  await refresh();
  return true;
}

async function play() {
  if (state.playing) { state.playing = false; $('#play').textContent = '▶ Play'; return; }
  if (state.idx >= state.times.length - 1) state.idx = 0;
  state.playing = true;
  $('#play').textContent = '❚❚ Pause';
  while (state.playing) {
    const t0 = performance.now();
    if (!(await step(1))) break;
    const wait = 1000 / Number($('#speed').value) - (performance.now() - t0);
    if (wait > 0) await new Promise(r => setTimeout(r, wait));
  }
  state.playing = false;
  $('#play').textContent = '▶ Play';
}

$$('[data-mode]').forEach(b => b.addEventListener('click', async () => {
  state.mode = b.dataset.mode;
  $$('[data-mode]').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
  $('#time-controls').classList.toggle('hidden', state.mode !== 'time');
  state.playing = false;
  if (state.mode === 'live') startLive();
  else { stopLive(); await loadPeriods(); await loadTimes(); }
}));
$('#period').addEventListener('change', loadTimes);
$('#slider').addEventListener('input', e => { state.idx = Number(e.target.value); refresh(); });
$('#prev').addEventListener('click', () => step(-1));
$('#next').addEventListener('click', () => step(1));
$('#play').addEventListener('click', play);

startLive();
