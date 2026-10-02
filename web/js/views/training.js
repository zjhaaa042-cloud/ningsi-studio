/**
 * 训练视图：大号专注度仪表 + 目标线、达标状态、分段表格、训练前后基线对比。
 *
 * 数据来源：
 * - SSE：`training_start`（模式/段数/初始目标）、`feedback`（逐窗目标与达标）、`segment`（分段统计）
 * - GET /api/sessions/{uuid}/training → segments[] 与 summary（含 baseline_before / baseline_after）
 */

import { api } from '../api.js';
import { describeError, toast } from '../store.js';
import { connectSessionEvents } from '../sse.js';
import {
  DASH, button, card, el, empty, fmtDuration, fmtInt, fmtNum, fmtPercent,
  list, pageFrame, pick, statusText, table,
} from '../util.js';
import { SERIES_COLORS, drawBarChart, drawGauge } from '../charts.js';

/**
 * 训练前后基线：结构来自引擎 Baseline.as_dict()
 * （spec/srate/channels/device/n_windows/valid/reasons/bands/indices），
 * indices 为 focus/relax/load 的 {mean,std,median,n}。
 */
function baselineRow(label, baseline) {
  const indices = pick(baseline, 'indices', {}) || {};
  const fmt = (name) => {
    const stat = indices[name];
    if (!stat) return DASH;
    return `${fmtNum(pick(stat, 'mean'), 3)}（n=${fmtInt(pick(stat, 'n'))}）`;
  };
  return [
    label,
    fmtInt(pick(baseline, 'n_windows')),
    `${pick(baseline, 'device', DASH)} · ${fmtNum(pick(baseline, 'srate'), 0)} Hz`,
    pick(baseline, 'valid') ? '有效' : '无效',
    `${fmt('focus')} / ${fmt('relax')} / ${fmt('load')}`,
  ];
}

function baselineEntries(summary) {
  const before = pick(summary, 'baseline_before');
  const after = pick(summary, 'baseline_after');
  const rows = [];
  if (before) rows.push(baselineRow('训练前基线', before));
  if (after) rows.push(baselineRow('训练后基线', after));
  return rows;
}

export function render(container, ctx) {
  const uuid = ctx.params.get('session') || pick(ctx.store.state.currentSession, 'uuid', null);
  const actions = uuid ? [
    button('流程视图', () => ctx.navigate(`#/flow?session=${uuid}`)),
    button('实时监测', () => ctx.navigate(`#/live?session=${uuid}`)),
    button('报告', () => ctx.navigate(`#/report?session=${uuid}`)),
  ] : [];
  const host = pageFrame(container, '神经反馈训练', actions);

  if (!uuid) {
    host.append(card('尚未选择会话', el('div', {}, [
      empty('请先选择一次包含训练阶段的会话。'),
      el('div', { class: 'row', style: 'margin-top:12px' }, [
        button('前往历史会话', () => ctx.navigate('#/history'), { primary: true }),
        button('前往被试管理', () => ctx.navigate('#/subjects')),
      ]),
    ])));
    return;
  }

  // 配置读取必须是"惰性"的：直接进本页时 /api/config 还没回来（boot 里是异步加载），
  // 一次性取值会把 target_min / target_max / hold_sec 全部固化成 "—"（实测渲染成 "—–—"）
  const trainingConfig = () => pick(ctx.store.state.config, 'training', {}) || {};
  const local = {
    uuid,
    target: null,
    score: null,
    onTarget: null,
    segment: null,
    mode: null,
    holdSec: pick(trainingConfig(), 'hold_sec', null),
    segments: [],
    summary: null,
    title: '训练进行中',
  };

  /**
   * 会话结束后 `feedback` 不再推送，local.score/target/onTarget 会被清空，
   * 于是"专注度仪表 / 达标状态"整片显示 "—"、仪表变灰。
   * 这里用 GET /api/sessions/{uuid}/training 的最终段 + summary 兜底：
   * 优先取实时值（正在训练时），没有实时值才回落到落库的最终口径。
   */
  const finalView = () => {
    const summarySegments = list(pick(local.summary, 'segments', []));
    const summarySeg = summarySegments.length ? summarySegments[summarySegments.length - 1] : null;
    const seg = local.segments.length ? local.segments[local.segments.length - 1] : summarySeg;
    const stats = (seg && seg.stats) || {};
    const firstDefined = (...values) => values.find((value) => value !== null && value !== undefined);
    return {
      usingFallback: typeof local.score !== 'number' || typeof local.target !== 'number',
      hasData: Boolean(seg),
      score: typeof local.score === 'number' ? local.score : firstDefined(pick(stats, 'mean'), pick(local.summary, 'mean_focus')),
      target: typeof local.target === 'number'
        ? local.target
        : firstDefined(seg && seg.target, pick(local.summary, 'final_target'), pick(local.summary, 'initial_target')),
      achieved: local.onTarget !== null
        ? local.onTarget
        : (typeof pick(stats, 'achieved') === 'boolean' ? pick(stats, 'achieved') : null),
      seq: local.segment !== null ? local.segment : (seg ? pick(seg, 'seq', pick(seg, 'index')) : null),
      holdSec: local.holdSec !== null && local.holdSec !== undefined
        ? local.holdSec
        : firstDefined(pick(seg, 'hold_after'), pick(seg, 'hold_sec'), pick(trainingConfig(), 'hold_sec')),
      mode: local.mode || pick(local.summary, 'mode'),
      ratio: firstDefined(pick(stats, 'on_target_ratio'), pick(local.summary, 'on_target_ratio')),
      n: pick(stats, 'n'),
      segmentCount: local.segments.length || summarySegments.length || null,
    };
  };

  const gaugeHost = el('div');
  const statusHost = el('div');
  const segmentHost = el('div');
  const baselineHost = el('div');
  const noteHost = el('div');
  host.append(el('div', { class: 'grid grid--2' }, [gaugeHost, statusHost]));
  host.append(segmentHost);
  host.append(baselineHost);
  host.append(noteHost);

  /**
   * noteHost 是"追加型"容器：所有写入都先清空再挂一个节点，
   * 避免同一张说明卡被 SSE 重放 / 轮询刷新追加多次（幂等渲染）。
   */
  const renderNote = (node) => {
    noteHost.textContent = '';
    if (node) noteHost.append(node);
  };

  const renderGauge = () => {
    gaugeHost.textContent = '';
    const view = finalView();
    const body = el('div');
    gaugeHost.append(card('专注度仪表', body, {
      sub: `目标线取自 feedback 事件的 target；达标判定阈值 ${fmtNum(pick(trainingConfig(), 'target_min'), 2)}–${fmtNum(pick(trainingConfig(), 'target_max'), 2)}`,
    }));
    drawGauge(body, view.score, {
      target: view.target,
      color: SERIES_COLORS[0],
      note: view.target === null || view.target === undefined ? '' : `目标 ${fmtNum(view.target, 3)}`,
      label: '专注度仪表',
    });
    if (view.usingFallback && view.hasData) {
      body.append(el('p', { class: 'muted', text: '会话已结束（feedback 不再推送）：仪表显示的是落库后的最终段 / 训练汇总口径。' }));
    }
  };

  const renderStatus = () => {
    statusHost.textContent = '';
    const view = finalView();
    const achieved = view.achieved === null || view.achieved === undefined
      ? DASH
      : (view.achieved ? '达标' : '未达标');
    const ratioText = view.ratio === null || view.ratio === undefined
      ? DASH
      : `${fmtPercent(view.ratio)}（有效点 n=${fmtInt(view.n)}）`;
    statusHost.append(card('达标状态', el('div', { class: 'grid grid--3' }, [
      el('div', {}, [el('p', { class: 'muted', text: '当前专注度' }), el('p', { class: 'mono kpi__value', text: fmtNum(view.score, 3) })]),
      el('div', {}, [el('p', { class: 'muted', text: '目标线' }), el('p', { class: 'mono kpi__value', text: fmtNum(view.target, 3) })]),
      el('div', {}, [el('p', { class: 'muted', text: '达标状态' }), el('p', { class: 'kpi__value', text: achieved })]),
      el('div', {}, [el('p', { class: 'muted', text: '当前段' }), el('p', { class: 'mono', text: fmtInt(view.seq) })]),
      el('div', {}, [el('p', { class: 'muted', text: '训练模式' }), el('p', { text: view.mode || DASH })]),
      el('div', {}, [el('p', { class: 'muted', text: '保持时长要求' }), el('p', { class: 'mono', text: fmtDuration(view.holdSec) })]),
      el('div', {}, [el('p', { class: 'muted', text: '本段达标时间占比' }), el('p', { class: 'mono', text: ratioText })]),
    ]), {
      sub: `达标需连续保持 ${fmtDuration(pick(trainingConfig(), 'hold_sec'))}；目标按表现自适应（步长 ${fmtNum(pick(trainingConfig(), 'target_step'), 2)}，区间 ${fmtNum(pick(trainingConfig(), 'target_min'), 2)}–${fmtNum(pick(trainingConfig(), 'target_max'), 2)}）`,
    }));
  };

  const renderSegments = () => {
    segmentHost.textContent = '';
    if (!local.segments.length) {
      segmentHost.append(card('训练分段', [empty('尚未收到 segment 事件（训练阶段开始后每段结束推送一次）。')]));
      return;
    }
    const rows = local.segments.map((segment) => ({
      ...segment,
      __stats: segment.stats || {},
    }));
    const body = el('div');
    const chartHost = el('div');
    body.append(table([
      { title: '段', render: (row) => fmtInt(pick(row, 'seq')) },
      { title: '目标（前→后）', render: (row) => `${fmtNum(row.target, 3)} → ${fmtNum(row.target_after, 3)}` },
      { title: '保持（前→后）', render: (row) => `${fmtDuration(row.hold_sec)} → ${fmtDuration(row.hold_after)}` },
      { title: '均值', align: 'right', render: (row) => fmtNum(pick(row, '__stats.mean'), 3) },
      { title: '达标占比', align: 'right', render: (row) => fmtPercent(pick(row, '__stats.on_target_ratio')) },
      { title: '波动', align: 'right', render: (row) => fmtNum(pick(row, '__stats.volatility'), 3) },
      { title: '有效点', align: 'right', render: (row) => fmtInt(pick(row, '__stats.n')) },
      { title: '排除窗', align: 'right', render: (row) => fmtInt(row.excluded_windows) },
    ], rows));
    body.append(el('h4', { style: 'margin:16px 0 6px', text: '各段达标时间占比' }));
    body.append(chartHost);
    segmentHost.append(card('训练分段', body, { sub: '统计口径与训练闭环同源，排除窗不计入统计' }));
    drawBarChart(chartHost, rows.map((row, index) => ({
      label: `第${fmtInt(row.seq ?? index)}段`,
      value: typeof pick(row, '__stats.on_target_ratio') === 'number' ? pick(row, '__stats.on_target_ratio') : null,
    })).filter((row) => typeof row.value === 'number'), { max: 1, percent: true, height: 180, label: '各段达标占比柱状图' });
  };

  const renderBaseline = (summary) => {
    baselineHost.textContent = '';
    const rows = baselineEntries(summary);
    const comparable = pick(summary, 'baseline_comparable');
    const body = el('div');
    if (rows.length) {
      body.append(table([
        { title: '基线', render: (row) => row[0] },
        { title: '可用窗', align: 'right', render: (row) => row[1] },
        { title: '设备 / 采样率', render: (row) => row[2] },
        { title: '有效性', render: (row) => row[3] },
        { title: '指标均值（专注 / 放松 / 负荷）', render: (row) => row[4] },
      ], rows));
    } else {
      body.append(empty('尚无训练前后基线记录。'));
    }
    body.append(el('div', { class: 'grid grid--3', style: 'margin-top:12px' }, [
      el('div', {}, [el('p', { class: 'muted', text: '训练平均专注度' }), el('p', { class: 'mono', text: fmtNum(pick(summary, 'mean_focus'), 3) })]),
      el('div', {}, [el('p', { class: 'muted', text: '达标时间占比' }), el('p', { class: 'mono', text: fmtPercent(pick(summary, 'on_target_ratio')) })]),
      el('div', {}, [el('p', { class: 'muted', text: '首末变化' }), el('p', { class: 'mono', text: fmtNum(pick(summary, 'first_to_last_change'), 4) })]),
    ]));
    if (comparable === false) {
      body.append(el('p', { class: 'muted', text: '训练前后基线不可比（设备 / 采样率 / 通道或可用窗不足），不作为效果结论。' }));
    }
    baselineHost.append(card('训练前后基线对比', body, {
      sub: pick(summary, 'target_rationale') && typeof pick(summary, 'target_rationale') === 'string'
        ? `目标依据：${pick(summary, 'target_rationale')}`
        : '训练目标初始值取自当次基线中位数对应的评分',
    }));
  };

  renderGauge();
  renderStatus();
  renderSegments();

  /* -------------------------------------------------------- 事件接线 */
  const handleEvent = (type, payload) => {
    switch (type) {
      case 'training_start': {
        local.mode = payload.mode;
        local.target = typeof payload.target === 'number' ? payload.target : null;
        local.holdSec = payload.hold_sec ?? local.holdSec;
        renderNote(card('训练设置', el('div', {}, [
          el('p', { text: `模式 ${payload.mode || DASH}｜${fmtInt(payload.segments)} 段 × ${fmtDuration(payload.segment_sec)}｜初始目标 ${fmtNum(payload.target, 3)}｜保持 ${fmtDuration(payload.hold_sec)}` }),
          payload.rationale ? el('p', { class: 'muted', text: `目标依据：${typeof payload.rationale === 'string' ? payload.rationale : JSON.stringify(payload.rationale)}` }) : null,
        ])));
        renderGauge();
        renderStatus();
        break;
      }
      case 'feedback': {
        local.segment = payload.segment;
        local.target = typeof payload.target === 'number' ? payload.target : local.target;
        local.score = typeof payload.score === 'number' ? payload.score : null;
        local.onTarget = payload.usable === false ? null : Boolean(payload.on_target);
        renderGauge();
        renderStatus();
        break;
      }
      case 'segment': {
        // 幂等入列：SSE 断线重连会带 Last-Event-ID 重放，按 seq 覆盖而不是重复 push，
        // 否则分段表与"各段达标时间占比"柱状图会出现重复条目
        const seq = Number(payload.seq);
        const existing = Number.isFinite(seq)
          ? local.segments.findIndex((item) => Number(item.seq) === seq)
          : -1;
        if (existing >= 0) local.segments[existing] = payload;
        else local.segments.push(payload);
        renderSegments();
        // 段结束后目标可能已自适应，及时同步
        if (typeof payload.target_after === 'number') local.target = payload.target_after;
        renderGauge();
        renderStatus();
        break;
      }
      case 'phase': {
        if (payload.key === 'training' && payload.state === 'done') {
          local.title = '训练阶段已结束';
          renderStatus();
        }
        break;
      }
      case 'finished': {
        if (!local.finishedNoted) {
          local.finishedNoted = true;
          renderNote(card('会话结束', el('div', {}, [
            el('p', { text: `最终状态：${statusText(payload.status)}` }),
            el('div', { class: 'row' }, [
              button('查看报告', () => ctx.navigate(`#/report?session=${uuid}`), { primary: true }),
              el('a', { href: api.exportZipUrl(uuid), text: '下载全部产物（zip）' }),
            ]),
          ])));
        }
        loadTraining();
        break;
      }
      default:
        break;
    }
  };

  /* ------------------------------------------------------ 初次加载 */
  const loadTraining = async () => {
    try {
      const response = await api.training(uuid);
      if (ctx.signal.aborted) return;
      const data = response.data || {};
      const segments = list(data.segments).map((segment) => ({
        ...segment,
        stats: segment.stats && typeof segment.stats === 'object' ? segment.stats : {
          mean: segment.mean_score,
          on_target_ratio: segment.on_target_ratio,
          n: segment.n,
        },
      }));
      if (segments.length) local.segments = segments;
      local.summary = data.summary || null;
      renderSegments();
      renderBaseline(data.summary || {});
      // 会话结束后 feedback 停推，仪表/达标状态必须由落库数据兜底刷新（否则整卡都是 —）
      renderGauge();
      renderStatus();
    } catch (error) {
      if (error && error.name === 'ApiError' && (error.status === 404 || error.status === 409)) {
        baselineHost.textContent = '';
        baselineHost.append(card('训练前后基线对比', [empty('该会话暂无训练记录（训练阶段尚未结束或未执行）。')]));
        return;
      }
      baselineHost.textContent = '';
      baselineHost.append(card('训练前后基线对比', [empty(`加载失败：${describeError(error)}`)]));
      toast(describeError(error));
    }
  };

  api.getSession(uuid).then((response) => {
    if (ctx.signal.aborted) return;
    const data = response.data || {};
    const session = data.session ? { ...data.session } : { ...data };
    session.uuid = uuid;
    ctx.store.setState({ currentSession: session });
  }).catch(() => {});

  loadTraining();

  const events = connectSessionEvents(uuid, {
    onEvent: (type, frame) => {
      if (ctx.signal.aborted) return;
      handleEvent(type, frame && frame.data);
    },
  });
  ctx.onCleanup(() => events.close());

  // 兜底：SSE 断线时每 10 秒拉一次训练汇总
  const timer = window.setInterval(() => {
    if (document.hidden) return;
    loadTraining();
  }, 10000);
  ctx.onCleanup(() => window.clearInterval(timer));
}

export default render;
