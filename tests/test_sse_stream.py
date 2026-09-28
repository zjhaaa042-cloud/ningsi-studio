"""SSE 事件流：运行中会话的 phase/window/progress、非运行会话的快照与 closed、
以及 `Last-Event-ID` 重放。"""

from __future__ import annotations

import time

try:                                              # 支持直接以脚本方式运行本文件
    from tests.helpers import QUICK_TIME_SCALE, StudioTestCase
except ModuleNotFoundError:                       # pragma: no cover - 仅脚本运行场景
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.helpers import QUICK_TIME_SCALE, StudioTestCase


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


if __name__ == "__main__":                        # pragma: no cover - 便于单文件调试
    import unittest

    unittest.main()
