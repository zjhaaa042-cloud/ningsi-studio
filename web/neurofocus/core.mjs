// Browser-independent data layer. No UI or device SDK assumptions.
export const STALE_MS = 3000;
export const MAX_FOCUS_POINTS = 21600; // 6 hours at 1 Hz; recording stops at the limit.
export const QUALITY = ['good', 'fair', 'poor', 'unknown'];
const finite = (n) => typeof n === 'number' && Number.isFinite(n);
const object = (x) => x !== null && typeof x === 'object' && !Array.isArray(x);
const timestamp = (n) => finite(n) && n > 0;

export class RingBuffer {
  constructor(capacity) {
    if (!Number.isInteger(capacity) || capacity < 1) throw new Error('Invalid capacity');
    this.capacity = capacity;
    this.data = new Array(capacity);
    this.length = 0;
    this.cursor = 0;
  }
  push(value) {
    this.data[this.cursor] = value;
    this.cursor = (this.cursor + 1) % this.capacity;
    this.length = Math.min(this.capacity, this.length + 1);
  }
  values() {
    const start = (this.cursor - this.length + this.capacity) % this.capacity;
    return Array.from({ length: this.length }, (_, i) => this.data[(start + i) % this.capacity]);
  }
  clear() { this.data.fill(undefined); this.length = 0; this.cursor = 0; }
}

export function normalizeFocus(score, info) {
  if (!info || !Array.isArray(info.range) || info.range.length !== 2) return null;
  const [min, max] = info.range;
  if (![score, min, max].every(finite) || max <= min || score < min || score > max) return null;
  return 100 * (score - min) / (max - min);
}

export function validateEvent(e, meta) {
  if (!object(e) || typeof e.type !== 'string') return 'invalid';
  if (e.type === 'device_meta') {
    const d = e.device, c = e.capabilities;
    if (e.schemaVersion !== '1.0' || !['demo', 'live', 'replay'].includes(e.source) || !object(d) || !object(c)) return 'invalid';
    if (typeof d.id !== 'string' || typeof d.model !== 'string' || !Array.isArray(d.channels)
      || d.channels.length > 32 || new Set(d.channels).size !== d.channels.length
      || !d.channels.every((x) => typeof x === 'string' && x.length > 0 && x.length <= 64)) return 'invalid';
    if (!['rawEEG', 'focus', 'bandPower', 'signalQuality'].every((key) => typeof c[key] === 'boolean')) return 'invalid';
    if (c.rawEEG && (!finite(d.samplingRateHz) || d.samplingRateHz < 1 || d.samplingRateHz > 2000
      || !d.channels.length || typeof d.eegUnit !== 'string' || !d.eegUnit)) return 'invalid';
    if (c.focus && (!object(e.scoreInfo) || normalizeFocus(e.scoreInfo.range?.[0], e.scoreInfo) === null
      || typeof e.scoreInfo.algorithmId !== 'string' || typeof e.scoreInfo.algorithmVersion !== 'string'
      || typeof e.scoreInfo.calibrated !== 'boolean')) return 'invalid';
    const b = e.bandPowerInfo;
    if (c.bandPower && (!object(b) || !['relative', 'absolute', 'psd'].includes(b.valueKind)
      || typeof b.unit !== 'string' || !object(b.bandsHz) || !Object.keys(b.bandsHz).length
      || !Object.values(b.bandsHz).every((range) => Array.isArray(range) && range.length === 2
        && range.every(finite) && range[0] >= 0 && range[1] > range[0]))) return 'invalid';
    return 'ok';
  }
  if (!meta) return 'invalid';
  if (e.seq !== undefined && (!Number.isSafeInteger(e.seq) || e.seq < 0)) return 'invalid';
  if (e.type === 'focus_unavailable') return timestamp(e.timestampMs) && typeof e.reason === 'string' ? 'ok' : 'invalid';
  if (e.type === 'eeg_snapshot') {
    const n = e.samples?.[0]?.length;
    return meta.capabilities.rawEEG && timestamp(e.timestampMs) && e.timeBasis === 'server-frame-relative'
      && e.samplingRateHz === meta.device.samplingRateHz && e.unit === meta.device.eegUnit
      && JSON.stringify(e.channelNames) === JSON.stringify(meta.device.channels)
      && Array.isArray(e.samples) && e.samples.length === meta.device.channels.length && n > 0 && n <= 20000
      && e.samples.every((c) => Array.isArray(c) && c.length === n && c.every(finite)) ? 'ok' : 'invalid';
  }
  if (e.type === 'focus') return meta.capabilities.focus && timestamp(e.timestampMs)
    && typeof e.valid === 'boolean' && QUALITY.includes(e.quality)
    && e.algorithmId === meta.scoreInfo.algorithmId && normalizeFocus(e.score, meta.scoreInfo) !== null
    && (e.confidence === undefined || e.confidence === null || (finite(e.confidence) && e.confidence >= 0 && e.confidence <= 1)) ? 'ok' : 'invalid';
  if (e.type === 'eeg_chunk') {
    const n = e.samples?.[0]?.length;
    return meta.capabilities.rawEEG && timestamp(e.startTimestampMs) && e.samplingRateHz === meta.device.samplingRateHz
      && e.unit === meta.device.eegUnit && Array.isArray(e.channelNames)
      && JSON.stringify(e.channelNames) === JSON.stringify(meta.device.channels)
      && Array.isArray(e.samples) && e.samples.length === e.channelNames.length && n > 0 && n <= 2000
      && e.samples.every((c) => Array.isArray(c) && c.length === n && c.every(finite)) ? 'ok' : 'invalid';
  }
  if (e.type === 'band_power') return meta.capabilities.bandPower && timestamp(e.timestampMs)
    && e.valueKind === meta.bandPowerInfo.valueKind && e.unit === meta.bandPowerInfo.unit
    && object(e.values) && Object.keys(e.values).length > 0
    && Object.entries(e.values).every(([k, v]) => meta.bandPowerInfo.bandsHz[k] && finite(v) && v >= 0
      && (e.valueKind !== 'relative' || v <= 1)) ? 'ok' : 'invalid';
  if (e.type === 'signal_quality') return meta.capabilities.signalQuality && timestamp(e.timestampMs)
    && QUALITY.includes(e.overall) && object(e.channels)
    && Object.entries(e.channels).every(([k, v]) => meta.device.channels.includes(k) && QUALITY.includes(v)) ? 'ok' : 'invalid';
  if (e.type === 'device_status') return timestamp(e.timestampMs)
    && ['connecting', 'connected', 'disconnected', 'calibrating', 'error'].includes(e.connection)
    && (e.batteryPercent === null || e.batteryPercent === undefined || (finite(e.batteryPercent) && e.batteryPercent >= 0 && e.batteryPercent <= 100)) ? 'ok' : 'invalid';
  return 'unknown';
}

export function demoMeta(scoresOnly = false) {
  return { type: 'device_meta', schemaVersion: '1.0', source: 'demo',
    device: { id: 'demo-seeded-42', model: 'Synthetic EEG · 演示源', channels: scoresOnly ? [] : ['CH1', 'CH2', 'CH3', 'CH4'], samplingRateHz: scoresOnly ? null : 250, eegUnit: scoresOnly ? null : 'µV' },
    capabilities: { rawEEG: !scoresOnly, focus: true, bandPower: !scoresOnly, signalQuality: true },
    scoreInfo: { algorithmId: 'demo-scenario', algorithmVersion: '1.0', range: [0, 100], calibrated: false, displayLabel: '演示专注度指数' },
    bandPowerInfo: { valueKind: 'relative', unit: 'ratio', windowSeconds: 1, bandsHz: { delta: [0.5, 4], theta: [4, 8], alpha: [8, 13], beta: [13, 30], gamma: [30, 45] } } };
}

export class MockAdapter {
  constructor(emit, { seed = 42, clock = () => Date.now() } = {}) {
    this.emit = emit; this.seed = seed; this.clock = clock;
    this.seq = 0; this.tickIndex = 0; this.scenario = 'normal'; this.timer = null; this.origin = null;
  }
  random() { this.seed = (Math.imul(1664525, this.seed) + 1013904223) >>> 0; return this.seed / 4294967296; }
  send(e) { this.emit({ ...e, seq: this.seq++ }); }
  metadata() { this.emit(demoMeta(this.scenario === 'score-only')); }
  setScenario(scenario) {
    this.scenario = scenario;
    this.metadata();
    this.send({ type: 'device_status', timestampMs: this.clock(), connection: scenario === 'disconnected' ? 'disconnected' : scenario === 'calibrating' ? 'calibrating' : 'connected', batteryPercent: null });
  }
  start() { if (this.timer !== null) return; if (this.origin === null) this.origin = this.clock(); this.nextSampleTime = null; this.metadata(); this.setScenario(this.scenario); this.step(); this.timer = setInterval(() => this.step(), 100); }
  stop() { if (this.timer !== null) clearInterval(this.timer); this.timer = null; }
  step() {
    const now = this.clock();
    if (this.origin === null) this.origin = now;
    const tick = this.tickIndex++;
    if (['stale', 'disconnected', 'calibrating'].includes(this.scenario)) return;
    // Event time follows actual emission time: timer suspension becomes a genuine gap.
    if (this.scenario !== 'score-only') {
      const start = this.nextSampleTime === null || this.nextSampleTime === undefined || now - this.nextSampleTime > 200 ? now - 100 : this.nextSampleTime;
      this.nextSampleTime = start + 100;
      const samples = Array.from({ length: 4 }, (_, ch) => Array.from({ length: 25 }, (_, i) => {
        const t = (start - this.origin) / 1000 + i / 250;
        return 16 * Math.sin(2 * Math.PI * (9 + ch * 0.7) * t + ch) + 7 * Math.sin(2 * Math.PI * 19 * t) + (this.random() - 0.5) * 5;
      }));
      this.send({ type: 'eeg_chunk', startTimestampMs: start, samplingRateHz: 250, channelNames: ['CH1', 'CH2', 'CH3', 'CH4'], unit: 'µV', samples });
    }
    if (tick % 10 !== 0) return;
    const t = tick / 10;
    const base = this.scenario === 'high' ? 87 : this.scenario === 'medium' ? 58 : this.scenario === 'low' ? 28 : 68;
    const score = Math.max(0, Math.min(100, base + 9 * Math.sin(t / 19) + 3 * Math.sin(t / 5)));
    const quality = this.scenario === 'poor' ? 'poor' : 'good';
    this.send({ type: 'signal_quality', timestampMs: now, overall: quality, channels: Object.fromEntries((this.scenario === 'score-only' ? [] : ['CH1', 'CH2', 'CH3', 'CH4']).map((c) => [c, quality])) });
    this.send({ type: 'focus', timestampMs: now, score, valid: quality === 'good', quality, confidence: null, algorithmId: 'demo-scenario' });
    if (this.scenario !== 'score-only') {
      const alpha = 0.26 + Math.sin(t / 11) * 0.04;
      this.send({ type: 'band_power', timestampMs: now, values: { delta: 0.11, theta: 0.16, alpha, beta: 0.62 - alpha, gamma: 0.11 }, valueKind: 'relative', unit: 'ratio' });
    }
  }
}

export class StreamStore {
  constructor({ clock = () => Date.now(), staleMs = STALE_MS, thresholds = [40, 70], dwellMs = 2000 } = {}) {
    this.clock = clock; this.staleMs = staleMs; this.thresholds = thresholds; this.dwellMs = dwellMs;
    this.meta = null; this.connection = 'idle'; this.latest = null; this.bands = null;
    this.quality = 'unknown'; this.qualityAt = null; this.receivedAt = null;
    this.focusReceivedAt = null; this.eegReceivedAt = null; this.bandsReceivedAt = null;
    this.lastSeq = -1; this.lastFocusTime = -1; this.lastEegEnd = -1;
    this.buffers = []; this.trend = new RingBuffer(1800); this.events = new RingBuffer(160);
    this.diagnostics = { rejected: 0, unknown: 0, duplicate: 0, accepted: 0 };
    this.session = null; this.sessions = []; this.level = null; this.candidate = null; this.candidateSince = 0;
  }
  log(kind, detail) {
    const item = { timestampMs: this.clock(), kind, detail }; this.events.push(item);
    if (this.session?.status === 'recording') {
      if (this.session.events.length < 5000) this.session.events.push(item);
      else this.session.omittedEventCount++;
    }
  }
  resetStream(metadata) {
    if (this.session) throw new Error('请先结束当前监测记录，再切换数据源');
    const previous = this.sessions;
    const options = { clock: this.clock, staleMs: this.staleMs, thresholds: this.thresholds, dwellMs: this.dwellMs };
    Object.assign(this, new StreamStore(options)); this.sessions = previous;
    this.ingest(metadata);
  }
  ingest(event) {
    const validation = validateEvent(event, this.meta);
    if (validation !== 'ok') { this.diagnostics[validation === 'unknown' ? 'unknown' : 'rejected']++; return false; }
    if (event.type === 'device_meta') {
      // Capabilities can change, but metadata cannot silently change source mid-recording.
      if (this.session && event.source !== this.session.source) { this.diagnostics.rejected++; return false; }
      const changed = JSON.stringify(this.meta?.device) !== JSON.stringify(event.device);
      this.meta = structuredClone(event);
      if (changed) { this.buffers = event.device.channels.map(() => new RingBuffer(Math.ceil(event.device.samplingRateHz * 10))); this.lastEegEnd = -1; this.eegReceivedAt = null; }
      if (!event.capabilities.bandPower) this.bands = null;
      if (this.session?.status === 'recording') {
        if (this.session.metadataChanges.length < 500) this.session.metadataChanges.push({ timestampMs: this.clock(), metadata: structuredClone(event) });
        else this.session.omittedEventCount++;
      }
      this.log('metadata', '数据能力更新'); return true;
    }
    if (event.seq !== undefined && event.seq <= this.lastSeq) { this.diagnostics.duplicate++; return false; }
    if (event.type === 'focus' && event.timestampMs <= this.lastFocusTime) { this.diagnostics.duplicate++; return false; }
    if (event.type === 'eeg_chunk' && event.startTimestampMs < this.lastEegEnd - 0.1) { this.diagnostics.duplicate++; return false; }
    if (event.seq !== undefined) this.lastSeq = event.seq;
    this.receivedAt = this.clock(); this.diagnostics.accepted++;
    if (event.type === 'device_status') {
      const previousConnection = this.connection;
      this.connection = event.connection;
      this.log('connection', event.connection);
      if (previousConnection !== event.connection || ['connecting', 'disconnected', 'error', 'calibrating'].includes(event.connection)) {
        this.latest = null; this.bands = null; this.level = null; this.focusReceivedAt = null;
        this.quality = 'unknown'; this.qualityAt = null;
      }
    }
    if (event.type === 'signal_quality') { this.quality = event.overall; this.qualityAt = this.clock(); }
    if (event.type === 'focus_unavailable') {
      this.latest = null; this.focusReceivedAt = null; this.level = null;
      const point = { ...event, score: null, normalized: null, valid: false, source: this.meta.source, receivedAt: this.clock() };
      this.trend.push(point);
      if (this.session?.status === 'recording') {
        this.session.points.push(point);
        if (this.session.points.length >= MAX_FOCUS_POINTS) this.stopSession();
      }
    }
    if (event.type === 'focus') {
      const now = this.clock();
      const qualityOK = event.quality === 'good' && this.quality === 'good' && this.qualityAt !== null && now - this.qualityAt <= this.staleMs;
      const valid = event.valid && qualityOK && this.connection === 'connected';
      if (this.lastFocusTime > 0 && event.timestampMs - this.lastFocusTime > (this.meta.focusIntervalMs || 1000) * 1.5) this.log('gap', `专注事件缺口 ${event.timestampMs - this.lastFocusTime}ms`);
      this.lastFocusTime = event.timestampMs; this.focusReceivedAt = now;
      const point = { ...event, scoreInfo: structuredClone(this.meta.scoreInfo), receivedAt: now, normalized: normalizeFocus(event.score, this.meta.scoreInfo), valid, source: this.meta.source };
      this.latest = point; this.trend.push(point);
      if (valid) this.updateLevel(point.normalized, now); else this.level = null;
      if (this.session?.status === 'recording') {
        this.session.points.push(structuredClone(point));
        if (this.session.points.length >= MAX_FOCUS_POINTS) { this.log('limit', '已达到 21600 点录制上限，自动结束'); this.stopSession(); }
      }
    }
    if (event.type === 'eeg_chunk') {
      const start = event.startTimestampMs, dt = 1000 / event.samplingRateHz;
      if (this.lastEegEnd > 0 && start - this.lastEegEnd > dt * 1.5) this.log('gap', `EEG 缺口 ${Math.round(start - this.lastEegEnd)}ms`);
      event.samples.forEach((channel, ch) => channel.forEach((value, i) => this.buffers[ch].push({ t: start + i * dt, value })));
      this.lastEegEnd = start + event.samples[0].length * dt; this.eegReceivedAt = this.clock();
    }
    if (event.type === 'band_power') { this.bands = structuredClone(event); this.bandsReceivedAt = this.clock(); }
    if (event.type === 'eeg_snapshot') {
      const dt = 1000 / event.samplingRateHz;
      this.buffers.forEach((b) => b.clear());
      event.samples.forEach((channel, ch) => channel.forEach((value, i) => this.buffers[ch].push({ t: event.timestampMs - (channel.length - 1 - i) * dt, value })));
      this.lastEegEnd = event.timestampMs; this.eegReceivedAt = this.clock();
    }
    return true;
  }
  updateLevel(value, now) {
    const [low, high] = this.thresholds;
    const next = value < low ? '低区间' : value < high ? '中区间' : '高区间';
    if (this.level === null) { this.level = next; this.candidate = null; this.candidateSince = 0; return; }
    if (next === this.level) { this.candidate = null; return; }
    if (this.candidate !== next) { this.candidate = next; this.candidateSince = now; }
    if (now - this.candidateSince >= this.dwellMs) { this.level = next; this.candidate = null; }
  }
  state(now = this.clock()) {
    if (this.session?.status === 'paused') return 'paused';
    if (['connecting', 'disconnected', 'error', 'idle'].includes(this.connection)) return this.connection;
    if (this.connection === 'calibrating') return 'calibrating';
    if (this.quality === 'poor' && this.qualityAt !== null && now - this.qualityAt <= this.staleMs) return 'poor';
    if (this.focusReceivedAt === null || this.latest === null) return 'waiting';
    if (now - this.focusReceivedAt > this.staleMs) return 'stale';
    if (!this.latest?.valid || this.quality !== 'good' || this.qualityAt === null || now - this.qualityAt > this.staleMs) return 'poor';
    return 'valid';
  }
  startSession() {
    if (!this.meta || this.session) return false;
    const now = this.clock();
    this.session = { id: `nf-${now}`, source: this.meta.source, metadata: structuredClone(this.meta), metadataChanges: [], omittedEventCount: 0, startedAt: now, endedAt: null, status: 'recording', points: [], events: [], activeMs: 0, resumedAt: now };
    this.log('session', '开始记录'); return true;
  }
  pauseSession() {
    if (this.session?.status !== 'recording') return false;
    this.log('session', '暂停记录'); this.session.activeMs += this.clock() - this.session.resumedAt;
    this.session.status = 'paused'; this.session.resumedAt = null; return true;
  }
  resumeSession() {
    if (this.session?.status !== 'paused') return false;
    this.latest = null; this.focusReceivedAt = null; this.quality = 'unknown'; this.qualityAt = null;
    this.session.status = 'recording'; this.session.resumedAt = this.clock(); this.log('gap', '暂停后恢复记录'); return true;
  }
  elapsed(now = this.clock()) { return this.session ? this.session.activeMs + (this.session.status === 'recording' ? now - this.session.resumedAt : 0) : 0; }
  stopSession() {
    if (!this.session) return null;
    this.session.activeMs = this.elapsed(); this.session.status = 'completed'; this.session.endedAt = this.clock();
    const result = this.session; this.sessions.unshift(result); this.sessions.length = Math.min(this.sessions.length, 10);
    this.session = null; this.connection = 'idle'; this.latest = null; this.level = null; return result;
  }
}

export function summarize(points, activeMs = 0, intervalMs = 1000) {
  const values = points.filter((p) => p.valid && finite(p.normalized)).map((p) => p.normalized).sort((a, b) => a - b);
  const n = values.length;
  const expected = Math.max(points.length, Math.ceil(activeMs / intervalMs));
  return { count: points.length, valid: n, coverage: expected ? n / expected : 0,
    mean: n ? values.reduce((a, b) => a + b, 0) / n : null,
    median: n ? (values[Math.floor((n - 1) / 2)] + values[Math.ceil((n - 1) / 2)]) / 2 : null,
    min: n ? values[0] : null, max: n ? values[n - 1] : null };
}

export function sessionExport(session) {
  return { schemaVersion: '1.0', timeBasis: session.metadata.focusTimeBasis || 'Unix epoch milliseconds; event timestamps',
    recording: 'Focus events and gaps only; raw EEG is display-only in bounded buffers/snapshots and is not included in this export',
    ...structuredClone(session), summary: summarize(session.points, session.activeMs, session.metadata.focusIntervalMs || 1000) };
}
export function sessionCSV(session) {
  const text = (v) => `"${String(v ?? '').replaceAll('"', '""')}"`;
  const meta = session.metadata;
  const headers = ['session_id', 'source', 'timestampMs', 'receivedAt', 'raw_score', 'range_min', 'range_max', 'normalized_display', 'valid', 'quality', 'algorithm_id', 'algorithm_version', 'calibrated'];
  const rows = session.points.map((p) => { const info = p.scoreInfo || meta.scoreInfo; return [session.id, session.source, p.timestampMs, p.receivedAt, p.score, ...info.range, p.normalized, p.valid, p.quality, info.algorithmId, info.algorithmVersion, info.calibrated]; });
  return '\uFEFF' + [headers, ...rows].map((row) => row.map(text).join(',')).join('\r\n');
}

// Segments never cross missing/invalid data. Used by both charts and tests.
export function segments(points, maxGapMs, valid = (p) => p.valid) {
  const result = []; let current = []; let last = null;
  for (const p of points) {
    const t = p.timestampMs ?? p.t;
    if (!valid(p)) { if (current.length) result.push(current); current = []; last = null; continue; }
    if (last !== null && t - last > maxGapMs) { if (current.length) result.push(current); current = []; }
    current.push(p); last = t;
  }
  if (current.length) result.push(current); return result;
}

export function minMaxColumns(points, width, start, end) {
  const bins = new Map();
  for (const p of points) {
    if (p.t < start || p.t > end) continue;
    const x = Math.min(width - 1, Math.max(0, Math.floor((p.t - start) / (end - start) * width)));
    const bin = bins.get(x);
    if (bin) { bin.min = Math.min(bin.min, p.value); bin.max = Math.max(bin.max, p.value); }
    else bins.set(x, { x, min: p.value, max: p.value });
  }
  return [...bins.values()];
}
