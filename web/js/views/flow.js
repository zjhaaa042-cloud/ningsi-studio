/**
 * 会话流程视图：11 阶段向导 + 量表作答 + SART / PVT 按键页。
 *
 * 交互约定（严格按 docs/API.md 与 core/runtime.py 的事件语义）：
 * - `phase`    → 阶段进度（state: running / done）
 * - `scale_request` → GET /api/scales/{code} 取题干，1–4 单选，POST 提交
 * - `behavior_request` + `trial` → 空格键作答，rt = performance.now() 差值（秒）
 *   注意：是否"该按"由服务端判定，前端只上报 responded / rt，绝不自行判 No-Go
 * - `phase.done` / `finished` → 结束当前交互，自动前进
 * - 快速模式（started.auto === true）后端已自行作答，前端只做展示、不再 POST
 *
 * 引导设计参考了参考工程（bsense-suite）的协议执行界面：每个阶段都给出
 * **被试动作指令**（headline）+ **要点**（details）+ **预计时长** + **推进方式**，
 * 页面顶部常驻"当前该做什么"面板，避免被试/操作者只能猜下一步。
 */

import { api } from '../api.js';
import { describeError, toast } from '../store.js';
import { connectSessionEvents } from '../sse.js';
import {
  DASH, button, card, el, empty, fmtDuration, fmtInt, fmtNum, fmtPercent, fmtSeconds,
  fmtTime, list, phaseStateText, phaseText, pick, statusText, table,
} from '../util.js';
import { drawAlertTimeline, drawWaveform } from '../charts.js';

/** 阶段清单兜底：started 事件没带 phases 时使用（字段与后端 phases.as_list() 对齐）。 */
const PHASE_FALLBACK = [
  { key: 'qc', label: '设备质检', headline: '先坐好别动，正在检查信号质量', duration_sec: 24 },
  { key: 'baseline_open', label: '睁眼基线', headline: '保持睁眼看着前方，安静坐 2 分钟', duration_sec: 120 },
  { key: 'baseline_closed', label: '闭眼基线', headline: '闭上眼睛，安静坐 2 分钟', duration_sec: 120 },
  { key: 'scales', label: '量表填写', headline: '按最近一周的实际感受，逐题作答', duration_sec: 300, advance: 'subject' },
  { key: 'sart', label: 'SART 持续注意', headline: '看到数字就按空格，看到“3”不要按', duration_sec: 300, advance: 'subject' },
  { key: 'pvt', label: 'PVT-B 警觉度', headline: '出现红点就尽快按空格', duration_sec: 180, advance: 'subject' },
  { key: 'monitor', label: '任务态监测', headline: '稍等，正在模拟三种状态并逐窗算指标', duration_sec: 70 },
  { key: 'training', label: '神经反馈训练', headline: '尽量让专注度稳定在目标线以上', duration_sec: 240 },
  { key: 'assessment', label: '联合评估', headline: '正在汇总脑电、量表与行为三类证据', duration_sec: 5 },
  { key: 'model', label: '模型训练', headline: '正在训练个体化分类模型', duration_sec: 5 },
  { key: 'report', label: '报告与产物', headline: '正在生成报告与可下载产物', duration_sec: 5 },
];

const INTERACTIVE_KEY = { scales: '量表作答', sart: 'SART 按键任务', pvt: 'PVT 按键任务' };

/** 推进方式 → 面板上的一行说明（与参考工程 timed / operator / form 语义一致）。 */
const ADVANCE_TEXT = {
  auto: '本阶段自动推进，不需要你操作',
  subject: '本阶段需要你作答',
  operator: '本阶段需要操作者确认',
};

/**
 * 试次事件到刺激真正呈现之间的服务端延时（秒）。
 * 服务端在 publish("trial") 之后才 sleep 再取下一窗刺激（见 core/runtime.py 的 _run_sart/_run_pvt），
 * 因此前端测到的 elapsed 里包含这段延时；上报 rt 时必须扣掉，否则会系统性偏长。
 * PVT 的 onset 是绝对时刻，按 onset 差值计算更准，故单独处理。
 */
const TRIAL_START_DELAY = { sart_main: 2.2, sart_practice: 1.6 };

export function render(container, ctx) {
  container.textContent = '';
  const uuid = ctx.params.get('session') || pick(ctx.store.state.currentSession, 'uuid', null)
    || pick(ctx.store.state.currentSession, 'session.uuid', null);

  if (!uuid) {
    container.append(el('h1', { text: '会话流程' }));
    container.append(card('尚未选择会话', el('div', {}, [
      empty('先到“被试管理”页面：新建一个被试（编号留空会自动生成），再点“开始新会话”。'),
      el('div', { class: 'row', style: 'margin-top:12px' }, [
        button('去被试管理 / 开始新会话', () => ctx.navigate('#/subjects'), { primary: true }),
        button('查看历史会话', () => ctx.navigate('#/history')),
      ]),
    ])));
    return;
  }

  /* ------------------------------------------------------------ 页面骨架 */
  const head = el('div', { class: 'row row--between' }, [
    el('h1', { text: '会话流程' }),
    el('div', { class: 'row' }, [
      el('span', { class: 'badge', text: `会话 ${uuid.slice(0, 8)}…` }),
      button('实时监测', () => ctx.navigate(`#/live?session=${uuid}`)),
      button('报告', () => ctx.navigate(`#/report?session=${uuid}`)),
      button('取消会话', async (event) => {
        const node = event.currentTarget;
        node.disabled = true;
        try {
          await api.cancelSession(uuid);
          toast('已请求取消本次会话', 'info');
        } catch (error) {
          toast(describeError(error));
        } finally {
          node.disabled = false;
        }
      }),
    ]),
  ]);

  const progressHost = el('div');
  const stepHost = el('div');
  const noticeHost = el('div');
  const phaseHost = el('div');
  const interactionHost = el('div');
  const scaleSummaryHost = el('div');
  const tailHost = el('div');

  container.append(head);
  container.append(stepHost);
  container.append(card('总体进度', progressHost));
  container.append(noticeHost);
  container.append(card('全部阶段（点击可查看该阶段说明）', phaseHost, {
    sub: '等待 / 进行中 / 完成由 SSE 的 phase 事件驱动；快速模式下交互阶段由后端自动作答',
  }));
  container.append(interactionHost);
  container.append(scaleSummaryHost);
  container.append(tailHost);

  /* ---------------------------------------------------------- 本地状态 */
  const local = {
    uuid,
    auto: false,
    phaseState: new Map(),          // key → { label, state, progress }
    phaseOrder: PHASE_FALLBACK.slice(),
    scales: new Map(),              // code → 已渲染的作答区状态
    scaleResults: new Map(),        // code → 后端计分结果（用于"量表计分汇总"卡）
    trial: null,                    // { task, phase, index, digit, total, keyReady, answer }
    finished: false,
    notices: [],
    offline: false,
    startedAt: null,                // 会话开始时刻（毫秒），用于显示已用时长
    phaseStartedAt: new Map(),      // key → 该阶段开始时刻（毫秒）
    now: Date.now(),                // 每秒刷新，驱动"已用/预计"
    expandedPhase: null,            // 被手动展开查看说明的阶段
    lastStepKey: null,              // 上一步骤，用于记录阶段开始时刻
  };

  /** 当前应该被高亮的阶段：进行中的优先，否则取最后一个已完成的下一阶段。 */
  const currentPhaseKey = () => {
    const running = local.phaseOrder.find(
      (phase) => (local.phaseState.get(phase.key) || {}).state === 'running');
    if (running) return running.key;
    let lastDone = -1;
    local.phaseOrder.forEach((phase, index) => {
      if ((local.phaseState.get(phase.key) || {}).state === 'done') lastDone = Math.max(lastDone, index);
    });
    if (lastDone >= 0 && lastDone + 1 < local.phaseOrder.length) return local.phaseOrder[lastDone + 1].key;
    return local.phaseOrder.length ? local.phaseOrder[0].key : null;
  };

  /** 把"已用 / 预计"折算成一句人话。 */
  const describeTiming = (phase, state) => {
    const expected = Number(pick(phase, 'duration_sec', 0)) || 0;
    if (!expected) return null;
    const startedAt = local.phaseStartedAt.get(phase.key);
    const elapsed = startedAt ? (local.now - startedAt) / 1000 : null;
    const scale = Number(pick(ctx.store.state.currentSession, 'time_scale', 1)) || 1;
    const shown = expected * scale;
    if (state === 'running' && elapsed !== null) {
      const left = Math.max(0, shown - elapsed);
      return `预计 ${fmtSeconds(shown, 0)}｜已用 ${fmtSeconds(elapsed, 0)}｜约剩 ${fmtSeconds(left, 0)}`;
    }
    const suffix = scale < 0.2 ? `（快速演示约 ${fmtSeconds(shown, 0)}）` : '';
    return `预计 ${fmtSeconds(expected, 0)}${suffix}`;
  };

  /** 统一刷新：步骤面板 + 总进度 + 阶段清单（任何状态变化都走这里）。 */
  const refresh = () => {
    renderProgress();
    renderPhases();
    renderStep();
  };

  /* ------------------------------------------------------ 当前该做什么 */
  const renderStep = () => {
    stepHost.textContent = '';
    const key = currentPhaseKey();
    if (!key) return;
    const index = local.phaseOrder.findIndex((phase) => phase.key === key);
    const phase = local.phaseOrder[index] || { key, label: key };
    const state = (local.phaseState.get(key) || {}).state || 'pending';
    // 第一次看到某阶段成为当前步骤时记下时刻，用于"已用/约剩"
    if (local.lastStepKey !== key) {
      if (!local.phaseStartedAt.has(key)) local.phaseStartedAt.set(key, Date.now());
      local.lastStepKey = key;
    }
    if (!local.startedAt) local.startedAt = Date.now();
    const stageProgress = (local.phaseState.get(key) || {}).progress;
    const interactive = pick(phase, 'interactive', false);
    const headline = pick(phase, 'headline', null) || pick(phase, 'description', null) || '本阶段进行中';
    const details = list(pick(phase, 'details', []));
    const advance = pick(phase, 'advance', 'auto');
    const isLast = index === local.phaseOrder.length - 1;
    const nextPhase = isLast ? null : local.phaseOrder[index + 1];

    const stateLabel = { pending: '即将开始', running: '进行中', done: '已完成' }[state] || state;
    const stateClass = state === 'running' ? ' step--running' : (state === 'done' ? ' step--done' : '');

    const body = [];
    body.push(el('div', { class: 'row row--between step__head' }, [
      el('div', { class: 'row', style: 'gap:10px;align-items:baseline' }, [
        el('span', { class: 'step__index mono', text: `${index + 1}/${local.phaseOrder.length}` }),
        el('span', { class: 'step__label', text: pick(phase, 'label', key) }),
        interactive ? el('span', { class: 'tag', text: INTERACTIVE_KEY[key] || '需交互' }) : null,
      ]),
      el('span', { class: 'badge' + (state === 'running' ? ' badge--strong' : ''), text: stateLabel }),
    ]));

    body.push(el('p', { class: 'step__headline', text: headline }));

    if (details.length) {
      body.push(el('ul', { class: 'step__details' },
        details.map((item) => el('li', { text: item }))));
    }

    const meta = [];
    const timing = describeTiming(phase, state);
    if (timing) meta.push(el('span', { class: 'step__meta-item mono', text: timing }));
    meta.push(el('span', { class: 'step__meta-item', text: ADVANCE_TEXT[advance] || ADVANCE_TEXT.auto }));
    if (state === 'running' && typeof stageProgress === 'number' && stageProgress !== null) {
      meta.push(el('span', { class: 'step__meta-item mono', text: `本阶段 ${fmtPercent(stageProgress)}` }));
    }
    body.push(el('div', { class: 'step__meta' }, meta));

    if (local.auto && pick(phase, 'auto_note', null)) {
      body.push(el('p', { class: 'muted step__auto', text: `快速演示：${pick(phase, 'auto_note')}` }));
    }

    if (state === 'pending' && nextPhase) {
      body.push(el('p', { class: 'muted step__next', text: `上一步完成后将进入：${pick(nextPhase, 'label', '')}` }));
    } else if (pick(phase, 'next_hint', null)) {
      body.push(el('p', { class: 'muted step__next', text: `之后：${pick(phase, 'next_hint')}` }));
    }

    // 需要被试作答的阶段：把作答区直接带到眼前，并给一句"往下看"的指引
    if (interactive && !local.auto) {
      body.push(el('div', { class: 'row step__action' }, [
        button('开始作答 / 查看题目', () => {
          const anchor = interactionHost.querySelector('.card');
          if (anchor && anchor.scrollIntoView) anchor.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }, { primary: true, small: true }),
        el('span', { class: 'muted', text: '作答区在本页下方' }),
      ]));
    }
    // 自动推进的阶段：给一个去"实时监测"看数据的入口
    if (!interactive && ['monitor', 'training', 'baseline_open', 'baseline_closed', 'qc'].includes(key)) {
      body.push(el('div', { class: 'row step__action' }, [
        button('看实时脑电波形', () => ctx.navigate(`#/live?session=${uuid}`), { small: true }),
        el('span', { class: 'muted', text: '波形与热力图在“实时监测”页' }),
      ]));
    }
    if (local.auto && interactive) {
      body.push(el('div', { class: 'row step__action' }, [
        button('看实时进度', () => ctx.navigate(`#/live?session=${uuid}`), { small: true }),
        el('span', { class: 'muted', text: '快速演示模式下不需要你作答' }),
      ]));
    }

    stepHost.append(card('当前该做什么', el('div', { class: `step${stateClass}` }, body), {
      sub: local.finished ? '会话已结束，可在“评估报告”查看结论与产物' : '按下面的提示做即可，页面会自动进入下一步',
    }));
  };

  /* -------------------------------------------------------------- 渲染 */
  const renderProgress = () => {
    progressHost.textContent = '';
    const session = ctx.store.state.currentSession;
    const progress = pick(session, 'progress', 0);
    const phase = pick(session, 'phase', null);
    // 详情接口对终态会话会把 phase_label 退化成原始键，统一交给 phaseText 处理
    const label = phaseText(phase, pick(session, 'phase_label', null));
    const bar = el('div', { class: 'progress' }, [
      el('div', { class: 'progress__bar', style: `width:${(Math.max(0, Math.min(1, Number(progress) || 0)) * 100).toFixed(1)}%` }),
    ]);
    progressHost.append(el('div', { class: 'stack' }, [
      el('div', { class: 'row row--between' }, [
        el('span', { text: `当前阶段：${label}` }),
        el('span', { class: 'mono', text: fmtPercent(progress) }),
      ]),
      bar,
      el('p', { class: 'muted', text: `状态：${statusText(pick(session, 'status', null))}｜设备：${pick(session, 'device', DASH)}｜时间倍率：${fmtNum(pick(session, 'time_scale'), 2)}｜采样率：${fmtNum(pick(session, 'srate'), 0)} Hz` }),
      local.auto ? el('p', { class: 'muted', text: '本次为快速演示模式（time_scale < 0.2）：量表与行为任务由服务端生成确定性作答。' }) : null,
      local.offline ? el('p', { class: 'muted', text: '实时事件流已断开，正在每 10 秒轮询 /api/sessions/{uuid}/live 兜底。' }) : null,
    ]));
  };

  const renderPhases = () => {
    phaseHost.textContent = '';
    const current = currentPhaseKey();
    const listNode = el('ul', { class: 'phase-list' });
    local.phaseOrder.forEach((phase, index) => {
      const state = local.phaseState.get(phase.key) || { state: 'pending', progress: null };
      const interactive = pick(phase, 'interactive', false);
      const isCurrent = phase.key === current;
      // 当前步与展开的步默认显示说明，其余折叠——避免一整屏文字让人抓不到重点
      const expanded = isCurrent || local.expandedPhase === phase.key;
      const duration = Number(pick(phase, 'duration_sec', 0)) || 0;
      const scale = Number(pick(ctx.store.state.currentSession, 'time_scale', 1)) || 1;
      const mark = state.state === 'done' ? '✓' : String(index + 1).padStart(2, '0');

      const detail = [];
      if (pick(phase, 'headline', null)) {
        detail.push(el('div', { class: 'phase-item__headline', text: pick(phase, 'headline') }));
      }
      if (expanded && pick(phase, 'description', null)) {
        detail.push(el('div', { class: 'phase-item__desc', text: pick(phase, 'description') }));
      }
      if (expanded) {
        for (const item of list(pick(phase, 'details', []))) {
          detail.push(el('div', { class: 'phase-item__bullet', text: `· ${item}` }));
        }
        if (pick(phase, 'next_hint', null)) {
          detail.push(el('div', { class: 'phase-item__next', text: `之后：${pick(phase, 'next_hint')}` }));
        }
      }

      const meta = [];
      if (duration) {
        const shown = duration * scale;
        meta.push(el('span', { class: 'muted mono', text: scale < 0.2 && scale !== 1
          ? `约 ${fmtSeconds(shown, 0)}（快速）` : `约 ${fmtSeconds(duration, 0)}` }));
      }
      if (state.progress !== null && state.progress !== undefined && state.state === 'running') {
        meta.push(el('span', { class: 'mono muted', text: fmtPercent(state.progress) }));
      }

      const item = el('li', {
        class: 'phase-item'
          + (isCurrent ? ' phase-item--current' : '')
          + (expanded ? ' phase-item--expanded' : ''),
        dataset: { state: state.state },
      }, [
        el('button', {
          class: 'phase-item__toggle',
          type: 'button',
          title: expanded ? '收起说明' : '展开该阶段说明',
          onClick: () => {
            local.expandedPhase = expanded ? null : phase.key;
            renderPhases();
          },
        }, [
          el('span', { class: 'phase-item__index mono', text: mark }),
          el('div', { class: 'phase-item__main' }, detail),
          el('div', { class: 'right' }, [
            interactive ? el('span', { class: 'tag', text: INTERACTIVE_KEY[phase.key] || '需交互' }) : null,
            el('span', { class: 'badge' + (state.state === 'running' ? ' badge--strong' : ''),
                         text: phaseStateText(state.state) }),
            ...meta,
          ]),
        ]),
      ]);
      listNode.append(item);
    });
    phaseHost.append(listNode);
  };

  const renderNotices = () => {
    noticeHost.textContent = '';
    if (!local.notices.length) return;
    noticeHost.append(card('采集提示', el('ul', { class: 'tag-list' }, local.notices.slice(-6).map((notice) => el('li', {
      class: 'tag',
      text: `[${notice.level || 'info'}] ${notice.message || ''}`,
    })))));
  };

  /* ------------------------------------------------------ 量表作答区 */
  /**
   * 量表计分结果（code → scored）。
   * 来源有二：SSE 的 `scales`（{results:[...]}，一次补齐全量表）与 `scale_scored`（单量表），
   * 已结束的会话不会再发这两类事件，所以加载时还会用 GET /report 的 `scales` 兜底。
   */
  const mergeScaleResults = (rows) => {
    for (const row of list(rows)) {
      if (!row || !row.code) continue;
      local.scaleResults.set(row.code, { ...(local.scaleResults.get(row.code) || {}), ...row });
    }
  };

  const renderScaleSummary = () => {
    scaleSummaryHost.textContent = '';
    const rows = [...local.scaleResults.values()];
    if (!rows.length) return;
    rows.sort((a, b) => String(a.code).localeCompare(String(b.code)));
    // 用紧凑的一行一量表（而不是表格）：内容不丢，但不让流程视图被这张"汇总卡"撑高
    scaleSummaryHost.append(card('量表计分汇总', el('div', { class: 'stack' }, [
      ...rows.map((row) => el('p', {
        class: 'mono',
        text: [
          `${row.code || DASH}${row.name ? `（${row.name}）` : ''}`,
          `版本 ${row.version || DASH}`,
          `原始分 ${fmtInt(row.raw_score)}`,
          `标准分 ${fmtInt(row.standard_score)}`,
          `${row.level || DASH}`,
          row.answered === undefined ? null : `已答 ${fmtInt(row.answered)} 题`,
        ].filter(Boolean).join('｜'),
      })),
      el('p', { class: 'muted', text: '量表结果仅用于研究与自我调节参考，不构成医学诊断。' }),
    ]), { sub: '计分由后端完成（来源：scale_scored / scales 事件，或已结束会话的报告接口）' }));
  };

  const ensureScaleSlot = (code, label, size, instruction) => {
    let slot = local.scales.get(code);
    if (slot) return slot;
    const host = el('div');
    slot = { code, host, submitted: false, loading: true };
    local.scales.set(code, slot);
    interactionHost.append(host);
    host.append(el('p', { class: 'muted', text: `正在加载量表 ${code}…` }));

    api.scaleDefinition(code).then((response) => {
      if (ctx.signal.aborted) return;
      slot.loading = false;
      slot.definition = response.data;
      renderScaleForm(slot, label, size, instruction);
    }).catch((error) => {
      slot.loading = false;
      host.textContent = '';
      host.append(card(`${label || code}`, [empty(`量表题干加载失败：${describeError(error)}`)]));
    });
    return slot;
  };

  const renderScaleForm = (slot, label, size, instruction) => {
    const definition = slot.definition || {};
    const items = list(definition.items);
    const options = list(definition.options);
    const answers = new Map();
    const optionNodes = [];

    const itemNodes = items.map((item) => {
      const name = `scale-${slot.code}-${item.index}`;
      const optionsRow = el('div', { class: 'scale-options' });
      for (const option of options) {
        const input = el('input', {
          type: 'radio',
          name,
          value: String(option.value),
        });
        input.addEventListener('change', () => {
          answers.set(item.index, Number(option.value));
          updateSubmitState();
        });
        optionNodes.push(input);
        optionsRow.append(el('label', { class: 'scale-option' }, [input, el('span', { text: `${option.value}. ${option.text}` })]));
      }
      return el('div', { class: 'scale-item' }, [
        el('p', { class: 'scale-item__text', text: `${item.index}. ${item.text}` }),
        optionsRow,
      ]);
    });

    const submit = button(`提交 ${slot.code}（${answers.size}/${items.length || size || 20}）`, null, { primary: true, disabled: true });
    const status = el('span', { class: 'muted' });

    const updateSubmitState = () => {
      const total = items.length || Number(size) || 20;
      submit.textContent = `提交 ${slot.code}（${answers.size}/${total}）`;
      submit.disabled = answers.size !== total || slot.submitted;
    };

    submit.addEventListener('click', async () => {
      const total = items.length || Number(size) || 20;
      const responses = [];
      for (let index = 1; index <= total; index += 1) responses.push(answers.get(index));
      if (responses.includes(undefined)) {
        toast('还有题目未作答', 'error');
        return;
      }
      submit.disabled = true;
      status.textContent = '提交中…';
      try {
        const response = await api.submitScale(uuid, slot.code, responses);
        slot.submitted = true;
        status.textContent = `已提交：粗分 ${fmtInt(pick(response.data, 'raw_score'))}，标准分 ${fmtInt(pick(response.data, 'standard_score'))}，${pick(response.data, 'level', DASH)}`;
        for (const node of optionNodes) node.disabled = true;
        updateSubmitState();
      } catch (error) {
        status.textContent = '';
        toast(describeError(error));
        submit.disabled = false;
      }
    });

    slot.host.textContent = '';
    slot.host.append(card(`${label || definition.name || slot.code}（${slot.code}）`, el('div', {}, [
      el('p', { class: 'muted', text: instruction || '请按最近一周的实际感受作答；量表结果只作提示，不作诊断。' }),
      definition.note ? el('p', { class: 'muted', text: definition.note }) : null,
      ...itemNodes,
      el('div', { class: 'row row--between', style: 'margin-top:12px' }, [status, submit]),
    ]), { sub: `${definition.name || ''}｜版本 ${definition.version || DASH}｜${items.length || size || DASH} 题｜反向题 ${list(definition.reverse_items).length} 项` }));
  };

  /* ------------------------------------------------------ 行为任务区 */
  const ensureTaskSlot = (task) => {
    let slot = local.taskSlots && local.taskSlots.get(task);
    if (slot) return slot;
    if (!local.taskSlots) local.taskSlots = new Map();
    const host = el('div');
    slot = { task, host };
    local.taskSlots.set(task, slot);
    interactionHost.append(host);
    return slot;
  };

  const renderTaskStage = (task, { digit = null, phaseLabel = '', index = null, total = null, hint = '' } = {}) => {
    const slot = ensureTaskSlot(task);
    slot.host.textContent = '';
    const title = task === 'sart' ? 'SART 持续注意任务' : 'PVT-B 警觉度任务';
    const stage = el('div', { class: 'task-stage' }, [
      el('div', { class: 'task-stage__digit', text: task === 'sart' && digit !== null ? String(digit) : '•' }),
      el('p', { class: 'muted', text: hint || (task === 'sart'
        ? '看到 1–9 按空格；看到 3 不要按（是否该按由服务端判定）。'
        : '刺激出现后尽快按空格。') }),
      el('p', { class: 'mono', text: `第 ${fmtInt(index)} 试次${total ? ` / ${fmtInt(total)}` : ''}${phaseLabel ? `｜${phaseLabel}` : ''}` }),
    ]);
    slot.host.append(card(title, el('div', {}, [
      stage,
      el('p', { class: 'muted', text: '键盘：空格作答（页面已阻止空格滚动）；反应时按刺激呈现到按键的 performance.now() 差值（秒）上报。' }),
    ])));
    slot.stage = stage;
    return slot;
  };

  /** 反应时（秒）：把服务端"事件→刺激"的延时扣掉，并做最小钳制避免负值。 */
  const reactionTime = (trial) => {
    const elapsed = (performance.now() - trial.onset) / 1000;
    const delay = trial.delay || 0;
    return Math.max(0.05, elapsed - delay);
  };

  /** 空格作答：仅在 trial 已就绪（本试次已渲染刺激）时接受，杜绝跨试次串答。 */
  const onKeyDown = (event) => {
    if (event.code !== 'Space' && event.key !== ' ') return;
    // 输入类元素里允许正常输入空格
    const tag = (event.target && event.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    event.preventDefault();
    const trial = local.trial;
    if (!trial || !trial.keyReady || trial.answer) return;
    trial.answer = { responded: true, rt: reactionTime(trial) };
    submitTrial();
  };
  window.addEventListener('keydown', onKeyDown);
  // 视图卸载时摘掉监听，避免其它视图误触发
  ctx.onCleanup(() => window.removeEventListener('keydown', onKeyDown));

  const submitTrial = async () => {
    const trial = local.trial;
    if (!trial || !trial.answer || trial.posting) return;
    trial.posting = true;
    trial.keyReady = false;
    const { task, phase, index, answer } = trial;
    renderTaskStage(task, {
      digit: trial.digit,
      index: trial.index + 1,
      total: trial.total,
      phaseLabel: phase === 'practice' ? '练习' : '正式',
      hint: answer.responded ? `已记录：反应时 ${fmtNum(answer.rt, 3)} s` : '本试次未按键',
    });
    if (local.auto) {
      // 快速模式：后端已自行作答，前端不再 POST（避免重复提交与 409）
      local.trial = null;
      return;
    }
    try {
      if (task === 'sart') {
        // 接口对 rt 做 0.05–10.0 的钳制，超出会 422；这里先夹一次再上报
        const rt = answer.responded ? Math.min(9.999, Math.max(0.051, answer.rt)) : null;
        await api.submitSartTrial(uuid, {
          phase, index, responded: answer.responded,
          rt: rt === null ? null : Number(rt.toFixed(3)),
        });
      } else {
        // PVT 只上报 index/responded/rt：抢答（false_start）需要前端自己判定刺激时刻，
        // 与服务端序列语义容易冲突，交给服务端按 index 顺序处理更安全。
        await api.submitPvtTrial(uuid, {
          index,
          responded: answer.responded,
          rt: answer.responded ? Number(Math.min(answer.rt, 9.999).toFixed(3)) : null,
        });
      }
      local.trial = null;
    } catch (error) {
      trial.posting = false;
      trial.keyReady = true;
      toast(describeError(error));
    }
  };

  /* ------------------------------------------------------------ SSE 接线 */
  const poll = async () => {
    try {
      const response = await api.sessionLive(uuid);
      if (ctx.signal.aborted) return;
      const data = response.data || {};
      const current = { ...(ctx.store.state.currentSession || {}) };
      current.uuid = data.uuid || current.uuid;
      current.status = data.status;
      current.phase = data.phase;
      current.progress = data.progress;
      current.phase_label = (local.phaseOrder.find((phase) => phase.key === data.phase) || {}).label || data.phase;
      ctx.store.setState({ currentSession: current, alerts: list(data.alerts) });
      renderProgress();
    } catch (error) {
      // 轮询失败只在控制台留痕，避免演示时刷屏
      console.warn('[flow] 轮询兜底失败', error);
    }
  };

  const applyPhaseEvent = (payload) => {
    const key = payload.key;
    if (!key) return;
    local.phaseState.set(key, { state: payload.state || 'running', progress: payload.progress });
    const session = { ...(ctx.store.state.currentSession || {}) };
    session.uuid = session.uuid || uuid;
    session.phase = key;
    session.phase_label = payload.label || key;
    if (typeof payload.progress === 'number') session.progress = payload.progress;
    if (payload.state === 'done') session.status = session.status || 'running';
    ctx.store.setState({ currentSession: session, lastEvent: { type: 'phase', data: payload } });
    refresh();
  };

  /**
   * 事件分发。
   * 注意 SSE 帧结构是 `{id, type, session, data, ...}`（见 docs/API.md 的事件示例），
   * 业务负载在内层 `payload`，外层 `envelope` 只用来取 id / type。
   */
  const handleEvent = (type, envelope, payload) => {
    payload = payload || {};
    ctx.store.setState({ lastEvent: { type, data: payload } });
    switch (type) {
      case 'started': {
        local.auto = Boolean(payload.auto);
        local.phaseOrder = list(payload.phases).length ? list(payload.phases) : PHASE_FALLBACK;
        const session = { ...(ctx.store.state.currentSession || {}) };
        session.uuid = uuid;
        session.participant = payload.participant ?? session.participant;
        session.device = payload.device ?? session.device;
        session.time_scale = payload.time_scale ?? session.time_scale;
        session.status = 'running';
        ctx.store.setState({
          currentSession: session,
          phases: local.phaseOrder,
          sessionRuntime: { source_kind: payload.source_kind, source_note: payload.source_note },
        });
        refresh();
        break;
      }
      case 'notice': {
        local.notices.push({ level: payload.level, message: payload.message });
        renderNotices();
        break;
      }
      case 'phase':
        applyPhaseEvent(payload);
        break;
      case 'progress': {
        const session = { ...(ctx.store.state.currentSession || {}) };
        if (typeof payload.progress === 'number') session.progress = payload.progress;
        session.phase = payload.key || session.phase;
        const current = local.phaseState.get(payload.key);
        local.phaseState.set(payload.key, {
          state: (current && current.state) || 'running',
          progress: payload.progress,
        });
        ctx.store.setState({ currentSession: session });
        refresh();
        break;
      }
      case 'quality': {
        const host = el('div', {}, [
          el('p', { text: `质检：${payload.passed ? '通过' : '未达门槛'}｜可用 ${fmtInt(payload.usable)}/${fmtInt(payload.windows)} 窗（${fmtPercent(payload.valid_ratio)}）` }),
          list(payload.reasons ? Object.entries(payload.reasons) : []).length
            ? el('ul', { class: 'tag-list' }, Object.entries(payload.reasons).map(([reason, count]) => el('li', { class: 'tag', text: `${reason}×${count}` })))
            : null,
        ]);
        ensureTaskSlot('quality').host.textContent = '';
        ensureTaskSlot('quality').host.append(card('设备质检结果', host));
        break;
      }
      case 'baseline': {
        const baseline = payload.baseline || {};
        const indices = pick(baseline, 'indices', {}) || {};
        const bands = pick(baseline, 'bands', {}) || {};
        const slot = ensureTaskSlot('baseline');
        slot.host.textContent = '';
        slot.host.append(card(`基线：${payload.label || payload.key || DASH}`, el('div', {}, [
          el('p', { class: 'muted', text: `可用窗 ${fmtInt(pick(baseline, 'n_windows'))}｜设备 ${pick(baseline, 'device', DASH)}｜采样率 ${fmtNum(pick(baseline, 'srate'), 0)} Hz｜${pick(baseline, 'valid') ? '有效' : '无效'}` }),
          table([
            { title: '类别', render: (row) => row[0] },
            { title: '均值', align: 'right', render: (row) => fmtNum(pick(row[1], 'mean'), 4) },
            { title: '标准差', align: 'right', render: (row) => fmtNum(pick(row[1], 'std'), 4) },
            { title: '中位数', align: 'right', render: (row) => fmtNum(pick(row[1], 'median'), 4) },
            { title: 'n', align: 'right', render: (row) => fmtInt(pick(row[1], 'n')) },
          ], [
            ...Object.entries(bands).map(([key, stat]) => [`频带 ${key}`, stat]),
            ...Object.entries(indices).map(([key, stat]) => [`指标 ${key}`, stat]),
          ]),
          pick(baseline, 'valid') === false
            ? el('p', { class: 'muted', text: '该基线无效，后续指标不可用（界面统一显示 "—"）。' })
            : null,
        ])));
        break;
      }
      case 'scales': {
        // 后端 core/runtime.py:374 发的是 {results:[scored, ...]}：
        // 每个 scored 含 code/name/version/raw_score/standard_score/level/responses
        mergeScaleResults(list(payload.results));
        renderScaleSummary();
        break;
      }
      case 'scale_request': {
        ensureScaleSlot(payload.code, payload.label, payload.size, payload.instruction);
        break;
      }
      case 'scale_scored': {
        const slot = local.scales.get(payload.code);
        if (slot) {
          slot.host.append(el('p', {
            class: 'muted',
            text: `计分完成：粗分 ${fmtInt(payload.raw_score)}，标准分 ${fmtInt(payload.standard_score)}，${payload.level || DASH}`,
          }));
        }
        mergeScaleResults([payload]);
        renderScaleSummary();
        break;
      }
      case 'behavior_request': {
        local.behaviorRequest = payload;
        if (payload.task === 'pvt') {
          renderTaskStage('pvt', { hint: 'PVT-B：刺激出现后尽快按空格（试次数与刺激时刻表由接口下发）。' });
        } else {
          renderTaskStage('sart', {
            hint: 'SART：看到 1–9 按空格；看到 3 不要按。等待首个刺激…',
          });
        }
        break;
      }
      case 'trial': {
        const task = payload.task || (local.behaviorRequest && local.behaviorRequest.task) || 'sart';
        const at = performance.now();
        const delay = task === 'sart'
          ? (payload.phase === 'practice' ? TRIAL_START_DELAY.sart_practice : TRIAL_START_DELAY.sart_main)
          : 0;
        // PVT 的 onset 是绝对时刻：当同一序列里两个 onset 都到达后，可用差值校准本地时钟
        let clockSkew = 0;
        if (task === 'pvt' && typeof payload.onset === 'number') {
          if (typeof local.previousOnset === 'number' && local.previousArrival) {
            clockSkew = (at - local.previousArrival) / 1000 - (payload.onset - local.previousOnset);
          }
          local.previousOnset = payload.onset;
          local.previousArrival = at;
        }
        local.trial = {
          task,
          phase: payload.phase || 'main',
          index: Number(payload.index ?? 0),
          digit: payload.digit ?? null,
          total: payload.total ?? null,
          onset: at,
          delay: delay + Math.max(0, clockSkew),
          keyReady: true,
          answer: null,
          posting: false,
        };
        renderTaskStage(task, {
          digit: payload.digit ?? null,
          index: local.trial.index + 1,
          total: payload.total,
          phaseLabel: payload.phase === 'practice' ? '练习' : '正式',
          hint: task === 'sart'
            ? (payload.digit !== null ? '按空格作答（是否该按由服务端判定）' : '按空格作答')
            : '刺激出现，尽快按空格',
        });
        if (payload.phase === 'done') local.trial = null;
        break;
      }
      case 'behavior': {
        const slot = ensureTaskSlot(`result-${payload.task}`);
        const result = payload.result || {};
        slot.host.textContent = '';
        const rows = payload.task === 'sart'
          ? [
            ['试次 / Go / No-Go', `${fmtInt(result.trials)} / ${fmtInt(result.go_trials)} / ${fmtInt(result.nogo_trials)}`],
            ['Go 正确率', fmtPercent(result.go_accuracy)],
            ['虚报率 / 漏报率', `${fmtPercent(result.commission_rate)} / ${fmtPercent(result.omission_rate)}`],
            ['反应时均值 ± 标准差', `${fmtNum(result.rt_mean, 3)} ± ${fmtNum(result.rt_sd, 3)} s`],
            ['反应时变异系数', fmtNum(result.rt_variability, 3)],
          ]
          : [
            ['试次 / 应答 / 漏失', `${fmtInt(result.trials)} / ${fmtInt(result.responded)} / ${fmtInt(result.missed)}`],
            ['中位反应时', `${fmtNum(result.rt_median, 3)} s`],
            ['慢反应率', fmtPercent(result.lapse_rate)],
            ['抢答次数', fmtInt(result.false_starts)],
            ['数据有效性', result.valid === true ? '有效' : result.valid === false ? '无效' : DASH],
          ];
        slot.host.append(card(`${payload.task === 'sart' ? 'SART' : 'PVT-B'} 结果`, table([
          { title: '指标', render: (row) => row[0] },
          { title: '值', align: 'right', render: (row) => row[1] },
        ], rows), { sub: `口径 ${pick(result, 'spec', DASH)}` }));
        break;
      }
      case 'monitor': {
        const summary = payload.summary || {};
        const indicators = summary.indicators || summary;
        const rows = ['focus', 'relax', 'load'].map((name) => {
          const stat = indicators && indicators[name] ? indicators[name] : {};
          return [`${{ focus: '专注度', relax: '放松度', load: '认知负荷' }[name]}`, `${fmtNum(pick(stat, 'mean'), 3)} ± ${fmtNum(pick(stat, 'std'), 3)}（n=${fmtInt(pick(stat, 'n'))}）`];
        });
        const slot = ensureTaskSlot('monitor');
        slot.host.textContent = '';
        slot.host.append(card('任务态监测汇总', table([
          { title: '指标', render: (row) => row[0] },
          { title: '均值 ± 标准差', align: 'right', render: (row) => row[1] },
        ], rows), { sub: `可用窗比例 ${fmtPercent(pick(payload.quality, 'valid_ratio'))}` }));
        break;
      }
      case 'window': {
        // 更新最近一窗波形，但做节流：监测阶段每 2 秒一窗，逐窗重绘会明显拖慢页面
        if (payload.signal && list(payload.signal.samples).length) {
          const now = performance.now();
          if (!local.lastWaveAt || now - local.lastWaveAt > 5000) {
            local.lastWaveAt = now;
            const slot = ensureTaskSlot('waveform');
            slot.host.textContent = '';
            const host = el('div');
            slot.host.append(card(`最近一窗波形（第 ${fmtInt(payload.index)} 窗，t=${fmtSeconds(payload.t_end)}）`, host));
            drawWaveform(host, payload.signal, { height: 140 });
          }
        }
        break;
      }
      case 'training_start': {
        const slot = ensureTaskSlot('training');
        slot.host.textContent = '';
        slot.host.append(card('训练开始', el('div', {}, [
          el('p', { text: `模式 ${payload.mode || DASH}｜${fmtInt(payload.segments)} 段 × ${fmtDuration(payload.segment_sec)}｜初始目标 ${fmtNum(payload.target, 3)}｜保持 ${fmtDuration(payload.hold_sec)}` }),
          payload.rationale ? el('p', { class: 'muted', text: `目标依据：${typeof payload.rationale === 'string' ? payload.rationale : JSON.stringify(payload.rationale)}` }) : null,
        ])));
        break;
      }
      case 'segment': {
        const slot = ensureTaskSlot('segments');
        const body = slot.body || (slot.body = el('div'));
        const stats = payload.stats || {};
        body.append(table([
          { title: '段', render: () => fmtInt(pick(payload, 'seq')) },
          { title: '目标（前→后）', render: () => `${fmtNum(payload.target, 3)} → ${fmtNum(payload.target_after, 3)}` },
          { title: '均值', align: 'right', render: () => fmtNum(pick(stats, 'mean'), 3) },
          { title: '达标占比', align: 'right', render: () => fmtPercent(pick(stats, 'on_target_ratio')) },
          { title: '波动', align: 'right', render: () => fmtNum(pick(stats, 'volatility'), 3) },
          { title: '有效点', align: 'right', render: () => fmtInt(pick(stats, 'n')) },
        ], [payload]));
        if (!slot.host.contains(body)) slot.host.append(card('训练分段', body));
        break;
      }
      case 'assessment': {
        const slot = ensureTaskSlot('assessment');
        slot.host.textContent = '';
        slot.host.append(card('联合评估结论', el('div', {}, [
          el('p', { text: payload.conclusion || DASH }),
          el('p', { class: 'muted', text: `注意力维度：${pick(payload, 'dimension_states.attention.state', DASH)}｜压力维度：${pick(payload, 'dimension_states.stress.state', DASH)}` }),
          el('p', { class: 'muted', text: `一致性：脑电 vs 量表 ${pick(payload, 'consistency.eeg_vs_scale', DASH)}；脑电 vs 行为 ${pick(payload, 'consistency.eeg_vs_behavior', DASH)}` }),
          payload.boundary ? el('p', { class: 'muted', text: `边界：${payload.boundary}` }) : null,
        ])));
        break;
      }
      case 'model': {
        const slot = ensureTaskSlot('model');
        slot.host.textContent = '';
        slot.host.append(card('基线模型指标', table([
          { title: '指标', render: (row) => row[0] },
          { title: '值', align: 'right', render: (row) => row[1] },
        ], [
          ['特征数', fmtInt(list(payload.features).length)],
          ['样本数', fmtInt(payload.samples)],
          ['训练 AUC', fmtNum(pick(payload, 'train.auc'), 4)],
          ['验证 AUC', fmtNum(pick(payload, 'validation.auc'), 4)],
          ['测试 AUC', fmtNum(pick(payload, 'test.auc'), 4)],
          ['模型文件', pick(payload, 'model_path', DASH)],
        ]), { sub: `划分 训练/验证/测试 = ${fmtInt(pick(payload, 'subject_split.train'))}/${fmtInt(pick(payload, 'subject_split.validation'))}/${fmtInt(pick(payload, 'subject_split.test'))}` }));
        break;
      }
      case 'artifacts': {
        const slot = ensureTaskSlot('artifacts');
        slot.host.textContent = '';
        slot.host.append(card('已生成产物', el('ul', { class: 'tag-list' }, Object.entries(payload || {}).map(([kind, path]) => el('li', {
          class: 'tag',
          text: kind,
          title: String(path),
        })))));
        break;
      }
      case 'cancelled': {
        toast(payload.message || '会话已取消', 'info');
        break;
      }
      case 'error': {
        const slot = ensureTaskSlot('error');
        slot.host.textContent = '';
        slot.host.append(card('运行异常', [empty(payload.message || '会话运行出错')]));
        break;
      }
      case 'finished': {
        local.finished = true;
        local.trial = null;
        const status = payload.status;
        const session = { ...(ctx.store.state.currentSession || {}) };
        session.uuid = session.uuid || uuid;
        session.status = status;
        session.error = payload.error;
        ctx.store.setState({ currentSession: session });
        refresh();
        tailHost.textContent = '';
        tailHost.append(card('会话结束', el('div', {}, [
          el('p', { text: `最终状态：${statusText(status)}${payload.error ? `｜错误：${payload.error}` : ''}` }),
          el('div', { class: 'row' }, [
            button('查看实时监测', () => ctx.navigate(`#/live?session=${uuid}`)),
            button('查看报告', () => ctx.navigate(`#/report?session=${uuid}`), { primary: true }),
            button('训练视图', () => ctx.navigate(`#/training?session=${uuid}`)),
            button('历史会话', () => ctx.navigate('#/history')),
            el('a', { href: api.exportZipUrl(uuid), text: '下载全部产物（zip）' }),
          ]),
        ])));
        break;
      }
      default:
        break;
    }
  };

  /* -------------------------------------------------------- 初次加载 */
  refresh();

  // 阶段清单必须带引导字段（headline/details/duration/advance）。
  // `started` 事件只在会话刚开始时推一次，页面在会话中途才打开时收不到，
  // 这时如果只用兜底清单就会丢掉每个阶段的引导说明——所以这里主动取一次
  // /api/config 的 phases（它来自后端 phases.as_list()，是同一份数据）。
  const ensurePhaseGuidance = async () => {
    const seeded = list(pick(ctx.store.state.config, 'phases', []));
    const hasGuidance = seeded.length && pick(seeded[0], 'headline', null);
    if (hasGuidance) {
      local.phaseOrder = seeded;
      refresh();
      return;
    }
    try {
      const response = await api.config();
      if (ctx.signal.aborted) return;
      const phases = list(pick(response.data, 'phases', []));
      if (!phases.length || !pick(phases[0], 'headline', null)) return;
      ctx.store.setState({ config: response.data });
      local.phaseOrder = phases;
      refresh();
    } catch (error) {
      console.warn('[flow] 阶段引导加载失败，沿用兜底清单', error);
    }
  };
  ensurePhaseGuidance();

  api.getSession(uuid).then((response) => {
    if (ctx.signal.aborted) return;
    const data = response.data || {};
    const session = { ...data };
    if (data.session) Object.assign(session, data.session);
    session.uuid = uuid;
    ctx.store.setState({ currentSession: session });
    const runtime = pick(data, 'runtime', {}) || {};
    ctx.store.setState({
      sessionRuntime: { source_kind: runtime.source_kind, source_note: pick(data, 'source_note', null) },
      alerts: list(pick(data, 'alerts', [])),
    });
    // 非运行中的会话：SSE 只会补发 phase + finished 快照，这里先把阶段状态按 runs 还原
    const sessionProgress = Number.isFinite(Number(data.progress)) ? Number(data.progress) : null;
    for (const run of list(pick(data, 'runs', []))) {
      if (!run || !run.phase) continue;
      // runs 表本身没有 progress 列（routes.py / schema.sql 确认，前端拿到的 payload 也已解码），所以：
      // 1) 先看该 run 自己的 payload.progress；2) 只有"会话当前阶段"能借用会话级 progress（data.progress）；
      // 3) 其余阶段保持 null，绝不编造进度。
      const runPayload = pick(run, 'payload', {}) || {};
      const rawProgress = pick(runPayload, 'progress', null);
      const progress = (rawProgress === null || rawProgress === undefined)
        ? (run.phase === data.phase ? sessionProgress : null)
        : rawProgress;
      local.phaseState.set(run.phase, {
        state: run.status === 'running' ? 'running' : run.status === 'done' ? 'done' : (run.status || 'pending'),
        progress,
      });
    }
    refresh();
    // 已结束/非运行中的会话不会再发 scales / scale_scored：用报告接口的量表结果兜底，
    // 否则"量表计分汇总"卡在复盘时会永远是空的
    if (data.status && data.status !== 'running') {
      api.report(uuid).then((reportResponse) => {
        if (ctx.signal.aborted) return;
        const scales = pick(reportResponse.data, 'scales', {}) || {};
        const rows = Array.isArray(scales) ? scales : Object.values(scales);
        if (!rows.length) return;
        mergeScaleResults(rows.map((row) => pick(row, 'result', row)));
        renderScaleSummary();
      }).catch(() => { /* 报告不可用时不阻塞流程视图 */ });
    }
    if (data.status && data.status !== 'running') {
      tailHost.textContent = '';
      tailHost.append(card('会话当前状态', el('div', {}, [
        el('p', { text: `${statusText(data.status)}｜阶段 ${phaseText(data.phase, data.phase_label)}` }),
        el('div', { class: 'row' }, [
          button('查看报告', () => ctx.navigate(`#/report?session=${uuid}`)),
          button('历史会话', () => ctx.navigate('#/history')),
          el('a', { href: api.exportZipUrl(uuid), text: '下载全部产物（zip）' }),
        ]),
      ])));
    }
    if (list(pick(data, 'alerts', [])).length) {
      const alertHost = el('div');
      tailHost.append(card('预警时间轴', alertHost));
      drawAlertTimeline(alertHost, list(data.alerts));
    }
  }).catch((error) => {
    toast(describeError(error));
    tailHost.append(card('会话信息加载失败', [empty(describeError(error))]));
  });

  const events = connectSessionEvents(uuid, {
    onEvent: (type, frame) => {
      if (ctx.signal.aborted) return;
      handleEvent(type, frame, frame && frame.data);
    },
    onOpen: () => {
      if (local.offline) {
        local.offline = false;
        renderProgress();
      }
    },
    onError: () => {
      // 断线兜底：切到 10 秒轮询 /live，恢复后由 onOpen 切回
      if (!local.offline) {
        local.offline = true;
        renderProgress();
      }
    },
  });

  // 每秒刷新一次"当前该做什么"，让"已用 / 约剩"跟着走（页面不可见时不刷）
  const ticker = window.setInterval(() => {
    if (document.hidden || local.finished) return;
    local.now = Date.now();
    renderStep();
  }, 1000);

  const poller = window.setInterval(() => {
    if (document.hidden) return;
    poll();
  }, 10000);
  ctx.onCleanup(() => {
    window.clearInterval(ticker);
    window.clearInterval(poller);
    events.close();
  });
}

export default render;
