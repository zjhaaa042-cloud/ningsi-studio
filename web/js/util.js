/**
 * 通用工具：格式化、DOM 帮助函数、极简 Markdown 渲染。
 *
 * 为什么统一在这里做格式化：接口对 NaN/Inf 一律返回 null（表示该窗不可用），
 * 若散落到各视图里做 `Number(x).toFixed()` 就会显示 "NaN"，因此所有数值都必须
 * 经过本模块的 fmt* 函数，统一降级为 "—"。
 */

/** 所有缺失值的统一占位符。 */
export const DASH = '—';

function isNil(value) {
  return value === null || value === undefined || value === '' || Number.isNaN(value);
}

function toNumber(value) {
  if (isNil(value)) return null;
  // 数值或纯数字字符串都接受；其余（如 'abc'）视为缺失
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** 数字格式化：`fmtNum(v, 2)` → "0.51"；缺失 → "—"。 */
export function fmtNum(value, digits = 2) {
  const number = toNumber(value);
  if (number === null) return DASH;
  return number.toFixed(digits);
}

/** 带符号数字（趋势、z 值用）：`+0.32` / `-0.10`。 */
export function fmtSigned(value, digits = 2) {
  const number = toNumber(value);
  if (number === null) return DASH;
  return (number >= 0 ? '+' : '') + number.toFixed(digits);
}

/** 整数格式化。 */
export function fmtInt(value) {
  const number = toNumber(value);
  return number === null ? DASH : String(Math.round(number));
}

/** 百分比：接口给 0–1 的小数。 */
export function fmtPercent(value, digits = 0) {
  const number = toNumber(value);
  if (number === null) return DASH;
  return `${(number * 100).toFixed(digits)}%`;
}

/** 秒 → 便于阅读的时长：`83.4` → "1:23.4"。 */
export function fmtDuration(seconds) {
  const number = toNumber(seconds);
  if (number === null) return DASH;
  const sign = number < 0 ? '-' : '';
  const total = Math.abs(number);
  const minutes = Math.floor(total / 60);
  const rest = total - minutes * 60;
  if (minutes === 0) return `${sign}${rest.toFixed(1)}s`;
  return `${sign}${minutes}:${rest < 10 ? '0' : ''}${rest.toFixed(1)}`;
}

/** 波形时间轴用：相对秒 → "12.0s"。 */
export function fmtSeconds(value, digits = 1) {
  const number = toNumber(value);
  return number === null ? DASH : `${number.toFixed(digits)}s`;
}

/** 字节数。 */
export function fmtBytes(value) {
  const number = toNumber(value);
  if (number === null) return DASH;
  const units = ['B', 'KB', 'MB', 'GB'];
  let index = 0;
  let size = number;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${index === 0 ? size : size.toFixed(1)} ${units[index]}`;
}

function parseDate(value) {
  if (isNil(value)) return null;
  // 后端时间为 ISO（含时区）；个别历史值是 sqlite 的 "YYYY-MM-DD HH:MM:SS"（UTC），补 Z
  const text = String(value);
  const normalized = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(text) ? `${text.replace(' ', 'T')}Z` : text;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** ISO 时间 → 本地可读文本。 */
export function fmtTime(value, withSeconds = true) {
  const date = parseDate(value);
  if (!date) return DASH;
  const pad = (part) => String(part).padStart(2, '0');
  const base = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return withSeconds ? `${base}:${pad(date.getSeconds())}` : base;
}

/** 仅日期。 */
export function fmtDate(value) {
  const date = parseDate(value);
  if (!date) return DASH;
  const pad = (part) => String(part).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** 相对时间（中文）。 */
export function fmtRelative(value) {
  const date = parseDate(value);
  if (!date) return DASH;
  const diff = Date.now() - date.getTime();
  if (diff < 0) return '刚刚';
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  return fmtDate(value);
}

/** 文本兜底：缺失 → "—"；其余转字符串。 */
export function fmtText(value) {
  if (isNil(value)) return DASH;
  return String(value);
}

/* ------------------------------------------------------------------ DOM */

/**
 * HTML 布尔属性：这些属性"只要出现"就生效，与值无关。
 * 典型陷阱：`setAttribute('disabled', 'false')` 仍然会禁用元素
 * （浏览器把属性名存在视为真），所以必须 false = 移除、true = 空值。
 */
const BOOLEAN_ATTRS = new Set([
  'disabled', 'checked', 'selected', 'readonly', 'required', 'multiple', 'hidden',
  'open', 'autofocus', 'autoplay', 'controls', 'loop', 'muted', 'novalidate',
  'allowfullscreen', 'default', 'inert', 'async', 'defer', 'ismap', 'itemscope',
]);

/** 写属性：布尔属性按 HTML 语义处理（false = 移除），其余转字符串。 */
function setAttribute(node, key, value) {
  if (BOOLEAN_ATTRS.has(key)) {
    if (value === false || value === 'false') node.removeAttribute(key);
    else node.setAttribute(key, '');
    return;
  }
  node.setAttribute(key, value === true ? '' : String(value));
}

/**
 * 创建元素。
 * `props` 中 `class` / `dataset` / `attrs` 单独处理，其余键优先按**元素属性**赋值
 * （`node.value` / `node.disabled` …），只有属性名不存在时才退化为 attribute；
 * 布尔属性按 HTML 语义处理（false 即移除）；以 `on` 开头的函数键绑定事件。
 */
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined) continue;
    if (key === 'class' || key === 'className') {
      node.setAttribute('class', value);
    } else if (key === 'dataset') {
      for (const [dataKey, dataValue] of Object.entries(value)) node.dataset[dataKey] = dataValue;
    } else if (key === 'attrs') {
      for (const [attrKey, attrValue] of Object.entries(value)) {
        if (attrValue === null || attrValue === undefined) continue;
        setAttribute(node, attrKey, attrValue);
      }
    } else if (key === 'text') {
      node.textContent = String(value);
    } else if (key === 'html') {
      // 仅在内部拼接可信片段时使用；外部数据一律走 textContent
      node.innerHTML = value;
    } else if (typeof value === 'function') {
      node.addEventListener(key.replace(/^on/, '').toLowerCase(), value);
    } else if (BOOLEAN_ATTRS.has(key)) {
      // 布尔属性：false 必须移除，否则元素会一直是"禁用/勾选"状态
      if (value === false || value === 'false') node.removeAttribute(key);
      else node.setAttribute(key, '');
    } else if (!(key in node) || typeof node[key] === 'function') {
      setAttribute(node, key, value);
    } else {
      node[key] = value;      // value / type / placeholder / title / maxLength …
    }
  }
  append(node, children);
  return node;
}

/** 追加子节点：接受字符串、节点或数组（自动忽略空值）。 */
export function append(parent, children) {
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

/** 清空容器。 */
export function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** 文本节点段。 */
export function text(value) {
  return document.createTextNode(fmtText(value));
}

/** 常用按钮。 */
export function button(label, onClick, options = {}) {
  const classes = ['btn'];
  if (options.primary) classes.push('btn--primary');
  if (options.small) classes.push('btn--sm');
  return el('button', {
    type: 'button',
    class: classes.join(' '),
    text: label,
    // 只在真的要禁用时才写这个属性：以前写成 `options.disabled || false`，
    // 会把 false 序列化成字符串 "false"，而 disabled 属性"存在即生效"，
    // 结果所有按钮都点不动（这是页面无法点击的根因）。
    disabled: options.disabled === true,
    title: options.title || '',
    onClick: onClick || null,
  });
}

/** 带标签的表单项容器。 */
export function field(labelText, control, hint) {
  const id = control.id || '';
  return el('div', { class: 'field' }, [
    el('label', { class: 'field__label', for: id, text: labelText }),
    control,
    hint ? el('span', { class: 'muted', text: hint }) : null,
  ]);
}

/** 卡片容器：统一标题 + 说明 + 主体。 */
export function card(title, body, options = {}) {
  const head = title
    ? el('div', { class: 'card__head' }, [
      el('div', {}, [
        el('h3', { class: 'card__title', text: title }),
        options.sub ? el('p', { class: 'card__sub', text: options.sub }) : null,
      ]),
      options.actions || null,
    ])
    : null;
  const bodyNode = el('div', { class: 'card__body' });
  append(bodyNode, body);
  return el('section', { class: 'card' + (options.class ? ` ${options.class}` : '') }, [head, bodyNode]);
}

/**
 * 表格构建器。
 * `columns`: [{ title, field|render, align, width }]，`render(row, index)` 返回节点或字符串。
 */
export function table(columns, rows, options = {}) {
  const thead = el('thead', {}, [
    el('tr', {}, columns.map((column) => el('th', {
      text: column.title,
      class: column.align === 'right' ? 'num' : '',
      style: column.width ? `width:${column.width}` : null,
    }))),
  ]);
  const tbody = el('tbody', {}, (rows || []).map((row, index) => el('tr', {}, columns.map((column) => {
    let content = column.field ? row[column.field] : null;
    if (typeof column.render === 'function') content = column.render(row, index);
    const cell = el('td', { class: column.align === 'right' ? 'num' : '' });
    append(cell, content === null || content === undefined ? DASH : content);
    return cell;
  }))));
  const node = el('table', { class: 'data' }, [thead, tbody]);  const wrapped = el('div', { class: 'table-wrap' }, [node]);
  if (options.caption) wrapped.prepend(el('p', { class: 'muted', text: options.caption }));
  return wrapped;
}

/** 空态占位。 */
export function empty(message) {
  return el('p', { class: 'empty', text: message });
}

/** 页面脚手架：标题 + 可选操作区 + 内容宿主，返回可继续追加的宿主。 */
export function pageFrame(container, title, actions = []) {
  container.textContent = '';
  const page = el('div');
  const actionNodes = (Array.isArray(actions) ? actions : [actions]).filter(Boolean);
  page.append(el('div', { class: 'row row--between' }, [
    el('h1', { text: title }),
    actionNodes.length ? el('div', { class: 'row' }, actionNodes) : null,
  ]));
  const host = el('div');
  page.append(host);
  container.append(page);
  return host;
}

/** 进度条节点（返回可更新引用的对象）。 */
export function progressBar(value, options = {}) {
  const percent = Math.max(0, Math.min(100, (toNumber(value) || 0) * 100));
  const fill = el('div', { class: 'progress__bar', style: `width:${percent.toFixed(1)}%` });
  const node = el('div', {
    class: 'progress' + (options.slim ? ' progress--slim' : ''),
    role: 'progressbar',
    'aria-valuemin': '0',
    'aria-valuemax': '100',
    'aria-valuenow': percent.toFixed(0),
    title: options.title || '',
  }, [fill]);
  return {
    node,
    update(next) {
      const target = Math.max(0, Math.min(100, (toNumber(next) || 0) * 100));
      fill.style.width = `${target.toFixed(1)}%`;
      node.setAttribute('aria-valuenow', target.toFixed(0));
    },
  };
}

/* -------------------------------------------------------------- Markdown */

function splitInlineStrong(target, source, onText) {
  // 只支持 **粗体** 与 `代码`；其余原样输出，避免引入 HTML 解析带来的注入面
  let rest = source;
  while (rest.length) {
    const match = /\*\*([^*]+)\*\*|`([^`]+)`/.exec(rest);
    if (!match) {
      onText(rest);
      return;
    }
    if (match.index > 0) onText(rest.slice(0, match.index));
    if (match[1] !== undefined) target.append(el('strong', {}, [match[1]]));
    else target.append(el('code', {}, [match[2]]));
    rest = rest.slice(match.index + match[0].length);
  }
}

/**
 * 极简 Markdown → DOM：支持标题（#–####）、有序/无序列表、粗体、行内代码、
 * 引用（>）、代码块（```）与管道表格。刻意不支持 HTML 直通。
 */
export function renderMarkdown(markdown) {
  const root = el('div', { class: 'report' });
  const lines = String(markdown || '').replace(/\r\n/g, '\n').split('\n');
  let index = 0;
  let paragraph = null;

  const flushParagraph = () => {
    if (!paragraph) return;
    const node = el('p');
    splitInlineStrong(node, paragraph, (chunk) => node.append(chunk));
    root.append(node);
    paragraph = null;
  };

  const splitRow = (line) => line.replace(/^\s*\|/, '').replace(/\|\s*$/, '')
    .split('|').map((cell) => cell.trim());

  while (index < lines.length) {
    const line = lines[index];

    if (/^\s*```/.test(line)) {
      flushParagraph();
      const code = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      index += 1;
      root.append(el('pre', {}, [el('code', { text: code.join('\n') })]));
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      const level = Math.min(heading[1].length, 6);
      const node = el(`h${level}`);
      splitInlineStrong(node, heading[2], (chunk) => node.append(chunk));
      root.append(node);
      index += 1;
      continue;
    }

    if (/^\s*>/.test(line)) {
      flushParagraph();
      const quote = [];
      while (index < lines.length && /^\s*>/.test(lines[index])) {
        quote.push(lines[index].replace(/^\s*>\s?/, ''));
        index += 1;
      }
      const node = el('blockquote');
      splitInlineStrong(node, quote.join(' '), (chunk) => node.append(chunk));
      root.append(node);
      continue;
    }

    // 表格：表头 + 分隔行（|---|）
    if (line.includes('|') && index + 1 < lines.length && /^\s*\|?[\s:-]*-[\s|:-]*\|?\s*$/.test(lines[index + 1]) && lines[index + 1].includes('-')) {
      flushParagraph();
      const head = splitRow(line);
      index += 2;
      const body = [];
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        body.push(splitRow(lines[index]));
        index += 1;
      }
      const tableNode = el('table', { class: 'data' }, [
        el('thead', {}, [el('tr', {}, head.map((cell) => el('th', { text: cell })))]),
        el('tbody', {}, body.map((cells) => el('tr', {}, cells.map((cell) => {
          const td = el('td');
          splitInlineStrong(td, cell, (chunk) => td.append(chunk));
          return td;
        })))),
      ]);
      root.append(el('div', { class: 'table-wrap' }, [tableNode]));
      continue;
    }

    const unordered = /^\s*[-*+]\s+(.*)$/.exec(line);
    const ordered = /^\s*(\d+)[.)]\s+(.*)$/.exec(line);
    if (unordered || ordered) {
      flushParagraph();
      const listNode = el(ordered ? 'ol' : 'ul');
      while (index < lines.length) {
        const item = ordered ? /^\s*(\d+)[.)]\s+(.*)$/.exec(lines[index])
          : /^\s*[-*+]\s+(.*)$/.exec(lines[index]);
        if (!item) {
          // 无序列在与有序列之间切换时结束当前列表
          if (ordered && /^\s*[-*+]\s+/.test(lines[index])) break;
          if (!ordered && /^\s*\d+[.)]\s+/.test(lines[index])) break;
          break;
        }
        const li = el('li');
        splitInlineStrong(li, item[item.length - 1], (chunk) => li.append(chunk));
        listNode.append(li);
        index += 1;
      }
      root.append(listNode);
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      index += 1;
      continue;
    }

    paragraph = paragraph ? `${paragraph} ${line.trim()}` : line.trim();
    index += 1;
  }
  flushParagraph();
  return root;
}

/* ------------------------------------------------------------- 杂项 */

/** 防抖：搜索框与轮询触发用。 */
export function debounce(fn, wait = 250) {
  let timer = null;
  return (...args) => {
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      timer = null;
      fn(...args);
    }, wait);
  };
}

/** 可取消的等待。 */
export function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

/** 取对象上的字段，路径不存在返回 undefined（接口字段可能缺省）。 */
export function pick(source, path, fallback = null) {
  let current = source;
  for (const key of String(path).split('.')) {
    if (current === null || current === undefined || typeof current !== 'object') return fallback;
    current = current[key];
  }
  return current === undefined ? fallback : current;
}

/** 数组兜底：接口没给 items 时返回 []，避免视图里到处判空。 */
export function list(value) {
  return Array.isArray(value) ? value : [];
}

/** 状态文案：会话状态与阶段状态的中文映射。 */
export const SESSION_STATUS_TEXT = {
  running: '进行中',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
  pending: '等待中',
  error: '异常',
};

export const PHASE_STATE_TEXT = {
  pending: '等待',
  running: '进行中',
  done: '完成',
  failed: '失败',
  cancelled: '已取消',
};

export function statusText(status) {
  return SESSION_STATUS_TEXT[status] || fmtText(status);
}

export function phaseStateText(state) {
  return PHASE_STATE_TEXT[state] || fmtText(state);
}
