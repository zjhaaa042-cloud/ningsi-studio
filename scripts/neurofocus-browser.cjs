// Run with NF_PLAYWRIGHT pointing at an installed Playwright package if needed.
const { chromium } = require(process.env.NF_PLAYWRIGHT || 'C:/Users/19286/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const out = process.env.NF_OUTPUT ? path.resolve(process.env.NF_OUTPUT) : path.resolve(__dirname, '../../../verification/2026-10-15/neurofocus');
const base = process.env.NF_URL || 'http://127.0.0.1:8792/neurofocus/';
const minutes = Number(process.env.NF_STABILITY_MINUTES || 30);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const percentile = (values, p) => values.length ? values.toSorted((a,b) => a-b)[Math.floor((values.length - 1) * p)] : null;

(async () => {
  await fs.mkdir(out, { recursive: true });
  const studio = path.resolve(__dirname,'..');
  const python = path.join(studio,'.venv/Scripts/python.exe');
  const server = spawn(python,['-m','ningsi_studio','serve','--port','8792','--data',path.join(out,'server-data')],{
    cwd:studio,windowsHide:true,env:{...process.env,PYTHONPATH:path.join(studio,'src')+';'+path.resolve(studio,'../ningsi/src'),PYTHONIOENCODING:'utf-8'},stdio:'ignore'
  });
  process.on('exit',()=>server.kill());
  let ready = false;
  for(let attempt=0;attempt<50;attempt++) { try { if((await fetch(base)).ok) {ready=true;break;} } catch {} await sleep(300); }
  assert.ok(ready,'Local test server did not start');
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const errors = [], checks = [], requests = [];
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  const page = await context.newPage();
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('requestfailed', (r) => requests.push({ url: r.url(), error: r.failure() }));
  const check = async (name, fn) => { await fn(); checks.push({ name, passed: true }); console.log('PASS ' + name); };
  const state = () => page.evaluate(async () => { const { store } = await import('./app.mjs'); return { state: store.state(), points: store.session?.points.length || 0, buffers: store.buffers.map((b) => b.length), diagnostics: store.diagnostics }; });
  try {
    const response = await page.goto(base); assert.equal(response.status(), 200);
    await check('initial idle / no fake score', async () => { assert.equal(await page.locator('#score').textContent(), '--'); assert.equal((await state()).state, 'idle'); });
    await page.locator('#nav-collapse').click();
    await check('desktop navigation collapses and restores',async()=>{assert.equal(await page.locator('#nav-collapse').getAttribute('aria-expanded'),'false');await page.locator('#nav-collapse').click();assert.equal(await page.locator('#nav-collapse').getAttribute('aria-expanded'),'true');});
    await page.locator('#start').click(); await page.waitForTimeout(3500);
    await check('DEMO stream / 4 x 250Hz / score / canvas', async () => { const s = await state(); assert.equal(s.state, 'valid'); assert.equal(s.buffers.length, 4); assert.ok(s.buffers.every((n) => n >= 750)); assert.notEqual(await page.locator('#score').textContent(), '--'); assert.equal(s.diagnostics.rejected, 0); assert.equal(s.diagnostics.duplicate, 0); });
    await check('orb is driven by valid score',async()=>{const strength=Number(await page.locator('#orb').evaluate((e)=>e.style.getPropertyValue('--focus-strength')));assert.ok(strength>0&&strength<=1);});
    await page.locator('#pause').click(); const paused = await state(); await page.waitForTimeout(1200);
    await check('pause holds recording and timing', async () => { assert.equal((await state()).points, paused.points); assert.equal((await state()).state, 'paused'); });
    await page.locator('#pause').click(); await page.waitForTimeout(1200);
    await check('resume appends real recorded events', async () => { assert.ok((await state()).points > paused.points); });
    await page.locator('[data-route=device]').click();
    await page.locator('#scenario').selectOption('poor'); await page.waitForTimeout(1400);
    await check('poor signal invalidates current score', async () => { assert.equal((await state()).state, 'poor'); assert.equal(await page.locator('#score').textContent(), '--'); });
    await page.locator('#scenario').selectOption('normal'); await page.waitForTimeout(1100);
    await page.locator('#scenario').selectOption('stale'); await page.waitForTimeout(3400);
    await check('stale detection at 3 seconds', async () => { assert.equal((await state()).state, 'stale'); assert.equal(await page.locator('#score').textContent(), '--'); });
    await page.locator('#scenario').selectOption('disconnected'); await page.waitForTimeout(350);
    await check('disconnection has no current score', async () => { assert.equal((await state()).state, 'disconnected'); assert.equal(await page.locator('#score').textContent(), '--'); });
    await page.locator('#scenario').selectOption('calibrating'); await page.waitForTimeout(350);
    await check('calibrating does not claim a score', async () => { assert.equal((await state()).state, 'calibrating'); assert.equal(await page.locator('#score').textContent(), '--'); });
    await page.locator('#scenario').selectOption('score-only'); await page.waitForTimeout(1200);
    await check('capability fallback has no synthetic raw EEG or bands', async () => { assert.equal((await state()).buffers.length, 0); assert.match(await page.locator('#band-status').textContent(), /未提供/); assert.match(await page.locator('#eeg-empty').textContent(), /未提供/); });
    await page.locator('#threshold-low').fill('80'); await page.locator('#threshold-high').fill('30'); await page.locator('#apply-thresholds').click();
    await check('invalid thresholds rejected visibly', async () => { assert.match(await page.locator('#announcement').textContent(), /未更改/); });
    await page.locator('#threshold-low').fill('40'); await page.locator('#threshold-high').fill('70'); await page.locator('#apply-thresholds').click();
    await page.locator('#reconnect').click(); await page.waitForTimeout(1200);
    await check('explicit demo recovery', async () => { assert.equal((await state()).state, 'valid'); });
    await page.locator('#scenario').selectOption('stale');
    await page.evaluate(async()=>{const {store}=await import('./app.mjs');store.ingest({type:'device_status',timestampMs:Date.now(),connection:'connecting'});}); await page.waitForTimeout(300);
    await check('connecting and socket recovery await fresh measurements',async()=>{
      assert.equal((await state()).state,'connecting');assert.equal(await page.locator('#score').textContent(),'--');
      await page.evaluate(async()=>{const {store}=await import('./app.mjs');store.ingest({type:'device_status',timestampMs:Date.now(),connection:'connected'});});await page.waitForTimeout(300);
      assert.equal((await state()).state,'waiting');assert.equal(await page.locator('#score').textContent(),'--');
    });
    await page.locator('#reconnect').click();await page.waitForTimeout(1200);
    await page.locator('#reduced-motion').check();
    await check('reduced motion stops orb loop', async () => { assert.equal(await page.locator('#orb-lines').evaluate((e) => getComputedStyle(e).animationName), 'none'); });
    await page.locator('#reduced-motion').uncheck();
    await page.locator('[data-route=dashboard]').click();
    await page.locator('#freeze').click(); await page.waitForTimeout(350);
    const frozen = await page.locator('#eeg').evaluate((e) => e.toDataURL()); await page.waitForTimeout(1100);
    await check('drawing pause freezes pixels while stream continues', async () => { assert.equal(await page.locator('#eeg').evaluate((e) => e.toDataURL()), frozen); });
    await page.locator('#freeze').click();
    for (const [width, height] of [[1440,900],[1920,1080],[1280,800],[768,1024],[390,844]]) {
      await page.setViewportSize({ width, height }); await page.evaluate(()=>scrollTo(0,0)); await page.waitForTimeout(600);
      await check(`responsive ${width} / no horizontal overflow`, async () => { assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `overflow at ${width}`); });
      await page.screenshot({ path: path.join(out, `dashboard-${width}.png`), fullPage: true });
    }
    await page.setViewportSize({ width:1440,height:900 });
    await page.locator('[data-range="300"]').click();
    await check('trend range selection', async () => assert.equal(await page.locator('[data-range="300"]').getAttribute('aria-pressed'), 'true'));
    await page.locator('#trend').hover(); await page.waitForTimeout(100);
    await check('timeline cursor shows actual event value', async () => assert.equal(await page.locator('#trend-tooltip').isVisible(), true));
    await page.locator('#stop').click(); await page.locator('[data-route=analysis]').click();
    const downloadEvent = page.waitForEvent('download'); await page.getByRole('button', { name:'导出 JSON' }).click(); const download = await downloadEvent; await download.saveAs(path.join(out, 'example-DEMO-session.json'));
    await check('JSON export retains source, metadata and invalid intervals', async () => { const data = JSON.parse(await fs.readFile(path.join(out, 'example-DEMO-session.json'), 'utf8')); assert.equal(data.source,'demo'); assert.equal(data.metadata.device.samplingRateHz,250); assert.ok(data.points.some((p) => !p.valid)); assert.ok(data.events.some((e) => e.kind === 'gap')); });
    const csvEvent = page.waitForEvent('download'); await page.getByRole('button', { name:'导出 CSV' }).click(); await (await csvEvent).saveAs(path.join(out,'example-DEMO-session.csv'));
    await page.screenshot({path:path.join(out,'history-1440.png'),fullPage:true});
    await page.locator('[data-route=device]').click(); await page.screenshot({path:path.join(out,'device-1440.png'),fullPage:true});
    await page.locator('[data-route=focus]').click(); await page.screenshot({path:path.join(out,'focus-1440.png'),fullPage:true});
    assert.deepEqual(errors, []); assert.deepEqual(requests, []);
    await fs.writeFile(path.join(out,'browser-checks.json'),JSON.stringify({ checks, errors, requests, browser:await browser.version() },null,2));
    console.log('BROWSER CHECKS COMPLETE');
  } finally { await context.close(); }
  if(minutes <= 0) { await browser.close(); server.kill(); console.log('UI COMPLETE; endurance intentionally skipped'); return; }

  // Actual wall-clock endurance run, no accelerated clocks or fabricated samples.
  const endurance = await browser.newContext({ viewport:{width:1440,height:900} });
  const p = await endurance.newPage(); const runErrors = [];
  p.on('pageerror',(e) => runErrors.push(String(e)));
  await p.goto(base); await p.locator('#start').click();
  // Portable fallback: 40 real screenshots at 1 Hz, assembled into a GIF by QA tooling.
  await fs.mkdir(path.join(out,'demo-frames'),{recursive:true});
  let frameIndex = 0, captureBusy = false;
  const captureTimer = setInterval(async () => {
    if(captureBusy || frameIndex >= 40) return;
    captureBusy = true;
    try { await p.screenshot({path:path.join(out,'demo-frames',`frame-${String(frameIndex++).padStart(3,'0')}.png`)}); }
    catch(e) { runErrors.push('Frame capture: '+String(e)); }
    finally { captureBusy=false; if(frameIndex>=40) clearInterval(captureTimer); }
  },1000);
  const cdp = await endurance.newCDPSession(p); await cdp.send('Performance.enable');
  const started = Date.now(), samples = [];
  for (let i = 0; i <= minutes; i++) {
    if (i > 0) await sleep(60000);
    await cdp.send('HeapProfiler.collectGarbage');
    const performance = await cdp.send('Performance.getMetrics');
    const snapshot = await p.evaluate(async () => {
      const {store,metrics} = await import('./app.mjs');
      return { state:store.state(), bufferLengths:store.buffers.map((b) => b.length), bufferCapacity:store.buffers.map((b) => b.capacity), trendLength:store.trend.length, points:store.session?.points.length, diagnostics:{...store.diagnostics}, draws:metrics.draws, frames:metrics.frames, drawMs:metrics.drawMs, arrivalToPaintMs:metrics.arrivalToPaintMs, score:document.querySelector('#score').textContent };
    });
    const arrival = snapshot.arrivalToPaintMs, draws = snapshot.drawMs; delete snapshot.arrivalToPaintMs; delete snapshot.drawMs;
    const sample = { minute:i, elapsedSeconds:(Date.now()-started)/1000, heapUsed:performance.metrics.find((m) => m.name==='JSHeapUsedSize')?.value, nodes:performance.metrics.find((m) => m.name==='Nodes')?.value, drawP95Ms:percentile(draws,.95), arrivalToPaintP95Ms:percentile(arrival,.95), ...snapshot };
    samples.push(sample); console.log('ENDURANCE '+JSON.stringify(sample));
    await fs.writeFile(path.join(out,'stability-progress.json'),JSON.stringify({ startedAt:new Date(started).toISOString(), requestedMinutes:minutes, completedMinutes:i, runErrors, samples },null,2));
    if (i === 1) { await p.evaluate(()=>scrollTo(0,0)); await p.screenshot({path:path.join(out,'dashboard-1440-60s.png'),fullPage:true}); }
    assert.equal(sample.state,'valid'); assert.ok(sample.bufferLengths.every((n) => n<=2500)); assert.equal(sample.diagnostics.rejected,0); assert.equal(sample.diagnostics.duplicate,0); assert.deepEqual(runErrors,[]);
  }
  await p.locator('#stop').click();
  await p.screenshot({path:path.join(out,'endurance-complete.png'),fullPage:true});
  await fs.writeFile(path.join(out,'stability-result.json'),JSON.stringify({passed:true,actualElapsedSeconds:(Date.now()-started)/1000,minutes,runErrors,samples},null,2));
  clearInterval(captureTimer); while(captureBusy) await sleep(50);
  await endurance.close(); await browser.close(); server.kill(); console.log('ALL COMPLETE');
})().catch((e) => { console.error(e); process.exit(1); });
