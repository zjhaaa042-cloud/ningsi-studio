"""会话接口：创建（201 与字段）/ 非法 time_scale(422) / 列表过滤 / 详情（runs+alerts）/
取消非运行会话(409) / 并发上限(429) / 产物下载响应头 / 模型训练与台账趋势。"""

from __future__ import annotations

from pathlib import Path

try:                                              # 支持直接以脚本方式运行本文件
    from tests.helpers import PHASE_KEYS, QUICK_TIME_SCALE, StudioTestCase
except ModuleNotFoundError:                       # pragma: no cover - 仅脚本运行场景
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.helpers import PHASE_KEYS, QUICK_TIME_SCALE, StudioTestCase

from ningsi import config
from ningsi_studio import bootstrap


class SessionCreateTests(StudioTestCase):
    """创建会话的响应结构与参数校验。"""

    port_base = 18920

    def test_create_session_returns_201_with_fields(self) -> None:
        payload = self.json_body(self.post("/api/sessions", {
            "participant": "m01", "device": "sim-bsense", "time_scale": QUICK_TIME_SCALE,
            "training_mode": "quick", "label": "第一次训练",
        }), 201, "创建会话应返回 201")

        session = payload["session"]
        self.assertRegex(session["uuid"], r"^[0-9a-f]{32}$", "会话 uuid 应为 32 位十六进制")
        self.assertEqual(session["participant"], "m01", "应回显被试编号")
        self.assertEqual(session["status"], "running", "新建会话状态应为 running")
        self.assertIn(session["phase"], PHASE_KEYS, "phase 应为已定义阶段之一")
        self.assertTrue(session["phase_label"], "应给出阶段中文名")
        self.assertEqual(session["device"], "sim-bsense", "应回显设备名")
        self.assertEqual(session["source"], "sim-bsense", "仿真设备的 source 应为 sim-bsense")
        self.assertEqual(session["time_scale"], QUICK_TIME_SCALE, "应回显时间倍率")
        self.assertEqual(session["training_mode"], "quick", "应回显训练模式")
        self.assertEqual(session["srate"], 250.0, "默认采样率应为 250 Hz")
        self.assertEqual(session["channels"], 1, "默认通道数应为 1")
        self.assertEqual(session["engine_versions"], bootstrap.engine_versions(),
                         "会话应固化引擎口径版本")
        self.assertTrue(session["started_at"], "started_at 不应为空")
        self.assertIsNone(session["ended_at"], "未结束时 ended_at 应为 null")
        self.assertIsNone(session["error"], "未失败时 error 应为 null")
        self.assertIsInstance(session["progress"], float, "progress 应为小数")
        self.assertGreaterEqual(session["progress"], 0.0, "progress 应不小于 0")
        self.assertLessEqual(session["progress"], 1.0, "progress 应不大于 1")

        self.assertEqual(payload["events_url"], f"/api/sessions/{session['uuid']}/events",
                         "events_url 应指向该会话的 SSE 地址")
        self.assertEqual([item["key"] for item in payload["phases"]], list(PHASE_KEYS),
                         "phases 应与后端阶段定义一致")
        self.assertTrue(payload["runtime"]["alive"], "创建后运行器应处于存活状态")
        self.assertEqual(payload["runtime"]["source"], "sim-bsense", "运行器应报告仿真数据源")

    def test_create_session_requires_participant(self) -> None:
        self.assert_error(self.post("/api/sessions", {"time_scale": QUICK_TIME_SCALE}),
                          422, "validation_failed", "缺少 participant 应返回 422")

    def test_create_session_unknown_participant_returns_404(self) -> None:
        self.assert_error(self.post("/api/sessions", {"participant": "z99", "create_subject": False}),
                          404, "not_found", "create_subject=false 且被试不存在应返回 404")

    def test_create_session_rejects_out_of_range_time_scale(self) -> None:
        for value in (1.5, 0.001, -1):
            with self.subTest(time_scale=value):
                self.assert_error(
                    self.post("/api/sessions", {"participant": "y01", "time_scale": value}),
                    422, "validation_failed", f"time_scale={value} 越界应返回 422")
        self.assert_error(self.post("/api/sessions", {"participant": "y01", "time_scale": "fast"}),
                          422, "validation_failed", "time_scale 非数字应返回 422")

    def test_create_session_rejects_unknown_training_mode(self) -> None:
        self.assert_error(self.post("/api/sessions", {"participant": "y02",
                                                     "training_mode": "不存在的模式"}),
                          422, "validation_failed", "未知训练模式应返回 422")


class SessionListTests(StudioTestCase):
    """会话列表过滤与分页。"""

    port_base = 18922

    def test_list_sessions_filters(self) -> None:
        first = self.create_session("m11", time_scale=1.0)
        self.create_session("m12", time_scale=1.0)

        payload = self.json_body(self.get("/api/sessions?participant=m11"), 200,
                                 "按被试过滤应返回 200")
        self.assertGreaterEqual(payload["total"], 1, "应命中该被试的会话")
        for item in payload["items"]:
            self.assertEqual(item["participant"], "m11", "过滤结果应只含该被试")
            self.assertIn("alert_count", item, "列表项应含 alert_count")
        self.assertEqual(payload["items"][0]["uuid"], first["uuid"],
                         "最近创建的会话应排在前面")

        running = self.json_body(self.get("/api/sessions?status=running"), 200,
                                 "按状态过滤应返回 200")
        for item in running["items"]:
            self.assertEqual(item["status"], "running", "过滤结果应只含该状态")

        limited = self.json_body(self.get("/api/sessions?limit=1"), 200, "limit 生效应返回 200")
        self.assertEqual(limited["limit"], 1, "limit 应回显")
        self.assertEqual(len(limited["items"]), 1, "limit=1 时应只返回 1 条")

        empty = self.json_body(self.get("/api/sessions?participant=zz99"), 200,
                               "无匹配也应返回 200")
        self.assertEqual(empty["total"], 0, "未知被试应返回 0 条")
        self.assertEqual(empty["items"], [], "未知被试的 items 应为空数组")

    def test_list_sessions_invalid_participant_returns_422(self) -> None:
        self.assert_error(self.get("/api/sessions?participant=!!"), 422, "validation_failed",
                          "非法被试编号过滤应返回 422")

    def test_list_sessions_carries_phase_label(self) -> None:
        """列表页也要中文阶段名：详情接口有 phase_label，列表曾只回英文键。

        已知流程阶段必须给出中文名（与 core/phases.py 同表）；终态 done/error 不是流程阶段，
        回 None，由界面本地化（前端 phaseText 兜底）。
        """
        self.create_session("m13", time_scale=1.0)

        running = self.json_body(self.get("/api/sessions?participant=m13&status=running"), 200,
                                 "按状态过滤应返回 200")
        self.assertTrue(running["items"], "应命中运行中的会话")
        item = running["items"][0]
        self.assertIn("phase_label", item, "列表项应含 phase_label")
        self.assertNotEqual(item["phase_label"], item["phase"],
                            "进行中的阶段应给中文名，而不是回显英文键")
        self.assertRegex(item["phase_label"], r"[\u4e00-\u9fff]",
                         "phase_label 应是中文，实际：" + repr(item["phase_label"]))

    def test_detail_runtime_source_kind_survives_session_end(self) -> None:
        """会话结束后运行器可能被回收，source_kind 仍须可判（否则"仿真"标注消失）。

        反推只按命名约定：`lsl:` ⇒ lsl，仿真源键 ⇒ sim，其它为 None（不猜）。
        """
        from ningsi_studio.api import routes
        from ningsi_studio.core import live_source

        self.assertEqual(routes._source_kind_from_key(live_source.SIM_SOURCE), "sim")
        self.assertEqual(routes._source_kind_from_key("lsl:my-stream"), "lsl")
        self.assertIsNone(routes._source_kind_from_key(None))
        self.assertIsNone(routes._source_kind_from_key("weird"))

        session = self.create_session("m14", time_scale=0.05)
        self.cancel_session_via_api(session["uuid"])
        detail = self.wait_session_done(session["uuid"], timeout=30.0,
                                        expect=("cancelled", "failed", "done"))
        self.assertEqual(detail["runtime"]["source"], "sim-bsense", "结束的仿真会话仍应标明实际数据源")
        self.assertEqual(detail["runtime"]["source_kind"], "sim",
                         "source_kind 不能因为会话结束就变 null（顶栏会丢掉「仿真」标注）")


class SessionDetailTests(StudioTestCase):
    """会话详情：runs / alerts / runtime。"""

    port_base = 18924

    def test_detail_contains_runs_and_runtime(self) -> None:
        session = self.create_session("m21", time_scale=1.0)
        uuid = session["uuid"]
        detail = self.wait_for(lambda: self.session_detail(uuid),
                               lambda item: bool(item.get("runs")),
                               timeout=20.0, message="会话启动后应写出 runs 记录")

        self.assertEqual(detail["uuid"], uuid, "详情应回显 uuid")
        self.assertEqual(detail["participant"], "m21", "详情应回显被试编号")
        self.assertIn("alerts", detail, "详情应含 alerts")
        self.assertIsInstance(detail["alerts"], list, "alerts 应为数组")
        self.assertIn("artifacts", detail, "详情应含 artifacts")
        self.assertIsInstance(detail["artifacts"], list, "artifacts 应为数组")
        self.assertIsInstance(detail["indicator_summary"], dict, "indicator_summary 应为对象")

        runs = detail["runs"]
        self.assertIsInstance(runs, list, "runs 应为数组")
        self.assertTrue(runs, "runs 不应为空")
        self.assertEqual(runs[0]["phase"], "qc", "第一条 run 应为设备质检")
        for item in runs:
            for key in ("phase", "status", "duration_ms", "error", "payload"):
                self.assertIn(key, item, f"run 记录应含 {key}")

        runtime = detail["runtime"]
        for key in ("alive", "awaiting_input", "source", "source_kind"):
            self.assertIn(key, runtime, f"runtime 应含 {key}")
        self.assertTrue(runtime["alive"], "会话进行中 runtime.alive 应为 true")
        self.assertEqual(runtime["source_kind"], "sim", "runtime.source_kind 应为 sim")
        self.assertEqual(runtime["source"], "sim-bsense", "runtime.source 应为 sim-bsense")

    def test_unknown_session_returns_404(self) -> None:
        self.assert_error(self.get("/api/sessions/" + "f" * 32), 404, "not_found",
                          "未知会话应返回 404")

    def test_assessment_not_ready_returns_conflict(self) -> None:
        session = self.create_session("m22", time_scale=1.0)
        self.assert_error(self.get(f"/api/sessions/{session['uuid']}/assessment"),
                          409, "conflict", "评估未完成时应返回 409")

    def test_report_partial_before_finish(self) -> None:
        session = self.create_session("m23", time_scale=1.0)
        payload = self.json_body(self.get(f"/api/sessions/{session['uuid']}/report"), 202,
                                 "报告未生成时应返回 202 部分结果")
        self.assertTrue(payload["partial"], "部分结果应标记 partial=true")
        self.assertIn("runs", payload, "部分结果应带阶段进度")


class SessionCancelTests(StudioTestCase):
    """取消与会话状态冲突。"""

    port_base = 18926

    def test_cancel_running_session_then_second_cancel_conflicts(self) -> None:
        session = self.create_session("m31", time_scale=1.0)
        uuid = session["uuid"]
        payload = self.cancel_session_via_api(uuid)
        self.assertEqual(payload["uuid"], uuid, "取消响应应回显 uuid")

        detail = self.wait_for(lambda: self.session_detail(uuid),
                               lambda item: item.get("status") != "running",
                               timeout=20.0, message="取消后会话状态应离开 running")
        self.assertEqual(detail["status"], "cancelled", "取消耗时会在基线阶段中断，状态应为 cancelled")
        self.assertFalse(detail["runtime"]["alive"], "取消后 runtime.alive 应为 false")

        self.assert_error(self.delete(f"/api/sessions/{uuid}"), 409, "conflict",
                          "取消非运行中的会话应返回 409")


class SessionConcurrencyTests(StudioTestCase):
    """并发运行会话上限。"""

    port_base = 18928

    def test_concurrent_limit_returns_429(self) -> None:
        self.cancel_active_sessions()
        self.create_session("m41", time_scale=1.0)
        self.create_session("m42", time_scale=1.0)
        self.assertEqual(len(self.app.manager.active_uuids()), self.max_active_sessions,
                         "此时应恰好占满并发名额")

        response = self.post("/api/sessions", {"participant": "m43", "device": "sim-bsense",
                                               "time_scale": 1.0, "training_mode": "quick"})
        self.assert_error(response, 429, "too_many_requests", "并发超限应返回 429")

        # 预检在落库之前，因此被拒绝的请求不应留下任何会话行（含幽灵 running 行）
        rejected = self.json_body(self.get("/api/sessions?participant=m43"), 200,
                                 "按被试查询应返回 200")
        self.assertEqual(rejected["total"], 0, "429 之后不应为新被试落库任何会话")
        self.assertEqual([item for item in rejected["items"] if item["status"] == "running"], [],
                         "429 之后不应出现没有运行线程的 running 会话")


class ArtifactDownloadTests(StudioTestCase):
    """跑完一次快速演示后校验产物清单与下载响应头（耗时较长，约 1 分钟）。"""

    port_base = 18930

    def test_artifact_list_and_download_headers(self) -> None:
        session = self.create_session("m51")
        uuid = session["uuid"]
        detail = self.wait_session_done(uuid)
        self.assertEqual(detail["status"], "done", "快速演示会话应正常结束")

        items = self.json_body(self.get(f"/api/sessions/{uuid}/artifacts"), 200,
                               "产物清单应返回 200")["items"]
        kinds = {item["kind"] for item in items}
        for kind in ("report_md", "report_json", "heatmap_svg", "trend_svg", "model", "history"):
            self.assertIn(kind, kinds, f"产物清单应包含 {kind}")
        for item in items:
            self.assertEqual(item["download"], f"/api/sessions/{uuid}/artifacts/{item['kind']}",
                             "download 字段应指向该产物的下载地址")
            self.assertTrue(item["exists"], f"产物 {item['kind']} 的文件应存在：{item['path']}")
            self.assertGreater(item["bytes"], 0, f"产物 {item['kind']} 的字节数应大于 0")
            self.assertEqual(len(item["sha256"]), 64, f"产物 {item['kind']} 应带 sha256")

        response = self.get(f"/api/sessions/{uuid}/artifacts/report_json")
        self.assert_status(response, 200, "report_json 下载应返回 200")
        self.assertEqual(response.content_type, "application/json; charset=utf-8",
                         "report_json 的 Content-Type 应为 JSON")
        self.assertIn("attachment", response.content_disposition,
                      "下载应带 Content-Disposition: attachment")
        self.assertIn("filename=", response.content_disposition, "下载响应应带文件名")
        report = response.json()
        self.assertEqual(report["spec"], "joint-assessment-v1", "报告口径应为 joint-assessment-v1")
        self.assertIn("report_markdown", report, "报告应附带 Markdown 原文")
        self.assertTrue(report["report_markdown"].startswith("#"), "Markdown 原文应以标题开头")

        markdown = self.get(f"/api/sessions/{uuid}/artifacts/report_md")
        self.assert_status(markdown, 200, "report_md 下载应返回 200")
        self.assertIn("text/markdown", markdown.content_type, "report_md 应为 markdown 类型")

        svg = self.get(f"/api/sessions/{uuid}/artifacts/heatmap_svg")
        self.assert_status(svg, 200, "heatmap_svg 下载应返回 200")
        self.assertEqual(svg.content_type, "image/svg+xml", "热力图应为 SVG 类型")
        self.assertIn(b"<svg", svg.body, "热力图文件应是 SVG 内容")

        self.assert_error(self.get(f"/api/sessions/{uuid}/artifacts/unknown_kind"),
                          404, "not_found", "未知产物应返回 404")


class ModelAndTrendTests(StudioTestCase):
    """模型训练接口与跨会话趋势的 JSONL 台账（`ledger_points`）。"""

    port_base = 18932

    def test_train_model_returns_metrics_and_saves(self) -> None:
        payload = self.json_body(self.post("/api/models/train",
                                           {"subjects": 3, "windows_per_state": 2}),
                                 200, "模型训练接口应返回 200")
        for key in ("spec", "version", "features", "subject_split", "samples", "participants",
                    "train", "validation", "test", "model_path"):
            self.assertIn(key, payload, f"训练结果应含 {key}")
        self.assertEqual(payload["version"], config.VERSION,
                         "version 应来自引擎 config.VERSION")
        self.assertEqual(len(payload["participants"]), 3, "3 个仿真被试应全部记入 participants")
        self.assertEqual(payload["samples"], 3 * 2 * 2,
                         "样本数应为 被试数 × 状态数(2) × 每状态窗数")
        self.assertTrue(payload["features"], "应给出特征名列表")
        self.assertIsInstance(payload["train"], dict, "train 应为评估对象")
        self.assertTrue(Path(payload["model_path"]).exists(),
                        f"模型文件应落盘：{payload['model_path']}")

    def test_reports_trend_reads_jsonl_ledger(self) -> None:
        """台账按会话写在 runs/<uuid>/history/sessions.jsonl，`ledger_points` 不能恒为空。"""
        participant = self.create_subject(auto_id=True, label="台账被试")["public_id"]
        session = self.create_session(participant)
        uuid = session["uuid"]
        detail = self.wait_session_done(uuid)
        self.assertEqual(detail["status"], "done", "快速演示会话应正常结束")

        ledger = Path(self.data_dir) / "runs" / uuid / "history" / "sessions.jsonl"
        self.assertTrue(ledger.exists(), f"每个会话应写一份台账：{ledger}")

        trend = self.json_body(self.get("/api/reports/trend?field=focus&period=week"), 200,
                               "跨会话趋势应返回 200")
        self.assertIsInstance(trend["ledger_points"], list, "ledger_points 应为数组")
        self.assertTrue(trend["ledger_points"],
                        "台账在 runs/<uuid>/history/sessions.jsonl，ledger_points 不应为空")
        for point in trend["ledger_points"]:
            for key in ("period", "mean", "n", "std", "rejected"):
                self.assertIn(key, point, f"台账趋势点应含 {key}")
        self.assertEqual(sum(point["n"] for point in trend["ledger_points"]), 1,
                         "本用例只有一次完成会话，台账聚合的 n 应为 1（条数与记录数一致）")


if __name__ == "__main__":                        # pragma: no cover - 便于单文件调试
    import unittest

    unittest.main()
