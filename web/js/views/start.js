/**
 * 「开始一次检测」向导：**三步，一屏一步**。
 *
 * 为什么单独做一个视图：原来要让别人跑一次检测，得自己明白"先去被试管理建被试 →
 * 回到同一页填设备名与时间倍率 → 点开始新会话 → 跳到会话流程"。作者知道这条链路，
 * 别人不知道。这里把它变成一条有编号、有下一步、每步只问一件事的向导：
 *
 *   ① 选/建被试  →  ② 确认设备有信号（可现场看波形）  →  ③ 选节奏与训练模式 → 开始
 *
 * 设计约束（与项目既有规则一致）：
 * - **不静默用仿真**：检测不到实时源时明确提示并给接线指引，"仿真源"必须显式选；
 * - 一步一屏（GOV.UK one-thing-per-page）：每屏只问一件事，底部只有"上一步/下一步"；
 * - 开始后直接进入 `#/flow?session=…&subject=1`，即**被测者视图**（隐藏操作者界面）。
 */
import { api } from '../api.js';
import { describeError, toast } from '../store.js';
import { button, card, el, empty, fmtInt, field, list, pick } from '../util.js';
import { renderPreviewCard } from '../preview.js';

const STEPS = [
  { key: 'subject', title: '选被试', hint: '这次检测记录给谁' },
  { key: 'device', title: '确认设备', hint: '有没有脑电信号' },
  { key: 'run', title: '节奏与开始', hint: '多久、怎么跑' },
];

const local = { step: 0, subject: null, device: '', scale: 1.0, mode: 'quick', protocol: 'full',
                label: '', filter: '', previewBuilt: false, previewHost: null };

const SCALE_CHOICES = [
  { value: 1.0, title: '真实节奏', note: '按真实时间跑；量表、SART、PVT 都由被测者本人作答（正式检测用这个）' },
  { value: 0.05, title: '快速演示', note: '约 2 分钟；量表与按键任务由服务端自动作答，只看流程与报告（演示/自测用）' },
];

// 协议档：完整（默认）与短协议。负担/时长由 /api/config 的 profiles 提供，这里只放标题与"省在哪"。
const PROTOCOL_CHOICES = [
  { value: 'full', title: '完整协议（默认）',
    note: '11 步全跑：含神经反馈训练与模型训练；SART 180 试次、PVT 3 分钟' },
  { value: 'short', title: '短协议',
    note: '9 步：不跑训练与模型；SART 90 试次（No-Go 10）、PVT 2 分钟——行为指标精度下降，报告会标注' },
];

const MODE_CHOICES = [
  { value: 'quick', title: '简短', note: '训练 2 段（约 4 分钟）' },
  { value: 'full', title: '标准', note: '训练 4 段（约 8 分钟）' },
];

function choiceCard(title, note, selected, onPick) {
  return el('button', {
    class: 'choice' + (selected ? ' choice--on' : ''),
    type: 'button',
    onClick: onPick,
  }, [
    el('span', { class: 'choice__title', text: title }),
    el('span', { class: 'choice__note', text: note }),
  ]);
}

export async function render(container, ctx) {
  container.textContent = '';
  const host = el('div', { class: 'wizard' });
  container.append(host);
  local.previewBuilt = false;
  local.previewHost = el('div');

  const bodyHost = el('div');
  const footHost = el('div', { class: 'row row--between wizard__foot' });
  const stepsHost = el('div', { class: 'wizard__steps' });

  // 被试候选：进页面就取一次（向导里要能"选"或"建"）
  let subjects = [];
  try {
    const response = await api.listSubjects({ limit: 200 });
    subjects = list(pick(response.data, 'items', []));
  } catch (error) {
    if (ctx.signal.aborted) return;
    subjects = [];
  }
  // 设备候选：探测一次（下一步会用到；失败不阻塞，被"0 个"处理）
  let sources = [];
  try {
    const response = await api.devices(1.2);
    sources = list(pick(response.data, 'sources', []));
  } catch (error) {
    sources = [];
  }
  // 协议参数（阶段时长、SART/PVT 试次、量表题数）：用于第 3 步如实告诉操作者"要花多久、要动几次手"，
  // 不在这里写死数字——口径来自 /api/config（引擎常量）。
  let protocol = {};
  try {
    const response = await api.config();
    protocol = pick(response.data, 'behavior', {}) || {};
    protocol.phases = list(pick(response.data, 'phases', []));
    // 协议档也必须带过来：第 3 步的档位卡与负担行都靠它
    // （第一版漏了这一行，界面"看起来正常"但协议档根本没渲染——探针断言当时太弱没抓到）
    protocol.profiles = list(pick(response.data, 'profiles', []));
  } catch (error) {
    protocol = {};
  }
  if (ctx.signal.aborted) return;

  const real = sources.filter((item) => pick(item, 'real')
    && !String(pick(item, 'key', '')).startsWith('unsupported:'));
  const realDevice = real.find((item) => !pick(item, 'simulated', false)) || real[0] || null;
  if (!local.device) local.device = realDevice ? String(pick(realDevice, 'key', '')) : '';

  const paintSteps = () => {
    stepsHost.textContent = '';
    STEPS.forEach((step, index) => {
      const state = index < local.step ? 'done' : (index === local.step ? 'now' : 'todo');
      stepsHost.append(el('div', { class: `wizard__step wizard__step--${state}` }, [
        el('span', { class: 'wizard__step-no mono', text: state === 'done' ? '✓' : String(index + 1) }),
        el('span', { class: 'wizard__step-title', text: step.title }),
        el('span', { class: 'wizard__step-hint', text: step.hint }),
      ]));
    });
  };

  const goto = (step) => {
    local.step = Math.max(0, Math.min(STEPS.length - 1, step));
    paint();
  };

  /**
   * 当前选的数据源能不能用来跑本产品。
   * 仿真源可以（显式选的演示）；真实流必须是脑电；手填的键交给后端判定。
   * 用于两处：第 2 步「下一步」的门控、以及选中非脑电流时的显式警告。
   */
  const deviceIsUsable = () => {
    if (!local.device) return false;
    if (local.device === 'sim-bsense') return true;
    const row = real.find((item) => String(pick(item, 'key', '')) === local.device);
    if (!row) return true;
    return pick(row, 'usable', true) !== false;
  };

  const canNext = () => {
    if (local.step === 0) return Boolean(local.subject);
    if (local.step === 1) return deviceIsUsable();
    return true;
  };

  const paintFoot = () => {
    footHost.textContent = '';
    footHost.append(
      local.step > 0
        ? button('上一页', () => goto(local.step - 1), { small: true })
        : el('span', { class: 'muted', text: '第 1 步：先确定这次检测记录给谁' }),
      local.step < STEPS.length - 1
        ? button('下一步', () => goto(local.step + 1), { primary: true, disabled: !canNext() })
        : button('开始检测', startSession, { primary: true, disabled: !canNext() }),
    );
  };

  /* ---------------------------------------------------------- 第 1 步：被试 */
  const paintSubjectStep = () => {
    const host2 = el('div', { class: 'stack' });
    if (subjects.length) {
      const keyword = (local.filter || '').trim().toLowerCase();
      const matched = keyword
        ? subjects.filter((item) => (`sub-${pick(item, 'public_id', '')} ${pick(item, 'label', '') || ''}`)
          .toLowerCase().includes(keyword))
        : subjects;
      // 被试多的时候（开发库里常有上百个）必须能筛：否则一屏几十个格子谁也找不到人
      if (subjects.length > 12) {
        const filterInput = el('input', {
          id: 'wizard-subject-filter', type: 'search', placeholder: '按编号或别名筛选，例如 p03 / 张三',
          value: local.filter || '',
        });
        filterInput.addEventListener('input', () => { local.filter = filterInput.value; paint(); });
        host2.append(el('div', { class: 'form-grid' }, [
          field(`筛选被试（共 ${fmtInt(subjects.length)} 位）`, filterInput, '输入编号或别名的一部分即可'),
        ]));
        // 重绘后恢复焦点与光标：否则每输一个字符光标就丢
        queueMicrotask(() => {
          const again = host2.querySelector('#wizard-subject-filter');
          if (again && local.filter !== null && document.activeElement !== again) {
            again.focus();
            again.setSelectionRange(again.value.length, again.value.length);
          }
        });
      }
      const listHost = el('div', { class: 'pick-list pick-list--scroll' });
      for (const item of matched.slice(0, 200)) {
        const id = pick(item, 'public_id', '');
        const label = pick(item, 'label', null);
        listHost.append(el('button', {
          class: 'pick' + (local.subject === id ? ' pick--on' : ''),
          type: 'button',
          onClick: () => { local.subject = id; paint(); },
        }, [
          el('span', { class: 'pick__title mono', text: `sub-${id}` }),
          el('span', { class: 'pick__note', text: label || '（无别名）' }),
        ]));
      }
      host2.append(el('p', { class: 'muted', text: matched.length
        ? `点一个选中（当前显示 ${fmtInt(matched.length)} 位）；没有合适的就在下面新建。`
        : '没有匹配的被试：清空筛选，或在下面新建。' }));
      host2.append(listHost);
    } else {
      host2.append(empty('还没有被试：在下面填一个别名（编号可留空自动生成）即可开始。'));
    }

    // 新建被试：主要动作是"选已有"，新建是次要动作 —— 收进折叠，避免表单把卡片撑高
    const details = el('details', { class: 'wizard__others' });
    if (!subjects.length) details.open = true;
    details.append(el('summary', { text: subjects.length ? '没有合适的？点这里新建一位被试' : '新建一位被试' }));
    // 只问必要的一件事（别名），编号/同意版本走默认值
    const labelInput = el('input', { id: 'wizard-subject-label', type: 'text', placeholder: '例如：张三 / 演示被试' });
    const idInput = el('input', { id: 'wizard-subject-id', type: 'text', placeholder: '留空自动生成', maxlength: 8 });
    const createBtn = button('新建并选中', async () => {
      const label = labelInput.value.trim();
      if (!label && !idInput.value.trim()) {
        toast('填一个别名或编号即可（别名只在本地使用）', 'error');
        return;
      }
      createBtn.disabled = true;
      createBtn.textContent = '创建中…';
      try {
        const response = await api.createSubject({
          auto_id: !idInput.value.trim(),
          public_id: idInput.value.trim() || null,
          label: label || null,
          consent_version: 'consent-v1',
        });
        const created = pick(response.data, 'subject', response.data) || {};
        const id = pick(created, 'public_id', idInput.value.trim());
        subjects = [created, ...subjects];
        local.subject = id;
        toast(`已创建并选中 sub-${id}`, 'info');
        paint();
      } catch (error) {
        toast(describeError(error));
        createBtn.disabled = false;
        createBtn.textContent = '新建并选中';
      }
    }, { primary: true, small: true });
    details.append(el('div', { class: 'form-grid' }, [
      field('别名（必填其一）', labelInput, '只在本机用于区分，写昵称即可'),
      field('编号（可留空）', idInput, '留空按 p01、p02… 自动生成'),
    ]));
    details.append(el('div', { class: 'row' }, [createBtn]));
    host2.append(details);
    return host2;
  };

  /**
   * 第 2 步：确认设备。
   *
   * 用户实测提问："这六个实时数据，应该怎么选择"——一台 BioMultiLite 会同时推
   * EEG / Metric / FNIRS / HeartRate / General Metric / Motion 六条流，界面原来把它们
   * 平铺成一排同等权重，谁也看不出该选哪个。实际只有 **EEG** 那条能用：
   * 质检、频谱、专注/放松/负荷指标与 SART/PVT 判定全都基于脑电通道。
   * 所以这里分成两段：脑电（推荐、默认选中）+ 折叠起来的"其它传感器流（本产品不使用）"。
   */
  const paintDeviceStep = () => {
    const host2 = el('div', { class: 'stack' });
    if (real.length) {
      host2.append(el('p', { class: 'wizard__ok', text: `✓ 检测到 ${fmtInt(real.length)} 个实时数据源` }));
      const eegStreams = real.filter((item) => pick(item, 'is_eeg', false) === true);
      const otherStreams = real.filter((item) => pick(item, 'is_eeg', false) !== true);
      const chip = (item) => {
        const key = String(pick(item, 'key', ''));
        const simulated = Boolean(pick(item, 'simulated', false));
        const kindLabel = pick(item, 'stream_kind_label', null) || '未知类型';
        return el('button', {
          class: 'pick' + (local.device === key ? ' pick--on' : ''),
          type: 'button',
          onClick: () => { local.device = key; paint(); },
        }, [
          el('span', { class: 'pick__title', text: String(pick(item, 'device', key)) }),
          el('span', { class: 'pick__note', text: `${kindLabel}｜${pick(item, 'channels', '?')} 通道 · ${pick(item, 'srate', '?')} Hz`
            + (simulated ? '｜内置仿真流（非真实设备）' : '') }),
        ]);
      };

      if (eegStreams.length) {
        host2.append(el('p', { class: 'wizard__section', text: '脑电（EEG）— 本产品只用这一条' }));
        const listHost = el('div', { class: 'pick-list' });
        for (const item of eegStreams) listHost.append(chip(item));
        host2.append(listHost);
        host2.append(el('p', { class: 'muted', text: '质检、频谱、专注/放松/负荷指标与 SART/PVT 判定都基于脑电通道，'
          + '所以选它。默认已经选好，直接「下一步」即可。' }));
      } else {
        host2.append(el('p', { class: 'wizard__warn', text: '✗ 没有检测到脑电（EEG）流' }));
        host2.append(el('p', { class: 'muted', text: '下面这些是设备附带的其它传感器流，本产品不用它们做指标；'
          + '请确认 BioMultiLite 已勾选 EEG 并开始推流。' }));
      }

      if (otherStreams.length) {
        const kinds = [...new Set(otherStreams.map((item) => pick(item, 'stream_kind_label', null) || '未知类型'))];
        const details = el('details', { class: 'wizard__others' });
        details.append(el('summary', {
          text: `其它 ${fmtInt(otherStreams.length)} 个传感器流（${kinds.join(' / ')}）— 本产品不使用，仅列出`,
        }));
        const otherHost = el('div', { class: 'pick-list' });
        for (const item of otherStreams) otherHost.append(chip(item));
        details.append(otherHost);
        details.append(el('p', { class: 'muted', text: '这些流是同一台设备附带的传感器数据（近红外、运动、心率、设备指标等），'
          + '本产品不读取它们；选它们开始检测会被后端拒绝（409），因为那样跑出的报告没有意义。' }));
        host2.append(details);
      }
    } else {
      host2.append(el('p', { class: 'wizard__warn', text: '✗ 没有检测到脑电信号' }));
      host2.append(el('div', { class: 'stack' }, [
        el('p', { class: 'muted', text: '按顺序检查：① 采集端（如 BioMultiLite / BSense-R）是否已启动并开始采集；'
          + '② 设备是否已连上电脑；③ 到「设备状态」页点「开始预览」确认能看到波形。' }),
      ]));
    }
    // 仿真源必须显式选：明确写清"这不是真实设备"
    host2.append(el('hr', { class: 'wizard__sep' }));
    host2.append(el('div', { class: 'row' }, [
      choiceCard('用仿真源演示（非真实设备）', '不接硬件也能把流程走完；报告里会标注"仿真"',
        local.device === 'sim-bsense', () => { local.device = 'sim-bsense'; paint(); }),
    ]));
    if (local.device && local.device !== 'sim-bsense' && deviceIsUsable() === false) {
      const row = real.find((item) => String(pick(item, 'key', '')) === local.device) || {};
      host2.append(el('p', { class: 'wizard__warn', text: `所选流不是脑电流（${pick(row, 'stream_kind_label', '未知类型')}）：`
        + '本产品需要脑电通道，请改选上面的 EEG 流，或显式选修仿真源做演示。' }));
    }
    host2.append(el('p', { class: 'muted', text: '选定后建议先在下面点「开始预览」，看到波形再进入下一步——'
      + '否则开始检测后才发现没信号，会白等一次。' }));
    // 预览卡只建一次：renderPreviewCard 会注册轮询定时器，每次 paint() 都重建会越积越多
    // （每次重建都新起一个 interval，只有第一个在卸载时被清掉）。节点复用即可。
    if (!local.previewBuilt) {
      renderPreviewCard(ctx, local.previewHost);
      local.previewBuilt = true;
    }
    host2.append(local.previewHost);
    return host2;
  };

  /* ---------------------------------------------------------- 第 3 步：节奏 */
  const paintRunStep = () => {
    const host2 = el('div', { class: 'stack' });
    host2.append(el('p', { class: 'muted', text: '选节奏（决定整场检测多久、要不要被测者本人操作）' }));
    const scaleHost = el('div', { class: 'choice-row' });
    for (const item of SCALE_CHOICES) {
      scaleHost.append(choiceCard(item.title, item.note, local.scale === item.value,
        () => { local.scale = item.value; paint(); }));
    }
    host2.append(scaleHost);
    // 协议档：完整 / 短协议。阶段数、时长、行为任务试次数全部来自 /api/config 的 profiles，
    // 界面只负责展示"省了哪几步、代价是什么"（不写死数字）。
    const profiles = list(protocol.profiles);
    const picked = profiles.find((item) => pick(item, 'key', '') === local.protocol) || profiles[0] || null;
    if (profiles.length) {
      host2.append(el('p', { class: 'muted', text: '选检测协议（决定跑几步、行为证据多细）' }));
      const protocolHost = el('div', { class: 'choice-row' });
      for (const item of profiles) {
        const key = String(pick(item, 'key', ''));
        const meta = [pick(item, 'summary', '')];
        if (Number(pick(item, 'phase_count', 0))) meta.push(`${fmtInt(pick(item, 'phase_count'))} 步`);
        if (Number(pick(item, 'total_sec', 0))) {
          meta.push(`约 ${(Number(pick(item, 'total_sec')) / 60).toFixed(0)} 分钟`);
        }
        if (key === 'short' && Number(pick(item, 'saved_sec', 0))) {
          meta.push(`比完整协议少 ${(Number(pick(item, 'saved_sec')) / 60).toFixed(1)} 分钟`);
        }
        const fallback = PROTOCOL_CHOICES.find((row) => row.value === key) || {};
        protocolHost.append(choiceCard(pick(item, 'label', fallback.title || key),
          meta.filter(Boolean).join('｜') || fallback.note || '', local.protocol === key,
          () => { local.protocol = key; paint(); }));
      }
      host2.append(protocolHost);
      if (pick(picked, 'caveat', null)) {
        host2.append(el('p', { class: 'wizard__warn', text: pick(picked, 'caveat') }));
      }
    }
    host2.append(el('p', { class: 'muted', text: '选训练时长' }));
    const modeHost = el('div', { class: 'choice-row' });
    for (const item of MODE_CHOICES) {
      modeHost.append(choiceCard(item.title, item.note, local.mode === item.value,
        () => { local.mode = item.value; paint(); }));
    }
    host2.append(modeHost);
    if (local.protocol === 'short') {
      // 短协议不跑训练：训练时长这个选项就没意义了，如实说明而不是让人选了没效果
      host2.append(el('p', { class: 'muted', text: '（短协议不跑神经反馈训练，上面的训练时长不生效）' }));
    }
    const labelInput = el('input', {
      id: 'wizard-session-label', type: 'text', placeholder: '例如：第一次检测', value: local.label,
    });
    labelInput.addEventListener('input', () => { local.label = labelInput.value; });
    host2.append(field('本次备注（可留空）', labelInput, '只写进本地记录，便于以后查找'));

    const subject = subjects.find((item) => pick(item, 'public_id') === local.subject);
    // 如实算出"这次检测要花多久、要动几次手"——**按当前选的协议档**算
    // （用户问过"会话流程中的操作都是必要的吗"；短协议的试次数与时长都不同）
    const allPhases = list(protocol.phases);
    const activeKeys = list(pick(picked, 'phases', []));
    const phases = activeKeys.length
      ? allPhases.filter((item) => activeKeys.includes(pick(item, 'key', '')))
      : allPhases;
    const totalSec = Number(pick(picked, 'total_sec', 0))
      || phases.reduce((sum, item) => sum + (Number(pick(item, 'duration_sec', 0)) || 0), 0);
    const interactive = phases.filter((item) => pick(item, 'interactive', false));
    const pvt = pick(protocol, 'pvt', {}) || {};
    const scales = pick(protocol, 'scales', {}) || {};
    const isi = list(pick(pvt, 'isi_sec', [1, 4]));
    const isiMean = isi.length ? (Number(isi[0]) + Number(isi[isi.length - 1])) / 2 : 2.5;
    const pvtSec = Number(pick(picked, 'pvt_duration_sec', 0))
      || Number(pick(pvt, 'duration_sec', 0));
    const pvtTrials = pvtSec ? Math.round(pvtSec / isiMean) : null;
    const sartTotal = (Number(pick(picked, 'sart_practice_trials', 0)) || 0)
      + (Number(pick(picked, 'sart_trials', 0)) || 0);
    const scaleItems = (Number(pick(scales, 'items_per_scale', 20)) || 20)
      * list(pick(scales, 'codes', ['SAS', 'SDS'])).length;
    const dropped = list(pick(picked, 'dropped_phases', []));
    const droppedLabels = dropped.map((key) => {
      const row = allPhases.find((item) => pick(item, 'key') === key);
      return row ? pick(row, 'label', key) : key;
    });
    const burden = [];
    if (totalSec) burden.push(`整场约 ${(totalSec / 60).toFixed(0)} 分钟`);
    if (interactive.length) {
      burden.push(`需要被测者动手的有 ${fmtInt(interactive.length)} 步：量表 ${fmtInt(scaleItems)} 题、`
        + `SART ${fmtInt(sartTotal)} 个试次（No-Go ${fmtInt(pick(picked, 'sart_nogo_trials', 0) || 0)} 个）、`
        + `PVT ${pvtTrials ? `约 ${fmtInt(pvtTrials)} 次按键` : '按键'}`);
    }
    if (phases.length) {
      burden.push(`其余 ${fmtInt(phases.length - interactive.length)} 步全自动（坐好等待即可，不用点任何东西）`);
    }
    if (droppedLabels.length) {
      burden.push(`本次不跑：${droppedLabels.join('、')}`);
    }
    host2.append(card('这次检测会发生什么', el('div', { class: 'stack' }, [
      burden.length ? el('p', { class: 'wizard__burden', text: burden.join('；') + '。' }) : null,
      el('ul', { class: 'wizard__plan' }, [
        el('li', { text: '① 设备质检 → ② 睁眼/闭眼静息基线 → ③ 量表（SAS/SDS 各 20 题）' }),
        el('li', { text: '④ SART 持续注意（看到数字按空格，看到 3 不要按）→ ⑤ PVT 警觉度（出现红点尽快按）' }),
        el('li', { text: '⑥ 任务态监测 → ⑦ 神经反馈训练 → ⑧ 联合评估、模型与报告' }),
        el('li', { text: local.scale === 1.0
          ? '需要被测者配合的只有 ③④⑤；其余阶段保持安静坐好即可'
          : '快速演示：③④⑤ 由服务端自动作答，不需要人操作' }),
      ]),
    ]), { sub: `被试 sub-${pick(subject, 'public_id', local.subject || '—')}`
      + `｜设备 ${local.device || '—'}｜${local.scale === 1.0 ? '真实节奏' : '快速演示'}` }));
    host2.append(el('p', { class: 'muted', text: '点「开始检测」后页面会自动切到被测者视图'
      + '（隐藏导航与顶栏，只留当前要做的动作）；操作者按 Esc 可随时退出该视图。' }));
    return host2;
  };

  const paint = () => {
    paintSteps();
    bodyHost.textContent = '';
    const step = STEPS[local.step];
    const body = local.step === 0 ? paintSubjectStep()
      : (local.step === 1 ? paintDeviceStep() : paintRunStep());
    bodyHost.append(card(step.title, body, { sub: step.hint }));
    paintFoot();
  };

  async function startSession() {
    const payload = {
      participant: local.subject,
      device: local.device || null,
      time_scale: local.scale,
      training_mode: local.mode,
      protocol: local.protocol,
      label: local.label.trim() || null,
      create_subject: false,
    };
    const startBtn = footHost.querySelector('button:last-child');
    if (startBtn) { startBtn.disabled = true; startBtn.textContent = '启动中…'; }
    try {
      const response = await api.createSession(payload);
      const uuid = pick(response.data, 'session.uuid');
      ctx.store.setState({
        currentSession: pick(response.data, 'session', null),
        phases: list(pick(response.data, 'phases', [])),
        sessionRuntime: pick(response.data, 'runtime', null),
      });
      toast('检测已开始，已切到被测者视图（按 Esc 退出）', 'info');
      ctx.navigate(`#/flow?session=${uuid}&subject=1`);
    } catch (error) {
      toast(describeError(error));
      paint();
    }
  }

  host.append(
    el('div', { class: 'row row--between' }, [
      el('h1', { text: '开始一次检测' }),
      button('看历史记录', () => ctx.navigate('#/history'), { small: true }),
    ]),
    stepsHost,
    bodyHost,
    footHost,
  );
  paint();
}
