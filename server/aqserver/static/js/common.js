// Shared helpers for all dashboard pages. All dynamic text goes through textContent.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export async function api(path, { method = 'GET', body, form } = {}) {
  const opts = { method, headers: {}, credentials: 'same-origin' };
  if (form) opts.body = form;
  else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j.detail) msg = typeof j.detail === 'string' ? j.detail : j.detail.map(d => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join('; ');
    } catch { /* not JSON */ }
    throw new Error(msg);
  }
  const type = res.headers.get('content-type') || '';
  return type.includes('application/json') ? res.json() : res;
}

export function ago(ms, now = Date.now()) {
  if (!ms) return 'never';
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 60) return `${s} s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${m % 60} min ago`;
  return `${Math.floor(h / 24)} days ago`;
}

export function duration(ms) {
  const m = Math.floor(ms / 60000);
  const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mm = m % 60;
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${mm} min`;
  return `${mm} min`;
}

const dtf = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'medium' });
const tf = new Intl.DateTimeFormat(undefined, { timeStyle: 'medium' });
export const fmtDateTime = ms => (ms ? dtf.format(new Date(ms)) : '–');
export const fmtTime = ms => (ms ? tf.format(new Date(ms)) : '–');

export function fmtBytes(n) {
  if (n === null || n === undefined) return '–';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i >= 3 ? 1 : 0)} ${units[i]}`;
}

export function fmtValue(v, metric) {
  if (v === null || v === undefined) return '–';
  const decimals = ['temp', 'hcho'].includes(metric) || Math.abs(v) < 10 && !Number.isInteger(v) ? 1 : 0;
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: Math.max(decimals, Number.isInteger(v) ? 0 : 2) });
}

let toastTimer;
// action: optional { label, onClick } shown as a button in the toast (e.g. Undo).
export function toast(msg, kind = '', action = null) {
  let t = $('.toast');
  if (!t) { t = el('div', { class: 'toast', role: 'status' }); document.body.append(t); }
  t.className = `toast ${kind}`;
  t.textContent = msg;
  if (action) {
    t.append(' ', el('button', { type: 'button', class: 'toast-action', onclick: () => { t.remove(); action.onClick(); } }, action.label));
  }
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), kind === 'error' || action ? 6000 : 3000);
}

let catalog;
export async function getCatalog() {
  if (!catalog) catalog = await api('/api/catalog');
  return catalog;
}
export function metricLabel(metric, cat) {
  return (cat && cat.metrics[metric] && cat.metrics[metric].label) || metric;
}

// Theme: follows the OS unless the viewer picks one (remembered in this browser only).
export function initTheme() {
  try {
    const saved = localStorage.getItem('theme');
    if (saved) document.documentElement.dataset.theme = saved;
  } catch { /* storage blocked */ }
}
function isDark() {
  const root = document.documentElement;
  return root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
}
function toggleTheme() {
  const root = document.documentElement;
  root.dataset.theme = isDark() ? 'light' : 'dark';
  try { localStorage.setItem('theme', root.dataset.theme); } catch { /* ignore */ }
  window.dispatchEvent(new Event('themechange'));
}

// Small line icons (24-unit grid, drawn with the current text colour).
const ICONS = {
  live: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  lined: '<rect x="3" y="4" width="5" height="16" rx="1"/><rect x="9.5" y="4" width="5" height="16" rx="1"/><rect x="16" y="4" width="5" height="16" rx="1"/>',
  cameras: '<path d="M3 8a2 2 0 0 1 2-2h2l2-2h6l2 2h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><circle cx="12" cy="12.5" r="3.5"/>',
  calibrate: '<path d="M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5a1 1 0 0 1-1-1v-4"/><path d="M9 12h6M12 9v6"/>',
  review: '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="m8.5 12.5 2.5 2.5 4.5-5"/>',
  results: '<path d="M4 20V4M4 20h16"/><path d="M8 16v-4M12 16V8M16 16v-6"/>',
  data: '<ellipse cx="12" cy="6" rx="7" ry="2.5"/><path d="M5 6v12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V6M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  logo: '<path d="M3 8c3-2.5 6-2.5 9 0s6 2.5 9 0M3 13c3-2.5 6-2.5 9 0s6 2.5 9 0M3 18c3-2.5 6-2.5 9 0s6 2.5 9 0"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
};
export function icon(name, size = 18) {
  const span = document.createElement('span');
  span.className = 'ico';
  span.setAttribute('aria-hidden', 'true');
  span.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ''}</svg>`;
  return span;
}

// Pages in the order of the work: watch the test, set it up, then analyse it.
const NAV = [
  [['index.html', 'Live', 'live'], ['lined.html', 'Lined up', 'lined']],
  [['cameras.html', 'Cameras', 'cameras'], ['calibrate.html', 'Calibrate', 'calibrate']],
  [['review.html', 'Review', 'review'], ['results.html', 'Results', 'results'], ['data.html', 'Data & exports', 'data']],
];

export function renderTopbar(active) {
  initTheme();
  const reviewCount = el('span', { class: 'count hidden', id: 'review-count', title: 'Readings waiting for review' });
  const nav = el('nav', { class: 'nav', 'aria-label': 'Main' },
    NAV.map((group, gi) => [gi ? el('span', { class: 'nav-sep', 'aria-hidden': 'true' }) : null,
      ...group.map(([href, label, ic]) => el('a', { href, 'aria-current': href === active ? 'page' : null },
        icon(ic, 17), el('span', {}, label), href === 'review.html' ? reviewCount : null))]).flat());
  const themeBtn = el('button', { class: 'iconbtn', type: 'button' });
  const paintTheme = () => {
    const dark = isDark();
    themeBtn.replaceChildren(icon(dark ? 'sun' : 'moon', 17), el('span', { class: 'theme-label' }, dark ? 'Light' : 'Dark'));
    themeBtn.title = dark ? 'Switch to the light theme' : 'Switch to the dark theme';
    themeBtn.setAttribute('aria-label', themeBtn.title);
  };
  themeBtn.addEventListener('click', () => { toggleTheme(); paintTheme(); });
  paintTheme();
  const bar = el('header', { class: 'topbar' },
    el('div', { class: 'topbar-inner' },
      el('a', { class: 'brand', href: 'index.html' }, el('span', { class: 'brand-mark' }, icon('logo', 18)), 'AQ Test Platform'),
      nav, el('div', { class: 'spacer' }), themeBtn));
  document.body.prepend(bar);
  nav.querySelector('[aria-current="page"]')?.scrollIntoView?.({ block: 'nearest', inline: 'center' });
  updateReviewCount();
  setInterval(updateReviewCount, 30000);
}

export async function updateReviewCount(n) {
  const badge = $('#review-count');
  if (!badge) return;
  if (n === undefined) {
    try { n = (await api('/api/review?limit=1')).total; } catch { return; }
  }
  badge.textContent = n;
  badge.classList.toggle('hidden', !n);
}

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Polls an image endpoint into an <img>, swapping only when the new frame has loaded.
export class ImagePoller {
  constructor(img, urlFn, { fastMs = 500, slowMs = 3000, onInfo } = {}) {
    Object.assign(this, { img, urlFn, fastMs, slowMs, onInfo });
    this.running = false;
    this.visible = true;
    this.lastUrl = null;
  }
  start() {
    if (this.running) return;
    this.running = true;
    this.loop();
  }
  stop() { this.running = false; clearTimeout(this.timer); }
  async loop() {
    if (!this.running) return;
    let delay = this.slowMs;
    if (this.visible && !document.hidden) {
      try {
        const res = await fetch(this.urlFn(), { cache: 'no-store', credentials: 'same-origin' });
        if (res.ok) {
          const kind = res.headers.get('x-kind') || 'still';
          const captured = Number(res.headers.get('x-captured-ms')) || null;
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          await new Promise(resolve => { this.img.onload = this.img.onerror = resolve; this.img.src = url; });
          if (this.lastUrl) URL.revokeObjectURL(this.lastUrl);
          this.lastUrl = url;
          this.onInfo && this.onInfo({ kind, captured, ok: true });
          delay = kind === 'live' ? this.fastMs : 2000;
        } else {
          this.onInfo && this.onInfo({ ok: false, status: res.status });
        }
      } catch {
        this.onInfo && this.onInfo({ ok: false });
      }
    }
    this.timer = setTimeout(() => this.loop(), delay);
  }
}
