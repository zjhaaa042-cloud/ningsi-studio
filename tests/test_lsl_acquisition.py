"""LSL 采集层与真机调理的口径测试。

这三件事此前完全没有测试覆盖，而它们恰好是真机链路上最容易悄悄错的地方：

1. **窗口长度**：按 LSL 时间戳切片时，同一个"4 秒窗"实测能拿到 2.7~6.8 秒的数据
   （`_analysis/lsl-hardware/window_geometry.json`），所以窗口长度必须由样点数兑现；
2. **流名匹配**：现场可能同时存在多条 EEG 流（真机 + 自己的仿真 outlet），
   只按 kind 取第一条会出现"界面写着 lsl:真机名、实际连到另一条流"；
3. **真机调理**：设备直出的原始值带数百 mV 直流偏置，不去直流就送质检会让每一窗都判为
   amplitude/channel_span 越界（`_analysis/lsl-hardware/ab2.json` 的 A 组）。
"""

from __future__ import annotations

import time
import unittest

import numpy as np

from ningsi import config
from ningsi.signal.window import analyze_window
from ningsi_studio.acquisition.lsl import (
    LiveStreamManager,
    StreamBuffer,
    StreamDescriptor,
    canonical_kind,
    describe_stream,
)
from ningsi_studio.core.live_source import ManagedLslSource

SRATE = 250.0


# ---------------------------------------------------------------------- 假 LSL 对象

class _ChannelNode:
    """pylsl 的 XMLElement 在 `_channel_labels()` 里只用到这几个方法。"""

    def __init__(self, labels, index=0) -> None:
        self.labels = list(labels)
        self.index = index

    def empty(self) -> bool:
        return self.index >= len(self.labels)

    def child_value(self, name: str) -> str:
        return self.labels[self.index] if self.index < len(self.labels) else ""

    def next_sibling(self):
        return _ChannelNode(self.labels, self.index + 1)


class _ChannelsNode:
    def __init__(self, labels) -> None:
        self.labels = list(labels)

    def child(self, name: str) -> _ChannelNode:
        return _ChannelNode(self.labels)


class _Desc:
    def __init__(self, labels) -> None:
        self.labels = list(labels)

    def child(self, name: str) -> _ChannelsNode:
        return _ChannelsNode(self.labels)


class FakeStreamInfo:
    def __init__(self, name: str, *, stream_type: str = "EEG", channels: int = 2,
                 srate: float = 250.0, labels=(), source_id: str = "") -> None:
        self._name = name
        self._type = stream_type
        self._channels = channels
        self._srate = srate
        self._labels = tuple(labels)
        self._source_id = source_id

    def name(self) -> str:
        return self._name

    def type(self) -> str:
        return self._type

    def channel_count(self) -> int:
        return self._channels

    def nominal_srate(self) -> float:
        return self._srate

    def source_id(self) -> str:
        return self._source_id

    def hostname(self) -> str:
        return "fake-host"

    def uid(self) -> str:
        return f"uid-{self._name}"

    def desc(self) -> _Desc:
        return _Desc(self._labels)


class FakeInlet:
    """按行喂样本的假 inlet；数据喂完后只回空块（模拟"设备停了"）。"""

    def __init__(self, samples, timestamps) -> None:
        self._rows = list(samples)
        self._stamps = list(timestamps)
        self.closed = False

    def pull_chunk(self, timeout: float = 0.25, max_samples: int = 1024):
        if not self._rows:
            time.sleep(min(0.02, timeout))
            return [], []
        rows = self._rows[:max_samples]
        stamps = self._stamps[:max_samples]
        del self._rows[:max_samples]
        del self._stamps[:max_samples]
        return rows, stamps

    def close_stream(self) -> None:
        self.closed = True


def fake_resolver(*infos):
    return lambda timeout: list(infos)


# ---------------------------------------------------------------------- 测试

class DescriptorTest(unittest.TestCase):
    def test_canonical_kind_normalizes_vendor_spellings(self):
        for stream_type, expected in (("EEG", "eeg"), ("eeg", "eeg"), ("Fnirs", "fnirs"),
                                      ("Heart Rate", "heart_rate"), ("Marker", None)):
            self.assertEqual(canonical_kind(stream_type), expected, f"{stream_type!r} 归一失败")

    def test_describe_stream_falls_back_to_channel_labels(self):
        info = FakeStreamInfo("BioMulti Lite EEG-00cde1", channels=2)
        descriptor = describe_stream(info)
        self.assertIsNotNone(descriptor)
        self.assertEqual(descriptor.kind, "eeg")
        self.assertEqual(descriptor.channel_count, 2)
        # 厂商 XML 描述为空（真机 desc 就是空的）时必须给 Fp1/Fp2 这类兜底标签
        self.assertEqual(tuple(descriptor.channel_labels), ("Fp1", "Fp2"))

    def test_describe_stream_rejects_unsupported_and_empty(self):
        self.assertIsNone(describe_stream(FakeStreamInfo("Marker", stream_type="Markers")))
        self.assertIsNone(describe_stream(FakeStreamInfo("Empty", channels=0)))


class WindowLengthTest(unittest.TestCase):
    """窗口长度必须由点数兑现，而不是由会漂的 LSL 时间戳决定。"""

    def setUp(self):
        self.descriptor = StreamDescriptor(kind="eeg", name="fake", stream_type="EEG",
                                          channel_count=2, nominal_srate=SRATE)

    def test_samples_count_returns_exactly_the_tail(self):
        buffer = StreamBuffer(self.descriptor, buffer_seconds=5.0)
        rows = [[float(index), float(index) * 10] for index in range(20)]
        buffer.append_chunk(rows, [index / SRATE for index in range(20)])

        tail = buffer.samples_count(5)
        self.assertEqual(tail.shape, (2, 5))
        self.assertEqual(tail[0].tolist(), [15.0, 16.0, 17.0, 18.0, 19.0])
        self.assertEqual(tail[1].tolist(), [150.0, 160.0, 170.0, 180.0, 190.0])

    def test_samples_count_clamps_and_handles_empty(self):
        buffer = StreamBuffer(self.descriptor, buffer_seconds=5.0)
        self.assertEqual(buffer.samples_count(100).shape, (2, 0))
        self.assertEqual(buffer.samples_count(0).shape, (2, 0))
        buffer.append_chunk([[1.0, 2.0]], [0.0])
        self.assertEqual(buffer.samples_count(100).shape, (2, 1))

    def test_timestamp_slice_can_exceed_requested_span(self):
        """同一段缓冲：按时间戳切片拿到 12 点，按点数切片稳定拿到 4 点。"""
        buffer = StreamBuffer(self.descriptor, buffer_seconds=5.0)
        rows = [[float(index), float(index)] for index in range(20)]
        # 时间戳被"压缩"：20 个样本只覆盖 0.08 秒 ⇒ 时间戳口径会把它们全算进 0.5 秒窗
        stamps = [index * 0.004 for index in range(20)]
        buffer.append_chunk(rows, stamps)
        self.assertGreater(buffer.samples_array(0.5).shape[1], 10)
        self.assertEqual(buffer.samples_count(4).shape[1], 4)


class StreamNamePinningTest(unittest.TestCase):
    """显式指定流名时，管理线程只能连到这条流。"""

    def test_only_the_requested_stream_is_connected(self):
        wanted = FakeStreamInfo("BioMulti Lite EEG-00cde1", channels=2)
        other = FakeStreamInfo("ningsi-sim-eeg", channels=2)
        inlets = {}

        def inlet_factory(info, buffer_seconds):
            inlets[info.name()] = FakeInlet(
                [[1.0, 2.0]] * 4, [index / SRATE for index in range(4)])
            return inlets[info.name()]

        manager = LiveStreamManager(resolver=fake_resolver(other, wanted),
                                    inlet_factory=inlet_factory, poll_interval=0.05,
                                    wanted_name="BioMulti Lite EEG-00cde1")
        try:
            manager.start()
            self.assertTrue(manager.wait_for("eeg", timeout=3.0, min_samples=1))
            descriptor = manager.descriptor("eeg")
            self.assertEqual(descriptor.name, "BioMulti Lite EEG-00cde1")
            self.assertIn("BioMulti Lite EEG-00cde1", inlets)
            self.assertNotIn("ningsi-sim-eeg", inlets)
            self.assertEqual(manager.status()["wanted_name"], "BioMulti Lite EEG-00cde1")
        finally:
            manager.stop()

    def test_without_pinning_any_supported_eeg_stream_is_connected(self):
        manager = LiveStreamManager(
            resolver=fake_resolver(FakeStreamInfo("any-eeg", channels=1)),
            inlet_factory=lambda info, seconds: FakeInlet([[1.0]], [0.0]),
            poll_interval=0.05)
        try:
            manager.start()
            self.assertTrue(manager.wait_for("eeg", timeout=3.0, min_samples=1))
            self.assertIsNone(manager.status()["wanted_name"])
        finally:
            manager.stop()


class ConditioningTest(unittest.TestCase):
    """真机调理：逐通道去直流 → 文档 8.2 四级链；不去直流时质检必然误判。"""

    def setUp(self):
        # 不调用 start()：只测 _condition()，无需真实 pylsl 设备
        self.source = ManagedLslSource("fake-stream", device="fake-stream",
                                       srate=SRATE, channels=2)

    @staticmethod
    def _raw(dc: float = -232_700.0, gap: float = 13_900.0, seconds: float = 6.0):
        """复刻真机实测形态：两通道各有几百 mV 的电极偏置，且直流不相等（实测差 ~13900 µV）。"""
        t = np.arange(int(seconds * SRATE)) / SRATE
        channel = dc + 20.0 * np.sin(2 * np.pi * 10.0 * t)
        return np.vstack([channel, channel + gap])

    def test_raw_window_fails_quality_gate(self):
        """不去直流：设备偏置直接顶穿幅度与通道跨度两条门槛（fix 之前的真机现状）。"""
        window = analyze_window(self._raw(), SRATE)
        self.assertFalse(window.usable)
        self.assertIn("amplitude", window.quality.reasons)
        self.assertIn("channel_span", window.quality.reasons)

    def test_conditioned_window_passes_quality_gate(self):
        expected = int(config.WINDOW_SEC * SRATE)
        conditioned = self.source._condition(self._raw(), expected)

        window = analyze_window(conditioned, SRATE)
        self.assertTrue(window.usable, f"调理后仍不可用：{window.quality.reasons}")
        self.assertEqual(conditioned.shape[0], 2)
        self.assertEqual(conditioned.shape[1], expected, "输出应恰好是一窗点数")
        for channel in conditioned:
            # 去直流在前、高通在后：残留直流应远小于 1 µV（实测 0.05 µV 量级）
            self.assertLess(abs(float(np.mean(channel))), 1.0, "逐通道去直流应把均值压到 0 附近")
        self.assertLess(float(np.max(np.abs(conditioned))), config.QUALITY["amp_max_uv"])
        # 10 Hz 落在 alpha 带里，调理不该把它滤掉
        self.assertGreater(window.rel.get("alpha", 0.0), 0.5)

    def test_conditioning_record_is_auditable(self):
        conditioned = self.source._condition(self._raw(), 400)
        self.assertEqual(conditioned.shape[1], 400)
        record = self.source.conditioning()
        self.assertEqual(record["spec"], "acq-condition-v1")
        self.assertEqual(record["dc_removal"], "per_channel_dc_removal")
        stages = [stage["stage"] for stage in record["chain"]["stages"]]
        self.assertEqual(stages[0], "drift_correction")
        self.assertEqual(stages[-1], "band_pass")
        self.assertEqual(record["expected_samples"], 400)
        self.assertEqual(record["context_samples"], int(6.0 * SRATE))
        self.assertIn("conditioning", self.source.status())

    def test_railed_device_is_reported_as_flat_not_as_amplitude(self):
        """设备贴轨（真机实测两通道恒为 375000.0）应判 flat_channel，而不是幅度越界。"""
        railed = np.full((2, int(6.0 * SRATE)), 375_000.0)
        conditioned = self.source._condition(railed, int(config.WINDOW_SEC * SRATE))
        window = analyze_window(conditioned, SRATE)
        self.assertFalse(window.usable)
        self.assertEqual(window.quality.reasons, ("flat_channel",))
        self.assertNotIn("amplitude", window.quality.reasons)

    def test_short_context_is_not_padded(self):
        """缓冲不足时不能凭空补零：返回多少点就是多少点。"""
        conditioned = self.source._condition(self._raw(seconds=1.0), int(config.WINDOW_SEC * SRATE))
        self.assertEqual(conditioned.shape[1], int(1.0 * SRATE))


class SimulatedOutletLabellingTest(unittest.TestCase):
    """内置仿真 outlet 走的是真 LSL 传输，但文案必须写明"不是真实设备"。"""

    def test_simulated_outlet_note_is_explicit(self):
        from ningsi_studio.core.live_source import _is_sim_outlet, _lsl_note

        self.assertTrue(_is_sim_outlet("ningsi-sim-outlet-v1"))
        self.assertFalse(_is_sim_outlet(""))
        self.assertFalse(_is_sim_outlet("BioMulti-00cde1"))

        simulated = _lsl_note({"source_id": "ningsi-sim-outlet-v1", "channel_count": 2,
                               "channel_labels": ["Fp1", "Fp2"]}, "ningsi-sim-eeg", 250.0)
        self.assertIn("内置仿真 LSL 流", simulated)
        self.assertIn("非真实设备", simulated)

        hardware = _lsl_note({"source_id": "", "channel_count": 2,
                              "channel_labels": ["Fp1", "Fp2"]}, "BioMulti Lite EEG-00cde1", 250.0)
        self.assertIn("真实 LSL 流", hardware)
        self.assertNotIn("非真实设备", hardware)


if __name__ == "__main__":
    unittest.main()
