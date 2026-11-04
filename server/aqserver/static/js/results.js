import { $, api, cssVar, duration, el, fmtDateTime, fmtValue, getCatalog, metricLabel, renderTopbar, toast } from './common.js';
import { MetricChart } from './chart.js';

renderTopbar('results.html');

const SVGNS = 'http://www.w3.org/2000/svg';
const SLOTS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8'];
const AVG_LABEL = { 60: '1-minute', 300: '5-minute', 900: '15-minute', 3600: '1-hour', 86400: '24-hour' };
const chart = new MetricChart($('#chart'), { height: 300 });
const state = { res: null, runs: [], order: [], catalog: null };
const form = $('#controls');

const num = (v, d = 2) => (v === null || v === undefined ? '–' : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 }));
const pct = v => (v === null || v === undefined ? '–' : `${num(v, 1)}%`);

function localInput(ms) {
  return new Date(ms - new Date(ms).getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function params() {
  const p = new URLSearchParams({ avg_s: form.avg.value, tz: -new Date().getTimezoneOffset() });
  if (form.run.value === 'custom') {
    p.set('since_ms', new Date(form.from.value).getTime());
    p.set('until_ms', new Date(form.to.value).getTime());
  } else p.set('run_id', form.run.value);
  if (form.metric.value) p.set('metric', form.metric.value);
  if (form.reference.value) p.set('reference', form.reference.value);
  return p;
}

async function init() {
  state.catalog = await getCatalog();
  state.runs = await api('/api/runs');
  form.run.replaceChildren(...state.runs.map(r => el('option', { value: r.id }, `${r.name}${r.ended_ms ? '' : ' (running)'}`)),
    el('option', { value: 'custom' }, 'Custom time range…'));
  if (!state.runs.length) form.run.value = 'custom';
  form.from.value = localInput(Date.now() - 86400e3);
  form.to.value = localInput(Date.now());
  const metrics = await api('/api/metrics');
  form.metric.replaceChildren(...metrics.map(m => el('option', { value: m.metric }, m.label)));
  toggleCustom();
  await load();
}

function toggleCustom() {
  document.querySelectorAll('.custom').forEach(n => n.classList.toggle('hidden', form.run.value !== 'custom'));
}

async function load() {
  try {
    const res = await api(`/api/results?${params()}`);
    state.res = res;
    form.reference.replaceChildren(...res.series.map(s => el('option', { value: s.key }, `${s.label}${s.kind === 'reference' ? ' · imported' : ''}`)));
    if (res.reference.key) form.reference.value = res.reference.key;
    const devKeys = res.devices.map(d => `dev:${d.id}`);
    const refKeys = res.series.filter(s => s.kind === 'reference').map(s => s.key).sort();
    state.order = [...devKeys, ...refKeys];
    render(res);
  } catch (e) {
    toast(e.message, 'error');
  }
}

function colorFor(key) {
  const i = state.order.indexOf(key);
  return i >= 0 && i < SLOTS.length ? cssVar(SLOTS[i]) : cssVar('--muted');
}

function render(res) {
  const q = params().toString();
  const unit = res.unit;
  // summary
  const run = res.run;
  const usedDevices = res.devices.filter(d => res.series.some(s => s.key === `dev:${d.id}`));
  $('#summary').replaceChildren(
    el('h2', {}, run ? run.name : 'Custom time range'),
    el('dl', { class: 'kv', style: 'margin-top:10px' },
      el('dt', {}, 'Period'), el('dd', {}, `${fmtDateTime(res.since_ms)} – ${fmtDateTime(res.until_ms)} (${duration(res.until_ms - res.since_ms)})`),
      run?.notes ? [el('dt', {}, 'Notes'), el('dd', {}, run.notes)] : null,
      el('dt', {}, 'Reading'), el('dd', {}, `${res.metric_label}${unit ? ` (${unit})` : ''}, ${AVG_LABEL[res.avg_s]} averages`),
      el('dt', {}, 'Reference'), el('dd', {}, res.reference.label || '–'),
      el('dt', {}, 'Monitors'), el('dd', {}, usedDevices.map(d => `${d.name}${d.model ? ` (${d.model})` : ''}${d.is_own ? ' [ours]' : ''}`).join(', ') || '–'),
      el('dt', {}, 'Events'), el('dd', {}, res.events.length ? el('ul', { class: 'plain' }, res.events.map(e => el('li', {},
        el('span', { class: 'muted' }, e.end_ms ? `${fmtDateTime(e.t_ms)} – ${fmtDateTime(e.end_ms)}` : fmtDateTime(e.t_ms)), ' ', e.label,
        e.notes ? el('span', { class: 'muted' }, ` (${e.notes})`) : null)))
        : el('span', { class: 'muted' }, 'none marked (mark them on the Live page while the test runs)'))),
  );
  $('#report').href = `/api/results/report.docx?${q}`;
  // stats table
  const cols = [
    ['Monitor', null], ['Pairs', 'Averaging periods where both this monitor and the reference have data'],
    ['Complete', 'Share of averaging periods in which this monitor has valid data'],
    ['Mean', 'Average of this monitor over the paired periods'], ['Slope', 'Regression slope against the reference; 1 = same scale'],
    ['Intercept', 'Regression intercept; 0 = no constant offset'], ['R²', 'How well it follows the reference (1 = perfectly)'],
    ['RMSE', 'Root-mean-square error vs reference'], ['NRMSE', 'RMSE divided by the reference mean'],
    ['MAE', 'Mean absolute error'], ['Bias', 'Average over (+) or under (−) reading vs reference'],
    ['Delay', 'Estimated delay vs the reference from 30 s data (+ = reacts later); blank when the data has no clear changes'],
  ];
  const rows = res.rows.filter(r => !r.is_reference);
  const refRow = res.rows.find(r => r.is_reference);
  $('#stats').replaceChildren(
    el('thead', {}, el('tr', {}, cols.map(([h, tip], i) => el('th', { class: i ? 'numcell' : null, title: tip }, h)))),
    el('tbody', {}, rows.length ? rows.map(r => el('tr', {},
      el('td', {}, el('span', { class: 'swatch-dot', style: `background:${colorFor(r.key)}` }), ' ', r.label,
        r.model ? el('div', { class: 'small muted' }, r.model) : null),
      el('td', { class: 'numcell' }, r.n ?? '–'), el('td', { class: 'numcell' }, pct(r.completeness_pct)),
      el('td', { class: 'numcell' }, num(r.mean)), el('td', { class: 'numcell' }, num(r.slope)),
      el('td', { class: 'numcell' }, num(r.intercept)), el('td', { class: 'numcell' }, num(r.r2, 3)),
      el('td', { class: 'numcell' }, num(r.rmse)), el('td', { class: 'numcell' }, pct(r.nrmse_pct)),
      el('td', { class: 'numcell' }, num(r.mae)),
      el('td', { class: 'numcell' }, r.bias === null || r.bias === undefined ? '–' : `${r.bias > 0 ? '+' : ''}${num(r.bias)} (${r.bias_pct > 0 ? '+' : ''}${pct(r.bias_pct)})`),
      el('td', { class: 'numcell' }, r.lag_s === null || r.lag_s === undefined ? '–' : `${r.lag_s > 0 ? '+' : ''}${r.lag_s} s`),
    )) : [el('tr', {}, el('td', { colspan: cols.length, class: 'muted' }, 'Nothing to compare in this period.'))]));
  $('#stats-help').textContent = (refRow ? `Reference ${refRow.label}: data in ${pct(refRow.completeness_pct)} of periods, mean ${num(refRow.mean ?? null)}. ` : '') +
    `All values in ${unit || 'the reading’s units'} except percentages. Hover a column title for its meaning. Slope 1 and intercept 0 mean the same scale; R² near 1 means it follows the changes.`;
  $('#downloads').replaceChildren(
    el('a', { class: 'btn', href: `/api/results/stats.csv?${q}` }, 'Statistics CSV'),
    el('a', { class: 'btn', href: `/api/results/pairs.csv?${q}` }, 'Averaged data CSV'),
    el('a', { class: 'btn', href: `/api/export/events.csv?${q}` }, 'Events CSV'));

  // time series
  $('#ts-title').textContent = `Over time (${AVG_LABEL[res.avg_s]} averages)`;
  chart.events = res.events;
  chart.render({ bucket_ms: res.avg_s * 1000, series: res.series }, state.order, unit, res.metric);

  // scatter plots
  $('#scatter').replaceChildren(...(rows.length ? rows.filter(r => res.scatter[r.key]?.length).map(r => scatterCard(r, res)) :
    [el('p', { class: 'muted' }, 'No paired data.')]));

  renderHumidity(res);
  renderQuality(res);
  $('#methods').textContent = res.methods;
}

// Low-cost PM sensors often read high in humid air: agreement per humidity band.
function renderHumidity(res) {
  const h = res.humidity;
  $('#humidity-panel').classList.toggle('hidden', !h);
  if (!h) return;
  const unit = res.unit ? ` ${res.unit}` : '';
  $('#humidity-help').textContent = `Relative humidity from ${h.source}. Each cell: mean bias against the reference (and in %), ` +
    'mean absolute error, and the number of averaging periods. A bias that grows with humidity is a known effect in many particle sensors.';
  $('#humidity').replaceChildren(
    el('thead', {}, el('tr', {}, el('th', {}, 'Monitor'), h.bands.map(b => el('th', { class: 'numcell' }, `Humidity ${b}`)))),
    el('tbody', {}, h.rows.map(r => el('tr', {},
      el('td', {}, el('span', { class: 'swatch-dot', style: `background:${colorFor(r.key)}` }), ' ', r.label),
      r.bands.map(b => el('td', { class: 'numcell' }, b.n
        ? [el('div', {}, `${b.bias > 0 ? '+' : ''}${num(b.bias)}${unit} (${b.bias_pct > 0 ? '+' : ''}${pct(b.bias_pct)})`),
          el('div', { class: 'small muted' }, `MAE ${num(b.mae)} · n ${b.n}`)]
        : el('span', { class: 'muted' }, '–')))))));
}

function niceStep(range) {
  const raw = range / 5;
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  return [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || mag * 10;
}

function scatterCard(r, res) {
  const pts = res.scatter[r.key];
  const W = 320, H = 300, m = { l: 46, r: 10, t: 10, b: 40 };
  const all = pts.flat();
  const lo0 = Math.min(...all), hi0 = Math.max(...all);
  const lo = lo0 >= 0 ? 0 : lo0 - (hi0 - lo0) * 0.05;
  const step = niceStep(hi0 - lo || 1);
  const hi = Math.ceil((hi0 + (hi0 - lo) * 0.03) / step) * step;
  const sx = v => m.l + ((v - lo) / (hi - lo)) * (W - m.l - m.r);
  const sy = v => H - m.b - ((v - lo) / (hi - lo)) * (H - m.t - m.b);
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `${r.label} against ${res.reference.label}: slope ${r.slope}, R² ${r.r2}`);
  const add = (tag, attrs, text) => {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text !== undefined) n.textContent = text;
    svg.append(n);
    return n;
  };
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) {
    add('line', { x1: m.l, x2: W - m.r, y1: sy(v), y2: sy(v), stroke: 'var(--grid)', 'stroke-width': 1 });
    add('line', { y1: m.t, y2: H - m.b, x1: sx(v), x2: sx(v), stroke: 'var(--grid)', 'stroke-width': 1 });
    add('text', { x: m.l - 6, y: sy(v) + 4, 'text-anchor': 'end', 'font-size': 11, fill: 'var(--muted)' }, fmtValue(v, res.metric));
    add('text', { x: sx(v), y: H - m.b + 15, 'text-anchor': 'middle', 'font-size': 11, fill: 'var(--muted)' }, fmtValue(v, res.metric));
  }
  add('line', { x1: sx(lo), y1: sy(lo), x2: sx(hi), y2: sy(hi), stroke: 'var(--muted)', 'stroke-width': 1.5, 'stroke-dasharray': '5 4' });
  const color = colorFor(r.key);
  const g = document.createElementNS(SVGNS, 'g');
  svg.append(g);
  for (const [x, y] of pts) {
    const c = document.createElementNS(SVGNS, 'circle');
    c.setAttribute('cx', sx(x)); c.setAttribute('cy', sy(y)); c.setAttribute('r', 3);
    c.setAttribute('fill', color); c.setAttribute('fill-opacity', 0.55);
    const t = document.createElementNS(SVGNS, 'title');
    t.textContent = `Reference ${fmtValue(x, res.metric)} · ${r.label} ${fmtValue(y, res.metric)}`;
    c.append(t);
    g.append(c);
  }
  if (r.slope !== null && r.slope !== undefined) {
    const y1 = r.slope * lo + r.intercept, y2 = r.slope * hi + r.intercept;
    add('line', { x1: sx(lo), y1: sy(Math.max(lo, Math.min(hi, y1))), x2: sx(hi), y2: sy(Math.max(lo, Math.min(hi, y2))), stroke: color, 'stroke-width': 2 });
  }
  add('text', { x: (m.l + W - m.r) / 2, y: H - 6, 'text-anchor': 'middle', 'font-size': 12, fill: 'var(--ink-2)' }, `Reference${res.unit ? ` (${res.unit})` : ''}`);
  add('text', { x: 12, y: (m.t + H - m.b) / 2, 'text-anchor': 'middle', 'font-size': 12, fill: 'var(--ink-2)', transform: `rotate(-90 12 ${(m.t + H - m.b) / 2})` }, r.label);
  const eq = r.slope === null || r.slope === undefined ? `n = ${r.n}` :
    `y = ${num(r.slope, 3)}x ${r.intercept >= 0 ? '+' : '−'} ${num(Math.abs(r.intercept))} · R² ${num(r.r2, 3)} · n ${r.n}`;
  return el('figure', { class: 'scatter', style: 'margin:0' }, el('h3', {}, r.label), el('div', { class: 'eq' }, eq), svg);
}

function renderQuality(res) {
  const t = res.ocr.totals;
  const total = Object.values(t).reduce((a, b) => a + b, 0);
  const p = n => (total ? `${((100 * (n || 0)) / total).toFixed(1)}%` : '–');
  const unit = res.unit ? ` ${res.unit}` : '';
  $('#quality').replaceChildren(
    el('p', { style: 'margin-top:0' }, total
      ? `${total.toLocaleString()} readings in this period (all readings and monitors): ${p(t.ok)} accepted automatically, ${p(t.verified)} confirmed by a person, ${p(t.corrected)} corrected, ${p(t.rejected)} unreadable, ${p(t.review)} still waiting for review.`
      : 'No readings in this period.'),
    res.own_checks.length
      ? el('div', {}, el('h3', {}, 'Camera reading vs. your own cloud data'),
        el('p', { class: 'small muted' }, 'For your own monitors: what the camera read off the screen, compared with what the monitor sent to your cloud (nearest values within 30 s). Differences include timing, not only reading errors.'),
        el('div', { class: 'table-wrap' }, el('table', {},
          el('thead', {}, el('tr', {}, ['Monitor', 'Cloud ID', 'Pairs', 'Exact match', 'Within 5%', 'Mean abs. difference'].map((h, i) => el('th', { class: i > 1 ? 'numcell' : null }, h)))),
          el('tbody', {}, res.own_checks.map(c => el('tr', {}, el('td', {}, c.name), el('td', {}, c.external_id),
            el('td', { class: 'numcell' }, c.n), el('td', { class: 'numcell' }, pct(c.exact_pct)), el('td', { class: 'numcell' }, pct(c.within_5pct)),
            el('td', { class: 'numcell' }, `${num(c.mae)}${unit}`)))))))
      : el('p', { class: 'small muted' }, 'Tip: mark one of your own monitors in a camera view (with its cloud ID) and import its cloud data, to measure how accurately the camera reads screens.'),
    accuracyTable(res),
  );
}

// Reading accuracy from spot checks (random automatically accepted readings checked by a person),
// plus what the review queue and the odd-jump check did.
function accuracyTable(res) {
  const rows = res.accuracy || [];
  if (!rows.length) return null;
  const checked = rows.some(a => a.spot_checked);
  return el('div', {},
    el('h3', {}, 'Reading accuracy'),
    el('p', { class: 'small muted' }, checked
      ? 'Accuracy: share of randomly picked, automatically accepted readings that a person found right (Review → Spot check), with its 95% confidence range.'
      : 'No spot checks yet. Open Review → Spot check and check about 50 random readings per monitor and reading to measure the accuracy.'),
    el('div', { class: 'table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {}, ['Monitor', 'Reading', 'Readings', 'Spot-checked', 'Accuracy', 'Sent to review', 'Machine was right', 'Corrected', 'Unreadable', 'Odd jumps caught']
        .map((h, i) => el('th', { class: i > 1 ? 'numcell' : null }, h)))),
      el('tbody', {}, rows.map(a => el('tr', {},
        el('td', {}, a.device_name), el('td', {}, metricLabel(a.metric, state.catalog)),
        el('td', { class: 'numcell' }, a.readings.toLocaleString()), el('td', { class: 'numcell' }, a.spot_checked),
        el('td', { class: 'numcell' }, a.spot_checked ? `${a.accuracy_pct}% (${a.ci_low}–${a.ci_high}%)` : el('a', { href: 'review.html#spot' }, 'check')),
        el('td', { class: 'numcell' }, a.reviewed), el('td', { class: 'numcell' }, a.review_machine_right),
        el('td', { class: 'numcell' }, a.review_corrected), el('td', { class: 'numcell' }, a.review_unreadable),
        el('td', { class: 'numcell' }, a.odd_jumps)))))));
}

form.addEventListener('submit', e => { e.preventDefault(); load(); });
form.run.addEventListener('change', () => { toggleCustom(); form.reference.value = ''; load(); });
form.metric.addEventListener('change', () => { form.reference.value = ''; load(); });
form.avg.addEventListener('change', load);
form.reference.addEventListener('change', load);
$('#table-mode').addEventListener('change', e => chart.setTableMode(e.target.checked));
$('#copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('#methods').textContent); toast('Copied'); } catch { toast('Copy failed; select the text instead', 'error'); }
});

// Print in light colours even if the screen is in dark mode.
let themeBeforePrint = null;
window.addEventListener('beforeprint', () => {
  themeBeforePrint = document.documentElement.dataset.theme ?? '';
  document.documentElement.dataset.theme = 'light';
  window.dispatchEvent(new Event('themechange'));
});
window.addEventListener('afterprint', () => {
  if (themeBeforePrint) document.documentElement.dataset.theme = themeBeforePrint;
  else delete document.documentElement.dataset.theme;
  window.dispatchEvent(new Event('themechange'));
});
$('#print').addEventListener('click', () => window.print());

init();
