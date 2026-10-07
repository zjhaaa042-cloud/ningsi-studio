"""数据源选择：**只用真实信号**，没有信号就如实显示"无信号"。

产品规则（2026-10-07 明确）：
- 没有脑机信号时**不再自动使用仿真源**——不静默、不降级，界面直接显示"无信号"；
- 仿真源只在**显式选择**时使用（`device="sim-bsense"`），且必须在界面与报告里标注；
- 没开会话也能看真实信号：`/api/devices/preview` 直接读实时流（见 `PreviewStream`）。

真实链路的代码路径始终存在且优先；"当前是仿真"这个事实必须如实出现在会话的 source 字段、
SSE 事件与报告里，不做静默替换。
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
    kind = str(descriptor.get("kind") or descriptor.get("stream_type") or "").lower()
    # 预览是"只接流不等样本"启动的，此时描述符还没解析出来：不要写成"None 通道"，
    # 如实说"尚未解析到描述符"（等有样本后 status() 会补上采样率与缓冲）。
    if not descriptor.get("channel_count"):
        return (f"{head}：{name}（尚未解析到描述符：流已可见但还没有样本推送）；"
                f"有样本后按 {CONDITION_SPEC} 调理：逐通道去直流 → 0.5 Hz 去漂移 → "
                f"50/60 Hz 陷波 → 45 Hz 低通（零相位）")
    return (f"{head}（{kind or '未知类型'}）：{name}（{descriptor.get('channel_count')} 通道，"
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


def sim_source_info() -> dict:
    """仿真源的描述（只在调用方**显式**选择仿真时使用，不再作为缺省）。"""
    return SourceInfo(SIM_SOURCE, "sim", 250.0, 1, SIM_SOURCE,
                      "仿真脑电源（仅用于演示与自动测试，必须在界面与报告中标注）", None).as_dict()


#: LSL 扫描结果的短时缓存：`/api/health` 会被前端高频轮询，每次真扫 0.2~1 秒会把接口拖慢。
#: 3 秒 TTL 对"插上设备后界面很快出现信号"足够灵敏，又不至于让 health 变慢。
_PROBE_CACHE: dict = {"at": 0.0, "sources": None, "probe": 0.0}


def cached_sources(probe_seconds: float = 0.4, ttl: float = 3.0) -> list[dict]:
    """带 TTL 的 `list_available()`：给健康检查/顶栏这类高频调用用。"""
    now = time.monotonic()
    cached = _PROBE_CACHE.get("sources")
    if cached is not None and (now - float(_PROBE_CACHE.get("at") or 0.0)) < ttl:
        return cached
    sources = list_available(probe_seconds)
    _PROBE_CACHE.update({"at": now, "sources": sources, "probe": probe_seconds})
    return sources


def detected_source(sources: list[dict] | None = None, probe_seconds: float = 0.4) -> dict | None:
    """当前**真正检测到**的实时数据源；一个都没有就返回 None（界面据此显示"无信号"）。

    优先级（实测踩过坑，顺序很重要）：
    1. **没数据也优先**：非仿真的流优先于内置仿真 outlet（后者仍标注为仿真）；
    2. **脑电流优先**：同一台设备常常同时推 EEG / Metric / Heart Rate / Motion 多条流，
       直接取"发现的第一条"很可能拿到 Metric（实测就是如此，那条流还没有样本，
       于是预览/建会话都会报"没有收到样本"）。所以按流类型排序，EEG 排最前。
    仿真脑电源 sim-bsense 永远不在此列：它必须被显式选择。
    """
    rows = sources if sources is not None else cached_sources(probe_seconds)
    lsl_rows = [row for row in rows
                if row.get("kind") == "lsl" and not str(row.get("key", "")).startswith("unsupported:")]
    if not lsl_rows:
        return None

    def rank(row: dict) -> tuple[int, int]:
        simulated = 1 if row.get("simulated") else 0
        is_eeg = 0 if str(row.get("stream_kind") or "").lower() == "eeg" else 1
        return (simulated, is_eeg)

    return min(lsl_rows, key=rank)


def detected_source_fields(sources: list[dict] | None = None, probe_seconds: float = 0.4) -> dict:
    """给 `/api/health`、`/api/overview` 的 `source`/`source_kind`/`source_note` 三件套。

    检测不到实时源时三者都是 None —— 顶栏要能如实写成「数据来源：无信号」，
    而不是回落到"新会话将使用仿真源"（那正是用户要求去掉的行为）。
    """
    row = detected_source(sources, probe_seconds)
    if row is None:
        return {"source": None, "source_kind": None, "source_note": None, "has_real_source": False}
    return {"source": row.get("key"), "source_kind": row.get("kind"),
            "source_note": row.get("note"), "has_real_source": not row.get("simulated")}


def default_device(sources: list[dict] | None = None, probe_seconds: float = 0.4) -> str | None:
    """新会话的缺省数据源键；没有检测到实时源时返回 None（调用方必须报错，不能改用仿真）。"""
    row = detected_source(sources, probe_seconds)
    return row.get("key") if row else None


def list_available(probe_seconds: float = 2.0) -> list[dict]:
    """列出可用数据源：**真实 LSL 流在前**，仿真源放最后并标记 `explicit_only`。

    顺序很关键：前端用"第一个 real 的源"作为缺省选择，仿真源不能排在前面，
    否则缺省又变成了仿真。
    """
    note = "仿真脑电源（仅用于演示与自动测试，必须在界面与报告中标注）"
    sim_entry = {**SourceInfo(SIM_SOURCE, "sim", 250.0, 1, SIM_SOURCE, note, None).as_dict(),
                 "explicit_only": True}
    sources: list[dict] = []

    try:
        from ningsi_studio.acquisition import lsl as lsl_module
    except Exception as error:                       # noqa: BLE001 - 导入失败也要能列出仿真源
        sim_entry["hardware_note"] = f"LSL 模块不可用：{error}"
        return [sim_entry]

    try:
        found = lsl_module.probe_streams(timeout=max(0.2, min(5.0, probe_seconds)))
    except lsl_module.LslUnavailable as error:
        sim_entry["hardware_note"] = str(error)
        return [sim_entry]
    except Exception as error:                       # noqa: BLE001 - 扫描失败不应让接口失败
        sim_entry["hardware_note"] = f"LSL 流扫描失败：{error}"
        return [sim_entry]

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
            # 归一后的流类型（eeg / metric / heart_rate / motion …）：缺省选源要按它排序，
            # 否则同一台设备的多条流里可能先拿到 Metric（还没有样本）而不是 EEG。
            "stream_kind": item.get("kind"),
            "channel_labels": item.get("channel_labels", []),
            "source_id": item.get("source_id", ""),
        })

    # 脑电流排到最前：前端"缺省预填第一个 real 源"与 detected_source() 都据此取值
    sources.sort(key=lambda row: 0 if str(row.get("stream_kind") or "").lower() == "eeg" else 1)

    if not sources:
        sim_entry["hardware_note"] = (
            "未发现 LSL 流（当前没有脑机信号）。请先启动采集端（如 BioMultiLite / BSense-R）；"
            "仅做演示/自测时，可显式选择仿真源 sim-bsense，"
            "或先用内置仿真流验证链路：python -m ningsi_studio simulate-outlet"
        )
    sources.append(sim_entry)
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
                 seed: int = 7, ready_timeout: float = 15.0, wait_ready: bool = True) -> SourceInfo:
    """按设备名构造数据源；真实设备不可用时抛错，由调用方决定怎么处理（**不再降级为仿真**）。

    `wait_ready=False`：只把流接上、不等样本（预览用）。这样"设备在推但还没推数据"
    会以 `live=false / no_signal=true` 呈现给界面，而不是直接报错——预览本身就是用来
    诊断"到底有没有信号"的，不能在没信号时拒绝启动。
    """
    if device and device.startswith("lsl:"):
        name = device.split(":", 1)[1]
        source = ManagedLslSource(name, device=name, srate=srate, channels=channels,
                                  ready_timeout=ready_timeout)
        if not wait_ready:
            try:
                source.start(wait=False)
            except Exception:                            # noqa: BLE001 - 未就绪交给 status() 报
                pass
            return SourceInfo(
                key=device, kind="lsl", srate=source.srate, channels=source.channels,
                device=name, note=_lsl_note(source.descriptor_dict(), name, source.srate),
                engine=source,
            )
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


class PreviewStream:
    """**无会话**的设备实时预览：直接读某个数据源，只做调理与质检，不落库、不出指标。

    为什么需要：会话是"按 11 个阶段跑一遍并落库"的重对象；只想确认"现在有没有信号、
    波形长什么样、这一窗能不能用"不该建会话、也不该在库里留下一条记录。
    设备状态页与实时监测页（未选会话时）都用它。

    - 未显式指定源时，自动用**当前检测到的实时源**；一个都没有就抛错（界面显示"无信号"）
      —— 与"不静默使用仿真"同一条规则：`sim-bsense` 必须被显式传入。
    - 线程安全：路由是并发的，start/stop/window 都加锁。
    """

    def __init__(self, *, window_seconds: float = 6.0, buffer_seconds: float = 60.0,
                 ready_timeout: float = 8.0) -> None:
        import threading

        self.window_seconds = float(window_seconds)
        self.buffer_seconds = float(buffer_seconds)
        self.ready_timeout = float(ready_timeout)
        self._lock = threading.RLock()
        self._source = None
        self._key: str | None = None
        self._started_at: float | None = None
        self._error: str | None = None

    # ------------------------------------------------------------------ 生命周期
    def start(self, device: str | None = None, *, probe_seconds: float = 0.8) -> dict:
        """启动预览（幂等）。`device` 为空时自动选当前检测到的实时源；没有源抛 RuntimeError。"""
        with self._lock:
            if self._source is not None and (device is None or device == self._key):
                return self.status()
            resolved = device or default_device(probe_seconds=probe_seconds)
            if not resolved:
                raise RuntimeError(
                    "未检测到脑电信号（LSL）：请先启动采集端（如 BioMultiLite / BSense-R），"
                    "或在“设备 / 数据源”里显式选择仿真源 sim-bsense（仅演示）")
            if self._source is not None:
                self.stop()
            try:
                # 预览只"接上流"，不等样本：流在但没数据时由 status() 报 live=false / no_signal=true，
                # 界面显示"无信号"，而不是整个预览起不来（那样就没法用它诊断了）。
                source = build_source(resolved, ready_timeout=self.ready_timeout, wait_ready=False)
            except Exception as exc:                     # noqa: BLE001 - 原样抛给界面显示
                self._error = str(exc)
                raise
            self._source = source
            self._key = resolved
            self._started_at = time.time()
            self._error = None
            return self.status()

    def stop(self) -> None:
        with self._lock:
            source = self._source
            self._source = None
            self._key = None
            self._started_at = None
        engine = getattr(source, "engine", None) if source is not None else None
        if engine is not None and hasattr(engine, "stop"):
            try:
                engine.stop()
            except Exception:                            # noqa: BLE001 - 停止失败不影响接口
                pass

    @property
    def active(self) -> bool:
        return self._source is not None

    # ------------------------------------------------------------------ 状态/取数
    def status(self) -> dict:
        with self._lock:
            source = self._source
            key = self._key
        if source is None:
            return {"active": False, "source": None, "kind": None, "device": None,
                    "no_signal": True, "note": self._error or "未开始预览：当前没有脑机信号",
                    "started_at": None}
        engine = source.engine
        stats: dict = {}
        has_status = hasattr(engine, "status")
        if has_status:
            try:
                stats = engine.status() or {}
            except Exception:                            # noqa: BLE001
                stats = {}
        stream = (stats.get("streams") or {}).get("eeg") or {}
        age = stream.get("seconds_since_last")
        if not has_status:
            # 仿真引擎没有流状态：它是即时生成的，直接视为有信号
            live, no_signal = True, False
        else:
            # 真机判据：**先要有过样本**（total/buffered > 0），再看新鲜度。
            # 只判"age is None ⇒ 新鲜"会把"流接上了但一个样本都没收到"误报成有信号。
            total = stream.get("total_samples")
            buffered = stream.get("buffered_samples")
            received = any(isinstance(value, (int, float)) and value > 0
                           for value in (total, buffered))
            stale = float(getattr(engine, "stale_timeout", 3.0) or 3.0)
            live = bool(received) and (age is None or float(age) <= stale)
            no_signal = not live
        conditioning = {}
        if hasattr(engine, "conditioning"):
            try:
                conditioning = engine.conditioning() or {}
            except Exception:                            # noqa: BLE001
                conditioning = {}
        return {
            "active": True,
            "source": key,
            "kind": source.kind,
            "device": source.device,
            "srate": round(float(source.srate or 0.0), 3),
            "channels": int(source.channels or 1),
            "channel_labels": list(getattr(source, "channel_labels", []) or []),
            "ready": bool(live),
            # live=False 就是界面上的"无信号"：流在，但最近没有样本（设备没开推 / 线掉了）
            "live": bool(live),
            "seconds_since_last": None if age is None else round(float(age), 2),
            "buffered_samples": stream.get("buffered_samples"),
            "conditioning": conditioning,
            "errors": stats.get("errors") or {},
            "note": source.note,
            "started_at": self._started_at,
            "no_signal": bool(no_signal),
        }

    def window(self) -> dict:
        """取一窗调理后的真实波形 + 质检判定（预览用，不落库）。"""
        with self._lock:
            source = self._source
        if source is None:
            return {"active": False, "no_signal": True, "samples": [], "seconds": 0.0,
                    "note": self._error or "未开始预览：当前没有脑机信号"}
        engine = source.engine
        window_sec = min(self.window_seconds, float(getattr(engine, "condition_seconds", 0.0) or self.window_seconds))
        try:
            # 真机（ManagedLslSource）支持 wait=False 关掉"有限等待"，预览才能按刷新率出帧；
            # 仿真源（SyntheticEEG）即时生成，接口不带该参数（与会话 /signal 通道同一处理）。
            if hasattr(engine, "condition_seconds"):
                data = engine.window("rest", seconds=window_sec, wait=False)
            else:
                data = engine.window("rest", seconds=window_sec)
        except Exception as exc:                         # noqa: BLE001 - 预览不阻塞界面
            return {**self.status(), "samples": [], "seconds": 0.0, "no_signal": True,
                    "note": f"读取失败：{exc}"}
        arr = np.atleast_2d(np.asarray(data, dtype=float))
        if arr.size == 0 or arr.shape[1] == 0:
            return {**self.status(), "samples": [], "seconds": 0.0, "no_signal": True}
        from ningsi.signal.artifacts import check_channels

        # 刚接上流时缓冲里可能只有一两百点，而 Welch 分段需要 segment_sec × srate 点
        # （250 Hz 时 500 点）——直接算会抛"样本数不足"并把预览接口打成 500。
        # 预览是"看一眼现场信号"的工具，**绝不能因为样本不够而失败**：如实报"样本不足"。
        srate = float(source.srate or 250.0)
        required = int(round(float(config.WELCH.get("segment_sec", 2.0)) * srate))
        if arr.shape[1] < required:
            quality = {"ok": None, "reasons": ["insufficient_samples"],
                       "metrics": {"samples": int(arr.shape[1]), "required": required}}
            note = (f"样本还不够算质检：本窗 {arr.shape[1]} 点，Welch 分段需要 {required} 点"
                    f"（约 {required / max(srate, 1e-6):.1f} 秒），等一两秒再看")
        else:
            try:
                quality = check_channels(arr, srate).as_dict()
                note = None
            except Exception as exc:                     # noqa: BLE001 - 预览不因质检失败而报错
                quality = {"ok": None, "reasons": ["quality_error"], "metrics": {"error": str(exc)}}
                note = f"质检计算失败：{exc}"
        payload_arr = arr
        if arr.shape[1] > 2400:                          # 只传显示需要的点数（避免响应过大）
            step = int(np.ceil(arr.shape[1] / 2400))
            payload_arr = arr[:, ::step]
        return {
            **self.status(),
            "no_signal": False,
            "seconds": round(float(arr.shape[1]) / srate, 3),
            "samples": [[round(float(value), 3) for value in channel] for channel in payload_arr],
            "peak_uv": round(float(np.max(np.abs(arr))), 2),
            "std_uv": round(float(np.mean(np.std(arr, axis=1))), 2),
            "quality": quality,
            "quality_note": note,
        }


#: 进程内单例：设备状态页与"未选会话"的实时监测页共用同一条预览流，避免重复打开设备。
_PREVIEW = PreviewStream()


def preview_stream() -> PreviewStream:
    return _PREVIEW
