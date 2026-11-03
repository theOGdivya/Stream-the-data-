// Record stills from a camera in this browser tab, the same way the capture program does:
// clock-aligned stills, SHA-256 per picture, store-and-forward queue, live view on demand,
// "marked monitors only" masking and heartbeats.
import { $, ago, api, el, renderTopbar, toast } from './common.js';

renderTopbar('cameras.html');
// Recording lives in this tab: other pages open in a new tab so it keeps going.
for (const a of document.querySelectorAll('.topbar a')) { a.target = '_blank'; a.rel = 'noopener'; }

const form = $('#form');
const video = $('#video');
const S = {
  stream: null, track: null, imageCapture: null, recording: false, cam: null, agentId: null,
  nextStill: 0, lastHeartbeat: 0, live: false, liveBusy: false, liveNext: 0, capture: null, lastFull: 0,
  saved: 0, uploaded: 0, rejected: 0, lastUpload: null, lastStill: null, problem: null,
  prevThumb: null, sameCount: 0, brightness: null, wakeLock: null, stillBusy: false, uploading: false,
};

// --- remembered choices (this browser only) ---------------------------------------------------
const mem = {
  get(k, d = null) { try { return JSON.parse(localStorage.getItem(`capture.${k}`)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(`capture.${k}`, JSON.stringify(v)); } catch { /* blocked */ } },
};
const slug = t => String(t).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'camera';

// --- picture queue: IndexedDB so nothing is lost if the internet drops (memory as a fallback) ---
const queue = {
  db: null, mem: [], seq: 0,
  async open() {
    try {
      this.db = await new Promise((resolve, reject) => {
        const req = indexedDB.open('aq-capture', 1);
        req.onupgradeneeded = () => req.result.createObjectStore('stills', { keyPath: 'id', autoIncrement: true });
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      });
    } catch { this.db = null; }
  },
  tx(mode) { return this.db.transaction('stills', mode).objectStore('stills'); },
  req(r) { return new Promise((resolve, reject) => { r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error); }); },
  async add(item) {
    if (this.db) { try { return await this.req(this.tx('readwrite').add(item)); } catch { /* fall through */ } }
    this.mem.push({ ...item, id: `m${++this.seq}` });
  },
  async oldest() {
    if (this.mem.length) return this.mem[0];
    if (!this.db) return null;
    return new Promise(resolve => {
      const r = this.tx('readonly').openCursor();
      r.onsuccess = () => resolve(r.result ? r.result.value : null);
      r.onerror = () => resolve(null);
    });
  },
  async remove(id) {
    if (typeof id === 'string') { this.mem = this.mem.filter(x => x.id !== id); return; }
    try { await this.req(this.tx('readwrite').delete(id)); } catch { /* ignore */ }
  },
  async count() {
    let n = this.mem.length;
    if (this.db) { try { n += await this.req(this.tx('readonly').count()); } catch { /* ignore */ } }
    return n;
  },
};

function problem(text) {
  S.problem = text;
  $('#problem').classList.toggle('hidden', !text);
  $('#problem-text').textContent = text || '';
}

// --- camera -----------------------------------------------------------------------------------
async function listCameras() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    problem('Camera access needs a secure page. Open this site with its https:// address (or http://localhost on this computer).');
    $('#start').disabled = true;
    return;
  }
  try {
    const tmp = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
    tmp.getTracks().forEach(t => t.stop());
  } catch (e) {
    problem(e.name === 'NotAllowedError'
      ? 'The browser was not allowed to use the camera. Click the camera icon in the address bar, allow it, then reload this page.'
      : e.name === 'NotFoundError' ? 'No camera was found on this computer. Plug one in and reload this page.'
        : `The camera could not be opened (${e.message}). Close other apps that use it (Zoom, Teams) and reload.`);
    $('#start').disabled = true;
    return;
  }
  const devices = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'videoinput');
  const sel = form.device;
  sel.replaceChildren(...devices.map((d, i) => el('option', { value: d.deviceId }, d.label || `Camera ${i + 1}`)));
  const remembered = mem.get('device');
  if (devices.some(d => d.deviceId === remembered)) sel.value = remembered;
  if (!form.name.value) form.name.value = mem.get('name') || (sel.selectedOptions[0]?.textContent || 'Webcam').replace(/\s*\([0-9a-f]{4}:[0-9a-f]{4}\)$/i, '');
  await openCamera(sel.value);
}

async function openCamera(deviceId) {
  S.stream?.getTracks().forEach(t => t.stop());
  try {
    S.stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { deviceId: deviceId ? { exact: deviceId } : undefined, width: { ideal: 1920 }, height: { ideal: 1080 } },
    });
  } catch (e) {
    problem(`This camera could not be opened (${e.name}). It may be in use by another app.`);
    return false;
  }
  S.track = S.stream.getVideoTracks()[0];
  S.track.addEventListener('ended', () => {
    problem('The camera stopped (unplugged or taken by another app). Trying to open it again…');
    setTimeout(() => S.recording && openCamera(form.device.value), 5000);
  });
  S.imageCapture = 'ImageCapture' in window ? new ImageCapture(S.track) : null;
  video.srcObject = S.stream;
  try { await video.play(); } catch { /* autoplay blocked: preview starts on first click */ }
  const st = S.track.getSettings();
  $('#resolution').textContent = st.width ? `Camera gives ${st.width}×${st.height}${st.frameRate ? ` at ${Math.round(st.frameRate)} fps` : ''}.` : '';
  if (S.problem && !S.problem.startsWith('Camera access needs')) problem(null);
  return true;
}

async function grab(maxWidth = 0) {
  let bmp = null;
  if (S.imageCapture) {
    try { bmp = await S.imageCapture.grabFrame(); } catch { bmp = null; }
  }
  const w0 = bmp ? bmp.width : video.videoWidth;
  const h0 = bmp ? bmp.height : video.videoHeight;
  if (!w0 || !h0) { bmp?.close?.(); return null; }
  const scale = maxWidth && w0 > maxWidth ? maxWidth / w0 : 1;
  const c = document.createElement('canvas');
  c.width = Math.round(w0 * scale);
  c.height = Math.round(h0 * scale);
  c.getContext('2d').drawImage(bmp || video, 0, 0, c.width, c.height);
  bmp?.close?.();
  return c;
}

const toBlob = (canvas, quality) => new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', quality));
const hex = buf => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');

function analyse(c) {
  const t = document.createElement('canvas');
  t.width = 64; t.height = 36;
  const ctx = t.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(c, 0, 0, 64, 36);
  const d = ctx.getImageData(0, 0, 64, 36).data;
  const gray = new Float32Array(64 * 36);
  let sum = 0;
  for (let i = 0; i < gray.length; i++) { gray[i] = 0.299 * d[i * 4] + 0.587 * d[i * 4 + 1] + 0.114 * d[i * 4 + 2]; sum += gray[i]; }
  if (S.prevThumb) {
    let diff = 0;
    for (let i = 0; i < gray.length; i++) diff += Math.abs(gray[i] - S.prevThumb[i]);
    S.sameCount = diff / gray.length < 0.05 ? S.sameCount + 1 : 0;
  }
  S.prevThumb = gray;
  S.brightness = Math.round((sum / gray.length) * 10) / 10;
}

async function takeStill(now) {
  if (S.stillBusy) return;
  S.stillBusy = true;
  try {
    const c = await grab();
    if (!c) return;
    analyse(c);
    // "Store only the marked monitors" (set per camera on Calibrate): black out the rest.
    let out = c;
    let masked = false;
    const regions = S.capture?.regions;
    if (regions && now - S.lastFull < (S.capture.full_every_s || 300) * 1000) {
      out = document.createElement('canvas');
      out.width = c.width; out.height = c.height;
      const ctx = out.getContext('2d');
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, out.width, out.height);
      for (const [x, y, w, h] of regions) ctx.drawImage(c, x, y, w, h, x, y, w, h);
      masked = true;
    } else {
      S.lastFull = now;
    }
    const blob = await toBlob(out, 0.9);
    const sha256 = hex(await crypto.subtle.digest('SHA-256', await blob.arrayBuffer()));
    await queue.add({ meta: { camera_id: S.cam.id, captured_ms: now, sha256, width: c.width, height: c.height,
      brightness: S.brightness, masked }, blob });
    S.saved++;
    S.lastStill = now;
    upload();
  } catch (e) {
    problem(`Could not save a picture: ${e.message}`);
  } finally {
    S.stillBusy = false;
  }
}

async function upload() {
  if (S.uploading) return;
  S.uploading = true;
  let backoff = 2000;
  try {
    for (;;) {
      const item = await queue.oldest();
      if (!item) break;
      const fd = new FormData();
      fd.append('meta', JSON.stringify(item.meta));
      fd.append('image', item.blob, 'still.jpg');
      let res = null;
      try { res = await fetch('/api/capture/frames', { method: 'POST', body: fd, credentials: 'same-origin' }); } catch { res = null; }
      if (res?.ok) { await queue.remove(item.id); S.uploaded++; S.lastUpload = Date.now(); backoff = 2000; continue; }
      if (res && [400, 413, 422].includes(res.status)) { await queue.remove(item.id); S.rejected++; continue; }
      if (res?.status === 401) problem('You were logged out. Reload this page and log in; waiting pictures are kept.');
      await new Promise(r => setTimeout(r, backoff));
      backoff = Math.min(backoff * 2, 60000);
    }
  } finally {
    S.uploading = false;
  }
}

async function heartbeat() {
  S.lastHeartbeat = Date.now();
  const st = S.track?.getSettings() || {};
  const connected = S.track?.readyState === 'live';
  const late = S.lastStill && Date.now() - S.lastStill > 2.5 * S.cam.interval * 1000;
  const error = !connected ? 'camera not available' : late ? 'pictures are late (recorder tab hidden or computer busy)' : null;
  const body = {
    agent_id: S.agentId, hostname: navigator.userAgent.slice(0, 200), version: 'browser', agent_time_ms: Date.now(),
    backlog: await queue.count(), disk_free_bytes: null,
    cameras: [{ id: S.cam.id, name: S.cam.name, connected, fps: st.frameRate || null, brightness: S.brightness,
      frozen: S.sameCount >= 5, error, width: st.width || null, height: st.height || null,
      still_interval_s: S.cam.interval, last_capture_ms: S.lastStill, live_fps: 2, source: 'browser' }],
  };
  try {
    const resp = await api('/api/capture/heartbeat', { method: 'POST', body });
    S.live = (resp.live || []).includes(S.cam.id);
    S.capture = resp.capture?.[S.cam.id] || null;
    if (S.problem?.startsWith('Cannot reach')) problem(null);
  } catch (e) {
    S.live = false;
    problem(`Cannot reach the server (${e.message}). Pictures are kept here and upload when it is back.`);
  }
}

async function sendLive(now) {
  if (S.liveBusy) return;
  S.liveBusy = true;
  try {
    const c = await grab(960);
    if (!c) return;
    const blob = await toBlob(c, 0.7);
    const res = await fetch(`/api/capture/live/${encodeURIComponent(S.cam.id)}`, {
      method: 'POST', body: blob, credentials: 'same-origin', headers: { 'Content-Type': 'image/jpeg', 'X-Captured-Ms': String(now) },
    });
    if (res.ok && !(await res.json()).watching) S.live = false;
  } catch { /* the next heartbeat sorts it out */ } finally {
    S.liveBusy = false;
  }
}

// A worker keeps time even when the browser slows down timers on this page.
const ticker = new Worker(URL.createObjectURL(new Blob(['setInterval(() => postMessage(0), 250);'], { type: 'text/javascript' })));
let lastStats = 0;
ticker.onmessage = () => {
  const now = Date.now();
  if (S.recording) {
    if (now >= S.nextStill) {
      const ms = S.cam.interval * 1000;
      S.nextStill = Math.floor(now / ms) * ms + ms; // aligned to the clock, like the capture program
      takeStill(now);
    }
    if (now - S.lastHeartbeat >= 5000) heartbeat();
    if (S.live && now >= S.liveNext) { S.liveNext = now + 500; sendLive(now); }
  }
  if (now - lastStats >= 1000) { lastStats = now; renderStats(); }
};

async function renderStats() {
  const waiting = await queue.count();
  const tile = (label, value, sub) => el('div', { class: 'stat' }, el('div', { class: 'label' }, label), el('div', { class: 'value' }, value),
    sub ? el('div', { class: 'sub' }, sub) : null);
  $('#stats').replaceChildren(
    tile('Pictures saved', String(S.saved), S.lastStill ? `last ${ago(S.lastStill)}` : 'none yet'),
    tile('Uploaded', String(S.uploaded), S.lastUpload ? `last ${ago(S.lastUpload)}` : '–'),
    tile('Waiting to upload', String(waiting), waiting > 50 ? 'internet slow or down?' : 'kept on this computer until sent'),
    tile('Live view', S.live ? 'Someone is watching' : 'Nobody watching', S.live ? 'sending 2 pictures a second' : 'starts when the dashboard opens it'),
  );
  const st = $('#state');
  st.className = `pill ${S.recording ? 'live' : ''}`;
  st.textContent = S.recording ? 'Recording' : 'Not recording';
}

async function keepAwake() {
  try { S.wakeLock = await navigator.wakeLock?.request('screen'); } catch { S.wakeLock = null; }
}

async function start() {
  const computer = form.computer.value.trim();
  const name = form.name.value.trim();
  if (!computer || !name) return;
  if (S.track?.readyState !== 'live' && !(await openCamera(form.device.value))) return;
  const key = `${slug(computer)}|${slug(name)}`;
  const ids = mem.get('ids', {});
  ids[key] = ids[key] || `web-${slug(name).slice(0, 30)}-${Math.random().toString(36).slice(2, 6)}`;
  mem.set('ids', ids);
  mem.set('computer', computer);
  mem.set('name', name);
  mem.set('device', form.device.value);
  mem.set('interval', form.interval.value);
  S.agentId = `browser-${slug(computer)}`;
  S.cam = { id: ids[key], name, interval: Number(form.interval.value) };
  S.recording = true;
  S.nextStill = 0;
  mem.set('active', true);
  await keepAwake();
  await heartbeat();
  for (const n of [form.computer, form.device, form.name, form.interval]) n.disabled = true;
  $('#start').classList.add('hidden');
  $('#stop').classList.remove('hidden');
  toast('Recording. Keep this tab open; other pages open in a new tab.');
}

function stop() {
  S.recording = false;
  mem.set('active', false);
  S.live = false;
  S.wakeLock?.release?.();
  for (const n of [form.computer, form.device, form.name, form.interval]) n.disabled = false;
  $('#start').classList.remove('hidden');
  $('#stop').classList.add('hidden');
  upload();
}

form.addEventListener('submit', e => { e.preventDefault(); start(); });
$('#stop').addEventListener('click', () => { if (confirm('Stop recording? Pictures not yet uploaded will still be sent.')) stop(); });
form.device.addEventListener('change', () => openCamera(form.device.value));
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && S.recording) keepAwake();
  if (S.recording) heartbeat();
});
window.addEventListener('beforeunload', e => { if (S.recording) { e.preventDefault(); e.returnValue = ''; } });

(async () => {
  form.computer.value = mem.get('computer', '') || '';
  form.interval.value = mem.get('interval', '10') || '10';
  await queue.open();
  upload(); // send anything left over from last time
  await listCameras();
  renderStats();
  // Was this page recording when it was closed or reloaded? Carry on by itself.
  if (mem.get('active') && form.computer.value && form.name.value && !$('#start').disabled) {
    await start();
    toast('Recording resumed');
  }
})();
