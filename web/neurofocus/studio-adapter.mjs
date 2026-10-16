// Read-only bridge to existing Studio sessions. It never creates/cancels backend sessions.
import { normalizeFocus } from './core.mjs';
const finite = Number.isFinite;
const terminal = new Set(['done', 'failed', 'error', 'cancelled', 'canceled']);
export function studioMetadata(session, config) {
  const runtime = session.runtime || {}, kind = runtime.source_kind;
  const score = config.focus_score;
  if (!['lsl', 'sim'].includes(kind) || !score || !Array.isArray(score.range)
    || score.range.length !== 2 || !score.range.every(finite) || score.range[1] <= score.range[0]
    || !score.algorithm_id || !score.algorithm_version) throw new Error('后端缺少来源或评分定义，请更新并重启 Studio');
  const note = runtime.source_note || '';
  if (kind === 'lsl' && !note) throw new Error('LSL 数据源尚未准备好，等待采集连接后再试');
  const simulated = kind === 'sim' || note.includes('内置仿真') || note.includes('ningsi-sim-outlet-v1');
  const interval = Number(config.step_sec) * Number(session.time_scale || 1) * 1000;
  if (!finite(interval) || interval <= 0 || !finite(config.window_sec)) throw new Error('缺少窗口/评分更新周期');
  return { type: 'device_meta', schemaVersion: '1.0', source: simulated ? 'demo' : 'live',
    transport: 'Studio REST + SSE', backendSessionId: session.uuid, backendSource: runtime.source || session.source,
    sourceNote: note, sourceKind: kind, simulated, backendTimeScale: session.time_scale, focusIntervalMs: interval,
    focusTimeBasis: 'Server emission epoch adjusted by HTTP Date offset; backendTEnd is session-relative seconds; not device sample timestamps',
    eegTimeBasis: 'server-frame-relative',
    device: { id: session.uuid, model: session.device || session.source || 'Studio', channels: [], samplingRateHz: null, eegUnit: null },
    capabilities: { rawEEG: false, focus: true, bandPower: !!config.bands && Object.keys(config.bands).length > 0, signalQuality: true },
    scoreInfo: { range: score.range, algorithmId: score.algorithm_id, algorithmVersion: String(score.algorithm_version), calibrated: false, displayLabel: '个体基线专注指数' },
    bandPowerInfo: { valueKind: 'relative', unit: 'ratio', windowSeconds: config.window_sec, bandsHz: config.bands || {} } };
}

export class StudioAdapter {
  constructor(emit, { fetcher = (...args) => fetch(...args), eventSource = (url) => new EventSource(url), clock = () => Date.now(), token = '', onNotice = () => {}, onTerminal = () => {}, onMetadata = () => {} } = {}) {
    Object.assign(this, { emit, fetcher, eventSource, clock, token, onNotice, onTerminal, onMetadata });
    this.streams = []; this.timer = null; this.seq = 0; this.generation = 0;
    this.meta = null; this.lastEventId = -1; this.lastFrameSeq = -1; this.seenWindows = new Set(); this.offset = null;
  }
  async request(path, signal) {
    const before = this.clock();
    const response = await this.fetcher(path, { signal, headers: this.token ? { 'X-API-Token': this.token } : {} });
    if (!response.ok) throw new Error(`Studio HTTP ${response.status}${response.status === 401 ? '：请检查访问令牌' : ''}`);
    const after = this.clock(), date = Date.parse(response.headers?.get('date'));
    if (this.offset === null && finite(date)) this.offset = date - (before + after) / 2;
    const body = await response.json();
    return body.data ?? body;
  }
  async list() { return (await this.request('/api/sessions?limit=50&page=1')).items || []; }
  async prepare(uuid) {
    if (!/^[a-f\d]{32}$/i.test(uuid)) throw new Error('请选择有效的 Studio 会话');
    this.stop(); this.offset = null; this.controller = new AbortController();
    const generation = this.generation;
    const config = await this.request('/api/config', this.controller.signal);
    const session = await this.request(`/api/sessions/${uuid}`, this.controller.signal);
    if (generation !== this.generation) throw new Error('已取消连接');
    if (this.offset === null) throw new Error('后端未提供 Date 时间基准，无法安全判断事件新鲜度');
    if (!session.runtime?.alive || terminal.has(session.status)) throw new Error('请选择正在运行的会话；历史记录不能作为实时数据');
    this.uuid = uuid; this.meta = studioMetadata(session, config); this.config = config;
    this.staleMs = Math.max(3000, (config.window_sec + config.step_sec) * 1000 + 2000);
    this.meta.serverClockOffsetMs = this.offset;
    this.lastEventId = -1; this.lastFrameSeq = -1; this.seenWindows.clear(); this.seq = 0;
    return this.meta;
  }
  send(e) { this.emit({ ...e, seq: this.seq++ }); }
  status(connection) { this.send({ type: 'device_status', timestampMs: this.clock(), connection }); }
  metadata() { if (this.meta) { this.emit(structuredClone(this.meta)); this.onMetadata(this.meta); } }
  start() {
    if (!this.meta || this.timer !== null) return;
    this.controller = new AbortController(); this.startedAt = this.clock(); this.status('connecting');
    const generation = this.generation;
    const open = (path, names, handler) => {
      const params = new URLSearchParams(path.includes('/signal') ? { hz: '5', seconds: '2', points: '4000' } : {});
      if (this.token) params.set('token', this.token);
      const source = this.eventSource(path + (params.size ? `?${params}` : ''));
      this.streams.push(source);
      for (const name of names) source.addEventListener(name, (event) => {
        if (generation !== this.generation || !event.data) return;
        try { handler(JSON.parse(event.data)); } catch (error) { this.onNotice(`已拒收异常消息：${error.message}`); }
      });
      source.onerror = () => { if (generation === this.generation) { this.status('connecting'); this.onNotice('数据通道中断，浏览器正在重连；旧评分已失效'); } };
    };
    open(`/api/sessions/${this.uuid}/events`, ['window', 'finished', 'cancelled', 'error', 'phase'], (event) => this.handleEvent(event));
    open(`/api/sessions/${this.uuid}/signal`, ['signal'], (frame) => this.handleFrame(frame));
    const poll = async () => {
      if (this.polling) return; this.polling = true;
      try {
        const data = await this.request(`/api/sessions/${this.uuid}/live`, this.controller.signal);
        if (generation !== this.generation) return;
        if (!data.runtime_alive || terminal.has(data.status)) this.finish(`后台会话已${data.status === 'done' ? '完成' : '停止'}，监测已结束`);
      } catch (error) { if (generation === this.generation) { this.status('connecting'); this.onNotice(error.message); } }
      finally { this.polling = false; }
    };
    this.timer = setInterval(poll, 2000); poll();
  }
  fresh(serverEpoch) {
    if (!finite(serverEpoch) || this.offset === null) return false;
    const age = this.clock() - (serverEpoch - this.offset);
    // HTTP Date has one-second resolution. Never replay pre-attach measurements as live.
    return age >= -2000 && age <= this.staleMs && serverEpoch - this.offset >= this.startedAt - 2000;
  }
  handleEvent(event) {
    if (this.uuid && event?.session !== this.uuid) return;
    if (!event || !Number.isSafeInteger(event.id) || event.id <= this.lastEventId) return;
    this.lastEventId = event.id;
    if (['finished', 'cancelled', 'error'].includes(event.type)) { this.finish(`后台会话${event.type === 'finished' ? '已完成' : '已停止'}，不再显示实时评分`); return; }
    if (event.type !== 'window' || !this.fresh(event.emitted_at_ms)) return;
    const p = event.data || {}, key = `${p.phase}:${p.index}`;
    if (this.seenWindows.has(key)) return;
    this.seenWindows.add(key); if (this.seenWindows.size > 1024) this.seenWindows.delete(this.seenWindows.values().next().value);
    if (p.source_kind !== this.meta.sourceKind) { this.finish('后台数据来源发生变化，请重新选择会话'); return; }
    const timestampMs = event.emitted_at_ms - this.offset;
    const score = p.scores?.focus;
    const hasScore = typeof score === 'number' && normalizeFocus(score, this.meta.scoreInfo) !== null;
    const unavailableReason = score !== undefined && score !== null && !hasScore ? 'score_out_of_range'
      : p.indicator_reason || p.reasons?.join(',') || '指标不可用';
    this.status(!hasScore && String(p.indicator_reason).startsWith('baseline') ? 'calibrating' : 'connected');
    const calibrated = hasScore && p.usable === true && p.indicator_available === true;
    if (calibrated) this.onNotice('');
    if (calibrated && !this.meta.scoreInfo.calibrated) { this.meta.scoreInfo.calibrated = true; this.metadata(); }
    const quality = p.usable === true ? 'good' : 'poor';
    this.send({ type: 'signal_quality', timestampMs, overall: quality, channels: {} });
    if (hasScore) this.send({ type: 'focus', timestampMs, score, valid: p.usable === true && p.indicator_available === true,
      quality, algorithmId: this.meta.scoreInfo.algorithmId, confidence: null,
      backendEmittedAtMs: event.emitted_at_ms, backendTEnd: p.t_end, backendEventId: event.id, backendWindow: key });
    else { this.send({ type: 'focus_unavailable', timestampMs, reason: unavailableReason, backendEmittedAtMs: event.emitted_at_ms, backendTEnd: p.t_end }); this.onNotice(`指标暂不可用：${unavailableReason}`); }
    const values = p.relative_band_powers;
    if (p.usable === true && values && Object.keys(values).length) this.send({ type: 'band_power', timestampMs, values, valueKind: 'relative', unit: 'ratio' });
  }
  handleFrame(frame) {
    if (this.uuid && frame?.session !== this.uuid) return;
    if (!frame || !Number.isSafeInteger(frame.seq) || frame.seq <= this.lastFrameSeq || !this.fresh(frame.t * 1000)) return;
    this.lastFrameSeq = frame.seq;
    if (frame.source_kind !== this.meta.sourceKind) { this.finish('波形来源与会话不一致，已停止'); return; }
    if (frame.realtime !== true || !frame.samples) { this.status('disconnected'); this.onNotice('采集端没有新鲜 EEG 样本'); return; }
    const channels = frame.channels;
    if (!Array.isArray(channels) || !channels.length || !finite(frame.srate) || frame.srate < 1 || frame.srate > 2000
      || channels.some((c) => !Array.isArray(c.samples) || c.points !== c.raw_points || c.samples.length !== c.raw_points || c.unit !== 'uV')) {
      this.onNotice('波形帧缺少原始采样位置或单位，拒绝按原始 EEG 绘制'); return;
    }
    const labels = channels.map((c) => c.label);
    if (JSON.stringify(labels) !== JSON.stringify(this.meta.device.channels) || frame.srate !== this.meta.device.samplingRateHz) {
      this.meta.device.channels = labels; this.meta.device.samplingRateHz = frame.srate; this.meta.device.eegUnit = 'µV'; this.meta.capabilities.rawEEG = true; this.metadata();
    }
    this.send({ type: 'eeg_snapshot', timestampMs: frame.t * 1000 - this.offset, timeBasis: 'server-frame-relative',
      samplingRateHz: frame.srate, channelNames: labels, unit: 'µV', samples: channels.map((c) => c.samples) });
  }
  finish(message) { this.stop(); this.status('disconnected'); this.onTerminal(message); }
  stop() { this.generation++; this.controller?.abort(); this.streams.forEach((s) => s.close()); this.streams = []; if (this.timer !== null) clearInterval(this.timer); this.timer = null; this.polling = false; }
}
