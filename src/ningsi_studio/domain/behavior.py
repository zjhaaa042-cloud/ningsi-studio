"""行为任务：SART 与 PVT-B 的序列持有、逐试次记录与计分。

序列由服务端生成（同一被试/会话/Run 确定性可复现），前端只负责呈现刺激与记录
按下时刻；计分统一走上游 `ningsi.behavior`，保证与 CLI/桌面版结果一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ningsi import config
from ningsi.behavior import pvt as pvt_module
from ningsi.behavior import sart as sart_module

SART_PRACTICE = sart_module.PRACTICE_TRIALS
SART_TRIALS = sart_module.TRIALS


@dataclass
class SartTask:
    participant: str
    session: str = "01"
    run: str = "001"
    sequence: object = None
    phase: str = "practice"                      # practice | main | done
    practice_index: int = 0
    trial_index: int = 0
    responded: list = field(default_factory=list)
    rts: list = field(default_factory=list)
    events: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.sequence is None:
            self.sequence = sart_module.build_sequence(self.participant, self.session, self.run)

    # ------------------------------------------------------------------ 序列
    def sequence_payload(self) -> dict:
        """只给刺激数字，不暴露 No-Go 位置（判定在服务端）。"""
        return {
            "task": "sart",
            "trials": self.sequence.trials,
            "nogo_trials": len(self.sequence.nogo_positions),
            "practice_trials": len(self.sequence.practice),
            "sequence_set_id": self.sequence.set_id,
            "seed": self.sequence.seed,
            "digits": list(self.sequence.digits),
            "practice": list(self.sequence.practice),
            "instruction": "看到 1–9 按空格；看到数字 3 不要按。练习 12 试次后进入 180 个正式试次。",
        }

    def next_trial(self) -> dict:
        if self.phase == "practice":
            if self.practice_index >= len(self.sequence.practice):
                self.phase = "main"
            else:
                digit = self.sequence.practice[self.practice_index]
                return {"phase": "practice", "index": self.practice_index,
                        "digit": int(digit), "total": len(self.sequence.practice)}
        if self.phase == "main":
            if self.trial_index >= self.sequence.trials:
                self.phase = "done"
                return {"phase": "done", "total": self.sequence.trials}
            digit = self.sequence.digits[self.trial_index]
            return {"phase": "main", "index": self.trial_index,
                    "digit": int(digit), "total": self.sequence.trials}
        return {"phase": "done", "total": self.sequence.trials}

    # ---------------------------------------------------------------- 逐试次
    def submit(self, *, phase: str, index: int, responded: bool, rt=None) -> dict:
        phase = phase or self.phase
        if phase == "practice":
            if index != self.practice_index:
                raise ValueError(f"练习试次顺序不一致：期望 {self.practice_index}，收到 {index}")
            expected = self.sequence.practice[index]
            correct = (int(expected) == sart_module.NOGO_DIGIT and not responded) or \
                      (int(expected) != sart_module.NOGO_DIGIT and responded)
            self.practice_index += 1
            if self.practice_index >= len(self.sequence.practice):
                self.phase = "main"
            self.events.append({"phase": "practice", "index": index, "digit": int(expected),
                                "responded": bool(responded), "rt": rt, "correct": bool(correct)})
            return {"accepted": True, "phase": "practice", "index": index, "correct": bool(correct),
                    "practice_remaining": max(0, len(self.sequence.practice) - self.practice_index),
                    "next": self.next_trial()}

        if index != self.trial_index:
            raise ValueError(f"正式试次顺序不一致：期望 {self.trial_index}，收到 {index}")
        self.responded.append(bool(responded))
        self.rts.append(None if rt is None else float(rt))
        self.events.append({"phase": "main", "index": index,
                            "digit": int(self.sequence.digits[index]),
                            "responded": bool(responded), "rt": rt})
        self.trial_index += 1
        if self.trial_index >= self.sequence.trials:
            self.phase = "done"
        return {"accepted": True, "phase": "main", "index": index,
                "progress": round(self.trial_index / self.sequence.trials, 4),
                "next": self.next_trial()}

    # ------------------------------------------------------------------ 计分
    def result(self) -> dict:
        if len(self.responded) < self.sequence.trials:
            raise ValueError(
                f"SART 尚未完成：已作答 {len(self.responded)}/{self.sequence.trials} 个正式试次")
        scored = sart_module.SartResult(
            self.participant, self.session, self.run, self.sequence,
            tuple(self.responded), tuple(self.rts),
            extra={"source": "studio-session"},
        ).score()
        scored["spec"] = config.LABEL_SPEC
        scored["practice"] = {"trials": len(self.sequence.practice),
                              "correct": sum(1 for item in self.events
                                             if item["phase"] == "practice" and item.get("correct"))}
        return scored

    def trials_payload(self) -> dict:
        return {"practice": [item for item in self.events if item["phase"] == "practice"],
                "main": [item for item in self.events if item["phase"] == "main"],
                "nogo_positions": list(self.sequence.nogo_positions),
                "sequence_set_id": self.sequence.set_id,
                "seed": self.sequence.seed}


@dataclass
class PvtTask:
    seed: int = 1
    duration_sec: float = pvt_module.DURATION_SEC
    onsets: list = field(default_factory=list)
    trials: list = field(default_factory=list)
    index: int = 0

    def __post_init__(self) -> None:
        if not self.onsets:
            self.onsets = pvt_module.build_schedule(self.seed, self.duration_sec)

    def sequence_payload(self) -> dict:
        return {
            "task": "pvt-b",
            "duration_sec": self.duration_sec,
            "trials": len(self.onsets),
            "onsets": list(self.onsets),
            "lapse_sec": pvt_module.LAPSE_SEC,
            "instruction": "屏幕出现计时器时尽快按空格；不要抢在计时器出现前按。",
        }

    def next_trial(self) -> dict:
        if self.index >= len(self.onsets):
            return {"phase": "done", "total": len(self.onsets)}
        return {"phase": "main", "index": self.index, "onset": self.onsets[self.index],
                "total": len(self.onsets)}

    def submit(self, *, index: int, responded: bool, rt=None, false_start: bool = False) -> dict:
        if index != self.index:
            raise ValueError(f"PVT 试次顺序不一致：期望 {self.index}，收到 {index}")
        self.trials.append(pvt_module.PvtTrial(
            onset=self.onsets[index],
            responded=bool(responded),
            rt=None if rt is None else float(rt),
            false_start=bool(false_start),
        ))
        self.index += 1
        return {"accepted": True, "index": index,
                "progress": round(self.index / max(1, len(self.onsets)), 4),
                "next": self.next_trial()}

    def result(self) -> dict:
        result = pvt_module.PvtResult(trials=list(self.trials), duration_sec=self.duration_sec)
        scored = result.score()
        scored["spec"] = config.LABEL_SPEC
        scored["source"] = "studio-session"
        return scored

    def trials_payload(self) -> dict:
        return {"onsets": list(self.onsets),
                "trials": [{"onset": item.onset, "responded": item.responded,
                            "rt": item.rt, "false_start": item.false_start}
                           for item in self.trials]}
