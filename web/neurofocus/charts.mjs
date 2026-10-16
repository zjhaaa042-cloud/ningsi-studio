import { minMaxColumns, segments } from './core.mjs';

const COLORS = ['#37d5c7', '#64abfa', '#9b84f2', '#bb9dda'];
function surface(canvas) {
  const box = canvas.getBoundingClientRect();
  if (!box.width || !box.height) return null;
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const w = Math.round(box.width), h = Math.round(box.height);
  if (canvas.width !== Math.round(w * ratio) || canvas.height !== Math.round(h * ratio)) {
    canvas.width = Math.round(w * ratio); canvas.height = Math.round(h * ratio);
  }
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, w, h);
  ctx.font = '12px "Segoe UI", "Microsoft YaHei", sans-serif'; ctx.lineWidth = 1;
  return { ctx, w, h };
}

export function drawTrend(canvas, points, start, end, hovered = null, maxGapMs = 1500) {
  const s = surface(canvas); if (!s) return;
  const { ctx, w, h } = s, left = 36, right = 15, top = 12, bottom = h - 26;
  const x = (t) => left + (t - start) / Math.max(1, end - start) * (w - left - right);
  const y = (v) => bottom - v / 100 * (bottom - top);
  ctx.fillStyle = '#8b9db7'; ctx.strokeStyle = '#28364b';
  for (let v = 0; v <= 100; v += 25) {
    ctx.fillText(String(v), 4, y(v) + 3); ctx.beginPath(); ctx.moveTo(left, y(v)); ctx.lineTo(w - right, y(v)); ctx.stroke();
  }
  const ticks = w < 450 ? 3 : 5;
  ctx.textAlign = 'center';
  for (let i = 0; i <= ticks; i++) {
    const t = start + (end - start) * i / ticks;
    ctx.fillText(new Date(t).toLocaleTimeString('zh-CN', { hour12: false, minute: '2-digit', second: '2-digit' }), x(t), h - 7);
  }
  ctx.save(); ctx.beginPath(); ctx.rect(left, top, w - left - right, bottom - top); ctx.clip();
  // Expose rejected-quality intervals without interpreting missing data as zero.
  for (const p of points) if (!p.valid && p.timestampMs >= start && p.timestampMs <= end) {
    ctx.fillStyle = '#f4bb6817'; ctx.fillRect(x(p.timestampMs - 500), top, Math.max(2, x(p.timestampMs + 500) - x(p.timestampMs - 500)), bottom - top);
  }
  const visible = points.filter((p) => p.timestampMs >= start && p.timestampMs <= end);
  const parts = segments(visible, maxGapMs);
  for (const part of parts) {
    ctx.beginPath();
    part.forEach((p, i) => i ? ctx.lineTo(x(p.timestampMs), y(p.normalized)) : ctx.moveTo(x(p.timestampMs), y(p.normalized)));
    ctx.strokeStyle = '#37d5c7'; ctx.lineWidth = 1.7; ctx.stroke();
    if (part.length === 1) { ctx.beginPath(); ctx.arc(x(part[0].timestampMs), y(part[0].normalized), 2, 0, Math.PI * 2); ctx.fillStyle = '#37d5c7'; ctx.fill(); }
    const gradient = ctx.createLinearGradient(0, top, 0, bottom); gradient.addColorStop(0, '#37d5c72a'); gradient.addColorStop(1, '#37d5c700');
    ctx.lineTo(x(part.at(-1).timestampMs), bottom); ctx.lineTo(x(part[0].timestampMs), bottom); ctx.closePath(); ctx.fillStyle = gradient; ctx.fill();
  }
  if (hovered) { ctx.setLineDash([3, 4]); ctx.strokeStyle = '#a3b3ca'; ctx.beginPath(); ctx.moveTo(x(hovered.timestampMs), top); ctx.lineTo(x(hovered.timestampMs), bottom); ctx.stroke(); ctx.setLineDash([]); }
  ctx.restore(); ctx.textAlign = 'start';
}

export function drawEEG(canvas, store, { channel = 'all', windowSeconds = 5, end = Date.now() } = {}) {
  const s = surface(canvas); if (!s) return;
  const { ctx, w, h } = s, left = 46, right = 9, top = 8, bottom = h - 24;
  const indices = channel === 'all' ? store.buffers.map((_, i) => i) : [Number(channel)].filter((i) => store.buffers[i]);
  if (!indices.length) return;
  const start = end - windowSeconds * 1000, height = (bottom - top) / indices.length, plotWidth = Math.max(1, Math.floor(w - left - right));
  const x = (t) => left + (t - start) / (end - start) * plotWidth;
  ctx.strokeStyle = '#243248'; ctx.fillStyle = '#8b9db7';
  for (let i = 0; i <= windowSeconds; i++) {
    const xx = left + plotWidth * i / windowSeconds;
    ctx.beginPath(); ctx.moveTo(xx, top); ctx.lineTo(xx, bottom); ctx.stroke();
    ctx.fillText(`${i - windowSeconds}s`, Math.min(w - 20, xx - 8), h - 6);
  }
  indices.forEach((ch, row) => {
    const center = top + height * (row + 0.5), scale = height * 0.42 / 50;
    ctx.fillStyle = COLORS[ch % COLORS.length]; ctx.fillText(store.meta.device.channels[ch], 1, center + 2);
    ctx.fillStyle = '#8294ad'; ctx.font = '12px "Segoe UI", sans-serif'; ctx.fillText('±50 µV', 1, center + 13); ctx.font = '12px "Segoe UI", sans-serif';
    ctx.strokeStyle = '#243248'; ctx.beginPath(); ctx.moveTo(left, center); ctx.lineTo(w - right, center); ctx.stroke();
    ctx.save(); ctx.beginPath(); ctx.rect(left, top + height * row, plotWidth, height); ctx.clip();
    const points = store.buffers[ch].values().filter((p) => p.t >= start && p.t <= end);
    const parts = segments(points, 1.5 * 1000 / store.meta.device.samplingRateHz, () => true);
    ctx.strokeStyle = COLORS[ch % COLORS.length]; ctx.lineWidth = .85;
    for (const part of parts) {
      // Min/max for every pixel column retains spikes while bounding draw work.
      const columns = minMaxColumns(part, plotWidth, start, end);
      ctx.beginPath();
      for (const col of columns) { ctx.moveTo(left + col.x, center - col.min * scale); ctx.lineTo(left + col.x, center - col.max * scale); }
      ctx.stroke();
      ctx.beginPath();
      columns.forEach((col, i) => { const yy = center - (col.min + col.max) / 2 * scale; if (i) ctx.lineTo(left + col.x, yy); else ctx.moveTo(left + col.x, yy); });
      ctx.stroke();
    }
    ctx.restore();
  });
}

export function populateOrb(svgLines, svgParticles) {
  const ns = 'http://www.w3.org/2000/svg';
  // Fibonacci sphere: abstract geometry only, never electrode positions.
  const points = Array.from({ length: 180 }, (_, i) => {
    const y = 1 - i / 179 * 2, r = Math.sqrt(1 - y * y), angle = i * Math.PI * (3 - Math.sqrt(5));
    return { x: 220 + Math.cos(angle) * r * 111, y: 154 + y * 111, z: Math.sin(angle) * r };
  });
  points.forEach((p, i) => {
    const circle = document.createElementNS(ns, 'circle');
    circle.setAttribute('cx', String(p.x)); circle.setAttribute('cy', String(p.y)); circle.setAttribute('r', String(p.z > .3 ? 1.5 : .8)); circle.setAttribute('opacity', String(.25 + (p.z + 1) * .3)); svgParticles.append(circle);
    for (let j = i + 1; j < points.length; j++) {
      const q = points[j], distance = Math.hypot(p.x - q.x, p.y - q.y);
      if (distance < 30 && distance > 14 && Math.abs(p.z - q.z) < .5) {
        const line = document.createElementNS(ns, 'line');
        Object.entries({ x1: p.x, y1: p.y, x2: q.x, y2: q.y, opacity: .15 + (p.z + 1) * .14 }).forEach(([k, v]) => line.setAttribute(k, String(v))); svgLines.append(line);
      }
    }
  });
}
