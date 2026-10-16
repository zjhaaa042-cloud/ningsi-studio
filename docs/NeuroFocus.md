# NeuroFocus 神经专注监测台

更新：2026-10-16。支持本地 DEMO 和现有 Studio 会话的 REST/SSE 接入。沿用原生 ES Modules、SVG/Canvas 与 Python 服务。

## 启动与操作

工作区根目录运行 `run.bat`，或 `.\run.ps1 -Action serve -NoBrowser`，访问 `http://127.0.0.1:8765/neurofocus/`。

**本地演示**：默认选中 DEMO，但不自动开始。显式开始后才生成固定种子的合成 EEG、演示分数与模拟频段。支持暂停/继续、结束、通道/窗口选择、趋势游标、波形冻结、减少动态效果和异常场景。

**后台接入**：

1. 先在原 Studio 开始检测，明确选择 LSL 或仿真源。LSL 会话必须使用时间倍率 **1.0**。
2. 从原“实时监测”页点击“NeuroFocus 监测台”直达；或在新页“数据与设备”选择“Studio 会话”。
3. 服务启用鉴权时填写访问令牌，刷新后台会话，明确选择运行中的会话，再点击“开始后台监测”。深链接只选中会话，不自动记录。
4. 波形可以先于评分出现。个体基线未建立、质量不足或数据失效时保持 `--`，不把会话汇总均值当作实时值。
5. **暂停、结束只控制本页订阅和记录，不取消后台实验。** 后台检测与保存继续；取消实验应回到原 Studio 操作。
6. 后台完成、取消、失败或不再运行后，关闭订阅并结束本页记录，不继续显示旧评分为实时值。

REST 使用现有 `X-API-Token`，SSE 使用现有查询令牌机制；支持 `?token=`。令牌不写入会话导出。没有另造 WebSocket 服务。

## 数据含义与边界

- **LSL 是传输方式。** 后端内置模拟 LSL 根据 `ningsi-sim-outlet-v1` / 来源说明识别，仍标 DEMO。不能因为 `source_kind=lsl` 就称为真实设备。
- 对真实 LSL 来源显示 LIVE。本轮只有合成 EEG 经实际 LSL 传输的验证，没有真实硬件、真人信号或识别效果验证。
- `/api/config.focus_score` 声明范围、算法 ID/版本。当前引擎范围 0–1，前端据此映射至 0–100 展示坐标；越界值拒收。
- 有效性取决于后端质量和 `indicator_available`。`calibrated` 表示后端已经产生有效个体基线评分，不是厂商硬件校准或临床校准证明。
- 分级阈值默认 40/70，最短驻留 2 秒，可修改；它们是显示用阈值，没有临床含义。
- 名义更新周期取 `step_sec × time_scale`，失效窗口按分析窗/步长确定；覆盖率按名义周期估计，不是采样丢包率。不能把本地 DEMO 的 1Hz / 3 秒直接套到后台。

## 时间和 EEG 快照

`window` 事件新增 `emitted_at_ms`，是服务器发送时间，不是设备采样时间。前端用 HTTP Date 估计钟差进行显示对齐与新鲜度检查，其精度受 HTTP 日期秒级分辨率影响。导出保留原 `backendEmittedAtMs`、相对会话的 `backendTEnd` 和事件 ID。没有设备端到端延迟或实际丢包率时不推测。

原 `/signal` 可能做过 min/max 抽稀。当前桥接请求 **2 秒 / 每通道最多 4000 点**，仅在 `points == raw_points == samples.length`、通道和 µV 单位一致时绘制；抽稀帧不会伪装为均匀原始采样。

新快照替换旧显示缓冲，不把重叠窗口重复累计为新样本。横轴是**服务器显示帧内的相对时间**，由样点数与声明采样率定位；不提供设备逐样本绝对时间戳，也不能反映窗内实际丢包时刻。已验证 2 通道 × 250Hz，其他负载仍需压测。

频段只接收当前可用窗的 `relative_band_powers`，频带边界、计算窗取后端配置，缺失频段不伪造。原始 EEG 尚未保存进会话导出。

## 记录与隐私

本页默认仅内存保存，最多最近 10 次监测，每次最多 21600 个评分/不可用事件；刷新清除。暂停不追加记录、不累计活跃时长，恢复只接收新事件，保留暂停/无效缺口。

JSON 包含后台会话 ID、来源、设备/算法元数据及变化、原分数、质量、事件与时间基准。CSV 使用每个点对应的算法、范围和基线标记。均值、中位数、极值只计算有效点；文件名标识 DEMO / LIVE。

本页不新增云上传或自动持久化。**后台仍执行原有 SQLite/JSONL 保存策略**，“本页仅存内存”不表示后台不保存。

## 实现与接口

`studio-adapter.mjs` 复用：

| 接口 | 用途 |
| --- | --- |
| `/api/config` | 评分范围、算法、频段、窗口/步长 |
| `/api/sessions/{uuid}` | 来源、运行状态、来源说明 |
| `/api/sessions/{uuid}/events` | 评分/质量 window 与终态 |
| `/api/sessions/{uuid}/signal` | 未抽稀显示窗 |
| `/api/sessions/{uuid}/live` | 每 2 秒检查后台是否仍运行 |

适配层校验会话 ID、源类型、事件 ID、分析窗标识、时间新鲜度和单位。重连时旧消息不会被重新当作当前评分。两条 SSE 独立清理；错误时清除旧评分并由浏览器重试，停止时关闭流、定时器和未完成请求。

`core.mjs` 新增 `focus_unavailable`（空值和原因）及 `eeg_snapshot`（明确 `timeBasis=server-frame-relative`）。不把缺失分数补成 0。

## 检查与构建

工作区根目录：

```powershell
node ningsi/ningsi-studio/scripts/neurofocus-check.mjs
.\run.ps1 -Action check
.\run.ps1 -Action test
```

第一条检查六个运行资产、DOM/导入契约及两组前端单元测试，生成 `verification/2026-10-16/neurofocus/static-build/` 和 SHA-256。现有项目没有 TypeScript/ESLint 工具链，不把原生语法检查称为 tsc/ESLint。

模拟 LSL 测试需要 Node、Playwright、Edge 与现有 Python `.venv`：

```powershell
# 终端 1：8796 端口、隔离数据库、明确的模拟 LSL 源
.\ningsi\ningsi-studio\.venv\Scripts\python.exe ningsi/ningsi-studio/scripts/neurofocus_lsl_fixture.py
# 终端 2
node ningsi/ningsi-studio/scripts/neurofocus-studio-browser.cjs
```

测试控制地址 `/api/__test__/outlet/*` 仅由 fixture 注册，不在正常应用路由内。结束后关闭 fixture，再跑“无设备”回归，避免模拟 LSL 干扰设备发现。`NF_PLAYWRIGHT` 可指定 Playwright 包路径。

2026-10-15 的 30 分钟 DEMO 稳定性记录属于当时版本。本轮新验收是实际节奏的模拟 LSL 接入/基线/断流恢复及 UI/回归，不能把历史耐久性成绩重复当作本版后台接入的测试成绩。详见 `verification/2026-10-16/neurofocus/接入验收记录.md`。

## 尚未完成

真实硬件/真人验收、文件回放、本页会话持久化、跨会话交互分析、原始 EEG 录制、厂商硬件校准、电量/阻抗、3D 和详细 FFT/PSD 工作台。

接入复用了现有后端算法，不要求设备额外提供专注度 SDK。真实设备上线前仍需核对通道、电极、采样率、单位、质量门控和算法适用性，不能把链路测试当作真人准确率或训练效果证明。
