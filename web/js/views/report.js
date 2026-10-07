/**
 * 报告视图：Markdown 极简渲染、证据表、建议、边界声明、产物下载列表。
 *
 * 数据来源：
 * - GET /api/sessions/{uuid}/report → 结构化报告 JSON；尚未生成时返回 202 且带 partial: true
 * - GET /api/sessions/{uuid}/artifacts → [{kind, path, bytes, sha256, exists, download}]
 * - 边界声明原文取自 GET /api/config 的 privacy.boundary
 *
 * 说明：接口返回的是结构化 JSON（不是 Markdown 字符串），因此这里先按接口字段拼出
 * Markdown 再由 util.renderMarkdown 渲染——只支持标题/列表/粗体/表格/引用/代码块。
 */

import { api } from '../api.js';
import { describeError, toast } from '../store.js';
import {
  DASH, button, card, el, empty, fmtBytes, fmtInt, fmtNum, fmtPercent, fmtTime,
  list, pageFrame, pick, renderMarkdown, statusText, table,
} from '../util.js';

const INDICATOR_LABELS = { focus: '专注度', relax: '放松度', load: '认知负荷' };
const DIMENSION_LABELS = { attention: '专注维度', stress: '压力维度' };

/** 表格单元格里的 `|` 会破坏极简 Markdown 表格语法，统一替换。 */
function cell(value) {
  if (value === null || value === undefined) return DASH;
  return String(value).replace(/\|/g, '／').replace(/\n/g, ' ');
}

/** 结构化报告 → Markdown 文本。 */
function composeMarkdown(report, config) {
  const lines = [];
  const participant = pick(report, 'participant', null);
  const session = pick(report, 'session', null);
  const run = pick(report, 'run', null);
  const versions = pick(report, 'versions', {}) || {};

  lines.push(`# 凝思状态评估报告（sub-${cell(participant)} / ses-${cell(session)} / run-${cell(run)}）`);
  lines.push('');
  lines.push(`- 评估口径：${cell(pick(report, 'spec'))}；频谱 ${cell(versions.spectrum)}；指标 ${cell(versions.indicator)}；基线 ${cell(versions.baseline)}`);
  lines.push(`- 软件版本：${cell(versions.product)}`);
  lines.push(`- 生成时间：${cell(fmtTime(pick(report, 'created_at') || new Date().toISOString()))}`);

  // 采集调理：只有真实设备会话才有（仿真源不做这一步），与后端 report.to_markdown() 表头同一口径。
  // 实时链路的波形/指标都是调理后的数据，报告里必须写清口径，否则同一段数据无法复算。
  const conditioning = pick(report, 'extras.signal_conditioning', null);
  if (conditioning) {
    const stages = list(pick(conditioning, 'chain.stages', []))
      .map((stage) => pick(stage, 'stage')).filter(Boolean).join(' → ');
    lines.push(`- 采集调理：${cell(pick(conditioning, 'spec'))}（${cell(pick(conditioning, 'dc_removal'))} → ${cell(stages)}，零相位）；上下文 ${cell(fmtInt(pick(conditioning, 'context_samples')))} 点 / 输出 ${cell(fmtInt(pick(conditioning, 'window_samples')))} 点`);
  }

  /* 一、状态评分 */
  lines.push('');
  lines.push('## 一、状态评分');
  const summary = pick(report, 'indicators.summary', null) || {};
  const indicatorKeys = Object.keys(summary).length ? Object.keys(summary) : [];
  if (indicatorKeys.length) {
    lines.push('');
    lines.push('| 指标 | 均值 | 标准差 | 有效窗数 |');
    lines.push('| --- | --- | --- | --- |');
    for (const key of indicatorKeys) {
      const stat = summary[key] || {};
      lines.push(`| ${cell(INDICATOR_LABELS[key] || key)} | ${cell(fmtNum(pick(stat, 'mean'), 3))} | ${cell(fmtNum(pick(stat, 'std'), 3))} | ${cell(fmtInt(pick(stat, 'n')))} |`);
    }
  } else {
    lines.push('- 无可用指标（见采集质量）。');
  }
  const bandZ = pick(report, 'indicators.band_z', null);
  if (bandZ && Object.keys(bandZ).length) {
    lines.push('');
    lines.push(`- 相对基线的频带 z 值：${Object.entries(bandZ).map(([key, value]) => `${key} ${value >= 0 ? '+' : ''}${fmtNum(value, 2)}`).join('、')}`);
  }

  /* 二、采集质量 */
  lines.push('');
  lines.push('## 二、采集质量');
  const quality = pick(report, 'quality', null) || {};
  lines.push(`- 4 秒窗 ${cell(fmtInt(pick(quality, 'windows_usable')))}/${cell(fmtInt(pick(quality, 'windows_total')))} 可用（可用窗比例 ${cell(fmtPercent(pick(quality, 'valid_ratio')))}）`);
  if (quality.unusable_reasons && Object.keys(quality.unusable_reasons).length) {
    lines.push(`- 排除原因：${Object.entries(quality.unusable_reasons).map(([reason, count]) => `${reason}×${count}`).join('、')}`);
  }
  // 真实设备 + 快速演示：相邻窗读到几乎同一段缓冲（实测质检阶段 5 个窗指标逐字节相同），
  // 窗计数不是独立样本。仿真源每窗重新生成数据，不受影响。与后端 report.to_markdown() 同一口径。
  const extras = pick(report, 'extras', null) || {};
  if (String(pick(extras, 'source_kind')) === 'lsl' && Number(pick(extras, 'time_scale') || 1) < 0.2) {
    lines.push('- 采集口径提醒：真实设备 + 快速演示模式（time_scale < 0.2）下相邻 4 秒窗读到的是几乎同一段缓冲数据，上面的可用窗计数是**高重叠样本**，只能用于链路自检；真实节奏采集请用 time_scale = 1.0（此时量表与按键任务由本人操作）。');
  }

  /* 三、量表 */
  lines.push('');
  lines.push('## 三、量表结果');
  const scales = pick(report, 'scales', null) || {};
  if (Object.keys(scales).length) {
    for (const [code, result] of Object.entries(scales)) {
      lines.push(`- ${cell(pick(result, 'name', code))}（${cell(code)}）：粗分 ${cell(fmtInt(pick(result, 'raw_score')))}，标准分 ${cell(fmtInt(pick(result, 'standard_score')))}，${cell(pick(result, 'level'))}；版本 ${cell(pick(result, 'version'))}`);
    }
  } else {
    lines.push('- 未采集。');
  }

  /* 四、行为任务 */
  lines.push('');
  lines.push('## 四、行为任务');
  const behavior = pick(report, 'behavior', null) || {};
  if (Object.keys(behavior).length) {
    for (const [name, result] of Object.entries(behavior)) {
      if (name === 'sart') {
        lines.push(`- SART：${cell(fmtInt(pick(result, 'trials')))} 试次（No-Go ${cell(fmtInt(pick(result, 'nogo_trials')))}），正确率 ${cell(fmtPercent(pick(result, 'go_accuracy')))}，虚报率 ${cell(fmtPercent(pick(result, 'commission_rate')))}，漏报率 ${cell(fmtPercent(pick(result, 'omission_rate')))}，反应时 ${cell(fmtNum(pick(result, 'rt_mean'), 3))} ± ${cell(fmtNum(pick(result, 'rt_sd'), 3))} s`);
      } else if (name === 'pvt') {
        lines.push(`- PVT-B：${cell(fmtInt(pick(result, 'trials')))} 试次，应答 ${cell(fmtInt(pick(result, 'responded')))}，漏失 ${cell(fmtInt(pick(result, 'missed')))}，中位反应时 ${cell(fmtNum(pick(result, 'rt_median'), 3))} s，慢反应率 ${cell(fmtPercent(pick(result, 'lapse_rate')))}，抢答 ${cell(fmtInt(pick(result, 'false_starts')))} 次`);
      } else {
        lines.push(`- ${cell(name)}：见结构化 JSON`);
      }
    }
  } else {
    lines.push('- 未采集。');
  }

  /* 五、联合评估 */
  const assessment = pick(report, 'assessment', null);
  lines.push('');
  lines.push('## 五、联合评估结论');
  lines.push(`- 结论：${cell(assessment ? pick(assessment, 'conclusion') : null)}`);
  if (assessment) {
    for (const [dimension, state] of Object.entries(pick(assessment, 'dimension_states', {}) || {})) {
      lines.push(`- ${cell(DIMENSION_LABELS[dimension] || dimension)}：${cell(pick(state, 'state'))}（依据 ${cell(list(pick(state, 'evidence_codes', [])).join('、') || '无')}）`);
    }
    lines.push(`- 一致性：脑电 vs 量表 ${cell(pick(assessment, 'consistency.eeg_vs_scale'))}；脑电 vs 行为 ${cell(pick(assessment, 'consistency.eeg_vs_behavior'))}`);

    lines.push('');
    lines.push('## 六、改善建议');
    const advice = list(pick(assessment, 'advice', []));
    if (advice.length) for (const item of advice) lines.push(`- ${cell(item)}`);
    else lines.push('- 暂无建议。');

    lines.push('');
    lines.push('## 七、证据回填');
    const evidence = list(pick(assessment, 'evidence', []));
    if (evidence.length) {
      lines.push('');
      lines.push('| 证据 | 来源 | 维度 | 可用 | 摘要 | 取值 | 方向 |');
      lines.push('| --- | --- | --- | --- | --- | --- | --- |');
      for (const item of evidence) {
        lines.push(`| ${cell(pick(item, 'code'))} | ${cell(pick(item, 'source'))} | ${cell(DIMENSION_LABELS[pick(item, 'dimension')] || pick(item, 'dimension'))} | ${cell(pick(item, 'available') ? '可用' : '不可用')} | ${cell(pick(item, 'summary'))} | ${cell(fmtNum(pick(item, 'value'), 3))} | ${cell(pick(item, 'direction'))} |`);
      }
    } else {
      lines.push('- 无证据条目。');
    }

    lines.push('');
    lines.push(`> 边界说明：${cell(pick(assessment, 'boundary'))}`);
  }

  /* 边界声明：即使评估缺失也要写出来，避免结论被误读 */
  const boundary = pick(config, 'privacy.boundary', null);
  if (boundary) {
    lines.push('');
    lines.push(`> ${cell(boundary)}`);
  }
  return lines.join('\n');
}

/** 评估未完成时的进度展示。ctx 用于"刷新报告"——重建当前视图即可重新请求，不再整页刷新。 */
function renderPartial(host, data, ctx) {
  const bar = el('div', { class: 'progress' }, [
    el('div', {
      class: 'progress__bar',
      style: `width:${(Math.max(0, Math.min(1, Number(pick(data, 'progress', 0)) || 0)) * 100).toFixed(1)}%`,
    }),
  ]);
  host.append(card('报告尚未生成', el('div', { class: 'stack' }, [
    el('p', { text: `报告将在"报告与产物"阶段落盘。当前状态：${statusText(pick(data, 'status', null))}，阶段 ${pick(data, 'phase', DASH)}。` }),
    el('div', { class: 'row row--between' }, [
      el('span', { class: 'muted', text: '阶段进度' }),
      el('span', { class: 'mono', text: fmtPercent(pick(data, 'progress')) }),
    ]),
    bar,
    button('刷新报告', () => (ctx && ctx.reload ? ctx.reload() : window.location.reload())),
  ]), { sub: '接口返回 202 + partial: true 属于正常状态，不是错误' }));

  const runs = list(pick(data, 'runs', []));
  if (runs.length) {
    host.append(card('阶段执行情况', table([
      { title: '阶段', render: (row) => pick(row, 'phase') },
      { title: '状态', render: (row) => (row && row.status === 'done' ? '完成' : pick(row, 'status')) },
      { title: '错误', render: (row) => pick(row, 'error') },
    ], runs)));
  }

  const indicators = pick(data, 'indicator_summary', null);
  if (indicators && Object.keys(indicators).length) {
    host.append(card('已产出的指标汇总', table([
      { title: '指标', render: (row) => INDICATOR_LABELS[row[0]] || row[0] },
      { title: '均值', align: 'right', render: (row) => fmtNum(pick(row[1], 'mean'), 3) },
      { title: '标准差', align: 'right', render: (row) => fmtNum(pick(row[1], 'std'), 3) },
      { title: 'n', align: 'right', render: (row) => fmtInt(pick(row[1], 'n')) },
    ], Object.entries(indicators))));
  }

  const alerts = list(pick(data, 'alerts', []));
  if (alerts.length) {
    host.append(card(`预警记录（${fmtInt(alerts.length)} 条）`, table([
      { title: '时间', render: (row) => `${fmtNum(pick(row, 't_sec'), 1)} s` },
      { title: '类型', render: (row) => pick(row, 'kind') },
      { title: '状态', render: (row) => pick(row, 'state') },
      { title: '说明', render: (row) => pick(row, 'message') },
    ], alerts.slice(-20))));
  }
}

export async function render(container, ctx) {
  const uuid = ctx.params.get('session') || pick(ctx.store.state.currentSession, 'uuid', null);
  const actions = uuid ? [
    button('流程视图', () => ctx.navigate(`#/flow?session=${uuid}`)),
    button('实时监测', () => ctx.navigate(`#/live?session=${uuid}`)),
    el('a', { href: api.exportZipUrl(uuid), text: '导出 zip' }),
  ] : [];
  const host = pageFrame(container, '评估报告', actions);

  if (!uuid) {
    host.append(card('尚未选择会话', el('div', {}, [
      empty('请先从历史会话中选择一次已完成（或进行中）的会话。'),
      el('div', { class: 'row', style: 'margin-top:12px' }, [
        button('前往历史会话', () => ctx.navigate('#/history'), { primary: true }),
        button('前往被试管理', () => ctx.navigate('#/subjects')),
      ]),
    ])));
    return;
  }

  const config = ctx.store.state.config || {};

  // 直接把 uuid 贴进地址栏（#/report?session=…）时也要把会话与 runtime 写回 store：
  // 顶栏的「会话 / 数据来源」读的是 store.currentSession 与 store.sessionRuntime，
  // 只在流程/实时/训练/被试视图里被写过，报告页此前从不写，于是从历史台账点进来会显示
  // 「会话：未选择 / 数据来源：—」，与页面上正在展示的这份报告自相矛盾。
  // 写法与 training.js 的回填保持一致（runtime 取自详情接口的 runtime.source_kind）。
  api.getSession(uuid).then((response) => {
    if (ctx.signal.aborted) return;
    const data = response.data || {};
    const session = data.session ? { ...data.session } : { ...data };
    session.uuid = uuid;
    ctx.store.setState({ currentSession: session, sessionRuntime: pick(data, 'runtime', null) });
  }).catch(() => {});

  /* --------------------------------------------------------- 报告内容 */
  try {
    const response = await api.report(uuid);
    if (ctx.signal.aborted) return;
    const data = response.data;
    if (response.status === 202 || (data && data.partial === true)) {
      renderPartial(host, data || {}, ctx);
    } else if (typeof data === 'string') {
      host.append(card('评估报告', renderMarkdown(data)));
    } else {
      const markdown = composeMarkdown(data || {}, config);
      host.append(card('评估报告', renderMarkdown(markdown), {
        sub: `被试 sub-${pick(data, 'participant', DASH)}`,
      }));

      // 建议与边界单独拆出来，便于快速阅读（原文仍在 Markdown 里）
      const advice = list(pick(data, 'assessment.advice', []));
      if (advice.length) {
        host.append(card('改善建议', el('ul', {}, advice.map((item) => el('li', { text: String(item) })))));
      }
      const boundary = pick(data, 'assessment.boundary', null) || pick(config, 'privacy.boundary', null);
      if (boundary) {
        host.append(card('边界声明', [el('blockquote', { text: String(boundary) })], {
          sub: '结论仅用于研究与自我调节训练，不构成医疗诊断',
        }));
      }
      const privacySubject = pick(config, 'privacy.subject_id', null);
      if (privacySubject) host.append(el('p', { class: 'muted', text: privacySubject }));
    }
  } catch (error) {
    if (error && error.name === 'ApiError' && (error.status === 404 || error.status === 409)) {
      host.append(card('报告尚未生成', [empty(`${describeError(error)}（会话可能尚未执行到报告阶段）`)]));
    } else {
      host.append(card('报告加载失败', [empty(describeError(error))]));
      toast(describeError(error));
    }
  }

  /* --------------------------------------------------------- 产物列表 */
  const artifactHost = el('div');
  host.append(card('产物下载', artifactHost));
  try {
    const response = await api.artifacts(uuid);
    if (ctx.signal.aborted) return;
    const items = list(pick(response.data, 'items', []));
    artifactHost.textContent = '';
    if (!items.length) {
      artifactHost.append(empty('该会话还没有产物（报告 / 热力图 / 趋势 / 模型文件等）。'));
    } else {
      artifactHost.append(table([
        { title: '类型', render: (row) => pick(row, 'kind') },
        { title: '大小', align: 'right', render: (row) => fmtBytes(pick(row, 'bytes')) },
        { title: 'sha256', render: (row) => el('span', { class: 'mono', text: String(pick(row, 'sha256', DASH)).slice(0, 16) + '…', title: pick(row, 'sha256', '') }) },
        { title: '文件存在', render: (row) => (pick(row, 'exists') ? '是' : '否') },
        {
          title: '下载',
          render: (row) => (pick(row, 'exists')
            ? el('a', { href: pick(row, 'download', api.artifactUrl(uuid, pick(row, 'kind'))), text: '下载' })
            : DASH),
        },
      ], items));
    }
    artifactHost.append(el('div', { class: 'row', style: 'margin-top:12px' }, [
      el('a', { href: api.exportZipUrl(uuid), text: '打包下载全部产物 + manifest.json（含 sha256）' }),
    ]));
  } catch (error) {
    artifactHost.textContent = '';
    artifactHost.append(empty(`产物列表加载失败：${describeError(error)}`));
  }
}

export default render;
