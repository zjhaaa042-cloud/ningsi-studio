"""高频信号流（SignalFeed）的单元测试。

回归背景：
1. `decimate_minmax` 曾用 `buckets * per > size` 的 reshape 直接抛
   "cannot reshape array of size 2500 into shape (600,5)"，导致整条信号流一帧都推不出来；
2. 抽稀必须**保峰值**（尾部余数并入最后一桶），否则尖峰/伪迹在界面上会消失；
3. 频谱计算不能放在推帧循环里——真实设备上 `window()` 会等缓冲（可达 2 秒），
   会把 10 FPS 拖成 0.5 FPS。
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from ningsi_studio.core import signal_feed as sf

from .helpers import StudioTestCase


def fake_source(seconds: float) -> np.ndarray:
    """确定性假数据源：两通道、250 Hz、含一个明显尖峰。"""
    count = int(round(seconds * 250.0))
    t = np.arange(count) / 250.0
    base = 20.0 * np.sin(2 * np.pi * 10.0 * t)
    return np.vstack([base, base * 0.5])


class DecimateTests(StudioTestCase):
    def test_preserves_peak_everywhere(self) -> None:
        """各种长度/上限组合下：抽稀结果必须与原始峰值一致。"""
        for size, cap in ((2500, 600), (12345, 1200), (1000, 200), (1999, 700), (777, 101), (500, 2)):
            data = np.arange(size, dtype=float)
            reduced = sf.decimate_minmax(data, cap)
            self.assertEqual(reduced.max(), data.max(), f"size={size} cap={cap} 峰值被抹掉")
            self.assertLessEqual(reduced.size, max(cap * 2, cap), f"size={size} cap={cap} 点数超上限")

    def test_preserves_tail_spike(self) -> None:
        """尖峰落在尾部余数里也不能丢（旧实现会整段裁掉余数）。"""
        data = np.zeros(2500)
        data[-1] = 999.0
        reduced = sf.decimate_minmax(data, 600)
        self.assertEqual(reduced.max(), 999.0, "尾部尖峰必须保留")

    def test_short_input_passthrough(self) -> None:
        data = np.arange(10, dtype=float)
        self.assertEqual(sf.decimate_minmax(data, 100).size, 10)
        self.assertEqual(sf.decimate_minmax(np.zeros(0), 100).size, 0)


class SignalFeedTests(StudioTestCase):
    def test_frame_shape(self) -> None:
        """一帧要含多通道波形、量程、采样信息。"""
        feed = sf.SignalFeed("s1", fake_source, srate=250.0, channels=2,
                             channel_labels=["Fp1", "Fp2"], device="sim", source_kind="sim",
                             config=sf.SignalConfig(window_sec=4.0, max_points_per_channel=400))
        frame = feed.frame(seq=1)
        self.assertEqual(frame["type"], "signal")
        self.assertEqual(frame["channel_count"], 2)
        self.assertEqual(frame["samples"], 1000)            # 4 s × 250 Hz
        self.assertTrue(frame["realtime"])
        self.assertEqual([c["label"] for c in frame["channels"]], ["Fp1", "Fp2"])
        for channel in frame["channels"]:
            self.assertLessEqual(channel["raw_points"], 1000)
            self.assertGreaterEqual(channel["points"], 2)

    def test_rate_and_thread_teardown(self) -> None:
        """按配置的刷新率推帧；最后一个订阅者离开后线程必须停掉。"""
        feed = sf.SignalFeed("s2", fake_source, srate=250.0, channels=1,
                             config=sf.SignalConfig(window_sec=2.0, refresh_hz=20.0))
        subscriber, _ = feed.subscribe()
        started = time.time()
        received = 0
        while time.time() - started < 1.2:
            try:
                subscriber.events.get(timeout=0.4)
                received += 1
            except Exception:  # noqa: BLE001 - 队列暂时为空
                continue
        span = time.time() - started
        self.assertGreater(received, 10, f"1.2 秒内应收到 >10 帧（20 FPS），实际 {received}")
        self.assertTrue(feed._thread is not None and feed._thread.is_alive())
        feed.unsubscribe(subscriber)
        time.sleep(0.6)
        self.assertEqual(feed.subscriber_count, 0)
        self.assertIsNone(feed._thread, "没有订阅者时必须停掉推帧线程，不能空转")

    def test_retune_changes_rate(self) -> None:
        """复用同一 feed 时，retune 必须让新刷新率生效（界面切 20 FPS 的场景）。"""
        feed = sf.SignalFeed("s3", fake_source, srate=250.0, channels=1,
                             config=sf.SignalConfig(window_sec=2.0, refresh_hz=5.0))
        self.assertFalse(feed.retune(sf.SignalConfig(window_sec=2.0, refresh_hz=5.0)))
        self.assertTrue(feed.retune(sf.SignalConfig(window_sec=2.0, refresh_hz=20.0)))
        self.assertEqual(feed.config.refresh_hz, 20.0)

    def test_registry_reuses_and_drops(self) -> None:
        registry = sf.SignalFeedRegistry()
        made = registry.get_or_create("a", lambda: sf.SignalFeed("a", fake_source, srate=250.0, channels=1))
        again = registry.get_or_create("a", lambda: sf.SignalFeed("a", fake_source, srate=250.0, channels=1))
        self.assertIs(made, again, "同一会话应复用同一个 feed")
        registry.drop("a")
        self.assertEqual(registry.count, 0)


class SpectrumProbeTests(StudioTestCase):
    def test_probe_returns_curve_and_bands(self) -> None:
        """频谱探针要同时给出曲线（freqs/power_db）与频带相对功率（rel）。"""
        from ningsi.acquisition.simulate import SyntheticEEG

        from ningsi_studio.domain.indicators import WindowEngine

        engine = WindowEngine(SyntheticEEG(srate=250.0, channels=1, seed=7), 250.0, device="sim-bsense")
        probe = engine.spectrum_probe(seconds=4.0)
        self.assertTrue(probe["usable"])
        self.assertGreater(len(probe["freqs"]), 10)
        self.assertEqual(len(probe["freqs"]), len(probe["power_db"]))
        self.assertTrue(all(freq <= 45.0 for freq in probe["freqs"]), "只画 0–45 Hz")
        self.assertIn("alpha", probe["rel"])
        self.assertAlmostEqual(sum(probe["rel"].values()), 1.0, places=3, msg="相对功率之和应为 1")

    def test_probe_passthrough_failure(self) -> None:
        """数据不足时必须给出 usable=False，而不是让调用方拿到半成品。"""
        from ningsi.acquisition.simulate import SyntheticEEG

        from ningsi_studio.domain.indicators import WindowEngine

        engine = WindowEngine(SyntheticEEG(srate=250.0, channels=1, seed=7), 250.0, device="sim-bsense")
        engine.source.window = lambda *args, **kwargs: np.zeros((1, 10))   # 样本太少
        probe = engine.spectrum_probe(seconds=4.0)
        self.assertFalse(probe["usable"])
        self.assertEqual(probe["freqs"], [])
