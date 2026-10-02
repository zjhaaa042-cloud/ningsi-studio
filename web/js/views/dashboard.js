/**
 * 仪表盘：KPI、周趋势、最近会话、快捷新建会话。
 *
 * 数据来源（严格按 docs/API.md）：
 * - GET /api/health、GET /api/overview
 * - GET /api/sessions/{uuid}（取最近会话的 indicator_summary、alert_count、runs 里的质检结果）
 * - GET /api/reports/trend?participant=&field=&period=
 */

import { api } from '../api.js';
import { describeError, toast } from '../store.js';
import {
  DASH, button, card, el, empty, fmtInt, fmtNum, fmtPercent, fmtRelative, fmtTime,
  list, phaseText, pick, statusText, table,
} from '../util.js';
import { SERIES_COLORS, drawGauge, drawLineChart } from '../charts.js';

/**
 * 从 runs 里取质检事件（phase=qc 的 payload 由后端 _decode_payload 落成对象）。
 * 保险起见仍兼容"payload 是 JSON 字符串"的旧数据，避免整卡退回"尚无质检记录"。
 */
function qualityFromRuns(runs) {
  for (const run of list(runs)) {
    let payload = run && run.payload;
    if (typeof payload === 'string') {
      try {
        payload = JSON.parse(payload);
      } catch (error) {
        payload = null;
      }
    }
    if (payload && typeof payload === 'object' && (payload.passed !== undefined || payload.windows !== undefined)) {
      return payload;
    }
  }
  return null;
}

/** 趋势点字段兼容：后端聚合结构可能是 {period, mean, n} 或 {label, value}。 */
function normalizeTrendPoint(point) {
  if (!point || typeof point !== 'object') return null;
  const label = point.period ?? point.label ?? point.bucket ?? point.t ?? point.key;
  const value = point.mean ?? point.value ?? point.focus ?? point.avg;
  const n = Number(point.n ?? point.count);
  const rejected = Number(point.rejected);
  return {
    label: label === undefined || label === null ? DASH : String(label),
    value: typeof value === 'number' && Number.isFinite(value) ? value : null,
    n: Number.isFinite(n) ? n : null,
    rejected: Number.isFinite(rejected) ? rejected : null,
  };
}

function renderKpi(label, value, hint) {
  return el('div', { class: 'card kpi' }, [
    el('span', { class: 'kpi__label', text: label }),
    el('span', { class: 'kpi__value', text: value }),
    hint ? el('span', { class: 'kpi__hint', text: hint }) : null,
  ]);
}

export async function render(container, ctx) {
  container.textContent = '';
  container.append(el('p', { class: 'muted', text: '正在加载仪表盘…' }));
  let health;
  let overview;
  let configResponse = null;
  let recent = [];
  try {
    // config 与仪表盘同时取：质检门槛（config.quality.valid_ratio_min）必须来自接口，
    // 不能写死；config 失败不拖垮整个仪表盘
    [health, overview, configResponse] = await Promise.all([
      api.health(), api.overview(), api.config().catch(() => null),
    ]);
  } catch (error) {
    container.textContent = '';
    container.append(card('仪表盘', [empty(`加载失败：${describeError(error)}`)]));
    toast(describeError(error));
    return;
  }
  if (ctx.signal.aborted) return;
  if (configResponse) ctx.store.setState({ config: configResponse.data });

  // 最近会话表格改用 GET /api/sessions：只有这个列表口径带 alert_count，
  // 用 /api/overview 的 latest_detail 会让"预警"整列都显示 —（实测）
  try {
    const response = await api.listSessions({ limit: 6, page: 1 });
    recent = list(pick(response.data, 'items', []));
  } catch (error) {
    console.warn('[dashboard] 会话列表加载失败，回退到 /api/overview 的 latest_detail', error);
  }
  if (ctx.signal.aborted) return;

  ctx.store.setState({ health: health.data, overview: overview.data });
  const stats = overview.data || {};
  const latest = (recent.length ? recent : list(pick(stats, 'latest_detail', []))).slice(0, 6);
  const newest = latest[0] || null;
  // KPI / 质检 / 预警的口径必须取"最近一条【已完成】会话"：latest[0] 可能正在运行，
  // 运行中的会话还没有 indicator_summary、qc 也未必落库，整排 KPI 会一起变成 —
  // （Lead 实测：最新会话 fe1817 status=running 时仪表盘 KPI 整排空）
  const newestDone = latest.find((row) => row.status === 'done') || null;
  const metricRow = newestDone || newest;

  // 最近会话的细节：KPI 里的均值/预警数都来自这里，避免用列表接口猜数据
  let detail = null;
  if (metricRow && metricRow.uuid) {
    try {
      const response = await api.getSession(metricRow.uuid);
      detail = response.data;
    } catch (error) {
      detail = null;
    }
  }
  if (ctx.signal.aborted) return;

  const summary = pick(detail, 'indicator_summary', {}) || {};
  const quality = qualityFromRuns(pick(detail, 'runs', []));
  // 预警数口径：明细存在（哪怕是 0 条）就用明细条数；只有"没有明细"才回落到列表的 alert_count。
  // 原来的 `list(...).length || alert_count` 会把"明细 0 条"串到另一个口径上。
  const alertItems = detail ? list(pick(detail, 'alerts', [])) : null;
  const alertCount = alertItems !== null
    ? alertItems.length
    : (metricRow ? pick(metricRow, 'alert_count', null) : null);
  const qualityConfig = pick(ctx.store.state.config, 'quality', {}) || {};
  const validRatioMin = pick(qualityConfig, 'valid_ratio_min', null);
  const subjectCount = stats.subjects;
  const doneCount = stats.sessions_done;
  const totalCount = stats.sessions;

  const page = el('div');
  page.append(el('div', { class: 'row row--between' }, [
    el('h1', { text: '仪表盘' }),
    // 会话只能在被试页创建（要先选/建被试），所以这里是"跳转"而不是"直接建会话"：
    // 按钮文案必须说明它只是导航，避免误解为点一下就建好会话
    button('去创建会话', () => ctx.navigate('#/subjects'), {
      primary: true,
      title: '前往“被试管理”：先选好被试，再点“开始新会话”',
    }),
  ]));

  /* ------------------------------------------------------------------ KPI */
  const kpiRow = el('div', { class: 'grid grid--kpi' }, [
    renderKpi('最近会话 · 专注度', fmtNum(pick(summary, 'focus.mean')), '指标均值（0–1）'),
    renderKpi('最近会话 · 放松度', fmtNum(pick(summary, 'relax.mean')), '指标均值（0–1）'),
    renderKpi('最近会话 · 认知负荷', fmtNum(pick(summary, 'load.mean')), '指标均值（0–1）'),
    // 无质检记录时给明确文案：不要只留一个静默的 "—" 让人猜
    renderKpi('质检通过率', quality ? fmtPercent(pick(quality, 'valid_ratio')) : '无记录',
      quality
        ? `${fmtInt(pick(quality, 'usable'))}/${fmtInt(pick(quality, 'windows'))} 窗可用`
        : '最近会话没有质检（qc）阶段记录'),
    renderKpi('预警数', fmtInt(alertCount), metricRow ? `最近会话 ${fmtRelative(metricRow.started_at)}` : '暂无会话'),
    renderKpi('被试 / 会话', `${fmtInt(subjectCount)} / ${fmtInt(totalCount)}`,
      `已完成 ${fmtInt(doneCount)} 次`),
  ]);
  page.append(card('关键指标', kpiRow, {
    sub: metricRow
      ? `最近会话 ${String(metricRow.uuid).slice(0, 8)}… · 状态 ${statusText(metricRow.status)}`
        + (newest && newest.status !== 'done' ? '（最新一条仍在运行，指标口径取最近一条已完成会话）' : '')
      : '尚无会话记录',
  }));
  // 质检可用窗比例用仪表盘再画一遍：评审时需要"一眼看出质量是否达标"
  // 门槛值读 config.quality.valid_ratio_min（原来是写死的 0.6，与文案"取自 config.quality"不符）
  if (quality) {
    const gaugeHost = el('div');
    const rawRatio = pick(quality, 'valid_ratio', null);
    const threshold = typeof validRatioMin === 'number' && Number.isFinite(validRatioMin) ? validRatioMin : null;
    drawGauge(gaugeHost, typeof rawRatio === 'number' && Number.isFinite(rawRatio) ? rawRatio : null, {
      color: SERIES_COLORS[2],
      target: threshold,
      note: threshold === null
        ? '门槛：config.quality.valid_ratio_min 未配置'
        : `门槛 ${fmtPercent(threshold)}（config.quality.valid_ratio_min）`,
      label: '质检可用窗比例仪表',
    });
    page.append(card('采集质量', gaugeHost, {
      sub: `可用窗 ${fmtInt(pick(quality, 'usable'))}/${fmtInt(pick(quality, 'windows'))}｜${pick(quality, 'passed') ? '质检通过' : '未达门槛，结果解释需谨慎'}`,
    }));
  } else {
    page.append(card('采集质量', [
      empty('最近会话没有质检（qc）阶段记录，无法给出可用窗比例。'),
    ], { sub: '质检结果来自 GET /api/sessions/{uuid} 的 runs（phase = qc）' }));
  }

  /* -------------------------------------------------------------- 趋势图 */
  const trendBody = el('div');
  const trendCard = card('专注度周趋势', trendBody);
  page.append(trendCard);

  // 趋势不能被"最新会话"绑死：最新一条可能正在运行（或该被试没有可聚合点），
  // 于是按最近几条会话的被试去重、依次查询，取第一个 points 非空的结果。
  const trendCandidates = [];
  for (const row of latest) {
    const name = row && row.participant;
    if (name && !trendCandidates.includes(name)) trendCandidates.push(name);
    if (trendCandidates.length >= 5) break;
  }
  if (!trendCandidates.length) {
    trendBody.append(empty('还没有可聚合的会话：先创建被试并完成一次会话。'));
  } else {
    let shown = null;          // { participant, data, points }
    let lastData = null;       // 最后一个可用的响应，用于解释空态原因
    let failed = 0;
    for (const participant of trendCandidates) {
      try {
        const response = await api.reportsTrend({ participant, field: 'focus', period: 'week' });
        if (ctx.signal.aborted) return;
        const data = response.data || {};
        lastData = data;
        // 趋势聚合只统计已完成会话（运行中的会话会让 points 为空）
        const points = list(data.points).map(normalizeTrendPoint).filter(Boolean);
        if (points.length) {
          shown = { participant, data, points };
          break;
        }
      } catch (error) {
        failed += 1;
        lastData = lastData || { error: true };
      }
    }
    if (!shown) {
      // 区分"有会话但都不可比"与"还没有已完成会话"，不要让人猜
      const rejected = Number(pick(lastData, 'rejected', 0)) || 0;
      const anyDone = Boolean(newestDone);
      trendBody.append(empty(anyDone
        ? `暂无趋势点：已按被试 ${trendCandidates.map((name) => `sub-${name}`).join('、')} 逐个查询，`
          + `均没有可聚合的已完成会话${rejected ? `（其中 ${fmtInt(rejected)} 条因设备/采样率/通道变化被判不可比）` : ''}。`
        : '暂无趋势点：最近的会话还没有完成（趋势聚合只统计已完成会话），完成后再看这里。'
          + (failed ? `（另有 ${fmtInt(failed)} 次查询失败）` : '')));
    } else {
      const { participant, data, points } = shown;
      trendBody.append(el('p', {
        class: 'muted',
        text: `被试 sub-${participant} · 参与聚合 ${fmtInt(data.sessions)} 次会话`
          + (Number(data.rejected) ? ` · 因跨设备不可比排除 ${fmtInt(data.rejected)} 条` : ''),
      }));
      const current = points[points.length - 1];
      // 只有一个数据点时折线本身几乎不可见，必须把"当期是哪个周期、值是多少"直接写出来
      trendBody.append(el('p', {
        class: 'mono',
        text: `当期 ${current.label}：专注度 ${fmtPercent(current.value, 1)}`
          + (current.n === null ? '' : `（有效窗 n=${fmtInt(current.n)}）`),
      }));
      // 单点 / 双点没有曲线形状信息，等价于"带标注的点位图"：用更矮的画布，
      // 点数多时才给 220px（drawLineChart 会按容器实测宽度 1:1 出图，不再等比放大）
      const compact = points.length <= 2;
      const canvasHost = el('div', { class: 'chart-host' });
      // 先入 DOM 再画：drawLineChart 需要容器已有真实宽度（clientWidth）
      trendBody.append(canvasHost);
      drawLineChart(canvasHost, points.map((point) => ({ label: point.label, values: { focus: point.value } })), {
        series: ['focus'],
        colors: [SERIES_COLORS[0]],
        max: 1,
        min: 0,
        percent: true,
        showValues: true,
        height: compact ? 170 : 220,
        label: '专注度周趋势折线图',
      });
      trendBody.append(table([
        { title: '周期', render: (row) => row.label },
        { title: '专注度均值', align: 'right', render: (row) => fmtPercent(row.value, 1) },
        { title: '有效窗 n', align: 'right', render: (row) => fmtInt(row.n) },
      ], points));
      if (data.note) {
        trendBody.append(el('p', { class: 'muted', text: `可比性说明：${data.note}` }));
      }
    }
  }

  /* ------------------------------------------------------ 最近会话表格 */
  const sessionRows = latest.map((row) => ({
    ...row,
    onOpen: () => ctx.navigate(`#/live?session=${row.uuid}`),
  }));
  const sessionsCard = card('最近会话', [
    sessionRows.length ? table([
      {
        title: '会话',
        render: (row) => el('button', {
          class: 'btn btn--sm',
          type: 'button',
          text: `${String(row.uuid).slice(0, 8)}…`,
          title: '打开实时监测视图',
          // 按钮文字只有"省略号编号"，读屏念不出目标，必须补 aria-label
          attrs: { 'aria-label': `打开会话 ${row.uuid} 的实时监测` },
          onClick: row.onOpen,
        }),
      },
      { title: '被试', render: (row) => (row.participant ? `sub-${row.participant}` : DASH) },
      { title: '状态', render: (row) => statusText(row.status) },
      // 列表接口的 phase_label 对流程阶段是中文名、终态为 null（后端不编造），
      // 终态本地化交给 phaseText，未知键原样露出
      { title: '阶段', render: (row) => phaseText(row.phase, row.phase_label) },
      { title: '进度', align: 'right', render: (row) => fmtPercent(row.progress) },
      { title: '预警', align: 'right', render: (row) => fmtInt(row.alert_count) },
      { title: '开始时间', render: (row) => fmtTime(row.started_at) },
    ], sessionRows) : empty('还没有会话记录，点右上角“去创建会话”到被试管理里开始一次完整流程。'),
  ], { sub: '点击会话编号进入实时监测' });
  page.append(sessionsCard);

  /* ------------------------------------------------------------ 运行环境 */
  const engine = pick(health.data, 'engine', {}) || {};
  const environmentCard = card('运行环境', el('div', { class: 'grid grid--3' }, [
    el('div', {}, [
      el('p', { class: 'muted', text: '引擎口径' }),
      el('p', { class: 'mono', text: `频谱 ${engine.spectrum || DASH}｜指标 ${engine.indicator || DASH}｜基线 ${engine.baseline || DASH}｜评估 ${engine.assessment || DASH}` }),
    ]),
    el('div', {}, [
      el('p', { class: 'muted', text: '并发上限' }),
      el('p', { class: 'mono', text: `${fmtInt(pick(health.data, 'max_active_sessions'))} 个会话；当前运行 ${fmtInt(list(pick(health.data, 'active_sessions', [])).length)} 个` }),
    ]),
    el('div', { class: 'env-cell' }, [
      el('p', { class: 'muted', text: '数据目录' }),
      // 用 `--data D:\...`（文档推荐的换目录方式）时这条路径可达 90+ 字符：
      // 原先是 `mono nowrap`，在 grid--3 里既不换行也不收缩，把整行撑出 297px 横向滚动。
      // 现在改成单行省略号（path-ellipsis）+ title 保留完整路径：
      // 既不撑破布局，也不抬高卡片高度（保证仪表盘整页高度不变），完整路径仍可 hover/复制。
      el('p', {
        class: 'mono path-ellipsis',
        text: pick(health.data, 'data_dir', DASH),
        title: pick(health.data, 'data_dir', ''),
      }),
    ]),
  ]));
  page.append(environmentCard);

  container.textContent = '';
  container.append(page);
}

export default render;
