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
from ningsi.signal.preprocess import preprocess as run_preprocess

SIM_SOURCE = "sim-bsense"

# 真机信号调理口径（只作用于真实设备；仿真源按原样出数，一切仿真数值不变）。
# 文档 8.2 的零相位处理链（0.5 Hz 去漂移 → 50/60 Hz 陷波 → 45 Hz 低通）此前只被它自己的
# 单元测试引用，实时链路把**未调理**的原始值直接送进质检与频谱，于是设备的直流偏置
# （实测均值 -2.3e5 µV，见 `_analysis/lsl-hardware/units.json`）让每一窗都判为
# amplitude / channel_span 越界，可用窗比例恒为 0（`lsl-hardware/ab2.json` 的 A 组）。
CONDITION_SPEC = "acq-condition-v1"
DC_REMOVAL = "per_channel_dc_removal"

#: 内置仿真 outlet 的 source_id（`acquisition/sim_outlet.py`）。它走的是真实 LSL 传输，
#: 但数据是算出来的，界面与报告必须写清楚"这不是真实设备"，否则演示时会误导。
SIM_OUTLET_SOURCE_ID = "ningsi-sim-outlet-v1"


def _is_sim_outlet(source_id) -> bool:
    return str(source_id or "").strip() == SIM_OUTLET_SOURCE_ID


def _lsl_note(descriptor: dict, name: str, srate: float) -> str:
    """真实设备与内置仿真 outlet 的说明文案分开写（后者必须显式标注是仿真）。"""
    head = (f"内置仿真 LSL 流（非真实设备，source_id={SIM_OUTLET_SOURCE_ID}）"
            if _is_sim_outlet(descriptor.get("source_id")) else "真实 LSL 流")
    return (f"{head}：{name}（{descriptor.get('channel_count')} 通道，"
            f"{srate:.0f} Hz，标签 {descriptor.get('channel_labels')}）；"
            f"信号已按 {CONDITION_SPEC} 调理：逐通道去直流 → 0.5 Hz 去漂移 → "
            f"50/60 Hz 陷波 → 45 Hz 低通（零相位）")


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


def default_source_info() -> dict:
    """新会话会用的默认数据源（不扫 LSL，供 /api/health、/api/overview 与顶栏用）。

    为什么需要：建会话时 `device` 缺省就是 `sim-bsense`，但界面在"尚未选择会话"时
    顶栏只能显示 `数据来源：—`、`设备：—`，看起来像坏了；这里给出默认源，
    让"新会话将使用仿真源"这件事在首页就说清楚（仿真必须显式标注）。
    """
    return SourceInfo(SIM_SOURCE, "sim", 250.0, 1, SIM_SOURCE,
                      "仿真脑电源：无设备联调、演示与自动化测试使用", None).as_dict()


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
            "note": _lsl_note(item, name, float(item.get("nominal_srate") or 0.0) or 250.0),
            "real": True,
            "simulated": _is_sim_outlet(item.get("source_id")),
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
    - **真机调理**：`window()` 返回的每一窗都先做"逐通道去直流 → 文档 8.2 四级链"，
      见 `_condition()`；调理记录随 `status()["conditioning"]` 与报告一起留痕。
    """

    #: 调理上下文长度（秒）。去漂移高通是 0.5 Hz，只滤 4 秒窗会让零相位镜像填充的
    #: 边缘效应吃掉大半个窗；因此多取一段历史一起滤、再截取最后一窗。上限同时也是
    #: 显示通道的跨度上限（纯 Python 双二阶的成本与时长线性相关）。
    condition_seconds = 6.0

    def __init__(self, stream_name: str, *, device: str, srate: float = 250.0,
                 channels: int = 1, buffer_seconds: float = 60.0,
                 ready_timeout: float = 15.0, fill_timeout: float = 2.0,
                 stale_timeout: float = 3.0) -> None:
        from ningsi_studio.acquisition.lsl import LiveStreamManager

        self.device = device
        self.stream_name = stream_name
        self._manager = LiveStreamManager(buffer_seconds=buffer_seconds, wanted_kinds=("eeg",),
                                          wanted_name=stream_name)
        self._ready = False
        self._ready_timeout = float(ready_timeout)
        self.fill_timeout = float(fill_timeout)
        self.stale_timeout = float(stale_timeout)
        self._buffer_seconds = float(buffer_seconds)
        self._srate = float(srate)
        self._channels = int(channels)
        self._last_condition: dict = {}


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

    def conditioning(self) -> dict:
        """最近一窗实际用到的调理口径（供报告与排查留痕）。"""
        return dict(self._last_condition)

    def status(self) -> dict:
        payload = self._manager.status()
        payload["conditioning"] = self.conditioning()
        return payload

    def errors(self) -> dict:
        return self._manager.errors()

    # ------------------------------------------------------------------ 调理
    def _condition(self, data, expected: int, observed_srate: float | None = None):
        """真机调理：逐通道去直流 → 文档 8.2 零相位处理链 → 截取最后一窗。

        为什么必须先去直流：设备把带几百 mV 电极偏置的原始值直接推出来，而 0.5 Hz
        高通的零相位实现（镜像填充 + 正反各滤一次）在这么大的阶跃上会产生远大于信号的
        启动瞬态——实测"只滤波"反而把 peak 从 2.5e5 压到 3.9e4 µV、eog 相对功率升到 1.0
        （`_analysis/lsl-hardware/ab2.json` 的 B 组），比不滤更糟。先扣均值再滤波，
        同一段数据的 peak 落到 44~53 µV、可用（同文件的 E/F 组）。
        """
        x = np.atleast_2d(np.asarray(data, dtype=float))
        if x.size == 0:
            return x
        x = x - x.mean(axis=1, keepdims=True)
        filtered = []
        log = None
        for channel in x:
            out, log = run_preprocess(channel, self.srate)
            filtered.append(out)
        y = np.vstack(filtered)
        if expected > 0 and y.shape[1] > expected:
            # 真机时间戳会漂（实测同一"4 秒窗"拿到 2.9~6.8 秒的数据，见
            # _analysis/lsl-hardware/window_geometry.json），因此按点数兜底截到一窗。
            y = y[:, -expected:]
        self._last_condition = {
            "spec": CONDITION_SPEC,
            "dc_removal": DC_REMOVAL,
            "chain": log.as_dict() if log is not None else {},
            "srate": round(float(self.srate), 3),
            # 声明采样率与实际投递速率不一致时，频率轴会按声明值解释：这里把实测值一并留痕，
            # 现场可以据此判断"频带是否可信"（本设备实测 248.5 Hz / 声明 250 Hz，偏差 0.6%）。
            "observed_srate": (None if observed_srate is None else round(float(observed_srate), 3)),
            "context_samples": int(x.shape[1]),
            "window_samples": int(y.shape[1]),
            "expected_samples": int(expected),
        }
        return y

    # ------------------------------------------------------------------ 取数
    def window(self, state: str = "rest", seconds: float | None = None, artifact: bool = False,
               wait: bool = True):
        """返回 (通道, 采样点)。

        三条真实设备特有的规则：
        - **有限等待**：刚连上时缓冲还没攒满一窗，最多等 `fill_timeout` 秒
          （显示通道用 `wait=False` 关掉，避免 10 FPS 的推帧被拖住）；
        - **新鲜度检查**：如果最近一次收到样本已经过去超过 `stale_timeout` 秒（设备停了、
          线掉了、采集端崩了），就返回空窗，让上层把这一窗判为不可用。
          否则会一直拿缓冲里的旧数据当"新数据"算指标，指标看着正常其实是冻结的。
        - **调理**：返回值一律经过 `_condition()`，质检与频谱不会再看到未去直流的原始值；
        - **按点数取窗**：窗口长度用样点数（`seconds × 声明采样率`）兑现，而不是用 LSL 时间戳
          切片——本设备的时间戳会漂，同一"4 秒窗"按时间戳能拿到 2.7~6.8 秒的数据
          （见 `_analysis/lsl-hardware/window_geometry.json`），窗长失真会连带 Welch 分辨率失真。
        """
        duration = float(seconds or config.WINDOW_SEC)
        expected = int(round(duration * self.srate))
        context = max(duration, min(self.condition_seconds, self._buffer_seconds))
        wanted = int(round(context * self.srate))
        deadline = time.monotonic() + (self.fill_timeout if wait else 0.0)
        while True:
            stats = self._manager.status().get("streams", {}).get("eeg", {})
            age = stats.get("seconds_since_last")
            fresh = age is None or age <= self.stale_timeout
            buffer = self._manager.buffer("eeg")
            data = (buffer.samples_count(wanted) if buffer is not None
                    else np.zeros((self.channels, 0), dtype=float))
            # 就绪判据只看"最后一窗"那部分，与调理前完全一致（多取的历史不参与判据）。
            window_part = data[:, -expected:] if (expected > 0 and data.shape[1] > expected) else data
            if data.size and fresh and window_part.shape[1] >= expected * 0.5:
                return self._condition(data, expected, stats.get("observed_srate"))
            if time.monotonic() >= deadline:
                if not fresh:
                    return np.zeros((self.channels, 0), dtype=float)   # 明确判为不可用
                if not data.size:
                    return np.zeros((self.channels, 0), dtype=float)
                return self._condition(data, expected, stats.get("observed_srate"))
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
                f"已连接 LSL 流 {name} 但 {ready_timeout:.0f} 秒内没有收到样本（{detail}）；"
                f"请确认该名称的流正在推送（GET /api/devices 可列出当前可见的流）")
        descriptor = source.descriptor_dict()
        return SourceInfo(
            key=device, kind="lsl", srate=source.srate, channels=source.channels,
            device=name,
            note=_lsl_note(descriptor, name, source.srate),
            engine=source,
        )

    engine = SyntheticEEG(srate=srate, channels=channels, seed=seed)
    return SourceInfo(
        key=SIM_SOURCE, kind="sim", srate=float(srate), channels=int(channels),
        device=SIM_SOURCE, note="仿真脑电源：数据来源已在界面与报告中标注", engine=engine,
    )
