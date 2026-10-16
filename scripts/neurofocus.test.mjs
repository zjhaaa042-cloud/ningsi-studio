import test from 'node:test';
import assert from 'node:assert/strict';
import { RingBuffer, normalizeFocus, demoMeta, validateEvent, MockAdapter, StreamStore, summarize, segments, minMaxColumns, sessionCSV, sessionExport, MAX_FOCUS_POINTS } from '../web/neurofocus/core.mjs';

const meta = demoMeta();
function fixture() {
  let now = 100000;
  const store = new StreamStore({ clock: () => now });
  store.ingest(meta);
  const status = (connection = 'connected') => store.ingest({ type: 'device_status', timestampMs: now, connection });
  const quality = (overall = 'good') => store.ingest({ type: 'signal_quality', timestampMs: now, overall, channels: {} });
  const focus = (extra = {}) => store.ingest({ type: 'focus', timestampMs: now, score: 82, valid: true, quality: 'good', algorithmId: 'demo-scenario', ...extra });
  return { store, status, quality, focus, advance: (ms) => { now += ms; }, time: () => now };
}
test('ring buffer stays bounded and returns chronological order', () => { const b = new RingBuffer(3); for (let i = 0; i < 100; i++) b.push(i); assert.deepEqual(b.values(), [97, 98, 99]); assert.equal(b.data.length, 3); b.clear(); assert.deepEqual(b.values(), []); });
test('normalization requires declared finite ascending range', () => { assert.equal(normalizeFocus(.82, { range: [0, 1] }), 82); for (const info of [null, { range: [1, 1] }, { range: [2, 1] }, { range: [0, Infinity] }]) assert.equal(normalizeFocus(1, info), null); assert.equal(normalizeFocus(120, { range: [0, 100] }), null); assert.equal(normalizeFocus(NaN, { range: [0, 100] }), null); });
test('metadata validates scientific meanings and capabilities', () => { assert.equal(validateEvent(meta), 'ok'); const invalid = structuredClone(meta); invalid.device.samplingRateHz = 0; assert.equal(validateEvent(invalid), 'invalid'); invalid.device.samplingRateHz = 250; delete invalid.bandPowerInfo; assert.equal(validateEvent(invalid), 'invalid'); });
test('initial state has no connected device or valid score', () => { const { store } = fixture(); assert.equal(store.state(), 'idle'); assert.equal(store.latest, null); assert.equal(store.buffers[0].length, 0); });
test('open connection alone is not valid data', () => { const { store, status } = fixture(); status(); assert.equal(store.state(), 'waiting'); });
test('valid quality and score produce normalized observation', () => { const f = fixture(); f.status(); f.quality(); assert.equal(f.focus(), true); assert.equal(f.store.state(), 'valid'); assert.equal(f.store.latest.normalized, 82); });
test('quality cannot be inferred from high focus', () => { const f = fixture(); f.status(); f.focus(); assert.equal(f.store.state(), 'poor'); assert.equal(f.store.latest.valid, false); });
test('poor quality rejects score feedback and statistical inclusion', () => { const f = fixture(); f.status(); f.quality('poor'); f.focus({ quality: 'poor' }); assert.equal(f.store.state(), 'poor'); assert.equal(summarize(f.store.trend.values()).valid, 0); });
test('stale score expires independently of raw EEG', () => { const f = fixture(); f.status(); f.quality(); f.focus(); f.advance(3001); assert.equal(f.store.state(), 'stale'); });
test('stale quality cannot authorize a new focus score', () => { const f = fixture(); f.status(); f.quality(); f.advance(3001); f.focus(); assert.equal(f.store.state(), 'poor'); });
test('out of range and nonfinite focus are rejected without state changes', () => { const f = fixture(); f.status(); f.quality(); f.focus(); f.advance(1000); for (const score of [101, -1, NaN, Infinity, '82']) assert.equal(f.focus({ score }), false); assert.equal(f.store.latest.score, 82); assert.equal(f.store.diagnostics.rejected, 5); });
test('duplicates and out of order sequence IDs never append', () => { const f = fixture(); f.status(); f.quality(); f.focus({ seq: 20 }); f.advance(1000); assert.equal(f.focus({ seq: 20 }), false); assert.equal(f.focus({ seq: 19 }), false); assert.equal(f.store.trend.length, 1); });
test('out of order event timestamps rejected without sequence IDs', () => { const f = fixture(); f.status(); f.quality(); f.focus(); assert.equal(f.focus(), false); assert.equal(f.focus({ timestampMs: f.time() - 1000 }), false); });
test('unknown event safe to ignore and counted', () => { const f = fixture(); assert.equal(f.store.ingest({ type: 'future_type' }), false); assert.equal(f.store.diagnostics.unknown, 1); });
test('malformed EEG never reaches buffers', () => { const f = fixture(); const e = { type: 'eeg_chunk', startTimestampMs: f.time(), samplingRateHz: 250, channelNames: meta.device.channels, unit: 'µV', samples: [[1, 2], [1], [2, 3], [3, 4]] }; assert.equal(f.store.ingest(e), false); e.samples = [[1, 2], [1, 2], [1, 2], [1, NaN]]; assert.equal(f.store.ingest(e), false); assert.equal(f.store.buffers[0].length, 0); });
test('score only adapter cannot fabricate raw EEG or bands', () => { const f = fixture(); f.store.ingest(demoMeta(true)); assert.equal(f.store.buffers.length, 0); assert.equal(f.store.meta.capabilities.rawEEG, false); assert.equal(f.store.ingest({ type: 'band_power', timestampMs: f.time(), values: { alpha: .2 }, valueKind: 'relative', unit: 'ratio' }), false); });
test('band values require matching metadata and physical validity', () => { const e = { type: 'band_power', timestampMs: 1, values: { alpha: .2 }, valueKind: 'relative', unit: 'ratio' }; assert.equal(validateEvent(e, meta), 'ok'); for (const values of [{ alpha: -1 }, { alpha: Infinity }, { alpha: 2 }, { madeUp: .2 }]) assert.equal(validateEvent({ ...e, values }, meta), 'invalid'); });
test('disconnection clears previous score', () => { const f = fixture(); f.status(); f.quality(); f.focus(); f.status('disconnected'); assert.equal(f.store.latest, null); assert.equal(f.store.state(), 'disconnected'); });
test('connecting and reconnecting cannot reuse earlier valid score or quality', () => {
  const f=fixture(); f.status(); f.quality(); f.focus(); f.status('connecting');
  assert.equal(f.store.state(),'connecting'); assert.equal(f.store.latest,null);
  f.status(); assert.equal(f.store.state(),'waiting'); f.advance(1000); f.focus(); assert.equal(f.store.state(),'poor');
  f.advance(1000); f.quality(); f.focus(); assert.equal(f.store.state(),'valid');
});
test('calibration state does not assert a score', () => { const f = fixture(); f.status('calibrating'); assert.equal(f.store.state(), 'calibrating'); });
test('fixed seed yields identical mock signals', () => { const a = [], b = []; const aa = new MockAdapter((e) => a.push(e), { clock: () => 10000 }); const bb = new MockAdapter((e) => b.push(e), { clock: () => 10000 }); aa.step(); bb.step(); assert.deepEqual(a, b); });
test('mock stale/disconnection stop new observations', () => { const events = []; const a = new MockAdapter((e) => events.push(e), { clock: () => 10000 }); for (const scenario of ['stale', 'disconnected', 'calibrating']) { a.scenario = scenario; a.step(); } assert.equal(events.length, 0); });
test('timer jitter does not cause overlapping mock EEG', () => { let now = 10000; const events = []; const a = new MockAdapter((e) => events.push(e), { clock: () => now }); for (const delta of [100, 99, 101, 98, 102, 800, 100]) { now += delta; a.step(); } const chunks = events.filter((e) => e.type === 'eeg_chunk'); for (let i = 1; i < chunks.length; i++) assert.ok(chunks[i].startTimestampMs >= chunks[i - 1].startTimestampMs + 100); });
test('mock cleanup stops its interval', () => { const a = new MockAdapter(() => {}); a.start(); assert.notEqual(a.timer, null); a.stop(); assert.equal(a.timer, null); a.stop(); });
test('recording lifecycle excludes pause time and paused observations', () => { const f = fixture(); f.status(); f.quality(); f.store.startSession(); f.focus(); f.advance(1000); f.store.pauseSession(); f.advance(5000); f.focus(); assert.equal(f.store.state(), 'paused'); assert.equal(f.store.session.points.length, 1); f.store.resumeSession(); f.advance(1000); f.quality(); f.focus(); const session = f.store.stopSession(); assert.equal(session.activeMs, 2000); assert.equal(session.points.length, 2); assert.equal(f.store.connection, 'idle'); });
test('coverage includes missing time, statistics exclude invalid scores', () => { const p = [{ valid: true, normalized: 80 }, { valid: false, normalized: 0 }, { valid: true, normalized: 60 }]; assert.deepEqual(summarize(p, 5000), { count: 3, valid: 2, coverage: .4, mean: 70, median: 70, min: 60, max: 80 }); });
test('trend segments preserve invalid data and time gaps', () => { const p = [0, 1000, 2000, 3000, 6000].map((t) => ({ timestampMs: t, valid: t !== 2000 })); assert.deepEqual(segments(p, 1500).map((x) => x.length), [2, 1, 1]); });
test('min/max rendering preserves narrow spikes', () => { const bins = minMaxColumns([{ t: 0, value: 1 }, { t: 1, value: 100 }, { t: 2, value: -90 }], 1, 0, 10); assert.equal(bins[0].max, 100); assert.equal(bins[0].min, -90); });
test('classification requires a minimum dwell time', () => { const f = fixture(); f.store.updateLevel(20, 1000); f.store.updateLevel(80, 2000); assert.equal(f.store.level, '低区间'); f.store.updateLevel(80, 4000); assert.equal(f.store.level, '高区间'); });
test('quality recovery does not reuse an old classification dwell timer',()=>{
  const f=fixture(); f.status(); f.quality(); f.focus({score:20});
  f.advance(1000);f.focus({score:80});
  f.advance(1000);f.quality('poor');f.focus({score:80,quality:'poor'});
  f.advance(10000);f.quality();f.focus({score:20});
  f.advance(1000);f.quality();f.focus({score:80});assert.equal(f.store.level,'低区间');
  f.advance(2000);f.quality();f.focus({score:80});assert.equal(f.store.level,'高区间');
});
test('session exports retain provenance, raw scores, invalid points and gaps', () => { const f = fixture(); f.status(); f.quality(); f.store.startSession(); f.focus({ score: 82 }); f.advance(5000); f.quality('poor'); f.focus({ score: 20, quality: 'poor' }); const session = f.store.stopSession(); const data = sessionExport(session); assert.equal(data.source, 'demo'); assert.equal(data.points.length, 2); assert.equal(data.points[1].valid, false); assert.ok(data.events.some((e) => e.kind === 'gap')); assert.ok(sessionCSV(session).includes('"demo-scenario"')); assert.ok(sessionCSV(session).includes('"false"')); });
test('source changes cannot mix real and demo session data', () => { const f = fixture(); f.store.startSession(); assert.equal(f.store.ingest({ ...demoMeta(), source: 'live' }), false); assert.equal(f.store.meta.source, 'demo'); });
test('session and event retention are bounded', () => { const f = fixture(); for (let i = 0; i < 15; i++) { f.store.startSession(); f.advance(100); f.store.stopSession(); } assert.equal(f.store.sessions.length, 10); f.store.startSession(); for (let i = 0; i < 5100; i++) f.store.log('test', 'bounded'); assert.equal(f.store.session.events.length, 5000); assert.ok(f.store.session.omittedEventCount > 0); });
test('recording safety limit ends a session at exactly 21600 points', () => {
  const f=fixture(); f.status(); f.store.startSession();
  for(let i=0;i<MAX_FOCUS_POINTS;i++) { f.advance(1000); f.quality(); f.focus(); }
  assert.equal(f.store.session,null); assert.equal(f.store.sessions[0].points.length,MAX_FOCUS_POINTS);
  assert.equal(f.store.connection,'idle'); assert.equal(f.store.trend.length,1800);
  assert.ok(f.store.sessions[0].events.some((e)=>e.kind==='limit'));
});
