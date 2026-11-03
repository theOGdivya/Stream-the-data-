// Time-series chart for one metric: every monitor read by camera (solid lines) plus
// reference data (dashed). Colours follow the device, not its position in the list.
/* global uPlot */
import { cssVar, el, fmtDateTime, fmtValue } from './common.js';

const SLOTS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8'];

export class MetricChart {
  constructor(root, { height = 320 } = {}) {
    this.root = root;
    this.height = height;
    this.legend = el('div', { class: 'legend', 'aria-label': 'Legend' });
    this.wrap = el('div', { class: 'chart-wrap' });
    this.tooltip = el('div', { class: 'tooltip hidden', role: 'presentation' });
    this.table = el('div', { class: 'table-wrap hidden' });
    this.empty = el('div', { class: 'chart-empty hidden' }, 'No readings in this time range yet.');
    this.wrap.append(this.tooltip);
    root.append(this.legend, this.wrap, this.empty, this.table);
    this.plot = null;
    this.tableMode = false;
    new ResizeObserver(() => this.plot && this.plot.setSize({ width: this.width(), height: this.height })).observe(root);
    window.addEventListener('themechange', () => this.last && this.render(...this.last));
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => this.last && this.render(...this.last));
  }

  width() { return Math.max(280, this.root.clientWidth); }

  setTableMode(on) {
    this.tableMode = on;
    if (this.last) this.render(...this.last);
  }

  // resp: /api/series response; order: array of series keys giving each a stable colour slot.
  render(resp, order, unit, metric) {
    this.last = [resp, order, unit, metric];
    const series = resp.series.filter(s => s.points.length);
    this.legend.replaceChildren();
    if (this.plot) { this.plot.destroy(); this.plot = null; }
    this.empty.classList.toggle('hidden', series.length > 0);
    this.wrap.classList.toggle('hidden', !series.length || this.tableMode);
    this.table.classList.toggle('hidden', !series.length || !this.tableMode);
    if (!series.length) return;

    const surface = cssVar('--surface');
    const styled = series.map(s => {
      const slot = order.indexOf(s.key);
      const color = slot >= 0 && slot < SLOTS.length ? cssVar(SLOTS[slot]) : cssVar('--muted');
      return { ...s, color, dashed: s.kind === 'reference' };
    });

    for (const s of styled) {
      this.legend.append(el('span', { class: 'key' },
        el('span', { class: `swatch${s.dashed ? ' dashed' : ''}`, style: `border-color:${s.color}` }),
        s.label + (s.is_own ? ' (your device)' : '') + (s.kind === 'reference' ? ' · reference data' : '')));
    }

    // Join all series on one x axis. undefined = no point here (line continues);
    // null = a real gap (camera offline), which breaks the line.
    const bucket = resp.bucket_ms;
    const maps = styled.map(s => new Map(s.points.map(p => [p[0], p[1]])));
    const xs = [...new Set(styled.flatMap(s => s.points.map(p => p[0])))].sort((a, b) => a - b);
    // Every x inside a real hole in a series (camera offline, readings excluded) is null, so
    // no line is drawn across it; x values that merely fall between this series' regular
    // points (another series has data there) stay undefined and the line continues.
    const columns = styled.map(s => {
      const pts = s.points;
      const limit = 3 * Math.max(bucket, medianSpacing(pts));
      let j = 0;
      return xs.map(x => {
        while (j < pts.length && pts[j][0] < x) j++;
        if (j < pts.length && pts[j][0] === x) return pts[j][1];
        if (j === 0 || j === pts.length) return undefined;
        return pts[j][0] - pts[j - 1][0] > limit ? null : undefined;
      });
    });
    const data = [xs.map(x => x / 1000), ...columns];

    if (this.tableMode) { this.renderTable(xs, styled, maps, unit, metric); return; }

    const axisStroke = cssVar('--muted');
    const grid = { stroke: cssVar('--grid'), width: 1 };
    const ticks = { stroke: cssVar('--axis'), width: 1, size: 4 };
    const opts = {
      width: this.width(),
      height: this.height,
      legend: { show: false },
      cursor: { y: false, points: { size: 8, width: 2, stroke: surface }, drag: { x: true, y: false } },
      scales: { x: { time: true } },
      axes: [
        { stroke: axisStroke, grid, ticks, font: '12px system-ui, sans-serif' },
        { stroke: axisStroke, grid, ticks, font: '12px system-ui, sans-serif', size: 56,
          label: unit || undefined, labelFont: '12px system-ui, sans-serif', labelSize: 18 },
      ],
      series: [{}, ...styled.map(s => ({
        label: s.label,
        stroke: s.color,
        width: 2,
        dash: s.dashed ? [6, 4] : undefined,
        spanGaps: false,
        points: { size: 8, width: 2, fill: s.color, stroke: surface },
      }))],
      hooks: { setCursor: [u => this.updateTooltip(u, styled, xs, maps, unit, metric)] },
    };
    this.plot = new uPlot(opts, data, this.wrap);
    this.plot.over.addEventListener('mouseleave', () => this.tooltip.classList.add('hidden'));
  }

  updateTooltip(u, styled, xs, maps, unit, metric) {
    const idx = u.cursor.idx;
    if (idx === null || idx === undefined) { this.tooltip.classList.add('hidden'); return; }
    const x = xs[idx];
    const rows = styled.map((s, i) => [s, nearestValue(maps[i], xs, idx, Math.max(this.last[0].bucket_ms * 1.5, 60000))]);
    this.tooltip.replaceChildren(
      el('div', { class: 'tt-time' }, fmtDateTime(x)),
      ...rows.map(([s, v]) => el('div', { class: 'tt-row' },
        el('span', { class: `swatch${s.dashed ? ' dashed' : ''}`, style: `border-color:${s.color}` }),
        el('b', {}, v === null ? '–' : fmtValue(v, metric)), s.label)),
      ...(unit ? [el('div', { class: 'tt-time' }, unit)] : []),
    );
    this.tooltip.classList.remove('hidden');
    const left = u.cursor.left + u.over.offsetLeft;
    const tw = this.tooltip.offsetWidth;
    const pos = left + 16 + tw > this.wrap.clientWidth ? left - tw - 16 : left + 16;
    this.tooltip.style.left = `${Math.max(0, pos)}px`;
    this.tooltip.style.top = `${u.over.offsetTop + 8}px`;
  }

  renderTable(xs, styled, maps, unit, metric) {
    const rows = xs.slice(-500).reverse();
    const table = el('table', {},
      el('thead', {}, el('tr', {}, el('th', {}, 'Time'), styled.map(s => el('th', { class: 'numcell' }, `${s.label}${unit ? ` (${unit})` : ''}`)))),
      el('tbody', {}, rows.map(x => el('tr', {}, el('td', {}, fmtDateTime(x)),
        maps.map(m => el('td', { class: 'numcell' }, m.get(x) === undefined || m.get(x) === null ? '' : fmtValue(m.get(x), metric)))))));
    this.table.replaceChildren(table);
    if (xs.length > 500) this.table.append(el('p', { class: 'muted small' }, `Showing the newest 500 of ${xs.length} rows. Use Data & exports for the full CSV.`));
  }
}

function medianSpacing(pts) {
  if (pts.length < 3) return 0;
  const d = [];
  for (let i = 1; i < pts.length; i++) d.push(pts[i][0] - pts[i - 1][0]);
  d.sort((a, b) => a - b);
  return d[Math.floor(d.length / 2)];
}

function nearestValue(map, xs, idx, window) {
  const x = xs[idx];
  for (let step = 0; step < 60; step++) {
    for (const j of step ? [idx - step, idx + step] : [idx]) {
      if (j < 0 || j >= xs.length || Math.abs(xs[j] - x) > window) continue;
      const v = map.get(xs[j]);
      if (v !== undefined && v !== null) return v;
    }
  }
  return null;
}
