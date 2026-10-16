const {chromium}=require(process.env.NF_PLAYWRIGHT || 'C:/Users/19286/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
const base='http://127.0.0.1:8796',out=path.resolve(__dirname,'../../../verification/2026-10-16/neurofocus');
async function api(p,body){const r=await fetch(base+p,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const b=await r.json();if(!r.ok)throw Error(JSON.stringify(b));return b.data??b;}
async function until(fn,timeout=300000){const start=Date.now();while(Date.now()-start<timeout){const v=await fn();if(v)return v;await new Promise(r=>setTimeout(r,500));}throw Error('Timeout awaiting backend evidence');}
(async()=>{
 await fs.mkdir(out,{recursive:true});
 const created=await api('/api/sessions',{participant:'p90',device:'lsl:nf-test-sim-eeg',time_scale:1.0,training_mode:'quick',protocol:'full',create_subject:true});
 const uuid=created.uuid || created.session?.uuid;assert.ok(uuid);
 await until(async()=> (await api('/api/sessions/'+uuid)).runtime?.source_note);
 const browser=await chromium.launch({channel:'msedge',headless:true});const context=await browser.newContext({viewport:{width:1440,height:900},acceptDownloads:true});const page=await context.newPage();
 const errors=[],checks=[];page.on('pageerror',e=>errors.push(String(e)));
 const snapshot=()=>page.evaluate(async()=>{const {store}=await import('./app.mjs');return {state:store.state(),meta:store.meta,points:store.session?.points.length||0,buffers:store.buffers.map(b=>b.length),diagnostics:store.diagnostics,latest:store.latest};});
 try{
  await page.goto(base+'/neurofocus/?session='+uuid+'#/device');await page.waitForFunction(()=>document.querySelector('#studio-session').value.length===32);
  assert.equal(await page.locator('#source').inputValue(),'studio');assert.equal(await page.locator('#score').textContent(),'--');
  checks.push('deep link selects backend session but does not auto-start');
  await page.locator('#start').click();await until(async()=> (await snapshot()).buffers.some(n=>n>0));
  const initial=await snapshot();assert.equal(initial.meta.source,'demo');assert.equal(initial.meta.sourceKind,'lsl');assert.equal(initial.meta.device.channels.length,2);assert.equal(initial.meta.device.samplingRateHz,250);
  assert.match(await page.locator('#source-badge').textContent(),/DEMO/);checks.push('actual LSL transport / 2 channels 250Hz / simulated provenance DEMO');
  await page.locator('[data-route=dashboard]').click();await page.screenshot({path:path.join(out,'lsl-awaiting-baseline.png'),fullPage:true});
  console.log('LSL waveform received; waiting for backend individual baseline and valid score');
  const scored=await until(async()=>{const s=await snapshot();return s.state==='valid'&&s.latest?.valid?s:null;});
  assert.equal(scored.latest.scoreInfo.range[1],1);assert.equal(scored.meta.scoreInfo.calibrated,true);assert.ok(scored.latest.backendEventId>0);checks.push('backend baseline / valid score / original range / event provenance');
  await page.screenshot({path:path.join(out,'lsl-dashboard-1440.png'),fullPage:true});
  await page.locator('#pause').click();const paused=(await snapshot()).points;await page.waitForTimeout(1100);assert.equal((await snapshot()).points,paused);assert.equal((await api('/api/sessions/'+uuid+'/live')).runtime_alive,true);checks.push('frontend pause leaves backend runtime running');
  await page.locator('#pause').click();await until(async()=> (await snapshot()).state==='valid',20000);checks.push('resume accepts fresh backend events');
  await api('/api/__test__/outlet/stop',{});await until(async()=> (await snapshot()).state!=='valid',20000);await page.waitForTimeout(1000);assert.equal(await page.locator('#score').textContent(),'--');checks.push('stopping simulated LSL invalidates current score');
  await page.screenshot({path:path.join(out,'lsl-disconnected.png'),fullPage:true});
  await api('/api/__test__/outlet/start',{});await until(async()=> (await snapshot()).state==='valid',60000);checks.push('LSL source recovery returns fresh valid score');
  await page.setViewportSize({width:390,height:844});await page.evaluate(()=>scrollTo(0,0));await page.waitForTimeout(600);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(out,'lsl-dashboard-390.png'),fullPage:true});checks.push('backend mode readable at 390px');
  await page.setViewportSize({width:1440,height:900});await page.locator('#stop').click();assert.equal((await api('/api/sessions/'+uuid+'/live')).runtime_alive,true);checks.push('ending frontend monitor does not cancel backend experiment');
  await page.locator('[data-route=analysis]').click();const pending=page.waitForEvent('download');await page.getByRole('button',{name:'导出 JSON'}).click();await(await pending).saveAs(path.join(out,'studio-LSL-DEMO-session.json'));
  const exported=JSON.parse(await fs.readFile(path.join(out,'studio-LSL-DEMO-session.json'),'utf8'));assert.equal(exported.source,'demo');assert.equal(exported.metadata.backendSessionId,uuid);assert.ok(exported.points.some(p=>p.valid&&p.backendEventId));checks.push('export retains backend session, source and measured event provenance');
  assert.deepEqual(errors,[]);
  await fs.writeFile(path.join(out,'studio-browser-results.json'),JSON.stringify({passed:true,uuid,checks,errors,initial,scored,final:await snapshot(),browser:await browser.version(),boundary:'Synthetic EEG over actual LSL; no human or real-hardware validity claim'},null,2));
  console.log('PASS '+checks.length+' Studio/LSL browser checks');
 }finally{await context.close();await browser.close();try{await fetch(base+'/api/sessions/'+uuid,{method:'DELETE'});}catch{}}
})().catch(e=>{console.error(e);process.exit(1)});
