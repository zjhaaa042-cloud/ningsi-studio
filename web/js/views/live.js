/**
 * 实时监测：指标条 + 阈值线、多通道高频波形 + 频谱 + 频带功率、热力图、预警时间轴。
 *
 * 数据来源：
 * - **高频信号流** `GET /api/sessions/{uuid}/signal`（SSE，默认 10 FPS）：多通道波形与频谱，
 *   与 4 秒分析窗解耦，刷新手感对齐厂家采集软件；可切换 5 / 10 / 20 FPS。
 * - SSE `window`（逐窗指标/预警/质检原因）、`monitor`、`feedback`、`quality`、`finished`
 * - 轮询兜底：GET /api/sessions/{uuid}/live、GET /api/sessions/{uuid}/heatmap（10 秒）
 * - 阈值：GET /api/config 的 assess / alerts；颜色：config.heatmap_bands、heatmap.legend、missing_color
 */

import { api } from '../api.js';
import { describeError, toast } from '../store.js';
import { connectSessionEvents, connectSignalStream } from '../sse.js';
import {
  DASH, button, card, el, empty, fmtInt, fmtNum, fmtPercent, fmtSeconds, fmtTime,
  list, pageFrame, pick, statusText, table,
} from '../util.js';
import {
  drawAlertTimeline, drawBandBars, drawHeatmap, drawMultiChannelEeg, drawSpectrum,
  legend, metricBar,
} from '../charts.js';

const INDICATORS = [
  { key: 'focus', label: '专注度' },
  { key: 'relax', label: '放松度' },
  { key: 'load', label: '认知负荷' },
];

/** 信号流刷新率选项（服务端上限 25 FPS）。 */
const RATE_OPTIONS = [
  { hz: 5, label: '5 FPS（省电）' },
  { hz: 10, label: '10 FPS（默认）' },
  { hz: 20, label: '20 FPS（顺滑）' },
];

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

  const config = ctx.store.state.config || {};
  const heatmapBands = list(pick(config, 'heatmap_bands', []));
  const alerts = pick(config, 'alerts', {}) || {};
  const assess = pick(config, 'assess', {}) || {};
  // 频谱面板的频带底色区间（来自接口，不在前端硬编码）
  const bandRanges = pick(config, 'bands', {}) || {};

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
    // 高频信号通道
    hz: Number(ctx.params.get('hz')) || 10,
    frames: 0,
    lastFrame: null,
    spectrum: null,
    bands: null,
    bandRanges,
    signalLive: false,
    signalError: null,
    signalFpsAt: 0,
    signalFpsFrames: 0,
    measuredFps: 0,
  };

  /* ---------------------------------------------------------- 页面分区 */
  const stateHost = el('div');
  const metricHost = el('div');
  const signalHost = el('div');
  const heatHost = el('div');
  const alertHost = el('div');
  const extraHost = el('div');
  host.append(stateHost, metricHost, signalHost, heatHost, alertHost, extraHost);

  const renderState = () => {
    stateHost.textContent = '';
    const session = ctx.store.state.currentSession || {};
    stateHost.append(card('会话状态', el('div', { class: 'stack' }, [
      el('div', { class: 'grid grid--3' }, [
        el('div', {}, [
          el('p', { class: 'muted', text: '状态 / 阶段' }),
          el('p', { text: `${statusText(pick(session, 'status', null))}｜${pick(session, 'phase_label', null) || pick(session, 'phase', null) || DASH}` }),
        ]),
        el('div', {}, [
          el('p', { class: 'muted', text: '数据源' }),
          el('p', { text: `${pick(session, 'device', DASH)}（${pick(ctx.store.state.sessionRuntime, 'source_kind', DASH) || DASH}）` }),
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
    metricHost.textContent = '';
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
      el('p', { class: 'muted', text: `窗口 ${fmtNum(pick(config, 'window_sec'), 1)} s / 步长 ${fmtNum(pick(config, 'step_sec'), 1)} s` }),
    ]));
    metricHost.append(card('指标与阈值', body, {
      sub: '阈值线为接口给出的判定门槛；每条进度条上的虚线即阈值位置',
    }));
  };

  /**
   * 高频原始信号面板：多通道波形 + 实时频谱 + 频带功率条。
   * 与"逐窗指标"分开：这里只用于观察信号本身，刷新率由信号流决定（默认 10 FPS）。
   */
  const renderSignal = () => {
    signalHost.textContent = '';
    const frame = local.lastFrame;

    // 刷新率切换（改 URL hash，视图重渲染后重连到新的 hz）
    const rateSelect = el('select', { id: 'signal-rate' });
    for (const option of RATE_OPTIONS) {
      rateSelect.append(el('option', { value: String(option.hz), text: option.label }));
    }
    rateSelect.value = String(local.hz);
    rateSelect.addEventListener('change', () => {
      const next = Number(rateSelect.value) || 10;
      local.hz = next;
      ctx.navigate(`#/live?session=${uuid}&hz=${next}`);
    });

    const statusBits = [];
    if (frame) {
      statusBits.push(`采样率 ${fmtNum(frame.srate, 0)} Hz`);
      statusBits.push(`通道 ${fmtInt(frame.channel_count)}`);
      statusBits.push(`${fmtInt(frame.samples)} 样本 / ${fmtNum(frame.window_sec, 0)} s`);
      if (local.measuredFps) statusBits.push(`实测 ${fmtNum(local.measuredFps, 1)} FPS`);
    }
    const statusRows = [
      el('div', { class: 'row', style: 'gap:12px;align-items:center' }, [
        el('span', { class: 'muted', text: '显示刷新率' }),
        rateSelect,
        el('span', {
          class: 'badge' + (local.signalLive ? ' badge--strong' : ''),
          text: local.signalLive ? '信号在收数' : (local.signalError ? `信号流异常：${local.signalError}` : '等待信号'),
        }),
        el('span', { class: 'muted', text: statusBits.join('｜') }),
      ]),
    ];

    if (!frame || !list(frame.channels).length) {
      signalHost.append(card('实时脑电（高频信号流）', [
        ...statusRows,
        empty(local.signalError
          || '会话运行中就会从采集端按显示刷新率推来原始样本；若长时间为空，请查看顶栏“设备”徽标是否已停/掉线。'),
      ], { sub: '该面板与 4 秒分析窗解耦：波形按显示刷新率更新，指标仍按窗计算' }));
      return;
    }

    const waveBody = el('div');
    const spectrumBody = el('div');
    const bandBody = el('div');
    signalHost.append(card('实时脑电波形', [el('div', {}, statusRows), waveBody], {
      sub: '每条通道独立量程与中线；底部为时间轴。抽稀采用 min/max 保峰值，尖峰会保留',
    }));
    signalHost.append(el('div', { class: 'grid grid--2' }, [
      card('实时频谱（Welch，与报告同口径）', spectrumBody, {
        sub: `最近一个可用窗的功率谱（dB）；竖直底色为频带区间定义`,
      }),
      card('频带相对功率', bandBody, {
        sub: '当前可用窗的各频带占比（theta / alpha / beta / gamma）',
      }),
    ]));

    drawMultiChannelEeg(waveBody, frame, {
      windowSec: Number(frame.window_sec), srate: Number(frame.srate),
    });
    drawSpectrum(spectrumBody, local.spectrum || {}, { bandRanges: local.bandRanges });
    drawBandBars(bandBody, local.bands || {});
  };

  const renderHeatmap = () => {
    heatHost.textContent = '';
    const body = el('div');
    heatHost.append(card('状态热力图', body, {
      sub: '颜色与分档全部来自接口 heatmap_bands / legend；缺失窗（index = -1）单独用缺失色块表示，不计入状态变化',
    }));
    drawHeatmap(body, local.cells, { legend: local.legend, missingColor: local.missingColor });
    if (local.legend.length) body.append(legend(local.legend, local.missingColor));
  };

  const renderAlerts = () => {
    alertHost.textContent = '';
    const items = list(ctx.store.state.alerts);
    const body = el('div');
    alertHost.append(card(`预警时间轴（${fmtInt(items.length)} 条）`, body, {
      sub: '预警口径来自 config.alerts：阈值 + 连续越界时长 + 解除条件',
    }));
    drawAlertTimeline(body, items);
  };

  const renderExtra = () => {
    extraHost.textContent = '';
    if (!local.lastFeedback) return;
    extraHost.append(card('神经反馈', el('div', { class: 'grid grid--3' }, [
      el('div', {}, [el('p', { class: 'muted', text: '当前段' }), el('p', { class: 'mono', text: fmtInt(local.lastFeedback.segment) })]),
      el('div', {}, [el('p', { class: 'muted', text: '目标线' }), el('p', { class: 'mono', text: fmtNum(local.lastFeedback.target, 3) })]),
      el('div', {}, [el('p', { class: 'muted', text: '当前专注度' }), el('p', { class: 'mono', text: fmtNum(local.lastFeedback.score, 3) })]),
      el('div', {}, [el('p', { class: 'muted', text: '是否达标' }), el('p', { text: local.lastFeedback.on_target ? '达标' : '未达标' })]),
    ])));
  };

  renderState();
  renderMetrics();
  renderSignal();
  renderHeatmap();
  renderAlerts();

  /* -------------------------------------------------------- 数据加载 */
  const loadHeatmap = async () => {
    try {
      const response = await api.sessionHeatmap(uuid);
      if (ctx.signal.aborted) return;
      const data = response.data || {};
      local.cells = list(data.cells);
      local.legend = list(data.legend);
      local.missingColor = data.missing_color || local.missingColor;
      renderHeatmap();
    } catch (error) {
      if (!ctx.signal.aborted) console.warn('[live] 热力图加载失败', error);
    }
  };

  const loadLive = async () => {
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
    } catch (error) {
      if (!ctx.signal.aborted) toast(describeError(error));
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
          : bandOf(typeof scores.focus === 'number' ? scores.focus : null, heatmapBands);
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
        const body = el('div', {}, [
          el('p', { text: `质检${payload.passed ? '通过' : '未达门槛'}：${fmtInt(payload.usable)}/${fmtInt(payload.windows)} 窗可用（${fmtPercent(payload.valid_ratio)}）` }),
        ]);
        extraHost.append(card('质检结果', body));
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
        extraHost.append(card('采集提示', [el('p', { text: payload.message || '' })]));
        break;
      }
      case 'finished': {
        const session = { ...(ctx.store.state.currentSession || {}) };
        session.status = payload.status;
        ctx.store.setState({ currentSession: session });
        renderState();
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

  let signalStream = connectSignalStream(uuid, {
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
        local.signalError = local.signalError || '会话未运行或信号流不可用';
      }
      renderSignal();
    },
  });

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

  const liveTimer = window.setInterval(() => {
    if (document.hidden) return;
    loadLive();
  }, 10000);
  const heatTimer = window.setInterval(() => {
    if (document.hidden) return;
    loadHeatmap();
  }, 10000);

  ctx.onCleanup(() => {
    window.clearInterval(liveTimer);
    window.clearInterval(heatTimer);
    events.close();
    signalStream.close();          // 断开会触发服务端 on_close，停掉推帧线程
  });
}

export default render;
