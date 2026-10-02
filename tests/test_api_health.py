"""基础接口与统一错误体：/api/health、/api/config、/api/devices、/api/openapi.json。

全部通过真实 HTTP 服务访问（urllib），覆盖状态码、字段结构与错误体格式。
"""

from __future__ import annotations

try:                                              # 支持直接以脚本方式运行本文件
    from tests.helpers import PHASE_KEYS, StudioTestCase
except ModuleNotFoundError:                       # pragma: no cover - 仅脚本运行场景
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.helpers import PHASE_KEYS, StudioTestCase

from ningsi import config
from ningsi_studio import bootstrap
from ningsi_studio.api.routes import API_TITLE, API_VERSION


class HealthTests(StudioTestCase):
    """`/api/health` 的字段结构。"""

    port_base = 18900

    def test_health_structure(self) -> None:
        payload = self.json_body(self.get("/api/health"), 200, "健康检查应返回 200")

        self.assertEqual(payload["status"], "ok", "健康状态应为 ok")
        self.assertEqual(payload["service"], "ningsi-studio", "服务名应为 ningsi-studio")
        self.assertEqual(payload["version"], API_VERSION, "接口版本应与代码常量一致")
        self.assertEqual(payload["product"], config.VERSION, "产品版本应来自引擎 config.VERSION")
        self.assertEqual(payload["engine"], bootstrap.engine_versions(),
                         "engine 口径版本应与 bootstrap.engine_versions() 一致")
        self.assertTrue(str(payload["engine_path"]).endswith("__init__.py"),
                        "engine_path 应指向引擎包入口文件")
        self.assertEqual(payload["data_dir"], str(self.data_dir),
                         "data_dir 应为注入的临时数据目录")
        self.assertEqual(payload["db"], str(self.data_dir / "studio.sqlite3"),
                         "数据库应位于 data_dir 下的 studio.sqlite3")
        self.assertEqual(payload["runs_root"], str(self.data_dir / "runs"),
                         "runs_root 应为 data_dir 下的 runs（每次会话的产物与台账目录）")
        self.assertEqual(payload["max_active_sessions"], self.max_active_sessions,
                         "并发上限应与设置一致")
        self.assertIsInstance(payload["active_sessions"], list, "active_sessions 应为数组")

        overview = payload["overview"]
        for key in ("subjects", "sessions", "sessions_done", "alerts", "latest"):
            self.assertIn(key, overview, f"overview 应含 {key}")
        self.assertIsInstance(overview["latest"], list, "overview.latest 应为数组")
        self.assertIsInstance(overview["subjects"], int, "overview.subjects 应为整数")

    def test_health_engine_versions_are_documented_specs(self) -> None:
        engine = self.json_body(self.get("/api/health"), 200, "健康检查应返回 200")["engine"]
        self.assertEqual(engine["spectrum"], config.SPECTRUM_SPEC, "处理链口径应为 welch-v1")
        self.assertEqual(engine["indicator"], config.INDICATOR_SPEC, "指标口径应为 indicator-v1")
        self.assertEqual(engine["baseline"], config.BASELINE_SPEC, "基线口径应为 baseline-v1")
        self.assertEqual(engine["assessment"], config.LABEL_SPEC, "评估口径应为 joint-assessment-v1")


class ConfigTests(StudioTestCase):
    """`/api/config` 的引擎口径全量定义。"""

    port_base = 18902

    def test_config_structure(self) -> None:
        payload = self.json_body(self.get("/api/config"), 200, "配置接口应返回 200")

        self.assertEqual(payload["version"], config.VERSION, "版本应来自引擎 config.VERSION")
        self.assertEqual(payload["specs"], bootstrap.engine_versions(),
                         "specs 应与 bootstrap.engine_versions() 一致")
        self.assertEqual(payload["window_sec"], 4.0, "分析窗长应为 4.0 秒")
        self.assertEqual(payload["step_sec"], 2.0, "步长应为 2.0 秒")
        self.assertEqual(payload["welch"]["window_sec"], 4.0, "Welch 窗长应与契约一致")
        self.assertEqual(payload["welch"]["overlap"], 0.5, "Welch 重叠率应为 50%")
        self.assertEqual(payload["total_band"], [0.5, 45.0], "总频带应为 0.5–45 Hz")

        self.assertEqual(sorted(payload["bands"]), ["alpha", "beta", "gamma", "low", "theta"],
                         "频带定义应含 5 个频带")
        for name, band in payload["bands"].items():
            self.assertEqual(len(band), 2, f"频带 {name} 应给出上下限")
        self.assertEqual(payload["indicators"],
                         {"focus": ["beta", "theta"], "relax": ["alpha", "beta"],
                          "load": ["theta", "alpha"]},
                         "三个指标的频带口径应固定")
        self.assertEqual(payload["score_gain"], 1.1, "评分斜率应为 1.1")
        self.assertEqual(payload["baseline_protocol"]["eyes_open_sec"], 120.0,
                         "睁眼基线应按协议采集 120 秒")
        self.assertEqual(payload["baseline_protocol"]["task_reference"], "eyes_open",
                         "任务态应以睁眼基线为参照")

        for key in ("quality", "alerts", "assess", "training"):
            self.assertIsInstance(payload[key], dict, f"{key} 应为对象")

        bands = payload["heatmap_bands"]
        self.assertEqual(len(bands), 5, "热力图应为五档")
        for item in bands:
            for key in ("low", "high", "label", "color"):
                self.assertIn(key, item, f"热力图分档应含 {key}")
        self.assertEqual(payload["scale_boundary"],
                         {"normal_max": 49, "mild_max": 59, "moderate_max": 69},
                         "量表分级边界应与引擎一致")

        phases = payload["phases"]
        self.assertEqual([item["key"] for item in phases], list(PHASE_KEYS),
                         "阶段顺序应与后端定义完全一致")
        for item in phases:
            for key in ("label", "interactive", "weight", "description"):
                self.assertIn(key, item, f"阶段 {item['key']} 应含 {key}")
        self.assertAlmostEqual(sum(item["weight"] for item in phases), 1.0, places=6,
                               msg="阶段权重之和应为 1")

        # 会话流程页的"当前该做什么"直接消费这些字段：缺了就退化成一屏没有指引的列表，
        # 所以这里把契约固定下来（headline / details / duration_sec / advance / next_hint）。
        for item in phases:
            for key in ("headline", "details", "duration_sec", "advance", "next_hint", "auto_note"):
                self.assertIn(key, item, f"阶段 {item['key']} 应含引导字段 {key}")
            self.assertTrue(str(item["headline"]).strip(),
                            f"阶段 {item['key']} 的 headline 不能为空（界面要直接显示给被试）")
            self.assertIsInstance(item["details"], list,
                                  f"阶段 {item['key']} 的 details 应是字符串列表")
            self.assertGreater(float(item["duration_sec"]), 0,
                               f"阶段 {item['key']} 应给出预计时长")
            self.assertIn(item["advance"], ("auto", "subject", "operator"),
                          f"阶段 {item['key']} 的推进方式取值非法")
        # 需要作答的阶段必须标成 subject，且交互标记要与之一致
        for item in phases:
            if item["interactive"]:
                self.assertEqual(item["advance"], "subject",
                                 f"交互阶段 {item['key']} 的推进方式应为 subject")

        privacy = payload["privacy"]
        self.assertIn("subject_id", privacy, "隐私声明应含被试编号说明")
        self.assertIn("boundary", privacy, "隐私声明应含结论边界原文")


class DevicesTests(StudioTestCase):
    """`/api/devices` 可用数据源。"""

    port_base = 18904

    def test_devices_structure(self) -> None:
        payload = self.json_body(self.get("/api/devices?probe=0.2"), 200, "设备列表应返回 200")
        sources = payload["sources"]
        self.assertIsInstance(sources, list, "sources 应为数组")
        self.assertTrue(sources, "至少应给出一个仿真数据源")

        sim = sources[0]
        self.assertEqual(sim["key"], "sim-bsense", "仿真源 key 应为 sim-bsense")
        self.assertEqual(sim["kind"], "sim", "仿真源 kind 应为 sim")
        self.assertEqual(sim["device"], "sim-bsense", "仿真源 device 应为 sim-bsense")
        self.assertEqual(sim["srate"], 250.0, "仿真源采样率应为 250 Hz")
        self.assertEqual(sim["channels"], 1, "仿真源通道数应为 1")
        self.assertFalse(sim["real"], "仿真源不应标记为真实设备")
        self.assertIn("note", sim, "数据源应带来源说明")
        if len(sources) == 1:
            self.assertIn("hardware_note", sim, "无真实 LSL 流时应给出硬件提示")

    def test_devices_probe_must_be_number(self) -> None:
        response = self.get("/api/devices?probe=abc")
        self.assert_error(response, 422, "validation_failed", "probe 非数字应返回 422")


class OverviewTests(StudioTestCase):
    """`/api/overview` 首页总览（在此之前零覆盖）。"""

    port_base = 18910

    def test_overview_structure(self) -> None:
        self.create_subject(auto_id=True, label="总览被试")
        session = self.create_session("o01", time_scale=1.0)

        payload = self.json_body(self.get("/api/overview"), 200, "总览接口应返回 200")
        for key in ("subjects", "sessions", "sessions_done", "alerts", "latest",
                    "latest_detail", "subjects_recent", "active_sessions", "source_note"):
            self.assertIn(key, payload, f"总览应含 {key}")
        self.assertGreaterEqual(payload["subjects"], 1, "至少应统计到刚建的被试")
        self.assertGreaterEqual(payload["sessions"], 1, "至少应统计到刚建的会话")
        self.assertIn(session["uuid"], payload["active_sessions"],
                      "运行中的会话应出现在 active_sessions")

        latest = payload["latest_detail"]
        self.assertTrue(latest, "latest_detail 不应为空（刚建了会话）")
        self.assertEqual(latest[0]["uuid"], session["uuid"], "最新会话应排在最前")
        for key in ("uuid", "participant", "status", "phase", "source"):
            self.assertIn(key, latest[0], f"latest_detail 项应含 {key}")

        recent = payload["subjects_recent"]
        self.assertIsInstance(recent, list, "subjects_recent 应为数组")
        self.assertTrue(recent, "应至少给出一个最近被试")
        for key in ("public_id", "label", "consent_version"):
            self.assertIn(key, recent[0], f"最近被试应含 {key}")
        self.assertTrue(payload["source_note"], "总览应给出当前数据源的说明文案")


class DeviceStatusTests(StudioTestCase):
    """`/api/devices/status` 运行中会话的设备体检（在此之前零覆盖）。"""

    port_base = 18912

    def test_device_status_reports_active_sources(self) -> None:
        session = self.create_session("d01", time_scale=1.0)
        uuid = session["uuid"]

        payload = self.json_body(self.get("/api/devices/status"), 200, "设备体检应返回 200")
        for key in ("active_sessions", "devices", "sources"):
            self.assertIn(key, payload, f"设备体检应含 {key}")
        self.assertIn(uuid, payload["active_sessions"], "运行中的会话应出现在 active_sessions")

        item = next((one for one in payload["devices"] if one["session"] == uuid), None)
        self.assertIsNotNone(item, "运行中的会话应有一条设备记录")
        for key in ("session", "key", "kind", "device", "srate", "channels", "note", "live"):
            self.assertIn(key, item, f"设备记录应含 {key}")
        self.assertEqual(item["key"], "sim-bsense", "仿真会话的设备 key 应为 sim-bsense")
        self.assertEqual(item["kind"], "sim", "仿真会话的设备 kind 应为 sim")
        self.assertEqual(item["device"], "sim-bsense", "应回显仿真设备名")
        self.assertTrue(item["live"], "仿真源没有硬件，应直接标记 live=true")

        sources = payload["sources"]
        self.assertTrue(sources, "设备体检应顺带列出可用数据源")
        self.assertEqual(sources[0]["key"], "sim-bsense", "首个数据源应为内置仿真源")


class OpenApiTests(StudioTestCase):
    """`/api/openapi.json` 与文档路径的一致性。"""

    port_base = 18906

    # (路径, 方法)：与 docs/API.md 逐一核对
    DOCUMENTED = (
        ("get", "/api/health"), ("get", "/api/config"), ("get", "/api/devices"),
        ("get", "/api/overview"),
        ("get", "/api/subjects"), ("post", "/api/subjects"),
        ("get", "/api/subjects/{public_id}"), ("patch", "/api/subjects/{public_id}"),
        ("get", "/api/subjects/{public_id}/sessions"),
        ("post", "/api/sessions"), ("get", "/api/sessions"),
        ("get", "/api/sessions/{uuid}"), ("delete", "/api/sessions/{uuid}"),
        ("get", "/api/sessions/{uuid}/events"), ("get", "/api/sessions/{uuid}/live"),
        ("get", "/api/sessions/{uuid}/heatmap"), ("get", "/api/sessions/{uuid}/trend"),
        ("get", "/api/scales"), ("get", "/api/scales/{code}"),
        ("post", "/api/sessions/{uuid}/scales/{code}"),
        ("get", "/api/sessions/{uuid}/behaviors/sart/sequence"),
        ("post", "/api/sessions/{uuid}/behaviors/sart/trial"),
        ("get", "/api/sessions/{uuid}/behaviors/pvt/sequence"),
        ("post", "/api/sessions/{uuid}/behaviors/pvt/trial"),
        ("get", "/api/sessions/{uuid}/behaviors/{task}/result"),
        ("get", "/api/sessions/{uuid}/assessment"), ("get", "/api/sessions/{uuid}/training"),
        ("get", "/api/sessions/{uuid}/report"), ("get", "/api/sessions/{uuid}/artifacts"),
        ("get", "/api/sessions/{uuid}/artifacts/{kind}"),
        ("get", "/api/sessions/{uuid}/export.zip"),
        ("post", "/api/models/train"), ("get", "/api/reports/trend"),
        ("get", "/api/openapi.json"),
    )

    def test_openapi_structure(self) -> None:
        payload = self.json_body(self.get("/api/openapi.json"), 200, "OpenAPI 描述应返回 200")
        self.assertEqual(payload["openapi"], "3.0.3", "OpenAPI 版本应为 3.0.3")
        self.assertEqual(payload["info"]["title"], API_TITLE, "描述标题应与代码常量一致")
        self.assertEqual(payload["info"]["version"], API_VERSION, "描述版本应与接口版本一致")
        self.assertIn("schemas", payload["components"], "components 应含 schemas")
        self.assertIn("Error", payload["components"]["schemas"], "应描述统一错误体 Error")
        self.assertEqual([item["key"] for item in payload["x-phases"]], list(PHASE_KEYS),
                         "x-phases 应与后端阶段定义一致")

    def test_openapi_covers_documented_paths(self) -> None:
        paths = self.json_body(self.get("/api/openapi.json"), 200, "OpenAPI 描述应返回 200")["paths"]
        for method, path in self.DOCUMENTED:
            self.assertIn(path, paths, f"OpenAPI 应描述路径 {path}")
            self.assertIn(method, paths[path], f"{path} 应支持 {method.upper()}")
            self.assertIn("responses", paths[path][method], f"{path} 应描述响应")


class ErrorBodyTests(StudioTestCase):
    """统一错误体与常见错误码（404 / 405 / 422 / 400）。"""

    port_base = 18908

    def test_unknown_api_path_returns_404(self) -> None:
        error = self.assert_error(self.get("/api/not-exist"), 404, "not_found",
                                  "未知接口应返回 404")
        self.assertIn("没有该接口", error["message"], "404 消息应说明接口不存在")

    def test_unknown_subject_returns_404(self) -> None:
        self.assert_error(self.get("/api/subjects/p99"), 404, "not_found",
                          "不存在的被试应返回 404")

    def test_unsupported_method_returns_405_with_allow(self) -> None:
        error = self.assert_error(self.post("/api/health"), 405, "internal_error",
                                  "方法不支持应返回 405")
        detail = error.get("detail") or {}
        self.assertEqual(detail.get("allow"), ["GET"], "405 响应应带 detail.allow=['GET']")

    def test_invalid_uuid_returns_422(self) -> None:
        self.assert_error(self.get("/api/sessions/not-a-uuid"), 422, "validation_failed",
                          "非法会话 id 应返回 422")

    def test_invalid_public_id_returns_422(self) -> None:
        self.assert_error(self.get("/api/subjects/%21%21bad%21%21"), 422, "validation_failed",
                          "非法被试编号应返回 422")

    def test_malformed_json_body_returns_400(self) -> None:
        response = self.post("/api/subjects", raw_body=b"{not-json",
                             content_type="application/json")
        error = self.assert_error(response, 400, "bad_request", "非法 JSON 请求体应返回 400")
        self.assertIn("JSON", error["message"], "400 消息应提示 JSON 解析失败")

    def test_parameterized_paths_are_routable(self) -> None:
        """路径参数必须被编译成命名分组，否则 26 条带参数的接口会全部 404。"""
        unknown = "f" * 32
        error = self.assert_error(self.get(f"/api/sessions/{unknown}"), 404, "not_found",
                                  "未知会话应返回 404")
        self.assertIn("会话不存在", error["message"],
                      f"该 404 应来自会话查询；若为「没有该接口」说明 {{uuid}} 未被路由匹配（实际：{error['message']}）")

        error = self.assert_error(self.get("/api/sessions/abc"), 422, "validation_failed",
                                  "非 32 位十六进制的会话 id 应返回 422")
        self.assertIn("32 位十六进制", error["message"], "422 消息应说明 uuid 格式要求")

        # 会话子路径不能被 /api/sessions/{uuid} 抢匹配（参数不允许跨 `/`）
        error = self.assert_error(self.get(f"/api/sessions/{unknown}/heatmap"), 404, "not_found",
                                  "未知会话的热力图应返回 404")
        self.assertIn("会话不存在", error["message"], "子路径应命中 /heatmap 而不是详情接口")


if __name__ == "__main__":                        # pragma: no cover - 便于单文件调试
    import unittest

    unittest.main()
