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
export function toast(msg, kind = '') {
  let t = $('.toast');
  if (!t) { t = el('div', { class: 'toast', role: 'status' }); document.body.append(t); }
  t.className = `toast ${kind}`;
  t.textContent = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), kind === 'error' ? 6000 : 3000);
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
function toggleTheme() {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  root.dataset.theme = dark ? 'light' : 'dark';
  try { localStorage.setItem('theme', root.dataset.theme); } catch { /* ignore */ }
  window.dispatchEvent(new Event('themechange'));
}

export function renderTopbar(active) {
  initTheme();
  const pages = [['index.html', 'Live'], ['lined.html', 'Lined up'], ['cameras.html', 'Cameras'], ['calibrate.html', 'Calibrate'], ['review.html', 'Review'],
    ['results.html', 'Results'], ['data.html', 'Data & exports']];
  const reviewCount = el('span', { class: 'count hidden', id: 'review-count' });
  const nav = el('nav', { class: 'nav', 'aria-label': 'Main' },
    pages.map(([href, label]) => el('a', { href, 'aria-current': href === active ? 'page' : null }, label, href === 'review.html' ? reviewCount : null)));
  const bar = el('header', { class: 'topbar' },
    el('div', { class: 'topbar-inner' },
      el('div', { class: 'brand' }, 'AQ Test Platform'), nav, el('div', { class: 'spacer' }),
      el('button', { class: 'iconbtn', type: 'button', title: 'Switch light/dark', onclick: toggleTheme }, '◐ Theme')));
  document.body.prepend(bar);
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
