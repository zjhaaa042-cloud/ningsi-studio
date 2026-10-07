/**
 * 入口：hash 路由、全局状态、顶栏接线、导航渲染。
 *
 * 关键设计：
 * - 每个视图统一导出 `render(container, ctx)`；ctx 提供 navigate/reload/params/signal/store/api，
 *   并用 onCleanup 注册销毁函数（SSE、定时器、键盘监听都在这里释放）；
 * - 路由切换时 abort 上一轮的 fetch（AbortController），避免慢响应写到新页面上；
 * - 顶栏数据来源徽标取 /api/health 与 `started` 事件的 source_kind / source_note，
 *   仿真源必须显式显示"数据来源：仿真"。
 */

import { api } from './api.js';
import { describeError, store, toast } from './store.js';
import { button, el, fmtInt, list, pick, statusText } from './util.js';

import * as dashboard from './views/dashboard.js';
import * as start from './views/start.js';
import * as subjects from './views/subjects.js';
import * as flow from './views/flow.js';
import * as live from './views/live.js';
import * as training from './views/training.js';
import * as history from './views/history.js';
import * as report from './views/report.js';
import * as panels from './views/panels.js';

// 「开始检测」放第一个：这是一个"跑一次检测"的产品，第一次用的人应该先看到它，
// 而不是先看到仪表盘/被试/设备这些零件。
const ROUTES = [
  { path: '#/start', label: '开始检测', module: start },
  { path: '#/dashboard', label: '仪表盘', module: dashboard },
  { path: '#/subjects', label: '被试管理', module: subjects },
  { path: '#/flow', label: '会话流程', module: flow },
  { path: '#/live', label: '实时监测', module: live },
  { path: '#/training', label: '训练视图', module: training },
  { path: '#/history', label: '历史与趋势', module: history },
  { path: '#/report', label: '评估报告', module: report },
  { path: '#/models', label: '模型训练', module: panels.models },
  { path: '#/devices', label: '设备状态', module: panels.devices },
];

const DEFAULT_ROUTE = '#/start';

/** 应用级运行时：路由、清理函数、当前控制器。 */
const app = {
  route: null,
  controller: null,
  cleanups: [],
  viewContainer: null,
  navList: null,
};

/* ------------------------------------------------------------------ 路由 */

function parseHash() {
  const raw = window.location.hash || DEFAULT_ROUTE;
  const [path, search] = raw.split('?');
  return {
    path: ROUTES.some((route) => route.path === path) ? path : DEFAULT_ROUTE,
    params: new URLSearchParams(search || ''),
  };
}

function runCleanups() {
  for (const cleanup of app.cleanups.splice(0)) {
    try {
      cleanup();
    } catch (error) {
      console.error('[main] 清理失败', error);
    }
  }
  if (app.controller) app.controller.abort();
}

function highlightNav(path) {
  for (const link of app.navList.querySelectorAll('a')) {
    if (link.getAttribute('href') === path) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
}

function navigate(hash) {
  if (window.location.hash === hash) {
    renderRoute();
    return;
  }
  window.location.hash = hash;
}

async function renderRoute() {
  const { path, params } = parseHash();
  runCleanups();
  app.controller = new AbortController();
  app.route = path;
  highlightNav(path);

  const route = ROUTES.find((item) => item.path === path) || ROUTES[0];
  document.title = `${route.label} · 凝思 Studio`;

  const ctx = {
    store,
    api,
    params,
    signal: app.controller.signal,
    navigate,
    reload: () => renderRoute(),
    onCleanup: (fn) => {
      if (typeof fn === 'function') app.cleanups.push(fn);
    },
  };

  app.viewContainer.textContent = '';
  try {
    await route.module.render(app.viewContainer, ctx);
  } catch (error) {
    console.error('[main] 视图渲染失败', error);
    app.viewContainer.textContent = '';
    app.viewContainer.append(el('p', { class: 'empty', text: `视图渲染失败：${describeError(error)}` }));
    toast(describeError(error));
  }
}

/* ------------------------------------------------------------------ 导航 */

function renderNav() {
  app.navList.textContent = '';
  for (const route of ROUTES) {
    app.navList.append(el('li', {}, [
      el('a', { class: 'sidenav__link', href: route.path, text: route.label }),
    ]));
  }
}

/* ------------------------------------------------------------------ 顶栏 */

/** 设备键 → 数据源类别：lsl:xxx 是实时设备，sim / sim-bsense 是仿真（source_kind 缺失时的兜底）。 */
function kindFromKey(key) {
  const raw = String(key || '').toLowerCase();
  if (!raw) return null;
  if (raw.startsWith('lsl:')) return 'lsl';
  if (raw === 'sim' || raw.startsWith('sim-')) return 'sim';
  return null;
}

/** 数据来源徽标：仿真源必须写明"数据来源：仿真"。 */
function sourceText(kind, note, key) {
  // /api/health 那条路径上 source_kind 可能为空：按设备键的命名约定兜底，
  // 否则历史会话会退化成显示设备型号（"数据来源：sim-bsense"）
  const effective = kind || kindFromKey(key);
  if (!effective) return key ? `数据来源：${key}${note ? `（${note}）` : ''}` : '数据来源：—';
  const normalized = String(effective).toLowerCase();
  if (normalized === 'sim') return '数据来源：仿真';
  if (normalized === 'lsl') return `数据来源：实时设备（${key || 'LSL'}）`;
  return `数据来源：${effective}${note ? `（${note}）` : ''}`;
}

function updateHeader() {
  const state = store.state;
  const sourceBadge = document.getElementById('source-badge');
  const engineBadge = document.getElementById('engine-badge');
  const deviceBadge = document.getElementById('device-badge');
  const sessionBadge = document.getElementById('session-state');
  const progressBar = document.getElementById('session-progress-bar');
  const progressHost = document.getElementById('session-progress');
  const percentNode = document.getElementById('session-percent');

  const health = state.health || {};
  const config = state.config || {};
  const runtime = state.sessionRuntime || {};
  const session = state.currentSession || {};

  // 数据来源：会话运行时的 source_kind 优先（更贴近当前会话），否则用 /api/health 的概览。
  // 检测不到实时源时概览里的 source/source_kind 都是 null —— 此时必须显示「无信号」，
  // **不能**回落成"仿真"（产品规则：没有脑机信号就不使用仿真，仿真只能被显式选择）。
  const kind = runtime.source_kind || pick(health, 'overview.source_kind', null) || null;
  const note = runtime.source_note || pick(health, 'overview.source_note', null);
  const deviceKey = pick(session, 'device', null) || pick(health, 'overview.source', null);
  // 历史已结束会话的 runtime.source_kind 可能是空的：按设备键兜底判定类别，
  // 徽标文案与"仿真高亮"用同一个 effectiveKind，避免一个说仿真、一个不高亮
  const effectiveKind = kind || kindFromKey(deviceKey);
  const noSignal = !effectiveKind && !deviceKey && !session.uuid;
  const label = noSignal ? '数据来源：无信号' : sourceText(kind, note, deviceKey);
  sourceBadge.textContent = label;
  sourceBadge.title = noSignal
    ? '未检测到 LSL 脑电信号。请到「设备状态」页启动采集端后重试；仅演示时可在建会话时显式选择仿真源 sim-bsense'
    : (note || '数据来源标注（仿真数据会在界面与报告中显式标注）');
  sourceBadge.classList.toggle('badge--strong', noSignal
    || String(effectiveKind || '').toLowerCase() === 'sim');

  // 引擎口径（product / spectrum=welch-v1 …）**不在顶栏显示**：用户明确说"口径是什么没必要显示"。
  // 需要时仍可查：仪表盘「运行环境」卡的 hover、评估报告正文与导出文档、GET /api/config。
  // 元素保留（验收/其它视图可能引用 id），内容只放进 title。
  const specs = pick(health, 'engine', null) || pick(config, 'specs', {}) || {};
  engineBadge.hidden = true;
  engineBadge.textContent = '';
  engineBadge.title = `引擎口径：${Object.entries(specs).map(([key, value]) => `${key}=${value}`).join(' / ') || '—'}`;

  // 设备徽标：有设备体检数据时显示"在收数 / 已停/掉线 + 实测采样率"，
  // 现场一眼能看出是设备掉了还是信号正常（仿真源直接标注为仿真）
  const deviceInfo = (Array.isArray(state.deviceStatus) ? state.deviceStatus : [])
    .find((item) => !session.uuid || item.session === session.uuid) || null;
  if (deviceInfo && deviceInfo.kind === 'lsl') {
    const live = deviceInfo.live === true;
    const rate = deviceInfo.observed_srate ? `${Number(deviceInfo.observed_srate).toFixed(0)} Hz` : '—';
    const age = deviceInfo.seconds_since_last === null || deviceInfo.seconds_since_last === undefined
      ? '—' : `${Number(deviceInfo.seconds_since_last).toFixed(1)}s`;
    deviceBadge.textContent = `设备：${deviceInfo.device || deviceKey || '—'}｜${live ? '在收数' : '已停/掉线'}｜${rate}`;
    deviceBadge.title = `真实 LSL 流｜实测 ${rate}｜缓冲 ${fmtInt(deviceInfo.buffered_samples)} 个样本`
      + `｜最近样本 ${age} 前` + (Object.keys(deviceInfo.stream_errors || {}).length
        ? `｜错误：${JSON.stringify(deviceInfo.stream_errors)}` : '');
    deviceBadge.classList.toggle('badge--strong', !live);
  } else {
    // 没有设备键就是"没检测到信号"：写清楚，不要只给一个 "—"（看起来像坏了）
    deviceBadge.textContent = `设备：${deviceKey || '未检测到信号'}`;
    deviceBadge.title = deviceKey
      ? `采样率 ${pick(session, 'srate', '—')} Hz｜通道 ${pick(session, 'channels', '—')}`
      : '未检测到 LSL 脑电设备；到「设备状态」页可查看接入指引与实时预览';
    deviceBadge.classList.toggle('badge--strong', noSignal);
  }

  if (!session.uuid) {
    sessionBadge.textContent = '会话：未选择';
    progressBar.style.width = '0%';
    percentNode.textContent = '—';
    progressHost.setAttribute('aria-valuenow', '0');
    return;
  }
  const progress = Number(pick(session, 'progress', 0)) || 0;
  const percent = Math.max(0, Math.min(100, progress * 100));
  sessionBadge.textContent = `会话：${statusText(pick(session, 'status', null))}｜${pick(session, 'phase_label', null) || pick(session, 'phase', null) || '—'}`;
  sessionBadge.title = `uuid ${session.uuid}`;
  progressBar.style.width = `${percent.toFixed(1)}%`;
  percentNode.textContent = `${percent.toFixed(0)}%`;
  progressHost.setAttribute('aria-valuenow', percent.toFixed(0));
}

/* ------------------------------------------------------------------ 启动 */

async function loadConfig() {
  try {
    const response = await api.config();
    store.setState({ config: response.data });
    // 阶段定义来自 config.phases，供流程视图在没有 POST 响应时兜底
    if (!list(store.state.phases).length) store.setState({ phases: list(pick(response.data, 'phases', [])) });
  } catch (error) {
    toast(`引擎配置加载失败：${describeError(error)}`);
  }
}

async function loadHealth() {
  try {
    const response = await api.health();
    store.setState({ health: response.data });
  } catch (error) {
    toast(`服务状态获取失败：${describeError(error)}`);
  }
}

/** 设备体检：有会话在跑时每 5 秒刷新一次，用于顶栏显示"在收数 / 已停/掉线" */
async function loadDeviceStatus() {
  try {
    const response = await api.deviceStatus();
    store.setState({ deviceStatus: list(pick(response.data, 'devices', [])) });
  } catch (error) {
    // 诊断接口失败不阻塞界面
    console.warn('[main] 设备状态获取失败', error);
  }
}

async function loadSubjects() {
  try {
    const response = await api.listSubjects({ limit: 100, page: 1 });
    store.setState({ subjects: list(pick(response.data, 'items', [])) });
  } catch (error) {
    // 被试列表失败不影响其它视图，静默记录即可
    console.warn('[main] 被试列表加载失败', error);
  }
}

function boot() {
  app.viewContainer = document.getElementById('view');
  app.navList = document.getElementById('nav-list');
  if (!app.viewContainer || !app.navList) {
    console.error('[main] 页面骨架缺少 #view 或 #nav-list');
    return;
  }
  renderNav();

  store.subscribe(['health', 'config', 'currentSession', 'sessionRuntime', 'deviceStatus'],
    updateHeader);
  updateHeader();

  window.addEventListener('hashchange', renderRoute);
  if (!window.location.hash) window.location.hash = DEFAULT_ROUTE;
  renderRoute();

  loadConfig();
  loadSubjects();
  loadHealth().then(updateHeader);
  loadDeviceStatus().then(updateHeader);
  // 顶栏的来源徽标、设备体检与并发状态需要定期刷新（SSE 只在会话内推送）
  window.setInterval(() => {
    if (!document.hidden) loadHealth();
  }, 60000);
  window.setInterval(() => {
    if (!document.hidden) loadDeviceStatus();
  }, 5000);

  const helpButton = button('接口自检', async () => {
    try {
      const response = await api.openapi();
      const paths = Object.keys(pick(response.data, 'paths', {}) || {});
      toast(`后端共有 ${paths.length} 个 /api 端点可用`, 'info');
    } catch (error) {
      toast(describeError(error));
    }
  }, { small: true });
  document.querySelector('.sidenav__foot')?.prepend(el('div', { class: 'row', style: 'margin-bottom:8px' }, [
    helpButton,
    el('span', { class: 'muted', text: `${fmtInt(ROUTES.length)} 个视图` }),
  ]));
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
