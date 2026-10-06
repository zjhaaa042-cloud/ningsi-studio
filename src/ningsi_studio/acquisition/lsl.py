"""LSL 采集层：把任意厂商的 LSL 数据流变成"随时可切片"的缓冲窗口。

与参考工程（bsense-suite）相同的做法，解决三个真实问题：

1. **不能按需阻塞拉取**：设备一旦不推数据，按需 `pull_chunk(timeout=…)` 会把调用方
   （信号处理链/会话线程）拖住。这里改成常驻线程持续拉取，`window()` 只做切片，永不阻塞。
2. **厂商拼写不统一**：流 type/name 可能是 `EEG`、`eeg`、`BioMultiLite-EEG`…，
   统一归一到 `eeg / fnirs / motion / metric / heart_rate / general_metric`。
3. **插拔/断线要能自愈**：发现线程每 2 秒重扫一次，inlet 断了会自动重连，
   并把错误暴露给上层（而不是静默丢数据）。

对外只需要 `LiveStreamManager`：`start()` → `window("eeg", 4.0)` → 拿到 `(通道, 采样点)` 数组。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import numpy as np

LOGGER = logging.getLogger("ningsi.lsl")

SUPPORTED_KINDS = ("eeg", "fnirs", "motion", "metric", "heart_rate", "general_metric")

KIND_LABELS = {
    "eeg": "EEG",
    "fnirs": "fNIRS",
    "motion": "Motion",
    "metric": "Metric",
    "heart_rate": "Heart Rate",
    "general_metric": "General Metric",
}

# 常见厂商/型号的 spelling 变体 → 统一 kind
_KIND_ALIASES = (
    ("general_metric", {"generalmetric", "generalmetrics"}),
    ("heart_rate", {"heartrate", "hr"}),
    ("fnirs", {"fnirs", "nir", "nirs", "ir"}),
    ("motion", {"motion", "imu", "accelerometer", "accel"}),
    ("metric", {"metric", "metrics"}),
    ("eeg", {"eeg"}),
)


class LslUnavailable(RuntimeError):
    """pylsl 未安装、未发现可用流，或流已断开。"""


def _normalize(text: str) -> str:
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def canonical_kind(stream_type: str, stream_name: str = "") -> str | None:
    """把厂商的 type/name 归一到稳定的数据种类；未知则返回 None。"""
    candidates = {_normalize(stream_type), _normalize(stream_name)}
    for kind, aliases in _KIND_ALIASES:
        if candidates & aliases:
            return kind
    return None


def _safe(info: Any, method: str, default: Any) -> Any:
    try:
        return getattr(info, method)()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return default


@dataclass(frozen=True)
class StreamDescriptor:
    """一个 LSL 流的稳定元信息（前端与报告都用它描述"数据从哪来"）。"""

    kind: str
    name: str
    stream_type: str
    channel_count: int
    nominal_srate: float
    channel_labels: tuple = ()
    source_id: str = ""
    hostname: str = ""
    uid: str = ""

    @property
    def label(self) -> str:
        return f"{self.name}（{self.channel_count}ch @ {self.nominal_srate:.0f}Hz）"

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "stream_type": self.stream_type,
            "channel_count": self.channel_count,
            "nominal_srate": self.nominal_srate,
            "channel_labels": list(self.channel_labels),
            "source_id": self.source_id,
            "hostname": self.hostname,
            "label": self.label,
        }


def _fallback_labels(kind: str, count: int) -> tuple:
    preset = {
        "eeg": ("Fp1", "Fp2", "F3", "F4", "C3", "C4", "O1", "O2"),
        "motion": ("Accel X", "Accel Y", "Accel Z", "Gyro X", "Gyro Y", "Gyro Z"),
        "heart_rate": ("Heart Rate",),
        "metric": ("Metric 1", "Metric 2"),
    }.get(kind, ())
    return tuple(preset[index] if index < len(preset) else f"Ch{index + 1}" for index in range(count))


def _channel_labels(info: Any, kind: str, count: int) -> tuple:
    labels: list = []
    try:
        channel = info.desc().child("channels").child("channel")
        for _ in range(count):
            if channel.empty():
                break
            labels.append(channel.child_value("label").strip() or f"Ch{len(labels) + 1}")
            channel = channel.next_sibling()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        labels = []
    fallback = _fallback_labels(kind, count)
    labels.extend(fallback[index] for index in range(len(labels), count))
    return tuple(labels)


def describe_stream(info: Any) -> StreamDescriptor | None:
    """从 pylsl 的 StreamInfo 构造描述；不支持/非法的流返回 None。"""
    name = str(_safe(info, "name", ""))
    stream_type = str(_safe(info, "type", ""))
    kind = canonical_kind(stream_type, name)
    if kind not in SUPPORTED_KINDS:
        return None
    channels = int(_safe(info, "channel_count", 0) or 0)
    if channels <= 0:
        return None
    return StreamDescriptor(
        kind=kind,
        name=name or KIND_LABELS[kind],
        stream_type=stream_type or KIND_LABELS[kind],
        channel_count=channels,
        nominal_srate=float(_safe(info, "nominal_srate", 0.0) or 0.0),
        channel_labels=_channel_labels(info, kind, channels),
        source_id=str(_safe(info, "source_id", "")),
        hostname=str(_safe(info, "hostname", "")),
        uid=str(_safe(info, "uid", "")),
    )


class StreamBuffer:
    """线程安全的环形缓冲：写入端是 LSL 拉取线程，读取端是信号处理链。"""

    def __init__(self, descriptor: StreamDescriptor, buffer_seconds: float = 60.0) -> None:
        rate = descriptor.nominal_srate if descriptor.nominal_srate > 0 else 100.0
        capacity = int(min(max(math.ceil(rate * buffer_seconds * 1.25), 2048), 250_000))
        self.descriptor = descriptor
        self._timestamps: deque = deque(maxlen=capacity)
        self._samples: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._count = 0
        self._last_monotonic: float | None = None

    def append_chunk(self, samples, timestamps) -> int:
        rows = []
        for stamp, sample in zip(timestamps, samples):
            if len(sample) != self.descriptor.channel_count:
                continue
            try:
                rows.append((float(stamp), tuple(float(value) for value in sample)))
            except (TypeError, ValueError):
                continue
        if not rows:
            return 0
        with self._lock:
            for stamp, row in rows:
                self._timestamps.append(stamp)
                self._samples.append(row)
            self._count += len(rows)
            self._last_monotonic = time.monotonic()
        return len(rows)

    def slice(self, seconds: float) -> tuple[list, list, int, float | None]:
        with self._lock:
            stamps = list(self._timestamps)
            rows = list(self._samples)
            count = self._count
            last = self._last_monotonic
        if stamps:
            cutoff = stamps[-1] - float(seconds)
            start = next((index for index, value in enumerate(stamps) if value >= cutoff), len(stamps))
            stamps, rows = stamps[start:], rows[start:]
        return stamps, rows, count, last

    def buffered_length(self) -> int:
        """缓冲里现有样本数（不要用 slice(0.0) 去数，那只会留下最后一个样本）。"""
        with self._lock:
            return len(self._samples)

    def samples_array(self, seconds: float) -> np.ndarray:
        """返回 (通道, 采样点) 的数组；无数据时返回空数组（不阻塞、不抛错）。"""
        _, rows, _, _ = self.slice(seconds)
        if not rows:
            return np.zeros((self.descriptor.channel_count, 0), dtype=float)
        return np.asarray(rows, dtype=float).T

    def samples_count(self, count: int) -> np.ndarray:
        """按**样点数**取最近 `count` 个样本，形状 (通道, 采样点)。

        为什么需要它：本设备（BioMulti Lite）的 LSL 时间戳会漂——同一个"4 秒窗"按时间戳切片
        实测拿到 670~1689 点（2.7~6.8 秒，见 `_analysis/lsl-hardware/window_geometry.json`），
        窗长忽长忽短会让 Welch 的频率分辨率与质检口径同时失真。声明采样率可靠时
        （本设备实测 248.5 Hz / 声明 250 Hz），按点数取窗是更稳的口径。
        """
        target = int(count)
        if target <= 0:
            return np.zeros((self.descriptor.channel_count, 0), dtype=float)
        with self._lock:
            rows = list(self._samples)[-target:]
        if not rows:
            return np.zeros((self.descriptor.channel_count, 0), dtype=float)
        return np.asarray(rows, dtype=float).T

    def stats(self) -> dict:
        with self._lock:
            stamps = list(self._timestamps)
            buffered = len(self._samples)
            count = self._count
            last = self._last_monotonic
        observed = None
        if len(stamps) > 1 and stamps[-1] > stamps[0]:
            observed = (len(stamps) - 1) / (stamps[-1] - stamps[0])
        return {
            "kind": self.descriptor.kind,
            "buffered_samples": buffered,
            "total_samples": count,
            "observed_srate": None if observed is None else round(observed, 3),
            "live": last is not None and (time.monotonic() - last) < 2.5,
            "seconds_since_last": None if last is None else round(time.monotonic() - last, 3),
        }


Resolver = Callable[[float], Iterable[Any]]


def _default_resolver(timeout: float) -> Iterable[Any]:
    from pylsl import resolve_streams

    return resolve_streams(timeout)


def _default_inlet(info: Any, buffer_seconds: float) -> Any:
    from pylsl import StreamInlet, proc_clocksync, proc_dejitter, proc_monotonize

    return StreamInlet(
        info,
        max_buflen=max(1, math.ceil(buffer_seconds)),
        recover=bool(str(_safe(info, "source_id", "")).strip()),
        processing_flags=proc_clocksync | proc_dejitter | proc_monotonize,
    )


class _StreamWorker(threading.Thread):
    """为一个流常驻拉取数据；出错时停掉自己并让管理器重连。"""

    def __init__(self, info, buffer: StreamBuffer, buffer_seconds: float,
                 inlet_factory, on_stopped) -> None:
        super().__init__(name=f"lsl-{buffer.descriptor.kind}", daemon=True)
        self.info = info
        self.buffer = buffer
        self.buffer_seconds = buffer_seconds
        self.inlet_factory = inlet_factory
        self.on_stopped = on_stopped
        self.stop_event = threading.Event()
        self.pull_timeout = 0.25

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        inlet = None
        error: str | None = None
        try:
            inlet = self.inlet_factory(self.info, self.buffer_seconds)
            while not self.stop_event.is_set():
                samples, timestamps = inlet.pull_chunk(timeout=self.pull_timeout, max_samples=1024)
                if timestamps:
                    self.buffer.append_chunk(samples, timestamps)
        except Exception as caught:  # noqa: BLE001 - 交由管理器上报并重连
            error = str(caught)
        finally:
            if inlet is not None:
                try:
                    inlet.close_stream()
                except (AttributeError, RuntimeError):
                    pass
            self.on_stopped(self.buffer.descriptor.kind, error)


class LiveStreamManager:
    """发现并维护每个数据种类一条常驻缓冲；`window()` 永不阻塞。"""

    def __init__(self, buffer_seconds: float = 60.0, *, resolver: Resolver | None = None,
                 inlet_factory: Callable | None = None, poll_interval: float = 2.0,
                 wanted_kinds: Iterable[str] = ("eeg",), wanted_name: str = "") -> None:
        self.buffer_seconds = float(buffer_seconds)
        self.poll_interval = float(poll_interval)
        self.wanted_kinds = tuple(wanted_kinds)
        # 指定流名称时只连这一条流：现场常常同时存在多条 EEG 流（真机 + 自己的仿真 outlet），
        # 之前只按 kind 取"第一条 EEG"，会出现"界面写着 lsl:真机名、实际连到另一条流"的静默错配。
        self.wanted_name = str(wanted_name or "").strip()
        self._resolver = resolver or _default_resolver
        self._inlet_factory = inlet_factory or _default_inlet
        self._buffers: dict[str, StreamBuffer] = {}
        self._workers: dict[str, _StreamWorker] = {}
        self._errors: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._refresh_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._sightings: list = []

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._discovery_loop, name="lsl-discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._refresh_event.set()
        with self._lock:
            workers = tuple(self._workers.values())
        for worker in workers:
            worker.stop()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        for worker in workers:
            worker.join(timeout=1.0)
        with self._lock:
            self._workers.clear()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ 发现
    def _discovery_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                found = list(self._resolver(1.0))
                self._sightings = [_ for _ in found]
                self._connect_new(found)
                with self._lock:
                    self._errors.pop("discovery", None)
            except Exception as caught:  # noqa: BLE001 - 发现失败要可重试
                with self._lock:
                    self._errors["discovery"] = str(caught)
            self._refresh_event.wait(timeout=self.poll_interval)
            self._refresh_event.clear()

    def _connect_new(self, infos) -> None:
        for info in infos:
            if self._stop_event.is_set():
                return
            descriptor = describe_stream(info)
            if descriptor is None:
                continue
            if self.wanted_name and descriptor.name != self.wanted_name:
                continue                                   # 明确指定了流名：其余流一律不连
            with self._lock:
                current = self._workers.get(descriptor.kind)
                if current is not None and current.is_alive():
                    continue
                buffer = self._buffers.get(descriptor.kind)
                if buffer is None or buffer.descriptor != descriptor:
                    buffer = StreamBuffer(descriptor, self.buffer_seconds)
                    self._buffers[descriptor.kind] = buffer
                worker = _StreamWorker(info, buffer, self.buffer_seconds,
                                       self._inlet_factory, self._worker_stopped)
                self._workers[descriptor.kind] = worker
                self._errors.pop(descriptor.kind, None)
            worker.start()
            LOGGER.info("已连接 LSL 流：%s -> %s", descriptor.label, descriptor.kind)

    def _worker_stopped(self, kind: str, error: str | None) -> None:
        with self._lock:
            if error:
                self._errors[kind] = error
            self._workers.pop(kind, None)
        if not self._stop_event.is_set():
            self._refresh_event.set()          # 立刻触发一轮重连

    # ------------------------------------------------------------------ 读取
    def wait_for(self, kind: str = "eeg", timeout: float = 10.0, min_samples: int = 1) -> bool:
        """等待某个种类出现并攒够样本；返回是否就绪（不抛错，便于上层给出友好提示）。"""
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            buffer = self.buffer(kind)
            if buffer is not None and buffer.buffered_length() >= min_samples:
                return True
            time.sleep(0.1)
        return False

    def buffer(self, kind: str = "eeg") -> StreamBuffer | None:
        with self._lock:
            return self._buffers.get(kind)

    def samples(self, kind: str, seconds: float) -> np.ndarray:
        """取最近的 seconds 秒数据，形状 (通道, 采样点)；没有数据返回 0 列。"""
        buffer = self.buffer(kind)
        if buffer is None:
            return np.zeros((0, 0), dtype=float)
        return buffer.samples_array(seconds)

    def descriptor(self, kind: str = "eeg") -> StreamDescriptor | None:
        buffer = self.buffer(kind)
        return buffer.descriptor if buffer is not None else None

    def descriptors(self) -> tuple:
        with self._lock:
            return tuple(buffer.descriptor for buffer in self._buffers.values())

    def errors(self) -> dict:
        with self._lock:
            return dict(self._errors)

    def status(self) -> dict:
        with self._lock:
            buffers = dict(self._buffers)
            errors = dict(self._errors)
        return {
            "running": self.running,
            "wanted_kinds": list(self.wanted_kinds),
            "wanted_name": self.wanted_name or None,
            "streams": {kind: buffer.stats() for kind, buffer in buffers.items()},
            "descriptors": {kind: buffer.descriptor.as_dict() for kind, buffer in buffers.items()},
            "errors": errors,
            "sighted_streams": [
                describe_stream(info) and describe_stream(info).as_dict()
                for info in self._sightings
            ],
        }


# ---------------------------------------------------------------------- 便捷入口

def probe_streams(timeout: float = 2.0) -> list[dict]:
    """扫描当前可见的 LSL 流（不建立连接），用于 /api/devices 与排查现场问题。"""
    try:
        found = _default_resolver(timeout)
    except ImportError as error:
        raise LslUnavailable("未安装 pylsl（pip install pylsl）") from error
    except Exception as error:  # noqa: BLE001
        raise LslUnavailable(f"LSL 扫描失败：{error}") from error
    out = []
    for info in found:
        descriptor = describe_stream(info)
        if descriptor is not None:
            item = descriptor.as_dict()
            item["supported"] = True
            out.append(item)
        else:
            out.append({
                "kind": None,
                "name": str(_safe(info, "name", "")),
                "stream_type": str(_safe(info, "type", "")),
                "channel_count": int(_safe(info, "channel_count", 0) or 0),
                "nominal_srate": float(_safe(info, "nominal_srate", 0.0) or 0.0),
                "supported": False,
                "label": f"{_safe(info, 'name', '')}（不支持的流类型，已忽略）",
            })
    return out
