"""逐窗分析引擎：把"取一窗数据 → 质检 → 指标 → 预警"串成一个可复用对象。

CLI / 桌面版把这段逻辑写在 `SessionRunner` 里；Studio 需要"每隔 2 秒推一次"的
流式推进，因此这里抽出一个只依赖引擎公开 API 的 `WindowEngine`，两侧口径一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ningsi import config
from ningsi.monitoring.alerts import AlertEngine
from ningsi.signal.baseline import build_baseline
from ningsi.signal.indicators import aggregate, compute_indicators
from ningsi.signal.window import analyze_window

INDICATOR_LABELS = {"focus": "专注度", "relax": "放松度", "load": "认知负荷"}


@dataclass
class WindowStep:
    """一窗的完整结果；score/dl 为空表示该窗不可用或未建基线。"""

    index: int
    t_end: float
    state: str
    window: object = None
    indicators: object = None
    scores: dict = field(default_factory=dict)
    alerts: list = field(default_factory=list)
    raw: object = None

    @property
    def usable(self) -> bool:
        return bool(self.window is not None and self.window.usable)

    @property
    def focus(self) -> float | None:
        return self.scores.get("focus")

    def payload(self, include_signal: bool = False, max_points: int = 200) -> dict:
        window = self.window
        quality = window.quality.as_dict() if window is not None else {}
        payload = {
            "index": self.index,
            "t_end": round(self.t_end, 3),
            "state": self.state,
            "usable": self.usable,
            "quality": quality,
            "scores": {key: round(value, 4) for key, value in self.scores.items()},
            "index_z": ({k: round(v, 4) for k, v in self.indicators.index_z.items()}
                        if self.indicators is not None and self.indicators.available else {}),
            "band_z": ({k: round(v, 4) for k, v in self.indicators.band_z.items()}
                       if self.indicators is not None and self.indicators.available else {}),
            "reasons": list(quality.get("reasons", [])),
            "alerts": self.alerts,
        }
        if include_signal and self.raw is not None:
            payload["signal"] = _trace(self.raw, self.window)
        return payload


def _trace(raw, window, max_points: int = 200) -> dict:
    """把原始波形与相对功率抽成前端可画的数组（<= max_points 点）。"""
    values: list[float] = []
    if raw is not None:
        import numpy as np
        data = np.atleast_2d(np.asarray(raw, dtype=float))[0]
        step = max(1, len(data) // max_points)
        values = [round(float(value), 2) for value in data[::step]][:max_points]
    relative = getattr(window, "rel", None) or {}
    return {
        "srate": round(float(getattr(window, "srate", 250.0)), 2),
        "channels": int(getattr(window, "channels", 1) or 1),
        "samples": values,
        "relative": {key: round(float(value), 4) for key, value in relative.items()},
        "time_domain": dict(getattr(window, "time_domain", {}) or {}),
    }


class WindowEngine:
    """按步长推进的窗口引擎，持有预警状态与基线。"""

    def __init__(self, source, srate: float, *, device: str, baseline=None,
                 alert_rules: dict | None = None) -> None:
        self.source = source
        self.srate = float(srate)
        self.device = device
        self.baseline = baseline
        self.alerts = AlertEngine(rules=alert_rules)
        self.index = 0
        self.t = 0.0
        self.fired_alerts: list[dict] = []

    # ------------------------------------------------------------------ 推进
    def step(self, state: str = "rest", *, seconds: float | None = None,
             artifact: bool = False) -> WindowStep:
        self.index += 1
        self.t += config.STEP_SEC
        raw = self.source.window(state, artifact=artifact)
        if not self._enough_data(raw):
            # 真实流刚接上/短暂断流：样本不足以算一个窗。
            # 这里绝不能把空数组丢给 analyze_window（会因"样本少于分段长度"直接抛错），
            # 而是产出一个"不可用窗"，让质量门控记录原因、指标与计时都跳过它。
            window = self._unusable_window(raw)
        else:
            window = analyze_window(raw, self.srate, t_end=self.t)
        indicators = None
        if window.usable and self.baseline is not None and self.baseline.valid:
            indicators = compute_indicators(window, self.baseline)
        events = self.alerts.feed(window, indicators)
        triggered = [event.as_dict() for event in events if event.state == "triggered"]
        self.fired_alerts.extend(triggered)
        return WindowStep(
            index=self.index, t_end=self.t, state=state, window=window,
            indicators=indicators,
            scores=dict(indicators.scores) if indicators is not None and indicators.available else {},
            alerts=triggered, raw=raw,
        )

    def _min_samples(self) -> int:
        """一个 4 秒窗至少要够 Welch 一个分段（segment_sec × srate）。"""
        segment = float(config.WELCH["segment_sec"])
        return max(1, int(round(segment * self.srate)))

    def _enough_data(self, raw) -> bool:
        import numpy as np
        if raw is None:
            return False
        data = np.atleast_2d(np.asarray(raw, dtype=float))
        return data.shape[1] >= self._min_samples()

    def _unusable_window(self, raw):
        import numpy as np
        from ningsi.signal.artifacts import Verdict
        from ningsi.signal.window import WindowFeatures
        data = np.atleast_2d(np.asarray(raw, dtype=float)) if raw is not None else np.zeros((1, 0))
        channels = int(data.shape[0]) or 1
        reason = "no_data" if data.shape[1] == 0 else "signal_too_short"
        return WindowFeatures(
            t_end=self.t,
            srate=self.srate,
            channels=channels,
            rel={},
            powers={},
            quality=Verdict(ok=False, reasons=(reason,), metrics={"samples": int(data.shape[1])}),
            spectrum={},
        )

    def run(self, state: str, count: int, *, artifact_every: int = 0) -> list[WindowStep]:
        steps = []
        for offset in range(count):
            artifact = artifact_every > 0 and (offset + 1) % artifact_every == 0
            steps.append(self.step(state, artifact=artifact))
        return steps

    # ---------------------------------------------------------------- 质检
    def device_qc(self, windows: int = 4, artifact: bool = True) -> dict:
        steps = [self.step("rest") for _ in range(int(windows))]
        if artifact:
            steps.append(self.step("rest", artifact=True))
        reasons: dict[str, int] = {}
        for step in steps:
            if step.window is None:
                continue
            for reason in step.window.quality.reasons:
                reasons[reason] = reasons.get(reason, 0) + 1
        usable = [step for step in steps if step.usable]
        return {
            "windows": len(steps),
            "usable": len(usable),
            "valid_ratio": round(len(usable) / len(steps), 4) if steps else 0.0,
            "reasons": reasons,
            "passed": len(usable) >= max(1, int(windows) - 1),
            "details": [step.payload() for step in steps],
        }

    # ---------------------------------------------------------------- 基线
    def build_baseline(self, state: str, seconds: float) -> tuple[object, list[WindowStep]]:
        count = max(1, int(round(float(seconds) / config.STEP_SEC)))
        steps = [self.step(state) for _ in range(count)]
        windows = [step.window for step in steps if step.window is not None]
        baseline = build_baseline(
            windows, device=self.device, srate=self.srate,
            min_windows=config.BASELINE_PROTOCOL["min_windows"],
        )
        self.baseline = baseline
        return baseline, steps

    # ---------------------------------------------------------------- 汇总
    @staticmethod
    def summarize(steps: list[WindowStep]) -> dict:
        windows = [step.window for step in steps if step.window is not None]
        results = [step.indicators for step in steps
                   if step.indicators is not None and step.indicators.available]
        return aggregate(windows, results)

    def spectrum_probe(self, seconds: float | None = None, *, max_points: int = 128,
                       channel: int = 0) -> dict:
        """给"实时频谱/频带"面板用的紧凑数据：同一套 welch-v1 口径，不在前端另写一套算法。

        返回 `{freqs, power_db, peaks, rel}`：
        - `freqs` / `power_db`：功率谱（dB），直接画曲线；
        - `peaks`：各频带的 dB 峰值（画频带条用）；
        - `rel`：各频带**相对功率**（与指标同口径，占比之和为 1）。
        数据不足时返回空结构，由前端显示"等待数据"而不是画一条假的曲线。
        """
        import numpy as np

        from ningsi.signal.spectrum import welch_psd

        duration = float(seconds or config.WINDOW_SEC)
        data = self.source.window("rest", seconds=duration)
        array = np.atleast_2d(np.asarray(data, dtype=float)) if data is not None else np.zeros((0, 0))
        if array.size == 0 or array.shape[1] < self._min_samples():
            return {"freqs": [], "power_db": [], "peaks": {}, "rel": {}, "usable": False}
        index = min(max(0, int(channel)), array.shape[0] - 1)
        try:
            spectrum = welch_psd(array[index], self.srate)
        except (ValueError, FloatingPointError):
            return {"freqs": [], "power_db": [], "peaks": {}, "rel": {}, "usable": False}

        freqs = np.asarray(spectrum.freqs, dtype=float)
        psd = np.asarray(spectrum.psd, dtype=float)
        power_db = 10.0 * np.log10(np.maximum(psd, 1e-12))
        # 抽稀到 max_points 以内（45 Hz 内约 90 个频点，通常不用抽）
        if freqs.size > max_points:
            step = int(np.ceil(freqs.size / max_points))
            freqs, power_db = freqs[::step], power_db[::step]

        raw_freqs = np.asarray(spectrum.freqs, dtype=float)
        raw_psd = np.asarray(spectrum.psd, dtype=float)
        band_peaks: dict[str, float] = {}
        band_power: dict[str, float] = {}
        for name, (low, high) in config.BANDS.items():
            mask = (raw_freqs >= low) & (raw_freqs <= high)
            if mask.any():
                band_peaks[name] = round(float(np.max(power_db[(freqs >= low) & (freqs <= high)])), 2) \
                    if ((freqs >= low) & (freqs <= high)).any() else None
                band_power[name] = float(raw_psd[mask].sum())
        band_peaks = {key: value for key, value in band_peaks.items() if value is not None}
        total = sum(band_power.values())
        rel = ({name: round(value / total, 4) for name, value in band_power.items()}
               if total > 0 else {})
        return {
            "freqs": [round(float(value), 2) for value in freqs],
            "power_db": [round(float(value), 2) for value in power_db],
            "peaks": band_peaks,
            "rel": rel,
            "segments": int(getattr(spectrum, "segments", 0) or 0),
            "delta_f_hz": round(float(freqs[1] - freqs[0]), 4) if freqs.size > 1 else 0.0,
            "usable": True,
        }

    def series(self, steps: list[WindowStep]) -> list[tuple[float, float | None]]:
        return [(step.t_end, step.focus) for step in steps]
