"""神经反馈训练：目标自适应与训练前后同口径基线对比（复用上游训练闭环）。"""

from __future__ import annotations

from ningsi import config
from ningsi.signal.indicators import compute_indicators
from ningsi.signal.window import analyze_window
from ningsi.training.neurofeedback import NeurofeedbackSession, initial_target


class StudioTraining:
    """包一层训练会话：把逐窗评分喂进去，并在结束时给出训练前后对比。"""

    def __init__(self, participant: str, session: str, run: str, baseline, mode: str = "quick") -> None:
        target, rationale = initial_target(baseline)
        self.inner = NeurofeedbackSession(participant, session, run, mode=mode,
                                          target=target, rationale=rationale)
        self.baseline_before = baseline
        self.plan = self.inner.plan()
        self.segment_index = 0
        self.samples: list[tuple[float, float]] = []
        self.excluded = 0

    # ------------------------------------------------------------------ 信息
    def describe(self) -> dict:
        return {
            "mode": self.inner.mode,
            "segments": self.plan["segments"],
            "segment_sec": self.plan["segment_sec"],
            "target": round(float(self.inner.target), 4),
            "rationale": self.inner.rationale,
            "initial_from": config.TRAINING["initial_from"],
            "target_step": config.TRAINING["target_step"],
            "hold_sec": config.TRAINING["hold_sec"],
        }

    @property
    def target(self) -> float:
        return round(float(self.inner.target), 4)

    def feed(self, score: float | None, t_sec: float, usable: bool = True) -> None:
        if score is None or not usable:
            self.excluded += 1
            return
        self.samples.append((float(t_sec), float(score)))

    # ---------------------------------------------------------------- 分段
    def close_segment(self) -> dict:
        target_before = round(float(self.inner.target), 4)
        hold_before = round(float(self.inner.hold_sec), 4)
        segment = self.inner.add_segment(
            list(self.samples), self.plan["segment_sec"], excluded_windows=self.excluded
        )
        stats = segment.stats()
        payload = {
            "seq": self.segment_index,
            "index": segment.index,
            "target": target_before,
            "target_after": round(float(self.inner.target), 4),
            "hold_sec": hold_before,
            "hold_after": round(float(self.inner.hold_sec), 4),
            "duration_sec": segment.duration_sec,
            "excluded_windows": segment.excluded_windows,
            "samples": [[round(float(t), 3), round(float(v), 4)] for t, v in segment.samples],
            "stats": stats,
            "mean_score": stats.get("mean"),
            "on_target_ratio": stats.get("on_target_ratio"),
            "volatility": stats.get("volatility"),
            "n": stats.get("n"),
            "achieved": stats.get("achieved"),
        }
        self.segment_index += 1
        self.samples = []
        self.excluded = 0
        return payload

    def summary(self, window_engine, seconds_after: float = 30.0) -> dict:
        """训练后按同口径再采一小段基线，给出训练前后对比。"""
        _, steps = window_engine.build_baseline("rest", seconds_after)
        baseline_after = window_engine.baseline
        return self.inner.summary(self.baseline_before, baseline_after)


def score_window(window, baseline):
    """给单窗算指标（训练/监测共用）。"""
    if window is None or not window.usable or baseline is None or not baseline.valid:
        return None
    return compute_indicators(window, baseline)


def analyze(state_source, srate: float, state: str, t_end: float):
    return analyze_window(state_source.window(state), srate, t_end=t_end)
