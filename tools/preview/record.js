// Visit every dashboard page of the running demo and record each /api/ response for the preview.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
// usage: node tools/preview/record.js <out-dir>   (with the demo running on port 8100, login demo/demo)
const OUT = process.argv[2];
const B = 'http://127.0.0.1:8100';
const DATA = path.join(OUT, 'data');
fs.mkdirSync(DATA, { recursive: true });

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, httpCredentials: { username: 'demo', password: 'demo' } });
  const page = await ctx.newPage();
  page.on('dialog', d => d.accept());
  const entries = [];
  const byHash = new Map();
  const t0 = Date.now();
  let n = 0;
  const pending = new Set();
  page.on('response', resp => {
    const url = new URL(resp.url());
    if (!url.pathname.startsWith('/api/')) return;
    const p = (async () => {
      const req = resp.request();
      let body;
      try { body = await resp.body(); } catch { body = Buffer.alloc(0); }
      if (!body.length && req.method() === 'GET') {
        // Pictures the page reads as a stream come back empty here: fetch them again directly.
        try { body = await ctx.request.get(resp.url()).then(r => r.body()); } catch { return; }
      }
      if (!body.length) return;
      const h = resp.headers();
      const ct = h['content-type'] || '';
      const ext = ct.includes('json') ? 'json' : ct.includes('png') ? 'png' : ct.includes('jpeg') ? 'jpg' : ct.includes('csv') ? 'csv' : 'bin';
      const sha = crypto.createHash('sha1').update(body).digest('hex');
      let f = byHash.get(sha);
      if (!f) {
        f = `${String(++n).padStart(5, '0')}.${ext}`;
        fs.writeFileSync(path.join(DATA, f), body);
        byHash.set(sha, f);
      }
      const keep = {};
      for (const k of ['content-type', 'x-kind', 'x-captured-ms', 'x-frame-id']) if (h[k]) keep[k] = h[k];
      entries.push({ m: req.method(), p: url.pathname, q: Object.fromEntries(url.searchParams), at: Date.now() - t0,
        s: resp.status(), h: keep, f, body: req.method() !== 'GET' ? (req.postData() || null) : undefined });
    })();
    pending.add(p);
    p.finally(() => pending.delete(p));
  });
  const wait = ms => page.waitForTimeout(ms);
  const go = async (p, ms = 3000) => { await page.goto(B + p, { waitUntil: 'networkidle' }); await wait(ms); };

  // Live: stay a while so the live view and readings move; try metrics, ranges and the big view.
  await go('/', 4000);
  for (const i of [2, 3, 1]) { await page.click(`#metric-seg button:nth-child(${i})`).catch(() => {}); await wait(1500); }
  for (const i of [1, 3, 5, 2]) { await page.click(`#range-seg button:nth-child(${i})`).catch(() => {}); await wait(1500); }
  await page.click('.viewport').catch(() => {});
  await wait(8000);
  await page.click('#zoom-close').catch(() => {});
  await wait(26000);

  // Lined up: live, then go back in time and play.
  await go('/lined.html', 9000);
  await page.click('[data-mode="time"]');
  await wait(3000);
  await page.selectOption('#speed', '3').catch(() => {});
  await page.evaluate(() => { const s = document.querySelector('#slider'); s.value = 0; s.dispatchEvent(new Event('input')); });
  await wait(1500);
  await page.click('#play');
  await wait(14000);
  await page.click('#play');
  for (let i = 0; i < 4; i++) { await page.click('#next'); await wait(700); }
  const periods = await page.$$eval('#period option', os => os.map(o => o.value));
  if (periods.length > 1) { await page.selectOption('#period', periods[periods.length - 2]); await wait(3000); }

  // Cameras, Calibrate, Review, Results, Data.
  await go('/cameras.html', 5000);
  await go('/calibrate.html', 2000);
  const devs = await page.$$('#device-list .list-item');
  for (let i = 0; i < devs.length; i++) {
    await page.click(`#device-list .list-item >> nth=${i}`);
    await wait(1500);
    await page.click('#test');
    await wait(2500);
  }
  await go('/review.html', 3000);
  await page.click('.review-card button:has-text("Full picture")').catch(() => {});
  await wait(2000);
  await go('/results.html', 3000);
  for (const avg of ['60', '300', '900', '3600']) { await page.selectOption('select[name=avg]', avg); await wait(2000); }
  for (const m of ['co2', 'temp', 'pm25']) { await page.selectOption('select[name=metric]', m); await wait(2000); }
  await page.selectOption('select[name=avg]', '60');
  await wait(2000);
  const refs = await page.$$eval('select[name=reference] option', os => os.map(o => o.value));
  for (const r of refs) { await page.selectOption('select[name=reference]', r); await wait(2000); }
  await go('/data.html', 3000);
  await page.click('#browse-form button[type=submit]');
  await wait(1500);
  for (let i = 0; i < 3; i++) { await page.click('#prev'); await wait(1200); }
  await go('/capture.html', 2000);
  await Promise.all([...pending]);
  const recordedAt = Date.now();
  fs.writeFileSync(path.join(OUT, 'index.json'), JSON.stringify({ recorded_at: recordedAt, entries }));
  console.log(JSON.stringify({ entries: entries.length, files: n, seconds: Math.round((recordedAt - t0) / 1000) }));
  await browser.close();
})();
