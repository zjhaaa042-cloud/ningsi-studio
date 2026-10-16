/**
 * 被试管理：列表 / 新建 / 详情（会话列表 + 开始新会话）。
 *
 * 数据来源：
 * - GET  /api/subjects、POST /api/subjects、PATCH /api/subjects/{public_id}
 * - GET  /api/subjects/{public_id}/sessions
 * - 开始检测统一进入三步向导，避免不同页面的设备与节奏选项不一致。
 */

import { api } from '../api.js';
import { describeError, toast } from '../store.js';
import {
  DASH, button, card, clear, el, empty, field, fmtDate, fmtInt, fmtNum, fmtPercent,
  fmtRelative, list, phaseText, pick, statusText, table,
} from '../util.js';

const AGE_BANDS = ['16-17', '18-25', '26-35', '36-45', '46-55', '56-65', '65+'];
const SEX_OPTIONS = ['女', '男', '不便告知'];
const HANDEDNESS_OPTIONS = ['右', '左', '双利手'];

/** 视图内状态：切换被试/刷新时保留（视图卸载即丢弃）。 */
const view = { publicId: null, query: '', editId: null };

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

/**
 * 被试档案编辑卡（内联表单，随列表页一起渲染）。
 *
 * 接口：PATCH /api/subjects/{public_id}（见 src/ningsi_studio/api/routes.py:327-352）
 * - 可更新字段：label / age_band / sex / handedness / note / consent_version / consent_at；
 * - 只写"请求里出现过的字段"，所以整表提交即可；清空 → 传空串
 *   （schemas.optional_text 把 ""/None 写成 null，routes.py:340-344）；
 * - 编号、创建时间、会话计数只读：PATCH 的 spec 里没有这三个键；
 * - 后端 409（编号冲突）/422（字段校验、没有可更新字段）都带 message 与 detail，
 *   这里如实显示，不吞掉。
 */
function renderSubjectEdit(ctx, subject, publicId) {
  const labelInput = el('input', {
    id: 'edit-subject-label',
    type: 'text',
    maxlength: 200,
    value: pick(subject, 'label', '') || '',
  });
  const ageSelect = selectInput('edit-subject-age', AGE_BANDS, { value: pick(subject, 'age_band', '') || '' });
  const sexSelect = selectInput('edit-subject-sex', SEX_OPTIONS, { value: pick(subject, 'sex', '') || '' });
  const handSelect = selectInput('edit-subject-hand', HANDEDNESS_OPTIONS, { value: pick(subject, 'handedness', '') || '' });
  const consentInput = el('input', {
    id: 'edit-subject-consent',
    type: 'text',
    maxlength: 40,
    value: pick(subject, 'consent_version', '') || '',
  });
  const noteInput = el('input', {
    id: 'edit-subject-note',
    type: 'text',
    maxlength: 500,
    value: pick(subject, 'note', '') || '',
  });
  const errorHost = el('div');
  const save = button('保存修改', null, { primary: true });
  const cancel = button('取消', () => {
    view.editId = null;
    ctx.reload();
  });

  save.addEventListener('click', async () => {
    save.disabled = true;
    save.textContent = '保存中…';
    errorHost.textContent = '';
    try {
      const response = await api.patchSubject(publicId, {
        label: labelInput.value.trim(),
        age_band: ageSelect.value,
        sex: sexSelect.value,
        handedness: handSelect.value,
        note: noteInput.value.trim(),
        consent_version: consentInput.value.trim(),
      });
      const updated = pick(response.data, 'public_id', publicId);
      view.editId = null;
      toast(`已保存被试 sub-${updated} 的档案`, 'info');
      await ctx.reload();
      const row = document.querySelector(`#view button[data-subject-id="${updated}"]`);
      if (row && row.scrollIntoView) row.scrollIntoView({ block: 'center' });
    } catch (error) {
      // 409 / 422 的 detail（例如 allowed 字段列表）一并显示，便于用户知道哪里不合法
      const detail = error && error.detail ? `（${JSON.stringify(error.detail)}）` : '';
      errorHost.textContent = '';
      errorHost.append(el('p', { class: 'empty', text: `${describeError(error)}${detail}` }));
      toast(describeError(error));
    } finally {
      save.disabled = false;
      save.textContent = '保存修改';
    }
  });

  const node = card('编辑被试', el('div', {}, [
    el('div', { class: 'grid grid--3' }, [
      el('div', {}, [
        el('p', { class: 'muted', text: '编号（只读）' }),
        el('p', { class: 'mono', text: `sub-${pick(subject, 'public_id', publicId)}` }),
      ]),
      el('div', {}, [
        el('p', { class: 'muted', text: '创建时间（只读）' }),
        el('p', { text: fmtDate(pick(subject, 'created_at')) }),
      ]),
      el('div', {}, [
        el('p', { class: 'muted', text: '会话（完成 / 总数，只读）' }),
        el('p', { class: 'mono', text: `${fmtInt(pick(subject, 'sessions.done'))} / ${fmtInt(pick(subject, 'sessions.total'))}` }),
      ]),
    ]),
    el('div', { class: 'form-grid', style: 'margin-top:12px' }, [
      field('别名', labelInput, '清空可移除别名；姓名等直接身份信息不进入报告'),
      field('年龄段', ageSelect),
      field('性别', sexSelect),
      field('利手', handSelect),
      field('同意版本', consentInput, '例如 consent-v1；改动会写进更新后的档案'),
      field('备注', noteInput, '最多 500 字'),
    ]),
    errorHost,
    el('div', { class: 'row row--end', style: 'margin-top:12px' }, [cancel, save]),
  ]), {
    sub: `PATCH /api/subjects/${publicId}：只提交这张表单里的字段；编号、创建时间、会话计数为只读。按 Esc 或“取消”退出且不提交。`,
  });

  // Esc 退出且不提交（任务书第 1 条要求）；视图卸载时移除监听，避免残留
  const onKeydown = (event) => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    view.editId = null;
    ctx.reload();
  };
  document.addEventListener('keydown', onKeydown);
  ctx.onCleanup(() => document.removeEventListener('keydown', onKeydown));

  return node;
}

function renderNewSessionPanel(ctx, publicId, options = {}) {
  const subjects = list(options.subjects);
  const select = el('select', { id: 'session-subject' });
  for (const subject of subjects) {
    const id = pick(subject, 'public_id', '');
    select.append(el('option', { value: id, text: `sub-${id}${subject.label ? `（${subject.label}）` : ''}` }));
  }
  return card('开始一次检测', el('div', {}, [
    el('p', { class: 'muted', text: '进入检测向导，确认设备信号、检测协议和训练时长。' }),
    publicId ? el('p', { text: `本次被试 sub-${publicId}` }) : field('被试', select),
    button('前往检测准备', () => {
      const id = publicId || select.value;
      ctx.navigate(id ? `#/start?participant=${encodeURIComponent(id)}` : '#/start');
    }, { primary: true }),
  ]));
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
        attrs: { title: row.uuid, 'aria-label': `打开会话 ${row.uuid} 的实时监测` },
        onClick: () => ctx.navigate(`#/live?session=${row.uuid}`),
      }),
    },
    { title: '状态', render: (row) => statusText(row.status) },
    { title: '阶段', render: (row) => phaseText(row.phase, row.phase_label) },
    { title: '进度', align: 'right', render: (row) => fmtPercent(row.progress) },
    { title: '时间倍率', align: 'right', render: (row) => fmtNum(row.time_scale, 2) },
    { title: '预警', align: 'right', render: (row) => fmtInt(row.alert_count) },
    { title: '开始时间', render: (row) => fmtRelative(row.started_at) },
  ], sessions) : empty('该被试还没有会话记录。'),
    { sub: '点击会话编号进入实时监测', class: 'card--scroll' }));
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

  // 编辑入口：点列表行里的“编辑”后 view.editId 有值，这里取该被试明细并渲染内联编辑表单。
  // 用 api.getSubject 而不是列表缓存：搜索结果/分页后目标行可能不在当前页。
  if (view.editId) {
    try {
      const editResponse = await api.getSubject(view.editId);
      if (ctx.signal.aborted) return;
      const editNode = renderSubjectEdit(ctx, editResponse.data || {}, view.editId);
      host.append(editNode);
      if (editNode.scrollIntoView) editNode.scrollIntoView({ block: 'center' });
    } catch (error) {
      host.append(card(`编辑被试 sub-${view.editId}`, [
        empty(`加载被试信息失败：${describeError(error)}`),
      ]));
    }
  }

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
            attrs: { 'aria-label': `查看被试 sub-${row.public_id} 的会话列表` },
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
          render: (row) => el('div', { class: 'row' }, [
            // 两枚按钮文字都只有两个字，读屏无法区分是哪一行：aria-label 带上被试编号
            el('button', {
              class: 'btn btn--sm',
              type: 'button',
              text: '编辑',
              attrs: {
                title: `编辑被试 sub-${row.public_id} 的档案`,
                'aria-label': `编辑被试 sub-${row.public_id} 的档案`,
              },
              onClick: () => {
                view.editId = row.public_id;
                ctx.reload();
              },
            }),
            el('button', {
              class: 'btn btn--sm btn--primary',
              type: 'button',
              text: '开始会话',
              attrs: {
                title: '用这位被试开始一次新会话',
                'aria-label': `用被试 sub-${row.public_id} 开始一次新会话`,
              },
              onClick: () => {
                view.publicId = row.public_id;
                ctx.navigate(`#/subjects?public_id=${encodeURIComponent(row.public_id)}`);
                ctx.reload();
              },
            }),
          ]),
        },
      ], items) : empty('还没有被试，先用上方表单创建一个。'),
    ]),
  ]), { sub: '点击编号进入该被试的会话列表，或点右侧“开始会话”', class: 'card--scroll' }));
}

export default render;
