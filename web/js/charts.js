/**
 * 自绘 SVG 图表（零依赖）。
 *
 * 设计原则：
 * - 颜色要么取 `config.heatmap_bands` / `heatmap.legend` 给的值，要么用中性前后景色（currentColor 体系），
 *   绝不自造状态色（例如"正常=绿、异常=红"）；
 * - 所有数值先经 util 的格式化函数，`null` 一律显示 "—" 或直接跳过该点（缺失窗不能连成折线）。
 */

import { DASH, el, fmtNum, fmtSeconds, list } from './util.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

/** 中性色阶：与热力图分档色同值，作为多序列图表的调色板（不算自造状态色）。 */
export const SERIES_COLORS = ['#1B3B6F', '#2E6DA4', '#6E9E8A', '#E69F00', '#D55E00'];

/** 创建 SVG 元素与根画布。 */
export function svgEl(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    node.setAttribute(key, String(value));
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child) node.append(child);
  }
  return node;
}

function canvas(width, height, padding, label, extraClass = '') {
  const root = svgEl('svg', {
    class: 'chart' + (extraClass ? ` ${extraClass}` : ''),
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: 'xMidYMid meet',
    role: 'img',
    'aria-label': label || '图表',
  });
  const plot = { x: padding[3], y: padding[0], w: width - padding[1] - padding[3], h: height - padding[0] - padding[2] };
  return { root, plot };
}

function emptyText(root, plot, message = '暂无数据') {
  root.append(svgEl('text', {
    x: plot.x + plot.w / 2,
    y: plot.y + plot.h / 2,
    class: 'chart__empty',
    'text-anchor': 'middle',
  }, [document.createTextNode(message)]));
}

/** 线性映射。 */
function scale(value, domainMin, domainMax, rangeMin, rangeMax) {
  if (domainMax === domainMin) return (rangeMin + rangeMax) / 2;
  const ratio = (value - domainMin) / (domainMax - domainMin);
  return rangeMin + ratio * (rangeMax - rangeMin);
}

/** Y 轴刻度：给定 0–1 的固定刻度或按数据范围取 4 段。 */
function drawYAxis(root, plot, min, max, formatter = (v) => fmtNum(v, 2), ticks = 4) {
  for (let index = 0; index <= ticks; index += 1) {
    const value = min + ((max - min) * index) / ticks;
    const y = plot.y + plot.h - (plot.h * index) / ticks;
    root.append(svgEl('line', { x1: plot.x, x2: plot.x + plot.w, y1: y, y2: y, class: 'chart__grid-line' }));
    root.append(svgEl('text', {
      x: plot.x - 6, y: y + 3, class: 'chart__tick', 'text-anchor': 'end',
    }, [document.createTextNode(formatter(value))]));
  }
  root.append(svgEl('line', { x1: plot.x, x2: plot.x, y1: plot.y, y2: plot.y + plot.h, class: 'chart__axis' }));
}

/**
 * 折线图（周/月趋势、波形叠加等）。
 * @param {Array<{label:string, values:number[]}>} points
 * @param {{ series?:string[], colors?:string[], height?:number, max?:number, thresholds?:Array<{value:number,label?:string}> }} options
 */
/**
 * 画布宽度：优先按容器实际宽度出图（1:1 像素），否则退回 720。
 *
 * 为什么必须量宽度：SVG 是 `width:100%; height:auto`，viewBox 比例会被等比放大——
 * 720×220 的图放进 1250px 宽的卡片会渲染成 382px 高，字号也被放大 1.7 倍，
 * 既让卡片虚高又让坐标轴文字失衡。量到真实宽度后 height 才是"真的多少像素高"。
 */
function measureWidth(container, fallback = 720) {
  const raw = container && (container.clientWidth
    || (container.parentElement && container.parentElement.clientWidth));
  const width = Number(raw);
  if (!Number.isFinite(width) || width < 320) return fallback;
  return Math.round(Math.min(width, 1600));
}

export function drawLineChart(container, points = [], options = {}) {
  container.textContent = '';
  const width = options.width || measureWidth(container);
  const height = options.height || 220;
  const padding = [16, 24, 28, 44];
  const { root, plot } = canvas(width, height, padding, options.label || '折线图');
  const rows = list(points).filter((point) => point);
  const seriesNames = options.series || ['focus'];
  const colors = options.colors || SERIES_COLORS;

  const values = [];
  for (const row of rows) {
    for (const name of seriesNames) {
      const value = row.values ? row.values[name] : null;
      if (typeof value === 'number' && Number.isFinite(value)) values.push(value);
    }
  }
  const max = options.max !== undefined ? options.max : Math.max(1e-6, ...values);
  const min = options.min !== undefined ? options.min : Math.min(0, ...values);
  const spanMax = options.percent ? 1 : Math.max(max, 1e-6);
  const formatTick = options.percent ? (v) => `${Math.round(v * 100)}%` : (v) => fmtNum(v, 2);

  // 空数据也要画出坐标轴刻度：只放一句"暂无数据"看起来像坏图
  if (!rows.length) {
    drawYAxis(root, plot, min, spanMax, formatTick);
    root.append(svgEl('line', {
      x1: plot.x, x2: plot.x + plot.w, y1: plot.y + plot.h, y2: plot.y + plot.h, class: 'chart__axis',
    }));
    emptyText(root, plot, options.emptyText || '暂无数据');
    container.append(root);
    return root;
  }

  drawYAxis(root, plot, min, spanMax, formatTick);

  // 阈值线（例如预警阈值），中性虚线
  for (const threshold of list(options.thresholds)) {
    if (typeof threshold.value !== 'number' || threshold.value < min || threshold.value > spanMax) continue;
    const y = scale(threshold.value, min, spanMax, plot.y + plot.h, plot.y);
    root.append(svgEl('line', {
      x1: plot.x, x2: plot.x + plot.w, y1: y, y2: y, class: 'chart__target',
    }));
    if (threshold.label) {
      root.append(svgEl('text', {
        x: plot.x + plot.w - 2, y: y - 4, class: 'chart__label', 'text-anchor': 'end',
      }, [document.createTextNode(threshold.label)]));
    }
  }

  // 单点时没有"步长"可算：直接放在绘图区中央，否则会贴在最左边几乎看不见
  const stepX = rows.length > 1 ? plot.w / (rows.length - 1) : 0;
  const xAt = (index) => (rows.length > 1 ? plot.x + stepX * index : plot.x + plot.w / 2);
  const showPoints = options.points !== false && rows.length <= 60;
  const pointRadius = options.pointRadius !== undefined
    ? Number(options.pointRadius)
    : (rows.length <= 3 ? 4.5 : 2.6);
  seriesNames.forEach((name, seriesIndex) => {
    const color = colors[seriesIndex % colors.length];
    // 缺失点（null）处断开折线，避免用直线"脑补"出不存在的数据
    let path = '';
    let pen = false;
    rows.forEach((row, index) => {
      const value = row.values ? row.values[name] : null;
      const hasValue = typeof value === 'number' && Number.isFinite(value);
      if (!hasValue) {
        pen = false;
        return;
      }
      const x = xAt(index);
      const y = scale(value, min, spanMax, plot.y + plot.h, plot.y);
      path += `${pen ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)} `;
      pen = true;
      if (showPoints) {
        root.append(svgEl('circle', { cx: x, cy: y, r: pointRadius, fill: color, stroke: color }));
      }
      // 少量点时把数值标在点旁：单点趋势图必须能直接读出当期数值
      if (options.showValues && (rows.length <= 12 || index === rows.length - 1)) {
        root.append(svgEl('text', {
          x, y: Math.max(plot.y + 10, y - pointRadius - 6),
          class: 'chart__tick', 'text-anchor': 'middle',
        }, [document.createTextNode(options.percent ? `${(value * 100).toFixed(1)}%` : fmtNum(value, 3))]));
      }
    });
    if (path) {
      root.append(svgEl('path', { d: path.trim(), class: 'chart__series', stroke: color }));
    }
  });

  // X 轴标签：点多时隔位显示，避免重叠；单点时居中显示
  const stride = Math.ceil(rows.length / 8);
  rows.forEach((row, index) => {
    if (index % stride !== 0 && index !== rows.length - 1) return;
    const x = xAt(index);
    root.append(svgEl('text', {
      x, y: plot.y + plot.h + 16, class: 'chart__tick', 'text-anchor': 'middle',
    }, [document.createTextNode(String(row.label ?? DASH).slice(0, 12))]));
  });

  root.append(svgEl('line', {
    x1: plot.x, x2: plot.x + plot.w, y1: plot.y + plot.h, y2: plot.y + plot.h, class: 'chart__axis',
  }));
  container.append(root);
  return root;
}

/**
 * 柱状图/条形图（分布、分段对比、跨会话比较）。
 * @param {Array<{label:string, value:number}>} items
 */
export function drawBarChart(container, items = [], options = {}) {
  container.textContent = '';
  const width = 720;
  const height = options.height || 220;
  const padding = [16, 24, 30, 44];
  const { root, plot } = canvas(width, height, padding, options.label || '柱状图');
  const rows = list(items).filter((row) => row && typeof row.value === 'number' && Number.isFinite(row.value));
  const formatTick = options.percent ? (v) => `${Math.round(v * 100)}%` : (v) => fmtNum(v, 2);
  if (!rows.length) {
    // 空态保留坐标轴刻度，避免看起来像"坏图"
    drawYAxis(root, plot, 0, options.max !== undefined ? options.max : 1, formatTick);
    root.append(svgEl('line', {
      x1: plot.x, x2: plot.x + plot.w, y1: plot.y + plot.h, y2: plot.y + plot.h, class: 'chart__axis',
    }));
    emptyText(root, plot, options.emptyText || '暂无数据');
    container.append(root);
    return root;
  }
  const max = options.max !== undefined ? options.max : Math.max(...rows.map((row) => row.value), 1e-6);
  drawYAxis(root, plot, 0, Math.max(max, 1e-6), formatTick);

  const slot = plot.w / rows.length;
  const barWidth = Math.max(6, Math.min(46, slot * 0.62));
  rows.forEach((row, index) => {
    const x = plot.x + slot * index + (slot - barWidth) / 2;
    const y = scale(row.value, 0, max, plot.y + plot.h, plot.y);
    const barHeight = Math.max(1, plot.y + plot.h - y);
    root.append(svgEl('rect', {
      x, y, width: barWidth, height: barHeight, class: 'chart__bar',
      fill: row.color || options.color || SERIES_COLORS[index % SERIES_COLORS.length],
    }));
    root.append(svgEl('text', {
      x: x + barWidth / 2, y: y - 4, class: 'chart__tick', 'text-anchor': 'middle',
    }, [document.createTextNode(options.percent ? `${Math.round(row.value * 100)}%` : fmtNum(row.value, 2))]));
    root.append(svgEl('text', {
      x: x + barWidth / 2, y: plot.y + plot.h + 16, class: 'chart__tick', 'text-anchor': 'middle',
    }, [document.createTextNode(String(row.label ?? DASH).slice(0, 10))]));
  });
  root.append(svgEl('line', {
    x1: plot.x, x2: plot.x + plot.w, y1: plot.y + plot.h, y2: plot.y + plot.h, class: 'chart__axis',
  }));
  container.append(root);
  return root;
}

/**
 * 半圆仪表（专注度反馈、质检通过率）。
 * @param {number|null} value 0–1
 */
export function drawGauge(container, value, options = {}) {
  container.textContent = '';
  const width = 320;
  const height = 190;
  // 仪表按 viewBox 固定比例显示：容器太宽时 SVG 会被等比放大成几百像素高的大控件，
  // 视觉上把整行撑开（用 .chart--gauge 限制最大宽度，见 css/app.css）
  const { root } = canvas(width, height, [10, 10, 10, 10], options.label || '仪表', 'chart--gauge');
  const cx = width / 2;
  const cy = 140;
  const radius = 96;
  const usable = typeof value === 'number' && Number.isFinite(value);
  const ratio = usable ? Math.max(0, Math.min(1, value)) : 0;

  const arcPath = (from, to, r) => {
    const angleFrom = Math.PI * (1 - from);
    const angleTo = Math.PI * (1 - to);
    const x1 = cx + r * Math.cos(angleFrom);
    const y1 = cy - r * Math.sin(angleFrom);
    const x2 = cx + r * Math.cos(angleTo);
    const y2 = cy - r * Math.sin(angleTo);
    const largeArc = Math.abs(to - from) > 0.5 ? 1 : 0;
    return `M${x1.toFixed(2)} ${y1.toFixed(2)} A${r} ${r} 0 ${largeArc} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
  };

  root.append(svgEl('path', {
    d: arcPath(0, 1, radius), fill: 'none', stroke: 'currentColor', opacity: 0.18, 'stroke-width': 14,
    'stroke-linecap': 'round',
  }));
  if (usable && ratio > 0.001) {
    root.append(svgEl('path', {
      d: arcPath(0, ratio, radius), fill: 'none', stroke: options.color || SERIES_COLORS[0],
      'stroke-width': 14, 'stroke-linecap': 'round',
    }));
  }

  for (let index = 0; index <= 4; index += 1) {
    const fraction = index / 4;
    const angle = Math.PI * (1 - fraction);
    const inner = radius - 22;
    root.append(svgEl('line', {
      x1: cx + inner * Math.cos(angle), y1: cy - inner * Math.sin(angle),
      x2: cx + (inner - 6) * Math.cos(angle), y2: cy - (inner - 6) * Math.sin(angle),
      class: 'chart__axis',
    }));
    root.append(svgEl('text', {
      x: cx + (inner - 16) * Math.cos(angle), y: cy - (inner - 16) * Math.sin(angle) + 3,
      class: 'chart__tick', 'text-anchor': 'middle',
    }, [document.createTextNode(String(index * 25))]));
  }

  if (typeof options.target === 'number' && Number.isFinite(options.target)) {
    const angle = Math.PI * (1 - Math.max(0, Math.min(1, options.target)));
    root.append(svgEl('line', {
      x1: cx + (radius - 26) * Math.cos(angle), y1: cy - (radius - 26) * Math.sin(angle),
      x2: cx + (radius + 10) * Math.cos(angle), y2: cy - (radius + 10) * Math.sin(angle),
      class: 'chart__target',
    }));
  }

  root.append(svgEl('text', {
    x: cx, y: cy - 18, class: 'chart__label', 'text-anchor': 'middle', 'font-size': 30,
  }, [document.createTextNode(usable ? `${Math.round(ratio * 100)}` : DASH)]));
  root.append(svgEl('text', {
    x: cx, y: cy + 6, class: 'chart__tick', 'text-anchor': 'middle',
  }, [document.createTextNode(usable ? '分' : '不可用')]));
  container.append(root);
  // 说明放在 SVG 之外：viewBox 只有 320 宽，长文案写进 SVG 会溢出控件边界
  if (options.note) container.append(el('p', { class: 'chart__note muted', text: options.note }));
  return root;
}

/**
 * 热力图网格。
 *
 * 缺失窗（index === -1）必须画成独立灰块：它表示"该窗不参与指标与预警计时"，
 * 而不是某个状态档位，因此不取 legend 里的颜色，只用 missing_color + 虚线边框。
 *
 * @param {Array<{index:number,label:string,color:string,score:number|null,t:number}>} cells
 * @param {{legend?:Array<{label:string,color:string,range?:string}>, missingColor?:string, title?:string}} options
 */
export function drawHeatmap(container, cells = [], options = {}) {
  container.textContent = '';
  const wrap = el('div', { class: 'heatmap' });
  const grid = el('div', { class: 'heatmap__grid', attrs: { role: 'list' } });
  const rows = list(cells);
  if (!rows.length) {
    wrap.append(el('p', { class: 'empty', text: '暂无热力图数据' }));
    container.append(wrap);
    return wrap;
  }
  for (const cell of rows) {
    const missing = cell && (cell.index === -1 || cell.index === undefined);
    const color = missing ? (options.missingColor || cell.color || '') : cell.color;
    const node = el('span', {
      class: 'heatmap__cell' + (missing ? ' heatmap__cell--missing' : ''),
      title: `${cell.label || (missing ? '低质量缺失' : '')}｜分值 ${fmtNum(cell.score)}｜t=${fmtSeconds(cell.t)}`,
      attrs: { role: 'listitem', 'aria-label': `${missing ? '低质量缺失' : cell.label || '状态'}，t=${fmtSeconds(cell.t)}` },
    });
    if (color) node.style.background = color;
    grid.append(node);
  }
  wrap.append(grid);

  const legendItems = list(options.legend).map((item) => el('span', { class: 'legend__item' }, [
    el('span', { class: 'legend__swatch', style: item.color ? `background:${item.color}` : null }),
    el('span', { text: item.label + (item.range ? `（${item.range}）` : '') }),
  ]));
  // 图例末尾补一个缺失色说明，来源仍是接口给的 missing_color
  if (options.missingColor) {
    legendItems.push(el('span', { class: 'legend__item' }, [
      el('span', { class: 'legend__swatch', style: `background:${options.missingColor}; border-style: dashed` }),
      el('span', { text: '低质量缺失（不参与指标与预警计时）' }),
    ]));
  }
  if (legendItems.length) wrap.append(el('div', { class: 'legend' }, legendItems));
  container.append(wrap);
  return wrap;
}

/**
 * 原始波形：按 signal.srate 标注时间轴。
 * @param {{samples:number[], srate:number}} signal
 */
export function drawWaveform(container, signal, options = {}) {
  container.textContent = '';
  const width = 720;
  const height = options.height || 140;
  const padding = [12, 16, 24, 44];
  const { root, plot } = canvas(width, height, padding, options.label || '原始脑电波形');
  const samples = list(signal && signal.samples).filter((value) => typeof value === 'number' && Number.isFinite(value));
  if (!samples.length) {
    emptyText(root, plot, '暂无波形（该窗未包含原始信号）');
    container.append(root);
    return root;
  }
  const srate = Number(signal && signal.srate) > 0 ? Number(signal.srate) : 250;
  const step = Math.max(1, Math.round(samples.length / 400));
  const reduced = samples.filter((_, index) => index % step === 0);
  const maxAbs = Math.max(...reduced.map((value) => Math.abs(value)), 1e-6);

  // 零点基线
  const zeroY = scale(0, -maxAbs, maxAbs, plot.y + plot.h, plot.y);
  root.append(svgEl('line', { x1: plot.x, x2: plot.x + plot.w, y1: zeroY, y2: zeroY, class: 'chart__grid-line' }));

  const pointCount = reduced.length;
  const path = reduced.map((value, index) => {
    const x = plot.x + (plot.w * index) / Math.max(1, pointCount - 1);
    const y = scale(value, -maxAbs, maxAbs, plot.y + plot.h, plot.y);
    return `${index === 0 ? 'M' : 'L'}${x.toFixed(2)} ${y.toFixed(2)}`;
  }).join(' ');

  root.append(svgEl('path', { d: path, class: 'chart__series', stroke: SERIES_COLORS[1], 'vector-effect': 'non-scaling-stroke' }));
  root.append(svgEl('text', {
    x: plot.x + plot.w, y: plot.y + 10, class: 'chart__tick', 'text-anchor': 'end',
  }, [document.createTextNode(`${fmtNum(srate, 0)} Hz · ±${fmtNum(maxAbs, 0)} µV · ${samples.length} 点`)]));

  // 时间轴：窗长 = 点数 / 采样率
  const spanSec = samples.length / srate;
  for (let index = 0; index <= 4; index += 1) {
    const value = (spanSec * index) / 4;
    const x = plot.x + (plot.w * index) / 4;
    root.append(svgEl('text', {
      x, y: plot.y + plot.h + 16, class: 'chart__tick', 'text-anchor': 'middle',
    }, [document.createTextNode(`${value.toFixed(1)}s`)]));
  }
  container.append(root);
  return root;
}

/**
 * 预警时间轴（用列表 + 中性圆点表示，颜色仍取接口给的分档色）。
 * @param {Array<{t_sec:number, kind:string, state:string, message:string, value:number}>} alerts
 */export function drawAlertTimeline(container, alerts = [], options = {}) {
  container.textContent = '';
  const rows = list(alerts);
  if (!rows.length) {
    container.append(el('p', { class: 'empty', text: options.emptyText || '本次会话没有触发预警' }));
    return container;
  }
  const kindLabels = { low_focus: '专注度偏低', high_load: '认知负荷偏高', poor_signal: '信号质量差' };
  const stateLabels = { triggered: '触发', active: '持续', released: '解除' };
  const items = rows.map((alert) => el('div', { class: 'timeline__item' }, [
    el('span', { class: 'timeline__dot' }),
    el('span', { class: 'timeline__time', text: fmtSeconds(alert.t_sec) }),
    el('strong', { text: kindLabels[alert.kind] || alert.kind || DASH }),
    el('span', { class: 'muted', text: ` ｜${stateLabels[alert.state] || alert.state || DASH}｜持续 ${fmtSeconds(alert.sustained_sec)}｜值 ${fmtNum(alert.value)}` }),
    el('p', { text: alert.message || '' }),
  ]));
  container.append(el('div', { class: 'timeline' }, items));
  return container;
}

/** 图表图例（供视图复用）。 */
export function legend(items = [], missingColor) {
  const nodes = list(items).map((item) => el('span', { class: 'legend__item' }, [
    el('span', { class: 'legend__swatch', style: item.color ? `background:${item.color}` : null }),
    el('span', { text: item.label + (item.range ? `（${item.range}）` : '') }),
  ]));
  if (missingColor) {
    nodes.push(el('span', { class: 'legend__item' }, [
      el('span', { class: 'legend__swatch', style: `background:${missingColor}; border-style: dashed` }),
      el('span', { text: '低质量缺失' }),
    ]));
  }
  return el('div', { class: 'legend' }, nodes);
}

/** 带阈值刻度的指标条（实时监测用）。 */
export function metricBar(label, value, options = {}) {
  const max = options.max !== undefined ? options.max : 1;
  const ratio = typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(1, value / max)) : null;
  const fill = el('div', { class: `bar__fill ${options.fillClass || 'fill-2'}` });
  if (ratio !== null) fill.style.width = `${(ratio * 100).toFixed(1)}%`;
  const track = el('div', {
    class: 'bar', attrs: { role: 'img', 'aria-label': `${label} ${fmtNum(value)}` },
  }, [fill]);
  if (typeof options.threshold === 'number' && Number.isFinite(options.threshold)) {
    track.append(el('div', {
      class: 'bar__threshold',
      style: `left:${((options.threshold / max) * 100).toFixed(1)}%`,
      title: options.thresholdLabel || `阈值 ${fmtNum(options.threshold)}`,
    }));
  }
  return el('div', { class: 'metric-row' }, [
    el('span', { text: label }),
    track,
    el('span', { class: 'metric-row__value', text: options.percent ? `${fmtNum(ratio === null ? null : ratio * 100, 0)}%` : fmtNum(value, options.digits === undefined ? 3 : options.digits) }),
  ]);
}

/* ------------------------------------------------------------------ 画布图表
 * 高频实时波形必须用 canvas：SVG 每帧重建上千个 path 节点会让浏览器掉帧，
 * 而 canvas 一次 clearRect + 折线绘制可以稳定跑 10–25 FPS（对齐厂家采集软件的观感）。
 */

/** 设备像素比适配：避免高分屏上线条发虚。 */
function prepareCanvas(host, height) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const width = Math.max(320, host.clientWidth || host.parentElement?.clientWidth || 720);
  let node = host.querySelector('canvas');
  if (!node) {
    node = document.createElement('canvas');
    node.className = 'chart chart--canvas';
    host.append(node);
  }
  const cssWidth = width;
  const cssHeight = height;
  node.style.width = `${cssWidth}px`;
  node.style.height = `${cssHeight}px`;
  node.width = Math.round(cssWidth * dpr);
  node.height = Math.round(cssHeight * dpr);
  const ctx = node.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);
  const style = getComputedStyle(host);
  return {
    ctx, width: cssWidth, height: cssHeight,
    line: style.getPropertyValue('--line').trim() || '#e3e6ea',
    lineStrong: style.getPropertyValue('--line-strong').trim() || '#c9ced6',
    text: style.getPropertyValue('--text-3').trim() || '#8a9099',
    textStrong: style.getPropertyValue('--text-2').trim() || '#5b6270',
  };
}

function gridLine(ctx, box, color, width = 1, dash = []) {
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.setLineDash(dash);
  ctx.beginPath();
  ctx.moveTo(box.x, box.y + box.h);
  ctx.lineTo(box.x + box.w, box.y + box.h);
  ctx.stroke();
  ctx.restore();
}

/**
 * 多通道实时波形（BioMultiLite 风格）：每通道一个独立面板、独立量程、
 * 左侧通道名与 ±量程标注、底部共享时间轴、每帧整体重绘。
 *
 * @param {HTMLElement} host 容器
 * @param {{channels:Array<{label:string,samples:number[],peak:number}>}} frame 服务端 signal 帧
 * @param {{height?:number, windowSec?:number, srate?:number}} [options]
 */
export function drawMultiChannelEeg(host, frame, options = {}) {
  const channels = list(frame && frame.channels);
  const windowSec = Number(options.windowSec) || Number(frame && frame.window_sec) || 10;
  const srate = Number(options.srate) || Number(frame && frame.srate) || 250;
  const perHeight = Number(options.channelHeight) || 120;
  const usable = channels.filter((channel) => list(channel.samples).length > 1);
  const height = Math.max(perHeight, perHeight * Math.max(1, usable.length)) + 18;
  const env = prepareCanvas(host, height);

  const left = 52;
  const right = 12;
  const top = 10;
  const bottom = 22;
  const box = { x: left, y: top, w: env.width - left - right, h: height - top - bottom };
  const panelHeight = box.h / Math.max(1, usable.length);

  env.ctx.font = '11px system-ui, sans-serif';
  env.ctx.textBaseline = 'middle';

  usable.forEach((channel, index) => {
    const panel = { x: box.x, y: box.y + index * panelHeight, w: box.w, h: panelHeight };
    const samples = list(channel.samples).map(Number).filter(Number.isFinite);
    const peak = Math.max(Number(channel.peak) || 0, ...samples.map(Math.abs), 1e-6);
    const scaleMax = peak * 1.15;

    // 面板边框 + 中线（0 µV）
    env.ctx.save();
    env.ctx.strokeStyle = env.line;
    env.ctx.lineWidth = 1;
    env.ctx.strokeRect(panel.x, panel.y, panel.w, panel.h);
    env.ctx.setLineDash([3, 3]);
    env.ctx.strokeStyle = env.lineStrong;
    env.ctx.beginPath();
    env.ctx.moveTo(panel.x, panel.y + panel.h / 2);
    env.ctx.lineTo(panel.x + panel.w, panel.y + panel.h / 2);
    env.ctx.stroke();
    env.ctx.restore();

    // 纵轴刻度（±量程）
    env.ctx.fillStyle = env.text;
    env.ctx.textAlign = 'right';
    env.ctx.fillText(`+${fmtNum(scaleMax, 0)}`, panel.x - 6, panel.y + 8);
    env.ctx.fillText('0', panel.x - 6, panel.y + panel.h / 2);
    env.ctx.fillText(`-${fmtNum(scaleMax, 0)}`, panel.x - 6, panel.y + panel.h - 8);

    // 通道名
    env.ctx.fillStyle = env.textStrong;
    env.ctx.textAlign = 'left';
    env.ctx.font = '12px system-ui, sans-serif';
    env.ctx.fillText(channel.label || `Ch${index + 1}`, panel.x + 6, panel.y + 12);
    env.ctx.font = '11px system-ui, sans-serif';

    // 波形：x 均匀铺满，y 按本通道量程归一
    if (samples.length > 1) {
      env.ctx.save();
      env.ctx.beginPath();
      env.ctx.strokeStyle = SERIES_COLORS[1];
      env.ctx.lineWidth = 1;
      env.ctx.lineJoin = 'round';
      const stepX = panel.w / Math.max(1, samples.length - 1);
      for (let position = 0; position < samples.length; position += 1) {
        const y = panel.y + panel.h / 2 - (samples[position] / scaleMax) * (panel.h / 2 - 2);
        const x = panel.x + position * stepX;
        if (position === 0) env.ctx.moveTo(x, y);
        else env.ctx.lineTo(x, y);
      }
      env.ctx.stroke();
      env.ctx.restore();
    } else {
      env.ctx.fillStyle = env.text;
      env.ctx.textAlign = 'center';
      env.ctx.fillText('等待样本…', panel.x + panel.w / 2, panel.y + panel.h / 2);
      env.ctx.textAlign = 'left';
    }
  });

  // 时间轴：0 → windowSec 秒
  env.ctx.fillStyle = env.text;
  env.ctx.textAlign = 'center';
  for (let tick = 0; tick <= 5; tick += 1) {
    const x = box.x + (box.w * tick) / 5;
    env.ctx.fillText(`${((windowSec * tick) / 5).toFixed(0)}s`, x, height - 8);
  }
  env.ctx.textAlign = 'left';
  env.ctx.fillStyle = env.text;
  const points = usable.reduce((total, channel) => total + list(channel.samples).length, 0);
  env.ctx.fillText(`${fmtNum(srate, 0)} Hz · ${windowSec}s 窗口 · 每通道 ${list(usable[0] && usable[0].samples).length} 点`
    + (points ? '' : ' · 等待数据'), box.x, box.y - 1 + 6);
  return host;
}

/**
 * 实时频谱：dB 对频率的曲线 + 频带区间底色（对齐厂家软件的 Spectrum 面板）。
 * @param {HTMLElement} host
 * @param {{freqs:number[], power_db:number[], segments?:number}} spectrum
 * @param {{bands?:object, height?:number, bandRanges?:object}} [options]
 */
export function drawSpectrum(host, spectrum, options = {}) {
  const freqs = list(spectrum && spectrum.freqs).map(Number);
  const power = list(spectrum && spectrum.power_db).map(Number);
  const height = Number(options.height) || 170;
  const env = prepareCanvas(host, height);
  const box = { x: 46, y: 12, w: env.width - 58, h: height - 34 };
  const ranges = options.bandRanges || {};
  const bandCount = Object.keys(ranges).length || 1;

  env.ctx.font = '11px system-ui, sans-serif';
  env.ctx.textBaseline = 'middle';

  const hasData = freqs.length > 1 && power.length > 1;
  const maxFreq = hasData ? Math.max(...freqs) : 45;

  // 频带底色（只作分区提示，不用状态色）
  let bandIndex = 0;
  for (const [name, range] of Object.entries(ranges)) {
    if (!Array.isArray(range) || range.length < 2) continue;
    const [low, high] = range.map(Number);
    const x0 = box.x + (Math.max(0, low) / maxFreq) * box.w;
    const x1 = box.x + (Math.min(maxFreq, high) / maxFreq) * box.w;
    env.ctx.save();
    env.ctx.fillStyle = `${SERIES_COLORS[bandIndex % SERIES_COLORS.length]}14`;
    env.ctx.fillRect(x0, box.y, Math.max(1, x1 - x0), box.h);
    env.ctx.restore();
    env.ctx.fillStyle = env.text;
    env.ctx.textAlign = 'center';
    env.ctx.fillText(name, (x0 + x1) / 2, box.y + box.h + 12);
    bandIndex += 1;
  }

  env.ctx.strokeStyle = env.line;
  env.ctx.lineWidth = 1;
  env.ctx.strokeRect(box.x, box.y, box.w, box.h);

  if (hasData) {
    const minDb = Math.min(...power);
    const maxDb = Math.max(...power);
    const span = Math.max(1e-6, maxDb - minDb);
    env.ctx.save();
    env.ctx.beginPath();
    env.ctx.strokeStyle = SERIES_COLORS[0];
    env.ctx.lineWidth = 1.4;
    for (let index = 0; index < freqs.length; index += 1) {
      const x = box.x + (freqs[index] / maxFreq) * box.w;
      const y = box.y + box.h - ((power[index] - minDb) / span) * (box.h - 8) - 4;
      if (index === 0) env.ctx.moveTo(x, y);
      else env.ctx.lineTo(x, y);
    }
    env.ctx.stroke();
    env.ctx.restore();

    // 纵轴与横轴刻度
    env.ctx.fillStyle = env.text;
    env.ctx.textAlign = 'right';
    env.ctx.fillText(`${fmtNum(maxDb, 0)} dB`, box.x - 6, box.y + 8);
    env.ctx.fillText(`${fmtNum(minDb, 0)} dB`, box.x - 6, box.y + box.h - 8);
    env.ctx.textAlign = 'center';
    for (let tick = 0; tick <= 4; tick += 1) {
      const value = (maxFreq * tick) / 4;
      env.ctx.fillText(`${value.toFixed(0)}`, box.x + (box.w * tick) / 4, box.y + box.h + 12);
    }
    env.ctx.textAlign = 'left';
    env.ctx.fillText(`频率 (Hz) · Welch 分段 ${spectrum.segments || 0}`, box.x, box.y - 2);
  } else {
    env.ctx.fillStyle = env.text;
    env.ctx.textAlign = 'center';
    env.ctx.fillText('等待频谱数据（需要至少一个可用窗）', box.x + box.w / 2, box.y + box.h / 2);
  }
  env.ctx.textAlign = 'left';
  return host;
}

/**
 * 频带相对功率条（Theta/Alpha/Beta/Gamma…）：直接画服务端给的 rel 值。
 * @param {HTMLElement} host
 * @param {object} bands `{theta: 0.01, alpha: 0.41, …}`
 */
export function drawBandBars(host, bands, options = {}) {
  const entries = Object.entries(bands || {}).filter(([, value]) => typeof value === 'number');
  const height = Number(options.height) || 170;
  const env = prepareCanvas(host, height);
  const box = { x: 44, y: 14, w: env.width - 56, h: height - 40 };
  env.ctx.font = '11px system-ui, sans-serif';
  env.ctx.textBaseline = 'middle';

  if (!entries.length) {
    env.ctx.fillStyle = env.text;
    env.ctx.textAlign = 'center';
    env.ctx.fillText('等待频带数据', box.x + box.w / 2, box.y + box.h / 2);
    env.ctx.textAlign = 'left';
    return host;
  }
  const max = Math.max(...entries.map(([, value]) => value), 1e-6);
  const barWidth = box.w / (entries.length * 1.6);
  env.ctx.strokeStyle = env.line;
  env.ctx.strokeRect(box.x, box.y, box.w, box.h);

  entries.forEach(([name, value], index) => {
    const centerX = box.x + (box.w * (index + 0.5)) / entries.length;
    const barHeight = (value / max) * (box.h - 26);
    env.ctx.save();
    env.ctx.fillStyle = SERIES_COLORS[index % SERIES_COLORS.length];
    env.ctx.globalAlpha = 0.85;
    env.ctx.fillRect(centerX - barWidth / 2, box.y + box.h - barHeight - 14, barWidth, barHeight);
    env.ctx.restore();
    env.ctx.fillStyle = env.textStrong;
    env.ctx.textAlign = 'center';
    env.ctx.fillText(name, centerX, box.y + box.h - 4);
    env.ctx.fillStyle = env.text;
    env.ctx.fillText(`${(value * 100).toFixed(1)}%`, centerX, box.y + box.h - barHeight - 22);
  });
  env.ctx.textAlign = 'left';
  env.ctx.fillStyle = env.text;
  env.ctx.fillText('相对频带功率（占 0.5–45 Hz 的比例）', box.x, box.y - 4);
  return host;
}
