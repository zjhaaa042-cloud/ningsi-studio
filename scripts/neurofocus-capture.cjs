const {chromium} = require(process.env.NF_PLAYWRIGHT || 'C:/Users/19286/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname,'../../../verification/2026-10-15/neurofocus');
(async()=>{
  const folder=path.join(root,'operation-frames'); await fs.mkdir(folder,{recursive:true});
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:900},deviceScaleFactor:1});
  const errors=[];page.on('pageerror',(e)=>errors.push(String(e)));
  await page.goto(process.env.NF_URL || 'http://127.0.0.1:8792/neurofocus/');
  const actions={
    1:async()=>page.locator('#start').click(),
    7:async()=>page.evaluate(()=>scrollTo(0,650)),
    11:async()=>{await page.locator('#pause').click();await page.evaluate(()=>scrollTo(0,0));},
    13:async()=>page.locator('#pause').click(),
    15:async()=>page.locator('[data-route=device]').click(),
    17:async()=>page.locator('#scenario').selectOption('poor'),
    19:async()=>page.locator('[data-route=dashboard]').click(),
    22:async()=>page.locator('[data-route=device]').click(),
    23:async()=>page.locator('#scenario').selectOption('disconnected'),
    24:async()=>page.locator('[data-route=dashboard]').click(),
    27:async()=>page.locator('[data-route=device]').click(),
    28:async()=>page.locator('#reconnect').click(),
    29:async()=>page.locator('[data-route=focus]').click(),
    34:async()=>page.locator('#stop').click(),
    35:async()=>page.locator('[data-route=analysis]').click()
  };
  const started=Date.now();const frames=[];
  for(let second=0;second<40;second++){
    if(actions[second])await actions[second]();
    const target=started+second*1000;
    if(Date.now()<target)await new Promise((r)=>setTimeout(r,target-Date.now()));
    const name=`frame-${String(second).padStart(3,'0')}.png`;
    await page.screenshot({path:path.join(folder,name)});
    frames.push({file:name,capturedAt:new Date().toISOString(),elapsedMs:Date.now()-started});
  }
  const build=JSON.parse(await fs.readFile(path.join(root,'build-manifest.json'),'utf8'));
  await fs.writeFile(path.join(root,'operation-recording.json'),JSON.stringify({width:1440,height:900,source:'DEMO',actualElapsedMs:Date.now()-started,sourceBuildFiles:build.files,frames,errors},null,2));
  await browser.close();console.log('40 operation frames recorded; errors='+errors.length);
})().catch((e)=>{console.error(e);process.exit(1);});
