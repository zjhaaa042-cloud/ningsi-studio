"""SSE 事件流：运行中会话的 phase/window/progress、非运行会话的快照与 closed、
`Last-Event-ID` 重放，以及高频信号流（/signal）与实时信号注册表的回收。"""

from __future__ import annotations

import http.client
import json
import time

try:                                              # 支持直接以脚本方式运行本文件
    from tests.helpers import QUICK_TIME_SCALE, StudioTestCase
except ModuleNotFoundError:                       # pragma: no cover - 仅脚本运行场景
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.helpers import QUICK_TIME_SCALE, StudioTestCase

from ningsi_studio.api.routes import StudioSessionManager
from ningsi_studio.core import signal_feed


class EventStreamRunningTests(StudioTestCase):
    """订阅运行中的会话：事件类型、结构与重放语义。"""

    port_base = 18940

    def test_running_session_streams_phase_window_progress(self) -> None:
        session = self.create_session("n01")
        uuid = session["uuid"]

        connection, response = self.open_event_stream(uuid, timeout=8.0)
        try:
            self.assertEqual(response.status, 200, "SSE 订阅应返回 200")
            content_type = response.getheader("Content-Type") or ""
            self.assertIn("text/event-stream", content_type,
                          "SSE 的 Content-Type 应为 text/event-stream")

            def ready(events: list) -> bool:
                types = {event["type"] for event in events}
                has_signal = any(event["type"] == "window"
                                 and isinstance((event.get("data") or {}).get("signal"), dict)
                                 for event in events)
                return "phase" in types and "progress" in types and has_signal

            events = self.read_event_stream(response, stop=ready, timeout=30.0, max_events=400)
        finally:
            self.close_event_stream(connection)

        types = [event["type"] for event in events]
        self.assertIn("phase", types, "运行中会话应推送 phase 事件")
        self.assertIn("progress", types, "运行中会话应推送 progress 事件")
        for event in events:
            self.assertEqual(event.get("session"), uuid, "事件应带 session 字段")
            self.assertIsInstance(event.get("id"), int, "事件应带自增整数 id")

        phase_events = [event for event in events if event["type"] == "phase"]
        for payload in (event["data"] for event in phase_events):
            for key in ("key", "label", "state", "progress"):
                self.assertIn(key, payload, f"phase 事件应含 {key}")
            self.assertIn(payload["state"], ("running", "done"), "phase.state 应为 running/done")

        progress_events = [event for event in events if event["type"] == "progress"]
        for payload in (event["data"] for event in progress_events):
            self.assertIn("key", payload, "progress 事件应含 key")
            self.assertIn("progress", payload, "progress 事件应含 progress")
            self.assertGreaterEqual(payload["progress"], 0.0, "progress 不应为负")
            self.assertLessEqual(payload["progress"], 1.0, "progress 不应大于 1")

        windows = [event for event in events if event["type"] == "window"]
        self.assertTrue(windows, "运行中会话应推送 window 事件")
        sample = next((event for event in windows
                       if isinstance((event.get("data") or {}).get("signal"), dict)), None)
        self.assertIsNotNone(sample, "至少有一个 window 事件应带 signal 波形")
        data = sample["data"]
        for key in ("index", "t_end", "state", "usable", "quality", "scores", "index_z",
                    "band_z", "reasons", "alerts", "signal"):
            self.assertIn(key, data, f"window 事件应含 {key}")
        self.assertIsInstance(data["usable"], bool, "usable 应为布尔值")
        self.assertIsInstance(data["reasons"], list, "reasons 应为数组")
        self.assertIsInstance(data["alerts"], list, "alerts 应为数组")
        self.assertIsInstance(data["scores"], dict, "scores 应为对象")
        for name, score in data["scores"].items():
            self.assertIn(name, ("focus", "relax", "load"), f"未知指标 {name}")
            self.assertGreaterEqual(score, 0.0, f"{name} 评分不应为负")
            self.assertLessEqual(score, 1.0, f"{name} 评分不应大于 1")

        quality = data["quality"]
        self.assertIsInstance(quality, dict, "quality 应为对象")
        for key in ("ok", "reasons", "metrics"):
            self.assertIn(key, quality, f"window.quality 应含 {key}")

        signal = data["signal"]
        for key in ("srate", "channels", "samples", "relative", "time_domain"):
            self.assertIn(key, signal, f"signal 应含 {key}")
        self.assertEqual(signal["channels"], 1, "仿真源应为单通道")
        self.assertTrue(signal["samples"], "signal.samples 不应为空")
        for value in signal["samples"]:
            self.assertIsInstance(value, (int, float), "signal.samples 应为数值数组")

    def test_last_event_id_replays_missed_events(self) -> None:
        session = self.create_session("n02")
        uuid = session["uuid"]
        time.sleep(1.0)                                   # 先让总线积累一批事件

        connection, response = self.open_event_stream(uuid, timeout=8.0)
        try:
            first = self.read_event_stream(response, count=1, timeout=15.0)
        finally:
            self.close_event_stream(connection)
        self.assertTrue(first, "订阅后应立刻收到事件（补发或实时）")
        start_id = first[0]["id"]
        self.assertIsInstance(start_id, int, "事件 id 应为整数")

        time.sleep(1.5)
        detail = self.session_detail(uuid)
        self.assertEqual(detail["status"], "running", "重放用例期间会话应仍在运行")
        self.assertTrue(detail["runtime"]["alive"], "重放用例期间运行器应存活")

        connection, response = self.open_event_stream(uuid, last_event_id=start_id, timeout=8.0)
        try:
            replayed = self.read_event_stream(response, count=3, timeout=20.0)
        finally:
            self.close_event_stream(connection)

        self.assertGreaterEqual(len(replayed), 3, "带 Last-Event-ID 重连应补发漏掉的事件")
        ids = [event["id"] for event in replayed]
        self.assertTrue(min(ids) > start_id, f"补发事件的 id 应全部大于 {start_id}：{ids}")
        self.assertEqual(ids, list(range(ids[0], ids[0] + len(ids))),
                         "补发事件 id 应连续，不丢帧")


class EventStreamSnapshotTests(StudioTestCase):
    """订阅非运行中的会话：快照 + closed。"""

    port_base = 18942

    def test_finished_session_snapshot_stream(self) -> None:
        session = self.create_session("n11", time_scale=1.0)
        uuid = session["uuid"]
        self.cancel_session_via_api(uuid)
        detail = self.wait_for(lambda: self.session_detail(uuid),
                               lambda item: item.get("status") != "running",
                               timeout=25.0, message="取消后会话应离开 running")
        self.assertEqual(detail["status"], "cancelled", "取消后状态应为 cancelled")

        connection, response = self.open_event_stream(uuid, timeout=8.0)
        try:
            self.assertEqual(response.status, 200, "非运行会话的快照订阅应返回 200")
            events = self.read_event_stream(
                response,
                stop=lambda items: any(event["type"] == "closed" for event in items),
                timeout=15.0)
        finally:
            self.close_event_stream(connection)

        types = [event["type"] for event in events]
        self.assertIn("phase", types, "非运行会话订阅后应立即收到 phase 快照")
        self.assertIn("closed", types, "总线已关闭时应收到 closed 事件，避免长连接挂死")
        self.assertIn("finished", types, "非运行会话订阅后应立即收到 finished 快照")

        phase = next(event for event in events if event["type"] == "phase")["data"]
        for key in ("key", "label", "state", "progress"):
            self.assertIn(key, phase, f"phase 快照应含 {key}")
        self.assertEqual(phase["state"], "cancelled", "phase 快照的 state 应是会话最终状态")

        finished = next(event for event in events if event["type"] == "finished")["data"]
        self.assertEqual(finished["status"], "cancelled", "finished 快照应给出最终状态")
        self.assertTrue(finished.get("snapshot"), "快照 finished 应带 snapshot=true")

    def test_last_event_id_replays_full_snapshot(self) -> None:
        session = self.create_session("n12", time_scale=1.0)
        uuid = session["uuid"]
        self.cancel_session_via_api(uuid)
        self.wait_for(lambda: self.session_detail(uuid),
                      lambda item: item.get("status") != "running",
                      timeout=25.0, message="取消后会话应离开 running")

        connection, response = self.open_event_stream(uuid, last_event_id=0, timeout=8.0)
        try:
            events = self.read_event_stream(
                response,
                stop=lambda items: any(event["type"] == "closed" for event in items),
                timeout=15.0)
        finally:
            self.close_event_stream(connection)

        types = [event["type"] for event in events]
        self.assertIn("phase", types, "全量重放应包含 phase 快照")
        self.assertIn("finished", types, "全量重放应包含 finished 快照")
        ids = [event["id"] for event in events if event["type"] != "closed"]
        self.assertEqual(ids, sorted(ids), "重放事件应按 id 递增")


class EventStreamLiveFallbackTests(StudioTestCase):
    """SSE 之外的轮询兜底端点。"""

    port_base = 18944

    def test_live_snapshot_fields(self) -> None:
        session = self.create_session("n21", time_scale=QUICK_TIME_SCALE)
        payload = self.json_body(self.get(f"/api/sessions/{session['uuid']}/live"), 200,
                                 "live 快照应返回 200")
        for key in ("uuid", "status", "phase", "progress", "indicator_summary", "alerts",
                    "awaiting_input", "runtime_alive"):
            self.assertIn(key, payload, f"live 快照应含 {key}")
        self.assertEqual(payload["uuid"], session["uuid"], "应回显会话 uuid")
        self.assertTrue(payload["runtime_alive"], "会话运行中 runtime_alive 应为 true")
        self.assertIsInstance(payload["alerts"], list, "alerts 应为数组")
        self.assertIsInstance(payload["awaiting_input"], list, "awaiting_input 应为数组")


class SignalStreamTests(StudioTestCase):
    """`/signal` 高频信号流：帧契约、NaN/Inf 净化、非运行会话的统一 409 错误体。"""

    port_base = 18946

    def open_signal_stream(self, session_uuid: str, query: str = ""):
        """打开 `/signal` 长连接（helpers 的 open_event_stream 只指向 /events）。"""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10.0)
        connection.request("GET", f"/api/sessions/{session_uuid}/signal{query}",
                           headers={"Accept": "text/event-stream"})
        return connection, connection.getresponse()

    @staticmethod
    def read_first_signal_frame(response, timeout: float = 15.0):
        """读到第一帧 signal，并返回 `(帧对象, 原始 SSE 文本)`。

        原始文本是必须的：Python 的 `json.loads` **接受** 裸 `NaN`/`Infinity`，
        只看解析结果无法发现"浏览器会整帧丢弃"这个问题。
        """
        raw_lines: list = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = response.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line or line.startswith(":"):
                continue
            raw_lines.append(line)
            if line.startswith("data:"):
                return json.loads(line[5:].strip()), "\n".join(raw_lines)
        raise AssertionError(f"未在 {timeout}s 内读到 signal 帧；已读到 {raw_lines[:20]}")

    def test_signal_frames_match_contract_and_have_no_nan(self) -> None:
        session = self.create_session("g01", time_scale=1.0)
        uuid = session["uuid"]
        connection, response = self.open_signal_stream(uuid, "?hz=5&seconds=2&points=200")
        try:
            self.assertEqual(response.status, 200, "运行中会话的 /signal 应返回 200")
            self.assertIn("text/event-stream", response.getheader("Content-Type") or "",
                          "/signal 的 Content-Type 应为 text/event-stream")
            frame, raw_text = self.read_first_signal_frame(response)
        finally:
            self.close_event_stream(connection)

        self.assertNotIn("NaN", raw_text, "SSE 帧里不能有裸 NaN（浏览器 JSON.parse 会丢弃整帧）")
        self.assertNotIn("Infinity", raw_text, "SSE 帧里不能有裸 Infinity")
        self.assertEqual(frame["type"], "signal", "信号帧类型应为 signal")
        self.assertEqual(frame["session"], uuid, "信号帧应带本会话 uuid")
        self.assertEqual(frame["refresh_hz"], 5.0, "refresh_hz 应回显请求的 5 FPS（retune 生效）")
        self.assertEqual(frame["window_sec"], 2.0, "window_sec 应回显请求的 2 秒")
        self.assertTrue(frame["realtime"], "仿真源应有实时数据")
        self.assertEqual(frame["source_kind"], "sim", "仿真会话的 source_kind 应为 sim")
        self.assertGreaterEqual(frame["channel_count"], 1, "至少应有一路通道")
        channel = frame["channels"][0]
        for key in ("index", "label", "unit", "peak", "points", "raw_points", "samples"):
            self.assertIn(key, channel, f"通道帧应含 {key}")
        self.assertTrue(channel["samples"], "通道波形不应为空")
        self.assertLessEqual(len(channel["samples"]), 400,
                             "points=200 时 min/max 抽稀后点数不应超过 2×200")

    def test_signal_for_non_running_session_returns_conflict(self) -> None:
        session = self.create_session("g02", time_scale=1.0)
        uuid = session["uuid"]
        self.cancel_session_via_api(uuid)
        self.wait_for(lambda: self.session_detail(uuid),
                      lambda item: not item["runtime"]["alive"], timeout=25.0,
                      message="取消后运行线程应退出")

        error = self.assert_error(self.get(f"/api/sessions/{uuid}/signal"), 409, "conflict",
                                  "非运行会话的 /signal 应返回统一 409 错误体（不再手拼响应体）")
        self.assertIn("运行", error["message"], "409 消息应说明会话不在运行中")


class SignalFeedRegistryTests(StudioTestCase):
    """实时信号注册表不随会话数增长：会话结束与应用停止都必须回收 feed。"""

    port_base = 18948

    @staticmethod
    def _probe_feed(registry, session_uuid: str):
        return registry.get_or_create(
            session_uuid,
            lambda: signal_feed.SignalFeed(session_uuid, lambda seconds: None,
                                           srate=250.0, channels=1))

    def test_feed_reclaimed_after_session_ends(self) -> None:
        session = self.create_session("g11", time_scale=1.0)
        uuid = session["uuid"]
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10.0)
        connection.request("GET", f"/api/sessions/{uuid}/signal?hz=5",
                           headers={"Accept": "text/event-stream"})
        response = connection.getresponse()
        try:
            self.assertEqual(response.status, 200, "运行中会话的 /signal 应返回 200")
            deadline = time.time() + 15
            while time.time() < deadline:
                raw = response.readline()
                if not raw or raw.startswith(b"data:"):
                    break
            self.assertIsNotNone(signal_feed.REGISTRY.get(uuid),
                                 "推流期间注册表里应有该会话的 feed")
        finally:
            self.close_event_stream(connection)

        self.cancel_session_via_api(uuid)
        self.wait_for(lambda: signal_feed.REGISTRY.get(uuid),
                      lambda feed: feed is None, timeout=25.0,
                      message="会话结束后 feed 必须从注册表回收（否则每个开过实时监测的会话都留一份）")

    def test_app_shutdown_stops_registry_feeds(self) -> None:
        """应用停止路径：`SessionManager.shutdown()` 必须顺带 `stop_all()`。"""
        probe_uuid = "shutdown-probe"
        feed = self._probe_feed(signal_feed.REGISTRY, probe_uuid)
        self.assertIsNotNone(signal_feed.REGISTRY.get(probe_uuid), "探针 feed 应已登记")

        StudioSessionManager(self.app.settings).shutdown()

        self.assertIsNone(signal_feed.REGISTRY.get(probe_uuid),
                          "shutdown() 后注册表应被清空（stop_all）")
        self.assertTrue(feed._stop.is_set(), "shutdown() 必须停掉 feed 的推帧线程")


if __name__ == "__main__":                        # pragma: no cover - 便于单文件调试
    import unittest

    unittest.main()
