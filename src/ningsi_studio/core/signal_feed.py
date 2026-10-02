"""高频原始信号流：把"显示"从"分析窗"里解耦出来。

原来的问题：界面只有在 4 秒分析窗完成时才拿到一帧信号，所以波形看起来 2 秒才跳一下。
这里改成独立的数据通道——按固定显示刷新率（默认 10 FPS）直接读采集缓冲的**最新样本**，
与窗分析节奏无关；并做 min/max 保峰值抽稀，让尖峰/伪迹在屏幕上不会消失。

设计要点：
- 每个会话一个订阅表；第一个订阅者到达时启动轮询线程，最后一个离开时停掉（不空转）。
- 帧内容按通道分组（每条通道独立量程），前端据此画 BioMultiLite 那样的多通道时间序列。
- 频谱用最近一个**已分析窗**的 Welch 结果（`spectrum_db`），与报告口径完全一致，
  不在前端重复实现 FFT，避免"界面频谱"和"报告频谱"两套算法。
"""

from __future__ import annotations

import logging
import queue as queue_module
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

LOGGER = logging.getLogger("ningsi_studio.signal")


@dataclass
class _Subscriber:
    """与 http.sse._Subscriber 同形：`events` 是队列，供 send_event_stream 消费。"""

    events: "object" = None
    dropped: int = 0

    def put(self, event: dict) -> None:
        queue = self.events
        if queue is None:
            return
        try:
            queue.put_nowait(event)           # type: ignore[attr-defined]
        except Exception:                     # noqa: BLE001 - 队列满时丢最旧的帧
            try:
                queue.get_nowait()            # type: ignore[attr-defined]
                queue.put_nowait(event)       # type: ignore[attr-defined]
                self.dropped += 1
            except Exception:                 # noqa: BLE001
                pass


def decimate_minmax(values: np.ndarray, max_points: int) -> np.ndarray:
    """保峰值抽稀：每段取 min/max 两个点，替代"每 N 点取一个"（会漏掉尖峰）。

    注意 buckets 必须由 `size // per` 反推：先按 buckets 算 per 会得到
    `buckets * per > size`，reshape 直接抛 "cannot reshape array"（真实踩过）。
    """
    data = np.asarray(values, dtype=float).ravel()
    if max_points <= 0 or data.size <= max_points:
        return data
    per = max(1, int(np.ceil(data.size / max(1, max_points // 2))))
    buckets = data.size // per
    if buckets < 1:
        return data
    trimmed = data[: buckets * per].reshape(buckets, per)
    mins = trimmed.min(axis=1)
    maxs = trimmed.max(axis=1)
    # 尾部余数（size % per 个点）不能丢：尖峰落在最后一段时会被整段抹掉，
    # 这里把余数并入最后一桶，保证"看到的峰值就是真实峰值"。
    remainder_start = buckets * per
    if remainder_start < data.size:
        tail = data[remainder_start:]
        mins[-1] = min(mins[-1], float(tail.min()))
        maxs[-1] = max(maxs[-1], float(tail.max()))
    interleaved = np.empty(buckets * 2, dtype=float)
    interleaved[0::2] = mins
    interleaved[1::2] = maxs
    return interleaved


@dataclass
class SignalConfig:
    window_sec: float = 10.0        # 每帧覆盖的时间跨度
    refresh_hz: float = 10.0        # 显示刷新率
    max_points_per_channel: int = 1200
    queue_size: int = 4             # 每订阅者最多积压几帧（多余的丢旧帧）


class SignalFeed:
    """一个会话的高频信号源；`samples_provider()` 需返回 (通道, 采样点) 数组。"""

    def __init__(self, session_uuid: str, samples_provider: Callable[[float], np.ndarray],
                 *, srate: float, channels: int, channel_labels: list[str] | None = None,
                 window_provider: Callable[[], dict | None] | None = None,
                 config: SignalConfig | None = None, device: str = "",
                 source_kind: str = "sim", spectrum_interval: float = 1.0) -> None:
        self.session_uuid = session_uuid
        self._samples = samples_provider
        self._window = window_provider
        self.config = config or SignalConfig()
        self.srate = float(srate)
        self.channels = int(channels)
        self.channel_labels = list(channel_labels or [])
        self.device = device
        self.source_kind = source_kind
        self.spectrum_interval = float(spectrum_interval)
        self._lock = threading.Lock()
        self._subscribers: list[_Subscriber] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frames = 0
        self._last_error: str | None = None
        self._spectrum_cache: dict | None = None
        self._spectrum_at: float = 0.0
        self._spectrum_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 订阅
    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def subscribe(self) -> tuple[_Subscriber, int]:
        """订阅并立即返回一帧（与 EventBus.subscribe 同形：(subscriber, 预置帧数)）。"""
        subscriber = _Subscriber(events=queue_module.Queue(maxsize=max(1, self.config.queue_size)))
        with self._lock:
            self._subscribers.append(subscriber)
            start = self._thread is None or not self._thread.is_alive()
        if start:
            self._start()
        # 立刻给一帧，避免前端等一整个刷新周期才看到内容
        try:
            subscriber.put(self.frame(seq=self._frames))
        except Exception as exc:  # noqa: BLE001 - 首帧失败不影响后续
            LOGGER.warning("首帧生成失败：%s", exc)
        LOGGER.info("signal 订阅: session=%s 首帧入队=%d 线程存活=%s",
                    self.session_uuid[:8], subscriber.events.qsize(),
                    bool(self._thread is not None and self._thread.is_alive()))
        return subscriber, subscriber.events.qsize()

    def is_subscribed(self, subscriber: _Subscriber) -> bool:
        with self._lock:
            return subscriber in self._subscribers

    def retune(self, config: SignalConfig) -> bool:
        """按新参数调整刷新率/窗口（同一会话的 SignalFeed 会被复用，参数变了要生效）。

        返回是否发生了变化。刷新率在下一帧循环里自然生效（循环每帧重算间隔）。
        """
        changed = (config.window_sec != self.config.window_sec
                   or config.refresh_hz != self.config.refresh_hz
                   or config.max_points_per_channel != self.config.max_points_per_channel)
        self.config = config
        return changed

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)
            empty = not self._subscribers
            thread = self._thread
            spectrum_thread = self._spectrum_thread
        if empty:
            self._stop.set()
            for target in (thread, spectrum_thread):
                if target is not None:
                    target.join(timeout=2.5)
            with self._lock:
                self._thread = None
                self._spectrum_thread = None
            self._stop = threading.Event()

    # ------------------------------------------------------------------ 取数
    def _channel_labels(self) -> list[str]:
        if len(self.channel_labels) >= self.channels:
            return self.channel_labels[: self.channels]
        preset = ["Fp1", "Fp2", "F3", "F4", "C3", "C4", "O1", "O2"]
        labels = list(self.channel_labels)
        for index in range(len(labels), self.channels):
            labels.append(preset[index] if index < len(preset) else f"Ch{index + 1}")
        return labels

    def frame(self, seq: int | None = None) -> dict:
        """生成一帧：多通道抽稀后的波形 + 最近窗的频谱与频带功率。"""
        span = float(self.config.window_sec)
        try:
            data = self._samples(span)
        except Exception as exc:  # noqa: BLE001 - 采样失败也要能推帧（前端显示错误）
            self._last_error = str(exc)
            data = np.zeros((0, 0), dtype=float)
        rows = np.atleast_2d(np.asarray(data, dtype=float)) if getattr(data, "size", 0) else np.zeros((0, 0))
        count = rows.shape[1] if rows.ndim == 2 and rows.size else 0
        actual_channels = rows.shape[0] if rows.ndim == 2 and rows.size else self.channels

        channels: list[dict] = []
        for index in range(actual_channels):
            series = rows[index] if rows.size else np.zeros(0, dtype=float)
            reduced = decimate_minmax(series, self.config.max_points_per_channel)
            peak = float(np.max(np.abs(series))) if series.size else 0.0
            channels.append({
                "index": index,
                "label": self._channel_labels()[index] if index < len(self._channel_labels()) else f"Ch{index + 1}",
                "samples": [round(float(value), 2) for value in reduced],
                "points": int(reduced.size),
                "raw_points": int(series.size),
                "unit": "uV",
                "peak": round(peak, 2),
            })

        payload = {
            "type": "signal",
            "session": self.session_uuid,
            "seq": self._frames if seq is None else seq,
            "t": round(time.time(), 3),
            "window_sec": span,
            "srate": self.srate,
            "channels": channels,
            "channel_count": actual_channels,
            "samples": count,
            "source_kind": self.source_kind,
            "device": self.device,
            "refresh_hz": self.config.refresh_hz,
            "realtime": count > 0,
            "note": self._last_error,
        }
        if self._window is not None:
            # 频谱放在**独立线程**里算（见 _spectrum_loop）。
            # 不能在 frame() 里同步算：engine.window() 在真实设备上会等缓冲填满（可达 2 秒），
            # 直接把 10 FPS 的推帧拖成 0.5 FPS，频谱反而永远来不及出。
            payload["spectrum"] = (self._spectrum_cache or {}).get("spectrum") or {}
            payload["bands"] = (self._spectrum_cache or {}).get("bands") or {}
            payload["band_peaks"] = (self._spectrum_cache or {}).get("band_peaks") or {}
        return payload

    def _spectrum_loop(self) -> None:
        """低频（默认 1 Hz）刷新频谱缓存；与波形推帧互不阻塞。"""
        while not self._stop.is_set():
            try:
                fresh = self._window() if self._window is not None else None
                if fresh:
                    with self._lock:
                        self._spectrum_cache = fresh
                        self._spectrum_at = time.monotonic()
            except Exception as exc:  # noqa: BLE001 - 频谱失败不影响波形
                LOGGER.debug("频谱刷新失败：%s", exc)
            self._stop.wait(timeout=max(0.2, self.spectrum_interval))

    # ------------------------------------------------------------------ 轮询线程
    def _start(self) -> None:
        self._stop.clear()
        thread = threading.Thread(target=self._loop, name=f"signal-{self.session_uuid[:8]}", daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        if self._window is not None:
            self._spectrum_thread = threading.Thread(
                target=self._spectrum_loop, name=f"spectrum-{self.session_uuid[:8]}", daemon=True)
            self._spectrum_thread.start()

    def _loop(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            with self._lock:
                subscribers = list(self._subscribers)
                # 每帧重算间隔：调用方通过 retune() 改了刷新率要立刻生效
                interval = 1.0 / max(0.5, float(self.config.refresh_hz))
            if not subscribers:
                break
            self._frames += 1
            try:
                event = self.frame(seq=self._frames)
            except Exception as exc:  # noqa: BLE001 - 生成失败记录后继续
                self._last_error = str(exc)
                LOGGER.debug("生成信号帧失败：%s", exc)
                event = None
            if event is not None:
                for subscriber in subscribers:
                    subscriber.put(event)
            next_tick += interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()      # 落后就对齐，避免堆积

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            threads = [t for t in (self._thread, self._spectrum_thread) if t is not None]
        for thread in threads:
            thread.join(timeout=2.5)


class SignalFeedRegistry:
    """按会话持有 SignalFeed；会话结束后释放。"""

    def __init__(self) -> None:
        self._feeds: dict[str, SignalFeed] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_uuid: str, factory: Callable[[], SignalFeed]) -> SignalFeed:
        with self._lock:
            feed = self._feeds.get(session_uuid)
            if feed is None:
                feed = factory()
                self._feeds[session_uuid] = feed
            return feed

    def get(self, session_uuid: str) -> SignalFeed | None:
        with self._lock:
            return self._feeds.get(session_uuid)

    def drop(self, session_uuid: str) -> None:
        with self._lock:
            feed = self._feeds.pop(session_uuid, None)
        if feed is not None:
            feed.stop()

    def sweep(self, is_live: Callable[[str], bool]) -> int:
        """回收「会话已结束且没有订阅者」的 feed，返回回收条数。

        `is_live(uuid)` 由调用方给出**运行器的实际存活状态**（不能用库里的 status：
        刚 done 但线程还在收尾时按库判会误清正在推流的 feed）。

        幂等且可重入：只处理 `subscriber_count == 0` 的条目，正在推流的 feed 不会被关掉；
        并发的新订阅者进来时该条目的订阅数已不为 0，本轮就不会被回收。
        """
        with self._lock:
            stale = [session_uuid for session_uuid, feed in self._feeds.items()
                     if feed.subscriber_count == 0 and not is_live(session_uuid)]
            feeds = [self._feeds.pop(session_uuid) for session_uuid in stale]
        for feed in feeds:
            feed.stop()
        if stale:
            LOGGER.info("回收实时信号 feed：%s", [session_uuid[:8] for session_uuid in stale])
        return len(stale)

    def stop_all(self) -> None:
        with self._lock:
            feeds = list(self._feeds.values())
            self._feeds.clear()
        for feed in feeds:
            feed.stop()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._feeds)


REGISTRY = SignalFeedRegistry()
