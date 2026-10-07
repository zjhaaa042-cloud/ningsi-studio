/**
 * 运维面板视图：模型训练（#/models）与设备状态（#/devices）。
 *
 * 两个面板都只读接口、只写自己的 DOM：
 * - POST /api/models/train   训练基线分类器（routes.py:941；body {subjects, windows_per_state}）
 * - GET  /api/devices/status 运行中会话的设备健康状况（routes.py:249）
 * - GET  /api/devices        可用数据源探测（routes.py:243，?probe=秒）
 *
 * 约束（task-4）：
 * - 接口报错/字段缺失/401/404 一律给空态卡，不允许白屏；
 * - 不做「选会话 / 选被试训练」——后端只用内置仿真被试（sim00 / sim01…），界面必须如实说明；
 * - 缺失字段用 DASH（—）占位，不能出现 undefined/NaN。
 */

import { api } from '../api.js';
import { describeError } from '../store.js';
import { renderPreviewCard } from '../preview.js';
import {
  DASH, button, card, el, empty, field, fmtInt, fmtNum, fmtPercent, fmtSeconds,
  list, pageFrame, pick, table,
} from '../util.js';

/* ============================================================ 模型训练面板 */

/** 视图内状态：上次训练的参数与结果（刷新页面即丢，后端没有 GET /api/models）。 */
const modelView = { subjects: 6, windowsPerState: 8, running: false, result: null, error: null };

/** 三份划分（train/validation/test）的指标表。 */
function metricTable(title, metrics) {
  if (!metrics) return el('p', { class: 'muted', text: `${title}：${DASH}（接口未返回该划分）` });
  const confusion = pick(metrics, 'confusion', {}) || {};
  // 注意：table() 的列必须给 field 或 render，否则单元格一律渲染 DASH。
  return table([
    { title: '划分', field: 'split' },
    { title: '样本 n', field: 'n', align: 'right' },
    { title: '准确率', field: 'accuracy', align: 'right' },
    { title: '敏感度', field: 'sensitivity', align: 'right' },
    { title: '特异度', field: 'specificity', align: 'right' },
    { title: 'AUC', field: 'auc', align: 'right' },
    { title: 'TP / TN / FP / FN', field: 'confusion', align: 'right' },
  ], [{
    split: title,
    n: fmtInt(pick(metrics, 'n', null)),
    accuracy: fmtPercent(pick(metrics, 'accuracy', null), 1),
    sensitivity: fmtPercent(pick(metrics, 'sensitivity', null), 1),
    specificity: fmtPercent(pick(metrics, 'specificity', null), 1),
    auc: fmtNum(pick(metrics, 'auc', null), 3),
    confusion: [fmtInt(confusion.tp), fmtInt(confusion.tn), fmtInt(confusion.fp), fmtInt(confusion.fn)].join(' / '),
  }]);
}

/** 训练结果卡：接口字段缺失时逐项降级为 —，而不是抛错。 */
function renderModelResult(result) {
  if (!result || typeof result !== 'object') {
    return card('训练结果', [
      empty('接口没有返回训练结果（响应为空）。'),
    ], { sub: '模型训练由后端完成；返回为空时这里只显示空态，不会伪造指标。' });
  }
  const features = list(pick(result, 'features', []));
  const split = pick(result, 'subject_split', {}) || {};
  const participants = list(pick(result, 'participants', []));
  const body = [
    el('div', { class: 'grid grid--2' }, [
      el('div', { class: 'path-cell' }, [
        el('p', { class: 'muted', text: '模型规格' }),
        el('p', { class: 'mono path-wrap', text: [
          pick(result, 'spec', DASH),
          pick(result, 'version', null) ? `版本 ${pick(result, 'version', '')}` : null,
          `样本 ${fmtInt(pick(result, 'samples', null))}`,
          `被试 ${participants.length ? participants.join('、') : DASH}`,
        ].filter(Boolean).join('｜') }),
      ]),
      el('div', { class: 'path-cell' }, [
        el('p', { class: 'muted', text: '被试划分（train / validation / test）' }),
        el('p', { class: 'mono path-wrap', text: [split.train, split.validation, split.test]
          .map((group) => (list(group).length ? list(group).join('、') : DASH)).join('｜') }),
      ]),
    ]),
    // 绝对 --data 下 model_path 会带上完整盘符路径（无空格，普通换行断不开）：
    // path-wrap = overflow-wrap:anywhere + word-break:break-all，见 app.css 的 task-9 小节
    el('p', { class: 'muted path-wrap', text: `模型文件：${pick(result, 'model_path', DASH)}` }),
    el('p', { class: 'muted path-wrap', text: `特征（${features.length} 个）：${features.length ? features.join('、') : DASH}` }),
    metricTable('train', pick(result, 'train', null)),
    metricTable('validation', pick(result, 'validation', null)),
    metricTable('test', pick(result, 'test', null)),
  ];
  return card('训练结果', body, {
    sub: '指标来自后端返回原文；validation 为 null 表示该划分没有足够被试（不是 0）。',
  });
}

export const models = {
  async render(container, ctx) {
    const host = pageFrame(container, '模型训练', [
      button('刷新', () => ctx.reload()),
    ]);

    const subjectsInput = el('input', {
      id: 'models-subjects', type: 'number', min: 2, max: 40,
      value: String(modelView.subjects),
    });
    const windowsInput = el('input', {
      id: 'models-windows', type: 'number', min: 1, max: 200,
      value: String(modelView.windowsPerState),
    });
    const resultHost = el('div');
    const errorHost = el('div');
    const statusHost = el('p', { class: 'muted' });

    const runButton = button('开始训练', async () => {
      const subjects = Number(subjectsInput.value);
      const windows = Number(windowsInput.value);
      modelView.subjects = subjects;
      modelView.windowsPerState = windows;
      runButton.disabled = true;
      runButton.textContent = '训练中…';
      statusHost.textContent = '训练中…（后端用内置仿真被试现算，通常几秒内返回）';
      errorHost.textContent = '';
      try {
        const response = await api.trainModel({ subjects, windows_per_state: windows });
        if (ctx.signal.aborted) return;
        modelView.result = response.data || null;
        modelView.error = null;
      } catch (error) {
        if (ctx.signal.aborted) return;
        modelView.result = null;
        modelView.error = error;
      } finally {
        runButton.disabled = false;
        runButton.textContent = '开始训练';
      }
      if (ctx.signal.aborted) return;
      renderResult();
    }, { primary: true, title: '在本机训练一个基线分类模型' });

    function renderResult() {
      resultHost.textContent = '';
      errorHost.textContent = '';
      modelView.running = false;
      if (modelView.error) {
        const error = modelView.error;
        const status = pick(error, 'status', null);
        statusHost.textContent = '';
        if (status === 401 || status === 404) {
          errorHost.append(card('无法训练模型', [
            empty(status === 401
              ? '没有权限调用训练接口（HTTP 401）。请在启动服务时配置访问令牌后重试。'
              : '服务器上没有这个接口（HTTP 404）。可能后端版本较旧，或该接口被关闭。'),
          ], { sub: '接口不可用时只显示空态，不会伪造训练结果。' }));
        } else {
          const detail = pick(error, 'detail', null) || pick(error, 'payload', null);
          errorHost.append(card('训练失败', [
            empty(`模型训练失败：${describeError(error)}`),
            detail ? el('p', { class: 'muted', text: typeof detail === 'string' ? detail : JSON.stringify(detail) }) : null,
          ], { sub: '参数需满足 subjects ∈ [2, 40]、windows_per_state ∈ [1, 200]，否则后端返回 422。' }));
        }
        return;
      }
      if (!modelView.result) {
        statusHost.textContent = '';
        resultHost.append(renderModelResult(null));
        return;
      }
      const result = modelView.result;
      statusHost.textContent = `最近一次训练：样本 ${fmtInt(pick(result, 'samples', null))}，被试 ${list(pick(result, 'participants', [])).join('、') || DASH}`;
      // 结果卡渲染也做保护：接口字段异常时给空态，而不是让整个面板消失
      try {
        resultHost.append(renderModelResult(result));
      } catch (error) {
        console.error('[panels] 渲染训练结果失败', error);
        resultHost.append(card('训练结果', [
          empty(`训练已返回，但结果渲染失败：${describeError(error)}`),
        ]));
      }
    }

    // 「这是什么」：作者本人都会问"模型训练是什么"，说明页面缺一段解释。
    host.append(card('这是什么 / 用什么数据', el('div', { class: 'stack' }, [
      el('p', { text: '用 6 个频带特征（θ/α/β 相对功率 + 三个比值）训练一个可解释的逻辑回归，'
        + '把每个 4 秒窗分成「专注」与「困倦」两类。用途：验证"频带特征能不能把这两种状态分开"，'
        + '并给整条链路一个可复现、可版本化的算法基线（模型就是一份 JSON 权重）。' }),
      el('p', { class: 'muted', text: '数据来源（重要）：训练数据来自后端内置仿真被试 sim00、sim01…，'
        + '不是你的真实被试数据。所以下面那些准确率 / AUC 是「算法基线 + 链路自检」的读数，'
        + '不能当成对真实被试的结论。' }),
      el('ul', { class: 'wizard__plan' }, [
        el('li', { text: '被试数 = 生成多少个仿真被试；每状态窗口数 = 每位仿真被试在每个状态取多少窗'
          + '（窗长 4 s / 步长 2 s，与报告同口径）。缺省 6 × 2 状态 × 8 窗 = 96 个样本。' }),
        el('li', { text: '三组划分按被试切分 train / validation / test = 6:2:2，同一被试的窗不会跨集合'
          + '（避免信息泄漏）；某一组显示 — 表示被试太少、组不出该集合，不是 0。' }),
        el('li', { text: '指标：准确率 / 灵敏度 / 特异度 / AUC + 混淆矩阵；AUC 0.5 就等于随机猜。' }),
        el('li', { text: '产物：模型权重（含标准化参数）存成 JSON —— 本页训练写到 var/studio/models/classifier.json；'
          + '会话流程第 10 阶段也会自动训练一次，模型进该次会话的产物目录，报告里能看到模型文件与 sha256。' }),
        el('li', { text: '边界：这是研究 / 自我调节训练用途的基线，不是诊断模型；'
          + '用你自己的真实数据训练目前没有实现入口（接口与本页都只用内置仿真被试）。' }),
      ]),
    ]), { sub: '一句话：把"频带特征能否区分专注/困倦"做成可复现的基线模型' }));

    host.append(card('训练参数', [
      el('div', { class: 'row' }, [
        field('仿真被试数（2–40）', subjectsInput, '生成多少个内置仿真被试参与训练'),
        field('每状态窗口数（1–200）', windowsInput, '每位仿真被试在每个状态取多少窗'),
        runButton,
      ]),
      statusHost,
      el('p', { class: 'muted', text: '本页与「会话流程」第 10 阶段都只用后端内置仿真被试（sim00、sim01…），'
        + '接口不接受「选会话 / 选被试」。真实数据训练当前没有实现入口（命令行也没有 train 子命令）——'
        + '这一点如实写在界面上，不做"看起来能用"的暗示。' }),
      errorHost,
    ], {
      sub: '在本机训练，不影响已有会话数据',
    }));

    host.append(resultHost);
    renderResult();
  },
};

/* ============================================================ 设备状态面板 */

/** 来源类别 → 中文（缺失时用 —，不显示 undefined）。 */
function kindText(kind) {
  const raw = String(kind || '').toLowerCase();
  if (raw === 'sim') return '仿真源';
  if (raw === 'lsl') return '实时设备（LSL）';
  if (!raw) return DASH;
  return raw;
}

function renderSources(sources, sourcesError, conditionNote) {
  const rows = list(sources);
  if (sourcesError) {
    return card('可用数据源', [
      empty(`探测数据源失败：${sourcesError}`),
    ], { sub: '探测失败时只显示空态，不影响已有会话' });
  }
  if (!rows.length) {
    return card('可用数据源', [
      empty('没有探测到可用数据源。若需要真实设备，请先启动采集端（如 BioMultiLite / BSense-R）或运行 python -m ningsi_studio simulate-outlet。'),
    ], { sub: '未探测到数据源' });
  }
  return card('可用数据源', [
    table([
      { title: '键', field: 'key' },
      { title: '类别', field: 'kind' },
      { title: '设备', field: 'device' },
      { title: '采样率', field: 'srate', align: 'right' },
      { title: '通道', field: 'channels', align: 'right' },
      { title: '实时硬件', field: 'real' },
      { title: '说明', field: 'note' },
    ], rows.map((row) => ({
      key: pick(row, 'key', DASH),
      kind: kindText(pick(row, 'kind', null)),
      device: pick(row, 'device', DASH),
      srate: fmtNum(pick(row, 'srate', null), 1),
      channels: fmtInt(pick(row, 'channels', null)),
      real: pick(row, 'real', null) === true ? '是' : (pick(row, 'real', null) === false ? '否（仿真）' : DASH),
      note: pick(row, 'note', DASH),
    })), { caption: conditionNote || '真实设备未启动时只有仿真源可选（仿真仅用于演示与自测）' }),
  ], { sub: '「实时硬件」列标出是否为真机；仿真源仅用于演示与自测', class: 'card--scroll' });
}

function renderDeviceHealth(devices, hardwareNote) {
  const rows = list(devices);
  if (!rows.length) {
    return card('运行中会话的设备健康状况', [
      empty('当前没有正在运行的会话占用信号源；启动一次实时会话后这里会显示是否在收数、实测采样率与流错误。'),
      hardwareNote ? el('p', { class: 'muted', text: hardwareNote }) : null,
    ], { sub: '只显示正在运行的会话' });
  }
  return card('运行中会话的设备健康状况', [
    table([
      { title: '会话', field: 'session' },
      { title: '来源键', field: 'key' },
      { title: '类别', field: 'kind' },
      { title: '在收数', field: 'live' },
      { title: '实测采样率', field: 'observed_srate', align: 'right' },
      { title: '距上次数据', field: 'since', align: 'right' },
      { title: '缓冲样本', field: 'buffered', align: 'right' },
      { title: '累计样本', field: 'total', align: 'right' },
      { title: '流错误', field: 'errors', align: 'right' },
    ], rows.map((row) => ({
      session: String(pick(row, 'session', DASH)).slice(0, 8),
      key: pick(row, 'key', DASH),
      kind: kindText(pick(row, 'kind', null)),
      live: pick(row, 'live', null) === null || pick(row, 'live', null) === undefined
        ? DASH : (pick(row, 'live', false) ? '是' : '否'),
      observed_srate: fmtNum(pick(row, 'observed_srate', null), 1),
      since: fmtSeconds(pick(row, 'seconds_since_last', null)),
      buffered: fmtInt(pick(row, 'buffered_samples', null)),
      total: fmtInt(pick(row, 'total_samples', null)),
      errors: fmtInt(pick(row, 'stream_errors', null)),
    })), {
      caption: '「在收数 / 实测采样率」缺失显示 —：仿真源没有硬件，不给实测值',
    }),
  ], { sub: '用于现场判断「设备掉了」还是「信号正常」。' });
}

export const devices = {
  async render(container, ctx) {
    const host = pageFrame(container, '设备状态', [
      button('立即刷新', () => ctx.reload()),
    ]);

    const statusHost = el('p', { class: 'muted', text: '正在读取设备状态…' });
    const bodyHost = el('div');
    host.append(statusHost, bodyHost);
    // 实时预览放在最前：现场第一步就是"现在到底有没有信号、波形正不正常"，
    // 不需要先建会话（2026-10-07 需求）。
    const previewHost = el('div');
    host.append(previewHost);
    renderPreviewCard(ctx, previewHost);

    async function loadDeviceStatus(showErrors = true) {
      try {
        const response = await api.deviceStatus();
        if (ctx.signal.aborted) return;
        const data = response.data || {};
        const devices = list(pick(data, 'devices', []));
        const sources = list(pick(data, 'sources', []));
        const conditionNote = pick(data, 'condition_note', null);
        const active = list(pick(data, 'active_sessions', []));
        statusHost.textContent = `运行中会话 ${active.length} 个｜可用数据源 ${sources.length} 个｜更新时间 ${new Date().toLocaleTimeString('zh-CN')}`;
        bodyHost.textContent = '';
        bodyHost.append(
          renderSources(pick(data, 'sources', []), pick(data, 'sources_error', null), conditionNote),
          renderDeviceHealth(devices, pick(sources.find((row) => pick(row, 'hardware_note', null)) || {}, 'hardware_note', null)),
        );
      } catch (error) {
        if (ctx.signal.aborted) return;
        const status = pick(error, 'status', null);
        if (status === 401 || status === 404) {
          statusHost.textContent = '';
          bodyHost.textContent = '';
          bodyHost.append(card('无法读取设备状态', [
            empty(status === 401
              ? '没有权限读取设备状态（HTTP 401）。请在启动服务时配置访问令牌后重试。'
              : '服务器上没有这个接口（HTTP 404）。可能后端版本较旧，或该接口被关闭。'),
          ], { sub: '接口不可用时只显示空态，不会白屏。' }));
          if (timer) { clearInterval(timer); timer = null; }
          return;
        }
        if (!showErrors) return;
        statusHost.textContent = '';
        bodyHost.textContent = '';
        bodyHost.append(card('设备状态读取失败', [
          empty(`设备状态读取失败：${describeError(error)}`),
        ]));
      }
    }

    // 自动轮询：10 秒一次，页面不可见时跳过（避免后台无意义请求）。
    // 定时器句柄只在 ctx.onCleanup 里清理，不改动 main.js 的既有清理逻辑。
    let timer = null;
    const tick = () => {
      if (document.hidden) return;
      loadDeviceStatus(false);
    };
    timer = setInterval(tick, 10000);
    ctx.onCleanup(() => {
      if (timer) { clearInterval(timer); timer = null; }
    });

    await loadDeviceStatus(true);
  },
};
