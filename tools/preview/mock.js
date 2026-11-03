/* Preview mode for the dashboard pages.
 *
 * Answers the pages' /api/ requests from responses recorded on a real demo run
 * (tools/preview/record.js), so the real pages can be clicked through without a server.
 * Recorded timestamps are shifted so the data looks current. Changes are not saved.
 * Loaded as the first script of every page in the preview build only.
 */
(function () {
  'use strict';
  const DATA = new URL('data/', document.currentScript.src);
  const realFetch = window.fetch.bind(window);
  const started = performance.now();
  const ready = realFetch(new URL('index.json', DATA)).then(r => r.json()).then(build);
  const VOLATILE = new Set(['t', 'since_ms', 'until_ms', 'max_points', 'h', 'w', 'c', 'before_ms', 'limit']);
  let rec = null;
  let shift = 0;
  let lo = 0;
  let hi = 0;

  function build(raw) {
    shift = Date.now() - raw.recorded_at;
    lo = raw.recorded_at - 30 * 86400e3;
    hi = raw.recorded_at + 86400e3;
    const byKey = new Map();
    const byShape = new Map();
    for (const e of raw.entries) {
      const key = `${e.m} ${e.p}`;
      if (!byKey.has(key)) byKey.set(key, []);
      byKey.get(key).push(e);
      const shape = `${e.m} ${e.p.replace(/\d+/g, '#')}`;
      if (!byShape.has(shape)) byShape.set(shape, []);
      byShape.get(shape).push(e);
    }
    rec = { byKey, byShape, entries: raw.entries };
    return rec;
  }

  const isTime = n => typeof n === 'number' && n > lo && n < hi;
  function shiftJSON(v) {
    if (Array.isArray(v)) return v.map(shiftJSON);
    if (v && typeof v === 'object') {
      const o = {};
      for (const [k, x] of Object.entries(v)) o[k] = shiftJSON(x);
      return o;
    }
    return isTime(v) ? v + shift : v;
  }

  // Pick the recorded response that best matches a request.
  function pick(method, url) {
    const u = new URL(url, location.href);
    const params = Object.fromEntries(u.searchParams);
    let cands = rec.byKey.get(`${method} ${u.pathname}`);
    if (!cands) {
      // Same endpoint shape with other ids (e.g. a picture never visited while recording):
      // the trailing ids must match, the first one is matched by nearest value.
      const nums = (u.pathname.match(/\d+/g) || []).map(Number);
      const shaped = rec.byShape.get(`${method} ${u.pathname.replace(/\d+/g, '#')}`) || [];
      let best = null;
      let bestD = Infinity;
      for (const e of shaped) {
        const en = (e.p.match(/\d+/g) || []).map(Number);
        if (en.slice(1).some((x, i) => x !== nums[i + 1])) continue;
        const d = Math.abs((en[0] || 0) - (nums[0] || 0));
        if (d < bestD) { bestD = d; best = e; }
      }
      return best;
    }
    if ('t_ms' in params) {
      const target = Number(params.t_ms) - shift;
      const timed = cands.filter(e => 't_ms' in e.q);
      if (timed.length) return timed.reduce((a, b) => (Math.abs(b.q.t_ms - target) < Math.abs(a.q.t_ms - target) ? b : a));
    } else {
      const untimed = cands.filter(e => !('t_ms' in e.q));
      if (untimed.length) cands = untimed;
    }
    // Best match on the parameters that matter; then cycle through repeats by time.
    const score = e => Object.entries(params).reduce((s, [k, v]) => (VOLATILE.has(k) ? s : s + (String(e.q[k]) === v ? 1 : -1)), 0)
      - Object.keys(e.q).filter(k => !VOLATILE.has(k) && !(k in params)).length;
    let top = -Infinity;
    for (const e of cands) top = Math.max(top, score(e));
    const group = cands.filter(e => score(e) === top);
    if (group.length === 1) return group[0];
    const first = group[0].at;
    const span = group[group.length - 1].at - first + 1500;
    const el = (performance.now() - started) % span;
    let chosen = group[0];
    for (const e of group) if (e.at - first <= el) chosen = e;
    return chosen;
  }

  const fileCache = new Map();
  function file(f) {
    if (!fileCache.has(f)) fileCache.set(f, realFetch(new URL(f, DATA)).then(r => r.blob()));
    return fileCache.get(f);
  }

  let warned = false;
  async function answer(method, url, body) {
    await ready;
    const u = new URL(url, location.href);
    if (method !== 'GET') {
      if (u.pathname.startsWith('/api/capture/')) return captureAnswer(u, body);
      const e = pick(method, url);
      if (!warned) { warned = true; toast('Preview: changes are not saved. On the real platform this would be stored.'); }
      if (e && e.s < 400) return respond(e);
      return json({ ok: true, preview: true });
    }
    const e = pick(method, url);
    if (!e) return json({ detail: 'not available in this preview' }, 404);
    return respond(e);
  }

  async function respond(e) {
    const headers = new Headers();
    for (const [k, v] of Object.entries(e.h || {})) headers.set(k, k === 'x-captured-ms' ? String(Number(v) + shift) : v);
    if (e.j !== undefined) return new Response(JSON.stringify(shiftJSON(e.j)), { status: e.s, headers });
    return new Response(await file(e.f), { status: e.s, headers });
  }

  function json(data, status = 200) {
    return new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });
  }

  // The recorder page talks to these; in the preview they just say "received".
  let frameId = 0;
  function captureAnswer(u) {
    if (u.pathname.endsWith('/heartbeat')) return json({ live: [], server_time_ms: Date.now(), capture: {}, cameras: [], probe: false });
    if (u.pathname.endsWith('/frames')) return json({ id: ++frameId, duplicate: false });
    return json({ watching: false });
  }

  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : input.url;
    if (typeof url === 'string' && url.startsWith('/api/')) return answer((init && init.method) || 'GET', url, init && init.body);
    return realFetch(input, init);
  };

  // <img> pictures served by the API.
  const objUrls = new Map();
  async function imageUrl(url) {
    await ready;
    const e = pick('GET', url);
    if (!e || !e.f) return PLACEHOLDER;
    if (!objUrls.has(e.f)) objUrls.set(e.f, URL.createObjectURL(await file(e.f)));
    return objUrls.get(e.f);
  }
  const PLACEHOLDER = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="320" height="180" fill="#232321"/><text x="160" y="95" font-family="sans-serif" font-size="14" fill="#c3c2b7" text-anchor="middle">Not recorded for this preview</text></svg>');
  const isApi = v => typeof v === 'string' && v.startsWith('/api/');
  const srcDesc = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
  Object.defineProperty(HTMLImageElement.prototype, 'src', {
    configurable: true, enumerable: true,
    get() { return srcDesc.get.call(this); },
    set(v) {
      if (isApi(v)) { this.dataset.api = v; imageUrl(v).then(u => { if (this.dataset.api === v) srcDesc.set.call(this, u); }); } else srcDesc.set.call(this, v);
    },
  });
  const setAttr = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if (this instanceof HTMLImageElement && name === 'src' && isApi(value)) { this.src = value; return; }
    return setAttr.call(this, name, value);
  };

  // The preview frame has no dialogs, printing or downloads.
  window.confirm = () => true;
  window.print = () => toast('Printing works on the real platform.');
  document.addEventListener('click', e => {
    const a = e.target.closest && e.target.closest('a[href^="/api/"]');
    if (a) { e.preventDefault(); toast('Downloads work on the real platform. This preview has no server behind it.'); }
  }, true);

  // A simulated camera for the recorder page (this preview cannot use a real webcam).
  if (/capture\.html$/.test(location.pathname)) {
    const md = navigator.mediaDevices || {};
    if (!navigator.mediaDevices) { try { Object.defineProperty(navigator, 'mediaDevices', { value: md, configurable: true }); } catch { /* ignore */ } }
    md.enumerateDevices = async () => [{ kind: 'videoinput', deviceId: 'preview-camera', groupId: '', label: 'Preview camera (recorded demo pictures)', toJSON() { return this; } }];
    md.getUserMedia = async () => {
      await ready;
      const frames = rec.entries.filter(e => e.p.endsWith('/live.jpg') && e.h['x-kind'] === 'live');
      const imgs = await Promise.all(frames.slice(0, 40).map(async e => createImageBitmap(await file(e.f))));
      const canvas = document.createElement('canvas');
      canvas.width = imgs[0] ? imgs[0].width : 960;
      canvas.height = imgs[0] ? imgs[0].height : 540;
      const ctx = canvas.getContext('2d');
      let i = 0;
      const draw = () => { if (imgs.length) ctx.drawImage(imgs[i++ % imgs.length], 0, 0); };
      draw();
      setInterval(draw, 500);
      return canvas.captureStream(10);
    };
  }

  // Banner and messages.
  let toastTimer;
  function toast(msg) {
    let t = document.getElementById('preview-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'preview-toast';
      t.setAttribute('role', 'status');
      t.style.cssText = 'position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:200;background:#0b0b0b;color:#fcfcfb;padding:10px 16px;border-radius:8px;font:600 14px system-ui,sans-serif;max-width:calc(100vw - 32px)';
      document.body.append(t);
    }
    t.textContent = msg;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.remove(), 4000);
  }
  document.addEventListener('DOMContentLoaded', () => {
    const bar = document.createElement('div');
    bar.style.cssText = 'background:#2a78d6;color:#fff;font:600 13px system-ui,sans-serif;padding:7px 16px;display:flex;gap:12px;flex-wrap:wrap;align-items:center';
    bar.append('Preview with data recorded from a demo run (fake camera and monitors). Changes are not saved.');
    const home = document.createElement('a');
    home.href = 'index.html';
    home.textContent = 'Preview home';
    home.style.cssText = 'color:#fff;margin-left:auto';
    bar.append(home);
    document.body.prepend(bar);
  });
})();
