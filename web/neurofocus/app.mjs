import { MockAdapter, StreamStore, demoMeta, summarize, sessionExport, sessionCSV } from './core.mjs';
import { drawTrend, drawEEG, populateOrb } from './charts.mjs';
import { StudioAdapter } from './studio-adapter.mjs';

const $ = (id) => document.getElementById(id);
export const store = new StreamStore();
export const metrics = { draws: 0, frames: 0, drawMs: [], arrivalToPaintMs: [], uiUpdates: 0 };
const demoAdapter = new MockAdapter((event) => { store.ingest(event); });
let adapter = demoAdapter, mode = 'demo', busy = false, operation = 0;
const intervalFor = (session) => session?.metadata.focusIntervalMs || 1000;
const sourceText = () => mode === 'demo' ? 'DEMO · 演示数据' : !store.meta.transport ? 'STUDIO · 待连接' : store.meta.source === 'live' ? 'LIVE · LSL 设备' : 'DEMO · 后端模拟源';
store.ingest(demoMeta());
const labels = { valid: '正在接收演示信号', idle: '未连接', connecting: '正在连接数据源', waiting: '等待数据', poor: '信号质量不足', stale: '数据过期 / 断流', disconnected: '演示连接已断开', calibrating: '演示校准中', paused: '会话已暂停', error: '数据源错误' };
const pages = { dashboard: ['实时总览', '每一刻专注，都有迹可循。', '从此刻开始，观察专注的节奏与变化。'], focus: ['沉浸专注', '留一段时间，给当下。', '温和反馈，安静观察。所有指标均来自演示源。'], analysis: ['会话记录', '看见专注走过的轨迹。', '只统计质量通过的有效点，保留真实记录的缺口。'], device: ['数据与设备', '每一个数值，都有来源。', '查看能力、算法定义与诊断信息，让数据可被追溯。'] };
let route = 'dashboard', range = '60', channel = 'all', windowSeconds = 5, frozen = false, frozenEnd = null;
let hovered = null, reducedMotion = false, lastUIDraw = 0, lastEEGDraw = 0, raf = null, disposed = false;
let lastPaintedArrival = null, lastValidUpdate = null, lastUIState = null;
const media = window.matchMedia('(prefers-reduced-motion: reduce)');
const cleanups = [];
const num = (v) => v === null || v === undefined ? '--' : Number(v).toFixed(1);
const clock = (t) => new Date(t).toLocaleTimeString('zh-CN', { hour12: false });
const elapsed = (ms) => `${String(Math.floor(ms / 60000)).padStart(2, '0')}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`;
function on(node, type, callback) { node.addEventListener(type, callback); cleanups.push(() => node.removeEventListener(type, callback)); }
function notice(text, warning = false) { $('announcement').textContent = text; $('announcement').dataset.tone = warning ? 'warning' : ''; }
function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }

function routeChanged() {
  window.scrollTo({ top: 0, behavior: 'instant' });
  const requested = location.hash.replace('#/', '').split('?')[0]; route = pages[requested] ? requested : 'dashboard';
  if (!pages[requested]) history.replaceState(null, '', '#/dashboard');
  const page = pages[route]; $('page-name').textContent = page[0]; $('page-title').textContent = page[1]; $('page-description').textContent = route === 'focus' ? '温和反馈，安静观察。数据来源以顶部标签为准。' : page[2];
  document.title = `${page[0]} · NeuroFocus`;
  for (const name of Object.keys(pages)) {
    $(`${name}-page`).hidden = name !== route;
    const link = document.querySelector(`[data-route="${name}"]`);
    if (name === route) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  }
  renderHistory(); renderMetadata(); updateUI();
}

function controls() {
  const session = store.session;
  $('start').disabled = !!session || busy;
  $('start').textContent = busy ? '正在连接…' : mode === 'demo' ? '▶ 开始演示会话' : '▶ 开始后台监测';
  $('pause').disabled = !session;
  $('pause').textContent = session?.status === 'paused' ? '▶ 继续' : 'Ⅱ 暂停';
  $('stop').disabled = !session && !busy;
  $('stop').textContent = mode === 'demo' ? '■ 结束' : '■ 结束监测';
  $('session-label').textContent = session ? `${sourceText()} · ${session.status === 'paused' ? '已暂停' : '记录中'}` : '尚未开始监测';
  $('session-clock').textContent = elapsed(store.elapsed());
  $('duration').disabled = !!session;
  for (const id of ['source','studio-session','studio-token','refresh-sessions']) $(id).disabled = !!session || busy;
  $('scenario').disabled = mode !== 'demo'; $('reconnect').disabled = mode !== 'demo';
}
async function start() {
  const id = ++operation;
  if (mode === 'studio') {
    busy = true; controls();
    try {
      adapter = new StudioAdapter((event) => store.ingest(event), { token: $('studio-token').value,
        onNotice: (message) => notice(message, true), onMetadata: () => renderMetadata(),
        onTerminal: (message) => { stop(); notice(message, true); } });
      const meta = await adapter.prepare($('studio-session').value);
      if (id !== operation) return;
      store.resetStream(meta); store.staleMs = adapter.staleMs;
      $('channel').replaceChildren(new Option('全部通道', 'all')); channel = 'all';
      $('wave-window').value = '2'; windowSeconds = 2;
    } catch (error) { if (id === operation) { adapter.stop(); notice(`连接未建立：${error.message}`, true); } return; }
    finally { if (id === operation) { busy = false; controls(); } }
  } else adapter.metadata();
  if (!store.startSession()) return;
  store.log('training', Number($('duration').value) ? `目标时长 ${$('duration').value} 分钟` : '不限时会话');
  lastValidUpdate = null;
  adapter.start(); controls(); renderMetadata();
  notice(mode === 'demo' ? '演示会话已开始，正在记录专注事件。原始 EEG 仅保留最近 10 秒供显示。' : '已附着后台会话。暂停/结束仅影响本页监测记录，不会停止后台检测；评分等待有效信号与个体基线。');
  updateUI();
}
function stop() {
  operation++; busy = false; adapter.stop();
  const session = store.stopSession(); controls();
  if (!session) return;
  const summary = summarize(session.points, session.activeMs, intervalFor(session));
  renderHistory();
  notice(`监测已结束 · 有效覆盖率 ${(summary.coverage * 100).toFixed(0)}% · 有效均值 ${num(summary.mean)}。可在“会话记录”导出。`);
  updateUI();
}
on($('start'), 'click', start);
on($('pause'), 'click', () => {
  if (store.session?.status === 'recording') { store.pauseSession(); adapter.stop(); notice(mode === 'demo' ? '会话已暂停：停止生成与记录新数据，画面停留在暂停时刻。' : '本页监测已暂停，后台检测继续；恢复时仅接收新数据。'); }
  else if (store.resumeSession()) { adapter.start(); notice('已继续记录。暂停区间保留缺口，不补齐数据。'); }
  controls(); updateUI();
});
on($('stop'), 'click', stop);
on($('nav-collapse'), 'click', () => {
  const collapsed = document.body.classList.toggle('nav-collapsed');
  $('nav-collapse').setAttribute('aria-expanded', String(!collapsed));
  $('nav-collapse').setAttribute('aria-label', collapsed ? '展开导航' : '收起导航');
});
on($('scenario'), 'change', () => {
  const value = $('scenario').value;
  adapter.setScenario(value); renderMetadata();
  if (!store.session) store.connection = 'idle';
  notice(`已选择诊断场景：${$('scenario').selectedOptions[0].textContent}。${store.session ? '' : '开始会话后生效。'}`, value !== 'normal');
  updateUI();
});
on($('reconnect'), 'click', () => {
  $('scenario').value = 'normal'; adapter.setScenario('normal'); renderMetadata();
  if (!store.session) store.connection = 'idle';
  notice(store.session ? '已恢复正常演示场景，等待下一批有效数据。' : '正常演示场景已就绪，请开始会话。'); updateUI();
});
async function refreshSessions() {
  const token = $('studio-token').value || new URLSearchParams(location.search).get('token') || document.querySelector('meta[name="ningsi-api-token"]')?.content || '';
  $('studio-token').value = token;
  const client = new StudioAdapter(() => {}, { token });
  $('refresh-sessions').disabled = true;
  try {
    const sessions = await client.list();
    $('studio-session').replaceChildren(new Option('请选择运行中的会话', ''));
    for (const item of sessions.filter((x) => x.status === 'running')) $('studio-session').add(new Option(`${item.participant || ''} · ${item.device || item.source || ''} · ${item.uuid.slice(0,8)}`, item.uuid));
    const requested = new URLSearchParams(location.search).get('session');
    if (requested && [...$('studio-session').options].some((o) => o.value === requested)) $('studio-session').value = requested;
    notice(sessions.some((x) => x.status === 'running') ? '请选择后台会话，再开始监测。连接不会创建新实验。' : '没有运行中的后台会话，请先在 Studio 开始检测。');
  } catch (error) { notice(error.message, true); }
  finally { $('refresh-sessions').disabled = !!store.session || busy; }
}
on($('refresh-sessions'), 'click', refreshSessions);
on($('source'), 'change', () => {
  adapter.stop(); mode = $('source').value;
  store.resetStream(demoMeta()); store.staleMs = 3000; lastValidUpdate = null; hovered = null; frozen = false;
  $('freeze').textContent = 'Ⅱ 暂停绘制'; $('freeze').setAttribute('aria-pressed','false');
  $('studio-options').hidden = mode !== 'studio';
  adapter = demoAdapter;
  if (mode === 'studio') refreshSessions();
  else { demoAdapter.scenario = $('scenario').value; $('channel').replaceChildren(new Option('全部通道','all'), ...[0,1,2,3].map((i)=>new Option(`CH${i+1}`,String(i)))); channel='all'; $('wave-window').value='5'; windowSeconds=5; notice('已选择本地演示源，请显式开始会话。'); }
  renderMetadata(); updateUI();
});
on($('freeze'), 'click', () => { frozen = !frozen; frozenEnd = frozen ? Date.now() : null; $('freeze').textContent = frozen ? '▶ 继续绘制' : 'Ⅱ 暂停绘制'; $('freeze').setAttribute('aria-pressed', String(frozen)); if (frozen) { drawEEG($('eeg'), store, { channel, windowSeconds, end: frozenEnd }); } });
on($('channel'), 'change', () => { channel = $('channel').value; if (frozen) drawEEG($('eeg'), store, { channel, windowSeconds, end: frozenEnd }); });
on($('wave-window'), 'change', () => { windowSeconds = Number($('wave-window').value); if (frozen) drawEEG($('eeg'), store, { channel, windowSeconds, end: frozenEnd }); });
for (const button of document.querySelectorAll('[data-range]')) on(button, 'click', () => {
  range = button.dataset.range;
  for (const item of document.querySelectorAll('[data-range]')) { const active = item === button; item.classList.toggle('selected', active); item.setAttribute('aria-pressed', String(active)); }
  drawTimeline();
});
function motionChanged() { reducedMotion = $('reduced-motion').checked || media.matches; document.body.classList.toggle('reduced-motion', reducedMotion); }
on($('reduced-motion'), 'change', motionChanged); on(media, 'change', motionChanged); motionChanged();
on($('apply-thresholds'), 'click', () => {
  const low = Number($('threshold-low').value), high = Number($('threshold-high').value);
  if (!Number.isFinite(low) || !Number.isFinite(high) || low < 1 || high > 99 || low >= high) { notice('分级未更改：请输入 1 ≤ 中区间起点 < 高区间起点 ≤ 99。', true); return; }
  store.thresholds = [low, high]; store.level = null; store.candidate = null; store.log('thresholds', `演示分级调整为 ${low} / ${high}`);
  notice(`演示分级已更新：${low} / ${high}；该分级未经校准。`); updateUI();
});

const bandNames = { delta: 'Delta', theta: 'Theta', alpha: 'Alpha', beta: 'Beta', gamma: 'Gamma' };
for (const [key, name] of Object.entries(bandNames)) {
  const row = element('div', 'band-row'); row.dataset.band = key;
  const label = element('span', 'band-name', name); label.append(element('small', '', `${demoMeta().bandPowerInfo.bandsHz[key].join('–')} Hz`));
  const track = element('div', 'band-track'); track.append(element('div', 'band-fill'));
  row.append(label, track, element('span', 'band-value', '--')); $('bands').append(row);
}
populateOrb($('orb-lines'), $('orb-particles'));

function timelineData() {
  const session = store.session;
  const points = range === 'session' && session ? session.points : store.trend.values();
  const now = session?.status === 'paused' ? session.startedAt + session.activeMs + (session.resumedAt ? Date.now() - session.resumedAt : 0) : Date.now();
  const lastTime = store.lastFocusTime > 0 ? store.lastFocusTime : null;
  const end = ['paused', 'idle', 'connecting', 'waiting', 'error', 'disconnected', 'stale', 'calibrating'].includes(store.state()) && lastTime ? lastTime : now;
  const startTime = range === 'session' && session ? session.startedAt : end - Number(range === 'session' ? 60 : range) * 1000;
  return { points, start: Math.min(startTime, end - 1000), end };
}
function drawTimeline() { const data = timelineData(); drawTrend($('trend'), data.points, data.start, data.end, hovered, (store.meta.focusIntervalMs || 1000) * 1.5); }
on($('trend'), 'pointermove', (event) => {
  const data = timelineData(), rect = $('trend').getBoundingClientRect();
  const relative = Math.max(0, Math.min(1, (event.clientX - rect.left - 36) / (rect.width - 51)));
  const t = data.start + (data.end - data.start) * relative;
  const visible = data.points.filter((p) => p.timestampMs >= data.start && p.timestampMs <= data.end);
  hovered = visible.reduce((nearest, p) => !nearest || Math.abs(p.timestampMs - t) < Math.abs(nearest.timestampMs - t) ? p : nearest, null);
  if (!hovered) return;
  const tip = $('trend-tooltip'); tip.hidden = false; tip.textContent = `${clock(hovered.timestampMs)} · ${hovered.valid ? num(hovered.normalized) : '质量无效 / 已排除'}`;
  tip.style.left = `${Math.min(Math.max(0, event.clientX - rect.left), Math.max(0, rect.width - 210))}px`; tip.style.top = '0px'; drawTimeline();
});
on($('trend'), 'pointerleave', () => { hovered = null; $('trend-tooltip').hidden = true; drawTimeline(); });

function renderMetadata() {
  const meta = store.meta; if (!meta) return;
  if (mode === 'studio' && meta.transport) {
    const old = $('channel').value;
    $('channel').replaceChildren(new Option('全部通道','all'), ...meta.device.channels.map((c,i)=>new Option(c,String(i))));
    $('channel').value = [...$('channel').options].some((o)=>o.value===old) ? old : 'all'; channel=$('channel').value;
  }
  for (const row of $('bands').children) { const definition=meta.bandPowerInfo?.bandsHz?.[row.dataset.band]; row.hidden=!definition; if (definition) row.querySelector('.band-name small').textContent=`${definition.join('–')} Hz`; }
  $('eeg-kind').textContent=mode==='demo'?'SYNTHETIC EEG':'STUDIO EEG';
  $('band-description').textContent=mode==='demo'?'演示相对功率 · 非实时 FFT 计算':'后端 Welch 计算 · 仅显示可用窗';
  $('trend-legend').textContent=mode==='demo'?'演示专注度':'后端专注度（展示坐标）';
  $('orb-source').textContent=sourceText(); $('channel-note').textContent=mode==='demo'?'CH1—CH4 为演示通道，不对应电极定位':'通道名来自后端；时轴是服务器显示帧的窗内相对时间';
  $('focus-description').textContent='温和反馈，安静观察。数据来源以顶部标签为准。';
  if (mode === 'studio' && !meta.transport) { $('metadata').textContent = '尚未连接后台会话；设备参数与评分定义将在连接后读取。'; return; }
  const items = [ ['设备 / 数据源', meta.device.model], ['算法', `${meta.scoreInfo.algorithmId} / ${meta.scoreInfo.algorithmVersion}`], ['原始评分范围', meta.scoreInfo.range.join(' – ')], ['分数映射', '声明范围线性映射至 0–100'], ['校准状态', meta.scoreInfo.calibrated ? '已建立后端个体基线' : '尚未建立有效个体基线'], ['质量门控', `good 且 valid，${store.staleMs/1000} 秒失效窗口`], ['原始 EEG', meta.capabilities.rawEEG ? `${meta.device.channels.length} 通道 × ${meta.device.samplingRateHz} Hz · ${meta.device.eegUnit}` : '不支持'], ['频段功率', meta.capabilities.bandPower ? `相对功率 · ratio · 窗口 ${meta.bandPowerInfo.windowSeconds}s` : '不支持'], ['延迟 / 电量 / 丢包率', '未提供；不推测'], ['录制内容', '专注事件、质量标记与缺口；不保存原始 EEG'], ['保留策略', '当前浏览期间，最多 10 会话 / 每会话 21600 点'], ['模型置信度', '未提供'] ];
  if (meta.transport) items.push(['后端会话',meta.backendSessionId],['传输/来源',`${meta.transport} · ${meta.sourceNote}`],['时间基准','评分：服务器事件发送时间；EEG：相对服务器显示帧，非设备逐样本时间戳'],['名义评分周期',`${meta.focusIntervalMs} ms；覆盖率据此估计`]);
  $('metadata').replaceChildren(...items.map(([key, value]) => { const item = element('div'); item.append(element('small', '', key), element('strong', '', value)); return item; }));
}
function download(session, format) {
  const body = format === 'json' ? JSON.stringify(sessionExport(session), null, 2) : sessionCSV(session);
  const url = URL.createObjectURL(new Blob([body], { type: format === 'json' ? 'application/json' : 'text/csv;charset=utf-8' }));
  const a = document.createElement('a'); a.href = url; a.download = `${session.id}-${session.source.toUpperCase()}.${format}`; document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function renderHistory() {
  if (!store.sessions.length) { $('history').replaceChildren(element('div', 'history-empty', '还没有已结束的会话。开始一次演示，结束后在这里回看与导出。')); return; }
  $('history').replaceChildren(...store.sessions.map((session) => {
    const summary = summarize(session.points, session.activeMs, intervalFor(session)), row = element('div', 'history-row');
    const title = element('div'); title.append(element('strong', '', `${new Date(session.startedAt).toLocaleDateString('zh-CN')} ${clock(session.startedAt)}`), element('small', '', `${session.source.toUpperCase()} · ${elapsed(session.activeMs)} · ${summary.count} 个评分事件`));
    const mean = element('div'); mean.append(element('small', '', '均值 / 中位数'), element('strong', '', `${num(summary.mean)} / ${num(summary.median)}`));
    const coverage = element('div'); coverage.append(element('small', '', '有效数据覆盖率'), element('strong', '', `${(summary.coverage * 100).toFixed(0)}%`));
    const actions = element('div', 'history-actions'); for (const format of ['json', 'csv']) { const button = element('button', '', `导出 ${format.toUpperCase()}`); button.addEventListener('click', () => download(session, format)); actions.append(button); }
    row.append(title, mean, coverage, actions, element('p', 'history-summary', `有效分数 ${summary.valid} 个 · 最低 ${num(summary.min)} / 最高 ${num(summary.max)} · 无效与缺失时段不计入均值。JSON 包含事件、元数据与时间基准。`)); return row;
  }));
}

function updateUI() {
  const now = Date.now(), state = store.state(), valid = state === 'valid';
  const stateLabel = mode === 'studio' ? ({valid:'正在接收后台数据',disconnected:'后台信号中断',calibrating:'建立个体基线中',paused:'本页监测已暂停',waiting:'等待新的后台分析窗'})[state] || labels[state] : labels[state];
  metrics.uiUpdates++;
  const score = valid ? Math.round(store.latest.normalized) : '--';
  $('score').textContent = String(score); $('focus-score').textContent = String(score);
  $('connection').textContent = `${valid ? '●' : '○'} ${stateLabel}`;
  for (const id of ['source-badge','data-source','footer-source']) $(id).textContent = sourceText(); $('connection').dataset.state = state;
  $('score-status').textContent = valid ? `${store.level || '观察中'} · 演示分级` : stateLabel;
  $('focus-state').textContent = valid ? `${store.level || '观察中'} · ${sourceText()}` : stateLabel;
  document.body.classList.toggle('invalid-score', !valid);
  $('orb').className = `orb ${state}`; $('orb-status').textContent = valid ? '跟随此刻的专注节奏' : stateLabel;
  $('orb').style.setProperty('--focus-strength', valid ? String(store.latest.normalized / 100) : '0');
  $('gauge-value').setAttribute('stroke-dasharray', valid ? `${score} 100` : '0 100');
  $('ring-score').textContent = String(score);
  $('calibration-note').textContent = mode === 'demo' ? '演示分级 · 未校准' : store.meta.scoreInfo.calibrated ? '后端个体基线已建立 · 显示分级仅供参考' : '等待后端有效个体基线';
  const currentQuality = store.qualityAt !== null && now-store.qualityAt <= store.staleMs ? store.quality : 'unknown';
  $('quality-status').textContent = `信号质量：${({good:'良好',fair:'一般',poor:'不足',unknown:'未知'})[currentQuality]}`;
  const session = store.session;
  const summary = summarize(session?.points || [], store.elapsed(), intervalFor(session));
  $('mean').textContent = num(summary.mean); $('coverage').textContent = session ? `${(summary.coverage * 100).toFixed(0)}%` : '--';
  if (valid) lastValidUpdate = store.latest.timestampMs;
  $('last-update').textContent = lastValidUpdate ? clock(lastValidUpdate) : '--';
  $('wall-clock').textContent = clock(now); controls();
  const meta = store.meta, raw = meta.capabilities.rawEEG, rawFresh = store.eegReceivedAt !== null && now - store.eegReceivedAt <= store.staleMs;
  $('sample-rate').textContent = raw ? `${meta.device.samplingRateHz} Hz` : '未提供'; $('channel-count').textContent = raw ? `${meta.device.channels.length} 通道` : '未提供';
  $('data-age').textContent = store.receivedAt !== null ? `${((now - store.receivedAt) / 1000).toFixed(1)} s` : '--';
  $('rejected').textContent = `${store.diagnostics.rejected} / ${store.diagnostics.duplicate}`;
  $('eeg-empty').hidden = raw && rawFresh;
  $('eeg-empty').textContent = !raw ? '此数据源未提供原始 EEG' : '等待数据 · 不补绘缺失样本';
  $('eeg-description').textContent = raw ? `${meta.device.channels.length} 通道 / ${meta.device.samplingRateHz} Hz / ${meta.device.eegUnit} · ${mode === 'studio' ? '后端未抽稀显示快照 · 窗内相对时间' : '合成演示波形'}` : '仅提供聚合专注度，无法显示原始 EEG';
  $('freeze').disabled = !raw; $('channel').disabled = !raw; $('wave-window').disabled = !raw || mode === 'studio';
  const bandsFresh = store.quality === 'good' && store.qualityAt !== null && now - store.qualityAt <= store.staleMs && ['connected','calibrating'].includes(store.connection) && meta.capabilities.bandPower && store.bandsReceivedAt !== null && now - store.bandsReceivedAt <= store.staleMs;
  for (const row of $('bands').children) { const value = bandsFresh ? store.bands?.values[row.dataset.band] : null; row.querySelector('.band-value').textContent = value === undefined || value === null ? '--' : `${(value * 100).toFixed(0)}%`; row.querySelector('.band-fill').style.width = value === undefined || value === null ? '0%' : `${value * 100}%`; }
  $('band-status').textContent = !meta.capabilities.bandPower ? '此数据源未提供频段功率' : bandsFresh ? (mode === 'demo' ? '模拟生成 · 窗口 1 秒 · ratio' : `后端 Welch 相对功率 · 窗口 ${meta.bandPowerInfo.windowSeconds}s · ratio`) : '等待有效频段数据';
  $('trend-empty').hidden = store.trend.length > 0;
  $('trend-window').textContent = range === 'session' ? '本次会话 · 本地事件时间' : `最近 ${range === '60' ? '60 秒' : '5 分钟'} · 本地时间`;
  if (route === 'device') $('events').replaceChildren(...store.events.values().slice(-20).reverse().map((e) => { const li = element('li'); li.append(element('time', '', clock(e.timestampMs)), element('span', '', `${e.kind} · ${e.detail}`)); return li; }));
  const minutes = Number($('duration').value);
  $('remaining').textContent = session ? minutes ? `剩余 ${elapsed(Math.max(0, minutes * 60000 - store.elapsed()))}` : `已记录 ${elapsed(store.elapsed())}` : '在上方开始会话，计时随操作推进';
  if (session && minutes > 0 && store.elapsed() >= minutes * 60000) { stop(); notice('已达到设置的训练时长，会话已结束。统计摘要可在“会话记录”查看。'); }
  // Gaps become part of the session audit trail once per state transition.
  if (state !== lastUIState) { if (['stale', 'poor', 'disconnected', 'calibrating'].includes(state)) store.log('gap', stateLabel); lastUIState = state; }
  if (!store.session && adapter.timer !== null) adapter.stop(); // recording safety limit
  if (route === 'dashboard') drawTimeline();
}

function frame(t) {
  if (disposed) return;
  metrics.frames++;
  const hidden = document.hidden;
  if (t - lastUIDraw >= (hidden ? 1000 : 250)) { updateUI(); lastUIDraw = t; }
  const now = Date.now(), fresh = store.eegReceivedAt !== null && now - store.eegReceivedAt <= store.staleMs;
  const drawInterval = reducedMotion ? 200 : 1000 / 60;
  if (!hidden && route === 'dashboard' && !frozen && t - lastEEGDraw >= drawInterval) {
    const begin = performance.now();
    const end = mode === 'studio' ? store.lastEegEnd : ['paused', 'stale', 'idle', 'connecting', 'waiting', 'error', 'disconnected', 'calibrating'].includes(store.state()) ? store.lastEegEnd : now;
    drawEEG($('eeg'), store, { channel, windowSeconds, end: end > 0 ? end : now });
    lastEEGDraw = t - (t - lastEEGDraw) % drawInterval; metrics.draws++; metrics.drawMs.push(performance.now() - begin); if (metrics.drawMs.length > 2000) metrics.drawMs.shift();
    if (fresh && lastPaintedArrival !== store.eegReceivedAt) { lastPaintedArrival = store.eegReceivedAt; metrics.arrivalToPaintMs.push(now - store.eegReceivedAt); if (metrics.arrivalToPaintMs.length > 2000) metrics.arrivalToPaintMs.shift(); }
  }
  raf = requestAnimationFrame(frame);
}
export function dispose() { if (disposed) return; disposed = true; adapter.stop(); if (raf !== null) cancelAnimationFrame(raf); cleanups.forEach((fn) => fn()); }
on(window, 'hashchange', routeChanged);
on(window, 'pagehide', () => { adapter.stop(); if (raf !== null) cancelAnimationFrame(raf); });
on(window, 'pageshow', (event) => { if (event.persisted && !disposed) { if (store.session?.status === 'recording') adapter.start(); raf = requestAnimationFrame(frame); } });
on(window, 'error', () => { notice('界面发生错误，请结束会话并刷新；已记录数据可尝试从会话记录导出。', true); });
routeChanged(); raf = requestAnimationFrame(frame);
if (new URLSearchParams(location.search).has('session')) { $('source').value='studio'; $('source').dispatchEvent(new Event('change')); }
