"""基础接口与统一错误体：/api/health、/api/config、/api/devices、/api/openapi.json。

全部通过真实 HTTP 服务访问（urllib），覆盖状态码、字段结构与错误体格式；
另含列表类响应的统一信封（`/api/scales`）与 `--token` 只保护 `/api` 的鉴权语义。
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
from ningsi_studio.settings import Settings


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
        self.assertTrue(sources, "至少应给出一个数据源")
        self.assertIn("has_real_source", payload, "应直接给出「当前有没有真实源」的判断")

        # 2026-10-07 产品规则变更：**真实流在前、仿真源放最后并标记 explicit_only**。
        # 原来断言"首个数据源是仿真源"，那会让前端把仿真当缺省选择；现在缺省必须跟着真机走，
        # 没有真机时靠 has_real_source=False + 显式选择仿真来区分。
        sim = next((item for item in sources if item["key"] == "sim-bsense"), None)
        self.assertIsNotNone(sim, "列表中应仍然包含仿真源（用于显式选择的演示）")
        self.assertEqual(sim["kind"], "sim", "仿真源 kind 应为 sim")
        self.assertEqual(sim["device"], "sim-bsense", "仿真源 device 应为 sim-bsense")
        self.assertEqual(sim["srate"], 250.0, "仿真源采样率应为 250 Hz")
        self.assertEqual(sim["channels"], 1, "仿真源通道数应为 1")
        self.assertFalse(sim["real"], "仿真源不应标记为真实设备")
        self.assertIn("note", sim, "数据源应带来源说明")
        self.assertTrue(sim.get("explicit_only"), "仿真源应标记 explicit_only（只能被显式选择）")
        real_rows = [item for item in sources if item.get("real") and not item.get("simulated")]
        if real_rows:
            self.assertNotEqual(sources[0]["key"], "sim-bsense", "有真实流时仿真源不应排在首位")
        else:
            self.assertFalse(payload["has_real_source"], "没有真实流时 has_real_source 应为 False")
            self.assertIn("hardware_note", sim, "无实时信号时应给出接入提示")

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
        # 数据来源三件套：检测到实时源时给出 key/kind/note；**没有信号时必须都是 None**
        # （2026-10-07 产品规则：没有脑机信号就不使用仿真，界面显示"无信号"）。
        for key in ("source", "source_kind", "source_note", "has_real_source"):
            self.assertIn(key, payload, f"总览应含 {key}")
        if payload["has_real_source"]:
            self.assertTrue(payload["source"], "有实时源时应给出 source key")
            self.assertTrue(payload["source_note"], "有实时源时应给出说明文案")
        else:
            self.assertIsNone(payload["source"], "没有实时源时不应回落到仿真源")
            self.assertIsNone(payload["source_kind"], "没有实时源时 source_kind 应为 None")
            self.assertIsNone(payload["source_note"], "没有实时源时 source_note 应为 None")


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
        self.assertTrue(any(item["key"] == "sim-bsense" for item in sources),
                        "设备体检的数据源列表里应仍含仿真源（供显式选择）")


class NoSignalPolicyTests(StudioTestCase):
    """没有脑机信号时的产品规则（2026-10-07）：

    · 建会话时 `device` 缺省/auto ⇒ 用当前检测到的实时源，**绝不回落仿真**；
      检测不到实时源 ⇒ `409`，消息里给接入指引；
    · 设备名只接受 `lsl:<流名称>` 或显式 `sim-bsense`，别的值一律 422；
    · 声明了 `lsl:<流名>` 但流不可见 ⇒ 409（fail fast，而不是建好会话才发现没信号）。

    注意：这几条要在"有真机"和"无真机"两种环境下都成立——开发机上常常真接着采集设备，
    所以断言写成"按检测结果分支"，但**每个分支都必须证明没有静默使用仿真**。
    """

    port_base = 18914

    def _detected(self) -> bool:
        payload = self.json_body(self.get("/api/devices?probe=1.0"), 200, "设备列表应返回 200")
        return bool(payload.get("has_real_source"))

    def _create(self, body: dict):
        return self.post("/api/sessions", {"participant": "ns01", **body})

    def test_default_device_never_falls_back_to_sim(self) -> None:
        response = self._create({"time_scale": 0.05, "create_subject": True})
        if self._detected():
            # 有实时源：应当用实时源（成功），或因为"流在但没样本"而如实失败；
            # 无论哪种，都**不允许**变成仿真会话。
            if response.status == 201:
                payload = response.json()
                self.assertTrue(str(payload["session"]["source"]).startswith("lsl:"),
                                "缺省建会话必须用实时源，不能是 sim-bsense")
                self.delete(f"/api/sessions/{payload['session']['uuid']}")
            else:
                error = self.assert_error(response, 409, "conflict",
                                          "实时源不可用时缺省建会话应 409（不得回落仿真）")
                self.assertNotIn("sim-bsense", json.dumps(error, ensure_ascii=False),
                                 "错误信息里不应把仿真源当成回退方案")
        else:
            error = self.assert_error(response, 409, "conflict",
                                      "没有实时信号时缺省建会话应 409（不得回落仿真）")
            self.assertIn("未检测到脑电信号", error["message"],
                          "错误信息应说明「未检测到脑电信号」并给出可操作建议")

    def test_auto_device_alias_matches_default(self) -> None:
        response = self._create({"device": "auto", "time_scale": 0.05, "create_subject": True})
        if self._detected() and response.status == 201:
            payload = response.json()
            self.assertTrue(str(payload["session"]["source"]).startswith("lsl:"),
                            "device=auto 与缺省同义：应选实时源")
            self.delete(f"/api/sessions/{payload['session']['uuid']}")
        else:
            self.assert_error(response, 409, "conflict", "device=auto 与缺省同义：没有可用实时源应 409")

    def test_unknown_device_is_rejected(self) -> None:
        response = self._create({"device": "随便写的设备", "time_scale": 0.05, "create_subject": True})
        self.assert_error(response, 422, "validation_failed", "非法设备名应 422（不再当成仿真）")

    def test_missing_lsl_stream_is_rejected(self) -> None:
        response = self._create({"device": "lsl:不存在的流-abc", "time_scale": 0.05,
                                 "create_subject": True})
        self.assert_error(response, 409, "conflict", "LSL 流不可见时应 409（fail fast）")

    def test_explicit_sim_still_allowed(self) -> None:
        response = self._create({"device": "sim-bsense", "time_scale": 0.05, "create_subject": True})
        payload = self.json_body(response, 201, "显式选择仿真源仍应允许（演示/自测）")
        self.assertEqual(payload["session"]["source"], "sim-bsense", "显式仿真会话应标注 source")
        self.delete(f"/api/sessions/{payload['session']['uuid']}")


class PreviewTests(StudioTestCase):
    """`/api/devices/preview`：**没开会话**也能看真实信号。

    · 检测到实时源 ⇒ 自动选源可用，且 `kind == "lsl"`（**不会**变成仿真）；
    · 检测不到 ⇒ 409 + 接入指引；显式 `sim-bsense` 仍可用于演示，并能取到一窗带质检判定的波形。
    """

    port_base = 18916

    def _detected(self) -> bool:
        payload = self.json_body(self.get("/api/devices?probe=1.0"), 200, "设备列表应返回 200")
        return bool(payload.get("has_real_source"))

    def test_preview_auto_source_never_falls_back_to_sim(self) -> None:
        status = self.json_body(self.get("/api/devices/preview"), 200, "预览状态应返回 200")
        self.assertFalse(status["active"], "没有启动时 active 应为 False")

        response = self.post("/api/devices/preview", {})
        if self._detected():
            payload = self.json_body(response, 200, "检测到实时源时自动选择应成功")
            self.assertEqual(payload["kind"], "lsl", "自动选源必须是实时流，不能是仿真")
            self.assertTrue(str(payload["source"]).startswith("lsl:"), "source 应为 lsl:<流名>")
            # 流在但没推样本时应当如实报"无信号"，而不是伪造数据
            self.assertIn("no_signal", payload, "预览状态应给出 no_signal")
            self.delete("/api/devices/preview")
        else:
            self.assert_error(response, 409, "conflict", "没有实时信号时自动预览应 409（不得回落仿真）")

    def test_preview_with_explicit_sim_and_window(self) -> None:
        started = self.json_body(self.post("/api/devices/preview", {"source": "sim-bsense"}),
                                 200, "显式选择仿真源应能启动预览")
        self.assertTrue(started["active"], "启动后 active 应为 True")
        self.assertEqual(started["kind"], "sim", "预览源 kind 应回显 sim")

        frame = self.json_body(self.get("/api/devices/preview?window=1"), 200, "取预览窗应返回 200")
        self.assertTrue(frame["samples"], "预览窗应带波形采样点")
        self.assertGreater(len(frame["samples"]), 0, "至少一个通道")
        self.assertTrue(frame["samples"][0], "采样点不应为空")
        self.assertIn("quality", frame, "预览窗应带质检判定")
        self.assertIn("ok", frame["quality"], "质检判定应含 ok")
        self.assertGreater(frame["seconds"], 0, "应报告实际窗长")

        stopped = self.json_body(self.delete("/api/devices/preview"), 200, "停止预览应返回 200")
        self.assertTrue(stopped["stopped"], "停止后应回 stopped=true")
        after = self.json_body(self.get("/api/devices/preview"), 200, "停止后再查状态应 200")
        self.assertFalse(after["active"], "停止后 active 应为 False")


class OpenApiTests(StudioTestCase):
    """`/api/openapi.json` 与文档路径的一致性。"""

    port_base = 18906

    # (路径, 方法)：与 docs/API.md 逐一核对
    DOCUMENTED = (
        ("get", "/api/health"), ("get", "/api/config"), ("get", "/api/devices"),
        ("get", "/api/devices/status"), ("get", "/api/devices/preview"),
        ("post", "/api/devices/preview"), ("delete", "/api/devices/preview"),
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


class CollectionEnvelopeTests(StudioTestCase):
    """列表类响应的统一信封 `{items,total,limit,page}`：`/api/scales` 是纯目录端点，
    最容易验证"信封字段齐全 + 真分页"（产物清单在 test_sessions_api 里用真实会话验证）。
    """

    port_base = 18918

    def test_scale_catalog_has_collection_envelope(self) -> None:
        payload = self.json_body(self.get("/api/scales"), 200, "量表目录应返回 200")
        self.assertEqual(set(payload), {"items", "total", "limit", "page"},
                         "量表目录应返回列表类响应的统一信封（见 docs/API.md 分页约定）")
        self.assertEqual(payload["total"], len(payload["items"]),
                         "total 应与本页 items 一致（目录只有一页）")
        self.assertEqual(payload["total"], 2, "目录应含 SAS/SDS 两套量表")
        self.assertEqual({item["code"] for item in payload["items"]}, {"SAS", "SDS"},
                         "目录项应为 SAS 与 SDS")
        self.assertEqual(payload["limit"], 50, "默认 limit 应为 50")
        self.assertEqual(payload["page"], 1, "page 应从 1 开始")

    def test_scale_catalog_honours_pagination_params(self) -> None:
        first = self.json_body(self.get("/api/scales?limit=1&page=1"), 200, "第 1 页应返回 200")
        second = self.json_body(self.get("/api/scales?limit=1&page=2"), 200, "第 2 页应返回 200")
        self.assertEqual((first["total"], second["total"]), (2, 2),
                         "两页的 total 应都指向目录总数 2（不是本页条数）")
        self.assertEqual((first["limit"], second["limit"]), (1, 1), "limit 应回显请求值")
        self.assertEqual((first["page"], second["page"]), (1, 2), "page 应回显请求页")
        self.assertEqual((len(first["items"]), len(second["items"])), (1, 1),
                         "limit=1 时每页只应返回 1 条")
        self.assertNotEqual(first["items"][0]["code"], second["items"][0]["code"],
                            "第 2 页应是不同的量表，而不是重复第 1 页")

        beyond = self.json_body(self.get("/api/scales?limit=1&page=3"), 200,
                                "超出范围的页应返回 200 与空 items")
        self.assertEqual(beyond["items"], [], "超出范围的页 items 应为空")
        self.assertEqual(beyond["total"], 2, "超出范围的页仍应回报 total=2")


class TokenAuthTests(StudioTestCase):
    """`--token`（`api_token` 非空）只保护 `/api`：静态资源放行，缺令牌返回 unauthorized。

    用 `make_settings` 注入令牌（基类的扩展点），不改 helpers。
    """

    port_base = 18990
    api_token = "test-token-2f7c"

    @classmethod
    def make_settings(cls, data_dir) -> Settings:
        settings = super().make_settings(data_dir)
        settings.api_token = cls.api_token
        return settings

    def setUp(self) -> None:
        # 基类 setUp 会调 /api 清运行中会话（不带令牌必然 401），本类不建会话，跳过即可。
        pass

    def test_api_requests_need_token(self) -> None:
        missing = self.assert_error(self.get("/api/health"), 401, "unauthorized",
                                   "配置令牌后未带令牌的 /api 请求应返回 401 unauthorized")
        self.assertIn("Token", missing["message"], "401 消息应说明令牌问题")

        self.assert_error(self.get("/api/health?token=wrong"), 401, "unauthorized",
                          "查询参数带错令牌应返回 401")
        self.assert_error(self.get("/api/health", headers={"X-API-Token": "wrong"}), 401,
                          "unauthorized", "请求头带错令牌应返回 401")

        header = self.json_body(self.get("/api/health", headers={"X-API-Token": self.api_token}),
                                200, "带 X-API-Token 请求头应放行")
        self.assertEqual(header["status"], "ok", "带正确令牌应拿到正常健康检查结果")
        query = self.json_body(self.get(f"/api/health?token={self.api_token}"), 200,
                               "带 ?token= 查询参数应放行")
        self.assertEqual(query["service"], "ningsi-studio", "两种给法应返回同一份数据")

    def test_static_resources_ignore_token(self) -> None:
        index = self.assert_status(self.get("/index.html"), 200,
                                   "带 --token 时静态入口仍应返回 200（否则 SPA 永远 boot 不起来）")
        self.assertIn("text/html", index.content_type, "index.html 应为 HTML")
        self.assert_status(self.get("/"), 200, "SPA 根路径不应被令牌拦截")
        self.assert_status(self.get("/js/api.js"), 200, "前端脚本不应被令牌拦截")

        # SPA 回退路径同样放行（未知非 /api 路径交给前端路由）
        self.assert_status(self.get("/history"), 200, "SPA 回退路径不应被令牌拦截")


if __name__ == "__main__":                        # pragma: no cover - 便于单文件调试
    import unittest

    unittest.main()
