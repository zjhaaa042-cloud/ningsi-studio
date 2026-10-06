# 凝思 Studio（Ningsi Studio）

**A09 赛题**「AI+便携脑电设备的专注力强化训练系统」的可运行 Web 工程：
**前端（浏览器界面）+ 后端（HTTP API + 会话运行时）+ 数据保存（SQLite 权威库 + JSONL 审计台账）**。

算法能力全部来自上游产品层仓库 [`ningsi`](https://github.com/zjhaaa042-cloud/ningsi)
（信号处理、量表、SART/PVT-B、联合评估、神经反馈训练、模型训练、报告），本仓库只做
**服务、持久化与界面**，因此三端（CLI / 桌面版 / Web）口径完全一致。

```
浏览器 (web/, 原生 ES Module + 自绘 SVG/Canvas)
   │  fetch / SSE
   ▼
HTTP 服务 (src/ningsi_studio/http/)  ──  静态资源同端口提供
   │
   ├── API 层 (api/routes.py)         REST + SSE 事件流 + OpenAPI 描述
   ├── 采集层 (acquisition/)          常驻缓冲 LSL 采集 + 内置仿真 LSL 采集端
   ├── 运行时 (core/runtime.py)        十一阶段状态机，后台线程逐窗推进
   ├── 领域层 (domain/)                封装 ningsi 引擎能力
   └── 数据层 (db/)                    SQLite(WAL) 权威库 + JSONL 台账双写
                                          │
                                          ▼
                              var/studio/  （数据库、产物、日志）
```

---

## 一、快速开始

### 0. 先装算法引擎（本仓库不含引擎）

引擎 `ningsi` **没有发布到 PyPI**，克隆本仓库后必须先装上它（任选一种）：

```powershell
# ① 与本仓库并排放在同一父目录时（推荐开发用）
cd <父目录>
git clone https://github.com/zjhaaa042-cloud/ningsi.git
cd ningsi-studio
pip install -e ..\ningsi

# ② 或者直接从 GitHub 安装
pip install git+https://github.com/zjhaaa042-cloud/ningsi.git
```

`run.bat` 的依赖检查会告诉你 `ningsi` 是否已就绪；缺失时会引导你运行
`scripts\bootstrap.ps1`，它同样会尝试上面两条路径。

### 1. 启动

```powershell
.\run.bat                 # 双击也可以：菜单里选 1 = 启动服务
```

`run.bat` 会依次：解析解释器（优先 `.venv`）→ 校验 `ningsi` / `ningsi_studio` 可导入 →
（必要时）引导 `scripts\bootstrap.ps1` 建虚拟环境并安装依赖 → 启动服务 → 打印访问地址。
双击时窗口会保持打开（失败也会停住并打印完整输出），日志同时写入 `%TEMP%\ningsi-studio-run.log`。

**最简兜底**：如果 PowerShell 不便使用（执行策略受限等），直接双击 **`start.bat`**——
它用纯 cmd + python 启动服务，不经过 PowerShell：

```bat
start.bat          :: 默认 8765 端口
start.bat 8790     :: 换端口
```

命令行方式：

```powershell
.\run.ps1 -Action bootstrap      # 只准备环境（venv + pip install -e ../ningsi -e .）
.\run.ps1 -Action check          # 静态自检（语法、接口/事件文档、前端资源与调用一致）
.\run.ps1 -Action serve -Open    # 启动服务并打开浏览器
.\run.ps1 -Action demo           # 无浏览器跑一次完整会话（快速模式）
.\run.ps1 -Action test           # 跑自动化测试
.\run.ps1 -Action smoke          # 对运行中的服务做 HTTP + 产物冒烟
.\run.ps1 -Action doctor         # 环境自检
```

直接使用 Python：

```powershell
# 引擎已用 pip install -e ..\ningsi 装进当前环境时：
$env:PYTHONPATH="src"
# 引擎未安装（或像本机一样 editable 安装被移动过、.pth 失效）时，把引擎源码一起指上：
# $env:PYTHONPATH="src;..\ningsi\src"      # 引擎放在同级目录
# $env:PYTHONPATH="src;..\ningsi\ningsi\src"  # 引擎在本仓库的同级子目录里
python -m ningsi_studio doctor
python -m ningsi_studio serve --port 8765 --open
python -m ningsi_studio demo --participant p01 --speed 0.05
python -m ningsi_studio export-ledger      # 从库重放 JSONL 台账
```

> 提示：`run.ps1` / `run.bat` 会自动探测引擎源码目录并设置 `PYTHONPATH`（`run.ps1` 的 `Test-Engine`
> 依次尝试 `src`、`..\ningsi\src`、`..\ningsi\ningsi\src`、`vendor\ningsi\src`），所以**用启动器不需要手动设**；
> 只有像上面这样直接敲 `python -m ...` 时才需要自己设。若报 `No module named 'ningsi_studio'`，
> 就是这个变量没设或设置成了不存在的路径。

访问：

| 地址 | 内容 |
|---|---|
| `http://127.0.0.1:8765/` | 前端界面（九个视图） |
| `http://127.0.0.1:8765/api/openapi.json` | 接口描述（机器可读） |
| `http://127.0.0.1:8765/api/health` | 服务与口径版本自检 |
| `http://127.0.0.1:8765/api/devices` | 可用数据源：仿真源 + 扫描到的真实 LSL 流 |
| `http://127.0.0.1:8765/api/devices/status` | 运行中会话的设备体检（是否在收数、实测采样率） |

### 接真实脑电数据（LSL）

**完整说明见 [接入真实脑电数据.md](接入真实脑电数据.md)**（含参考工程 bsense-suite 的采集经验对照、
现场排查表）。两条命令即可验证整条采集链路：

```powershell
# 终端 1：没有硬件时，用内置仿真流假扮采集端（链路上每一层都是真的 LSL）
$env:PYTHONPATH="src;..\ningsi\src"; python -m ningsi_studio simulate-outlet --name ningsi-sim-eeg --srate 250

# 终端 2：自检（扫描流 + 实测采样率），然后起服务
$env:PYTHONPATH="src;..\ningsi\src"; python -m ningsi_studio lsl-check --device lsl:ningsi-sim-eeg --seconds 6
$env:PYTHONPATH="src;..\ningsi\src"; python -m ningsi_studio serve --port 8765
```

真设备只需把它在 LSL 上发布的流名填进新建会话的「设备 / 数据源」：`lsl:<流名>`
（新建会话的输入框带 `GET /api/devices` 的下拉候选，同时保留手填），
并把时间倍率设为 `1.0`（真实节奏）。采集侧采用常驻缓冲 + 断线重连（见
`src/ningsi_studio/acquisition/lsl.py`）：设备停掉时窗口会被判为不可用，
**不会**拿缓冲里的旧数据继续算指标。

真机链路上还有三条与仿真源不同的口径（都有回归测试，见 `tests/test_lsl_acquisition.py`）：

1. **按点数取窗**：窗口长度 = `窗长 × 声明采样率` 个样点。真机时间戳会漂，按时间戳切片
   实测会把"4 秒窗"切成 2.7~6.8 秒，连带 Welch 分辨率失真；
2. **逐通道去直流 + 文档 8.2 处理链**（0.5 Hz 去漂移 → 50/60 Hz 陷波 → 45 Hz 低通，零相位）：
   设备直出值带几百 mV 电极偏置，不去直流就送质检会让每一窗都判 `amplitude/channel_span` 越界；
   调理口径随会话留痕（`status()["conditioning"]`、报告 `extras.signal_conditioning` 与正文"采集调理"行）；
3. **按流名精确匹配**：现场可能同时有真机与 `ningsi-sim-eeg` 两条 EEG 流，填了哪条只连哪条，
   不会"只按类型取第一条"而把界面写着的设备名与实际采集的流错配。


端口被占用时自动向后探测 5 个端口；`--port` 可指定起始端口。

Linux / macOS 等价入口（首次需要 `chmod +x run.sh scripts/smoke.sh`）：

```bash
cd ningsi-studio
./run.sh bootstrap        # 准备环境
./run.sh serve            # 启动服务（http://127.0.0.1:8765/）
./run.sh demo             # 无浏览器跑一次完整会话
./run.sh test             # 自动化测试
./run.sh check            # 静态自检
./scripts/smoke.sh        # 对运行中的服务做冒烟
```

### 打包成单文件 exe（给没有 Python 的机器用）

```powershell
.\run.ps1 -Action package        # 等价于 .\scripts\build_exe.ps1（默认单文件）
.\scripts\build_exe.ps1 -Mode onedir   # 目录形式：启动更快，但要整个目录一起拷
```

产物 **`dist\ningsi-studio.exe`（约 20 MB，单文件）**：里面已含引擎、numpy、pylsl（带 liblsl）与整套前端资源。

- **双击即用**：没有子命令时默认 `serve --open`（起服务并打开浏览器）；
- 与源码运行**同一套 CLI**：`ningsi-studio.exe serve|demo|doctor|lsl-check|simulate-outlet|export-ledger`；
  `serve --port 8790 --data D:\ningsi-data` 可指定端口与数据目录；
- **数据放哪**：优先 exe 同级 `var\studio`（便携，整个目录拷到 U 盘就能带走）；
  该目录不可写（例如装在 `Program Files`）时自动退到 `%LOCALAPPDATA%\ningsi-studio\var\studio`；
  也可用 `--data` 或环境变量 `NINGSI_STUDIO_DATA` 指定；
- **只读资源**（`web/`）从包内读（`sys._MEIPASS`），**不写进临时目录**，所以每次运行数据不丢；
- `check` 与 `ui-check` 需要源码树（exe 里没有 `src/tests/scripts`），打包后会明确提示改用源码运行；
- 首次启动约 2–3 秒（单文件自解压），之后同机启动更快。

本机实测（Windows 10 + Python 3.12.4 / PyInstaller 6.22.3）：`ningsi-studio.exe doctor` 退出码 0、
`lsl-check` 能在 exe 内扫到真实 LSL 流（7 条，含 BioMulti Lite EEG）、`demo` 跑完整场会话并落盘 6 类产物、
`serve` 起服务后 `scripts\smoke.ps1` **全部通过**（含报告/热力图/趋势/`export.zip`）。

### 依赖

| 依赖 | 必要性 | 说明 |
|---|---|---|
| Python ≥ 3.11 | 必需 | 只用到标准库 |
| `numpy` | 必需 | 引擎算法依赖 |
| `ningsi` | 必需 | 算法引擎（`pip install -e ../ningsi` 或已发布版本） |
| `pylsl==1.18.2` | 必需（已随依赖安装） | 接入真实脑电设备与 LSL 仿真流都需要它；万一装不上，服务仍可启动但只能用仿真源，界面与报告会明确标注 |

**刻意不使用 Web 框架**：`http.server` 足够支撑本工程的接口与 SSE，换来"任何机器一条命令可跑"，
不需要安装 FastAPI/uvicorn，也避免了评审现场的网络与依赖问题。

---

## 二、能演示什么

一条完整会话包含 11 个阶段（进度权重见 `/api/config` 的 `phases`）：

| # | 阶段 | 产出 |
|---|---|---|
| 1 | 设备质检 | 4 个静息窗 + 1 个伪迹窗，逐窗质检与不可用原因统计 |
| 2–3 | 睁眼 / 闭眼静息基线 | 各 2 分钟（4 秒窗 / 2 秒步长），各自独立建基线，任务态以睁眼为参照 |
| 4 | 量表 | SAS / SDS 各 20 题（含反向题），粗分 × 1.25 得标准分并分级 |
| 5 | SART | 12 练习 + 180 正式试次（20 个 No-Go），服务端持有序列并计分 |
| 6 | PVT-B | 3 分钟警觉任务，中位反应时、慢反应率、抢答 |
| 7 | 任务态监测 | 35 窗（rest/focused/drowsy）逐窗指标 + 三类实时预警 |
| 8 | 神经反馈训练 | 专注度驱动反馈，目标按表现自适应（步长 0.05，保持 6–20 秒） |
| 9 | 联合评估 | 脑电 / 量表 / 行为三类证据一致性判定与建议 |
| 10 | 模型训练 | 六维频带特征逻辑回归，被试级 6:2:2 划分 + AUC |
| 11 | 报告与产物 | Markdown / JSON 报告、热力图 SVG、趋势 SVG、模型 JSON、zip 打包 |

**演示模式**：创建会话时把 `time_scale` 设为 `0.05`（20 倍速），服务端进入快速演示模式——
量表、SART、PVT 由服务端生成确定性作答，一次完整链路（含模型训练与报告落盘）约 1–2 分钟，
适合评委现场演示与自动化测试；`time_scale = 1.0` 为真实节奏，此时量表与按键任务由前端交互完成，
一条完整会话需要 20 分钟以上（SART 2.2 秒/试次、PVT-B 3 分钟、两段静息基线各 2 分钟）。

数据来源始终标注：未接真实设备时界面顶栏、会话 `source` 字段与报告 `extras` 都会写明"仿真"，
不作静默替换（与 `ningsi/docs/REQUIREMENT_COVERAGE.md` 的边界说明一致）。

**交互阶段可断点续做**：SSE 只推新事件、不重放历史，因此"页面在交互阶段中途打开/刷新"曾经拿不到
作答区（量表是纯前端表单缺失；SART/PVT 更严重——试次由客户端驱动、`INPUT_TIMEOUT_SEC = 900s`
且不随 `time_scale` 缩放，没人作答就整段停摆）。现在 `web/js/views/flow.js` 在拿到
`GET /api/sessions/{uuid}` 的 `runtime.awaiting_input` 后重建交互区：量表按 `scales:<CODE>`
重建作答表单（幂等，多次刷新不会出现两份）；SART/PVT 则补交一笔 `responded=false`（不伪造反应时）
解锁在途试次，等下一个 `trial` 事件继续，并把行为任务区滚到视口内（否则 1000px 视口下刺激在折叠线以下）。

---

## 三、数据模型（SQLite，WAL 模式）

| 表 | 内容 |
|---|---|
| `subjects` | 被试匿名编号、年龄段/性别/利手、同意版本与时间、备注 |
| `sessions` | 一次会话：设备、采样率、通道、数据源、时间倍率、状态、阶段、进度、口径版本 |
| `runs` | 每阶段一行：开始/结束、耗时、可用窗比例、错误、阶段中间结果（JSON） |
| `metrics` | 逐窗指标与阶段汇总（`kind = indicator / indicator_summary`） |
| `alerts` | 预警事件（类型、状态、时刻、持续秒数、文案） |
| `scale_runs` | 量表作答、粗分、标准分、分级、逐题有效分 |
| `behavior_runs` | SART / PVT-B 指标与逐试次记录 |
| `training_segments` | 训练分段：目标、达标时间占比、波动、采样序列 |
| `artifacts` | 产物清单（路径、字节数、sha256） |
| `schema_version` | 迁移版本（幂等建表） |

**JSONL 台账**：`runs/<uuid>/history/sessions.jsonl` 与 `runs/<uuid>/scales/*.jsonl` 由
`core/paired_ledger.py` 双写，格式与 `ningsi.monitoring.history` / `ningsi.scales.store` 一致，
因此 Studio 与 CLI 产出的记录可以被同一套趋势逻辑读取。台账写入失败只记警告不阻断会话，
可用 `ningsi-studio export-ledger` 从库中重放修复。

产物目录（默认 `var/studio/runs/<uuid>/`）：

```
reports/sub-<id>_ses-01_run-001_report.md      评估报告（Markdown）
reports/sub-<id>_ses-01_run-001_report.json    评估报告（结构化）
reports/sub-<id>_ses-01_run-001_heatmap.svg    状态热力图
reports/trend.svg                              专注度周趋势
models/classifier.json                         逻辑回归模型与指标
history/sessions.jsonl                         历史台账
export.zip                                     全部产物 + manifest（含 sha256）
```

数据目录可用 `--data` 或环境变量 `NINGSI_STUDIO_DATA` 指定。

---

## 四、接口

完整契约、事件类型与示例见 **[docs/API.md](docs/API.md)**；`GET /api/openapi.json` 为机器可读版本。
主要分组：

- 基础：`/api/health`、`/api/config`、`/api/devices`、`/api/overview`、`/api/openapi.json`
- 被试：`GET|POST /api/subjects`、`GET|PATCH /api/subjects/{public_id}`、`.../sessions`
- 会话：`POST|GET /api/sessions`、`GET|DELETE /api/sessions/{uuid}`
- 实时：`GET /api/sessions/{uuid}/events`（SSE）、`/live`（轮询兜底）、`/heatmap`、`/trend`
- 量表：`GET /api/scales[/{code}]`、`POST /api/sessions/{uuid}/scales/{code}`
- 行为：`GET .../behaviors/{sart|pvt}/sequence`、`POST .../behaviors/{sart|pvt}/trial`、`.../result`
- 评估与训练：`/assessment`、`/training`、`/report`
- 产物：`/artifacts`、`/artifacts/{kind}`、`/export.zip`
- 模型与趋势：`POST /api/models/train`、`GET /api/reports/trend`

SSE 事件：`started / notice / phase / progress / quality / baseline / window / scale_request /
scale_scored / behavior_request / trial / behavior / monitor / training_start / feedback / segment /
assessment / model / artifacts / cancelled / error / finished`。

错误统一为 `{"error": {"code", "message", "detail"}}`，状态码语义固定（400/401/404/405/409/422/429/500）。

---

## 五、测试与验收

```powershell
.\run.ps1 -Action check       # 静态自检：语法、接口与事件文档、前端资源与接口调用一致性
.\run.ps1 -Action test        # 105 个用例：单元 + 集成 + 端到端（无需浏览器与真实设备）
.\run.ps1 -Action smoke       # 对已启动服务做 HTTP 冒烟：建被试 → 建会话 → 校验报告/热力图/趋势/zip
```

测试覆盖：健康与配置端点、被试 CRUD 与校验（含 PATCH 编辑）、会话生命周期与并发上限、SQLite 持久化与重开、
SSE 事件与 `Last-Event-ID` 重放（含裸 `NaN/Infinity` 净化）、量表/行为计分与引擎口径一致、
会话列表 `phase_label` 与结束后 `runtime.source_kind` 回归、终态 `phase` 与 `status` 对齐、
`SignalFeedRegistry` 清扫、趋势台账聚合、集合响应统一信封（`/api/scales`、产物清单）与真分页、
`--token` 只保护 `/api`（静态资源放行、401 `code=unauthorized`）、
`/api/overview`、`/api/devices/status`、非运行会话 `/signal` 409、`POST /api/models/train`、端到端产物齐全、
**LSL 采集层与真机调理**（`tests/test_lsl_acquisition.py`：流类型归一与描述符兜底、按点数取窗、
流名精确匹配、去直流+四级链后才过质检、贴轨设备判 `flat_channel`、内置仿真 outlet 的文案标注）。

**本机验证记录（Windows 10 + PowerShell 5.1 + Anaconda Python 3.12.4 + NumPy 2.2.6）**

| 检查 | 命令 | 结果 |
|---|---|---|
| 语法编译 | `python -m compileall -q src tests` | 通过 |
| 静态自检 | `python -m ningsi_studio check` | 通过（48 个 py 文件 / 36 条路由 / 23 种文档事件 / 24 种发布事件） |
| 自动化测试 | `python -m unittest discover -s tests -t .` | **105 个用例全部通过**（实测约 170 秒） |
| 端到端链路 | `python -m ningsi_studio demo --participant p01 --speed 0.05` | `status=done`，产出报告 md/json、热力图、趋势、模型、台账 |
| HTTP 冒烟 | `scripts\smoke.ps1`（对运行中的服务） | 全部通过，含 `export.zip` 9.8 KB |
| 前端资源 | 逐个请求 `web/` 下 15 个文件 | 全部 200，MIME 正确（含 `text/javascript`） |
| 启动器 | `run.bat check` / `run.ps1 -Action doctor` | 退出码 0 |

> 冒烟与启动器均在 **Windows PowerShell 5.1** 下跑通；`run.ps1` 与两个 `scripts\*.ps1` 因此保持纯 ASCII
> （5.1 会按 ANSI/GBK 读取脚本文件，非 ASCII 字节会破坏解析）。中文说明放在 `STUDIO-GUIDE.md`。

---

## 六、边界与隐私

- 系统用于研究、竞赛与自我调节训练，**不提供医疗诊断**；量表结果只作提示，不作诊断依据。
- 结论不得用于处罚、自动上岗决策或永久能力画像（报告与界面均保留该声明）。
- 被试编号匿名且跨会话一致；姓名等直接身份信息不进入文件名、报告与数据库。
- 真实设备链路需现场逐台验收；未安装 `pylsl` 时全部演示与测试使用仿真源，并如实标注。
- 上游 `bsense-suite/`（采集与数据集工程）保持只读，本工程不修改其代码。

## 七、与上游的关系

| 仓库 | 版本/位置 | 本工程如何使用 |
|---|---|---|
| `ningsi`（本工作区同级目录） | 0.1.0 | **算法引擎**：`signal/ scales/ behavior/ assessment/ training/ monitoring/ models/ acquisition/` 全部复用 |
| `bsense-suite/bsense-lsl` | 0.8.0 | 只读参照：采集协议、实时监测、质量门控 |
| `bsense-suite/bsense-dataset-studio` | 0.2.0 | 只读参照：试采产物格式（Studio 通过引擎适配器读取其 `records.csv`） |

本仓库自研代码以 MIT 许可发布；上游采集工程未附带许可证，本工程未复制其代码。
