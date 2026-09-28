/**
 * EventSource 封装。
 *
 * 为什么还需要一层封装：
 * 1. 后端每条事件都是 `event: <type>` 具名事件，`onmessage` 收不到，必须逐个 `addEventListener`，
 *    这里集中登记 docs/API.md 表格中出现过的全部事件名，避免视图里漏注册；
 * 2. 断线后浏览器会自动重连并自行携带 `Last-Event-ID`，但业务侧仍需知道"已断开"以便切到
 *    轮询兜底，因此把 onopen/onerror 一并暴露出去（断线原因不对用户报错，由视图提示兜底状态）。
 */

/** docs/API.md 事件表 + 服务端实际会发的补充事件（started/scales/closed）。 */
export const EVENT_TYPES = [
  'started', 'notice', 'phase', 'progress', 'quality', 'baseline', 'window',
  'scale_request', 'scale_scored', 'behavior_request', 'trial', 'behavior',
  'monitor', 'training_start', 'feedback', 'segment', 'assessment', 'model',
  'artifacts', 'cancelled', 'error', 'finished', 'scales', 'closed',
];

/**
 * EventSource 无法自定义请求头，因此令牌只能走查询参数（后端支持 `?token=`）。
 * 未配置令牌时返回空串，URL 不带额外参数。
 */
function apiToken() {
  const node = document.querySelector('meta[name="ningsi-api-token"]');
  return node && node.content ? node.content.trim() : '';
}

/**
 * 建立会话事件流。
 * @param {string} uuid 会话 uuid
 * @param {object} options
 * @param {(type: string, payload: object, event: MessageEvent) => void} options.onEvent 业务负载在内层 data 字段
 * @param {() => void} [options.onOpen] 首次打开或重连成功
 * @param {(error: Event, info: {opened: boolean, state: number}) => void} [options.onError] 连接异常（含自动重连中）
 */
export function connectSessionEvents(uuid, options = {}) {
  const { onEvent = () => {}, onOpen = () => {}, onError = () => {} } = options;
  const token = apiToken();
  const url = `/api/sessions/${uuid}/events${token ? `?token=${encodeURIComponent(token)}` : ''}`;
  const source = new EventSource(url);
  const handlers = new Map();
  let opened = false;
  let closed = false;

  const dispatch = (type) => (event) => {
    let payload = {};
    try {
      payload = event.data ? JSON.parse(event.data) : {};
    } catch (error) {
      // 单条事件解析失败不应影响整条流（服务端续传时可能截断）
      payload = { type, parse_error: true, raw: event.data };
    }
    onEvent(type, payload, event);
  };

  for (const type of EVENT_TYPES) {
    const handler = dispatch(type);
    handlers.set(type, handler);
    source.addEventListener(type, handler);
  }

  source.onopen = () => {
    opened = true;
    onOpen();
  };

  source.onerror = (error) => {
    // readyState 为 CONNECTING 表示浏览器正在自动重连；不为用户弹错，只上报状态
    onError(error, { opened, state: source.readyState });
  };

  // 后端在会话不是运行中时会先补发快照再关流，此时 readyState 会落到 CLOSED
  source.addEventListener('closed', () => {
    if (source.readyState === 2) source.close();
  });

  return {
    get state() {
      return source.readyState;
    },
    get opened() {
      return opened;
    },
    close() {
      if (closed) return;
      closed = true;
      for (const [type, handler] of handlers) source.removeEventListener(type, handler);
      source.close();
    },
  };
}

/**
 * 建立**高频原始信号**流（用于实时波形与频谱，与 4 秒分析窗解耦）。
 *
 * 与 `connectSessionEvents` 的区别：这条流只有一个具名事件 `signal`，
 * 事件内容是多通道抽稀后的采样点与频谱；服务端按 `hz`（默认 10 FPS）推帧，
 * 因此界面可以做到接近厂家软件的刷新手感。
 *
 * 失败语义：会话不在运行中时服务端返回 409，此时 EventSource 会反复重连——
 * 视图必须通过 onError 判断并按需降级（例如改回轮询或提示"会话未运行"）。
 *
 * @param {string} uuid 会话 uuid
 * @param {object} options
 * @param {(frame: object) => void} options.onFrame 每帧信号
 * @param {() => void} [options.onOpen]
 * @param {(error: Event, info: {opened: boolean, state: number}) => void} [options.onError]
 * @param {number} [options.hz] 期望刷新率（1–25，默认 10）
 * @param {number} [options.seconds] 每帧时间跨度（默认 10 秒）
 * @param {number} [options.points] 每通道最大点数（默认 1200）
 */
export function connectSignalStream(uuid, options = {}) {
  const {
    onFrame = () => {}, onOpen = () => {}, onError = () => {},
    hz = 10, seconds = 10, points = 1200,
  } = options;
  const token = apiToken();
  const params = new URLSearchParams({
    hz: String(hz), seconds: String(seconds), points: String(points),
  });
  if (token) params.set('token', token);
  const url = `/api/sessions/${uuid}/signal?${params.toString()}`;
  const source = new EventSource(url);
  let opened = false;
  let closed = false;

  const handler = (event) => {
    try {
      onFrame(event.data ? JSON.parse(event.data) : {});
    } catch (error) {
      // 单帧损坏不应影响整条流
      console.warn('[sse] 信号帧解析失败', error);
    }
  };
  source.addEventListener('signal', handler);

  source.onopen = () => {
    opened = true;
    onOpen();
  };
  source.onerror = (error) => {
    onError(error, { opened, state: source.readyState });
  };

  return {
    get state() {
      return source.readyState;
    },
    get opened() {
      return opened;
    },
    close() {
      if (closed) return;
      closed = true;
      source.removeEventListener('signal', handler);
      source.close();
    },
  };
}

export default connectSessionEvents;
