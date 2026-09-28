# 凝思 Studio 使用与演示说明

本文件是给使用者的中文说明。**黑窗口（启动器）与命令行输出的提示语现在都是中文**：

- `run.ps1`、`scripts\bootstrap.ps1`、`scripts\smoke.ps1` → 中文界面，保存为 **UTF-8 BOM**；
- `run.bat`、`start.bat` → 纯 ASCII（cmd 没有 BOM 机制，中文容易乱码，所以它们只说英文，
  真正的中文提示由它们调起的 PowerShell 脚本输出）；
- Python 侧（`doctor` / `demo` / `lsl-check` / 启动横幅）→ 中文；
- 网页界面 → 中文。

> 改脚本前请留意编码：`.ps1` 必须以 **UTF-8 with BOM** 保存，否则 PowerShell 5.1 按 GBK 读取，
> 中文会破坏语法解析（典型报错 `Missing closing '}'`）。补 BOM 的命令：
> `python scripts\add_bom.py`。

## 一、三条命令就够

```powershell
cd <克隆下来的 ningsi-studio 目录>
.\run.ps1 -Action bootstrap     # 1. 准备环境（首次或换机器时执行一次）
.\run.ps1 -Action serve -Open   # 2. 启动服务并打开浏览器
.\run.ps1 -Action demo          # 3. 或者：无浏览器跑一次完整会话（快速模式）
```

不想用 PowerShell：**双击 `run.bat`**，在菜单里选 `1`（启动服务）。
默认地址 `http://127.0.0.1:8765/`；端口被占用时会自动往后找，启动日志里会打印实际地址。

**最省事的兜底入口**：双击 **`start.bat`**（纯 cmd + python，不经过 PowerShell，不受执行策略影响）。
需要换端口时用命令行：`start.bat 8790`。

## 二、菜单与参数

| 菜单 | 动作 | 等价命令 | 作用 |
|---|---|---|---|
| 1 | `serve` | `python -m ningsi_studio serve --port 8765` | 启动"前端 + 接口"，Ctrl+C 停止 |
| 2 | `demo` | `python -m ningsi_studio demo --participant p01 --speed 0.05` | 无浏览器跑完整链路，落盘报告与产物 |
| 3 | `test` | `python -m unittest discover -s tests -t .` | 自动化测试（无需浏览器/设备） |
| 4 | `check` | `python -m ningsi_studio check` | 静态自检：语法、接口/事件文档、前端资源与接口调用一致性 |
| 5 | `smoke` | `scripts\smoke.ps1` | 对**运行中**的服务做 HTTP 冒烟并校验产物 |
| 6 | `doctor` | `python -m ningsi_studio doctor` | 自检：引擎包、前端资源、依赖、库统计 |
| 7 | `bootstrap` | `scripts\bootstrap.ps1` | 建/复用 `.venv`，安装 `ningsi` 与 `ningsi-studio` |

常用参数：`-Port 8765`、`-Participant p01`、`-Speed 0.05`、`-Open`（启动后开浏览器）、
`-Recreate`（重建虚拟环境，仅 bootstrap 用）。

**双击 `run.bat` 的行为**：菜单执行完一个动作后会问「回到菜单继续吗？」，可以直接连着做几件事；
任何失败都会把完整输出打印出来并停住窗口（不会一闪而过），同时全程日志写在
`%TEMP%\ningsi-studio-run.log`。若依赖没装好，启动器会先打印一份诊断表
（python / numpy / ningsi / ningsi_studio / pylsl 各自正常还是缺失），并询问是否立即 bootstrap。

> **脚本编码注意（改脚本的人必看）**：`run.ps1`、`scripts\bootstrap.ps1`、`scripts\smoke.ps1`
> 都保存为 **UTF-8 BOM**。Windows PowerShell 5.1 默认按 ANSI/GBK 读取 .ps1 文件，
> 没有 BOM 时中文会破坏语法解析（典型报错是 `Missing closing '}'`）。
> 用编辑器另存为时请选择 "UTF-8 with BOM"，或运行 `python scripts\add_bom.py` 自动补上。
> 纯 cmd 的 `run.bat` / `start.bat` 保持 ASCII，因此不受影响。

Linux / macOS 用 `run.sh`（首次 `chmod +x run.sh scripts/smoke.sh`）：

```bash
./run.sh bootstrap     # 准备环境
./run.sh serve         # 启动服务
./run.sh demo          # 无浏览器跑完整会话
./run.sh test          # 自动化测试
./run.sh check         # 静态自检
./scripts/smoke.sh     # 对运行中的服务做冒烟
```

## 三、演示动线（建议 6 分钟）

1. `.\run.ps1 -Action serve -Open`，浏览器打开首页"概览"：先看顶栏的
   **数据来源徽标**与**引擎口径版本**（`welch-v1 / indicator-v1 / baseline-v1`）。
2. 进"被试管理"新建一名被试（编号留空会自动生成 `p01`），进入其会话列表。
3. 点"开始新会话"，**时间倍率保持默认 `0.05`（20 倍速演示）**，训练模式选 `quick`。
4. 自动跳到"会话流程"：顶部 **"当前该做什么"** 面板会大字写明这一步被试要做什么、
   要点、预计时长（每秒走动的已用/约剩）与推进方式；下面"全部阶段"清单可点开任意一步看说明。
   阶段逐步点亮（质检 → 基线 → 量表 → SART → PVT → 监测 → 训练 → 评估 → 模型 → 报告）。
5. 切到"实时监测"：看三指标进度条与阈值线、原始波形、状态热力图（灰块=低质量窗，不是状态变化）、预警时间轴。
6. 训练阶段在"训练中心"看仪表与目标线、达标保持计时、分段表格与训练前后基线对比。
7. 完成后到"报告"看结论、证据回填表、建议与边界声明；下载 zip 或单个产物。
8. 回到"历史与趋势"看周趋势（设备/采样率/通道变化时明确标注不可比）。

> 想演示**真实节奏与手动交互**（量表逐题作答、SART/PVT 按键）：新建会话时把时间倍率改成 `1.0`。
> 此时量表和按键任务由前端交互完成，界面会切换到答题页与刺激页（空格键作答），
> "当前该做什么"面板会提示你先点"开始作答 / 查看题目"，并在完成后自动进入下一步。
> 一条完整会话约需 20 分钟以上，建议只演示其中一段。

> **给改代码的人**：每个阶段的引导文案定义在 `src/ningsi_studio/core/phases.py`
> （`Phase.headline / details / duration_sec / advance / next_hint / auto_note`），
> 通过 `/api/config` 与 `started` 事件同时下发；`tests/test_api_health.py` 会校验这些字段
> 一个都不能少（缺了流程页就退化成一列没有指引的清单）。

## 四、数据落在哪里

默认 `ningsi-studio\var\studio\`：

```
studio.sqlite3                 权威数据库（WAL 模式）
runs\<uuid>\reports\*.md|json|svg
runs\<uuid>\models\classifier.json
runs\<uuid>\history\sessions.jsonl      与引擎 CLI 同格式的审计台账
runs\<uuid>\export.zip                  全部产物 + manifest(含 sha256)
logs\
```

换位置：`--data D:\path\to\data`，或设置环境变量 `NINGSI_STUDIO_DATA`。
台账若被误删，可用 `python -m ningsi_studio export-ledger` 从库中重放。

## 五、常见问题

| 现象 | 处理 |
|---|---|
| `未找到凝思引擎包 ningsi` | 执行 `.\run.ps1 -Action bootstrap`；或 `pip install git+https://github.com/zjhaaa042-cloud/ningsi.git` |
| 提示"前端资源缺失 web/index.html" | 仓库不完整，确认 `web/` 目录存在 |
| 端口被占用 | 启动日志会显示实际端口（自动 +1 探测），或 `-Port 8800` 指定 |
| 只有仿真数据，没有真实脑电 | 正常：未安装 `pylsl` 或未启动采集端。装好后在"设备"接口能看到 `lsl:<流名>` 并可在新建会话时选择 |
| 量表和按键任务没有交互界面 | 说明使用了快速演示模式（`time_scale < 0.2`），服务端自动作答；改 `1.0` 即为手动交互 |
| 趋势图只有一个点 | 属真实状态：同一被试可比记录只有一条，不要硬凑曲线 |
| 需要给接口加访问口令 | `serve --token <字符串>`，之后请求需带 `X-API-Token` |
