import { $, api, duration, el, fmtBytes, fmtDateTime, getCatalog, metricLabel, renderTopbar, toast } from './common.js';

renderTopbar('data.html');
let catalog;

const exportLinks = q => [
  el('a', { class: 'btn', href: `/api/export/readings.csv?${q}` }, 'Readings CSV'),
  el('a', { class: 'btn', href: `/api/export/frames.csv?${q}` }, 'Pictures manifest'),
  el('a', { class: 'btn', href: `/api/export/reference.csv?${q}` }, 'Reference CSV'),
];

async function loadRuns() {
  const runs = await api('/api/runs');
  const now = Date.now();
  $('#runs').replaceChildren(
    el('thead', {}, el('tr', {}, ['Run', 'Started', 'Length', 'Notes', 'Export'].map(h => el('th', {}, h)))),
    el('tbody', {}, runs.length ? runs.map(r => el('tr', {},
      el('td', {}, el('strong', {}, r.name), r.ended_ms ? null : el('div', {}, el('span', { class: 'pill live' }, 'Recording'))),
      el('td', {}, fmtDateTime(r.started_ms), el('div', { class: 'muted small' }, `by ${r.created_by || '–'}`)),
      el('td', {}, duration((r.ended_ms || now) - r.started_ms), r.ended_ms ? null : ' so far'),
      el('td', { class: 'small' }, r.notes || ''),
      el('td', {}, el('div', { class: 'row' }, ...exportLinks(`run_id=${r.id}`),
        r.ended_ms ? null : el('button', { type: 'button', class: 'danger', onclick: async () => {
          if (!confirm(`Stop "${r.name}"?`)) return;
          await api(`/api/runs/${r.id}/stop`, { method: 'POST' });
          loadRuns();
        } }, 'Stop'))))) : [el('tr', {}, el('td', { colspan: 5, class: 'muted' }, 'No test runs yet.'))]));
}

$('#run-form').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target;
  await api('/api/runs', { method: 'POST', body: { name: f.run_name.value.trim(), notes: f.notes.value.trim() || null } });
  f.reset();
  toast('Run started (any previous run was stopped)');
  loadRuns();
  loadAudit();
});

// --- custom range ---------------------------------------------------------------------------
function localInput(ms) {
  const d = new Date(ms - new Date(ms).getTimezoneOffset() * 60000);
  return d.toISOString().slice(0, 19);
}
function updateRangeLinks() {
  const f = $('#range-form');
  const from = new Date(f.from.value).getTime();
  const to = new Date(f.to.value).getTime();
  $('#range-links').replaceChildren(...(from && to && from < to ? exportLinks(`since_ms=${from}&until_ms=${to}`)
    : [el('span', { class: 'muted small' }, 'Pick a start and end time')]));
}
$('#range-form').from.value = localInput(Date.now() - 86400e3);
$('#range-form').to.value = localInput(Date.now());
$('#range-form').addEventListener('input', updateRangeLinks);
updateRangeLinks();

// --- reference data -------------------------------------------------------------------------
const offset = -new Date().getTimezoneOffset();
$('#ref-form').tz.value = offset ? `${offset > 0 ? '+' : '-'}${String(Math.floor(Math.abs(offset) / 60)).padStart(2, '0')}:${String(Math.abs(offset) % 60).padStart(2, '0')}` : 'UTC';
$('#push-example').textContent = `curl -X POST ${location.origin}/api/agent/reference \\
  -H "Authorization: Bearer $AGENT_TOKEN" -H "Content-Type: application/json" \\
  -d '{"source": "own-cloud", "rows": [
        {"device": "AQ-0042", "metric": "pm25", "t": "2026-11-03T10:00:00Z", "value": 12.0},
        {"device": "AQ-0042", "metric": "co2",  "t": "2026-11-03T10:00:00Z", "value": 640}]}'`;

$('#ref-form').addEventListener('submit', async e => {
  e.preventDefault();
  const form = new FormData(e.target);
  try {
    const res = await api('/api/reference/import', { method: 'POST', form });
    toast(`Imported ${res.added} new values (${res.rows - res.added} were already there)`);
    e.target.file.value = '';
    loadSources();
    loadAudit();
  } catch (err) { toast(err.message, 'error'); }
});

async function loadSources() {
  const rows = await api('/api/reference/sources');
  $('#sources').replaceChildren(
    el('thead', {}, el('tr', {}, ['Source', 'Device', 'Metric', 'Values', 'From', 'To', ''].map(h => el('th', {}, h)))),
    el('tbody', {}, rows.length ? rows.map(r => el('tr', {},
      el('td', {}, r.source), el('td', {}, r.device), el('td', {}, metricLabel(r.metric, catalog)),
      el('td', { class: 'numcell' }, r.count.toLocaleString()), el('td', {}, fmtDateTime(r.first_ms)), el('td', {}, fmtDateTime(r.last_ms)),
      el('td', {}, el('button', { type: 'button', class: 'danger', onclick: async () => {
        if (!confirm(`Delete all "${r.source}" data for ${r.device}?`)) return;
        await api(`/api/reference?source=${encodeURIComponent(r.source)}&device=${encodeURIComponent(r.device)}`, { method: 'DELETE' });
        loadSources();
      } }, 'Delete')))) : [el('tr', {}, el('td', { colspan: 7, class: 'muted' }, 'No reference data imported yet.'))]));
}

// --- picture browser ------------------------------------------------------------------------
let current = null;
async function showFrame(frame) {
  current = frame;
  const detail = await api(`/api/frames/${frame.id}`);
  const devices = Object.fromEntries((await api('/api/cameras')).flatMap(c => c.devices).map(d => [d.id, d.name]));
  $('#browse').replaceChildren(
    el('div', { class: 'small muted', style: 'margin-bottom:8px' },
      `Taken ${fmtDateTime(detail.captured_ms)} · received ${fmtDateTime(detail.received_ms)} · ${fmtBytes(detail.size_bytes)} · SHA-256 `,
      el('code', { style: 'overflow-wrap:anywhere' }, detail.sha256)),
    el('img', { src: `/api/frames/${detail.id}/image.jpg`, alt: 'Stored camera picture', style: 'width:100%;border-radius:8px;background:#000' }),
    el('div', { class: 'table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {}, ['Monitor', 'Metric', 'Machine read', 'Value used', 'Status', 'Confidence'].map(h => el('th', {}, h)))),
      el('tbody', {}, detail.readings.length ? detail.readings.map(r => el('tr', {},
        el('td', {}, devices[r.device_id] || r.device_id), el('td', {}, metricLabel(r.metric, catalog)),
        el('td', { class: 'numcell' }, r.raw_text || '–'), el('td', { class: 'numcell' }, r.final_value ?? '–'),
        el('td', {}, r.status), el('td', { class: 'numcell' }, `${Math.round(r.confidence * 100)}%`)))
        : [el('tr', {}, el('td', { colspan: 6, class: 'muted' }, `No readings for this picture (${detail.ocr_status}).`))]))),
  );
}
$('#browse-form').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target;
  try {
    await showFrame(await api(`/api/frames/nearest?camera_id=${encodeURIComponent(f.camera.value)}&t_ms=${new Date(f.at.value).getTime()}`));
  } catch (err) { toast(err.message, 'error'); }
});
async function step(dir) {
  if (!current) return;
  const t = current.captured_ms + (dir > 0 ? 1 : -1);
  const list = dir < 0
    ? await api(`/api/frames?camera_id=${encodeURIComponent(current.camera_id)}&before_ms=${current.captured_ms}&limit=1`)
    : [await api(`/api/frames/nearest?camera_id=${encodeURIComponent(current.camera_id)}&t_ms=${t + 1}`)];
  const next = list[0];
  if (next && next.id !== current.id && (dir < 0 || next.captured_ms > current.captured_ms)) {
    $('#browse-form').at.value = localInput(next.captured_ms);
    showFrame(next);
  } else toast(dir < 0 ? 'No earlier picture' : 'No later picture');
}
$('#prev').addEventListener('click', () => step(-1));
$('#next').addEventListener('click', () => step(1));

// --- audit ----------------------------------------------------------------------------------
const ACTIONS = {
  'reading.review': 'Reviewed a reading', 'reading.flag': 'Flagged a reading', 'run.start': 'Started a run', 'run.stop': 'Stopped a run',
  'run.edit': 'Edited a run', 'device.create': 'Added a monitor', 'device.edit': 'Changed a monitor', 'device.remove': 'Removed a monitor',
  'field.create': 'Added a number box', 'field.edit': 'Changed a number box', 'field.remove': 'Removed a number box',
  'camera.reprocess': 'Re-read past pictures', 'reference.import': 'Imported reference data', 'reference.delete': 'Deleted reference data',
};
function summary(a) {
  const d = a.detail || {};
  if (a.action === 'reading.review') return `${d.action}: machine read "${d.ocr_text}", value now ${d.after?.final_value ?? '–'} (${d.after?.status})`;
  if (a.action.startsWith('run.')) return d.name || `run #${d.run_id}`;
  if (a.action === 'camera.reprocess') return `${d.camera_id}: ${d.frames} pictures`;
  if (a.action === 'reference.import') return `${d.filename}: ${d.added} new of ${d.rows}`;
  if (a.action.startsWith('device.')) return d.name || `monitor #${d.device_id}`;
  return '';
}
async function loadAudit() {
  const rows = await api('/api/audit?limit=100');
  $('#audit').replaceChildren(
    el('thead', {}, el('tr', {}, ['When', 'Who', 'What', 'Details'].map(h => el('th', {}, h)))),
    el('tbody', {}, rows.map(a => el('tr', {}, el('td', { class: 'small' }, fmtDateTime(a.at_ms)), el('td', {}, a.actor || '–'),
      el('td', {}, ACTIONS[a.action] || a.action), el('td', { class: 'small' }, summary(a))))));
}

(async () => {
  catalog = await getCatalog();
  const cams = await api('/api/cameras');
  $('#browse-form').camera.replaceChildren(...cams.map(c => el('option', { value: c.id }, c.name || c.id)));
  $('#browse-form').at.value = localInput(Date.now());
  await Promise.all([loadRuns(), loadSources(), loadAudit()]);
})();
