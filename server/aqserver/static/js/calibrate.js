import { $, $$, api, el, fmtDateTime, getCatalog, metricLabel, renderTopbar, toast } from './common.js';

renderTopbar('calibrate.html');

const SVGNS = 'http://www.w3.org/2000/svg';
const CORNERS = ['top-left', 'top-right', 'bottom-right', 'bottom-left'];
const state = {
  catalog: null, cameras: [], camId: null, frame: null, deviceId: null,
  mode: null, // 'corners' while clicking corners
  pending: null, // {create: {...form}} or {deviceId} while clicking corners
  points: [], fieldId: null, draftBox: null,
};

function svgEl(tag, attrs = {}, text) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text !== undefined) n.textContent = text;
  return n;
}
function svgPoint(svg, evt) {
  const pt = svg.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}
const enc = encodeURIComponent;
const cam = () => state.cameras.find(c => c.id === state.camId);
const device = () => cam()?.devices.find(d => d.id === state.deviceId);

function loadImg(img, src) {
  return new Promise((resolve, reject) => {
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('image failed to load'));
    img.src = src;
  });
}

// --- cameras & frames -------------------------------------------------------------------------

async function loadCameras(preferId) {
  state.cameras = await api('/api/cameras');
  const sel = $('#camera');
  if (!state.cameras.length) {
    sel.replaceChildren(el('option', {}, 'No cameras yet'));
    showFrameHint('No camera has sent a picture yet. Start the capture agent first.');
    return;
  }
  sel.replaceChildren(...state.cameras.map(c => el('option', { value: c.id }, c.name ? `${c.name} (${c.id})` : c.id)));
  const want = preferId || state.camId || decodeURIComponent(location.hash.slice(1));
  state.camId = state.cameras.some(c => c.id === want) ? want : state.cameras[0].id;
  sel.value = state.camId;
  if (!device()) state.deviceId = null;
}

async function showNewestFrame() {
  const c = cam();
  if (!c) return;
  try {
    const f = await api(`/api/cameras/${enc(c.id)}/latest-frame`);
    const img = await loadImg($('#frame-img'), `/api/frames/${f.id}/image.jpg`);
    state.frame = { id: f.id, w: img.naturalWidth, h: img.naturalHeight, captured_ms: f.captured_ms };
    $('#frame-svg').setAttribute('viewBox', `0 0 ${state.frame.w} ${state.frame.h}`);
    $('#frame-info').textContent = `Picture taken ${fmtDateTime(f.captured_ms)}${f.masked ? ' (areas outside the monitors are blacked out)' : ''}`;
  } catch (e) {
    showFrameHint(`No picture from this camera yet (${e.message}).`);
  }
}

function showFrameHint(text) {
  const h = $('#frame-hint');
  h.textContent = text || '';
  h.classList.toggle('hidden', !text);
}

function renderCameraSettings() {
  const c = cam();
  $('#storage-box').classList.toggle('hidden', !c);
  if (!c) return;
  $('#mask-enabled').checked = c.mask_enabled;
  $('#full-every').value = Math.round((c.full_every_s || 300) / 60);
  $('#mask-note').textContent = c.mask_enabled
    ? 'On: the capture computer blacks out everything except the marked monitors before saving each still.'
    : 'Off: full pictures are stored.';
}

async function saveCameraSettings() {
  const c = cam();
  if (!c) return;
  const enabling = $('#mask-enabled').checked && !c.mask_enabled;
  if (enabling && !confirm('Only the marked monitors will be kept in stored pictures. Anything not marked (including monitors you have not marked yet) is blacked out and cannot be recovered. Continue?')) {
    $('#mask-enabled').checked = false;
    return;
  }
  const minutes = Math.max(1, Math.min(1440, Number($('#full-every').value) || 5));
  try {
    await api(`/api/cameras/${enc(c.id)}`, { method: 'PATCH', body: { mask_enabled: $('#mask-enabled').checked, full_every_s: minutes * 60 } });
    toast('Saved. The capture computer picks this up within a few seconds.');
    await loadCameras();
    renderCameraSettings();
  } catch (err) { toast(err.message, 'error'); }
}
$('#mask-enabled').addEventListener('change', saveCameraSettings);
$('#full-every').addEventListener('change', saveCameraSettings);

async function refreshAll(preferId) {
  await loadCameras(preferId);
  renderCameraSettings();
  await showNewestFrame();
  renderDevices();
  renderFrameOverlay();
  await renderScreen();
}

// --- monitors (devices) -----------------------------------------------------------------------

function renderDevices() {
  const list = $('#device-list');
  const devices = cam()?.devices || [];
  if (!devices.length) {
    list.replaceChildren(el('p', { class: 'muted small' }, 'No monitors marked on this camera yet.'));
  } else {
    list.replaceChildren(...devices.map(d => el('button', {
      type: 'button', class: 'list-item', 'aria-selected': String(d.id === state.deviceId),
      onclick: () => selectDevice(d.id),
    }, el('span', { style: 'flex:1' }, el('strong', {}, d.name), d.model ? el('span', { class: 'muted small' }, ` · ${d.model}`) : null),
    d.is_own ? el('span', { class: 'tag' }, 'Ours') : null,
    alignTag(d),
    el('span', { class: 'muted small' }, `${d.fields.length} number${d.fields.length === 1 ? '' : 's'}`))));
  }
  const form = $('#device-form');
  const d = device();
  if (d && !state.pending) {
    form.classList.remove('hidden');
    $('#device-form-title').textContent = `Monitor: ${d.name}`;
    form.dev_name.value = d.name;
    form.model.value = d.model || '';
    form.is_own.checked = d.is_own;
    form.external_id.value = d.external_id || '';
    $('#device-save').textContent = 'Save details';
    $('#device-delete').classList.remove('hidden');
    renderAlignment(d);
  } else if (!state.pending) {
    form.classList.add('hidden');
  }
  if (!d || state.pending) $('#align-box').replaceChildren();
}

function alignTag(d) {
  const a = d.alignment || {};
  if (a.status === 'lost') return el('span', { class: 'pill bad' }, 'Not found');
  if (a.status === 'adjusted') return el('span', { class: 'pill warn' }, `Shifted ${Math.round(a.shift_px)} px`);
  if (a.status === 'kept') return el('span', { class: 'pill' }, 'Picture changed');
  return null;
}

// Camera-shift detection status for the selected monitor.
function renderAlignment(d) {
  const a = d.alignment || {};
  const box = $('#align-box');
  const when = a.checked_ms ? ` (picture from ${fmtDateTime(a.checked_ms)})` : '';
  if (a.status === 'adjusted') {
    box.replaceChildren(el('div', { class: 'alert', style: 'border-color:var(--warning);background:color-mix(in srgb, var(--warning) 10%, var(--surface))' },
      el('span', { class: 'icon', 'aria-hidden': 'true' }, '!'),
      el('div', {},
        el('strong', {}, `Moved ${Math.round(a.shift_px)} px since it was marked`), when,
        el('p', { class: 'small', style: 'margin:4px 0 8px' }, 'Readings are being corrected automatically (orange dashed outline = where it is now). If the camera or monitor was moved on purpose, accept the new position.'),
        el('button', { type: 'button', class: 'primary', onclick: () => acceptAlignment(d) }, 'Accept new position'))));
  } else if (a.status === 'kept') {
    box.replaceChildren(el('div', { class: 'alert', style: 'border-color:var(--border)' },
      el('span', { class: 'icon', 'aria-hidden': 'true' }, 'i'),
      el('div', {},
        el('strong', {}, 'The picture looks different from when this monitor was marked'), when,
        el('p', { class: 'small', style: 'margin:4px 0 8px' }, 'Usually the room light or screen brightness changed. The numbers still read fine at the marked spot, so readings carry on. If the setup is fine, make the newest picture the reference.'),
        el('button', { type: 'button', class: 'primary', onclick: () => acceptAlignment(d) }, 'Use newest picture as reference'))));
  } else if (a.status === 'lost') {
    box.replaceChildren(el('div', { class: 'alert', role: 'alert' },
      el('span', { class: 'icon', 'aria-hidden': 'true' }, '!'),
      el('div', {}, el('strong', {}, 'Not found in the latest picture'), when,
        el('p', { class: 'small', style: 'margin:4px 0 0' }, 'The camera may have moved a lot, or something is blocking the view. Fix the setup, or re-mark the corners on the newest picture.'),
        el('button', { type: 'button', style: 'margin-top:8px', onclick: () => {
          state.pending = { deviceId: d.id };
          startCorners();
        } }, 'Re-mark corners'))));
  } else {
    box.replaceChildren(el('p', { class: 'small muted', style: 'margin:0' },
      a.status ? `Position check: in place${when}.` : 'Position check: waiting for the next picture.'));
  }
}

async function acceptAlignment(d) {
  try {
    await api(`/api/devices/${d.id}/accept-alignment`, { method: 'POST' });
    toast('New position accepted. It is now the reference for this monitor.');
    await refreshAll();
  } catch (err) { toast(err.message, 'error'); }
}

function selectDevice(id) {
  if (state.mode) return;
  state.deviceId = id;
  state.fieldId = null;
  state.draftBox = null;
  renderDevices();
  renderFrameOverlay();
  renderScreen();
}

$('#add-device').addEventListener('click', () => {
  if (!state.frame) { toast('Wait for a picture from this camera first', 'error'); return; }
  state.deviceId = null;
  state.pending = { create: true };
  const form = $('#device-form');
  form.reset();
  form.classList.remove('hidden');
  $('#device-form-title').textContent = 'New monitor';
  $('#device-save').textContent = 'Next: click corners';
  $('#device-delete').classList.add('hidden');
  renderDevices();
  renderFrameOverlay();
  renderScreen();
  form.dev_name.focus();
});

$('#device-form').addEventListener('submit', async e => {
  e.preventDefault();
  const form = e.target;
  const details = { name: form.dev_name.value.trim(), model: form.model.value.trim() || null,
    is_own: form.is_own.checked, external_id: form.external_id.value.trim() || null };
  if (state.pending?.create) {
    state.pending.details = details;
    startCorners();
    return;
  }
  const d = device();
  if (!d) return;
  await api(`/api/devices/${d.id}`, { method: 'PATCH', body: details });
  toast('Saved');
  await refreshAll();
});

$('#device-cancel').addEventListener('click', () => {
  state.pending = null;
  state.mode = null;
  state.points = [];
  showFrameHint('');
  if (!device()) $('#device-form').classList.add('hidden');
  renderDevices();
  renderFrameOverlay();
});

$('#device-delete').addEventListener('click', async () => {
  const d = device();
  if (!d || !confirm(`Remove "${d.name}" from this camera? Its past readings are kept.`)) return;
  await api(`/api/devices/${d.id}`, { method: 'DELETE' });
  state.deviceId = null;
  toast('Monitor removed');
  await refreshAll();
});

function startCorners() {
  state.mode = 'corners';
  state.points = [];
  cornerHint();
  renderFrameOverlay();
}
function cornerHint() {
  const n = state.points.length;
  showFrameHint(`Click the ${CORNERS[n].toUpperCase()} corner of the screen (${n + 1} of 4). Esc to cancel, Backspace to undo.`);
}

document.addEventListener('keydown', e => {
  if (state.mode !== 'corners') return;
  if (e.key === 'Escape') $('#device-cancel').click();
  if (e.key === 'Backspace' && state.points.length) {
    e.preventDefault();
    state.points.pop();
    cornerHint();
    renderFrameOverlay();
  }
});

$('#frame-svg').addEventListener('pointerdown', async e => {
  if (state.mode !== 'corners') return;
  const p = svgPoint(e.currentTarget, e);
  state.points.push([Math.round(p.x * 10) / 10, Math.round(p.y * 10) / 10]);
  if (state.points.length < 4) {
    cornerHint();
    renderFrameOverlay();
    return;
  }
  const corners = state.points;
  state.mode = null;
  state.points = [];
  showFrameHint('');
  try {
    let id;
    if (state.pending?.create) {
      const d = await api(`/api/cameras/${enc(state.camId)}/devices`, { method: 'POST', body: { ...state.pending.details, corners, ref_frame_id: state.frame.id } });
      id = d.id;
      toast('Monitor added. Now draw a box around each number on its screen below.');
    } else {
      id = state.pending.deviceId;
      await api(`/api/devices/${id}`, { method: 'PATCH', body: { corners, ref_frame_id: state.frame.id } });
      toast('Corners updated');
    }
    state.pending = null;
    state.deviceId = id;
    state.fieldId = null; // a number of another monitor must not stay selected
    state.draftBox = null;
    $('#field-form').classList.add('hidden');
    await refreshAll();
    $('#screen-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (err) {
    toast(err.message, 'error');
    state.pending = null;
    renderFrameOverlay();
  }
});

function renderFrameOverlay() {
  const svg = $('#frame-svg');
  svg.replaceChildren();
  if (!state.frame) return;
  const fs = state.frame.w / 55;
  const r = state.frame.w / 140;
  for (const d of cam()?.devices || []) {
    const sel = d.id === state.deviceId;
    const poly = svgEl('polygon', {
      points: d.corners.map(p => p.join(',')).join(' '),
      fill: sel ? 'rgba(57,135,229,0.22)' : 'rgba(57,135,229,0.08)', stroke: '#3987e5',
      'stroke-width': sel ? 3 : 2, 'vector-effect': 'non-scaling-stroke', style: 'cursor:pointer',
    });
    poly.addEventListener('click', () => selectDevice(d.id));
    svg.append(poly);
    if (d.alignment?.status === 'adjusted' && d.alignment.corners) {
      svg.append(svgEl('polygon', { points: d.alignment.corners.map(p => p.join(',')).join(' '), fill: 'none', stroke: '#eb6834',
        'stroke-width': 2, 'stroke-dasharray': '8 6', 'vector-effect': 'non-scaling-stroke', style: 'pointer-events:none' }));
    }
    const [x, y] = d.corners[0];
    svg.append(svgEl('text', { x, y: y - fs * 0.4, 'font-size': fs, 'font-weight': 700, fill: '#fff',
      stroke: '#000', 'stroke-width': fs / 6, 'paint-order': 'stroke', style: 'pointer-events:none' }, d.name));
    if (sel && !state.mode) addCornerHandles(svg, d, poly, r);
  }
  if (state.mode === 'corners' && state.points.length) {
    svg.append(svgEl('polyline', { points: state.points.map(p => p.join(',')).join(' '), fill: 'none', stroke: '#eb6834',
      'stroke-width': 2, 'vector-effect': 'non-scaling-stroke' }));
    state.points.forEach(([x, y]) => svg.append(svgEl('circle', { cx: x, cy: y, r, fill: '#eb6834', stroke: '#fff', 'stroke-width': 2,
      'vector-effect': 'non-scaling-stroke' })));
  }
}

function addCornerHandles(svg, d, poly, r) {
  const corners = d.corners.map(p => [...p]);
  corners.forEach((pt, i) => {
    const c = svgEl('circle', { cx: pt[0], cy: pt[1], r: r * 1.2, fill: '#3987e5', stroke: '#fff', 'stroke-width': 2,
      'vector-effect': 'non-scaling-stroke', style: 'cursor:grab;touch-action:none' });
    c.append(svgEl('title', {}, `Drag to adjust the ${CORNERS[i]} corner`));
    c.addEventListener('pointerdown', e => {
      e.stopPropagation();
      c.setPointerCapture(e.pointerId);
      const move = ev => {
        const p = svgPoint(svg, ev);
        corners[i] = [Math.round(p.x * 10) / 10, Math.round(p.y * 10) / 10];
        c.setAttribute('cx', corners[i][0]);
        c.setAttribute('cy', corners[i][1]);
        poly.setAttribute('points', corners.map(q => q.join(',')).join(' '));
      };
      const up = async () => {
        c.removeEventListener('pointermove', move);
        c.removeEventListener('pointerup', up);
        try {
          await api(`/api/devices/${d.id}`, { method: 'PATCH', body: { corners, ref_frame_id: state.frame.id } });
          toast('Corner moved. Re-read past pictures if this monitor already had numbers.');
          await refreshAll();
        } catch (err) { toast(err.message, 'error'); }
      };
      c.addEventListener('pointermove', move);
      c.addEventListener('pointerup', up);
    });
    svg.append(c);
  });
}

// --- numbers on a screen (fields) -------------------------------------------------------------

async function renderScreen() {
  const d = device();
  $('#screen-panel').classList.toggle('hidden', !d);
  if (!d || !state.frame) return;
  $('#screen-title').textContent = `Numbers on ${d.name}`;
  const img = await loadImg($('#screen-img'), `/api/devices/${d.id}/screen.jpg?frame_id=${state.frame.id}&w=${d.warp_w}&c=${enc(JSON.stringify(d.corners))}`);
  $('#screen-svg').setAttribute('viewBox', `0 0 ${img.naturalWidth} ${img.naturalHeight}`);
  renderFields();
}

function renderFields() {
  const d = device();
  if (!d) return;
  const svg = $('#screen-svg');
  const [W, H] = svg.getAttribute('viewBox').split(' ').slice(2).map(Number);
  svg.replaceChildren();
  const fs = W / 30;
  for (const f of d.fields) {
    const sel = f.id === state.fieldId;
    const box = sel && state.draftBox ? state.draftBox : f.box;
    const rect = svgEl('rect', { x: box[0] * W, y: box[1] * H, width: box[2] * W, height: box[3] * H, fill: sel ? 'rgba(235,104,52,0.15)' : 'rgba(57,135,229,0.12)',
      stroke: sel ? '#eb6834' : '#3987e5', 'stroke-width': sel ? 3 : 2, 'vector-effect': 'non-scaling-stroke', style: 'cursor:pointer' });
    rect.addEventListener('pointerdown', e => { e.stopPropagation(); selectField(f.id); });
    svg.append(rect, svgEl('text', { x: box[0] * W + 4, y: box[1] * H - 6, 'font-size': fs, 'font-weight': 700, fill: '#fff', stroke: '#000',
      'stroke-width': fs / 6, 'paint-order': 'stroke', style: 'pointer-events:none' }, f.label || metricLabel(f.metric, state.catalog)));
  }
  if (state.draftBox && !state.fieldId) {
    const b = state.draftBox;
    svg.append(svgEl('rect', { x: b[0] * W, y: b[1] * H, width: b[2] * W, height: b[3] * H, fill: 'rgba(235,104,52,0.15)', stroke: '#eb6834',
      'stroke-width': 3, 'vector-effect': 'non-scaling-stroke' }));
  }
  const list = $('#field-list');
  list.replaceChildren(...(d.fields.length ? d.fields.map(f => el('button', {
    type: 'button', class: 'list-item', 'aria-selected': String(f.id === state.fieldId), onclick: () => selectField(f.id),
  }, el('strong', { style: 'flex:1' }, f.label || metricLabel(f.metric, state.catalog)),
  el('span', { class: 'muted small' }, READER_NAMES[f.recognizer] || f.recognizer))) : [el('p', { class: 'muted small' }, 'No numbers yet. Drag a box on the screen.')]));
  $('#screen-hint').textContent = state.fieldId ? 'Drag a new box to move this number, then save.' : 'Drag a box around one number.';
}

function selectField(id) {
  state.fieldId = id;
  state.draftBox = null;
  const f = device().fields.find(x => x.id === id);
  fillFieldForm(f);
  renderFields();
}

function fillFieldForm(f) {
  const form = $('#field-form');
  form.classList.remove('hidden');
  $('#field-form-title').textContent = f ? `Edit: ${f.label || metricLabel(f.metric, state.catalog)}` : 'New number';
  $('#field-delete').classList.toggle('hidden', !f);
  const metric = f ? f.metric : guessMetric();
  form.metric.value = metric;
  const def = state.catalog.metrics[metric] || {};
  form.unit.value = f ? f.unit || '' : def.unit || '';
  form.label.value = f ? f.label || '' : '';
  form.recognizer.value = f ? f.recognizer : 'auto';
  const o = f ? f.options || {} : {};
  form.polarity.value = o.polarity || 'auto';
  form.decimals.value = String(o.decimals ?? (metric === 'temp' ? 1 : 0));
  form.slant.value = o.slant ?? 0;
  form.min_value.value = f ? f.min_value ?? '' : def.min ?? '';
  form.max_value.value = f ? f.max_value ?? '' : def.max ?? '';
}

function guessMetric() {
  const used = new Set(device().fields.map(f => f.metric));
  return ['pm25', 'pm10', 'co2', 'tvoc', 'hcho', 'temp', 'rh'].find(m => !used.has(m)) || 'other';
}

$('#field-form').metric.addEventListener('change', e => {
  if (state.fieldId) return;
  const def = state.catalog.metrics[e.target.value] || {};
  const form = $('#field-form');
  form.unit.value = def.unit || '';
  form.min_value.value = def.min ?? '';
  form.max_value.value = def.max ?? '';
  form.decimals.value = e.target.value === 'temp' ? '1' : '0';
});

// Drag to draw a box on the straightened screen.
(() => {
  const svg = $('#screen-svg');
  let start = null;
  svg.addEventListener('pointerdown', e => {
    if (!device()) return;
    svg.setPointerCapture(e.pointerId);
    start = svgPoint(svg, e);
  });
  svg.addEventListener('pointermove', e => {
    if (!start) return;
    const [W, H] = svg.getAttribute('viewBox').split(' ').slice(2).map(Number);
    const p = svgPoint(svg, e);
    const x0 = Math.max(0, Math.min(start.x, p.x)), y0 = Math.max(0, Math.min(start.y, p.y));
    const x1 = Math.min(W, Math.max(start.x, p.x)), y1 = Math.min(H, Math.max(start.y, p.y));
    state.draftBox = [x0 / W, y0 / H, (x1 - x0) / W, (y1 - y0) / H].map(v => Math.round(v * 10000) / 10000);
    renderFields();
  });
  svg.addEventListener('pointerup', () => {
    if (!start) return;
    start = null;
    const b = state.draftBox;
    if (!b || b[2] < 0.01 || b[3] < 0.01) { state.draftBox = null; renderFields(); return; }
    if (!state.fieldId) fillFieldForm(null);
    $('#field-form').metric.focus();
  });
})();

$('#field-form').addEventListener('submit', async e => {
  e.preventDefault();
  const form = e.target;
  const num = v => (v === '' ? null : Number(v));
  const body = {
    metric: form.metric.value, unit: form.unit.value.trim() || null, label: form.label.value.trim() || null,
    recognizer: form.recognizer.value,
    options: { polarity: form.polarity.value, decimals: form.decimals.value === 'auto' ? 'auto' : Number(form.decimals.value),
      slant: Number(form.slant.value || 0) },
    min_value: num(form.min_value.value), max_value: num(form.max_value.value),
  };
  try {
    const label = form.label.value.trim() || metricLabel(form.metric.value, state.catalog);
    if (state.fieldId) {
      if (state.draftBox) body.box = state.draftBox;
      await api(`/api/fields/${state.fieldId}`, { method: 'PATCH', body });
      toast(`${label} saved. Testing…`);
    } else {
      if (!state.draftBox) { toast('Drag a box around the number first', 'error'); return; }
      body.box = state.draftBox;
      await api(`/api/devices/${state.deviceId}/fields`, { method: 'POST', body });
      toast(`${label} saved. Drag a box around the next number, or check the test below.`);
    }
    // Done with this number: the next box drawn makes a new one (click a number in the list to edit it).
    state.fieldId = null;
    state.draftBox = null;
    $('#field-form').classList.add('hidden');
    await refreshAll();
    await runTest();
  } catch (err) {
    toast(err.message, 'error');
  }
});

$('#field-cancel').addEventListener('click', () => {
  state.fieldId = null;
  state.draftBox = null;
  $('#field-form').classList.add('hidden');
  renderFields();
});

$('#field-delete').addEventListener('click', async () => {
  if (!state.fieldId || !confirm('Remove this number? Its past readings are kept.')) return;
  await api(`/api/fields/${state.fieldId}`, { method: 'DELETE' });
  state.fieldId = null;
  $('#field-form').classList.add('hidden');
  await refreshAll();
});

// --- test & reprocess -------------------------------------------------------------------------

const READER_NAMES = { auto: 'automatic', sevenseg: 'segment digits', tesseract: 'normal font' };

// In Automatic mode both readers look at the number. When they saw different things, one click
// tells the server which kind of display this is, and every later picture is read that way.
function whichNumber(r) {
  const c = r.candidates;
  if (r.recognizer !== 'auto' || !c || c.sevenseg === c.tesseract) return null;
  const choose = async recognizer => {
    try {
      await api(`/api/fields/${r.field_id}`, { method: 'PATCH', body: { recognizer } });
      toast(`Display type set to ${READER_NAMES[recognizer]}`);
      await refreshAll();
      if (state.fieldId === r.field_id) selectField(r.field_id);
      await runTest();
    } catch (err) {
      toast(err.message, 'error');
    }
  };
  const options = [['sevenseg', c.sevenseg], ['tesseract', c.tesseract]].filter(([, v]) => v);
  if (!options.length) return null;
  return el('div', { class: 'which-number' },
    el('span', {}, 'Which number is on the screen?'),
    ...options.map(([rec, v]) => el('button', { type: 'button', onclick: () => choose(rec) },
      el('strong', { class: 'num' }, v), ` (${READER_NAMES[rec]})`)),
    el('span', { class: 'muted small' }, 'Neither? Make the box tighter around the digits and test again.'));
}

async function runTest() {
  if (!state.frame) return;
  const panel = $('#test-panel');
  panel.classList.remove('hidden');
  $('#test-info').textContent = 'Reading…';
  try {
    const res = await api('/api/ocr/test', { method: 'POST', body: { camera_id: state.camId, frame_id: state.frame.id } });
    const names = Object.fromEntries((cam()?.devices || []).map(d => [d.id, d.name]));
    $('#test-info').textContent = `Picture taken ${fmtDateTime(res.captured_ms)}. Nothing is saved by a test.`;
    $('#test-results').replaceChildren(...(res.results.length ? res.results.map(r => el('div', { class: 'result' },
      el('div', { class: 'info' },
        el('strong', {}, `${names[r.device_id] || r.device_id} · ${metricLabel(r.metric, state.catalog)}`),
        el('span', { class: `pill ${r.status === 'ok' ? 'good' : 'warn'}` }, r.status === 'ok' ? 'Accepted' : 'Would need review'),
        el('span', {}, 'Read: ', el('strong', { class: 'num' }, r.raw_text || '(nothing)')),
        el('span', { class: 'muted' }, `confidence ${Math.round(r.confidence * 100)}%`),
        r.note ? el('span', { class: 'flag' }, r.note) : null,
        whichNumber(r)),
      r.crop ? el('figure', { style: 'margin:0' }, el('img', { src: r.crop, alt: 'Camera crop' }), el('figcaption', { class: 'muted small' }, 'Camera')) : el('span'),
      r.binary ? el('figure', { style: 'margin:0' }, el('img', { src: r.binary, alt: 'What the reader sees' }), el('figcaption', { class: 'muted small' }, 'What the reader sees (digits should be white and complete)')) : el('span'),
    )) : [el('p', { class: 'muted' }, 'No numbers marked on this camera yet.')]));
  } catch (err) {
    $('#test-info').textContent = `Test failed: ${err.message}`;
  }
}
$('#test').addEventListener('click', runTest);

$$('[data-reprocess]').forEach(btn => btn.addEventListener('click', async () => {
  if (!state.camId) return;
  const kind = btn.dataset.reprocess;
  const body = {};
  if (kind === '24h') body.since_ms = Date.now() - 86400e3;
  if (kind === 'run') {
    const active = (await api('/api/runs')).find(r => !r.ended_ms);
    if (!active) { toast('No test run is active', 'error'); return; }
    body.since_ms = active.started_ms;
  }
  if (!confirm('Re-read these pictures with the current settings? Automatic readings are replaced; reviewed ones are kept.')) return;
  const res = await api(`/api/cameras/${enc(state.camId)}/reprocess`, { method: 'POST', body });
  toast(`${res.frames_queued} pictures queued for re-reading`);
}));

$('#camera').addEventListener('change', async e => {
  state.deviceId = null;
  state.fieldId = null;
  history.replaceState(null, '', `#${enc(e.target.value)}`);
  await refreshAll(e.target.value);
});
$('#newest').addEventListener('click', async () => { await showNewestFrame(); renderFrameOverlay(); await renderScreen(); });

(async () => {
  state.catalog = await getCatalog();
  $('#field-form').metric.replaceChildren(...Object.entries(state.catalog.metrics).map(([k, v]) => el('option', { value: k }, v.label)));
  await refreshAll();
})();
