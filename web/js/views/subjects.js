/**
 * 被试管理：列表 / 新建 / 详情（会话列表 + 开始新会话）。
 *
 * 数据来源：
 * - GET  /api/subjects、POST /api/subjects、PATCH /api/subjects/{public_id}
 * - GET  /api/subjects/{public_id}/sessions
 * - POST /api/sessions（time_scale 默认 0.05 快速演示，训练模式 quick/full）
 */

import { api } from '../api.js';
import { describeError, toast } from '../store.js';
import {
  DASH, button, card, el, empty, field, fmtDate, fmtInt, fmtNum, fmtPercent,
  fmtRelative, list, pick, statusText, table,
} from '../util.js';

const AGE_BANDS = ['16-17', '18-25', '26-35', '36-45', '46-55', '56-65', '65+'];
const SEX_OPTIONS = ['女', '男', '不便告知'];
const HANDEDNESS_OPTIONS = ['右', '左', '双利手'];

/** 视图内状态：切换被试/刷新时保留（视图卸载即丢弃）。 */
const view = { publicId: null, query: '' };

function selectInput(id, options, { placeholder = '请选择', value = '' } = {}) {
  const node = el('select', { id });
  node.append(el('option', { value: '', text: placeholder }));
  for (const option of options) node.append(el('option', { value: option, text: option }));
  node.value = value;
  return node;
}

function renderSubjectForm(ctx, onCreated) {
  const idInput = el('input', {
    id: 'subject-public-id',
    type: 'text',
    placeholder: '留空自动生成',
    maxlength: 8,
  });
  const labelInput = el('input', { id: 'subject-label', type: 'text', placeholder: '例如：演示被试' });
  const ageSelect = selectInput('subject-age', AGE_BANDS);
  const sexSelect = selectInput('subject-sex', SEX_OPTIONS);
  const handSelect = selectInput('subject-hand', HANDEDNESS_OPTIONS);
  const noteInput = el('input', { id: 'subject-note', type: 'text', placeholder: '例如：仅用于演示' });
  const consentInput = el('input', { id: 'subject-consent', type: 'text', value: 'consent-v1' });
  const submit = button('创建被试', null, { primary: true });

  submit.addEventListener('click', async () => {
    submit.disabled = true;
    submit.textContent = '创建中…';
    try {
      const body = {
        auto_id: !idInput.value.trim(),
        label: labelInput.value.trim() || null,
        age_band: ageSelect.value || null,
        sex: sexSelect.value || null,
        handedness: handSelect.value || null,
        note: noteInput.value.trim() || null,
        consent_version: consentInput.value.trim() || 'consent-v1',
      };
      if (idInput.value.trim()) body.public_id = idInput.value.trim();
      const response = await api.createSubject(body);
      const newId = pick(response.data, 'public_id', '');
      toast(`已创建被试 sub-${newId}，可在下方列表里看到，并可用它开始新会话`, 'info');
      idInput.value = '';
      labelInput.value = '';
      noteInput.value = '';
      ageSelect.value = '';
      sexSelect.value = '';
      handSelect.value = '';
      await onCreated(newId);
    } catch (error) {
      toast(describeError(error));
    } finally {
      submit.disabled = false;
      submit.textContent = '创建被试';
    }
  });

  return card('新建被试', el('div', {}, [
    el('div', { class: 'form-grid' }, [
      field('被试编号', idInput, '留空即自动生成 p01、p02…（也叫编号）；自定义请用「字母+数字」格式，如 p07 / sub-p07'),
      field('别名', labelInput, '姓名等直接身份信息不进入报告'),
      field('年龄段', ageSelect),
      field('性别', sexSelect),
      field('利手', handSelect),
      field('同意版本', consentInput),
      field('备注', noteInput),
    ]),
    el('div', { class: 'row row--end', style: 'margin-top:12px' }, [submit]),
  ]), { sub: '编号留空时由后端按 p01、p02… 自动生成；重复编号会返回 409' });
}

function renderNewSessionPanel(ctx, publicId, options = {}) {
  // 时间倍率选项显式列出，避免 select 索引与文案错位
  const scaleSelect = selectInput('session-time-scale', [], { placeholder: '' });
  for (const [value, text] of [
    ['0.05', '0.05（快速演示，20 倍速，后端自动作答）'],
    ['0.2', '0.2（半自动，仍与真实节奏不同）'],
    ['0.5', '0.5（慢速）'],
    ['1.0', '1.0（真实节奏，每窗 2 秒）'],
  ]) {
    scaleSelect.append(el('option', { value, text }));
  }
  scaleSelect.value = '0.05';
  const modeSelect = selectInput('session-mode', [], { placeholder: '' });
  for (const [value, text] of [
    ['quick', 'quick（2 段训练）'],
    ['full', 'full（4 段训练）'],
  ]) {
    modeSelect.append(el('option', { value, text }));
  }
  modeSelect.value = 'quick';
  const deviceInput = el('input', { id: 'session-device', type: 'text', value: 'sim-bsense' });
  const labelInput = el('input', { id: 'session-label', type: 'text', placeholder: '例如：第一次训练' });
  const submit = button('开始新会话', null, { primary: true });

  // 列表态需要先挑被试；详情态被试已确定，直接显示编号
  const subjectOptions = list(options.subjects);
  let subjectSelect = null;
  if (subjectOptions.length) {
    subjectSelect = selectInput('session-subject', [], { placeholder: '' });
    for (const item of subjectOptions) {
      const id = pick(item, 'public_id');
      if (!id) continue;
      const label = pick(item, 'label');
      subjectSelect.append(el('option', {
        value: id,
        text: `sub-${id}${label ? `（${label}）` : ''}`,
      }));
    }
    subjectSelect.value = publicId || pick(subjectOptions[0], 'public_id') || '';
  }
  const who = subjectSelect
    ? field('被试', subjectSelect, '选好要跑这次会话的被试')
    : el('p', { class: 'muted', text: `被试：sub-${publicId || DASH}` });

  submit.addEventListener('click', async () => {
    const participant = subjectSelect
      ? (subjectSelect.value || '').trim()
      : (publicId || '').trim();
    if (!participant) {
      toast('请先选择被试（若还没有被试，先用上方表单创建一个）', 'error');
      return;
    }
    submit.disabled = true;
    submit.textContent = '启动中…';
    try {
      const response = await api.createSession({
        participant,
        device: deviceInput.value.trim() || 'sim-bsense',
        time_scale: Number(scaleSelect.value),
        training_mode: modeSelect.value,
        label: labelInput.value.trim() || null,
        create_subject: true,
      });
      const uuid = pick(response.data, 'session.uuid');
      ctx.store.setState({
        currentSession: pick(response.data, 'session', null),
        phases: list(pick(response.data, 'phases', [])),
        sessionRuntime: pick(response.data, 'runtime', null),
      });
      toast('会话已启动，正在进入八阶段流程', 'info');
      ctx.navigate(`#/flow?session=${uuid}`);
    } catch (error) {
      toast(describeError(error));
    } finally {
      submit.disabled = false;
      submit.textContent = '开始新会话';
    }
  });

  return card('开始新会话', el('div', {}, [
    el('p', { class: 'muted', text: 'time_scale < 0.2 时后端会生成确定性作答（量表/ SART / PVT 无需前端交互），用于演示与自动测试；1.0 为真实节奏。' }),
    el('div', { class: 'form-grid' }, [
      who,
      field('时间倍率 time_scale', scaleSelect),
      field('训练模式', modeSelect),
      field('设备 / 数据源', deviceInput, '仿真源 sim-bsense；接入设备请填 lsl:<流名称>'),
      field('会话标签', labelInput),
    ]),
    el('div', { class: 'row row--end', style: 'margin-top:12px' }, [submit]),
  ]), { sub: '提交后会直接跳到“会话流程”，约 1–2 分钟跑完（快速演示模式）' });
}

async function renderSubjectDetail(host, ctx) {
  const publicId = view.publicId;
  let subject = null;
  let sessions = [];
  try {
    const [subjectResponse, sessionsResponse] = await Promise.all([
      api.getSubject(publicId),
      api.subjectSessions(publicId, { limit: 20, page: 1 }),
    ]);
    subject = subjectResponse.data;
    sessions = list(pick(sessionsResponse.data, 'items', []));
  } catch (error) {
    host.append(card(`被试 sub-${publicId}`, [empty(`加载失败：${describeError(error)}`)]));
    toast(describeError(error));
    return;
  }
  if (ctx.signal.aborted) return;

  host.append(el('div', { class: 'row row--between' }, [
    el('h2', { text: `被试 sub-${publicId}` }),
    button('返回被试列表', () => {
      view.publicId = null;
      ctx.navigate('#/subjects');
    }),
  ]));

  host.append(card('档案', el('div', { class: 'grid grid--3' }, [
    el('div', {}, [
      el('p', { class: 'muted', text: '别名 / 年龄段 / 性别' }),
      el('p', { text: `${pick(subject, 'label', DASH) || DASH}｜${pick(subject, 'age_band', DASH) || DASH}｜${pick(subject, 'sex', DASH) || DASH}` }),
    ]),
    el('div', {}, [
      el('p', { class: 'muted', text: '利手 / 同意版本' }),
      el('p', { text: `${pick(subject, 'handedness', DASH) || DASH}｜${pick(subject, 'consent_version', DASH) || DASH}` }),
    ]),
    el('div', {}, [
      el('p', { class: 'muted', text: '会话（完成 / 总数）' }),
      el('p', { class: 'mono', text: `${fmtInt(pick(subject, 'sessions.done'))} / ${fmtInt(pick(subject, 'sessions.total'))}` }),
    ]),
    el('div', {}, [
      el('p', { class: 'muted', text: '创建时间' }),
      el('p', { text: fmtDate(pick(subject, 'created_at')) }),
    ]),
    el('div', {}, [
      el('p', { class: 'muted', text: '备注' }),
      el('p', { text: pick(subject, 'note', DASH) || DASH }),
    ]),
  ])));

  host.append(renderNewSessionPanel(ctx, publicId));

  host.append(card('会话列表', sessions.length ? table([
    {
      title: '会话',
      render: (row) => el('button', {
        class: 'btn btn--sm',
        type: 'button',
        text: `${String(row.uuid).slice(0, 8)}…`,
        onClick: () => ctx.navigate(`#/live?session=${row.uuid}`),
      }),
    },
    { title: '状态', render: (row) => statusText(row.status) },
    { title: '阶段', render: (row) => row.phase_label || row.phase || DASH },
    { title: '进度', align: 'right', render: (row) => fmtPercent(row.progress) },
    { title: '时间倍率', align: 'right', render: (row) => fmtNum(row.time_scale, 2) },
    { title: '预警', align: 'right', render: (row) => fmtInt(row.alert_count) },
    { title: '开始时间', render: (row) => fmtRelative(row.started_at) },
  ], sessions) : empty('该被试还没有会话记录。'), { sub: '点击会话编号进入实时监测' }));
}

export async function render(container, ctx) {
  container.textContent = '';
  view.publicId = view.publicId || ctx.params.get('public_id') || null;

  const page = el('div');
  page.append(el('div', { class: 'row row--between' }, [
    el('h1', { text: '被试管理' }),
    view.publicId ? null : button('刷新', () => ctx.reload()),
  ]));
  const host = el('div');
  page.append(host);
  container.append(page);

  if (view.publicId) {
    await renderSubjectDetail(host, ctx);
    return;
  }

  // 列表态：搜索 + 新建 + 表格
  host.append(el('p', { class: 'muted', text: '正在加载被试列表…' }));
  let response;
  try {
    response = await api.listSubjects({ query: view.query || undefined, limit: 50, page: 1 });
  } catch (error) {
    host.textContent = '';
    host.append(card('被试列表', [empty(`加载失败：${describeError(error)}`)]));
    toast(describeError(error));
    return;
  }
  if (ctx.signal.aborted) return;

  const items = list(pick(response.data, 'items', []));
  ctx.store.setState({ subjects: items });

  const searchInput = el('input', {
    id: 'subject-search',
    type: 'search',
    placeholder: '按编号 / 别名 / 备注搜索',
    value: view.query,
  });
  let timer = null;
  searchInput.addEventListener('input', () => {
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      view.query = searchInput.value.trim();
      ctx.reload();
    }, 300);
  });

  host.textContent = '';
  host.append(renderSubjectForm(ctx, async (publicId) => {
    // 创建后留在列表页并刷新：用户能立刻看到新被试，也能接着用"开始新会话"卡片。
    // （以前会跳进该被试的详情页，看起来像"点了一下但什么都没发生"。）
    view.query = '';
    await ctx.reload();
    const row = document.querySelector(`#view button[data-subject-id="${publicId}"]`);
    if (row && row.scrollIntoView) row.scrollIntoView({ block: 'center' });
  }));

  // 列表态也能直接开始会话：没有被试时给出明确指引，有被试时可在表单里挑选
  if (items.length) {
    host.append(renderNewSessionPanel(ctx, null, { subjects: items }));
  } else {
    host.append(card('开始新会话', [
      el('p', { class: 'muted', text: '还没有被试，无法开始会话。请先用上方“新建被试”创建一个（编号留空即自动生成）。' }),
    ]));
  }

  host.append(card('被试列表', el('div', {}, [
    el('div', { class: 'filters' }, [
      field('搜索', searchInput),
      el('span', { class: 'muted', text: `共 ${fmtInt(pick(response.data, 'total'))} 位被试` }),
    ]),
    el('div', { style: 'margin-top:12px' }, [
      items.length ? table([
        {
          title: '编号',
          render: (row) => el('button', {
            class: 'btn btn--sm',
            type: 'button',
            text: `sub-${row.public_id}`,
            dataset: { subjectId: row.public_id },
            onClick: () => {
              view.publicId = row.public_id;
              ctx.navigate(`#/subjects?public_id=${encodeURIComponent(row.public_id)}`);
              ctx.reload();
            },
          }),
        },
        { title: '别名', render: (row) => pick(row, 'label', DASH) || DASH },
        { title: '年龄段', render: (row) => pick(row, 'age_band', DASH) || DASH },
        { title: '性别', render: (row) => pick(row, 'sex', DASH) || DASH },
        { title: '利手', render: (row) => pick(row, 'handedness', DASH) || DASH },
        { title: '会话（完成/总）', align: 'right', render: (row) => `${fmtInt(pick(row, 'sessions.done'))} / ${fmtInt(pick(row, 'sessions.total'))}` },
        { title: '创建时间', render: (row) => fmtDate(pick(row, 'created_at')) },
        { title: '备注', render: (row) => pick(row, 'note', DASH) || DASH },
        {
          title: '操作',
          render: (row) => el('button', {
            class: 'btn btn--sm btn--primary',
            type: 'button',
            text: '开始会话',
            title: '用这位被试开始一次新会话',
            onClick: () => {
              view.publicId = row.public_id;
              ctx.navigate(`#/subjects?public_id=${encodeURIComponent(row.public_id)}`);
              ctx.reload();
            },
          }),
        },
      ], items) : empty('还没有被试，先用上方表单创建一个。'),
    ]),
  ]), { sub: '点击编号进入该被试的会话列表，或点右侧“开始会话”' }));
}

export default render;
