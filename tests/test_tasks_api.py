"""量表与行为任务：目录/题干结构、计分与上游口径一致、非法作答 422；
SART/PVT 序列与结果字段齐全，并与 `ningsi.behavior` 直接比对。"""

from __future__ import annotations

import json
import math

try:                                              # 支持直接以脚本方式运行本文件
    from tests.helpers import StudioTestCase, read_behavior_run
except ModuleNotFoundError:                       # pragma: no cover - 仅脚本运行场景
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.helpers import StudioTestCase, read_behavior_run

from ningsi import config
from ningsi.behavior import pvt as pvt_module
from ningsi.behavior import sart as sart_module
from ningsi.scales.instruments import get_scale
from ningsi.scales.scoring import level_of, score_scale

SAS_RESPONSES = [2, 2, 3, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]


class ScaleCatalogTests(StudioTestCase):
    """量表目录与题干。"""

    port_base = 18950

    def test_catalog(self) -> None:
        payload = self.json_body(self.get("/api/scales"), 200, "量表目录应返回 200")
        items = payload["items"]
        self.assertEqual({item["code"] for item in items}, {"SAS", "SDS"},
                         "目录应含 SAS 与 SDS")
        for item in items:
            for key in ("code", "name", "size", "version", "estimate_minutes"):
                self.assertIn(key, item, f"目录项应含 {key}")
            self.assertEqual(item["size"], 20, "SAS/SDS 均为 20 题")
            self.assertEqual(item["version"], "zung-cn-v1", "量表版本应为 zung-cn-v1")

    def test_definition_matches_engine(self) -> None:
        payload = self.json_body(self.get("/api/scales/SAS"), 200, "量表定义应返回 200")
        for key in ("code", "name", "version", "factor", "size", "options", "items",
                    "reverse_items", "standard_factor", "boundaries", "note"):
            self.assertIn(key, payload, f"量表定义应含 {key}")
        self.assertEqual(payload["code"], "SAS", "应返回 SAS")
        self.assertEqual(payload["size"], 20, "SAS 应为 20 题")
        self.assertEqual(len(payload["items"]), 20, "题干应有 20 条")
        self.assertEqual([item["index"] for item in payload["items"]], list(range(1, 21)),
                         "题干序号应为 1–20")
        for item in payload["items"]:
            self.assertIn("text", item, "题干应含文本")
            self.assertIsInstance(item["reverse"], bool, "题干应标注是否反向计分")
        self.assertEqual(len(payload["options"]), 4, "四级选项应有 4 个")
        self.assertEqual([option["value"] for option in payload["options"]], [1, 2, 3, 4],
                         "选项取值应为 1–4")
        self.assertEqual(payload["standard_factor"], 1.25, "标准分系数应为 1.25")
        self.assertEqual(payload["boundaries"],
                         {"normal_max": 49, "mild_max": 59, "moderate_max": 69},
                         "分级边界应来自引擎 config.SCALE_BOUNDARY")

        upstream = get_scale("SAS")
        self.assertEqual([item["text"] for item in payload["items"]],
                         [text for text, _ in upstream.items], "题干应与引擎逐题一致")
        self.assertEqual(payload["reverse_items"], list(upstream.reverse_indices()),
                         "反向题索引应与引擎一致")

    def test_lowercase_code_is_accepted(self) -> None:
        payload = self.json_body(self.get("/api/scales/sds"), 200, "小写量表码也应返回 200")
        self.assertEqual(payload["code"], "SDS", "小写码应被正规化成 SDS")

    def test_unknown_scale_returns_404(self) -> None:
        self.assert_error(self.get("/api/scales/ABC"), 404, "not_found", "未知量表应返回 404")


class ScaleSubmitTests(StudioTestCase):
    """量表作答与计分口径。"""

    port_base = 18952

    def test_submit_matches_engine_scoring(self) -> None:
        session = self.create_session("t01", time_scale=1.0)
        uuid = session["uuid"]

        payload = self.json_body(self.post(f"/api/sessions/{uuid}/scales/SAS",
                                           {"responses": SAS_RESPONSES}),
                                 200, "提交量表应返回 200")
        expected = score_scale("SAS", SAS_RESPONSES)
        self.assertEqual(payload["code"], "SAS", "应回显量表码")
        self.assertEqual(payload["raw_score"], expected.raw_score,
                         "粗分应与 ningsi.scales.scoring.score_scale 一致")
        self.assertEqual(payload["standard_score"], expected.standard_score,
                         "标准分应与上游一致")
        self.assertEqual(payload["standard_score"],
                         int(math.floor(expected.raw_score * 1.25 + 0.5)),
                         "标准分应为 floor(粗分 × 1.25 + 0.5)")
        self.assertEqual(payload["level"], expected.level, "分级应与上游一致")
        self.assertEqual(payload["level"], level_of(expected.standard_score),
                         "分级应按引擎边界函数给出")
        self.assertEqual(payload["answered"], 20, "应记录 20 题已答")
        self.assertEqual(payload["missing"], [], "无缺答时 missing 应为空")
        self.assertIsInstance(payload["delivered_to_runtime"], bool,
                              "delivered_to_runtime 应为布尔值")

        sds = self.json_body(self.post(f"/api/sessions/{uuid}/scales/SDS",
                                       {"responses": [4] * 20}),
                             200, "提交 SDS 应返回 200")
        sds_expected = score_scale("SDS", [4] * 20)
        self.assertEqual(sds["raw_score"], sds_expected.raw_score, "SDS 粗分应与上游一致")
        self.assertEqual(sds["standard_score"], sds_expected.standard_score,
                         "SDS 标准分应与上游一致")
        self.assertEqual(sds["level"], sds_expected.level, "SDS 分级应与上游一致")

    def test_invalid_responses_return_422(self) -> None:
        session = self.create_session("t02", time_scale=1.0)
        uuid = session["uuid"]
        cases = {
            "缺少 responses": {},
            "只有 19 题": {"responses": SAS_RESPONSES[:19]},
            "多出 1 题": {"responses": SAS_RESPONSES + [2]},
            "出现 5 分": {"responses": [5] + SAS_RESPONSES[1:]},
            "出现 0 分": {"responses": [0] + SAS_RESPONSES[1:]},
            "非整数": {"responses": ["a"] + SAS_RESPONSES[1:]},
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self.assert_error(self.post(f"/api/sessions/{uuid}/scales/SAS", body),
                                  422, "validation_failed", f"非法作答（{name}）应返回 422")

    def test_submit_to_unknown_scale_returns_404(self) -> None:
        session = self.create_session("t03", time_scale=1.0)
        self.assert_error(self.post(f"/api/sessions/{session['uuid']}/scales/SAD",
                                    {"responses": SAS_RESPONSES}),
                          404, "not_found", "未支持的量表应返回 404")


class BehaviorSequenceTests(StudioTestCase):
    """行为任务序列下发与试次提交。"""

    port_base = 18954

    def test_sart_sequence_matches_engine(self) -> None:
        session = self.create_session("t11", time_scale=1.0)   # 真实节奏：本用例内不会进入 SART
        payload = self.json_body(self.get(f"/api/sessions/{session['uuid']}/behaviors/sart/sequence"),
                                 200, "SART 序列应返回 200")
        self.assertEqual(payload["task"], "sart", "任务名应为 sart")
        self.assertEqual(payload["trials"], 180, "正式试次应为 180")
        self.assertEqual(payload["nogo_trials"], 20, "No-Go 应为 20 个")
        self.assertEqual(payload["practice_trials"], 12, "练习试次应为 12")
        self.assertEqual(len(payload["digits"]), 180, "应下发 180 个刺激数字")
        self.assertEqual(len(payload["practice"]), 12, "应下发 12 个练习数字")
        self.assertEqual(payload["digits"].count(3), 20, "数字 3 应恰好出现 20 次")
        self.assertNotIn("nogo_positions", payload, "响应不应泄露 No-Go 位置")
        self.assertTrue(payload["instruction"], "应给出中文指导语")

        participant = session["participant"]
        sequence = sart_module.build_sequence(participant, "01", "001")
        self.assertEqual(payload["digits"], list(sequence.digits),
                         "序列应与引擎 build_sequence 完全一致（可复现）")
        self.assertEqual(payload["seed"], sequence.seed, "随机种子应与引擎一致")
        self.assertEqual(payload["sequence_set_id"], sequence.set_id, "序列集编号应与引擎一致")

    def test_pvt_sequence_shape(self) -> None:
        session = self.create_session("t12", time_scale=1.0)
        payload = self.json_body(self.get(f"/api/sessions/{session['uuid']}/behaviors/pvt/sequence"),
                                 200, "PVT 序列应返回 200")
        self.assertEqual(payload["task"], "pvt-b", "任务名应为 pvt-b")
        self.assertEqual(payload["duration_sec"], 180.0, "PVT 时长为 180 秒")
        self.assertEqual(payload["lapse_sec"], 0.5, "慢反应阈值为 0.5 秒")
        self.assertEqual(len(payload["onsets"]), payload["trials"],
                         "trials 应等于刺激时刻个数")
        self.assertEqual(payload["onsets"], sorted(payload["onsets"]), "刺激时刻应递增")
        for onset in payload["onsets"]:
            self.assertGreaterEqual(onset, 2.0, "首个刺激不早于 2 秒")
            self.assertLess(onset, 180.0, "刺激应落在 180 秒内")
        self.assertTrue(payload["instruction"], "应给出中文指导语")

    def test_trial_submission_is_accepted_and_validated(self) -> None:
        session = self.create_session("t13", time_scale=1.0)
        uuid = session["uuid"]
        accepted = self.json_body(self.post(f"/api/sessions/{uuid}/behaviors/sart/trial",
                                            {"phase": "main", "index": 0,
                                             "responded": True, "rt": 0.37}),
                                  200, "运行中会话的试次提交应被接受")
        self.assertTrue(accepted["accepted"], "试次提交应返回 accepted=true")

        for label, body in (("反应时过小", {"phase": "main", "index": 0, "rt": 0.001}),
                            ("反应时过大", {"phase": "main", "index": 0, "rt": 99})):
            with self.subTest(case=label):
                self.assert_error(self.post(f"/api/sessions/{uuid}/behaviors/sart/trial", body),
                                  422, "validation_failed", f"{label}应返回 422")
        self.assert_error(self.post(f"/api/sessions/{uuid}/behaviors/pvt/trial",
                                    {"index": 0, "rt": 0.001}),
                          422, "validation_failed", "PVT 反应时越界应返回 422")

    def test_non_running_session_returns_409(self) -> None:
        session = self.create_session("t14", time_scale=1.0)
        uuid = session["uuid"]
        self.cancel_session_via_api(uuid)
        self.wait_for(lambda: self.session_detail(uuid),
                      lambda item: item.get("status") != "running",
                      timeout=25.0, message="取消后会话应离开 running")

        self.assert_error(self.get(f"/api/sessions/{uuid}/behaviors/sart/sequence"),
                          409, "conflict", "非运行会话不应下发 SART 序列")
        self.assert_error(self.get(f"/api/sessions/{uuid}/behaviors/pvt/sequence"),
                          409, "conflict", "非运行会话不应下发 PVT 序列")
        self.assert_error(self.post(f"/api/sessions/{uuid}/behaviors/sart/trial",
                                    {"phase": "main", "index": 0, "responded": True, "rt": 0.3}),
                          409, "conflict", "非运行会话不应接受试次")
        # 该会话在基线阶段就被取消，SART 尚未开始，因此结果不可用
        self.assert_error(self.get(f"/api/sessions/{uuid}/behaviors/sart/result"),
                          404, "not_found", "未开始的 SART 不应给出结果")

    def test_unknown_task_result_returns_404(self) -> None:
        session = self.create_session("t15", time_scale=1.0)
        self.assert_error(self.get(f"/api/sessions/{session['uuid']}/behaviors/n-back/result"),
                          404, "not_found", "未支持的行为任务应返回 404")


class BehaviorResultTests(StudioTestCase):
    """跑完一次快速演示后，用引擎函数复算 SART / PVT 指标（耗时较长，约 1 分钟）。"""

    port_base = 18956

    def test_results_match_engine_scoring(self) -> None:
        session = self.create_session("t21")
        uuid = session["uuid"]
        detail = self.wait_session_done(uuid)
        self.assertEqual(detail["status"], "done", "快速演示会话应正常结束")
        participant = detail["participant"]

        # ---------------------------------------------------------------- SART
        payload = self.json_body(self.get(f"/api/sessions/{uuid}/behaviors/sart/result"), 200,
                                 "SART 完成后应能取到结果")
        self.assertIn("result", payload, "结果应包装为 {result, trials}")
        self.assertIn("trials", payload, "结果应带逐试次明细")
        result, trials = payload["result"], payload["trials"]
        for field in ("trials", "go_trials", "nogo_trials", "go_accuracy", "commission_rate",
                      "omission_rate", "rt_mean", "rt_sd", "rt_p50", "rt_variability",
                      "sequence_set_id", "seed", "spec"):
            self.assertIn(field, result, f"SART 结果应含字段 {field}")

        sequence = sart_module.build_sequence(participant, "01", "001")
        self.assertEqual(result["trials"], sequence.trials, "正式试次数应为 180")
        self.assertEqual(result["nogo_trials"], len(sequence.nogo_positions), "No-Go 应为 20 个")
        self.assertEqual(result["sequence_set_id"], sequence.set_id, "序列集编号应与引擎一致")
        self.assertEqual(result["seed"], sequence.seed, "随机种子应与引擎一致")
        self.assertEqual(result["spec"], config.LABEL_SPEC, "口径版本应为 joint-assessment-v1")
        self.assertEqual(trials["nogo_positions"], list(sequence.nogo_positions),
                         "落库的 No-Go 位置应可用引擎复现")
        self.assertEqual(trials["seed"], sequence.seed, "逐试次明细应带同一随机种子")
        self.assertEqual(len(trials["practice"]), 12, "练习试次明细应为 12 条")

        main = sorted(trials["main"], key=lambda item: item["index"])
        self.assertEqual(len(main), sequence.trials, "正式试次明细应完整")
        expected = sart_module.SartResult(
            participant, "01", "001", sequence,
            tuple(item["responded"] for item in main),
            tuple(item["rt"] for item in main),
            extra={"source": "studio-session"},
        ).score()
        for field in ("trials", "go_trials", "nogo_trials", "go_accuracy", "commission_errors",
                      "commission_rate", "omission_errors", "omission_rate", "rt_mean", "rt_sd",
                      "rt_p50", "rt_variability"):
            with self.subTest(task="sart", field=field):
                self.assertAlmostEqual(float(result[field]), float(expected[field]), places=9,
                                       msg=f"SART {field} 应与 ningsi.behavior.sart 计分一致")

        # ---------------------------------------------------------------- PVT
        pvt_payload = self.json_body(self.get(f"/api/sessions/{uuid}/behaviors/pvt/result"), 200,
                                     "PVT 完成后应能取到结果")
        pvt = pvt_payload.get("result", pvt_payload)
        pvt_trials = pvt_payload.get("trials", {})
        for field in ("trials", "responded", "missed", "rt_mean", "rt_median", "rt_sd",
                      "rt_p90", "lapses", "lapse_rate", "false_starts", "valid"):
            self.assertIn(field, pvt, f"PVT 结果应含字段 {field}")
        self.assertEqual(pvt["task"], "pvt-b", "任务名应为 pvt-b")
        self.assertEqual(pvt["spec"], config.LABEL_SPEC, "PVT 口径版本应为 joint-assessment-v1")
        self.assertTrue(pvt["valid"], "自动作答下 PVT 结果应判定为有效")
        self.assertEqual(len(pvt_trials["trials"]), pvt["trials"], "逐试次明细应完整")
        self.assertEqual(len(pvt_trials["onsets"]), pvt["trials"], "刺激时刻表应与试次数一致")

        rebuilt = pvt_module.PvtResult(
            trials=[pvt_module.PvtTrial(onset=item["onset"], responded=item["responded"],
                                        rt=item["rt"], false_start=item["false_start"])
                    for item in pvt_trials["trials"]],
            duration_sec=pvt["duration_sec"],
        ).score()
        for field in ("trials", "responded", "missed", "rt_mean", "rt_median", "rt_sd", "rt_p90",
                      "lapses", "lapse_rate", "false_starts"):
            with self.subTest(task="pvt", field=field):
                self.assertAlmostEqual(float(pvt[field]), float(rebuilt[field]), places=9,
                                       msg=f"PVT {field} 应与 ningsi.behavior.pvt 计分一致")
        self.assertEqual(pvt["valid"], rebuilt["valid"], "valid 应与引擎一致")

        # ---------------------------------------------------------------- 落库核对
        sart_stored = read_behavior_run(self.db_path, uuid, "sart")
        self.assertTrue(sart_stored, "SART 结果应以 sart 为键落库")
        pvt_stored = read_behavior_run(self.db_path, uuid, "pvt-b")
        self.assertTrue(pvt_stored, "PVT 结果应以 pvt-b 为键落库（与上游口径一致）")
        stored_metrics = json.loads(pvt_stored["metrics"])
        self.assertEqual(stored_metrics["trials"], pvt["trials"], "落库的试次数应与接口一致")
        self.assertEqual(len(json.loads(pvt_stored["trials"])["trials"]), pvt["trials"],
                         "落库的逐试次明细应完整")


if __name__ == "__main__":                        # pragma: no cover - 便于单文件调试
    import unittest

    unittest.main()
