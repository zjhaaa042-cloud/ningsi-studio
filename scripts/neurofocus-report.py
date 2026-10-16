"""Generate the delivery record from completed test artifacts, never from targets."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[3]
OUT = WORKSPACE / "verification/2026-10-15/neurofocus"


def load(name: str):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def main():
    endurance = load("stability-result.json")
    assert endurance["passed"] and endurance["minutes"] >= 30
    assert endurance["actualElapsedSeconds"] >= 1800
    browser = load("final-ui/browser-checks.json")
    targeted = load("final-ui/targeted-checks.json")
    regression = load("regression-results.json")
    build = load("build-manifest.json")
    recording = load("operation-recording.json")
    integration = load("integration-checks.json")
    assert not browser["errors"] and not targeted["errors"] and not endurance["runErrors"]
    samples = endurance["samples"]
    first, last = samples[1], samples[-1]
    draw_rate = (last["draws"] - first["draws"]) / (last["elapsedSeconds"] - first["elapsedSeconds"])
    worst_draw = max(s["drawP95Ms"] for s in samples[1:])
    worst_arrival = max(s["arrivalToPaintP95Ms"] for s in samples[1:])
    date = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    checks = len(browser["checks"])
    unit_log = (OUT / "unit-tests.txt").read_text(encoding="utf-8")
    unit_count = int(re.search(r"tests (\d+)", unit_log).group(1))
    assert unit_count >= 32 and "fail 0" in unit_log
    file_rows = "\n".join(f"| `{f['file']}` | {f['bytes']} | `{f['sha256']}` |" for f in build["files"])
    matrix = "\n".join(f"- [x] {c['name']}" for c in browser["checks"])
    extra = "\n".join(f"- [x] {c}" for c in targeted["checks"])
    report = f"""# NeuroFocus P0 交付与验收记录

生成时间：{date}。本文根据已完成测试的 JSON 与日志生成，不把目标值当成实测成绩。

## 交付结果

在现有凝思 Studio 中新增独立的 `/neurofocus/` 入口，保留原检测、LSL、算法、API 和数据存储流程。本次沿用 Python 静态服务与原生 ES Modules，未替换为第二套 React 工程。

已完成 P0 的暗色响应式应用壳、可收起导航、分数/圆环、抽象 SVG 神经球、趋势游标与时间范围、多通道 Canvas EEG、频段功率、显式 DEMO、质量门控、断流/断连/校准演示、会话开始/暂停/结束、减少动态效果、数据契约、记录保留上限及异常拒收。

另外交付了浏览器内会话摘要、专注事件 JSON/CSV 导出和限时沉浸视图。它们是部分 P1 能力，不能据此声称完整 P1 已完成。

## 启动

工作区根目录运行 `run.bat`，或 `.\\run.ps1 -Action serve -NoBrowser`，访问 [NeuroFocus](http://127.0.0.1:8765/neurofocus/)。原 Studio 左侧也有入口。此处的 8765 是正常启动端口；验收使用隔离的 8792 服务和独立测试数据目录。

开始演示需要显式点击“开始演示会话”。初始无连接、无分数，不会自动伪造设备已连接。所有当前数值均是演示源，原始 EEG 是合成波形。

## 文件

- `ningsi/ningsi-studio/web/neurofocus/`：五个运行资产。
- `ningsi/ningsi-studio/web/index.html`：新增 NeuroFocus 导航入口。
- `ningsi/ningsi-studio/docs/NeuroFocus.md` 与工作区 `README.md`：运行、适配接口、数据含义、保留策略与未完成项。
- `ningsi/ningsi-studio/scripts/neurofocus*.mjs/.cjs/.py`：单元、静态构建、浏览器、操作帧、最终界面和报告生成脚本。
- `verification/2026-10-15/neurofocus/`：本报告、原始验收 JSON、截图、GIF、示例导出与构建清单。

只增补新的前端模块和原入口链接，未重建历史 EXE、比赛提交包，也未把原工作区已有未提交改动当作本次实现。

## 实际检查结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 新增前端单元测试 | {unit_count} / {unit_count} 通过 | `unit-tests.txt` |
| 原引擎回归 | {regression['engine']['passed']} / {regression['engine']['tests']} 通过 | `regression-results.json`，实际命令结果摘要 |
| 原 Studio 回归 | {regression['studio']['passed']} / {regression['studio']['tests']} 通过 | 同上；不含其他上游工程 |
| 原项目静态检查 | 通过 | `run.ps1 -Action check` 实际结果 |
| 新模块语法、DOM ID/资源/导入契约 | 通过 | `build-manifest.json` |
| 最终常规浏览器检查 | {checks} 项通过，页面/请求错误 0 | `final-ui/browser-checks.json` |
| 补充界面验收 | {len(targeted['checks'])} 项通过 | `final-ui/targeted-checks.json` |
| 原 Studio 入口与 HTTP 资产集成 | {len(integration['checks'])} 个断言通过 | `integration-checks.json` |
| 实际 5 分钟自动结束 | 通过，验证等待 {targeted['trainingActualElapsedMs']/1000:.2f} 秒 | `final-ui/targeted-checks.json`；未加速时钟 |
| 30 分钟稳定性 | 通过，实际 {endurance['actualElapsedSeconds']:.2f} 秒 | `stability-result.json` |
| 静态资产构建 | 五个资产与 SHA-256 清单已生成 | `static-build/`、`build-manifest.json` |

现有项目没有 TypeScript / ESLint 工具链，本轮没有假称运行 tsc 或 ESLint。构建指原生静态资产校验和复制，不是 Webpack/Vite 打包。

### 30 分钟测试条件与读数

- 浏览器：无头 Microsoft Edge {browser['browser']}，1440×900，正常动态效果。
- 数据：固定种子 DEMO，4 通道 × 250 Hz 合成 EEG；真实墙钟连续运行，无测试时钟加速。
- 结束时已记录评分事件：{last['points']}；趋势缓冲 {last['trendLength']} 点；EEG 每通道 {last['bufferLengths']} 点，容量 {last['bufferCapacity']}。
- 拒收 {last['diagnostics']['rejected']}，重复/乱序 {last['diagnostics']['duplicate']}，页面错误 {len(endurance['runErrors'])}。
- 从第 1 分钟至结束的 Canvas 调度平均 {draw_rate:.2f} 次/秒；各分钟滚动窗口的绘制耗时 P95 最大 {worst_draw:.2f} ms，EEG 事件接收至开始绘制 P95 最大 {worst_arrival:.2f} ms。
- 显式 GC 后 JS heap：第 1 分钟 {first['heapUsed']/1048576:.3f} MiB，结束 {last['heapUsed']/1048576:.3f} MiB；DOM 节点 {first['nodes']} → {last['nodes']}。

这些是此浏览器与合成负载下的工程记录：Canvas 调度次数不等于物理屏幕 FPS，接收至绘制不等于设备端到端延迟。Heap 是每分钟 GC 后的快照，不是进程总内存或峰值；当前会话专注事件随录制增长是预期行为，存在显式上限。

长时间测试覆盖正常连续数据路径，使用 60Hz 绘制调度版本。测试期间另完善了文字、圆环标注、导航与视觉细节，并补强连接中/恢复时的评分质量门控与异常后的分级驻留时间；最终常规与补充浏览器检查及新增单元测试单独复核这些改动。因此不能把该长时间记录解释为对最终所有异常分支均进行 30 分钟运行，也不构成真实设备稳定性或识别准确率验证。

### 浏览器检查明细

{matrix}

补充最终界面检查：

{extra}

## 截图与操作证据

五种宽度均在最终界面上重新截图。下面的尺寸是测试视口；保存为整页截图，因此 PNG 高度可能大于视口高度：

- [1440×900 桌面](final-ui/dashboard-1440.png)
- [1920×1080 桌面](final-ui/dashboard-1920.png)
- [1280×800 桌面](final-ui/dashboard-1280.png)
- [768px 平板](final-ui/dashboard-768.png)
- [390px 移动](final-ui/dashboard-390.png)
- [滚动时持续可见的 DEMO](final-ui/source-badge-scrolled.png)
- [训练自动完成](final-ui/training-auto-complete.png)
- [40 秒操作动图](NeuroFocus-40s-DEMO.gif)：{len(recording['frames'])} 张真实操作帧，采集时长 {recording['actualElapsedMs']/1000:.2f}s；包括开始、暂停/继续、信号质量差、断连、恢复、沉浸视图与结束。原始时间记录在 `operation-recording.json`。FFmpeg 录像组件不可用，使用 PNG 序列生成 GIF，没有把静态伪造界面当作操作录像。
- `example-DEMO-session.json/.csv`：含无效段的实际浏览器导出样例。

已人工查看桌面与移动截图；修复了滚动截图中侧栏位置、隐藏跳转链接、小字可读性、频段单位换行以及圆环语义问题。

## 当前接入边界

LIVE / REPLAY 在此监测台均明确为未接入。现有 Studio 的 LSL 页面不受影响。尚需项目组确认硬件/SDK、协议、通道及单位、算法定义/范围/频率/校准、质量含义、时钟和失效窗口；接入字段见 `docs/NeuroFocus.md`。未实现真实 WS 适配和重连、文件回放、原始 EEG 录制、跨会话交互分析、持久化、3D、FFT/PSD 详细视图。

默认仅内存保存，最多 10 个会话，每会话最多 21600 个专注事件；原始 EEG 只有显示缓冲，不包含在会话导出中。刷新会清空数据。演示质量、阈值和频段能量不能当作真实科研或医学结果。

## 最终运行资产指纹

| 文件 | 字节 | SHA-256 |
| --- | --- | --- |
{file_rows}
"""
    (OUT / "验收记录.md").write_text(report, encoding="utf-8")
    print(OUT / "验收记录.md")


if __name__ == "__main__":
    main()
