"""会话阶段定义：顺序、中文名、进度权重、是否需交互，以及"这一步在干什么"。

设计参考了参考工程（bsense-suite）的协议定义方式：每个步骤都同时给出
**给被试看的动作指令**、**给操作者的细节**、**预计时长**和**怎么推进**
（自动推进 / 需要被试作答 / 需要操作者确认）。会话流程页据此渲染
"当前该做什么"面板，而不是只丢一个阶段名让人猜。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 推进方式：与参考工程 Step.advance 的 timed / operator / form 对应
ADVANCE_AUTO = "auto"          # 后端按时间自动推进（被试只需保持状态）
ADVANCE_SUBJECT = "subject"    # 需要被试作答（量表 / SART / PVT）
ADVANCE_OPERATOR = "operator"  # 需要操作者确认（例如戴好设备后再开始）


@dataclass(frozen=True)
class Phase:
    key: str
    label: str
    weight: float
    interactive: bool = False
    description: str = ""
    # ---- 面向被试的引导（会话流程页的"当前该做什么"直接用这些字段）----
    headline: str = ""                                          # 一句话：现在请你做什么
    details: tuple[str, ...] = field(default_factory=tuple)      # 补充要点（逐条显示）
    duration_sec: float = 0.0                                    # 标准时长（真实节奏，秒）
    advance: str = ADVANCE_AUTO
    next_hint: str = ""                                          # 这一步之后会发生什么
    auto_note: str = ""                                          # 快速演示模式下会怎么跑

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "interactive": self.interactive,
            "weight": self.weight,
            "description": self.description,
            "headline": self.headline,
            "details": list(self.details),
            "duration_sec": self.duration_sec,
            "advance": self.advance,
            "next_hint": self.next_hint,
            "auto_note": self.auto_note,
        }


PHASES: tuple[Phase, ...] = (
    Phase(
        "qc", "设备质检", 0.04, False,
        "4 个静息窗 + 1 个伪迹窗，逐窗质检并统计不可用原因",
        headline="先坐好别动，正在检查信号质量",
        details=(
            "保持自然坐姿、双手放松，尽量不说话、不吞咽大动作",
            "这一阶段只判断信号能不能用，不出指标",
        ),
        duration_sec=24.0,
        next_hint="信号可用窗比例达标就进入静息基线采集",
        auto_note="质检由服务端自动完成，只需等待",
    ),
    Phase(
        "baseline_open", "睁眼基线", 0.10, False,
        "睁眼静息 2 分钟，4 秒窗 / 2 秒步长，独立建基线",
        headline="保持睁眼看着前方，安静坐 2 分钟",
        details=(
            "不要刻意盯着某个点，也不需要刻意放松",
            "避免说话、咀嚼、频繁眨眼或转头",
            "这一段的平均值将作为你个人的比较基准",
        ),
        duration_sec=120.0,
        next_hint="紧接着换闭眼基线，两个基线各自独立统计",
        auto_note="由服务端自动推进，2 分钟内不要起身",
    ),
    Phase(
        "baseline_closed", "闭眼基线", 0.10, False,
        "闭眼静息 2 分钟，与睁眼各自独立统计",
        headline="闭上眼睛，安静坐 2 分钟",
        details=(
            "保持清醒，不要睡着；困了可以轻微活动一下手指",
            "闭眼阶段容易出 α 波，这一条用于和睁眼对比",
        ),
        duration_sec=120.0,
        next_hint="基线完成后开始填量表，需要你逐题作答",
        auto_note="由服务端自动推进",
    ),
    Phase(
        "scales", "量表填写", 0.06, True,
        "SAS / SDS 各 20 题，含反向题，粗分×1.25",
        headline="按最近一周的实际感受，逐题作答",
        details=(
            "共 40 题（焦虑 SAS 20 题、抑郁 SDS 20 题）",
            "按第一感觉选，不要反复权衡；结果只作提示，不作诊断",
            "全部答完后点“提交”，未答完无法提交",
        ),
        duration_sec=300.0,
        advance=ADVANCE_SUBJECT,
        next_hint="量表提交后开始持续注意任务（SART）",
        auto_note="快速演示模式：服务端自动生成确定性作答，无需手动点选",
    ),
    Phase(
        "sart", "SART 持续注意", 0.20, True,
        "12 练习 + 180 正式试次，20 个 No-Go",
        headline="看到数字就按空格，看到“3”不要按",
        details=(
            "12 个练习试次不计分，用来熟悉节奏",
            "正式 180 试次中有 20 个是 3（抑制反应）",
            "尽量又快又准；按键有反应时记录",
        ),
        duration_sec=300.0,
        advance=ADVANCE_SUBJECT,
        next_hint="SART 之后是 3 分钟警觉度任务（PVT）",
        auto_note="快速演示模式：服务端自动作答，练习与正式都不需要按键",
    ),
    Phase(
        "pvt", "PVT-B 警觉度", 0.10, True,
        "3 分钟，慢反应率与中位反应时",
        headline="出现红点就尽快按空格",
        details=(
            "测试持续 3 分钟，中间会有长短不一的等待",
            "注意力放在红点上，感觉要打盹就提醒自己",
            "过早按键会被记为抢答，不计入有效反应",
        ),
        duration_sec=180.0,
        advance=ADVANCE_SUBJECT,
        next_hint="警觉度结束后进入任务态监测（会推送实时波形）",
        auto_note="快速演示模式：服务端自动作答",
    ),
    Phase(
        "monitor", "任务态监测", 0.14, False,
        "rest/focused/drowsy 共 35 窗，逐窗指标与预警",
        headline="稍等，正在模拟三种状态并逐窗算指标",
        details=(
            "界面会实时画出脑电波形与三指标曲线",
            "灰色格表示那一窗信号质量不合格，已排除、不计入指标",
            "低于阈值并持续一定时间会触发预警",
        ),
        duration_sec=70.0,
        next_hint="监测出基线水平后进入神经反馈训练",
        auto_note="由服务端自动推进；可在“实时监测”页看实时波形",
    ),
    Phase(
        "training", "神经反馈训练", 0.16, False,
        "专注度驱动反馈，目标按表现自适应（步长 0.05）",
        headline="尽量让专注度稳定在目标线以上",
        details=(
            "目标线会根据你的表现自动调整（高一点或低一点）",
            "达标需要保持一段时间，不是一下冲高就算",
            "分若干段进行，每段结束会给出该段达标比例",
        ),
        duration_sec=240.0,
        next_hint="训练结束后进入联合评估与模型训练",
        auto_note="由服务端自动推进；训练视图可看达标情况",
    ),
    Phase(
        "assessment", "联合评估", 0.04, False,
        "三类证据一致性判定与建议生成",
        headline="正在汇总脑电、量表与行为三类证据",
        details=(
            "三类证据一致时结论更稳；不一致会提示复测",
            "结论仅用于研究与自我调节参考，不构成医疗诊断",
        ),
        duration_sec=5.0,
        next_hint="评估后训练分类模型并生成报告",
        auto_note="由服务端自动完成",
    ),
    Phase(
        "model", "模型训练", 0.03, False,
        "六维频带特征 + 被试级 6:2:2 划分 + AUC",
        headline="正在训练个体化分类模型",
        details=("用本次会话的频带特征训练，产出模型文件与 AUC",),
        duration_sec=5.0,
        next_hint="产出报告、热力图、趋势与模型文件",
        auto_note="由服务端自动完成",
    ),
    Phase(
        "report", "报告与产物", 0.03, False,
        "Markdown / JSON / 热力图 / 趋势 / 模型文件",
        headline="正在生成报告与可下载产物",
        details=(
            "完成后可在“评估报告”页查看结论与证据表",
            "可打包下载全部产物（含 sha256 清单）",
        ),
        duration_sec=5.0,
        next_hint="会话结束",
        auto_note="由服务端自动完成",
    ),
)

PHASE_KEYS = tuple(phase.key for phase in PHASES)
PHASE_BY_KEY = {phase.key: phase for phase in PHASES}


def phase_index(key: str) -> int:
    try:
        return PHASE_KEYS.index(key)
    except ValueError:
        return -1


def progress_for(key: str, within: float = 0.0) -> float:
    """把"当前阶段 + 阶段内完成度"折算成 0–1 总进度。"""
    total = sum(phase.weight for phase in PHASES) or 1.0
    done = 0.0
    for phase in PHASES:
        if phase.key == key:
            done += phase.weight * max(0.0, min(1.0, within))
            break
        done += phase.weight
    return round(done / total, 4)


def as_list() -> list[dict]:
    return [phase.as_dict() for phase in PHASES]
