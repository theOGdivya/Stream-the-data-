import { $, ago, api, el, ImagePoller, renderTopbar, toast } from './common.js';

renderTopbar('cameras.html');

const panels = new Map(); // agent id -> panel state
const INTERVALS = [5, 10, 15, 30, 60];

function sourceLabel(src) {
  if (src === 'synthetic' || src === 'synthetic-bump') return 'Fake camera (demo)';
  if (typeof src === 'number' || /^\d+$/.test(String(src))) return `Webcam ${src}`;
  return String(src ?? '–');
}

function makePanel(agent) {
  const status = el('span', { class: 'pill' });
  const scanInfo = el('span', { class: 'small muted' });
  const scanBtn = el('button', { type: 'button', onclick: () => scan(agent.id) }, 'Scan for cameras');
  const select = el('select', { name: 'source', 'aria-label': 'Camera' });
  const url = el('input', { type: 'url', name: 'url', placeholder: 'rtsp://user:pass@192.168.1.50:554/stream1', class: 'hidden' });
  const name = el('input', { name: 'name', maxlength: 255, required: true, placeholder: 'e.g. Bench 1 webcam' });
  const interval = el('select', { name: 'interval' }, INTERVALS.map(s => el('option', { value: s, selected: s === 10 }, `every ${s} s`)));
  const previewImg = el('img', { alt: 'Preview of the selected camera', class: 'hidden' });
  const previewText = el('div', { class: 'empty' });
  const preview = el('div', { class: 'viewport', style: 'cursor:default' }, previewImg, previewText);
  const form = el('form', { class: 'picker' },
    el('div', { style: 'display:grid;gap:10px;align-content:start' },
      el('label', { class: 'field' }, 'Camera', select),
      url,
      el('label', { class: 'field' }, 'Name (shown everywhere)', name),
      el('label', { class: 'field' }, 'Save a picture', interval),
      el('div', { class: 'row' }, el('button', { type: 'submit', class: 'primary' }, 'Start recording'))),
    preview);
  const recording = el('div', { class: 'cams-mini' });
  const stopped = el('div', { class: 'small' });
  const tips = el('details', { class: 'small' }, el('summary', {}, "Don't see your webcam?"),
    el('ul', {},
      el('li', {}, 'Close other apps that may be using it (Zoom, Teams, the Camera app), then press Scan for cameras.'),
      el('li', {}, 'Windows: Settings → Privacy & security → Camera → turn on "Let desktop apps access your camera".'),
      el('li', {}, 'Mac: allow Terminal when macOS asks, or System Settings → Privacy & Security → Camera → Terminal. Then restart the demo.'),
      el('li', {}, 'USB webcams: try another USB port; a hub may not carry several cameras.')));
  const node = el('section', { class: 'panel' },
    el('div', { class: 'panel-head' }, el('h2', {}, `Computer: ${agent.id}`), status, scanInfo, el('div', { class: 'spacer' }), scanBtn),
    el('h3', {}, 'Add a camera'),
    form, tips,
    el('h3', { style: 'margin-top:16px' }, 'Recording on this computer'), recording, stopped);
  const p = { node, status, scanInfo, scanBtn, select, url, name, interval, previewImg, previewText, recording, stopped,
    tiles: new Map(), detectedSig: null, nameTouched: false, userChose: false, agent };
  name.addEventListener('input', () => { p.nameTouched = true; });
  select.addEventListener('change', () => { p.userChose = true; onSelect(p); });
  form.addEventListener('submit', e => { e.preventDefault(); start(p); });
  return p;
}

function onSelect(p) {
  const v = p.select.value;
  const opt = p.select.selectedOptions[0];
  p.url.classList.toggle('hidden', v !== 'url');
  p.url.required = v === 'url';
  if (/^\d+$/.test(v)) {
    const det = (p.agent.detected?.cameras || []).find(c => String(c.index) === v);
    if (det?.has_preview) {
      p.previewImg.src = `/api/agents/${encodeURIComponent(p.agent.id)}/detected/${v}.jpg?t=${p.agent.detected.probed_ms}`;
      p.previewImg.classList.remove('hidden');
      p.previewText.classList.add('hidden');
    } else {
      showPreviewText(p, 'No preview for this camera.');
    }
    if (!p.nameTouched) p.name.value = det?.name ? det.name : `Webcam ${v}`;
  } else if (v === 'synthetic') {
    showPreviewText(p, 'A fake camera that films two fake air-quality monitors with changing numbers. Good for trying everything without hardware.');
    if (!p.nameTouched) p.name.value = 'Fake monitors (demo)';
  } else if (v === 'url') {
    showPreviewText(p, 'A network (IP) camera with an RTSP or HTTP stream. Enter its address.');
    if (!p.nameTouched) p.name.value = 'Network camera';
  } else {
    showPreviewText(p, opt?.dataset.hint || '');
  }
}

function showPreviewText(p, text) {
  p.previewImg.classList.add('hidden');
  p.previewImg.removeAttribute('src');
  p.previewText.classList.remove('hidden');
  p.previewText.textContent = text;
}

function renderOptions(p) {
  const det = p.agent.detected;
  const sig = JSON.stringify([det?.probed_ms, det?.cameras?.map(c => [c.index, c.in_use_by, c.name])]);
  if (sig === p.detectedSig) return;
  p.detectedSig = sig;
  const before = p.select.value;
  const opts = [];
  const cams = det?.cameras || [];
  for (const c of cams) {
    const label = `Camera ${c.index}: ${c.name || 'webcam'}${c.name && !c.name_certain ? ' (probably)' : ''}`;
    if (c.in_use_by) opts.push(el('option', { value: c.index, disabled: true }, `${label} (already recording as "${c.in_use_by}")`));
    else opts.push(el('option', { value: c.index }, `${label}${c.width ? ` · ${c.width}×${c.height}` : ''}`));
  }
  if (det && !cams.length) opts.push(el('option', { value: '', disabled: true, dataset: { hint: 'No webcams were found on this computer. See the tips below.' } }, 'No webcams found on this computer'));
  if (!det) opts.push(el('option', { value: '', disabled: true, dataset: { hint: 'Looking for cameras on this computer…' } }, 'Looking for cameras…'));
  opts.push(el('option', { value: 'synthetic' }, 'Fake camera with demo monitors (no webcam needed)'));
  opts.push(el('option', { value: 'url' }, 'Network camera (stream address)…'));
  p.select.replaceChildren(...opts);
  const usable = [...p.select.options].filter(o => !o.disabled).map(o => o.value);
  // Keep the visitor's own choice; otherwise suggest the first webcam found.
  p.select.value = p.userChose && usable.includes(before) ? before : usable[0];
  onSelect(p);
}

function renderRecording(p) {
  const active = p.agent.cameras.filter(c => c.enabled);
  const ids = new Set(active.map(c => c.id));
  for (const [id, t] of p.tiles) if (!ids.has(id)) { t.poller.stop(); t.node.remove(); p.tiles.delete(id); }
  if (!active.length) {
    if (!p.recording.querySelector('.muted')) p.recording.replaceChildren(el('p', { class: 'muted small' }, 'Nothing yet. Add a camera above.'));
  } else {
    p.recording.querySelector('p.muted')?.remove();
  }
  for (const c of active) {
    let t = p.tiles.get(c.id);
    if (!t) {
      const img = el('img', { alt: `Live view of ${c.name || c.id}` });
      const badge = el('span', { class: 'pill badge' }, 'Starting…');
      const info = el('div', { class: 'small' });
      const actions = el('div', { class: 'row' });
      const node = el('article', { class: 'tile' }, el('div', { class: 'viewport' }, img, badge), el('div', { class: 'tile-body' }, info, actions));
      const poller = new ImagePoller(img, () => `/api/cameras/${encodeURIComponent(c.id)}/live.jpg?t=${Date.now()}`, {
        onInfo: i => {
          if (!i.ok) { badge.className = 'pill badge'; badge.textContent = 'Waiting for the first picture…'; return; }
          badge.className = i.kind === 'live' ? 'pill badge live' : 'pill badge';
          badge.textContent = i.kind === 'live' ? 'Live' : `Still · ${ago(i.captured)}`;
        },
      });
      poller.start();
      t = { node, info, actions, poller };
      p.tiles.set(c.id, t);
      p.recording.append(node);
    }
    const st = c.status || {};
    t.info.replaceChildren(
      el('div', { class: 'device-name' }, c.name || c.id,
        el('span', { class: `pill ${c.online ? 'good' : st.error ? 'bad' : 'warn'}` }, c.online ? 'Recording' : st.error ? 'Problem' : 'Starting')),
      el('div', { class: 'muted' }, `${sourceLabel(c.source)} · picture every ${Math.round(c.still_interval_s || 10)} s${st.width ? ` · ${st.width}×${st.height}` : ''}`),
      ...(st.error && !c.online ? [el('div', { class: 'flag' }, st.error === 'starting' ? 'Opening the camera…' : st.error)] : []));
    t.actions.replaceChildren(
      el('a', { class: 'btn', href: `calibrate.html#${encodeURIComponent(c.id)}` }, 'Mark monitors →'),
      c.managed
        ? el('button', { type: 'button', class: 'danger', onclick: () => stop(c) }, 'Stop')
        : el('span', { class: 'tag', title: 'Set in the capture program’s config file on that computer' }, 'From config file'));
  }
  const off = p.agent.cameras.filter(c => !c.enabled);
  p.stopped.replaceChildren(...(off.length ? [el('p', { class: 'muted', style: 'margin:12px 0 4px' }, 'Stopped:'),
    ...off.map(c => el('div', { class: 'row', style: 'margin-bottom:4px' }, `${c.name || c.id} (${sourceLabel(c.source)})`,
      el('button', { type: 'button', onclick: () => restart(c) }, 'Start again')))] : []));
}

async function scan(agentId) {
  try {
    await api(`/api/agents/${encodeURIComponent(agentId)}/scan`, { method: 'POST' });
    toast('Scanning for cameras… this takes a few seconds');
    refresh();
  } catch (e) { toast(e.message, 'error'); }
}

async function start(p) {
  const v = p.select.value;
  const source = v === 'url' ? p.url.value.trim() : /^\d+$/.test(v) ? Number(v) : v;
  if (source === '' || source === undefined) { toast('Choose a camera first', 'error'); return; }
  try {
    await api(`/api/agents/${encodeURIComponent(p.agent.id)}/cameras`, { method: 'POST',
      body: { source, name: p.name.value.trim(), still_interval_s: Number(p.interval.value) } });
    toast('Starting the camera… the picture appears in a few seconds');
    p.nameTouched = false;
    p.userChose = false;
    refresh();
  } catch (e) { toast(e.message, 'error'); }
}

async function stop(c) {
  if (!confirm(`Stop recording "${c.name || c.id}"? Its pictures and readings so far are kept.`)) return;
  await api(`/api/cameras/${encodeURIComponent(c.id)}/stop`, { method: 'POST' });
  refresh();
}

async function restart(c) {
  await api(`/api/cameras/${encodeURIComponent(c.id)}/start`, { method: 'POST' });
  toast('Starting the camera…');
  refresh();
}

async function refresh() {
  let data;
  try { data = await api('/api/setup'); } catch (e) { toast(`Cannot reach the server: ${e.message}`, 'error'); return; }
  $('#no-agents').classList.toggle('hidden', data.agents.length > 0);
  const host = $('#agents');
  for (const a of data.agents) {
    let p = panels.get(a.id);
    if (!p) { p = makePanel(a); panels.set(a.id, p); host.append(p.node); }
    p.agent = a;
    p.status.className = `pill ${a.online ? 'good' : 'bad'}`;
    p.status.textContent = a.online ? 'Connected' : `Offline (last seen ${ago(a.last_seen_ms, data.server_time_ms)})`;
    p.scanInfo.textContent = a.scanning ? 'Scanning…' : a.detected?.probed_ms ? `Scanned ${ago(a.detected.probed_ms, data.server_time_ms)}` : '';
    p.scanBtn.disabled = !a.online || a.scanning;
    renderOptions(p);
    renderRecording(p);
  }
}

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 3000);
