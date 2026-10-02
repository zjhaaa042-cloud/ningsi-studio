"""端到端：建被试 → 建会话（time_scale=0.05 快速演示）→ 等待 done →
校验产物齐全、报告口径、联合评估、训练分段与指标区间。"""

from __future__ import annotations

import io
import json
import zipfile

try:                                              # 支持直接以脚本方式运行本文件
    from tests.helpers import QUICK_TIME_SCALE, StudioTestCase
except ModuleNotFoundError:                       # pragma: no cover - 仅脚本运行场景
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.helpers import QUICK_TIME_SCALE, StudioTestCase

from ningsi import config
from ningsi.monitoring.heatmap import MISSING_COLOR
from ningsi_studio import bootstrap

REQUIRED_ARTIFACTS = ("report_md", "report_json", "heatmap_svg", "trend_svg", "model", "history")


class DemoFlowTests(StudioTestCase):
    """一次完整快速演示流程（无浏览器、无真实设备、无网络）。"""

    port_base = 18970

    def test_full_demo_flow(self) -> None:
        subject = self.create_subject(auto_id=True, label="端到端演示被试")
        participant = subject["public_id"]

        created = self.json_body(
            self.post("/api/sessions", {"participant": participant, "device": "sim-bsense",
                                        "time_scale": QUICK_TIME_SCALE,
                                        "training_mode": "quick", "label": "自动化端到端"}),
            201, "创建会话应返回 201")
        session = created["session"]
        uuid = session["uuid"]
        self.assertEqual(created["events_url"], f"/api/sessions/{uuid}/events",
                         "创建响应应给出 SSE 地址")
        self.assertTrue(created["runtime"]["alive"], "创建后运行器应存活")

        detail = self.wait_session_done(uuid, timeout=180.0)
        self.assertEqual(detail["status"], "done", "端到端会话应正常结束")
        self.assertEqual(detail["phase"], "done", "结束后 phase 应为 done")
        self.assertEqual(detail["progress"], 1.0, "结束后进度应为 1.0")
        self.assertTrue(detail["ended_at"], "结束后应有 ended_at")
        self.assertIsNone(detail["error"], "正常结束不应有 error")
        self.assertFalse(detail["runtime"]["alive"], "结束后运行器应已退出")
        self.assertEqual(detail["participant"], participant, "会话应绑定到该被试")

        self._assert_artifacts(uuid)
        report = self._assert_report(uuid, participant)
        self._assert_assessment(uuid, report)
        self._assert_training(uuid)
        self._assert_indicator_summary(detail)
        self._assert_heatmap(uuid)
        self._assert_trend(uuid, participant)
        self._assert_export_zip(uuid)

    # ------------------------------------------------------------------ 子断言
    def _assert_artifacts(self, uuid: str) -> None:
        items = self.json_body(self.get(f"/api/sessions/{uuid}/artifacts"), 200,
                               "产物清单应返回 200")["items"]
        kinds = {item["kind"] for item in items}
        for kind in REQUIRED_ARTIFACTS:
            self.assertIn(kind, kinds, f"产物清单应包含 {kind}（实际：{sorted(kinds)}）")
        for item in items:
            self.assertTrue(item["exists"], f"产物 {item['kind']} 的文件应存在：{item['path']}")
            self.assertGreater(item["bytes"], 0, f"产物 {item['kind']} 不应为空文件")
            self.assertEqual(len(item["sha256"]), 64, f"产物 {item['kind']} 应带 sha256")
            self.assertEqual(item["download"], f"/api/sessions/{uuid}/artifacts/{item['kind']}",
                             "download 字段应指向下载地址")

        model = self.get(f"/api/sessions/{uuid}/artifacts/model")
        self.assert_status(model, 200, "模型文件应可下载")
        self.assertIn("feature_names", model.json(), "模型文件应带特征名列表")
        trend_svg = self.get(f"/api/sessions/{uuid}/artifacts/trend_svg")
        self.assert_status(trend_svg, 200, "趋势图应可下载")
        self.assertIn(b"<svg", trend_svg.body, "趋势图应是 SVG")

    def _assert_report(self, uuid: str, participant: str) -> dict:
        report = self.json_body(self.get(f"/api/sessions/{uuid}/report"), 200,
                                "已完成会话应返回完整报告")
        self.assertEqual(report["spec"], config.LABEL_SPEC, "报告口径应为 joint-assessment-v1")
        self.assertEqual(report["versions"], bootstrap.engine_versions(),
                         "报告版本应与引擎口径一致")
        self.assertEqual(report["participant"], participant, "报告应绑定该被试")
        self.assertEqual(report["session"], "01", "会话编号应为 01")
        self.assertEqual(report["run"], "001", "Run 编号应为 001")

        for key in ("indicators", "quality", "scales", "behavior", "assessment", "extras"):
            self.assertIn(key, report, f"报告应含 {key}")
        self.assertIn("report_markdown", report, "报告应附 Markdown 原文")
        self.assertTrue(report["report_markdown"].startswith("#"),
                        "Markdown 原文应以标题开头")

        quality = report["quality"]
        self.assertGreater(quality["windows_total"], 0, "应统计分析窗总数")
        self.assertGreaterEqual(quality["valid_ratio"], 0.0, "可用窗比例不应为负")
        self.assertLessEqual(quality["valid_ratio"], 1.0, "可用窗比例不应大于 1")

        self.assertEqual(sorted(report["scales"]), ["SAS", "SDS"], "报告应含 SAS 与 SDS")
        for code, scale in report["scales"].items():
            for key in ("raw_score", "standard_score", "level", "version"):
                self.assertIn(key, scale, f"{code} 结果应含 {key}")
            self.assertEqual(scale["version"], "zung-cn-v1", f"{code} 版本应为 zung-cn-v1")

        behavior = report["behavior"]
        self.assertEqual(sorted(behavior), ["pvt", "sart"], "报告应含 SART 与 PVT")
        self.assertEqual(behavior["sart"]["trials"], 180, "SART 应为 180 个正式试次")
        self.assertTrue(behavior["pvt"]["valid"], "PVT 结果应判定为有效")

        extras = report["extras"]
        self.assertEqual(extras["device"], "sim-bsense", "报告应标注仿真数据源")
        self.assertEqual(extras["source_kind"], "sim", "数据来源类型应为 sim")
        self.assertEqual(extras["time_scale"], QUICK_TIME_SCALE, "报告应记录时间倍率")
        self.assertIn("engine_versions", extras, "报告应记录口径版本")
        self.assertIn("training", extras, "报告应嵌入训练汇总")
        return report

    def _assert_assessment(self, uuid: str, report: dict) -> None:
        assessment = self.json_body(self.get(f"/api/sessions/{uuid}/assessment"), 200,
                                    "联合会评估应返回 200")
        for key in ("spec", "conclusion", "dimension_states", "consistency", "evidence",
                    "advice", "boundary"):
            self.assertIn(key, assessment, f"联合评估应含 {key}")
        self.assertTrue(assessment["conclusion"], "联合评估结论不应为空")
        self.assertTrue(assessment["boundary"], "联合评估应带结论边界说明")
        self.assertIn("医疗诊断", assessment["boundary"], "边界说明应含非诊断声明")
        for dimension in ("attention", "stress"):
            self.assertIn(dimension, assessment["dimension_states"],
                          f"维度状态应含 {dimension}")
            self.assertIn("state", assessment["dimension_states"][dimension],
                          f"{dimension} 应有状态取值")
        for key in ("eeg_vs_scale", "eeg_vs_behavior"):
            self.assertIn(key, assessment["consistency"], f"一致性应含 {key}")
        self.assertTrue(assessment["advice"], "应给出改善建议")
        for item in assessment["evidence"]:
            for key in ("code", "source", "dimension", "summary", "value", "direction",
                        "available", "ref"):
                self.assertIn(key, item, f"证据项应含 {key}")
        self.assertEqual(report["assessment"]["conclusion"], assessment["conclusion"],
                         "报告内的评估结论应与评估接口一致")

    def _assert_training(self, uuid: str) -> None:
        training = self.json_body(self.get(f"/api/sessions/{uuid}/training"), 200,
                                  "训练记录应返回 200")
        segments = training["segments"]
        self.assertEqual(len(segments), 2, "quick 模式应有 2 个训练分段")
        for segment in segments:
            for key in ("seq", "target", "mean_score", "on_target_ratio", "hold_sec",
                        "samples", "stats"):
                self.assertIn(key, segment, f"训练分段应含 {key}")
            self.assertIsInstance(segment["samples"], list, "samples 应为数组")
            self.assertGreaterEqual(segment["on_target_ratio"], 0.0, "达标时间占比不应为负")
            self.assertLessEqual(segment["on_target_ratio"], 1.0, "达标时间占比不应大于 1")

        summary = training["summary"]
        for key in ("mean_focus", "on_target_ratio", "first_to_last_change", "baseline_before",
                    "baseline_after", "target_rationale", "baseline_comparable"):
            self.assertIn(key, summary, f"训练汇总应含 {key}")
        self.assertIsInstance(summary["baseline_comparable"], bool,
                              "baseline_comparable 应为布尔值")
        self.assertGreaterEqual(summary["mean_focus"], 0.0, "平均专注度不应为负")
        self.assertLessEqual(summary["mean_focus"], 1.0, "平均专注度不应大于 1")
        self.assertTrue(summary["target_rationale"], "应给出目标设定理由")

    def _assert_indicator_summary(self, detail: dict) -> None:
        summary = detail["indicator_summary"]
        self.assertTrue(summary, "应写入指标汇总")
        self.assertIn("focus", summary, "指标汇总应含 focus")
        for name, stat in summary.items():
            self.assertIn(name, ("focus", "relax", "load"), f"未知指标 {name}")
            for key in ("mean", "std", "n"):
                self.assertIn(key, stat, f"{name} 汇总应含 {key}")
            self.assertGreaterEqual(stat["mean"], 0.0, f"{name} 均值不应为负")
            self.assertLessEqual(stat["mean"], 1.0, f"{name} 均值不应大于 1")
            self.assertGreater(stat["n"], 0, f"{name} 应有可用窗计数")

    def _assert_heatmap(self, uuid: str) -> None:
        heatmap = self.json_body(self.get(f"/api/sessions/{uuid}/heatmap"), 200,
                                 "热力图应返回 200")
        self.assertEqual(heatmap["columns"], 30, "热力图每行 30 列")
        cells = heatmap["cells"]
        self.assertTrue(cells, "热力图应给出单元格")
        for cell in cells:
            self.assertEqual(set(cell), {"index", "label", "color", "score", "low", "high", "t"},
                             "热力图单元格结构应统一")
            if cell["index"] < 0:
                self.assertIsNone(cell["score"], "缺失窗的 score 应为 null")
                self.assertIsNone(cell["low"], "缺失窗不应有档位下限")
                self.assertEqual(cell["color"], MISSING_COLOR, "缺失窗应使用缺失色")
            else:
                self.assertGreaterEqual(cell["score"], 0.0, "评分不应为负")
                self.assertLessEqual(cell["score"], 1.0, "评分不应大于 1")
        self.assertEqual(heatmap["scored"] + heatmap["missing"], len(cells),
                         "scored + missing 应等于单元格总数")
        self.assertEqual(heatmap["scored"], sum(1 for cell in cells if cell["index"] >= 0),
                         "scored 应为有评分窗计数")
        self.assertEqual(heatmap["missing"], sum(1 for cell in cells if cell["index"] < 0),
                         "missing 应为缺失窗计数")
        self.assertEqual(heatmap["missing_color"], MISSING_COLOR, "缺失色应来自引擎常量")
        legend = heatmap["legend"]
        self.assertGreaterEqual(len(legend), 5, "图例应覆盖五档")
        self.assertEqual([item["label"] for item in legend[:5]],
                         [band[2] for band in config.HEATMAP_BANDS],
                         "图例前五档应与 config.HEATMAP_BANDS 一致")
        self.assertTrue(any(item["label"] == "低质量缺失" for item in legend),
                        "图例应说明缺失色块")
        self.assertTrue(heatmap["note"], "热力图应给出缺失窗口径说明")

    def _assert_trend(self, uuid: str, participant: str) -> None:
        trend = self.json_body(self.get(f"/api/sessions/{uuid}/trend?field=focus&period=week"),
                               200, "会话趋势应返回 200")
        self.assertEqual(trend["field"], "focus", "应回显 field")
        self.assertEqual(trend["period"], "week", "应回显 period")
        self.assertGreaterEqual(trend["sessions"], 1, "至少应统计到本次会话")
        self.assertEqual(trend["comparable"] + trend["rejected"], trend["sessions"],
                         "可比 + 不可比应等于记录总数")
        self.assertIsInstance(trend["rejected_reasons"], dict, "rejected_reasons 应为对象")
        self.assertGreaterEqual(len(trend["points"]), 1, "已完成会话应聚合出趋势点")
        for point in trend["points"]:
            for key in ("period", "mean", "n", "std"):
                self.assertIn(key, point, f"趋势点应含 {key}")

        global_trend = self.json_body(
            self.get(f"/api/reports/trend?participant={participant}&field=focus&period=week"),
            200, "跨会话趋势应返回 200")
        for key in ("field", "period", "points", "ledger_points", "sessions", "comparable",
                    "rejected", "rejected_reasons", "note"):
            self.assertIn(key, global_trend, f"跨会话趋势应含 {key}")
        self.assertIsInstance(global_trend["ledger_points"], list, "台账趋势应为数组")
        # 台账按会话写在 runs/<uuid>/history/sessions.jsonl：这里不能是空数组，
        # 且聚合出的 n 之和应等于本用例写入的台账记录数（一次完成会话 = 1 条）。
        self.assertTrue(global_trend["ledger_points"],
                        "ledger_points 不应为空（台账写在 runs/<uuid>/history/sessions.jsonl）")
        self.assertEqual(sum(point["n"] for point in global_trend["ledger_points"]), 1,
                         "本用例只有一次完成会话，台账聚合的 n 应为 1（条数与记录数一致）")

        field_error = self.assert_error(self.get(f"/api/sessions/{uuid}/trend?field=xxx"), 422,
                                        None, "非法 field 应返回 422")
        self.assert_error(self.get(f"/api/sessions/{uuid}/trend?period=year"), 422,
                          None, "非法 period 应返回 422")
        self.assert_error(self.get("/api/reports/trend?field=xxx"), 422, None,
                          "跨会话趋势的非法 field 也应返回 422")
        # 契约错误表：422 一律对应 validation_failed（当前实现抛的是基类 ApiError，code 为 internal_error）
        self.assertEqual(field_error["code"], "validation_failed",
                         "422 的错误码应为 validation_failed")

    def _assert_export_zip(self, uuid: str) -> None:
        response = self.get(f"/api/sessions/{uuid}/export.zip")
        self.assert_status(response, 200, "打包下载应返回 200")
        self.assertEqual(response.content_type, "application/zip", "打包应为 zip 类型")
        self.assertIn("attachment", response.content_disposition, "打包下载应带附件头")
        self.assertTrue(response.body.startswith(b"PK"), "zip 内容应以 PK 开头")
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            names = archive.namelist()
            self.assertIn("manifest.json", names, "打包应包含 manifest.json")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        self.assertEqual(manifest["meta"]["uuid"], uuid, "manifest 应记录会话 uuid")
        self.assertIn("report_json", manifest["files"], "manifest 应逐文件列出 sha256")
        self.assertTrue(manifest["files"]["report_json"]["exists"], "manifest 应写明文件存在")
        self.assertEqual(len(manifest["files"]["report_json"]["sha256"]), 64,
                         "manifest 的 sha256 应为 64 位十六进制")


if __name__ == "__main__":                        # pragma: no cover - 便于单文件调试
    import unittest

    unittest.main()
