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
  list, pick, statusText, table,
} from '../util.js';
import { SERIES_COLORS, drawGauge, drawLineChart } from '../charts.js';

/** 从 runs 里取质检事件（phase=qc 的 payload 由后端 merge 写入）。 */
function qualityFromRuns(runs) {
  for (const run of list(runs)) {
    const payload = run && run.payload;
    if (payload && typeof payload === 'object' && payload.passed !== undefined) return payload;
  }
  return null;
}

/** 趋势点字段兼容：后端聚合结构可能是 {period, mean, n} 或 {label, value}。 */
function normalizeTrendPoint(point) {
  if (!point || typeof point !== 'object') return null;
  const label = point.period ?? point.label ?? point.bucket ?? point.t ?? point.key;
  const value = point.mean ?? point.value ?? point.focus ?? point.avg;
  return {
    label: label === undefined || label === null ? DASH : String(label),
    value: typeof value === 'number' && Number.isFinite(value) ? value : null,
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
  try {
    [health, overview] = await Promise.all([api.health(), api.overview()]);
  } catch (error) {
    container.textContent = '';
    container.append(card('仪表盘', [empty(`加载失败：${describeError(error)}`)]));
    toast(describeError(error));
    return;
  }
  if (ctx.signal.aborted) return;

  ctx.store.setState({ health: health.data, overview: overview.data });
  const stats = overview.data || {};
  const latest = list(pick(stats, 'latest_detail', [])).slice(0, 6);
  const newest = latest[0] || null;

  // 最近会话的细节：KPI 里的均值/预警数都来自这里，避免用列表接口猜数据
  let detail = null;
  if (newest && newest.uuid) {
    try {
      const response = await api.getSession(newest.uuid);
      detail = response.data;
    } catch (error) {
      detail = null;
    }
  }
  if (ctx.signal.aborted) return;

  const summary = pick(detail, 'indicator_summary', {}) || {};
  const quality = qualityFromRuns(pick(detail, 'runs', []));
  const alertCount = list(pick(detail, 'alerts', null)).length || (newest ? pick(newest, 'alert_count', null) : null);
  const subjectCount = stats.subjects;
  const doneCount = stats.sessions_done;
  const totalCount = stats.sessions;

  const page = el('div');
  page.append(el('div', { class: 'row row--between' }, [
    el('h1', { text: '仪表盘' }),
    // 会话只能在被试页创建（要先选/建被试），所以这里直接送到被试管理
    button('新建会话', () => ctx.navigate('#/subjects'), {
      primary: true,
      title: '去“被试管理”里选好被试并点“开始新会话”',
    }),
  ]));

  /* ------------------------------------------------------------------ KPI */
  const kpiRow = el('div', { class: 'grid grid--kpi' }, [
    renderKpi('最近会话 · 专注度', fmtNum(pick(summary, 'focus.mean')), '指标均值（0–1）'),
    renderKpi('最近会话 · 放松度', fmtNum(pick(summary, 'relax.mean')), '指标均值（0–1）'),
    renderKpi('最近会话 · 认知负荷', fmtNum(pick(summary, 'load.mean')), '指标均值（0–1）'),
    renderKpi('质检通过率', quality ? fmtPercent(pick(quality, 'valid_ratio')) : DASH,
      quality ? `${fmtInt(pick(quality, 'usable'))}/${fmtInt(pick(quality, 'windows'))} 窗可用` : '尚无质检记录'),
    renderKpi('预警数', fmtInt(alertCount), newest ? `最近会话 ${fmtRelative(newest.started_at)}` : '暂无会话'),
    renderKpi('被试 / 会话', `${fmtInt(subjectCount)} / ${fmtInt(totalCount)}`,
      `已完成 ${fmtInt(doneCount)} 次`),
  ]);
  page.append(card('关键指标', kpiRow, {
    sub: newest ? `最近会话 ${String(newest.uuid).slice(0, 8)}… · 状态 ${statusText(newest.status)}` : '尚无会话记录',
  }));
  // 质检可用窗比例用仪表盘再画一遍：评审时需要"一眼看出质量是否达标"
  if (quality) {
    const gaugeHost = el('div');
    drawGauge(gaugeHost, Number(pick(quality, 'valid_ratio')), {
      color: SERIES_COLORS[2],
      note: `门槛 ${fmtPercent(0.6)}（valid_ratio_min，取自 config.quality）`,
      label: '质检可用窗比例仪表',
    });
    page.append(card('采集质量', gaugeHost, {
      sub: `可用窗 ${fmtInt(pick(quality, 'usable'))}/${fmtInt(pick(quality, 'windows'))}｜${pick(quality, 'passed') ? '质检通过' : '未达门槛，结果解释需谨慎'}`,
    }));
  }

  /* -------------------------------------------------------------- 趋势图 */
  const trendBody = el('div');
  const trendCard = card('专注度周趋势', trendBody);
  page.append(trendCard);

  const participant = newest ? newest.participant : null;
  if (!participant) {
    trendBody.append(empty('还没有可聚合的会话，先创建被试并完成一次会话。'));
  } else {
    try {
      const response = await api.reportsTrend({ participant, field: 'focus', period: 'week' });
      if (ctx.signal.aborted) return;
      const data = response.data || {};
      const points = list(data.points).map(normalizeTrendPoint).filter(Boolean);
      trendBody.append(el('p', {
        class: 'muted',
        text: `被试 sub-${participant} · 参与聚合 ${fmtInt(data.sessions)} 次会话`,
      }));
      if (points.length) {
        const canvasHost = el('div');
        drawLineChart(canvasHost, points.map((point) => ({ label: point.label, values: { focus: point.value } })), {
          series: ['focus'],
          colors: [SERIES_COLORS[0]],
          max: 1,
          min: 0,
          percent: true,
          label: '专注度周趋势折线图',
        });
        trendBody.append(canvasHost);
      } else {
        trendBody.append(empty('暂无趋势点（会话尚未完成，或因跨设备不可比而未被聚合）'));
      }
      if (data.note) {
        trendBody.append(el('p', { class: 'muted', text: `可比性说明：${data.note}` }));
      }
    } catch (error) {
      trendBody.append(empty(`趋势加载失败：${describeError(error)}`));
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
          onClick: row.onOpen,
        }),
      },
      { title: '被试', render: (row) => (row.participant ? `sub-${row.participant}` : DASH) },
      { title: '状态', render: (row) => statusText(row.status) },
      { title: '阶段', render: (row) => row.phase_label || row.phase || DASH },
      { title: '进度', align: 'right', render: (row) => fmtPercent(row.progress) },
      { title: '预警', align: 'right', render: (row) => fmtInt(row.alert_count) },
      { title: '开始时间', render: (row) => fmtTime(row.started_at) },
    ], sessionRows) : empty('还没有会话记录，点击右上角"新建会话"开始一次八阶段流程。'),
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
    el('div', {}, [
      el('p', { class: 'muted', text: '数据目录' }),
      el('p', { class: 'mono nowrap', text: pick(health.data, 'data_dir', DASH), title: pick(health.data, 'data_dir', '') }),
    ]),
  ]));
  page.append(environmentCard);

  container.textContent = '';
  container.append(page);
}

export default render;
