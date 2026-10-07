/**
 * 「设备实时预览」共用组件：**不建会话**也能看真实脑电。
 *
 * 为什么单独抽出来：设备状态页（#/devices）和"未选会话"的实时监测页（#/live）都要这块能力，
 * 逻辑必须一致，否则两处会各写一套轮询与文案。
 *
 * 产品规则（2026-10-07，用户明确）：
 * - 没有脑机信号时**不使用仿真**：这里只认检测到的实时源，检测不到就显示"无信号"并给接入指引；
 * - 仿真源可以被预览，但必须由人**显式**点「预览仿真源（仅演示）」；
 * - 预览只读实时流（调理 + 质检判定），**不落库、不出指标**——它不代表一次会话。
 */
import { api } from './api.js';
import { button, card, el, list, pick } from './util.js';
import { drawMultiChannelEeg } from './charts.js';

const POLL_MS = 1200;

/**
 * 渲染预览卡。
 * @param {object} ctx 视图上下文（用 ctx.signal.aborted 判断卸载）
 * @param {HTMLElement} host 容器
 */
export function renderPreviewCard(ctx, host) {
  const statusLine = el('p', { class: 'muted', text: '正在探测设备…' });
  const deviceLine = el('p', { class: 'mono', text: '' });
  const qualityLine = el('p', { class: 'muted', text: '' });
  const waveHost = el('div');
  const actionHost = el('div', { class: 'row', style: 'margin:10px 0' });
  let timer = null;
  let running = false;

  const stopPolling = () => {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  };
  ctx.onCleanup(stopPolling);

  const paint = (payload) => {
    const active = payload && payload.active === true;
    const noSignal = !active || payload.no_signal === true;
    if (!active) {
      statusLine.textContent = payload && payload.note
        ? payload.note
        : '未开始预览：点击下方按钮读取当前检测到的脑电信号。';
      deviceLine.textContent = '';
      qualityLine.textContent = '';
      waveHost.textContent = '';
      waveHost.append(el('p', { class: 'muted', text: '预览不会创建会话，也不写入数据库。' }));
      return;
    }
    const live = payload.live !== false && payload.no_signal !== true;
    statusLine.textContent = live
      ? `正在接收信号：${payload.device || payload.source}（${payload.kind === 'sim' ? '仿真源，仅演示' : '实时设备'}）`
      : `无信号：数据源 ${payload.source} 已连接但最近没有样本`
        + (payload.seconds_since_last === null || payload.seconds_since_last === undefined
          ? '' : `（最近一个样本 ${payload.seconds_since_last}s 前）`);
    deviceLine.textContent = [
      `采样率 ${payload.srate || '—'} Hz`,
      `通道 ${payload.channels || '—'}`,
      payload.buffered_samples === null || payload.buffered_samples === undefined
        ? null : `缓冲 ${payload.buffered_samples} 点`,
      payload.conditioning && payload.conditioning.spec ? `调理 ${payload.conditioning.spec}` : null,
    ].filter(Boolean).join('｜');

    const samples = list(payload.samples);
    if (!samples.length) {
      waveHost.textContent = '';
      waveHost.append(el('p', { class: 'muted', text: '这一窗没有取到样本（设备可能刚接上，等一两秒）。' }));
    } else {
      // 先清掉上一次的"没取到样本"提示或旧画布，否则会残留在新波形上方
      waveHost.textContent = '';
      const frame = {
        srate: payload.srate,
        window_sec: payload.seconds,
        channels: samples.map((channel, index) => ({
          label: list(payload.channel_labels)[index] || `CH${index + 1}`,
          samples: channel,
          peak: payload.peak_uv,
        })),
      };
      drawMultiChannelEeg(waveHost, frame, { srate: payload.srate, windowSec: payload.seconds, channelHeight: 110 });
    }
    const verdict = pick(payload, 'quality', null);
    const note = pick(payload, 'quality_note', null);
    if (verdict) {
      const ok = verdict.ok;
      const label = ok === true ? '可用'
        : (ok === false ? `不可用（${list(verdict.reasons).join('、')}）` : '样本不足，暂不判定');
      qualityLine.textContent = `本窗质检：${label}`
        + `｜峰峰值 ${payload.peak_uv} µV｜均值标准差 ${payload.std_uv} µV`
        + (note ? `｜${note}` : '')
        + '（判定门槛与报告一致：config.quality）';
    } else {
      qualityLine.textContent = '';
    }
  };

  const refresh = async () => {
    try {
      const response = running ? await api.devicePreviewWindow() : await api.devicePreview();
      if (ctx.signal.aborted) return;
      paint(response.data);
      const active = pick(response.data, 'active', false) === true;
      const real = pick(response.data, 'kind', null) === 'lsl';
      if (active !== running || (active && real !== (actionHost.dataset.real === '1'))) {
        running = active;
        actionHost.dataset.real = real ? '1' : '0';
        renderActions();
      }
    } catch (error) {
      if (ctx.signal.aborted) return;
      statusLine.textContent = `读取预览失败：${error && error.message ? error.message : error}`;
    }
  };

  const start = async (source) => {
    statusLine.textContent = '正在启动预览…';
    try {
      const response = await api.startDevicePreview(source);
      if (ctx.signal.aborted) return;
      running = true;
      actionHost.dataset.real = pick(response.data, 'kind', null) === 'lsl' ? '1' : '0';
      paint(response.data);
      renderActions();
      stopPolling();
      timer = window.setInterval(refresh, POLL_MS);
    } catch (error) {
      // 没有实时信号时后端返回 409 与接入指引，直接展示给现场
      statusLine.textContent = error && error.message ? error.message : String(error);
      running = false;
      renderActions();
    }
  };

  const stop = async () => {
    stopPolling();
    running = false;
    try {
      await api.stopDevicePreview();
    } catch (error) {
      // 停止失败不影响界面复原（后端是幂等的）
    }
    if (ctx.signal.aborted) return;
    renderActions();
    paint({ active: false, note: '预览已停止。' });
  };

  const renderActions = () => {
    actionHost.textContent = '';
    if (running) {
      actionHost.append(
        button('停止预览', stop, { small: true }),
        el('span', { class: 'muted', text: '预览只读实时流，不创建会话、不写入数据库' }),
      );
      return;
    }
    actionHost.append(
      button('开始预览（自动选检测到的设备）', () => start(null), { primary: true, small: true }),
      button('预览仿真源（仅演示）', () => start('sim-bsense'), { small: true }),
    );
  };

  renderActions();
  host.append(card('设备实时预览', el('div', {}, [
    el('p', { class: 'muted', text: '不开始会话也能看当前脑电信号：读取检测到的 LSL 流，做与报告一致的真机调理，'
      + '并给出这一窗的质检判定。' }),
    actionHost,
    deviceLine,
    statusLine,
    waveHost,
    qualityLine,
  ]), { sub: '来源：GET/POST/DELETE /api/devices/preview（无会话预览）' }));
  refresh();
  return { stopPolling };
}
