/**
 * 实时监测：指标条 + 阈值线、多通道高频波形 + 频谱 + 频带功率、热力图、预警时间轴。
 *
 * 数据来源：
 * - **高频信号流** `GET /api/sessions/{uuid}/signal`（SSE，默认 10 FPS）：多通道波形与频谱，
 *   与 4 秒分析窗解耦，刷新手感对齐厂家采集软件；可切换 5 / 10 / 20 / 25 FPS（25 为服务端上限）。
 * - SSE `window`（逐窗指标/预警/质检原因）、`monitor`、`feedback`、`quality`、`finished`
 * - 轮询兜底：GET /api/sessions/{uuid}/live、GET /api/sessions/{uuid}/heatmap（10 秒）
 * - 阈值：GET /api/config 的 assess / alerts；颜色：config.heatmap_bands、heatmap.legend、missing_color
 */

import { api } from '../api.js';
import { describeError, toast } from '../store.js';
import { connectSessionEvents, connectSignalStream } from '../sse.js';
import {
  DASH, button, card, el, empty, fmtInt, fmtNum, fmtPercent, fmtSeconds, fmtTime,
  list, pageFrame, phaseText, pick, statusText, table,
} from '../util.js';
import {
  drawAlertTimeline, drawBandBars, drawHeatmap, drawMultiChannelEeg, drawSpectrum,
  metricBar,
} from '../charts.js';

const INDICATORS = [
  { key: 'focus', label: '专注度' },
  { key: 'relax', label: '放松度' },
  { key: 'load', label: '认知负荷' },
];

/** 信号流刷新率选项（服务端上限 25 FPS，见 routes.py SIGNAL_MAX_HZ）。 */
const RATE_OPTIONS = [
  { hz: 5, label: '5 FPS（省电）' },
  { hz: 10, label: '10 FPS（默认）' },
  { hz: 20, label: '20 FPS（顺滑）' },
  { hz: 25, label: '25 FPS（上限）' },
];

/** 服务端对 hz 的上限（routes.py SIGNAL_MAX_HZ）；界面取值不能超过它。 */
const SIGNAL_MAX_HZ = 25;

/** 会话终态（与后端 routes.py:963 的判据一致）：终态后 /signal 停止推帧、/live 的 runtime_alive 为 false。 */
const TERMINAL_STATUSES = new Set(['done', 'failed', 'error', 'cancelled', 'canceled']);

/** 缺失窗兜底色：heatmap 未返回时用它，与引擎 MISSING_COLOR 一致。 */
const MISSING_FALLBACK = '#6b6b6b';

/** 按 config.heatmap_bands 把分值落到区间（index === -1 表示低质量缺失）。 */
function bandOf(score, bands) {
  if (typeof score !== 'number' || !Number.isFinite(score)) {
    return { index: -1, label: '低质量缺失', color: MISSING_FALLBACK, score: null };
  }
  const sorted = list(bands).slice().sort((left, right) => Number(left.low) - Number(right.low));
  for (let index = 0; index < sorted.length; index += 1) {
    const band = sorted[index];
    const low = Number(band.low);
    const high = Number(band.high);
    if (score < high || index === sorted.length - 1) {
      return {
        index,
        label: band.label,
        color: band.color,
        score,
        range: `${Number.isFinite(low) ? low.toFixed(2) : ''}–${Number.isFinite(high) ? high.toFixed(2) : ''}`,
      };
    }
  }
  return { index: -1, label: '低质量缺失', color: MISSING_FALLBACK, score: null };
}

export function render(container, ctx) {
  const uuid = ctx.params.get('session') || pick(ctx.store.state.currentSession, 'uuid', null);
  const actions = uuid ? [
    button('流程视图', () => ctx.navigate(`#/flow?session=${uuid}`)),
    button('训练视图', () => ctx.navigate(`#/training?session=${uuid}`)),
    button('报告', () => ctx.navigate(`#/report?session=${uuid}`)),
  ] : [];
  const host = pageFrame(container, '实时监测', actions);

  if (!uuid) {
    host.append(card('尚未选择会话', el('div', {}, [
      empty('请先从历史会话或被试管理中选择一个会话。'),
      el('div', { class: 'row', style: 'margin-top:12px' }, [
        button('前往历史会话', () => ctx.navigate('#/history'), { primary: true }),
        button('前往被试管理', () => ctx.navigate('#/subjects')),
      ]),
    ])));
    return;
  }

  // 配置一律"惰性读取"：直接进本页时 /api/config 可能还没回来（boot 是异步加载），
  // 一次性取值会把阈值 / 频带区间固化成 "—"（实测）
  const configNow = () => ctx.store.state.config || {};
  const heatmapBands = () => list(pick(configNow(), 'heatmap_bands', []));
  const alertsConfig = () => pick(configNow(), 'alerts', {}) || {};
  const assessConfig = () => pick(configNow(), 'assess', {}) || {};
  const bandRanges = () => pick(configNow(), 'bands', {}) || {};

  const local = {
    uuid,
    scores: {},
    signal: null,
    cells: [],
    legend: [],
    missingColor: MISSING_FALLBACK,
    windowIndex: 0,
    offline: false,
    auto: false,
    lastFeedback: null,
    notices: [],
    missing: false,                 // 404：会话不存在/已删除，静默并停止轮询
    // 高频信号通道
    hz: Math.min(SIGNAL_MAX_HZ, Math.max(1, Number(ctx.params.get('hz')) || 10)),
    frames: 0,
    lastFrame: null,
    spectrum: null,
    bands: null,
    signalLive: false,
    signalError: null,
    signalFpsAt: 0,
    signalFpsFrames: 0,
    measuredFps: 0,
    ended: false,                   // 会话终态：服务端停止推帧/心跳，界面必须主动降级，不能"假在线"
  };

  /* ---------------------------------------------------------- 页面分区 */
  const stateHost = el('div');
  const metricHost = el('div');
  const signalHost = el('div');
  const heatHost = el('div');
  const alertHost = el('div');
  const extraHost = el('div');
  const qualityHost = el('div');
  const noticeHost = el('div');
  host.append(stateHost, metricHost, signalHost, heatHost, alertHost, extraHost, qualityHost, noticeHost);

  // SSE / 轮询句柄集中放这里：404 静默分支要能把它们全部停掉
  const connections = { events: null, signal: null };
  let liveTimer = null;
  let heatTimer = null;
  const stopPolling = () => {
    if (liveTimer !== null) window.clearInterval(liveTimer);
    if (heatTimer !== null) window.clearInterval(heatTimer);
    liveTimer = null;
    heatTimer = null;
  };

  /**
   * 会话不存在（HTTP 404）：这是"业务正常分支"而不是故障——
   * 静默展示明确空态、停止 10 秒轮询与两条 SSE，
   * 否则每轮询一次就弹一个错误 toast（实测：打开已结束/不存在的会话会反复弹）。
   */
  const markMissing = (error) => {
    if (local.missing) return;
    local.missing = true;
    stopPolling();
    connections.events?.close();
    connections.signal?.close();
    for (const node of [stateHost, metricHost, signalHost, heatHost, alertHost, extraHost, qualityHost, noticeHost]) node.textContent = '';
    stateHost.append(card('会话不存在', el('div', {}, [
      empty(`找不到会话 ${uuid}（HTTP 404${error && error.code ? ` / ${error.code}` : ''}）：可能已被删除，或链接里的 uuid 有误。`),
      el('div', { class: 'row', style: 'margin-top:12px' }, [
        button('前往历史会话', () => ctx.navigate('#/history'), { primary: true }),
        button('前往被试管理', () => ctx.navigate('#/subjects')),
      ]),
    ]), { sub: '已停止自动轮询与信号流：404 不弹错误提示' }));
  };

  /**
   * 会话到达终态（done / failed / error / cancelled）后的降级。
   * 后端 /signal 的 keep_alive 含 `runtime.alive`：会话结束后服务端**直接停止推帧与心跳**，
   * 浏览器 EventSource 不会触发 onerror（静默），所以不能靠"收不到帧"判断，
   * 必须以 `/live` 的 `runtime_alive === false` 或 SSE `finished` 事件为依据，
   * 否则实时监测页会一直显示"信号在收数"（假在线）。
   */
  const markEnded = () => {
    if (local.missing || local.ended) return;
    local.ended = true;
    local.signalLive = false;
    stopPolling();                      // 终态后 /live 与 /heatmap 都不再变化，停止轮询
    connections.signal?.close();        // 服务端已停止推帧，前端不再保持这条流
    renderState();
    renderSignal({ force: true });
  };

  const renderState = () => {
    if (local.missing) return;
    stateHost.textContent = '';
    const session = ctx.store.state.currentSession || {};
    stateHost.append(card('会话状态', el('div', { class: 'stack' }, [
      el('div', { class: 'grid grid--3' }, [
        el('div', {}, [
          el('p', { class: 'muted', text: '状态 / 阶段' }),
          el('p', { text: `${statusText(pick(session, 'status', null))}｜${phaseText(pick(session, 'phase', null), pick(session, 'phase_label', null))}` }),
        ]),
        el('div', {}, [
          el('p', { class: 'muted', text: '数据源' }),
          el('p', { text: `${pick(session, 'device', DASH)}（${pick(ctx.store.state.sessionRuntime, 'source_kind', null) || pick(session, 'source', null) || DASH}）` }),
        ]),
        el('div', {}, [
          el('p', { class: 'muted', text: '已接收窗' }),
          el('p', { class: 'mono', text: fmtInt(local.windowIndex) }),
        ]),
      ]),
      el('div', {}, [
        el('div', { class: 'row row--between' }, [
          el('span', { class: 'muted', text: '总进度' }),
          el('span', { class: 'mono', text: fmtPercent(pick(session, 'progress', null)) }),
        ]),
        el('div', { class: 'progress' }, [
          el('div', {
            class: 'progress__bar',
            style: `width:${(Math.max(0, Math.min(1, Number(pick(session, 'progress', 0)) || 0)) * 100).toFixed(1)}%`,
          }),
        ]),
      ]),
      local.offline
        ? el('p', { class: 'muted', text: '事件流已断开：已切换到 10 秒轮询 /api/sessions/{uuid}/live 与 /heatmap 兜底，恢复后自动切回。' })
        : null,
      local.auto
        ? el('p', { class: 'muted', text: '快速演示模式（time_scale < 0.2）：交互阶段由服务端自动作答。' })
        : null,
      table([
        { title: '字段', render: (row) => row[0] },
        { title: '值', render: (row) => row[1] },
      ], [
        ['会话 uuid', uuid],
        ['时间倍率', fmtNum(pick(session, 'time_scale'), 2)],
        ['采样率 / 通道', `${fmtNum(pick(session, 'srate'), 0)} Hz / ${fmtInt(pick(session, 'channels'))}`],
        ['开始时间', fmtTime(pick(session, 'started_at'))],
        ['引擎口径', Object.entries(pick(session, 'engine_versions', {}) || {}).map(([key, value]) => `${key}=${value}`).join('、') || DASH],
      ]),
    ])));
  };

  const renderMetrics = () => {
    if (local.missing) return;
    metricHost.textContent = '';
    const alerts = alertsConfig();
    const assess = assessConfig();
    const summaryStat = (name) => {
      const value = local.scores[name];
      return typeof value === 'number' ? value : null;
    };
    const body = el('div', { class: 'stack' });
    body.append(metricBar('专注度', summaryStat('focus'), {
      threshold: pick(alerts, 'low_focus.threshold'),
      thresholdLabel: `低专注阈值 ${fmtNum(pick(alerts, 'low_focus.threshold'), 2)}（持续 ${fmtSeconds(pick(alerts, 'low_focus.sustain_sec'), 0)}）`,
      fillClass: 'fill-0',
    }));
    body.append(metricBar('放松度', summaryStat('relax'), {
      threshold: pick(assess, 'relax_low'),
      thresholdLabel: `放松度参考下限 ${fmtNum(pick(assess, 'relax_low'), 2)}`,
      fillClass: 'fill-1',
    }));
    body.append(metricBar('认知负荷', summaryStat('load'), {
      threshold: pick(alerts, 'high_load.threshold'),
      thresholdLabel: `高负荷阈值 ${fmtNum(pick(alerts, 'high_load.threshold'), 2)}（持续 ${fmtSeconds(pick(alerts, 'high_load.sustain_sec'), 0)}）`,
      fillClass: 'fill-2',
    }));
    body.append(el('div', { class: 'grid grid--3' }, [
      el('p', { class: 'muted', text: `预警阈值：专注 < ${fmtNum(pick(alerts, 'low_focus.threshold'), 2)}；负荷 > ${fmtNum(pick(alerts, 'high_load.threshold'), 2)}；信号可用窗比例 < ${fmtNum(pick(alerts, 'poor_signal.valid_ratio_min'), 2)}` }),
      el('p', { class: 'muted', text: `评估门槛：专注低位 ${fmtNum(pick(assess, 'focus_low'), 2)} / 高位 ${fmtNum(pick(assess, 'focus_high'), 2)}；负荷高位 ${fmtNum(pick(assess, 'load_high'), 2)}` }),
      el('p', { class: 'muted', text: `窗口 ${fmtNum(pick(configNow(), 'window_sec'), 1)} s / 步长 ${fmtNum(pick(configNow(), 'step_sec'), 1)} s` }),
    ]));
    metricHost.append(card('指标与阈值', body, {
      sub: '阈值线为接口给出的判定门槛；每条进度条上的虚线即阈值位置',
    }));
  };

  /**
   * 高频原始信号面板：多通道波形 + 实时频谱 + 频带功率条。
   *
   * 性能约定（原实现每帧 `signalHost.textContent=''` 后重建 select + 3 个 canvas，
   * 在 10–25 FPS 下会造成 DOM 抖动与位图垃圾）：
   * - **结构只建一次**：刷新率选择器、状态徽标、三块画布宿主都复用同一批节点；
   * - 每帧只更新文本 + 重绘波形画布（prepareCanvas 会复用已有 canvas 节点）；
   * - 频谱 / 频带按后端 1 秒缓存频率刷新（后端频谱本就按秒更新，逐帧重绘纯属浪费）。
   */
  const signalPanel = {
    built: false,
    rateSelect: null,
    badge: null,
    bits: null,
    note: null,
    waveBody: null,
    spectrumBody: null,
    bandBody: null,
    lastSpectrumAt: 0,
    spectrumDrawn: false,
  };

  const renderSignal = (options = {}) => {
    if (local.missing) return;
    const frame = local.lastFrame;
    const hasFrame = Boolean(frame && list(frame.channels).length);

    if (!signalPanel.built) {
      // 刷新率切换（改 URL hash，视图重渲染后重连到新的 hz）
      signalPanel.rateSelect = el('select', { id: 'signal-rate' });
      for (const option of RATE_OPTIONS) {
        signalPanel.rateSelect.append(el('option', { value: String(option.hz), text: option.label }));
      }
      signalPanel.rateSelect.addEventListener('change', () => {
        const next = Math.min(SIGNAL_MAX_HZ, Math.max(1, Number(signalPanel.rateSelect.value) || 10));
        local.hz = next;
        ctx.navigate(`#/live?session=${uuid}&hz=${next}`);
      });
      signalPanel.badge = el('span', { class: 'badge' });
      signalPanel.bits = el('span', { class: 'muted' });
      signalPanel.note = el('p', { class: 'empty' });
      signalPanel.waveBody = el('div');
      signalPanel.spectrumBody = el('div');
      signalPanel.bandBody = el('div');
      const statusRow = el('div', { class: 'row', style: 'gap:12px;align-items:center' }, [
        el('span', { class: 'muted', text: '显示刷新率' }),
        signalPanel.rateSelect,
        signalPanel.badge,
        signalPanel.bits,
      ]);
      signalHost.append(card('实时脑电波形', [statusRow, signalPanel.note, signalPanel.waveBody], {
        sub: '每条通道独立量程与中线；底部为时间轴。抽稀采用 min/max 保峰值，尖峰会保留',
      }));
      signalHost.append(el('div', { class: 'grid grid--2' }, [
        card('实时频谱（Welch，与报告同口径）', signalPanel.spectrumBody, {
          sub: '最近一个可用窗的功率谱（dB）；竖直底色为频带区间定义',
        }),
        card('频带相对功率', signalPanel.bandBody, {
          sub: '当前可用窗的各频带占比（theta / alpha / beta / gamma）',
        }),
      ]));
      signalPanel.built = true;
    }

    // 下面全是"只改数据、不动结构"
    signalPanel.rateSelect.value = String(local.hz);
    signalPanel.badge.textContent = local.ended
      ? '会话已结束'
      : (local.signalLive
        ? '信号在收数'
        : (local.signalError ? `信号流异常：${local.signalError}` : '等待信号'));
    signalPanel.badge.classList.toggle('badge--strong', local.signalLive);
    const statusBits = [];
    if (frame) {
      statusBits.push(`采样率 ${fmtNum(frame.srate, 0)} Hz`);
      statusBits.push(`通道 ${fmtInt(frame.channel_count)}`);
      statusBits.push(`${fmtInt(frame.samples)} 样本 / ${fmtNum(frame.window_sec, 0)} s`);
      if (local.measuredFps) statusBits.push(`实测 ${fmtNum(local.measuredFps, 1)} FPS`);
    }
    signalPanel.bits.textContent = statusBits.join('｜');
    signalPanel.note.hidden = hasFrame && !local.ended;
    if (local.ended) {
      // ended 分支必须保留"不可用"字样：验收脚本用 未运行/不可用/无信号 判定空态文案
      signalPanel.note.textContent = '会话已结束：实时信号流不再可用（服务端已停止推帧与心跳），波形不再更新。';
      // 频谱/频带只在收数期间按秒缓存：一次都没画过就补空态，避免留两个空白卡片
      if (!signalPanel.spectrumDrawn) {
        for (const [node, text] of [
          [signalPanel.spectrumBody, '会话已结束：没有可用的频谱样本（频谱只在收数期间按秒缓存）。'],
          [signalPanel.bandBody, '会话已结束：没有可用的频带样本。'],
        ]) {
          node.textContent = '';
          node.append(empty(text));
        }
      }
      return;
    }
    if (!hasFrame) {
      signalPanel.note.textContent = local.signalError
        || '会话运行中就会从采集端按显示刷新率推来原始样本；若长时间为空，请查看顶栏“设备”徽标是否已停/掉线。';
      return;
    }

    drawMultiChannelEeg(signalPanel.waveBody, frame, {
      windowSec: Number(frame.window_sec), srate: Number(frame.srate),
    });
    // 频谱 / 频带按 1 秒更新（后端 spectrum/bans 也是按秒缓存），避免逐帧重绘
    const now = Date.now();
    if (options.force || !signalPanel.spectrumDrawn || now - signalPanel.lastSpectrumAt >= 1000) {
      signalPanel.lastSpectrumAt = now;
      signalPanel.spectrumDrawn = true;
      drawSpectrum(signalPanel.spectrumBody, local.spectrum || {}, { bandRanges: bandRanges() });
      drawBandBars(signalPanel.bandBody, local.bands || {});
    }
  };

  const renderHeatmap = () => {
    if (local.missing) return;
    heatHost.textContent = '';
    const body = el('div');
    heatHost.append(card('状态热力图', body, {
      sub: '颜色与分档全部来自接口 heatmap_bands / legend；缺失窗（index = -1）单独用缺失色块表示，不计入状态变化',
    }));
    // 图例由 drawHeatmap 内部按 options.legend 渲染；这里不要再补一次（会重复一整套图例）
    drawHeatmap(body, local.cells, { legend: local.legend, missingColor: local.missingColor });
  };

  const renderAlerts = () => {
    if (local.missing) return;
    alertHost.textContent = '';
    const items = list(ctx.store.state.alerts);
    const body = el('div');
    alertHost.append(card(`预警时间轴（${fmtInt(items.length)} 条）`, body, {
      sub: '预警口径来自 config.alerts：阈值 + 连续越界时长 + 解除条件',
    }));
    drawAlertTimeline(body, items);
  };

  const renderExtra = () => {
    if (local.missing) return;
    extraHost.textContent = '';
    const feedback = local.lastFeedback;
    if (!feedback) return;
    // 三态：缺测（usable === false）不能被当成"未达标"——那是两种完全不同的结论
    const usable = feedback.usable !== false;
    const stateText = !usable
      ? '缺测（本窗不可用，不计入达标统计）'
      : (feedback.on_target ? '达标' : '未达标');
    extraHost.append(card('神经反馈', el('div', { class: 'grid grid--3' }, [
      el('div', {}, [el('p', { class: 'muted', text: '当前段' }), el('p', { class: 'mono', text: fmtInt(feedback.segment) })]),
      el('div', {}, [el('p', { class: 'muted', text: '目标线' }), el('p', { class: 'mono', text: fmtNum(feedback.target, 3) })]),
      el('div', {}, [el('p', { class: 'muted', text: '当前专注度' }), el('p', { class: 'mono', text: fmtNum(feedback.score, 3) })]),
      el('div', {}, [el('p', { class: 'muted', text: '本窗是否可用' }), el('p', { text: usable ? '可用' : '不可用（缺测）' })]),
      el('div', {}, [el('p', { class: 'muted', text: '是否达标' }), el('p', { text: stateText })]),
    ])));
  };

  renderState();
  renderMetrics();
  renderSignal();
  renderHeatmap();
  renderAlerts();

  /* -------------------------------------------------------- 数据加载 */
  const loadHeatmap = async () => {
    if (local.missing) return;
    try {
      const response = await api.sessionHeatmap(uuid);
      if (ctx.signal.aborted) return;
      const data = response.data || {};
      local.cells = list(data.cells);
      local.legend = list(data.legend);
      local.missingColor = data.missing_color || local.missingColor;
      renderHeatmap();
    } catch (error) {
      if (ctx.signal.aborted) return;
      if (isNotFound(error)) {
        markMissing(error);
        return;
      }
      console.warn('[live] 热力图加载失败', error);
    }
  };

  /** 404 判定：api.js 抛出的错误带 status（见 ApiError）。 */
  const isNotFound = (error) => Boolean(error) && (error.status === 404 || error.code === 'not_found');

  const loadLive = async () => {
    if (local.missing) return;
    try {
      const response = await api.sessionLive(uuid);
      if (ctx.signal.aborted) return;
      const data = response.data || {};
      const session = { ...(ctx.store.state.currentSession || {}) };
      session.uuid = uuid;
      session.status = data.status;
      session.phase = data.phase;
      session.progress = data.progress;
      ctx.store.setState({ currentSession: session, alerts: list(data.alerts) });
      const summary = data.indicator_summary || {};
      for (const item of INDICATORS) {
        const mean = pick(summary, `${item.key}.mean`);
        if (typeof mean === 'number' && Number.isFinite(mean)) local.scores[item.key] = mean;
      }
      renderState();
      renderMetrics();
      renderAlerts();
      // 终态降级：后端 /live 明确给出 runtime_alive（routes.py:599）；字段缺失时退回"状态是否终态"
      if (data.runtime_alive === false || TERMINAL_STATUSES.has(String(data.status))) markEnded();
    } catch (error) {
      if (ctx.signal.aborted) return;
      // 404 是"会话不存在"的正常分支：静默 + 停轮询，不再每 10 秒弹一次 toast
      if (isNotFound(error)) {
        markMissing(error);
        return;
      }
      toast(describeError(error));
    }
  };

  api.getSession(uuid).then((response) => {
    if (ctx.signal.aborted) return;
    const data = response.data || {};
    const session = data.session ? { ...data.session } : { ...data };
    session.uuid = uuid;
    ctx.store.setState({
      currentSession: session,
      sessionRuntime: pick(data, 'runtime', null),
      alerts: list(pick(data, 'alerts', [])),
    });
    const summary = pick(data, 'indicator_summary', {}) || {};
    for (const item of INDICATORS) {
      const mean = pick(summary, `${item.key}.mean`);
      if (typeof mean === 'number') local.scores[item.key] = mean;
    }
    renderState();
    renderMetrics();
    renderAlerts();
  }).catch((error) => {
    if (ctx.signal.aborted) return;
    if (isNotFound(error)) {
      markMissing(error);
      return;
    }
    toast(describeError(error));
  });

  loadHeatmap();
  loadLive();

  /* -------------------------------------------------------------- SSE */
  const handleEvent = (type, payload) => {
    switch (type) {
      case 'started': {
        local.auto = Boolean(payload.auto);
        const startedSession = { ...(ctx.store.state.currentSession || {}) };
        startedSession.uuid = uuid;
        startedSession.status = 'running';
        ctx.store.setState({
          currentSession: startedSession,
          sessionRuntime: { source_kind: payload.source_kind, source_note: payload.source_note },
        });
        renderState();
        break;
      }
      case 'window': {
        local.windowIndex = Number(payload.index ?? local.windowIndex);
        const scores = payload.scores || {};
        for (const item of INDICATORS) {
          const value = scores[item.key];
          local.scores[item.key] = typeof value === 'number' && Number.isFinite(value) ? value : null;
        }
        if (payload.signal && list(payload.signal.samples).length) {
          local.signal = { samples: payload.signal.samples, srate: payload.signal.srate, channels: payload.signal.channels, tEnd: payload.t_end };
        }
        // 增量热力图：与后端同口径（不可用窗 → 缺失档）
        const band = payload.usable === false
          ? { index: -1, label: '低质量缺失', color: local.missingColor, score: null }
          : bandOf(typeof scores.focus === 'number' ? scores.focus : null, heatmapBands());
        if (band.index === -1) band.color = local.missingColor;
        local.cells.push({ ...band, t: payload.t_end });
        if (local.cells.length > 600) local.cells = local.cells.slice(-600);
        if (list(payload.alerts).length) {
          ctx.store.setState({ alerts: list(ctx.store.state.alerts).concat(list(payload.alerts)) });
        }
        const session = { ...(ctx.store.state.currentSession || {}) };
        if (typeof payload.overall_progress === 'number') session.progress = payload.overall_progress;
        ctx.store.setState({ currentSession: session, lastEvent: { type, data: payload } });
        renderState();
        renderMetrics();
        renderSignal();
        renderHeatmap();
        if (list(payload.alerts).length) renderAlerts();
        break;
      }
      case 'monitor': {
        const summary = payload.summary || {};
        const indicators = summary.indicators || summary;
        for (const item of INDICATORS) {
          const mean = pick(indicators, `${item.key}.mean`);
          if (typeof mean === 'number') local.scores[item.key] = mean;
        }
        renderMetrics();
        break;
      }
      case 'feedback': {
        local.lastFeedback = payload;
        renderExtra();
        break;
      }
      case 'quality': {
        // 幂等：每个分析窗都会来一次 quality，不能每次都往 extraHost 追加卡片
        const body = el('div', {}, [
          el('p', { text: `质检${payload.passed ? '通过' : '未达门槛'}：${fmtInt(payload.usable)}/${fmtInt(payload.windows)} 窗可用（${fmtPercent(payload.valid_ratio)}）` }),
          el('p', { class: 'muted', text: `窗口 ${fmtInt(payload.window ?? payload.index)}｜门槛 valid_ratio ≥ ${fmtNum(pick(alertsConfig(), 'poor_signal.valid_ratio_min', pick(configNow(), 'quality.valid_ratio_min')), 2)}` }),
        ]);
        qualityHost.textContent = '';
        qualityHost.append(card('质检结果', body));
        break;
      }
      case 'phase': {
        const session = { ...(ctx.store.state.currentSession || {}) };
        session.phase = payload.key;
        session.phase_label = payload.label;
        if (typeof payload.progress === 'number') session.progress = payload.progress;
        ctx.store.setState({ currentSession: session });
        renderState();
        break;
      }
      case 'progress': {
        const session = { ...(ctx.store.state.currentSession || {}) };
        if (typeof payload.progress === 'number') session.progress = payload.progress;
        ctx.store.setState({ currentSession: session });
        renderState();
        break;
      }
      case 'notice': {
        // 幂等：同一条提示重复推送时只保留一条（最多保留最近 5 条不同提示）
        const message = payload.message || '';
        if (!message || local.notices.includes(message)) break;
        local.notices.push(message);
        if (local.notices.length > 5) local.notices.shift();
        const body = el('div', {}, local.notices.map((item) => el('p', { text: item })));
        noticeHost.textContent = '';
        noticeHost.append(card('采集提示', body));
        break;
      }
      case 'finished': {
        const session = { ...(ctx.store.state.currentSession || {}) };
        session.status = payload.status;
        ctx.store.setState({ currentSession: session });
        // 服务端在终态停止推帧与心跳：这里主动降级（停轮询 / 关信号流 / 徽标改为已结束）
        markEnded();
        loadHeatmap();
        break;
      }
      default:
        break;
    }
  };

  /* --------------------------------------------------- 高频信号通道（SSE） */
  const handleFrame = (frame) => {
    if (ctx.signal.aborted) return;
    local.frames += 1;
    local.signalFpsFrames += 1;
    local.lastFrame = frame;
    if (frame.spectrum && list(frame.spectrum.freqs).length) local.spectrum = frame.spectrum;
    if (frame.bands && Object.keys(frame.bands).length) local.bands = frame.bands;
    local.signalLive = frame.realtime !== false;
    local.signalError = frame.note || null;
    // 实测刷新率：每秒结算一次，便于确认"高频"是否真的到了
    const now = Date.now();
    if (!local.signalFpsAt) {
      local.signalFpsAt = now;
    } else if (now - local.signalFpsAt >= 1000) {
      local.measuredFps = (local.signalFpsFrames * 1000) / (now - local.signalFpsAt);
      local.signalFpsAt = now;
      local.signalFpsFrames = 0;
    }
    renderSignal();
  };

  const signalStream = connectSignalStream(uuid, {
    hz: local.hz,
    seconds: 10,
    points: 1200,
    onFrame: handleFrame,
    onOpen: () => {
      local.signalLive = true;
      local.signalError = null;
    },
    onError: (_error, info) => {
      // 会话不在运行中（服务端 409）时不必反复重连，直接给出提示
      if (!info.opened) {
        local.signalLive = false;
        local.signalError = local.signalError || '会话未运行或信号流不可用（HTTP 409，已停止重连）';
      }
      renderSignal();
    },
  });
  connections.signal = signalStream;

  renderSignal();

  const events = connectSessionEvents(uuid, {
    onEvent: (type, frame) => {
      if (ctx.signal.aborted) return;
      handleEvent(type, frame && frame.data);
    },
    onOpen: () => {
      if (local.offline) {
        local.offline = false;
        renderState();
      }
    },
    onError: () => {
      if (!local.offline) {
        local.offline = true;
        renderState();
      }
    },
  });
  connections.events = events;

  liveTimer = window.setInterval(() => {
    if (document.hidden || local.missing) return;
    loadLive();
  }, 10000);
  heatTimer = window.setInterval(() => {
    if (document.hidden || local.missing) return;
    loadHeatmap();
  }, 10000);

  // config 到达（boot 异步）后重渲染一次：阈值 / 频带 / 图例不该停留在 "—"
  const unsubscribeConfig = ctx.store.subscribe(['config'], () => {
    if (local.missing) return;
    renderMetrics();
    renderState();
    renderSignal({ force: true });
    renderHeatmap();
  });
  ctx.onCleanup(unsubscribeConfig);

  ctx.onCleanup(() => {
    stopPolling();
    events.close();
    signalStream.close();          // 断开会触发服务端 on_close，停掉推帧线程
  });
}

export default render;
