"""数据源选择：优先真实硬件（LSL），不可用时退回仿真源并明确标注。

赛题要求真实脑电采集，因此真实链路的代码路径始终存在且优先；
"当前是仿真"这个事实必须如实出现在会话的 source 字段、SSE 事件与报告里，不做静默替换。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from ningsi import config
from ningsi.acquisition.simulate import SyntheticEEG

SIM_SOURCE = "sim-bsense"


@dataclass
class SourceInfo:
    key: str
    kind: str            # lsl | sim
    srate: float
    channels: int
    device: str
    note: str
    engine: object

    @property
    def is_real(self) -> bool:
        return self.kind == "lsl"

    def as_dict(self) -> dict:
        return {"key": self.key, "kind": self.kind, "srate": self.srate,
                "channels": self.channels, "device": self.device, "note": self.note,
                "real": self.is_real}


def list_available(probe_seconds: float = 2.0) -> list[dict]:
    """列出可用数据源：永远含仿真源；装了 pylsl 时再扫描真实 LSL 流。"""
    note = "仿真脑电源：无设备联调、演示与自动化测试使用"
    sources = [SourceInfo(SIM_SOURCE, "sim", 250.0, 1, SIM_SOURCE, note, None).as_dict()]

    try:
        from ningsi_studio.acquisition import lsl as lsl_module
    except Exception as error:                       # noqa: BLE001 - 导入失败也要能列出仿真源
        sources[0]["hardware_note"] = f"LSL 模块不可用：{error}"
        return sources

    try:
        found = lsl_module.probe_streams(timeout=max(0.2, min(5.0, probe_seconds)))
    except lsl_module.LslUnavailable as error:
        sources[0]["hardware_note"] = str(error)
        return sources
    except Exception as error:                       # noqa: BLE001 - 扫描失败不应让接口失败
        sources[0]["hardware_note"] = f"LSL 流扫描失败：{error}"
        return sources

    for item in found:
        if not item.get("supported") or not item.get("kind"):
            sources.append({
                **item,
                "key": f"unsupported:{item.get('name', '')}",
                "device": item.get("name", ""),
                "note": item.get("label", "不支持的流类型"),
                "real": False,
            })
            continue
        name = item["name"]
        sources.append({
            "key": f"lsl:{name}",
            "kind": "lsl",
            "srate": float(item.get("nominal_srate") or 0.0) or 250.0,
            "channels": int(item.get("channel_count") or 0) or 1,
            "device": name,
            "note": (f"真实 LSL 流：{name}"
                     f"（{item.get('channel_count')} 通道，{float(item.get('nominal_srate') or 0):.0f} Hz）"),
            "real": True,
            "stream_type": item.get("stream_type"),
            "channel_labels": item.get("channel_labels", []),
            "source_id": item.get("source_id", ""),
        })

    if len(sources) == 1:
        sources[0]["hardware_note"] = (
            "未发现 LSL 流。请先启动采集端（如 BioMultiLite / BSense-R），"
            "或先用内置仿真流验证链路：python -m ningsi_studio simulate-outlet"
        )
    return sources


class ManagedLslSource:
    """常驻缓冲的 LSL 数据源：`window()` 从缓冲切片，不会因为设备停顿而卡住采集线程。

    - `start()` 立刻返回是否就绪；未就绪时 `window()` 返回空窗（0 列），
      上层质量门控会把它判为不可用窗并记录原因，而不是让会话挂死；
    - 设备插拔由 LiveStreamManager 自动重连，`status()`/`errors()` 暴露细节给界面。
    """

    def __init__(self, stream_name: str, *, device: str, srate: float = 250.0,
                 channels: int = 1, buffer_seconds: float = 60.0,
                 ready_timeout: float = 15.0, fill_timeout: float = 2.0,
                 stale_timeout: float = 3.0) -> None:
        from ningsi_studio.acquisition.lsl import LiveStreamManager

        self.device = device
        self.stream_name = stream_name
        self._manager = LiveStreamManager(buffer_seconds=buffer_seconds, wanted_kinds=("eeg",))
        self._ready = False
        self._ready_timeout = float(ready_timeout)
        self.fill_timeout = float(fill_timeout)
        self.stale_timeout = float(stale_timeout)
        self._srate = float(srate)
        self._channels = int(channels)

    # ------------------------------------------------------------------ 生命周期
    def start(self, *, wait: bool = True) -> bool:
        try:
            self._manager.start()
        except Exception as error:                   # noqa: BLE001
            raise RuntimeError(f"启动 LSL 采集失败：{error}") from error
        if wait:
            self._ready = self._manager.wait_for("eeg", timeout=self._ready_timeout, min_samples=1)
        else:
            self._ready = False
        if self._ready:
            descriptor = self._manager.descriptor("eeg")
            if descriptor is not None:
                self._srate = descriptor.nominal_srate or self._srate
                self._channels = descriptor.channel_count or self._channels
        return self._ready

    def stop(self) -> None:
        self._manager.stop()

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def srate(self) -> float:
        descriptor = self._manager.descriptor("eeg")
        return float(descriptor.nominal_srate) if descriptor and descriptor.nominal_srate else self._srate

    @property
    def channels(self) -> int:
        descriptor = self._manager.descriptor("eeg")
        return int(descriptor.channel_count) if descriptor else self._channels

    def descriptor_dict(self) -> dict:
        descriptor = self._manager.descriptor("eeg")
        return descriptor.as_dict() if descriptor is not None else {}

    def status(self) -> dict:
        return self._manager.status()

    def errors(self) -> dict:
        return self._manager.errors()

    # ------------------------------------------------------------------ 取数
    def window(self, state: str = "rest", seconds: float | None = None, artifact: bool = False):
        """返回 (通道, 采样点)。

        两条真实设备特有的规则：
        - **有限等待**：刚连上时缓冲还没攒满一窗，最多等 `fill_timeout` 秒；
        - **新鲜度检查**：如果最近一次收到样本已经过去超过 `stale_timeout` 秒（设备停了、
          线掉了、采集端崩了），就返回空窗，让上层把这一窗判为不可用。
          否则会一直拿缓冲里的旧数据当"新数据"算指标，指标看着正常其实是冻结的。
        """
        duration = float(seconds or config.WINDOW_SEC)
        expected = int(round(duration * self.srate))
        deadline = time.monotonic() + self.fill_timeout
        while True:
            stats = self._manager.status().get("streams", {}).get("eeg", {})
            age = stats.get("seconds_since_last")
            fresh = age is None or age <= self.stale_timeout
            data = self._manager.samples("eeg", duration)
            if data.size and fresh and data.shape[1] >= expected * 0.5:
                return data
            if time.monotonic() >= deadline:
                if not fresh:
                    return np.zeros((self.channels, 0), dtype=float)   # 明确判为不可用
                return data if data.size else np.zeros((self.channels, 0), dtype=float)
            time.sleep(0.1)


def build_source(device: str = SIM_SOURCE, *, channels: int = 1, srate: float = 250.0,
                 seed: int = 7, ready_timeout: float = 15.0) -> SourceInfo:
    """按设备名构造数据源；真实设备不可用时抛错，由调用方决定是否降级。"""
    if device and device.startswith("lsl:"):
        name = device.split(":", 1)[1]
        source = ManagedLslSource(name, device=name, srate=srate, channels=channels,
                                  ready_timeout=ready_timeout)
        if not source.start():
            status = source.status()
            source.stop()
            detail = status.get("errors") or "缓冲里还没有样本"
            raise RuntimeError(
                f"已连接 LSL 流 {name} 但 {ready_timeout:.0f} 秒内没有收到样本（{detail}）")
        descriptor = source.descriptor_dict()
        return SourceInfo(
            key=device, kind="lsl", srate=source.srate, channels=source.channels,
            device=name,
            note=(f"真实 LSL 流：{name}（{descriptor.get('channel_count')} 通道，"
                  f"{source.srate:.0f} Hz，标签 {descriptor.get('channel_labels')}）"),
            engine=source,
        )

    engine = SyntheticEEG(srate=srate, channels=channels, seed=seed)
    return SourceInfo(
        key=SIM_SOURCE, kind="sim", srate=float(srate), channels=int(channels),
        device=SIM_SOURCE, note="仿真脑电源：数据来源已在界面与报告中标注", engine=engine,
    )
