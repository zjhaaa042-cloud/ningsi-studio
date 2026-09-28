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
| 401 | unauthorized | 配置了 `--token` 但请求未带 `X-API-Token` |
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
  "active_sessions": ["<uuid>"], "max_active_sessions": 2,
  "overview": { "subjects": 1, "sessions": 3, "sessions_done": 2, "alerts": 5, "latest": [] }
}
```

### `GET /api/config`
返回 `version`、`specs`、`window_sec`(4.0)、`step_sec`(2.0)、`welch`、`bands`、`total_band`、
`indicators`、`score_gain`、`baseline_protocol`、`quality`、`alerts`、`assess`、`training`、
`heatmap_bands`、`scale_boundary`、`phases`、`privacy`（隐私与结论边界原文）。

### `GET /api/devices?probe=1.0`
```json
{ "sources": [ { "key": "sim-bsense", "kind": "sim", "srate": 250.0, "channels": 1,
                 "device": "sim-bsense", "note": "仿真脑电源：数据来源已在界面与报告中标注",
                 "real": false, "hardware_note": "未发现 LSL 流。请先启动采集端…" },
               { "key": "lsl:ningsi-sim-eeg", "kind": "lsl", "srate": 250.0, "channels": 1,
                 "device": "ningsi-sim-eeg", "real": true,
                 "note": "真实 LSL 流：ningsi-sim-eeg（1 通道，250 Hz）",
                 "stream_type": "EEG", "channel_labels": ["Fp1"], "source_id": "" } ] }
```
装了 `pylsl` 且扫描到流时会追加 `kind: "lsl"` 的条目；不认识的流类型也会列出并标 `supported: false`。

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
`live=false` 且 `seconds_since_last` 持续变大，说明设备已停/线掉了：此时会话会把窗判为不可用
（界面上显示为缺失色块），不会拿旧数据继续算指标。

### `GET /api/overview`
首页统计 + 最近会话 `latest_detail` + 最近被试 `subjects_recent` + `active_sessions` + `source_note`
（当前默认数据源的说明文案，供顶栏展示）。

### `GET /api/sessions` 与列表类响应的分页约定
`{"items": [...], "total": N, "limit": L, "page": P}`；`page` 从 1 开始，`limit` 上限 200。

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
{ "participant": "p01", "device": "sim-bsense", "time_scale": 0.05,
  "training_mode": "quick", "srate": 250.0, "channels": 1, "label": "第一次训练",
  "create_subject": true }
```
`time_scale` 取值 `[0.01, 1.0]`；**`time_scale < 0.2` 视为快速演示模式**，量表、SART、PVT 由服务端
生成确定性作答（无需前端交互），用于评审演示与自动化测试。

响应 `201`：
```json
{ "session": { "uuid": "<32位>", "participant": "p01", "status": "running", "phase": "qc",
                "phase_label": "设备质检", "progress": 0.0, "device": "sim-bsense",
                "time_scale": 0.05, "training_mode": "quick", "engine_versions": {},
                "source": "sim-bsense", "srate": 250.0, "channels": 1,
                "started_at": "...", "ended_at": null, "error": null },
  "events_url": "/api/sessions/<uuid>/events",
  "phases": [ { "key": "qc", "label": "设备质检", "interactive": false, "weight": 0.04 } ],
  "runtime": { "alive": true, "source": "sim-bsense" } }
```
并发超限返回 `429`。

### `GET /api/sessions?participant=p01&status=done&page=1&limit=50`
`items[]` 为会话对象，额外含 `alert_count`。

### `GET /api/sessions/{uuid}`
会话对象 + `alerts`、`runs`（每阶段一行：`phase/status/duration_ms/error/payload`）、
`artifacts`、`indicator_summary`、`runtime{ alive, awaiting_input, source, source_kind }`。

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
| `started` | `participant, device, source_kind, source_note, time_scale, auto, phases[], engine_versions` |
| `notice` | `level, message`（例如"当前使用仿真脑电源"） |
| `phase` | `key, label, state(running/done), progress` |
| `progress` | `key, progress, windows_done?, windows_total?, trials_done?, trials_total?, segments_done?, segments_total?` |
| `quality` | `windows, usable, valid_ratio, reasons{}, passed` |
| `baseline` | `key(baseline_open/baseline_closed), label, baseline{...}` |
| `window` | `index, t_end, state, usable, quality, scores{focus,relax,load}, index_z, band_z, reasons[], alerts[], signal{srate,channels,samples[],relative,time_domain}` |
| `scale_request` | `code, label, size, instruction` |
| `scale_scored` | `code, raw_score, standard_score, level` |
| `behavior_request` | `task, digits[], nogo_trials, practice[], sequence_set_id, seed`（PVT 为 `onsets[],duration_sec`） |
| `trial` | SART：`phase(practice/main/done), index, digit, total`；PVT：`index, onset, total` |
| `behavior` | `task, result{...}` |
| `monitor` | `summary, quality` |
| `feedback` | `segment, target, score, on_target, usable, t` |
| `segment` | `seq, index, target, target_after, hold_sec, hold_after, stats{mean,on_target_ratio,volatility,n}, samples[[t,score]]` |
| `training_start` | `mode, segments, segment_sec, target, rationale, hold_sec` |
| `assessment` | 同 `GET .../assessment` |
| `scales` | `results[]`（SAS/SDS 计分结果汇总，量表阶段结束时下发一次） |
| `model` | `spec, features[], subject_split, train, validation, test, model_path` |
| `artifacts` | `kind -> path` |
| `cancelled` | `message` |
| `error` | `message, detail` |
| `finished` | `status(done/failed/cancelled), error, summary` |

重连：带上 `Last-Event-ID: <id>` 头（或 `?last_event_id=`）即可补发漏掉的事件；
不带 id 订阅时只补发"快照类"事件（阶段快照 `phase`，会话已结束时还有 `finished`），
随后若总线已关闭会立刻收到 `closed`，客户端据此结束连接。

### `GET /api/sessions/{uuid}/signal?hz=10&seconds=10&points=1200`
**高频原始信号 SSE**（`text/event-stream`）：给"实时监测"页的波形与频谱供数，
与 4 秒分析窗**解耦**——按 `hz`（默认 10 帧/秒，上限 25）直接读采集缓冲的最新样本。

- `seconds`：每帧覆盖的时间跨度（2–30 秒，默认 10）
- `points`：每通道最多返回多少个点（200–4000，默认 1200）；抽稀用 **min/max 保峰值**，
  尖峰/伪迹不会被均匀抽样抹掉
- 会话不在运行中时返回 409

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

- `GET /api/scales` → `{ "items": [{ "code": "SAS", "name": "焦虑自评量表", "size": 20,
  "version": "zung-cn-v1", "estimate_minutes": 5 }] }`
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
  评估未完成时 `409`。
- `GET /api/sessions/{uuid}/training` → `{ "segments": [...], "summary": {...} }`；
  `summary` 含 `mean_focus, on_target_ratio, first_to_last_change, baseline_before,
  baseline_after, target_rationale, baseline_comparable`。
- `GET /api/sessions/{uuid}/report` → 报告 JSON（`spec/participant/indicators/quality/scales/
  behavior/assessment/extras`），并附 `report_markdown`（报告原文，Markdown 文本）与
  `report_markdown_path`；尚未生成时返回 `202` 且带 `partial: true` 与阶段进度。
- `GET /api/sessions/{uuid}/artifacts` → `{ "items": [{ kind, path, bytes, sha256, exists, download }] }`
- `GET /api/sessions/{uuid}/artifacts/{kind}` → 文件下载（`report_md/report_json/heatmap_svg/
  trend_svg/model/history/zip`），带 `Content-Disposition`。
- `GET /api/sessions/{uuid}/export.zip` → 打包全部产物 + `manifest.json`（含 sha256）。

## 模型与趋势

- `POST /api/models/train` → `{ "subjects": 6, "windows_per_state": 8 }`
  返回 `spec, features[], subject_split{train,validation,test}, samples, train, validation, test,
  model_path`。
- `GET /api/reports/trend?participant=p01&field=focus&period=week`
  → `points[]`（来自数据库）、`ledger_points[]`（来自 JSONL 台账）、`sessions`、
  `comparable` / `rejected` / `rejected_reasons`（可比性过滤结果）、`note`。

---

## 认证（可选）

以 `serve --token <字符串>` 启动后，所有 `/api` 请求都必须带令牌，两种方式任选：

- 请求头 `X-API-Token: <字符串>`（前端 `fetch` 用这种）；
- 查询参数 `?token=<字符串>`（`EventSource` 无法自定义请求头时用这种，SSE 订阅即靠它）。

未配置令牌时（默认）不做任何校验，本机演示无需认证。

1. 实时：优先 `EventSource('/api/sessions/<uuid>/events')`；断线后浏览器自动重连并带 `Last-Event-ID`。
2. 兜底：`setInterval` 轮询 `/live`、`/heatmap`（10 秒）。
3. 交互阶段：收到 `scale_request` / `behavior_request` / `trial` 事件后渲染对应界面，
   逐试次 `POST`；收到 `phase.done` 或 `finished` 后停止。
4. 所有数值均已做 NaN/Inf → `null` 处理；前端需容忍 `null`（表示该窗不可用）。
5. 颜色只用 `config.heatmap_bands` 与 `heatmap.legend` 给的值，不要自造状态色。
