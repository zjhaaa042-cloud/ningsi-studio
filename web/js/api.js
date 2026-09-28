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
 * 令牌由后端注入到 index.html 的 meta 标签；未注入时为空，不影响默认（无令牌）部署。
 */
function apiToken() {
  const node = document.querySelector('meta[name="ningsi-api-token"]');
  return node ? (node.getAttribute('content') || '').trim() : '';
}

export class ApiError extends Error {
  constructor(message, { status = 0, code = '', detail = null, payload = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.payload = payload;
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
    const info = (payload && payload.error) || {};
    throw new ApiError(info.message || `接口返回 ${response.status}`, {
      status: response.status,
      code: info.code || 'internal_error',
      detail: info.detail || null,
      payload,
    });
  }
  return { status: response.status, data: payload };
}

const get = (path, params, options) => request('GET', path, { ...options, params });
const post = (path, body, options) => request('POST', path, { ...options, body: body === undefined ? {} : body });

/* ------------------------------------------------------------------ 基础 */
export const api = {
  health: () => get('/api/health'),
  config: () => get('/api/config'),
  devices: (probe) => get('/api/devices', { probe }),
  deviceStatus: () => get('/api/devices/status'),
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
