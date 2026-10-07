/**
 * 接口封装：路径与 docs/API.md 一一对应，不做任何前端推断。
 *
 * 统一行为：
 * - 默认 15 秒超时（AbortController），超时与断网都转成可读中文提示；
 * - 非 2xx 一律抛出 ApiError，携带 HTTP 状态、后端错误 `code/message/detail`；
 * - 202 视为正常响应（例如报告尚未生成时会返回 202 + partial: true）。
 */

const DEFAULT_TIMEOUT = 15000;

/**
 * 可选令牌：后端用 `--token` 启动时所有 /api 都需要 `X-API-Token`。
 * 令牌来源有二（按优先级）：
 * 1) index.html 的 `<meta name="ningsi-api-token">`（部署时注入，默认空）；
 * 2) 当前地址的 `?token=...`（docs/API.md 336 行给出的另一种方式，也方便人工排查）。
 * 第 2 种命中的令牌缓存到模块内存，避免同一次会话里每请求都解析一遍地址。
 */
let urlToken = '';

function apiToken() {
  const node = document.querySelector('meta[name="ningsi-api-token"]');
  const meta = node ? (node.getAttribute('content') || '').trim() : '';
  if (meta) return meta;
  if (!urlToken) {
    try {
      urlToken = (new URLSearchParams(window.location.search).get('token') || '').trim();
    } catch (error) {
      urlToken = '';
    }
  }
  return urlToken;
}

/** 401/403 的统一可读文案：告诉使用者"令牌该怎么给"，并保留后端 message 供追溯。 */
const TOKEN_HINT = '服务开启了访问令牌（--token）：请在地址后加 ?token=你的令牌，或联系管理员。';

function unauthorizedError(response, payload, path) {
  const info = (payload && payload.error) || {};
  const reason = info.message ? `服务端返回：${info.message}` : `接口返回 ${response.status}`;
  return new ApiError(`${TOKEN_HINT}（${reason}）`, {
    status: response.status,
    code: info.code || (response.status === 403 ? 'forbidden' : 'unauthorized'),
    detail: info.detail || null,
    payload,
    tokenIssue: true,
    path,
  });
}

export class ApiError extends Error {
  constructor(message, { status = 0, code = '', detail = null, payload = null, tokenIssue = false, path = '' } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.payload = payload;
    /** 是否为"访问令牌缺失/错误"类失败（401/403）：视图据此显示空态而不是当成业务 404。 */
    this.tokenIssue = tokenIssue;
    this.path = path;
  }

  /** 便于视图区分"正常业务分支"（如 404 未生成、409 未到阶段）与真实故障。 */
  is(status) {
    return this.status === status;
  }
}

function buildQuery(params) {
  if (!params) return '';
  const usable = Object.entries(params).filter(([, value]) => value !== undefined
    && value !== null && value !== '');
  if (!usable.length) return '';
  const search = new URLSearchParams();
  for (const [key, value] of usable) search.set(key, String(value));
  return `?${search.toString()}`;
}

async function request(method, path, { body, params, timeout = DEFAULT_TIMEOUT, accept } = {}) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeout);
  const headers = { Accept: accept || 'application/json' };
  const token = apiToken();
  if (token) headers['X-API-Token'] = token;
  const init = { method, headers, signal: controller.signal };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json; charset=utf-8';
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path + buildQuery(params), init);
  } catch (error) {
    if (error && error.name === 'AbortError') {
      throw new ApiError(`请求超时（${Math.round(timeout / 1000)} 秒）：${path}`, { status: 0, code: 'timeout' });
    }
    throw new ApiError('无法连接后端服务，请确认服务是否正在运行。', { status: 0, code: 'network' });
  } finally {
    window.clearTimeout(timer);
  }

  const contentType = response.headers.get('content-type') || '';
  let payload = null;
  if (contentType.includes('application/json')) {
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
  } else if (response.status >= 400) {
    // 非 JSON 的错误体（例如 405）保留文本，便于排查
    try {
      payload = { error: { code: 'internal_error', message: await response.text() } };
    } catch (error) {
      payload = null;
    }
  }

  if (!response.ok) {
    // 令牌类失败单独聚合：401/403 只弹一句后端原文，使用者不知道该做什么（task-5 口径）。
    if (response.status === 401 || response.status === 403) {
      throw unauthorizedError(response, payload, path);
    }
    const info = (payload && payload.error) || {};
    throw new ApiError(info.message || `接口返回 ${response.status}`, {
      status: response.status,
      code: info.code || 'internal_error',
      detail: info.detail || null,
      payload,
      path,
    });
  }
  return { status: response.status, data: payload };
}

const get = (path, params, options) => request('GET', path, { ...options, params });
const post = (path, body, options) => request('POST', path, { ...options, body: body === undefined ? {} : body });
const del = (path, options) => request('DELETE', path, { ...options });

/* ------------------------------------------------------------------ 基础 */
export const api = {
  health: () => get('/api/health'),
  config: () => get('/api/config'),
  devices: (probe) => get('/api/devices', { probe }),
  deviceStatus: () => get('/api/devices/status'),
  // 无会话的设备实时预览：状态 / 一窗波形 / 启动 / 停止
  devicePreview: () => get('/api/devices/preview'),
  devicePreviewWindow: () => get('/api/devices/preview', { window: 1 }),
  startDevicePreview: (source) => post('/api/devices/preview', { source: source || null }),
  stopDevicePreview: () => del('/api/devices/preview'),
  overview: () => get('/api/overview'),
  openapi: () => get('/api/openapi.json'),

  /* -------------------------------------------------------------- 被试 */
  listSubjects: (params) => get('/api/subjects', params),
  createSubject: (body) => post('/api/subjects', body),
  getSubject: (publicId) => get(`/api/subjects/${encodeURIComponent(publicId)}`),
  patchSubject: (publicId, body) => request('PATCH', `/api/subjects/${encodeURIComponent(publicId)}`,
    { body }),
  subjectSessions: (publicId, params) => get(`/api/subjects/${encodeURIComponent(publicId)}/sessions`, params),

  /* -------------------------------------------------------------- 会话 */
  createSession: (body) => post('/api/sessions', body),
  listSessions: (params) => get('/api/sessions', params),
  getSession: (uuid) => get(`/api/sessions/${uuid}`),
  cancelSession: (uuid) => request('DELETE', `/api/sessions/${uuid}`),
  sessionLive: (uuid) => get(`/api/sessions/${uuid}/live`),
  sessionHeatmap: (uuid) => get(`/api/sessions/${uuid}/heatmap`),
  sessionTrend: (uuid, params) => get(`/api/sessions/${uuid}/trend`, params),

  /* -------------------------------------------------------------- 量表 */
  listScales: () => get('/api/scales'),
  scaleDefinition: (code) => get(`/api/scales/${encodeURIComponent(code)}`),
  submitScale: (uuid, code, responses) => post(`/api/sessions/${uuid}/scales/${encodeURIComponent(code)}`,
    { responses }),

  /* ---------------------------------------------------------- 行为任务 */
  sartSequence: (uuid) => get(`/api/sessions/${uuid}/behaviors/sart/sequence`),
  submitSartTrial: (uuid, body) => post(`/api/sessions/${uuid}/behaviors/sart/trial`, body),
  pvtSequence: (uuid) => get(`/api/sessions/${uuid}/behaviors/pvt/sequence`),
  submitPvtTrial: (uuid, body) => post(`/api/sessions/${uuid}/behaviors/pvt/trial`, body),
  behaviorResult: (uuid, task) => get(`/api/sessions/${uuid}/behaviors/${task}/result`),

  /* ------------------------------------------------------ 评估/训练/报告 */
  assessment: (uuid) => get(`/api/sessions/${uuid}/assessment`),
  training: (uuid) => get(`/api/sessions/${uuid}/training`),
  report: (uuid) => get(`/api/sessions/${uuid}/report`),
  artifacts: (uuid) => get(`/api/sessions/${uuid}/artifacts`),
  artifactUrl: (uuid, kind) => `/api/sessions/${uuid}/artifacts/${encodeURIComponent(kind)}`,
  exportZipUrl: (uuid) => `/api/sessions/${uuid}/export.zip`,

  /* ------------------------------------------------------- 模型与趋势 */
  trainModel: (body) => post('/api/models/train', body),
  reportsTrend: (params) => get('/api/reports/trend', params),
};

export default api;

/** SSE 地址（大屏/实时视图用）。 */
export function eventsUrl(uuid, lastEventId) {
  const base = `/api/sessions/${uuid}/events`;
  return lastEventId ? `${base}?last_event_id=${encodeURIComponent(lastEventId)}` : base;
}
