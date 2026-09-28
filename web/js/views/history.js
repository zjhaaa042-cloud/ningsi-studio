/**
 * 历史视图：会话表格（分页 + 按被试/状态筛选）、周/月趋势、不可比提示、单会话导出。
 *
 * 数据来源：
 * - GET /api/sessions?participant=&status=&page=&limit=
 * - GET /api/subjects?limit=100（筛选下拉）
 * - GET /api/reports/trend?participant=&field=&period=（含 note 与 ledger_points）
 * - GET /api/sessions/{uuid}/export.zip（导出链接，直接指向接口）
 */

import { api } from '../api.js';
import { describeError, toast } from '../store.js';
import {
  DASH, button, card, el, empty, field, fmtInt, fmtNum, fmtPercent,
  fmtTime, list, pageFrame, pick, statusText, table,
} from '../util.js';
import { SERIES_COLORS, drawLineChart } from '../charts.js';

const STATUS_OPTIONS = [
  ['', '全部状态'],
  ['running', '进行中'],
  ['done', '已完成'],
  ['failed', '失败'],
  ['cancelled', '已取消'],
];

/** 视图内筛选条件：切换页签/返回时保留。 */
const view = { participant: '', status: '', page: 1, field: 'focus', period: 'week' };

function normalizePoint(point) {
  if (!point || typeof point !== 'object') return null;
  const label = point.period ?? point.label ?? point.bucket ?? point.t ?? point.key;
  const value = point.mean ?? point.value ?? point.focus ?? point.avg;
  return {
    label: label === undefined || label === null ? DASH : String(label),
    value: typeof value === 'number' && Number.isFinite(value) ? value : null,
  };
}

export async function render(container, ctx) {
  const host = pageFrame(container, '历史与趋势', [
    button('刷新', () => ctx.reload()),
  ]);

  /* ------------------------------------------------------------ 筛选项 */
  // 被试下拉优先用全局缓存；直接进入本页时缓存可能还没填好，这里补一次拉取
  if (!list(ctx.store.state.subjects).length) {
    try {
      const response = await api.listSubjects({ limit: 100, page: 1 });
      if (ctx.signal.aborted) return;
      ctx.store.setState({ subjects: list(pick(response.data, 'items', [])) });
    } catch (error) {
      console.warn('[history] 被试列表加载失败', error);
    }
  }
  const participants = (() => {
    const fromStore = list(ctx.store.state.subjects).map((subject) => subject.public_id);
    return Array.from(new Set(fromStore.filter(Boolean)));
  })();

  const participantSelect = el('select', { id: 'history-participant' });
  participantSelect.append(el('option', { value: '', text: '全部被试' }));
  for (const publicId of participants) participantSelect.append(el('option', { value: publicId, text: `sub-${publicId}` }));
  participantSelect.value = view.participant;

  const statusSelect = el('select', { id: 'history-status' });
  for (const [value, text] of STATUS_OPTIONS) statusSelect.append(el('option', { value, text }));
  statusSelect.value = view.status;

  const fieldSelect = el('select', { id: 'history-field' });
  for (const [value, text] of [['focus', '专注度'], ['relax', '放松度'], ['load', '认知负荷']]) {
    fieldSelect.append(el('option', { value, text }));
  }
  fieldSelect.value = view.field;

  const periodSelect = el('select', { id: 'history-period' });
  for (const [value, text] of [['week', '按周'], ['month', '按月']]) {
    periodSelect.append(el('option', { value, text }));
  }
  periodSelect.value = view.period;

  const applyFilters = () => ctx.reload();
  for (const node of [participantSelect, statusSelect]) {
    node.addEventListener('change', () => {
      view.participant = participantSelect.value;
      view.status = statusSelect.value;
      view.page = 1;
      applyFilters();
    });
  }

  const filterCard = card('筛选与口径', el('div', { class: 'filters' }, [
    field('被试', participantSelect),
    field('会话状态', statusSelect),
    field('趋势指标', fieldSelect),
    field('趋势周期', periodSelect),
    button('重算趋势', () => {
      view.field = fieldSelect.value;
      view.period = periodSelect.value;
      applyFilters();
    }),
  ]), { sub: '趋势只聚合"可比"记录：设备、采样率或通道数变化会被排除（可比性保护）' });
  host.append(filterCard);

  /* -------------------------------------------------------- 会话表格 */
  let sessionsData = null;
  try {
    const response = await api.listSessions({
      participant: view.participant || undefined,
      status: view.status || undefined,
      page: view.page,
      limit: 20,
    });
    sessionsData = response.data || {};
  } catch (error) {
    host.append(card('会话列表', [empty(`加载失败：${describeError(error)}`)]));
    toast(describeError(error));
    return;
  }
  if (ctx.signal.aborted) return;

  const items = list(pick(sessionsData, 'items', []));
  const total = Number(pick(sessionsData, 'total', 0)) || 0;
  const limit = Number(pick(sessionsData, 'limit', 20)) || 20;
  const page = Number(pick(sessionsData, 'page', view.page)) || 1;
  const pageCount = Math.max(1, Math.ceil(total / limit));

  const pagination = el('div', { class: 'row row--between', style: 'margin-top:12px' }, [
    el('span', { class: 'muted', text: `第 ${fmtInt(page)} / ${fmtInt(pageCount)} 页，共 ${fmtInt(total)} 条` }),
    el('div', { class: 'row' }, [
      button('上一页', () => {
        view.page = Math.max(1, page - 1);
        ctx.reload();
      }, { small: true, disabled: page <= 1 }),
      button('下一页', () => {
        view.page = Math.min(pageCount, page + 1);
        ctx.reload();
      }, { small: true, disabled: page >= pageCount }),
    ]),
  ]);

  host.append(card('会话列表', el('div', {}, [
    items.length ? table([
      {
        title: '会话',
        render: (row) => el('button', {
          class: 'btn btn--sm',
          type: 'button',
          text: `${String(row.uuid).slice(0, 8)}…`,
          onClick: () => ctx.navigate(`#/live?session=${row.uuid}`),
        }),
      },
      { title: '被试', render: (row) => (row.participant ? `sub-${row.participant}` : DASH) },
      { title: '状态', render: (row) => statusText(row.status) },
      { title: '阶段', render: (row) => row.phase_label || row.phase || DASH },
      { title: '进度', align: 'right', render: (row) => fmtPercent(row.progress) },
      { title: '设备', render: (row) => pick(row, 'device', DASH) || DASH },
      { title: '时间倍率', align: 'right', render: (row) => fmtNum(row.time_scale, 2) },
      { title: '预警', align: 'right', render: (row) => fmtInt(row.alert_count) },
      { title: '开始时间', render: (row) => fmtTime(row.started_at) },
      {
        title: '操作',
        render: (row) => el('div', { class: 'row' }, [
          el('a', { href: api.exportZipUrl(row.uuid), text: '导出 zip', title: '打包下载该会话全部产物' }),
          el('a', { href: `#/report?session=${row.uuid}`, text: '报告' }),
          el('a', { href: `#/training?session=${row.uuid}`, text: '训练' }),
        ]),
      },
    ], items) : empty('没有符合筛选条件的会话。'),
    pagination,
  ])));

  /* ---------------------------------------------------------- 趋势图 */
  const trendHost = el('div');
  host.append(card('周 / 月趋势', trendHost, {
    sub: '字段与周期由上方筛选控制；数据来自 GET /api/reports/trend',
  }));

  try {
    const response = await api.reportsTrend({
      participant: view.participant || undefined,
      field: view.field,
      period: view.period,
    });
    if (ctx.signal.aborted) return;
    const data = response.data || {};
    const points = list(data.points).map(normalizePoint).filter(Boolean);
    const ledgerPoints = list(data.ledger_points).map(normalizePoint).filter(Boolean);

    trendHost.append(el('p', {
      class: 'muted',
      text: `指标 ${pick(data, 'field', view.field)}｜周期 ${pick(data, 'period', view.period)}｜参与聚合会话 ${fmtInt(data.sessions)} 次（台账点 ${fmtInt(ledgerPoints.length)} 个）`,
    }));
    if (points.length) {
      const chartHost = el('div');
      drawLineChart(chartHost, points.map((point) => ({ label: point.label, values: { value: point.value } })), {
        series: ['value'],
        colors: [SERIES_COLORS[0]],
        min: 0,
        max: 1,
        percent: true,
        label: '跨会话趋势折线图',
      });
      trendHost.append(chartHost);
      trendHost.append(table([
        { title: '周期', render: (row) => row.label },
        { title: '均值', align: 'right', render: (row) => fmtNum(row.value, 3) },
      ], points));
    } else {
      trendHost.append(empty('暂无趋势点（会话尚未完成，或因跨设备不可比被排除）。'));
    }
    if (data.note) {
      trendHost.append(el('p', { class: 'muted', text: `不可比说明：${data.note}` }));
    }
    if (ledgerPoints.length) {
      trendHost.append(el('p', { class: 'muted', text: `台账点（JSONL）示例：${ledgerPoints.slice(-3).map((point) => `${point.label}=${fmtNum(point.value, 3)}`).join('，')}` }));
    }
  } catch (error) {
    trendHost.append(empty(`趋势加载失败：${describeError(error)}`));
    toast(describeError(error));
  }
}

export default render;
