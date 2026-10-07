# 凝思 Studio 接口契约（/api）

- 基地址：与前端同端口（默认 `http://127.0.0.1:8765`）
- 全部接口在 `/api` 前缀下；`GET /api/openapi.json` 返回机器可读描述
- 请求/响应均为 `application/json; charset=utf-8`
- 时间倍率 `time_scale`：`1.0` 为真实节奏（每窗 2 秒），`0.05` 为 20 倍速演示；算法口径与窗口长度不变
- 运行中会话的实时数据通过 **SSE** 推送，`GET /api/sessions/{uuid}/live` 为轮询兜底

## 错误体

```json
{ "error": { "code": "conflict", "message": "当前会话不处于 SART 阶段或已结束", "detail": {} } }
```

| HTTP | code | 场景 |
|---|---|---|
| 400 | bad_request | 请求体不是合法 JSON |
| 401 | unauthorized | 配置了 `--token` 但 `/api` 请求未带（或带错）令牌；静态资源不受影响 |
| 404 | not_found | 被试/会话/产物不存在 |
| 405 | internal_error→405 | 方法不被支持（响应带 `detail.allow`） |
| 409 | conflict | 状态冲突：会话不在运行、任务未到该阶段、编号重复 |
| 422 | validation_failed | 字段缺失或取值越界 |
| 429 | too_many_requests | 并发运行会话超上限（默认 2） |
| 500 | internal_error | 未预期异常（已记录日志，服务不退出） |

---

## 基础

### `GET /api/health`
```json
{
  "status": "ok", "service": "ningsi-studio", "version": "0.1.0", "product": "0.1.0",
  "engine": { "product": "0.1.0", "spectrum": "welch-v1", "indicator": "indicator-v1",
              "baseline": "baseline-v1", "assessment": "joint-assessment-v1" },
  "engine_path": ".../ningsi/src/ningsi/__init__.py",
  "db": ".../var/studio/studio.sqlite3", "data_dir": ".../var/studio",
  "runs_root": ".../var/studio/runs",
  "active_sessions": ["<uuid>"], "max_active_sessions": 2,
  "overview": { "subjects": 1, "sessions": 3, "sessions_done": 2, "alerts": 5, "latest": [] }
}
```
`runs_root` 是会话运行目录的根：每个会话写在 `<runs_root>/<uuid>/`，其 JSONL 台账在
`<runs_root>/<uuid>/history/sessions.jsonl`（全局趋势的 `ledger_points` 就是从这里汇总的）。

### `GET /api/config`
返回 `version`、`specs`、`window_sec`(4.0)、`step_sec`(2.0)、`welch`、`bands`、`total_band`、
`indicators`、`score_gain`、`baseline_protocol`、`quality`、`alerts`、`assess`、`training`、
`heatmap_bands`、`scale_boundary`、`phases`、`privacy`（隐私与结论边界原文）。

### `GET /api/devices?probe=1.0`
```json
{ "sources": [ { "key": "lsl:BioMultiLite EEG-00cde1", "kind": "lsl", "srate": 250.0,
                 "channels": 2, "device": "BioMultiLite EEG-00cde1", "real": true,
                 "simulated": false, "note": "真实 LSL 流：…（信号已按 acq-condition-v1 调理）",
                 "channel_labels": ["Fp1", "Fp2"], "source_id": "BioMulti_Lite_EEG-00cde1" },
               { "key": "sim-bsense", "kind": "sim", "srate": 250.0, "channels": 1,
                 "device": "sim-bsense", "real": false, "explicit_only": true,
                 "note": "仿真脑电源（仅用于演示与自动测试，必须在界面与报告中标注）" } ],
  "source": "lsl:BioMultiLite EEG-00cde1", "source_kind": "lsl", "source_note": "…",
  "has_real_source": true }
```

**顺序与缺省规则（2026-10-07 起）**：

- **真实 LSL 流排在前面**；仿真源 `sim-bsense` 永远排最后并带 `explicit_only: true`，
  只能被**显式**选择，不再作为缺省（原来它排第一，前端就把仿真当默认了）。
- `has_real_source` 直接回答"现在到底有没有脑机信号"；没有实时源时列表里只有仿真源，
  且它带 `hardware_note`（接入指引）。
- `simulated: true` 表示这条 `lsl:` 流是**本仓库内置的仿真 outlet**（`simulate-outlet`）：
  传输是真的、数据是算出来的，note 会写明"非真实设备"。

### `GET /api/devices/preview` · `POST /api/devices/preview` · `DELETE /api/devices/preview`
**无会话的设备实时预览**：不创建会话也能看当前脑电信号（读取检测到的实时流，
做与报告一致的 `acq-condition-v1` 真机调理，并给出这一窗的质检判定）。

```json
GET  /api/devices/preview            → { "active": false, "source": null, "no_signal": true,
                                         "note": "未开始预览：当前没有脑机信号" }
POST /api/devices/preview            → body { "source": "lsl:<流名>" 或 "sim-bsense"（可省略） }
     { "active": true, "source": "lsl:…", "kind": "lsl", "device": "…", "srate": 250.0,
       "channels": 2, "channel_labels": ["Fp1","Fp2"], "live": true,
       "seconds_since_last": 0.02, "buffered_samples": 4820,
       "conditioning": { "spec": "acq-condition-v1", … }, "no_signal": false }
GET  /api/devices/preview?window=1   → 上面字段 + { "seconds": 4.0, "samples": [[…], […]],
                                         "peak_uv": 42.1, "std_uv": 8.3,
                                         "quality": { "ok": true, "reasons": [], "metrics": {…} } }
DELETE /api/devices/preview          → { "active": false, "stopped": true }
```

- `source` 省略时用**当前检测到的实时源**；一个都没有 ⇒ `409 conflict`
  （「未检测到脑电信号…」），**不会**回落到仿真源。
- 预览**只读**：不建会话、不落库、不出指标；`quality` 用的是与报告同一份 `config.QUALITY` 门槛。
- `live=false` / `no_signal=true` 表示流还在但最近没有样本（设备停了/线掉了），界面据此显示"无信号"。

### `GET /api/devices/status`
正在运行的会话所用设备的**活体健康**：是否在收数、实测采样率、缓冲量、错误。
```json
{ "active_sessions": ["<uuid>"],
  "devices": [ { "session": "<uuid>", "key": "lsl:ningsi-sim-eeg", "kind": "lsl",
                 "device": "ningsi-sim-eeg", "srate": 250.0, "channels": 1,
                 "live": true, "seconds_since_last": 0.02, "observed_srate": 249.98,
                 "buffered_samples": 4820, "total_samples": 15230, "stream_errors": {},
                 "descriptor": { "kind": "eeg", "name": "ningsi-sim-eeg", "channel_labels": ["Fp1"] },
                 "note": "真实 LSL 流：ningsi-sim-eeg（1 通道，250 Hz，标签 ['Fp1']）" } ],
  "sources": [ { "key": "sim-bsense", "kind": "sim" } ] }
```
上例是 **LSL 源**的形状：`live`、`seconds_since_last`、`observed_srate`、`buffered_samples`、`total_samples`、
`stream_errors`、`descriptor` 这些**活体字段只有 LSL 源才有**（引擎能自查流状态）；仿真源
（`kind: "sim"`）只给 `session`/`key`/`kind`/`device`/`srate`/`channels`/`note`/`live`（`live` 恒为 `true`，
其余字段直接缺失，前端按「—」显示，不是 0）。
`live=false` 且 `seconds_since_last` 持续变大，说明设备已停/线掉了：此时会话会把窗判为不可用
（界面上显示为缺失色块），不会拿旧数据继续算指标。

### `GET /api/overview`
首页统计 + 最近会话 `latest_detail` + 最近被试 `subjects_recent` + `active_sessions` +
`source` / `source_kind` / `source_note`（**当前检测到的实时源**的键、类别与说明文案，供顶栏展示）
+ `has_real_source`。

**检测不到实时源时 `source`/`source_kind`/`source_note` 都是 `null`**，`has_real_source` 为 `false`
——顶栏据此如实写成「数据来源：无信号」。仿真源 `sim-bsense` **不参与**这个回退：它必须由调用方
显式指定（见 `POST /api/sessions` 的取值规则），没有脑机信号时不会自动使用仿真。

> `GET /api/health` 里的 `overview` 也带同样四个字段（LSL 扫描结果有 3 秒缓存，health 不会被拖慢）。

### `GET /api/sessions` 与列表类响应的分页约定
`{"items": [...], "total": N, "limit": L, "page": P}`；`page` 从 1 开始，`limit` 上限 200。
会话、被试、被试的会话列表、`/api/scales`、`/api/sessions/{uuid}/artifacts` 全部遵守该信封，
并都接受 `?page=&limit=`（默认 `limit=50&page=1`）。

---

## 被试

### `GET /api/subjects?query=&page=1&limit=50`
```json
{ "items": [ { "public_id": "p01", "label": "演示被试", "age_band": null, "sex": null,
               "handedness": null, "consent_version": "consent-v1", "consent_at": "2026-01-01T00:00:00+00:00",
               "note": null, "created_at": "...", "updated_at": "...",
               "sessions": { "done": 1, "total": 1 } } ],
  "total": 1, "limit": 50, "page": 1 }
```

### `POST /api/subjects`
请求（`public_id` 省略且 `auto_id != false` 时自动生成 `p01`、`p02`…）：
```json
{ "auto_id": true, "label": "演示被试", "age_band": "18-25", "sex": "女",
  "handedness": "右", "consent_version": "consent-v1", "note": "仅用于演示" }
```
响应 `201` 同单个被试对象；编号重复返回 `409`。

### `PATCH /api/subjects/{public_id}`
可改 `label / age_band / sex / handedness / note / consent_version / consent_at`，返回更新后的对象。

### `GET /api/subjects/{public_id}/sessions?page=&limit=`
返回该被试的会话列表（结构同 `GET /api/sessions`）。

---

## 会话

### `POST /api/sessions`
请求：
```json
{ "participant": "p01", "device": "lsl:BioMultiLite EEG-00cde1", "time_scale": 0.05,
  "training_mode": "quick", "srate": 250.0, "channels": 1, "label": "第一次训练",
  "create_subject": true }
```
`time_scale` 取值 `[0.01, 1.0]`；**`time_scale < 0.2` 视为快速演示模式**，量表、SART、PVT 由服务端
生成确定性作答（无需前端交互），用于评审演示与自动化测试。

**`device` 取值规则（2026-10-07 起收紧：不再有"缺省仿真"）**：

| `device` | 行为 |
|---|---|
| 省略 / `""` / `auto` | 用**当前检测到的实时源**（真机优先、脑电流优先于同机的 Metric/HeartRate 等，其次内置仿真 outlet）；**一个都没有 ⇒ `409`**，消息里给接入指引 |
| `lsl:<流名称>` | 先确认该流现在可见，否则 `409`（fail fast，不再"建好会话才发现没信号"）；连接后 `srate`/`channels` 按流描述符回写 |
| `sim-bsense` | **允许，但必须显式**：这是演示/自测入口，`source`/SSE 事件/报告都会标注为仿真 |
| 其它任意字符串 | `422 validation_failed`（不再被当成仿真源） |

**流类型守卫（2026-10-07 新增）**：一台 BioMultiLite 会同时推 6~7 条流
（`eeg` / `metric` / `fnirs` / `heart_rate` / `general_metric` / `motion` / `marker`），
**只有 `eeg` 那条能用于本产品**——质检、频谱、专注/放松/负荷指标与 SART/PVT 判定全部基于脑电通道。
选错流的后果不是报错而是"算出一堆看着很像的数字"，所以建会话时：

- 流类型属于 `fnirs` / `metric` / `motion` / `heart_rate` / `general_metric` / `marker`
  ⇒ **`409`**，消息里写明它是什么流、为什么不能用，并在 `detail.eeg_candidates` 里给出可用的 EEG 流键；
- 类型为空/未知的流**不拦**（可能只是采集端没写 `type` 字段）；
- 确实要用非脑电流采集时显式传 **`allow_non_eeg: true`**（放行后仍会因流打不开而如实失败）。

`GET /api/devices` 的每条 source 因此多了三个字段，界面据此把 EEG 单独标为推荐、其余折叠：
`stream_kind_label`（中文类型名，如 `近红外（FNIRS）`）、`is_eeg`（是否脑电）、`usable`（本产品能否用它跑检测）。

不再有"真实设备打不开就降级为仿真源"的静默替换：真实源不可用时会话启动失败并写明
`无信号：数据源 <device> 不可用（…）`。

响应 `201`：
```json
{ "session": { "uuid": "<32位>", "participant": "p01", "subject_id": 1, "label": "第一次训练",
                "status": "running", "phase": "qc",
                "phase_label": "设备质检", "progress": 0.0,
                "device": "lsl:BioMultiLite EEG-00cde1",
                "time_scale": 0.05, "training_mode": "quick",
                "engine_versions": { "spectrum": "welch-v1", "indicator": "indicator-v1",
                                     "baseline": "baseline-v1", "assessment": "joint-assessment-v1" },
                "source": "lsl:BioMultiLite EEG-00cde1", "srate": 250.0, "channels": 2,
                "started_at": "...", "ended_at": null, "created_at": "...", "error": null },
  "events_url": "/api/sessions/<uuid>/events",
  "phases": [ { "key": "qc", "label": "设备质检", "interactive": false, "weight": 0.04 } ],
  "runtime": { "alive": true, "source": "lsl:BioMultiLite EEG-00cde1" } }
```
`source` 是**实际数据源**（`lsl:<name>` 或显式的 `sim-bsense`），与 `runtime.source` /
`GET .../{uuid}` 的 `runtime.source_kind` 同源，不会出现"标着 lsl、实际跑 sim"的自相矛盾。

连上真实流时，`srate` 与 `channels` 用**流描述符里的真实值回写**（请求里不传时默认 250.0 / 1）：
`device="lsl:BioMulti Lite EEG-00cde1"` 的响应会是 `"channels": 2`，与 `runtime.source.channels`
以及报告 `extras.channels` 同源；流名按 `lsl:` 后的字符串**精确匹配**，同名多条流时只连这一条。
并发超限返回 `429`。

连上真实流时，`srate` 与 `channels` 用**流描述符里的真实值回写**（请求里不传时默认 250.0 / 1）：
`device="lsl:BioMulti Lite EEG-00cde1"` 的响应会是 `"channels": 2`，与 `runtime.source.channels`
以及报告 `extras.channels` 同源；流名按 `lsl:` 后的字符串**精确匹配**，同名多条流时只连这一条。
并发超限返回 `429`。

### `GET /api/sessions?participant=p01&status=done&page=1&limit=50`
`items[]` 为会话对象，额外含 `alert_count` 与 `phase_label`（已知阶段的**中文名**，与详情接口同一张
`core/phases.py` 映射表；终态 `done`/`failed`/`cancelled` 不是流程阶段，此字段为 `null`，由界面本地化）。
`phase` 运行中是流程阶段键（`qc`/`baseline`/`scale`/…），会话收尾后写的**就是终态本身**
（`done`/`failed`/`cancelled`，与 `status` 一致），所以取消掉的会话是「已取消」而不是「失败」。

### `GET /api/sessions/{uuid}`
会话对象 + `alerts`、`runs`（每阶段一行：`phase/status/duration_ms/error/payload`；`payload` 已解析为
对象，不是 JSON 字符串）、`artifacts`、`indicator_summary`、
`runtime{ alive, awaiting_input, source, source_kind }`。
`runtime.source_kind` 优先取运行期实际种类；**运行器已被回收的历史会话**按落库的 `source` 键反推
（`lsl:` ⇒ `lsl`，仿真键 ⇒ `sim`），避免会话结束后界面上"仿真"标注消失（`_source_kind_from_key`）。
会话仍在运行时，`source` 取运行时**实际**数据源，与 `runtime.source` / `runtime.source_kind`
一致（LSL 未就绪而内部降级为仿真时，这里会显示 `sim`，不会仍标 `lsl`）。

### `DELETE /api/sessions/{uuid}`
取消运行中的会话，返回 `{"cancelled": true, "uuid": "..."}`；不在运行中返回 `409`。

### `GET /api/sessions/{uuid}/events`（SSE）
`Content-Type: text/event-stream`。每条事件形如：

```
id: 12
event: window
data: {"id":12,"type":"window","session":"<uuid>","data":{"index":7,"t_end":14.0,"scores":{"focus":0.62}}, "phase":"monitor"}
```

> 事件负载约定：**业务字段一律包在 `data` 里**；`id` / `type` / `session` / `phase` 等为事件元数据。
> 下方的"data 关键字段"即指 `event.data` 的属性。

| event | data 关键字段 |
|---|---|
| `started` | `uuid, participant, device, source_kind, source_note, time_scale, auto, phases[], engine_versions` |
| `notice` | `level, message`（例如"当前使用仿真脑电源"） |
| `phase` | `key, label, state(running/done), progress` |
| `progress` | `key, progress, windows_done?, windows_total?, trials_done?, trials_total?, segments_done?, segments_total?` |
| `quality` | `windows, usable, valid_ratio, reasons{}, passed` |
| `baseline` | `key(baseline_open/baseline_closed), label, baseline{...}` |
| `window` | `index, t_end, state, usable, quality, scores{focus,relax,load}, index_z, band_z, reasons[], alerts[], signal{srate,channels,samples[],relative,time_domain}` |
| `scale_request` | `code, label, size, instruction` |
| `scale_scored` | `code, raw_score, standard_score, level` |
| `behavior_request` | `task, digits[], nogo_trials, practice[], sequence_set_id, seed, instruction`（PVT 为 `trials, onsets[], duration_sec, lapse_sec, instruction`） |
| `trial` | SART：`phase(practice/main/done), index, digit, total, response_window`；PVT：`index, onset, total, response_window` |
| `behavior` | `task, result{...}` |
| `monitor` | `summary, quality` |
| `feedback` | `segment, target, score, on_target, usable, t` |
| `segment` | `seq, index, target, target_after, hold_sec, hold_after, duration_sec, excluded_windows, stats{mean,on_target_ratio,volatility,n,achieved}, mean_score, on_target_ratio, volatility, n, achieved, samples[[t,score]]` |
| `training_start` | `mode, segments, segment_sec, target, rationale, initial_from, target_step, hold_sec` |
| `assessment` | 同 `GET .../assessment` |
| `scales` | `results[]`（SAS/SDS 计分结果汇总，量表阶段结束时下发一次） |
| `model` | `spec, features[], subject_split, train, validation, test, model_path` |
| `artifacts` | `kind -> path` |
| `cancelled` | `message` |
| `error` | `message, detail` |
| `finished` | `status(done/failed/cancelled), error, summary` |

> **`trial.response_window`（秒，真实时间）**：本试次的作答窗口，是试次能否推进的关键——
> 试次是"服务端发一个、前端答一个"驱动的，而 **SART 的 No-Go 试次（显示 3）正确做法就是不按键**；
> 前端必须在窗口到点时自动补交一笔 `{responded:false, rt:null}`（Go 记漏报、No-Go 记正确抑制），
> 否则服务端会一直等到 `INPUT_TIMEOUT_SEC`（900 秒），现场看到的就是"显示 3 之后任务不动了"。
> 该值已按 `time_scale` 折算（演示模式整体节奏变快），且不小于 0.35 秒。
> 服务端自身只等 `response_window + 0.6s`，并校验提交里的 `index` 必须与当前试次一致
> （晚到的作答按"未作答"记账，不会算到下一试次头上）；单次超时按未作答继续，
> 连续 5 次才判定前端掉线并使该阶段失败。
>
> 注：本说明必须放在事件表**之外**——表格中间夹引用块或空行会把 Markdown 表格截断，
> `ningsi_studio check` 的"文档事件"统计会掉（本文件被踩过一次：23 → 11）。

重连：带上 `Last-Event-ID: <id>` 头（或 `?last_event_id=`）即可补发漏掉的事件；
不带 id 订阅时只补发"快照类"事件（阶段快照 `phase`，会话已结束时还有 `finished`），
随后若总线已关闭会立刻收到 `closed`，客户端据此结束连接。

### `GET /api/sessions/{uuid}/signal?hz=10&seconds=10&points=1200`
**高频原始信号 SSE**（`text/event-stream`）：给"实时监测"页的波形与频谱供数，
与 4 秒分析窗**解耦**——按 `hz`（默认 10 帧/秒，上限 25）直接读采集缓冲的最新样本。

- `seconds`：每帧覆盖的时间跨度（2–30 秒，默认 10）；**真实 LSL 源会被压到 6 秒以内**
  （等于 `ManagedLslSource.condition_seconds`）：真机调理链是纯 Python 双二阶，成本与窗长线性相关
  （2 通道 10 秒窗约 30 ms/帧），10 FPS 的显示通道按长窗调理会把推帧线程吃满。
  这是"显示窗口"的上限，帧里回报的 `window_sec` 会同步变小（前端按它定标），**不改变** 4 秒分析窗口径。
- `points`：每通道最多返回多少个点（200–4000，默认 1200）；抽稀用 **min/max 保峰值**，
  尖峰/伪迹不会被均匀抽样抹掉
- 会话不在运行中时返回 `409`，走**统一错误体**（`{"error":{"code":"conflict","message":...}}`，
  不是自造的 `session_not_running`）；会话结束（`finished`/`cancelled`）后服务端会停止推帧并收流，
  客户端应以 `EventSource.onerror` + `GET .../live` 的 `runtime_alive` 作为降级依据，不必依赖连接 EOF

事件类型固定为 `signal`：
```json
{ "type": "signal", "session": "<uuid>", "seq": 42, "t": 1765000000.12,
  "window_sec": 10.0, "srate": 250.0, "channel_count": 2, "samples": 2500,
  "realtime": true, "refresh_hz": 10.0, "source_kind": "lsl", "device": "ningsi-sim-eeg",
  "channels": [ { "index": 0, "label": "Fp1", "unit": "uV", "peak": 78.5,
                  "points": 1200, "raw_points": 2500, "samples": [3.2, -4.1, "..."] } ],
  "spectrum": { "freqs": [0.0, 0.49, "..."], "power_db": [-18.5, 21.9, "..."],
                "segments": 3, "delta_f_hz": 0.4883 },
  "band_peaks": { "theta": 12.4, "alpha": 18.1 },
  "bands": { "theta": 0.011, "alpha": 0.415 } }
```
说明：`samples` 只用于**显示**，不参与指标与计时（报告口径仍以 4 秒窗的 `window` 事件为准）；
`spectrum` 用最近一个窗的 welch-v1 结果，1 秒缓存一次，避免与报告出现两套算法。

### `GET /api/sessions/{uuid}/live`
轮询兜底：`status, phase, progress, indicator_summary, alerts[], awaiting_input[], runtime_alive`。

### `GET /api/sessions/{uuid}/heatmap`
```json
{ "uuid": "...", "columns": 30,
  "cells": [ { "index": 2, "label": "灰绿", "color": "#6E9E8A", "score": 0.51,
               "low": 0.4, "high": 0.6, "t": 12.0 },
             { "index": -1, "label": "低质量缺失", "color": "#6b6b6b", "score": null,
               "low": null, "high": null, "t": 14.0 } ],
  "legend": [ { "label": "深蓝", "color": "#1B3B6F", "range": "0.00–0.20" } ],
  "missing_color": "#6b6b6b",
  "scored": 34, "missing": 1,
  "note": "低质量窗不参与指标与预警计时，单独用缺失色块表示。" }
```
`index = -1` 即缺失窗，其 `score` 为 `null`（前端必须画成灰色块，不得当作 0 或状态变化）。

### `GET /api/sessions/{uuid}/trend?field=focus&period=week`
`field ∈ {focus, relax, load}`，`period ∈ {week, month}`；
`points[{period, mean, n, std}]`，另返回 `sessions / comparable / rejected / rejected_reasons`
（设备、采样率或通道数变化的历史记录会被排除在聚合之外）。

---

## 量表

- `GET /api/scales?page=1&limit=50` → `{ "items": [{ "code": "SAS", "name": "焦虑自评量表", "size": 20,
  "version": "zung-cn-v1", "estimate_minutes": 5 }], "total": 2, "limit": 50, "page": 1 }`
  （目录目前固定 SAS/SDS 两套；信封与分页参数见「列表类响应的分页约定」）
- `GET /api/scales/{code}`（`code` 取 `SAS` 或 `SDS`）→ `code, name, version, factor, size, options[{value,text}]`,
  `items[{index,text,reverse}]`, `reverse_items[]`, `standard_factor`, `boundaries`, `note`
- `POST /api/sessions/{uuid}/scales/{code}`
  ```json
  { "responses": [2,2,3,1,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2] }
  ```
  响应：`{ "code": "SAS", "delivered_to_runtime": true, "raw_score": 45,
            "standard_score": 56, "level": "轻度", "answered": 20, "missing": [] }`
  作答值必须是 1–4 的整数，长度 20；否则 `422`。仅当会话正等待该量表时
  `delivered_to_runtime` 为 `true`（也可在阶段开始前预提交）。

---

## 行为任务

### SART
- `GET /api/sessions/{uuid}/behaviors/sart/sequence`
  `{ "task": "sart", "trials": 180, "nogo_trials": 20, "practice_trials": 12,
     "sequence_set_id": 5, "seed": 4157852885, "digits": [1..9...], "practice": [7,2,...],
     "instruction": "看到 1–9 按空格；看到数字 3 不要按。…" }`
- `POST /api/sessions/{uuid}/behaviors/sart/trial`
  ```json
  { "phase": "main", "index": 0, "responded": true, "rt": 0.37 }
  ```
  响应 `202`-style：`{ "accepted": true }`；服务端按 `index` 校验顺序，顺序不符返回 `409`。
  未运行时（快速模式）该接口返回 `409`。

### PVT-B
- `GET /api/sessions/{uuid}/behaviors/pvt/sequence` → `{ "task": "pvt-b", "duration_sec": 180.0,
  "trials": 69, "onsets": [2.0, 4.13, ...], "lapse_sec": 0.5, "instruction": "..." }`
- `POST /api/sessions/{uuid}/behaviors/pvt/trial` → `{ "index": 0, "responded": true, "rt": 0.31,
  "false_start": false }`

### 结果
`GET /api/sessions/{uuid}/behaviors/{task}/result`（`task` 取 `sart` 或 `pvt`）
落库后返回 `{ "result": {...}, "trials": {...} }`；未完成时 `404`/`409`。
SART 结果字段：`trials, go_trials, nogo_trials, go_accuracy, commission_rate, omission_rate,
rt_mean, rt_sd, rt_p50, rt_variability, sequence_set_id, seed, spec`；
PVT 结果字段：`trials, responded, missed, rt_mean, rt_median, rt_sd, rt_p90, lapses,
lapse_rate, false_starts, valid`。

---

## 评估、训练、报告

- `GET /api/sessions/{uuid}/assessment` → 联合评估对象：
  `spec, conclusion, dimension_states{attention,stress}, consistency{eeg_vs_scale,eeg_vs_behavior},
  evidence[{code,source,dimension,summary,value,direction,available,ref}], advice[], boundary`。
  `consistency` 的取值：`一致`（两侧都有可用证据且同向提示偏离）、`不一致`、
  `无冲突`（两侧都有证据但无冲突），以及三种缺证据写法——`无法比较（脑电不可用）`、
  `无法比较（量表不可用）`、`无法比较（量表与脑电均无可用证据）`（行为任务同理）。
  **任一侧缺证据时不再报"一致"**（脑电 0 可用窗却写"脑电与量表一致"属于凭空断言一致性）。
  评估未完成时 `409`。
- `GET /api/sessions/{uuid}/training` → `{ "segments": [...], "summary": {...} }`（`segments[]` 的字段见
  上方 `segment` 事件）；`summary` 含 `mean_focus, on_target_ratio, first_to_last_change, baseline_before,
  baseline_after, target_rationale, baseline_comparable`，另含 `describe`（训练方案：
  `mode, segments, segment_sec, target, rationale, initial_from, target_step, hold_sec`，
  与 `training_start` 事件同一份）。
- `GET /api/sessions/{uuid}/report` → 报告 JSON（`spec/participant/indicators/quality/scales/
  behavior/assessment/extras`），并附 `report_markdown`（报告原文，Markdown 文本）与
  `report_markdown_path`；尚未生成时返回 `202` 且带 `partial: true` 与阶段进度。
  `extras.signal_conditioning` **只在真实 LSL 会话里出现**（仿真源不做调理），形状为
  `{spec, dc_removal, chain{spec,stages[]}, srate, observed_srate, context_samples, window_samples,
  expected_samples}`；它同时会渲染成报告正文表头的「采集调理」一行，便于复算与审计。
- `GET /api/sessions/{uuid}/artifacts?page=1&limit=50`
  → `{ "items": [{ kind, path, bytes, sha256, exists, download }], "total": 7, "limit": 50, "page": 1 }`
  同一信封与分页参数；单会话产物通常不足 10 条（`report_md/report_json/heatmap_svg/trend_svg/
  model/history/zip` 的子集），默认一页即可取全，`total` 为登记到 `artifacts` 表的产物总数。
- `GET /api/sessions/{uuid}/artifacts/{kind}` → 文件下载（`report_md/report_json/heatmap_svg/
  trend_svg/model/history/zip`），带 `Content-Disposition`。
- `GET /api/sessions/{uuid}/export.zip` → 打包全部产物 + `manifest.json`（含 sha256）。

## 模型与趋势

- `POST /api/models/train` → `{ "subjects": 6, "windows_per_state": 8 }`
  返回 `spec, version`（引擎 `config.VERSION`）、`features[]`、`subject_split{train,validation,test}`、
  `samples`（= 被试数 × 状态数 × 每状态窗数）、`participants[]`（参与训练的被试编号）、
  `train, validation, test`（后两者在划分不足时可能为 `null`）、`model_path`。
- `GET /api/reports/trend?participant=p01&field=focus&period=week`
  → `points[]`（来自数据库）、`ledger_points[]`（来自 JSONL 台账）、`sessions`、
  `comparable` / `rejected` / `rejected_reasons`（可比性过滤结果）、`note`。
  `ledger_points` 汇总的是**每个会话自己的**台账 `<runs_root>/<uuid>/history/sessions.jsonl`
  （外加 `export-ledger` 导出的 `<data_dir>/history/sessions.jsonl`），按 `session_uuid` 去重；
  聚合口径与 `GET .../{uuid}/trend` 一致（`period/mean/n/std`，并带 `rejected` 计数）。

---

## 认证（可选）

以 `serve --token <字符串>` 启动后，**只有 `/api` 请求**必须带令牌，两种方式任选：

- 请求头 `X-API-Token: <字符串>`（前端 `fetch` 用这种）；
- 查询参数 `?token=<字符串>`（`EventSource` 无法自定义请求头时用这种，SSE 订阅即靠它）。

**静态资源（`/`、`/index.html`、`/js/*`、`/css/*` 与 SPA 回退路径）不受令牌约束**：页面本身不含数据，
令牌是接口访问控制；否则带 `--token` 启动时连 SPA 入口都拿不到，页面无法 boot。缺少或错误的令牌返回
`401` + `error.code = "unauthorized"`。

未配置令牌时（默认）不做任何校验，本机演示无需认证。

1. 实时：优先 `EventSource('/api/sessions/<uuid>/events')`；断线后浏览器自动重连并带 `Last-Event-ID`。
2. 兜底：`setInterval` 轮询 `/live`、`/heatmap`（10 秒）。
3. 交互阶段：收到 `scale_request` / `behavior_request` / `trial` 事件后渲染对应界面，
   逐试次 `POST`；收到 `phase.done` 或 `finished` 后停止。
4. 所有数值均已做 NaN/Inf → `null` 处理；前端需容忍 `null`（表示该窗不可用）。
5. 颜色只用 `config.heatmap_bands` 与 `heatmap.legend` 给的值，不要自造状态色。
