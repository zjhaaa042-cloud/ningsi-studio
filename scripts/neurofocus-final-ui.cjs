const {chromium}=require(process.env.NF_PLAYWRIGHT || 'C:/Users/19286/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const path=require('node:path');const fs=require('node:fs/promises');const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../../verification/2026-10-15/neurofocus');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:900}});
 const checks=[],errors=[]; page.on('pageerror',(e)=>errors.push(String(e)));
 await page.goto(process.env.NF_URL || 'http://127.0.0.1:8792/neurofocus/');
 await page.keyboard.press('Tab'); assert.equal(await page.evaluate(()=>document.activeElement.className),'skip-link');
 await page.keyboard.press('Enter'); await page.waitForTimeout(100);
 assert.equal(await page.evaluate(()=>document.activeElement.id),'main'); checks.push('keyboard skip link reaches main');
 await page.locator('#start').click(); await page.waitForTimeout(400);
 await page.emulateMedia({reducedMotion:'reduce'}); await page.waitForTimeout(100);
 assert.ok(await page.locator('#orb').evaluate((e)=>e.classList.contains('valid')));
 assert.equal(await page.locator('#orb-lines').evaluate((e)=>getComputedStyle(e).animationName),'none'); checks.push('OS reduced-motion stops orb animation');
 await page.emulateMedia({reducedMotion:'no-preference'});
 await page.waitForTimeout(60000);
 await page.evaluate(()=>scrollTo(0,650));
 const badge=await page.locator('.source-tag').boundingBox(); assert.ok(badge.y>=0 && badge.y<76); checks.push('DEMO badge remains visible while scrolling');
 await page.screenshot({path:path.join(root,'final-ui/source-badge-scrolled.png')});
 for(const [width,height] of [[1440,900],[1920,1080],[1280,800],[768,1024],[390,844]]){
  await page.setViewportSize({width,height});await page.evaluate(()=>scrollTo(0,0));await page.waitForTimeout(500);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:path.join(root,`final-ui/dashboard-${width}.png`),fullPage:true});
 }
 const metrics=await page.evaluate(async()=>{const {metrics,store}=await import('./app.mjs');return {drawMs:metrics.drawMs,arrivalToPaintMs:metrics.arrivalToPaintMs,state:store.state(),diagnostics:store.diagnostics};});
 await page.setViewportSize({width:1440,height:900});await page.locator('#stop').click();
 await page.locator('[data-route=focus]').click();await page.locator('#duration').selectOption('5');await page.locator('#start').click();
 const started=Date.now();console.log('Final responsive screenshots / keyboard / OS motion / persistent DEMO checks passed; actual five-minute training timer started.');
 for(let minute=1;minute<=5;minute++){
  await page.waitForTimeout(60000);
  console.log(`TRAINING ${minute} minutes elapsed`);
 }
 await page.waitForTimeout(750);
 assert.equal(await page.evaluate(async()=>{const {store}=await import('./app.mjs');return store.session;}),null);
 assert.match(await page.locator('#announcement').textContent(),/达到设置/);checks.push('actual 5-minute training auto-stops');
 await page.screenshot({path:path.join(root,'final-ui/training-auto-complete.png'),fullPage:true});
 assert.deepEqual(errors,[]);
 await fs.writeFile(path.join(root,'final-ui/targeted-checks.json'),JSON.stringify({checks,errors,trainingActualElapsedMs:Date.now()-started,metrics},null,2));
 await browser.close();console.log('FINAL UI CHECKS COMPLETE');
})().catch((e)=>{console.error(e);process.exit(1);});
