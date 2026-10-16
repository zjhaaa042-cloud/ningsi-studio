import { spawnSync } from 'node:child_process';
import { readFile, readdir, mkdir, copyFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { createHash } from 'node:crypto';
import assert from 'node:assert/strict';

const studio = fileURLToPath(new URL('../', import.meta.url));
const source = path.join(studio, 'web/neurofocus');
const files = ['index.html', 'style.css', 'core.mjs', 'charts.mjs', 'app.mjs', 'studio-adapter.mjs'];
const out = path.resolve(studio, '../../verification/2026-10-16/neurofocus');
const results = [];
for (const file of files.filter((f) => f.endsWith('.mjs'))) {
  const result = spawnSync(process.execPath, ['--check', path.join(source, file)], { encoding:'utf8' });
  assert.equal(result.status, 0, result.stderr); results.push({ check:`syntax ${file}`, passed:true });
}
const html = await readFile(path.join(source,'index.html'),'utf8');
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
assert.equal(ids.length,new Set(ids).size,'Duplicate HTML IDs');
const app = await readFile(path.join(source,'app.mjs'),'utf8');
for(const [,id] of app.matchAll(/\$\('([^']+)'\)/g)) assert.ok(ids.includes(id),`Missing DOM target ${id}`);
for(const [,asset] of html.matchAll(/(?:src|href)="\.\/([^"]+)"/g)) assert.ok(files.includes(asset),`Missing asset ${asset}`);
for(const file of files.filter((f)=>f.endsWith('.mjs'))) {
  const body = await readFile(path.join(source,file),'utf8');
  for(const [,asset] of body.matchAll(/from '\.\/([^']+)'/g)) assert.ok(files.includes(asset),`Missing import ${asset}`);
  assert.ok(!/\beval\(|new Function\(/.test(body),'Unsafe dynamic evaluation');
}
results.push({check:'HTML references, unique IDs, module imports',passed:true});
const tests=spawnSync(process.execPath,['--test',path.join(studio,'scripts/neurofocus.test.mjs'),path.join(studio,'scripts/neurofocus-studio.test.mjs')],{encoding:'utf8'});
process.stdout.write(tests.stdout); process.stderr.write(tests.stderr);
assert.equal(tests.status,0,'Data-layer tests failed');
await mkdir(out,{recursive:true});
await writeFile(path.join(out,'unit-tests.txt'),tests.stdout+tests.stderr);
const build=path.join(out,'static-build'); await mkdir(build,{recursive:true});
const manifest=[];
for(const file of files) {
  const content = await readFile(path.join(source,file));
  await copyFile(path.join(source,file),path.join(build,file));
  manifest.push({file,bytes:content.length,sha256:createHash('sha256').update(content).digest('hex')});
}
await writeFile(path.join(out,'build-manifest.json'),JSON.stringify({builtAt:new Date().toISOString(),format:'buildless ES modules; serve at /neurofocus/ alongside Studio',checks:results,files:manifest},null,2));
console.log(`Static build verified: ${build}`);
console.log(`No TypeScript / bundler / ESLint toolchain in this existing native JS application; Node syntax, DOM/import contracts and behavior tests applied.`);
