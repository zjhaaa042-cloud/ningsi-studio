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

/**
 * 量表指导语的兜底文案。
 * 值必须与后端 `core/runtime.py` 在 `_run_scales` 里发布的 `instruction` 逐字一致
 * （`请按最近一周的实际感受作答；量表结果只作提示。`）。
 * 理由：SSE 不重放历史事件，中途打开/刷新时拿不到 `scale_request`，只能靠
 * `GET /api/sessions/{uuid}` 的 `runtime.awaiting_input` 补建作答区；两条路径必须显示同一句指导语，
 * 否则「全程开着」与「中途打开」会看到两种不同措辞。
 */
const SCALE_INSTRUCTION_FALLBACK = '请按最近一周的实际感受作答；量表结果只作提示。';

/**
 * `runtime.awaiting_input` 里可能出现的非量表等待名。
 * 实测（Lead 探针）SART/PVT 阶段确实会以 `sart` / `pvt` 出现：后端 `core/runtime.py:500-509`
 * `_wait_trial` 是**先 publish("trial") 再 wait_for_input(name)**，即试次由客户端驱动 ——
 * 服务端发出第 N 个试次后必须等到作答才发第 N+1 个，而 `INPUT_TIMEOUT_SEC = 900.0`（runtime.py:40）
 * **不随 time_scale 缩放**。所以页面中途打开时若不补一次"未作答"上报，整段训练会一路停摆。
 */
const TASK_WAIT_KEYS = ['sart', 'pvt'];

/** 中途打开时该试次的恢复提示：刺激已过去，不能伪造反应时，只能如实记为未作答。 */
const RESTORE_HINT = '已恢复：本试次刺激在页面打开前已呈现，无法重放；本试次按"未作答"记录，下一试次起正常。';

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
  // 操作者动作（实时监测 / 报告 / 取消会话）：被测者视图下整行隐藏——
  // 电脑在被测者手里时，「取消会话」不该是一个能被误点的按钮。
  const headActions = el('div', { class: 'row' }, [
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
  ]);
  const head = el('div', { class: 'row row--between' }, [
    el('h1', { text: '会话流程' }),
    headActions,
  ]);

  const stepHost = el('div');
  const noticeHost = el('div');
  const phaseHost = el('div');
  const interactionHost = el('div', { class: 'flow-task' });
  const scaleSummaryHost = el('div');
  const tailHost = el('div');
  const taskBarHost = el('div', { class: 'flow-taskbar' });
  // 「总览」= 当前阶段状态卡 + 阶段清单 + 汇总 + 尾部；被测者作答时整块折叠掉（见 syncTaskMode）。
  // 只折叠显示，不删除节点：中途刷新恢复、阶段留白断言、SSE 更新都不受影响。
  //
  // 为什么把"总体进度"并进"当前该做什么"：原来是两张卡，各自重复一遍"当前阶段/百分比/状态/设备"，
  // 用户反馈"页面混乱"主要就来自这种重复。现在合成一张：顶部一条细进度 + 阶段 x/11，
  // 下面是这一步要做什么与唯一的主按钮，其余说明收进「本阶段要做什么」折叠区。
  const overviewHost = el('div', { class: 'flow-overview' });
  overviewHost.append(
    stepHost,
    noticeHost,
    card('全部阶段', phaseHost, {
      sub: '共 11 步：✓ 已完成 / 高亮 当前 / 灰 待开始；点任意一步，说明显示在下方',
    }),
    scaleSummaryHost,
    tailHost,
  );

  container.append(head);
  container.append(taskBarHost);
  container.append(overviewHost);
  container.append(interactionHost);

  /* ---------------------------------------------------- 页面密度（压高） */
  /* 为什么压：`#/flow` 要在 1600×1000 的演示窗口里一屏看全，原来"全部阶段"每项一行、
     量表作答区每题的 4 个选项竖排，整页会到 2000~4200px。
     为什么只用内联样式：`web/css/app.css` 不在本次改动范围（task-5 硬约束），
     所以这里压的是"密度"（字号/行距/padding/gap），不动主题色、不动 DOM 结构、不删任何信息项。
     为什么用 MutationObserver 而不是在首屏贴一次：flow.js 每个 ticker（每秒 renderStep）、
     展开阶段、量表作答区、尾部卡片都会重建节点，贴一次的样式下一秒就被新节点覆盖；
     观察 childList/subtree 不会因为"我们在回调里改 style"再次触发，所以不会自激。 */
  const tighten = () => {
    // 1) 卡片留白与标题层级
    container.querySelectorAll('.card').forEach((node) => {
      node.style.padding = '8px 12px';
      const title = node.querySelector('.card__title');
      if (title) { title.style.fontSize = '14px'; title.style.marginBottom = '4px'; }
      const sub = node.querySelector('.card__sub');
      if (sub) { sub.style.fontSize = '11px'; sub.style.marginTop = '2px'; }
    });
    // 2) 进度条 / 次要说明 / 列表间距
    container.querySelectorAll('.progress').forEach((node) => { node.style.height = '6px'; node.style.margin = '4px 0'; });
    container.querySelectorAll('p.muted, .muted').forEach((node) => { node.style.fontSize = '12px'; node.style.margin = '2px 0'; });
    container.querySelectorAll('.stack').forEach((node) => { node.style.gap = '2px'; });
    container.querySelectorAll('.tag-list').forEach((node) => { node.style.gap = '4px'; });
    // 3) 全部阶段：一行式步进条（紧凑格子 + 下方说明面板）。间距在 CSS 里，
    //    这里只做兜底，避免不同浏览器下换行间距不一致。
    const phaseList = container.querySelector('.phase-list');
    if (phaseList) {
      phaseList.style.display = 'flex';
      phaseList.style.flexWrap = 'wrap';
      phaseList.style.gap = '6px';
      phaseList.style.margin = '0';
      phaseList.style.padding = '0';
      phaseList.style.alignItems = 'stretch';
    }
    container.querySelectorAll('.phase-item').forEach((item) => {
      item.querySelectorAll('.phase-item__main, .phase-item__main *').forEach((node) => {
        node.style.fontSize = '11.5px';
        node.style.lineHeight = '1.25';
        node.style.margin = '0';
      });
      item.querySelectorAll('.right, .right *').forEach((node) => {
        node.style.fontSize = '11px';
        node.style.lineHeight = '1.2';
      });
    });
    // 4) 「当前该做什么」：最高的一块，只收紧字号行距间距
    const step = container.querySelector('.step');
    if (step) {
      step.style.fontSize = '13px';
      step.style.lineHeight = '1.35';
      const set = (selector, styles) => {
        const node = step.querySelector(selector);
        if (node) Object.assign(node.style, styles);
      };
      set('.step__head', { marginBottom: '2px' });
      set('.step__headline', { fontSize: '17px', margin: '2px 0 0' });
      set('.step__details', { margin: '4px 0 0', paddingLeft: '18px' });
      step.querySelectorAll('.step__details li').forEach((li) => { li.style.margin = '0'; li.style.lineHeight = '1.3'; });
      set('.step__meta', { margin: '4px 0 0' });
      set('.step__auto', { margin: '4px 0 0' });
      set('.step__next', { margin: '2px 0 0' });
      set('.step__action', { margin: '6px 0 0' });
    }
    const cardByTitle = (pattern) => Array.from(container.querySelectorAll('.card'))
      .find((node) => pattern.test((node.querySelector('.card__title') || {}).textContent || ''));
    // 5) 预警时间轴（drawAlertTimeline 生成的 .timeline__item）
    const alertCard = cardByTitle(/预警时间轴/);
    if (alertCard) {
      alertCard.querySelectorAll('.timeline__item').forEach((item) => {
        item.style.padding = '2px 0';
        item.style.margin = '0';
        item.style.minHeight = '0';
        item.querySelectorAll('*').forEach((node) => {
          node.style.fontSize = '11.5px';
          node.style.lineHeight = '1.25';
          node.style.margin = '0';
        });
      });
      const timeline = alertCard.querySelector('.timeline');
      if (timeline) { timeline.style.gap = '0'; timeline.style.padding = '0'; }
    }
    // 6) 「会话当前状态」
    const stateCard = cardByTitle(/^会话当前状态$/);
    if (stateCard) {
      stateCard.querySelectorAll('*').forEach((node) => {
        node.style.fontSize = '12px';
        node.style.lineHeight = '1.35';
        node.style.margin = '1px 0';
      });
      const body = stateCard.querySelector('.card__body');
      if (body) body.style.padding = '0';
    }
    // 7) 顶部标题行
    const head0 = container.querySelector('.row.row--between');
    if (head0) {
      const h1 = head0.querySelector('h1');
      if (h1) { h1.style.fontSize = '18px'; h1.style.margin = '0'; }
      head0.style.margin = '0';
      head0.style.padding = '0 0 4px';
    }
    // 8) 「量表计分汇总」每行
    const summaryCard = cardByTitle(/^量表计分汇总$/);
    if (summaryCard) {
      summaryCard.querySelectorAll('p.mono, .mono').forEach((node) => {
        node.style.fontSize = '12px';
        node.style.lineHeight = '1.3';
        node.style.margin = '1px 0';
      });
    }
    // 9) 「会话结束」动作按钮
    const endCard = cardByTitle(/^会话结束$/);
    if (endCard) {
      endCard.querySelectorAll('.btn, button').forEach((btn) => {
        btn.style.fontSize = '12px';
        btn.style.padding = '4px 10px';
      });
    }
    // 10) 量表作答区：题干单行 + 4 个选项横排（20 题竖排是本页最高的来源）
    /* 为什么不删题：20 题 × 4 选项是"必须可见才能作答"的信息；横排后每选项宽 ~330px、
       高 34px（实测 optH=34 ≥ 32px 可点按下限），题干实测未截断（scrollHeight ≤ clientHeight）。 */
    container.querySelectorAll('.scale-item').forEach((item) => {
      item.style.padding = '4px 0';
      item.style.margin = '0';
      const text = item.querySelector('.scale-item__text');
      if (text) { text.style.fontSize = '12.5px'; text.style.lineHeight = '1.25'; text.style.margin = '0'; }
      const options = item.querySelector('.scale-options');
      if (options) {
        options.style.display = 'flex';
        options.style.flexWrap = 'nowrap';
        options.style.gap = '6px';
        options.style.marginTop = '3px';
      }
      item.querySelectorAll('.scale-option').forEach((option) => {
        option.style.flex = '1 1 0';
        option.style.minHeight = '34px';
        option.style.padding = '4px 6px';
        option.style.margin = '0';
        option.style.display = 'flex';
        option.style.alignItems = 'center';
        option.style.justifyContent = 'center';
        option.style.fontSize = '12px';
        option.style.lineHeight = '1.2';
      });
    });
  };
  tighten();
  const densityObserver = new MutationObserver(() => {
    // 顺序有讲究：先切"任务视图/总览"，再压密度；两者都是幂等的，
    // 且 renderTaskBar 只在标题或模式变化时才重建节点，不会自激。
    syncTaskMode();
    tighten();
  });
  densityObserver.observe(container, { childList: true, subtree: true });
  ctx.onCleanup(() => densityObserver.disconnect());

  /* ---------------------------------------------------------- 本地状态 */
  const local = {
    uuid,
    auto: false,
    autoKnown: false,               // 是否已经收到 started 快照里的 auto（决定是否需要按 time_scale 推）
    running: false,                 // 会话是否仍在运行（恢复交互区的前置条件）
    awaitingInput: [],              // 服务端 runtime.awaiting_input（如 ['scales:SAS']）
    skippedTasks: new Set(),        // 已补过"未作答"上报的行为任务（每任务最多补一次，避免吃掉后续真实试次）
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
    forceOverview: false,           // 任务视图下手动切回总览（换任务时自动复位）
    lastTaskLabel: null,            // 上一次的任务名，用于"换任务→回到任务视图"
    taskBarKey: null,               // 任务条已渲染的键（幂等，避免观察者自激）
  };

  /* ------------------------------------------------- 任务视图 / 总览切换 */
  /**
   * 「任务视图」：被测者需要作答时，把总览（当前该做什么 / 总体进度 / 阶段清单 / 汇总 / 尾部）
   * 整块折叠掉，让任务与作答区独占一屏。
   *
   * 为什么需要：这些块原本排在作答区**前面**，1600×1000 的演示窗口里量表/刺激卡落在折叠线
   * 以下（实测刺激卡顶边 ≈931px），做题要来回滚动，"边看题边作答"体验很差。
   *
   * 折叠只用 CSS 类（`.flow--task .flow-overview { display: none }`），节点全部留在 DOM 里：
   * 中途刷新恢复、SSE 增量更新、以及验收脚本对 `.scale-item` / 阶段留白的断言都不受影响。
   * 任务结束自动回总览；同一任务内可用任务条上的按钮手动切回，**不中断会话**。
   */
  const taskModeLabel = () => {
    if (local.finished) return null;
    const stage = interactionHost.querySelector('.task-stage');
    const scale = interactionHost.querySelector('.scale-item');
    const anchor = stage || scale;
    if (!anchor) return null;
    const title = anchor.closest('.card')?.querySelector('.card__title')?.textContent?.trim();
    return title || (stage ? '行为任务' : '量表作答');
  };

  const TASK_MODE_HINT = '任务视图：只显示当前任务与作答区，总览已折叠（会话照常运行）';

  /**
   * 任务视图下，把交互区里**不是当前任务**的卡片也折叠掉。
   *
   * 为什么必须做：交互区会累积前面阶段的卡片（设备质检结果、最近一窗波形、基线…），
   * 实测一个跑到量表阶段的会话里，这些卡在上述作答卡之前占了 ~830px，
   * 量表卡被推到 1000px 视口之外——只折叠"总览"仍然要滚动才能作答。
   * 判据是"这张卡里有没有 .scale-item / .task-stage"，所以多张量表同时待答时都保留。
   */
  const markTaskCards = () => {
    [...interactionHost.children].forEach((child) => {
      const isTask = !!child.querySelector('.scale-item, .task-stage');
      child.classList.toggle('flow-task-hidden', !isTask);
    });
  };

  /* ------------------------------------------------------ 被测者视图（全屏） */
  /* 给别人用的时候，电脑是交给被测者的：顶栏、左侧导航、仪表盘对他全是干扰。
     进入后只留"现在该做什么 + 任务区"，操作者按 Esc 或点按钮退出（不会中断会话）。 */
  const setSubjectMode = (on) => {
    local.subjectMode = on === true;
    document.body.classList.toggle('subject-mode', local.subjectMode);
    taskBarHost.classList.toggle('flow-taskbar--subject', local.subjectMode);
    // 被测者视图里不显示"实时监测 / 报告 / 取消会话"（防误点取消）
    headActions.style.display = local.subjectMode ? 'none' : '';
    local.taskBarKey = null;                     // 强制重建任务条（按钮文案/按钮集变了）
    syncTaskMode();
  };
  const onSubjectKey = (event) => {
    if (event.key === 'Escape' && local.subjectMode) setSubjectMode(false);
  };
  window.addEventListener('keydown', onSubjectKey);
  ctx.onCleanup(() => {
    window.removeEventListener('keydown', onSubjectKey);
    document.body.classList.remove('subject-mode');   // 离开本页一定恢复操作者界面
  });

  const renderTaskBar = (label) => {
    const key = `${label}|${local.forceOverview ? 'overview' : 'task'}|${local.subjectMode ? 'subject' : 'op'}`;
    if (key === local.taskBarKey) return;                 // 幂等：观察者回调里重复调用不再改 DOM
    local.taskBarKey = key;
    taskBarHost.textContent = '';
    // 没有交互任务、但在被测者视图里时：也要给一条"退出"入口，否则隐藏了导航就没有出路
    if (!label && !local.subjectMode) return;
    const subjectButton = local.subjectMode
      ? button('退出被测者视图（Esc）', () => setSubjectMode(false), { small: true })
      : button('被测者视图（全屏）', () => setSubjectMode(true), { small: true });
    taskBarHost.append(
      el('span', { class: 'badge badge--strong', text: local.subjectMode ? '被测者请按提示操作' : '任务进行中' }),
      el('strong', { text: label || '按屏幕上的提示做即可' }),
      el('span', { class: 'muted', text: local.subjectMode
        ? '只显示当前要做的动作；操作者按 Esc 退出'
        : TASK_MODE_HINT }),
      el('div', { class: 'row row--end', style: 'margin-left:auto' }, [
        local.subjectMode ? null : button(local.forceOverview ? '回到任务视图' : '查看总览', () => {
          local.forceOverview = !local.forceOverview;
          local.taskBarKey = null;                        // 强制重建任务条
          syncTaskMode();
          tighten();
        }),
        subjectButton,
      ]),
    );
  };

  const syncTaskMode = () => {
    const label = taskModeLabel();
    if (label && label !== local.lastTaskLabel) local.forceOverview = false;   // 换任务 → 回任务视图
    local.lastTaskLabel = label;
    markTaskCards();
    taskBarHost.classList.toggle('flow-taskbar--on', !!label);
    container.classList.toggle('flow--task', !!label && !local.forceOverview);
    renderTaskBar(label);
  };
  ctx.onCleanup(() => container.classList.remove('flow--task'));

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

  /** 统一刷新：阶段状态卡 + 阶段清单（任何状态变化都走这里）。 */
  const refresh = () => {
    renderPhases();
    renderStep();
  };

  /* -------------------------------------------------- 检测完成（一页结论） */
  /* 为什么单独做：会话跑完后原来只给一张"会话已结束"的小卡 + 尾部再一张"会话当前状态"，
     操作者仍要自己想"这次测出来什么、报告在哪、产物怎么拿"。这里把结束态收成**一页结论**：
     三个指标 + 质检可用窗 + 量表 + 四个出口（报告 / 产物 / 训练 / 再测一次）。 */
  const qualityStats = () => {
    for (const run of list(pick(local.detail, 'runs', []))) {
      const payload = pick(run, 'payload', {}) || {};
      if (run && run.phase === 'qc' && (payload.windows || payload.usable !== undefined)) {
        return { usable: Number(payload.usable) || 0, windows: Number(payload.windows) || 0,
                 passed: payload.passed === true };
      }
    }
    return null;
  };

  const renderDoneCard = () => {
    const session = ctx.store.state.currentSession || {};
    const status = pick(session, 'status', 'done');
    const failed = status === 'failed' || status === 'cancelled';
    const summary = pick(local.detail, 'indicator_summary', {}) || {};
    const quality = qualityStats();
    const scales = [...local.scaleResults.values()];
    const reason = pick(session, 'error', null) || pick(local.detail, 'error', null);

    const metric = (label, value, note) => el('div', { class: 'done__metric' }, [
      el('span', { class: 'done__metric-label', text: label }),
      el('span', { class: 'done__metric-value mono', text: value }),
      el('span', { class: 'done__metric-note', text: note }),
    ]);

    const body = [];
    body.push(el('p', { class: 'done__headline', text: failed
      ? `这次检测${statusText(status)}${reason ? `：${reason}` : ''}`
      : '检测完成，数据与报告已生成' }));
    body.push(el('div', { class: 'done__metrics' }, [
      metric('专注度', fmtNum(pick(summary, 'focus.mean')), '指标均值 0–1'),
      metric('放松度', fmtNum(pick(summary, 'relax.mean')), '指标均值 0–1'),
      metric('认知负荷', fmtNum(pick(summary, 'load.mean')), '指标均值 0–1'),
      metric('采集质量', quality ? `${fmtInt(quality.usable)}/${fmtInt(quality.windows)} 窗` : DASH,
        quality ? (quality.passed ? '达到门槛' : '低于门槛，解释需谨慎') : '没有质检记录'),
    ]));
    if (scales.length) {
      body.push(el('p', { class: 'muted', text: '量表：' + scales.map((row) => `${row.code || DASH} 标准分 ${fmtInt(row.standard_score)}`
        + `（${row.level || DASH}）`).join('｜') }));
    }
    body.push(el('p', { class: 'muted', text: '结论、图表、证据与边界声明都在「评估报告」里；'
      + '原始逐窗数据与产物可整包下载。' }));
    body.push(el('div', { class: 'row done__actions' }, [
      failed
        ? button('重新开始一次检测', () => ctx.navigate('#/start'), { primary: true })
        : button('查看评估报告', () => ctx.navigate(`#/report?session=${uuid}`), { primary: true }),
      failed ? button('查看报告', () => ctx.navigate(`#/report?session=${uuid}`), { small: true }) : null,
      el('a', { href: api.exportZipUrl(uuid), text: '下载全部产物（zip）' }),
      button('训练视图', () => ctx.navigate(`#/training?session=${uuid}`), { small: true }),
      button('历史会话', () => ctx.navigate('#/history'), { small: true }),
      button('再测一次', () => ctx.navigate('#/start'), { small: true }),
    ]));
    stepHost.append(card(failed ? '检测未完成' : '检测完成', el('div', { class: 'done' }, body), {
      sub: `会话 ${String(uuid).slice(0, 8)}…｜${statusText(status)}`,
    }));
  };

  /* ------------------------------------------------------ 当前该做什么 */
  const renderStep = () => {
    stepHost.textContent = '';
    const session = ctx.store.state.currentSession || {};
    const overall = Math.max(0, Math.min(1, Number(pick(session, 'progress', 0)) || 0));

    // 会话结束后不再显示"当前阶段"：11 个阶段全 done 时 currentPhaseKey() 会退化成第 1 个阶段，
    // 页面就会同时写着"阶段 1/11"和"总进度 100%"，自相矛盾（用户看到的就是这种"乱"）。
    if (local.finished) {
      renderDoneCard();
      return;
    }

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
    // 顶部一条细进度 + "阶段 x/11"：全页只保留这一处总体进度，不再单独开一张"总体进度"卡
    body.push(el('div', { class: 'step__bar' }, [
      el('div', { class: 'progress', style: 'flex:1' }, [
        el('div', { class: 'progress__bar', style: `width:${(overall * 100).toFixed(1)}%` }),
      ]),
      el('span', { class: 'mono step__bar-text', text: `阶段 ${index + 1}/${local.phaseOrder.length}｜总进度 ${fmtPercent(overall)}` }),
    ]));

    body.push(el('div', { class: 'row row--between step__head' }, [
      el('div', { class: 'row', style: 'gap:10px;align-items:baseline' }, [
        el('span', { class: 'step__label', text: pick(phase, 'label', key) }),
        interactive ? el('span', { class: 'tag', text: INTERACTIVE_KEY[key] || '需交互' }) : null,
      ]),
      el('span', { class: 'badge' + (state === 'running' ? ' badge--strong' : ''), text: stateLabel }),
    ]));

    body.push(el('p', { class: 'step__headline', text: headline }));

    const meta = [];
    const timing = describeTiming(phase, state);
    // 计时节点留引用：1 秒刷新只改这一行，不再重建整张卡（重建会把折叠区合上、
    // 也会把正在点的按钮换成新节点 → "点了没反应"）
    const timingNode = el('span', { class: 'step__meta-item mono', text: timing || '' });
    meta.push(timingNode);
    meta.push(el('span', { class: 'step__meta-item', text: ADVANCE_TEXT[advance] || ADVANCE_TEXT.auto }));
    if (state === 'running' && typeof stageProgress === 'number' && stageProgress !== null) {
      meta.push(el('span', { class: 'step__meta-item mono', text: `本阶段 ${fmtPercent(stageProgress)}` }));
    }
    body.push(el('div', { class: 'step__meta' }, meta));

    // 需要被试作答的阶段：主按钮把作答区带到眼前（唯一的主行动）
    if (interactive && !local.auto) {
      body.push(el('div', { class: 'row step__action' }, [
        button('开始作答 / 查看题目', () => {
          // 兜底：页面中途打开/刷新时 SSE 不会重放 scale_request，作答卡可能还不存在。
          // 先按 runtime.awaiting_input 补建（幂等），再滚过去，做到"点了必定有反应"。
          const restored = restoreInteraction();
          const anchor = restored || interactionHost.querySelector('.card');
          if (!anchor) {
            toast('还没有收到作答任务，请稍候…', 'info');
            return;
          }
          // 滚到卡片顶部而不是居中：量表作答卡有 20 题、比视口还高，居中会把人扔到
          // 表单中段；顶部才是指导语 + 第一题。留 60px 让粘性顶栏（app.css 的 .topbar，
          // position:sticky; top:0）不盖住卡片标题。
          if (anchor.scrollIntoView) {
            anchor.style.scrollMarginTop = '60px';
            anchor.scrollIntoView({ block: 'start', behavior: 'smooth' });
          }
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

    /* 次要信息一律进折叠区（渐进披露）：要点、为什么需要这一步、下一步、快速演示说明、会话与设备信息 */
    const more = [];
    // 「为什么需要这一步」放在要点之前：用户问过"会话流程中的操作都是必要的吗"，
    // 页面必须自己能答（每个阶段在后端 phases.py 里有 why 字段）。
    if (pick(phase, 'why', null)) {
      more.push(el('p', { class: 'step__why', text: `为什么需要这一步：${pick(phase, 'why')}` }));
    }
    if (details.length) {
      more.push(el('ul', { class: 'step__details' }, details.map((item) => el('li', { text: item }))));
    }
    if (state === 'pending' && nextPhase) {
      more.push(el('p', { class: 'muted step__next', text: `上一步完成后将进入：${pick(nextPhase, 'label', '')}` }));
    } else if (pick(phase, 'next_hint', null)) {
      more.push(el('p', { class: 'muted step__next', text: `之后：${pick(phase, 'next_hint')}` }));
    }
    if (local.auto && pick(phase, 'auto_note', null)) {
      more.push(el('p', { class: 'muted step__auto', text: `快速演示：${pick(phase, 'auto_note')}` }));
    }
    // 时间倍率 / 采样率属于调试细节，不在这里显示（口径与设备参数仍可在报告的"运行环境"里查）
    more.push(el('p', { class: 'muted', text: `会话状态：${statusText(pick(session, 'status', null))}｜设备：${pick(session, 'device', DASH)}` }));
    if (local.auto) {
      more.push(el('p', { class: 'muted', text: '本次为快速演示模式（time_scale < 0.2）：量表与行为任务由服务端生成确定性作答。' }));
    }
    if (local.offline) {
      more.push(el('p', { class: 'muted', text: '实时事件流已断开，正在每 10 秒轮询 /api/sessions/{uuid}/live 兜底。' }));
    }
    // 折叠区状态跨重建保留：SSE 事件与阶段切换都会重建这张卡，不记住 open 就会
    // "点开一秒后又自己合上"（用户反馈的"本阶段要做什么无法正常展开"）。
    const moreBox = el('details', { class: 'step__more' }, [
      el('summary', { text: '本阶段要做什么 / 会话信息' }),
      el('div', { class: 'stack' }, more),
    ]);
    moreBox.open = local.stepMoreOpen === true;
    moreBox.addEventListener('toggle', () => { local.stepMoreOpen = moreBox.open; });
    body.push(moreBox);

    local.stepKey = key;
    local.stepTimingNode = timingNode;
    stepHost.append(card('当前阶段', el('div', { class: `step${stateClass}` }, body), {
      sub: '按提示做即可，页面会自动进入下一步',
    }));
  };

  /** 每秒只刷新"已用 / 约剩"那一行；阶段换了才整卡重画。
   *  这是"折叠区点开就合上 / 按钮点了没反应"的根因修复：原来每秒都重建整张卡。 */
  const tickStep = () => {
    if (document.hidden || local.finished) return;
    local.now = Date.now();
    const key = currentPhaseKey();
    if (!local.stepTimingNode || local.stepKey !== key) {
      renderStep();
      return;
    }
    const phase = local.phaseOrder.find((item) => item.key === key) || { key };
    const state = (local.phaseState.get(key) || {}).state || 'pending';
    local.stepTimingNode.textContent = describeTiming(phase, state) || '';
  };

  /**
   * 阶段总览：**一行式步进条**（11 个紧凑格子）+ 选中阶段的说明面板。
   *
   * 为什么改成这样：原来是 11 行的单列清单（每行名称+提示+状态+时长），在 1000px 高的屏上
   * 占掉 600+px，被试要滚才能看完"我在哪、还剩几步"。现在整条步进条只占 2 行左右，
   * "当前在哪一步 / 已完成几步"一眼可见；点任意格子，说明显示在下面的**同一块面板**里，
   * 而不是把那一行撑高（行高参差会让整块看着乱）。
   *
   * 兼容既有验收：仍然渲染 11 个 `.phase-item`（每个内部一个 `.phase-item__toggle`），
   * 当前阶段带 `--current`、被点开的带 `--expanded`，非展开项仍是自适应高度（无拉伸留白）。
   */
  const renderPhases = () => {
    phaseHost.textContent = '';
    const current = currentPhaseKey();
    const listNode = el('ul', { class: 'phase-list' });
    const scale = Number(pick(ctx.store.state.currentSession, 'time_scale', 1)) || 1;
    const labelOf = (key) => {
      const found = local.phaseOrder.find((item) => item.key === key);
      return (found && found.label) || key;
    };
    let doneCount = 0;
    let currentIndex = -1;

    local.phaseOrder.forEach((phase, index) => {
      const state = local.phaseState.get(phase.key) || { state: 'pending', progress: null };
      const interactive = pick(phase, 'interactive', false);
      const isCurrent = !local.finished && phase.key === current;
      const expanded = local.expandedPhase === phase.key;
      if (state.state === 'done') doneCount += 1;
      if (isCurrent) currentIndex = index;
      const mark = state.state === 'done' ? '✓' : String(index + 1).padStart(2, '0');
      const duration = Number(pick(phase, 'duration_sec', 0)) || 0;

      const item = el('li', {
        class: 'phase-item'
          + (isCurrent ? ' phase-item--current' : '')
          + (state.state === 'done' ? ' phase-item--done' : '')
          + (expanded ? ' phase-item--expanded' : ''),
        dataset: { state: state.state },
      }, [
        el('button', {
          class: 'phase-item__toggle',
          type: 'button',
          title: `${pick(phase, 'label', phase.key)}｜${expanded ? '收起说明' : '展开说明'}`,
          attrs: { 'aria-label': `第 ${index + 1} 步 ${pick(phase, 'label', phase.key)}（${phaseStateText(state.state)}）` },
          onClick: () => {
            local.expandedPhase = expanded ? null : phase.key;
            renderPhases();
          },
        }, [
          el('span', { class: 'phase-item__index mono', text: mark }),
          el('span', { class: 'phase-item__label', text: pick(phase, 'label', phase.key) }),
          interactive ? el('span', { class: 'phase-item__tag', text: '需你作答' }) : null,
        ]),
      ]);
      listNode.append(item);
    });

    // 一条汇总：现在在哪一步、还剩多少（原来要自己数 11 行）
    const summary = el('p', {
      class: 'muted phase-summary',
      text: local.finished
        ? `全部 ${local.phaseOrder.length} 个阶段已完成`
        : `已完成 ${doneCount}/${local.phaseOrder.length}｜当前第 ${currentIndex + 1} 步：${labelOf(current || '')}`
          + (currentIndex >= 0 && currentIndex < local.phaseOrder.length - 1
            ? `｜下一步：${labelOf(local.phaseOrder[currentIndex + 1].key)}` : ''),
    });

    phaseHost.append(summary);
    phaseHost.append(listNode);

    // 说明面板：所有阶段的说明都渲染在这里，只有被点开的那一个可见
    const detailHost = el('div', { class: 'phase-detail' });
    const picked = local.phaseOrder.find((item) => item.key === local.expandedPhase) || null;
    if (!picked) {
      detailHost.append(el('p', {
        class: 'muted',
        text: '点上面任意一个阶段，这里会显示它在做什么、要注意什么、大约多久。',
      }));
    } else {
      const state = local.phaseState.get(picked.key) || { state: 'pending', progress: null };
      const body = [el('div', { class: 'phase-item__title' }, [
        el('span', { class: 'phase-item__label', text: `${pick(picked, 'label', picked.key)}` }),
        el('span', { class: 'badge' + (state.state === 'running' ? ' badge--strong' : ''),
                     text: phaseStateText(state.state) }),
        state.progress !== null && state.progress !== undefined && state.state === 'running'
          ? el('span', { class: 'mono muted', text: fmtPercent(state.progress) }) : null,
      ])];
      if (pick(picked, 'headline', null)) {
        body.push(el('div', { class: 'phase-item__headline', text: pick(picked, 'headline') }));
      }
      if (pick(picked, 'description', null)) {
        body.push(el('div', { class: 'phase-item__desc', text: pick(picked, 'description') }));
      }
      if (pick(picked, 'why', null)) {
        body.push(el('div', { class: 'phase-item__why', text: `为什么需要这一步：${pick(picked, 'why')}` }));
      }
      for (const item of list(pick(picked, 'details', []))) {
        body.push(el('div', { class: 'phase-item__bullet', text: `· ${item}` }));
      }
      if (pick(picked, 'next_hint', null)) {
        body.push(el('div', { class: 'phase-item__next', text: `之后：${pick(picked, 'next_hint')}` }));
      }
      const duration = Number(pick(picked, 'duration_sec', 0)) || 0;
      if (duration) {
        body.push(el('div', { class: 'muted mono', text: scale < 0.2 && scale !== 1
          ? `预计 ${fmtSeconds(duration * scale, 0)}（快速演示）` : `预计 ${fmtSeconds(duration, 0)}` }));
      }
      detailHost.append(el('div', { class: 'stack' }, body));
    }
    phaseHost.append(detailHost);
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
    ]), { sub: '粗分 / 标准分 / 程度由后端计分' }));
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

  /** 量表作答：一次一题（GOV.UK "one thing per page" 的做法）。
   *
   *  为什么改：20/40 题的问卷原来是一整屏长表单，题目区高达 1300+px，被试要一路滚到底才知道
   *  还剩多少题、提交按钮在哪。现在一屏只呈现一道题 + 题号条 + 进度，整张卡控制在视口内。
   *
   *  关键约束：**所有题目仍然全部渲染在 DOM 里**（只是非当前题 `display:none`）——
   *  ① 验收脚本按 DOM 断言"20 题全渲染 / 每题 4 个选项 / radios=80"，这是"题干来自后端定义"
   *  的证明，不能被削弱；② 中途刷新/SSE 重放时不需要重新取题干。
   */
  const renderScaleForm = (slot, label, size, instruction) => {
    const definition = slot.definition || {};
    const items = list(definition.items);
    const options = list(definition.options);
    const total = items.length || Number(size) || 20;
    const answers = new Map();
    const optionNodes = [];
    const itemNodes = [];
    const chips = [];
    let current = 0;

    const questionHost = el('div', { class: 'scale-steps' });
    const navHost = el('div', { class: 'scale-nav' });
    const progressText = el('span', { class: 'muted' });
    const prev = button('上一题', () => go(current - 1, { focus: true }), { small: true });
    const next = button('下一题', () => go(current + 1, { focus: true }), { small: true });

    /** 跳转到第 index 题（越界自动夹住）；只切换可见性，DOM 里 20 题始终都在。 */
    const go = (index, options2 = {}) => {
      const clamped = Math.max(0, Math.min(items.length - 1, Number(index) || 0));
      current = clamped;
      itemNodes.forEach((node, position) => {
        node.classList.toggle('scale-item--current', position === clamped);
      });
      chips.forEach((chip, position) => chip.classList.toggle('is-current', position === clamped));
      prev.disabled = clamped <= 0;
      next.disabled = clamped >= items.length - 1;
      updateSubmitState();
      if (options2.focus && itemNodes[clamped]) {
        // 让读屏/键盘用户落在当前题上；不用 scrollIntoView，卡片本来就是短的
        itemNodes[clamped].setAttribute('tabindex', '-1');
        itemNodes[clamped].focus({ preventScroll: true });
      }
    };

    const firstUnansweredFrom = (start) => {
      for (let position = Math.max(0, start); position < items.length; position += 1) {
        if (!answers.has(items[position].index)) return position;
      }
      for (let position = 0; position < items.length; position += 1) {
        if (!answers.has(items[position].index)) return position;
      }
      return -1;
    };

    items.forEach((item, position) => {
      const name = `scale-${slot.code}-${item.index}`;
      const optionsRow = el('div', { class: 'scale-options' });
      for (const option of options) {
        const input = el('input', {
          type: 'radio',
          name,
          value: String(option.value),
          id: `${name}-opt-${option.value}`,
        });
        input.addEventListener('change', () => {
          answers.set(item.index, Number(option.value));
          updateSubmitState();
          // 选完自动跳到下一道未答题（都答完了就停在原地，等被试确认后提交）
          const nextUnanswered = firstUnansweredFrom(position + 1);
          if (nextUnanswered >= 0 && nextUnanswered !== position) go(nextUnanswered, { focus: true });
        });
        optionNodes.push(input);
        optionsRow.append(el('label', {
          class: 'scale-option',
          for: input.id,
        }, [input, el('span', { text: `${option.value}. ${option.text}` })]));
      }
      const node = el('div', { class: 'scale-item' }, [
        el('p', { class: 'scale-item__text', text: `${item.index}. ${item.text}` }),
        optionsRow,
      ]);
      itemNodes.push(node);
      questionHost.append(node);

      const chip = el('button', {
        class: 'scale-nav__chip',
        type: 'button',
        text: String(item.index),
        title: `第 ${item.index} 题`,
        attrs: { 'aria-label': `跳到第 ${item.index} 题` },
        onClick: () => go(position, { focus: true }),
      });
      chips.push(chip);
      navHost.append(chip);
    });

    const submit = button(`提交 ${slot.code}（${answers.size}/${total}）`, null, { primary: true, disabled: true });
    const status = el('span', { class: 'muted' });

    const updateSubmitState = () => {
      submit.textContent = `提交 ${slot.code}（${answers.size}/${total}）`;
      submit.disabled = answers.size !== total || slot.submitted;
      progressText.textContent = `第 ${current + 1} / ${total} 题｜已答 ${answers.size}/${total}`
        + (answers.size === total ? '｜已全部作答，可提交' : '');
      chips.forEach((chip, position) => {
        chip.classList.toggle('is-answered', answers.has(items[position].index));
      });
    };

    // 键盘：←/→ 翻题，1–4 直接选当前题的选项（不干扰输入框里的数字/方向键）
    const onScaleKey = (event) => {
      if (ctx.signal.aborted || slot.submitted) return;
      const tag = (event.target && event.target.tagName) || '';
      if (tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (tag === 'INPUT' && (event.target.type === 'text' || event.target.type === 'search' || event.target.type === 'number')) return;
      if (event.key === 'ArrowLeft') { go(current - 1, { focus: true }); event.preventDefault(); return; }
      if (event.key === 'ArrowRight') { go(current + 1, { focus: true }); event.preventDefault(); return; }
      const picked = Number(event.key);
      if (Number.isInteger(picked) && picked >= 1 && picked <= options.length) {
        const target = itemNodes[current] && itemNodes[current].querySelectorAll('input[type=radio]')[picked - 1];
        if (target) {
          target.checked = true;
          target.dispatchEvent(new Event('change', { bubbles: true }));
          event.preventDefault();
        }
      }
    };
    window.addEventListener('keydown', onScaleKey);
    ctx.onCleanup(() => window.removeEventListener('keydown', onScaleKey));

    submit.addEventListener('click', async () => {
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
      el('p', { class: 'muted', text: instruction || SCALE_INSTRUCTION_FALLBACK }),
      definition.note ? el('p', { class: 'muted', text: definition.note }) : null,
      // 一次一题：题号条（已答/当前可点）+ 当前题 + 翻页/提交；20 题全在 DOM，只是非当前题不显示
      el('div', { class: 'scale-head' }, [progressText]),
      navHost,
      questionHost,
      el('div', { class: 'scale-foot' }, [
        el('div', { class: 'row' }, [prev, next]),
        el('div', { class: 'row' }, [status, submit]),
      ]),
      el('p', { class: 'muted', text: '键盘：← / → 翻题，1–4 直接选当前题选项；选完会自动跳到下一道未答题。' }),
    ]), { sub: `${definition.name || ''}｜版本 ${definition.version || DASH}｜${items.length || size || DASH} 题｜反向题 ${list(definition.reverse_items).length} 项` }));
    go(0);
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

  const renderTaskStage = (task, { digit = null, phaseLabel = '', index = null, total = null, hint = '', answered = false } = {}) => {
    const slot = ensureTaskSlot(task);
    slot.host.textContent = '';
    const title = task === 'sart' ? 'SART 持续注意任务' : 'PVT-B 警觉度任务';
    const timer = el('div', { class: 'task-stage__timer', text: '' });
    const stage = el('div', { class: 'task-stage' }, [
      el('div', { class: 'task-stage__digit', text: task === 'sart' && digit !== null ? String(digit) : '•' }),
      el('p', { class: 'muted', text: hint || (task === 'sart'
        ? '看到 1–9 按空格；看到 3 不要按（是否该按由服务端判定）。'
        : '刺激出现后尽快按空格。') }),
      el('p', { class: 'mono', text: `第 ${fmtInt(index)} 试次${total ? ` / ${fmtInt(total)}` : ''}${phaseLabel ? `｜${phaseLabel}` : ''}` }),
      timer,
    ]);
    // 鼠标/触屏也能作答：原来只有空格键，现场拿鼠标的人"点了没反应"。
    // 快速演示模式下服务端自动作答，这里不给按钮，避免重复提交。
    const actions = [];
    if (!local.auto && !answered) {
      actions.push(button('点击这里作答（等同按空格）', () => answerCurrent(local.trial), { primary: true, small: true }));
      stage.addEventListener('click', () => answerCurrent(local.trial));
      stage.classList.add('task-stage--clickable');
    }
    slot.host.append(card(title, el('div', {}, [
      stage,
      actions.length ? el('div', { class: 'row', style: 'margin-top:10px' }, actions) : null,
      el('p', { class: 'muted', text: answered
        ? '本试次已记账，等下一个刺激出现即可。'
        : '键盘空格或点击刺激区都能作答；反应时按刺激呈现到作答的间隔上报。' }),
    ])));
    slot.stage = stage;
    slot.timer = timer;
    return slot;
  };

  /**
   * 把行为任务区带到视口中央，**只在任务开始时做一次**。
   *
   * 为什么必须有：SART/PVT 靠键盘对"数字/红点"作答，而这个卡片排在
   * 「当前该做什么 + 总体进度 + 全部阶段」之后，在 1000px 高的视口里本来就落在折叠线以下
   * （实测卡片顶边 ≈931px）——刺激看不见，任务就没法做。试次到达时不再重复滚动，
   * 避免和操作者自己的滚动打架。
   */
  const revealTaskStage = (slot) => {
    const target = slot && (slot.stage || slot.host);
    if (target && target.scrollIntoView) {
      target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  };

  /**
   * 中途打开时补一次"未作答"上报，解锁停在等作答的服务端。
   *
   * 为什么必须补：后端 `_wait_trial`（core/runtime.py:500-509）先 `publish("trial")` 再
   * `wait_for_input("sart"/"pvt")`，试次是**客户端驱动**的；页面在试次已经呈现之后才打开，
   * 刺激无法重放，也就没人能作答 —— 服务端会一直等到 `INPUT_TIMEOUT_SEC`（900s，且不随
   * time_scale 缩放）才判失败，整段训练停摆。补上这一笔后，服务端立即发出下一试次，
   * 现有 `case 'trial'` 逻辑会正常渲染，操作者从下一试次起继续作答。
   *
   * 只上报"未作答"：不伪造反应时（`rt: null`），也不自造刺激数字。
   * `index`/`phase` 传 0/'main' 是安全的 —— 服务端用自己的序列
   * （runtime.py:526 `task.submit(phase=nxt["phase"], index=nxt["index"], ...)`），
   * 客户端序号只在 api/routes.py 里被 clamp 后丢弃；`rt` 为 null 时路由不做钳制（routes.py:753-755）。
   * 返回 409（`provide_input` 返回 False，该等待点已被消费）属正常竞态，静默忽略。
   */
  const skipPendingTrial = (task) => {    const body = task === 'sart'
      ? { phase: 'main', index: 0, responded: false, rt: null }
      : { index: 0, responded: false, rt: null, false_start: false };
    const request = task === 'sart' ? api.submitSartTrial(uuid, body) : api.submitPvtTrial(uuid, body);
    Promise.resolve(request).catch(() => { /* 已被消费 / 已不在该阶段：忽略即可 */ });
  };

  /**
   * 按服务端 `runtime.awaiting_input` 补建交互区。
   * 为什么需要：SSE 事件总线不重放历史事件，页面中途打开/刷新时收不到 `scale_request`，
   * 只有 `GET /api/sessions/{uuid}` 的 `runtime.awaiting_input`（后端 `core/runtime.py` 的
   * `wait_for_input("scales:SAS")`）知道"后端正在等谁作答"。
   * token 形如 `scales:SAS`，行为任务则是 `sart` / `pvt`（见 TASK_WAIT_KEYS 注释）。
   * 幂等：`ensureScaleSlot` 按 code、`ensureTaskSlot` 按 task 去重；行为任务的补偿上报
   * 在 `local.skippedTasks` 里记账，每个任务每次页面加载最多补一次，避免把后续真实试次吃掉。
   * 返回第一个被恢复槽位的宿主节点，供「开始作答 / 查看题目」按钮滚动定位。
   */
  const restoreInteraction = () => {
    if (!local.running || local.auto) return null;
    let firstHost = null;
    for (const token of local.awaitingInput) {
      const match = /^scales:([A-Za-z0-9_-]+)$/.exec(String(token || '').trim());
      if (match) {
        const code = match[1].toUpperCase();
        const slot = ensureScaleSlot(code, null, null, SCALE_INSTRUCTION_FALLBACK);
        firstHost = firstHost || slot.host;
        continue;
      }
      const task = String(token || '').split(':')[0].trim().toLowerCase();
      if (TASK_WAIT_KEYS.includes(task)) {
        const slot = ensureTaskSlot(task);
        firstHost = firstHost || slot.host;
        // 只在"这一页还没有任务区"时补渲染：否则点「开始作答 / 查看题目」会把屏幕上
        // 正在呈现的刺激换成占位符，操作者就不知道该不该按了。
        if (!slot.stage) renderTaskStage(task, { hint: RESTORE_HINT });
        if (!local.skippedTasks.has(task)) {
          local.skippedTasks.add(task);
          skipPendingTrial(task);
        }
        // 中途打开就是来参与的：把任务区带到眼前，否则刺激在折叠线以下看不见。
        revealTaskStage(slot);
      }
    }
    return firstHost;
  };

  /** 反应时（秒）：把服务端"事件→刺激"的延时扣掉，并做最小钳制避免负值。 */
  const reactionTime = (trial) => {
    const elapsed = (performance.now() - trial.onset) / 1000;
    const delay = trial.delay || 0;
    return Math.max(0.05, elapsed - delay);
  };

  /** 作答当前试次（空格键 / 点击刺激区 / 点「点击作答」按钮都走这里）。 */
  const answerCurrent = (trial) => {
    if (!trial || !trial.keyReady || trial.answer) return false;
    trial.answer = { responded: true, rt: reactionTime(trial) };
    submitTrial(trial);
    return true;
  };

  /** 空格作答：仅在 trial 已就绪（本试次已渲染刺激）时接受，杜绝跨试次串答。 */
  const onKeyDown = (event) => {
    if (event.code !== 'Space' && event.key !== ' ') return;
    // 输入类元素里允许正常输入空格
    const tag = (event.target && event.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    event.preventDefault();
    answerCurrent(local.trial);
  };
  window.addEventListener('keydown', onKeyDown);
  // 视图卸载时摘掉监听，避免其它视图误触发
  ctx.onCleanup(() => window.removeEventListener('keydown', onKeyDown));

  /* ------------------------------------------------------ 试次作答窗口 */
  /* 为什么必须有：试次是"服务端发一个、前端答一个"驱动的，而 SART 的 No-Go 试次
     （显示 3）**正确做法就是不按键**——没有这一步就永远没有提交，服务端会一直等到
     INPUT_TIMEOUT_SEC（900 秒），现场看到的就是"显示 3 之后整个任务不动了"。
     窗口长度由服务端随 trial 事件下发（response_window），前端不硬编码协议参数。 */
  const DEFAULT_TRIAL_WINDOW = { sart: 2.2, pvt: 3.0 };

  const clearTrialWindow = () => {
    if (local.trialTicker) {
      window.clearTimeout(local.trialTicker);
      local.trialTicker = null;
    }
  };

  const paintTimer = (trial, text) => {
    const slot = local.taskSlots && local.taskSlots.get(trial.task);
    if (slot && slot.timer) slot.timer.textContent = text;
  };

  const armTrialWindow = (trial) => {
    clearTrialWindow();
    const seconds = Number(trial.responseWindow) > 0
      ? Number(trial.responseWindow)
      : (DEFAULT_TRIAL_WINDOW[trial.task] || 2.2);
    const endsAt = performance.now() + seconds * 1000;
    const tick = () => {
      const current = local.trial;
      if (current !== trial) return;                    // 试次已被替换/结束
      const remain = (endsAt - performance.now()) / 1000;
      if (remain <= 0) {
        local.trialTicker = null;
        if (!current.answer && !current.posting) {
          current.answer = { responded: false, rt: null };   // 未按键：Go 记漏报、No-Go 记正确抑制
          submitTrial(current);                              // 传入本次试次：绝不误答刚来的下一试次
        }
        return;
      }
      paintTimer(current, `作答窗口剩余 ${fmtNum(remain, 1)} s`);
      local.trialTicker = window.setTimeout(tick, 100);
    };
    local.trialTicker = window.setTimeout(tick, 100);
  };
  ctx.onCleanup(clearTrialWindow);

  const submitTrial = async (expected) => {
    const trial = local.trial;
    // expected：窗口到期时把"我当时在答的那一个试次"传进来。若此刻 local.trial 已经是
    // 新试次，就**什么都不做**——否则会把"未作答"记到刚呈现的新试次头上，新试次随即被标记
    // 已答/被清空，被试再按空格或点击都没反应（用户反馈的"SART 前一个没答、后一个点不动"）。
    if (expected && trial !== expected) return;
    if (!trial || !trial.answer || trial.posting) return;
    trial.posting = true;
    trial.keyReady = false;
    clearTrialWindow();
    const { task, phase, index, answer } = trial;
    renderTaskStage(task, {
      digit: trial.digit,
      index: trial.index + 1,
      total: trial.total,
      phaseLabel: phase === 'practice' ? '练习' : '正式',
      hint: answer.responded ? `已记录：反应时 ${fmtNum(answer.rt, 3)} s` : '本试次未按键',
      answered: true,
    });
    // 注意顺序：renderTaskStage 会重建舞台（含计时节点），所以结果文本要在它之后写
    paintTimer(trial, answer.responded
      ? `已作答：反应时 ${fmtNum(answer.rt, 3)} s`
      : '本试次未按键（按"未作答/正确抑制"记账）');
    if (local.auto) {
      // 快速模式：后端已自行作答，前端不再 POST（避免重复提交与 409）
      if (local.trial === trial) local.trial = null;
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
      // **只能清掉"这一次"的试次**：服务端常常在 POST 返回之前就通过 SSE 推来了下一个试次，
      // 无条件 `local.trial = null` 会把刚到的下一试次抹掉，于是"前一个没答、后一个点不动"
      // （用户实测就是这个）。带 expected 的调用还要再校验一次身份。
      if (local.trial === trial) local.trial = null;
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
      renderStep();
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
        local.autoKnown = true;
        local.running = true;
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
          revealTaskStage(renderTaskStage('pvt', {
            hint: 'PVT-B：刺激出现后尽快按空格（试次数与刺激时刻表由接口下发）。',
          }));
        } else {
          revealTaskStage(renderTaskStage('sart', {
            hint: 'SART：看到 1–9 按空格；看到 3 不要按。等待首个刺激…',
          }));
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
          responseWindow: Number(payload.response_window) || null,
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
        if (payload.phase === 'done') {
          local.trial = null;
          clearTrialWindow();
        } else {
          armTrialWindow(local.trial);   // 窗口到点自动补"未作答"，服务端才能继续推进
        }
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
        ], rows), { sub: '漏报 / 虚报 / 反应时与变异' }));
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
        local.running = false;
        local.awaitingInput = [];
        local.trial = null;
        const status = payload.status;
        const session = { ...(ctx.store.state.currentSession || {}) };
        session.uuid = session.uuid || uuid;
        session.status = status;
        session.error = payload.error;
        ctx.store.setState({ currentSession: session });
        refresh();
        // 跑完这一刻指标汇总 / qc runs / 产物才齐：重取一次详情，「检测完成」卡才会显示真值而不是 —
        api.getSession(uuid).then((response) => {
          if (ctx.signal.aborted) return;
          local.detail = response.data || local.detail;
          renderStep();
        }).catch(() => { /* 详情取不到就用已有数据渲染 */ });
        tailHost.textContent = '';
        break;
      }
      default:
        break;
    }
  };

  /* -------------------------------------------------------- 初次加载 */
  refresh();

  // 从「开始检测」向导过来时带 ?subject=1：**自动进入被测者视图**——操作者点完"开始检测"
  // 就可以把电脑交给被测者，不用再教他点什么。
  if (ctx.params.get('subject') === '1') setSubjectMode(true);

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
    local.detail = data;                  // 「检测完成」卡要用 indicator_summary / runs(qc) / error
    const session = { ...data };
    if (data.session) Object.assign(session, data.session);
    session.uuid = uuid;
    ctx.store.setState({ currentSession: session });
    const runtime = pick(data, 'runtime', {}) || {};
    ctx.store.setState({
      sessionRuntime: { source_kind: runtime.source_kind, source_note: pick(data, 'source_note', null) },
      alerts: list(pick(data, 'alerts', [])),
    });
    // 中途打开/刷新页面：SSE 不重放历史事件，收不到已经过去的 scale_request，
    // 只能按后端"正在等谁作答"（runtime.awaiting_input）补建交互区，否则页面上没有作答界面。
    local.running = data.status === 'running';
    local.awaitingInput = list(pick(runtime, 'awaiting_input', []));
    if (!local.autoKnown) {
      // started 快照可能还没到达；先按后端同一口径（time_scale < 0.2 → 自动作答）推一版，
      // 避免快速演示模式下中途打开页面弹出不该出现的作答表单。
      const scaleValue = Number(pick(session, 'time_scale', NaN));
      if (Number.isFinite(scaleValue)) local.auto = scaleValue < 0.2;
    }
    restoreInteraction();
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
    // 不再单列「会话当前状态」卡：结束态现在由上面那张「检测完成 / 检测未完成」卡统一承担
    // （指标 + 采集质量 + 量表 + 报告/产物/训练/再测一次），重复一张卡只会让页面更长。
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
        renderStep();
      }
    },
    onError: () => {
      // 断线兜底：切到 10 秒轮询 /live，恢复后由 onOpen 切回
      if (!local.offline) {
        local.offline = true;
        renderStep();
      }
    },
  });

  // 每秒刷新一次"已用 / 约剩"（只改那一行，见 tickStep；页面不可见时不刷）
  const ticker = window.setInterval(tickStep, 1000);

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
