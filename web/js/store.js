/**
 * 全局状态与发布订阅。
 *
 * 为什么不用框架：这是零依赖的静态前端，视图需要共享的只有"配置 / 健康信息 / 当前会话 /
 * 被试列表"几项，一个扁平对象 + 订阅表足够；视图销毁时退订即可，不会互相污染。
 */

export const store = {
  /** 全局状态；视图只读，写入统一走 setState 以便广播。 */
  state: {
    config: null,            // GET /api/config
    health: null,            // GET /api/health
    overview: null,          // GET /api/overview
    subjects: [],            // 被试列表缓存
    currentSubject: null,    // 当前选中的被试 public_id
    currentSession: null,    // 当前会话语义对象（session_public 结构）
    sessionRuntime: null,    // 当前会话 runtime / source 信息
    phases: [],              // 阶段定义（来自 POST /api/sessions 或 config.phases）
    lastEvent: null,         // 最近一条 SSE 事件（供顶栏与视图观察）
    alerts: [],              // 最近预警缓存
  },

  _listeners: new Map(),

  /**
   * 订阅一个或多个 key（'*' 表示全部）。
   * @returns {() => void} 退订函数
   */
  subscribe(keys, callback) {
    const list = Array.isArray(keys) ? keys : [keys];
    for (const key of list) {
      if (!this._listeners.has(key)) this._listeners.set(key, new Set());
      this._listeners.get(key).add(callback);
    }
    return () => {
      for (const key of list) {
        const set = this._listeners.get(key);
        if (set) set.delete(callback);
      }
    };
  },

  /** 批量合并状态并通知订阅者；值为 undefined 时跳过（便于局部刷新）。 */
  setState(patch, options = {}) {
    const changed = [];
    for (const [key, value] of Object.entries(patch || {})) {
      if (value === undefined) continue;
      if (this.state[key] === value) continue;
      this.state[key] = value;
      changed.push(key);
    }
    if (!changed.length && !options.force) return changed;
    const notified = new Set();
    for (const key of changed.concat(['*'])) {
      const set = this._listeners.get(key);
      if (!set) continue;
      for (const callback of set) {
        if (notified.has(callback)) continue;
        notified.add(callback);
        try {
          callback(this.state, key);
        } catch (error) {
          // 一个订阅者抛错不应中断其它订阅者
          console.error('[store] 订阅回调异常', error);
        }
      }
    }
    return changed;
  },

  /** 切换会话/被试时清理上一条会话的实时缓存，避免串号。 */
  resetSession() {
    this.setState({ currentSession: null, sessionRuntime: null, phases: [], alerts: [], lastEvent: null });
  },
};

/* ------------------------------------------------------------------ 提示条 */

/**
 * 可读错误提示：不用弹窗（alert/confirm/prompt 一律禁止）。
 * @param {string} message
 * @param {'error'|'info'} level
 */
export function toast(message, level = 'error') {
  const host = document.getElementById('toast-host');
  if (!host) return;
  const node = document.createElement('div');
  node.className = `toast toast--${level}`;
  node.setAttribute('role', level === 'error' ? 'alert' : 'status');
  node.textContent = message;
  host.append(node);
  // 提示条自动消失，但保证屏幕阅读器有时间读完
  window.setTimeout(() => {
    node.remove();
  }, level === 'error' ? 9000 : 5000);
}

/** 统一把异常转成用户能读懂的一句话。 */
export function describeError(error) {
  if (!error) return '未知错误';
  if (error.name === 'ApiError') {
    if (error.status === 0) return error.message;
    return `${error.message}（HTTP ${error.status}${error.code ? ` / ${error.code}` : ''}）`;
  }
  return error.message || String(error);
}

export default store;
