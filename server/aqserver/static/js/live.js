import {
  $, ago, api, duration, el, fmtBytes, fmtDateTime, fmtTime, fmtValue, getCatalog, icon, ImagePoller, metricLabel, renderTopbar, toast,
  updateReviewCount,
} from './common.js';
import { MetricChart } from './chart.js';

renderTopbar('index.html');

const RANGES = [['1h', 3600e3], ['6h', 6 * 3600e3], ['24h', 86400e3], ['7d', 7 * 86400e3], ['run', null]];
const state = { ov: null, cards: new Map(), metric: null, range: '6h', catalog: null };
try {
  state.metric = localStorage.getItem('live.metric');
  state.range = localStorage.getItem('live.range') || '6h';
} catch { /* storage blocked */ }

const chart = new MetricChart($('#chart'));

// --- status strip ---------------------------------------------------------------------------

// level: '', 'good', 'warn' or 'bad' (a coloured edge; the text always says it too).
function tile(label, value, sub, extra, level = '') {
  return el('div', { class: `stat ${level}${label === 'Test run' ? ' stat-wide' : ''}` }, el('div', { class: 'label' }, label), el('div', { class: 'value' }, value),
    sub ? el('div', { class: 'sub' }, sub) : null, extra || null);
}

function runTile(ov) {
  if (ov.run) {
    const stop = el('button', { type: 'button', class: 'danger', onclick: async () => {
      if (!confirm(`Stop test run "${ov.run.name}"?`)) return;
      await api(`/api/runs/${ov.run.id}/stop`, { method: 'POST' });
      toast('Test run stopped');
      refresh();
    } }, 'Stop run');
    return tile('Test run', el('span', { class: 'pill live' }, 'Recording'),
      `${ov.run.name} · ${duration(ov.server_time_ms - ov.run.started_ms)} so far`, el('div', { class: 'row', style: 'margin-top:8px' }, stop), 'good');
  }
  const input = el('input', { type: 'text', id: 'run-name', placeholder: 'Name, e.g. Office 7 days', 'aria-label': 'Run name', style: 'flex:1 1 120px' });
  const form = el('form', { class: 'row', style: 'margin-top:8px', onsubmit: async e => {
    e.preventDefault();
    if (!input.value.trim()) { input.focus(); return; }
    await api('/api/runs', { method: 'POST', body: { name: input.value.trim() } });
    toast('Test run started');
    refresh();
  } }, input, el('button', { class: 'primary', type: 'submit' }, 'Start'));
  return tile('Test run', el('span', { class: 'pill' }, 'Not recording a run'),
    'Cameras record anyway, but offline alerts are only raised during a run, and Results and exports use its period.', form);
}

function renderStrip(ov) {
  const cams = ov.cameras;
  const online = cams.filter(c => c.online).length;
  const got = cams.reduce((a, c) => a + c.frames_last_hour, 0);
  const expected = cams.reduce((a, c) => a + (c.expected_last_hour || 0), 0);
  const completeness = expected ? Math.min(100, Math.round((100 * got) / expected)) : null;
  const agents = ov.agents.map(a => `${a.id}: ${a.online ? 'online' : 'offline'}` +
    (a.online && a.clock_offset_ms !== null ? `, clock ${(a.clock_offset_ms / 1000).toFixed(1)} s` : '') +
    (a.backlog ? `, ${a.backlog} waiting to upload` : '')).join(' · ');
  const strip = $('#strip');
  // Keep the run tile's form intact while the viewer is typing in it.
  if (strip.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return;
  const camLevel = !cams.length ? '' : online === cams.length ? 'good' : online ? 'warn' : 'bad';
  const stillsLevel = completeness === null ? '' : completeness >= 90 ? 'good' : completeness >= 60 ? 'warn' : 'bad';
  const disk = Math.round(ov.disk.used_fraction * 100);
  strip.replaceChildren(
    runTile(ov),
    tile('Cameras online', el('span', { class: `pill ${camLevel}` }, `${online} of ${cams.length}`),
      agents || 'No capture computer has connected yet', null, camLevel),
    tile('Pictures received, last hour', completeness === null ? '–' : `${completeness}%`,
      expected ? `${got} of ${expected} expected` : 'Shown once cameras report their interval', null, stillsLevel),
    tile('Readings to review', el('a', { href: 'review.html', title: 'Open the review queue' }, String(ov.review_pending)),
      ov.review_pending ? `${ov.ocr_pending_frames} pictures still being read · open Review to check them` : `${ov.ocr_pending_frames} pictures still being read`,
      null, ov.review_pending ? 'warn' : ''),
    tile('Server disk', `${disk}% used`, `${fmtBytes(ov.disk.free)} free${disk >= 85 ? ' · running low' : ''}`, null,
      disk >= 95 ? 'bad' : disk >= 85 ? 'warn' : ''),
  );
}

function renderAlerts(ov) {
  $('#alerts').replaceChildren(...ov.alerts.map(a => el('div', { class: 'alert', role: 'alert' },
    el('span', { class: 'icon', 'aria-hidden': 'true' }, '!'),
    el('span', {}, el('strong', {}, 'Alert: '), a.message),
    el('span', { class: 'when' }, `since ${ago(a.opened_ms, ov.server_time_ms)}`))));
}

// --- camera cards ---------------------------------------------------------------------------

const visibility = new IntersectionObserver(entries => {
  for (const e of entries) {
    const card = [...state.cards.values()].find(c => c.node === e.target);
    if (card) card.poller.visible = e.isIntersecting;
  }
});

function makeCard(cam) {
  const img = el('img', { alt: `Camera ${cam.name || cam.id}` });
  const badge = el('span', { class: 'pill badge' }, 'Connecting…');
  const empty = el('div', { class: 'empty' }, 'No picture yet from this camera.');
  const viewport = el('div', { class: 'viewport', role: 'button', tabindex: '0', title: 'Open large view' }, img, badge, empty);
  const title = el('h2');
  const status = el('span', { class: 'pill' });
  const meta = el('div', { class: 'cam-meta' });
  const devices = el('div', { class: 'devices' });
  const node = el('article', { class: 'cam' }, el('div', { class: 'cam-head' }, title, status), viewport, meta, devices);
  const poller = new ImagePoller(img, () => `/api/cameras/${encodeURIComponent(cam.id)}/live.jpg?t=${Date.now()}`, {
    onInfo: info => {
      empty.classList.toggle('hidden', info.ok);
      if (!info.ok) { badge.className = 'pill badge'; badge.textContent = info.status === 204 || info.status === 404 ? 'No picture yet' : 'Unavailable'; return; }
      if (info.kind === 'live') { badge.className = 'pill badge live'; badge.textContent = 'Live'; }
      else { badge.className = 'pill badge'; badge.textContent = `Picture · ${ago(info.captured)}`; }
    },
  });
  const open = () => openZoom(cam.id);
  viewport.addEventListener('click', open);
  viewport.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  visibility.observe(node);
  poller.start();
  return { node, title, status, meta, devices, poller };
}

function readingNode(f, now) {
  const r = f.latest;
  const label = f.label || metricLabel(f.metric, state.catalog);
  if (!r) return el('div', { class: 'reading' }, el('div', { class: 'm' }, label), el('div', { class: 'v muted' }, '–'),
    el('div', { class: 't' }, 'no reading yet'));
  const review = r.status === 'review';
  const shown = review ? (r.raw_text || '?') : fmtValue(r.value, f.metric);
  return el('div', { class: `reading${review ? ' review' : ''}` },
    el('div', { class: 'm' }, label),
    el('div', { class: 'v num' }, shown, f.unit ? el('span', { class: 'u' }, f.unit) : null),
    review ? el('div', { class: 'flag' }, '⚠ needs review') : el('div', { class: 't' }, ago(r.t_ms, now)));
}

// Camera-shift detection: say so when a monitor has moved in the picture (always with text, not colour alone).
function alignBadge(d, cam) {
  const a = d.alignment || {};
  if (a.status === 'lost') {
    return el('a', { class: 'pill bad', href: `calibrate.html#${encodeURIComponent(cam.id)}`, title: 'Readings from this monitor go to review until it is found again' }, 'Not found in picture');
  }
  if (a.status === 'adjusted' && a.shift_px >= 8) {
    return el('a', { class: 'pill warn', href: `calibrate.html#${encodeURIComponent(cam.id)}`, title: 'Readings are corrected automatically. Check the setup, then accept the new position on Calibrate.' },
      `View shifted ${Math.round(a.shift_px)} px · auto-corrected`);
  }
  return null;
}

function updateCard(card, cam, ov) {
  card.title.textContent = cam.name || cam.id;
  card.status.className = `pill ${cam.stopped ? '' : cam.online ? 'good' : 'bad'}`;
  card.status.textContent = cam.stopped ? 'Stopped' : cam.online ? 'Online' : 'Offline';
  const st = cam.status || {};
  const bits = [
    `Computer: ${cam.agent_id || '–'}`,
    `Last picture: ${ago(cam.last_frame_ms, ov.server_time_ms)}`,
    cam.expected_last_hour ? `Pictures last hour: ${cam.frames_last_hour}/${cam.expected_last_hour}` : null,
    st.fps ? `${st.fps} fps` : null,
    st.brightness !== undefined && st.brightness !== null ? `Brightness ${Math.round(st.brightness)}` : null,
    st.width ? `${st.width}×${st.height}` : null,
    cam.avg_still_bytes ? `Avg picture ${fmtBytes(cam.avg_still_bytes)}` : null,
    cam.mask_enabled ? 'Storing marked monitors only' : null,
  ].filter(Boolean);
  card.meta.replaceChildren(...bits.map(b => el('span', {}, b)),
    ...(st.error ? [el('span', { class: 'flag' }, `⚠ ${st.error}`)] : []));
  if (!cam.devices.length) {
    card.devices.replaceChildren(el('div', { class: 'device muted small' },
      'No monitors marked on this camera yet. ', el('a', { href: `calibrate.html#${encodeURIComponent(cam.id)}` }, 'Calibrate it')));
    return;
  }
  card.devices.replaceChildren(...cam.devices.map(d => el('div', { class: 'device' },
    el('div', { class: 'device-name' }, d.name, d.model ? el('span', { class: 'tag' }, d.model) : null,
      d.is_own ? el('span', { class: 'tag' }, 'Your device') : null, alignBadge(d, cam)),
    el('div', { class: 'readings' }, d.fields.map(f => readingNode(f, ov.server_time_ms))))));
}

// Getting started: shown until a camera is recording, its monitors are marked and a run is on.
function renderOnboarding(ov) {
  const hasCam = ov.cameras.length > 0;
  const devices = ov.cameras.flatMap(c => c.devices);
  const hasNumbers = devices.some(d => d.fields.length);
  const steps = [
    [hasCam, 'Choose a camera', ['On ', el('a', { href: 'cameras.html' }, 'Cameras'), ', pick a webcam on the computer next to the monitors (or open the recorder in a browser).']],
    [hasNumbers, 'Mark the monitors', ['On ', el('a', { href: 'calibrate.html' }, 'Calibrate'), ', click the 4 corners of each screen, box each number, press Test reading.']],
    [Boolean(ov.run), 'Start a test run', ['Name it in the Test run box above and press Start. Charts, Results and exports use the run period.']],
    [false, 'During the test', ['Mark events (incense, window…) under the chart, clear ', el('a', { href: 'review.html' }, 'Review'), ' daily, do a few spot checks.']],
  ];
  const panel = $('#onboard');
  const done = hasCam && hasNumbers && ov.run;
  let dismissed = false;
  try { dismissed = localStorage.getItem('onboard.hide') === '1'; } catch { /* ignore */ }
  panel.classList.toggle('hidden', Boolean(done) || dismissed);
  if (done || dismissed) return;
  const next = steps.findIndex(([ok]) => !ok);
  panel.replaceChildren(
    el('div', { class: 'panel-head', style: 'margin-bottom:0' }, el('h2', {}, 'Getting started'),
      el('span', { class: 'small muted' }, 'four steps from webcam to white-paper data'), el('div', { class: 'spacer' }),
      el('button', { type: 'button', class: 'iconbtn', onclick: () => {
        try { localStorage.setItem('onboard.hide', '1'); } catch { /* ignore */ }
        panel.classList.add('hidden');
      } }, 'Hide')),
    el('ol', { class: 'steps' }, steps.map(([ok, title, what], i) => el('li', { class: ok ? 'done' : i === next ? 'next' : '' },
      el('span', { class: 'num-badge', 'aria-hidden': 'true' }, ok ? icon('check', 16) : String(i + 1)),
      el('strong', {}, title, ok ? el('span', { class: 'sr-only' }, ' (done)') : null),
      el('div', { class: 'what' }, what)))),
  );
}

function renderCams(ov) {
  const host = $('#cams');
  $('#no-cams')?.classList.toggle('hidden', ov.cameras.length > 0);
  for (const cam of ov.cameras) {
    let card = state.cards.get(cam.id);
    if (!card) {
      card = makeCard(cam);
      state.cards.set(cam.id, card);
      host.append(card.node);
    }
    updateCard(card, cam, ov);
  }
}

// --- zoomed live view -----------------------------------------------------------------------

let zoomPoller = null;
function openZoom(camId) {
  const cam = state.ov.cameras.find(c => c.id === camId);
  $('#zoom-title').textContent = cam ? cam.name || cam.id : camId;
  const badge = $('#zoom-badge');
  zoomPoller = new ImagePoller($('#zoom-img'), () => `/api/cameras/${encodeURIComponent(camId)}/live.jpg?t=${Date.now()}`, {
    fastMs: 400,
    onInfo: info => {
      if (!info.ok) { badge.className = 'pill bad'; badge.textContent = 'Unavailable'; return; }
      badge.className = info.kind === 'live' ? 'pill live' : 'pill';
      badge.textContent = info.kind === 'live' ? 'Live' : `Picture · ${ago(info.captured)} (live view starting…)`;
    },
  });
  zoomPoller.start();
  $('#zoom').showModal();
}
$('#zoom').addEventListener('close', () => { zoomPoller?.stop(); zoomPoller = null; });
$('#zoom-close').addEventListener('click', () => $('#zoom').close());

// --- chart ----------------------------------------------------------------------------------

async function loadMetrics() {
  const metrics = await api('/api/metrics');
  if (!metrics.length) {
    $('#metric-seg').replaceChildren(el('span', { class: 'muted small' }, 'Calibrate a camera to see readings here.'));
    return [];
  }
  if (!metrics.some(m => m.metric === state.metric)) state.metric = metrics[0].metric;
  $('#metric-seg').replaceChildren(...metrics.map(m => el('button', {
    type: 'button', 'aria-pressed': String(m.metric === state.metric),
    onclick: () => { state.metric = m.metric; save(); loadMetrics(); loadChart(); },
  }, m.label)));
  return metrics;
}

function renderRanges() {
  $('#range-seg').replaceChildren(...RANGES.filter(([k]) => k !== 'run' || state.ov?.run).map(([k]) => el('button', {
    type: 'button', 'aria-pressed': String(k === state.range),
    onclick: () => { state.range = k; save(); renderRanges(); loadChart(); loadEvents().catch(() => {}); },
  }, k === 'run' ? 'This run' : k)));
}

function save() {
  try { localStorage.setItem('live.metric', state.metric); localStorage.setItem('live.range', state.range); } catch { /* ignore */ }
}

let chartBusy = false;
async function loadChart() {
  if (!state.metric || !state.ov || chartBusy) return;
  chartBusy = true;
  try {
    const now = Date.now();
    let since;
    if (state.range === 'run' && state.ov.run) since = state.ov.run.started_ms;
    else since = now - (RANGES.find(([k]) => k === state.range)?.[1] || 6 * 3600e3);
    const params = new URLSearchParams({ metric: state.metric, since_ms: since, until_ms: now,
      include_review: $('#include-review').checked, max_points: Math.min(1500, Math.max(300, $('#chart').clientWidth)) });
    const resp = await api(`/api/series?${params}`);
    const deviceKeys = state.ov.cameras.flatMap(c => c.devices).map(d => d.id).sort((a, b) => a - b).map(id => `dev:${id}`);
    const refKeys = resp.series.filter(s => s.kind === 'reference').map(s => s.key).sort();
    const info = state.catalog.metrics[state.metric] || {};
    chart.render(resp, [...deviceKeys, ...refKeys], info.unit, state.metric);
    const b = resp.bucket_ms / 1000;
    $('#chart-note').textContent = `Each point is the average over ${b < 60 ? `${b} s` : b < 3600 ? `${b / 60} min` : `${b / 3600} h`}. ` +
      'Solid lines: numbers read from the camera pictures. Dashed lines: reference data you imported. Drag across the chart to zoom; double-click to reset.';
  } catch (e) {
    $('#chart-note').textContent = `Could not load chart: ${e.message}`;
  } finally {
    chartBusy = false;
  }
}
$('#include-review').addEventListener('change', loadChart);

// --- events -----------------------------------------------------------------------------------
// The team marks what happens during the test; markers are drawn on the chart and go into the
// exports and the report.

const QUICK = 6; // presets shown as buttons; the rest are suggestions in the form

function localInput(ms) {
  return new Date(ms - new Date(ms).getTimezoneOffset() * 60000).toISOString().slice(0, 19);
}

async function addEvent(body) {
  const ev = await api('/api/events', { method: 'POST', body });
  toast(`Marked: ${ev.label}`, '', { label: 'Undo', onClick: () => deleteEvent(ev, true) });
  await loadEvents();
  return ev;
}

async function deleteEvent(ev, quiet = false) {
  if (!quiet && !confirm(`Remove the event “${ev.label}”?`)) return;
  await api(`/api/events/${ev.id}`, { method: 'DELETE' });
  if (quiet) toast('Event removed');
  await loadEvents();
}

async function endEventNow(ev) {
  await api(`/api/events/${ev.id}`, { method: 'PATCH', body: { end_ms: Date.now() } });
  await loadEvents();
}

let presets = [];
async function loadEvents() {
  const since = chartSince();
  const res = await api(`/api/events?${new URLSearchParams({ since_ms: since, until_ms: Date.now() + 60000 })}`);
  presets = res.presets;
  if (!$('#event-presets').children.length) {
    $('#event-presets').replaceChildren(...presets.slice(0, QUICK).map(label =>
      el('button', { type: 'button', onclick: () => addEvent({ label }).catch(e => toast(e.message, 'error')) }, label)));
    const list = el('datalist', { id: 'event-suggestions' }, ...presets.map(p => el('option', { value: p })));
    $('#event-form').append(list);
    $('#event-form').label.setAttribute('list', 'event-suggestions');
  }
  chart.setEvents(res.events);
  const evs = [...res.events].reverse();
  $('#event-list').replaceChildren(...(evs.length ? [el('strong', { class: 'small' }, 'Events in this view'), ...evs.map(ev => el('div', { class: 'ev' },
    el('time', {}, ev.end_ms ? `${fmtDateTime(ev.t_ms)} – ${fmtTime(ev.end_ms)}` : fmtDateTime(ev.t_ms)),
    el('strong', {}, ev.label),
    ev.notes ? el('span', { class: 'muted' }, ev.notes) : null,
    el('span', { class: 'muted' }, ev.created_by ? `marked by ${ev.created_by}` : ''),
    el('span', { class: 'spacer' }),
    !ev.end_ms && Date.now() - ev.t_ms < 6 * 3600e3 ? el('button', { type: 'button', onclick: () => endEventNow(ev).catch(e => toast(e.message, 'error')), title: 'Set when it stopped' }, 'Ended now') : null,
    el('button', { type: 'button', class: 'danger', onclick: () => deleteEvent(ev).catch(e => toast(e.message, 'error')) }, 'Remove')))] : []));
}

function chartSince() {
  const now = Date.now();
  if (state.range === 'run' && state.ov?.run) return state.ov.run.started_ms;
  return now - (RANGES.find(([k]) => k === state.range)?.[1] || 6 * 3600e3);
}

$('#event-open').addEventListener('click', () => {
  const f = $('#event-form');
  f.classList.toggle('hidden');
  if (!f.classList.contains('hidden')) {
    f.at.value = localInput(Date.now());
    f.label.focus();
  }
});
$('#event-cancel').addEventListener('click', () => $('#event-form').classList.add('hidden'));
$('#event-form').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target;
  try {
    await addEvent({ label: f.label.value, t_ms: new Date(f.at.value).getTime(),
      end_ms: f.until.value ? new Date(f.until.value).getTime() : null, notes: f.notes.value || null });
    f.reset();
    f.classList.add('hidden');
  } catch (err) { toast(err.message, 'error'); }
});
$('#table-mode').addEventListener('change', e => chart.setTableMode(e.target.checked));

// --- main loop ------------------------------------------------------------------------------

async function refresh() {
  try {
    const ov = await api('/api/overview');
    state.ov = ov;
    renderStrip(ov);
    renderOnboarding(ov);
    renderAlerts(ov);
    renderCams(ov);
    updateReviewCount(ov.review_pending);
    document.title = ov.alerts.length ? `(${ov.alerts.length}) Live monitor` : 'Live monitor';
  } catch (e) {
    toast(`Cannot reach the server: ${e.message}`, 'error');
  }
}

(async () => {
  state.catalog = await getCatalog();
  await refresh();
  renderRanges();
  await loadMetrics();
  await loadChart();
  await loadEvents().catch(() => {});
  setInterval(refresh, 3000);
  setInterval(() => { renderRanges(); loadChart(); loadEvents().catch(() => {}); }, 15000);
  setInterval(loadMetrics, 60000);
})();
