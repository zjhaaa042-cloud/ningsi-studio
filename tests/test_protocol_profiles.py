"""协议档（full / short）：短协议少跑哪两步、行为任务减到多少、报告怎么标注。

背景（2026-10-07 用户选定的方案 B）：完整协议的 11 步里，「神经反馈训练」与「模型训练」
都不是测量——前者是训练干预、后者用内置仿真被试训练且与本次会话数据无关。于是新增
**短协议**：9 步（去掉这两步）+ SART 180→90 试次（No-Go 20→10）+ PVT 180→120 秒。
代价必须留痕：报告表头写「检测协议：短协议（本次未跑：…）」+「协议差异提醒」。

这一组测试用**真实服务 + 仿真源 + 快速演示**跑完一次短协议会话（约 60–90 秒），
同时守住"完整协议行为不变"（默认档不能被动过）。
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers import QUICK_TIME_SCALE, WAIT_DONE_SECONDS, StudioTestCase  # noqa: E402

SHORT_PHASES = ("qc", "baseline_open", "baseline_closed", "scales", "sart",
                "pvt", "monitor", "assessment", "report")
DROPPED_PHASES = ("training", "model")


class ProtocolProfileUnitTests(StudioTestCase):
    """不跑会话的契约检查：配置档、阶段集合、SART 序列、非法档名。"""

    port_base = 18931

    def test_config_exposes_two_profiles(self) -> None:
        payload = self.json_body(self.get("/api/config"), 200, "/api/config 应返回 200")
        profiles = {row["key"]: row for row in payload.get("profiles", [])}
        self.assertEqual(set(profiles), {"full", "short"}, "应恰好暴露 full 与 short 两个协议档")
        self.assertEqual(profiles["full"]["phase_count"], 11, "完整协议 11 步")
        self.assertEqual(profiles["short"]["phase_count"], 9, "短协议 9 步")
        self.assertEqual(profiles["short"]["dropped_phases"], list(DROPPED_PHASES),
                         "短协议去掉的必须是 training 与 model")
        self.assertLess(profiles["short"]["total_sec"], profiles["full"]["total_sec"],
                        "短协议总时长必须小于完整协议")
        self.assertGreater(profiles["short"]["saved_sec"], 300,
                           "节省时间要如实算（SART 缩短 + 去掉训练），不能只减名义值")
        self.assertIn("精度", profiles["short"]["caveat"], "短协议必须自带代价说明")

    def test_phases_payload_follows_profile(self) -> None:
        from ningsi_studio.core import phases as phase_module
        full = [row["key"] for row in phase_module.as_list("full")]
        short = [row["key"] for row in phase_module.as_list("short")]
        self.assertEqual(full, list(phase_module.PHASE_KEYS), "完整协议阶段顺序不能变")
        self.assertEqual(tuple(short), SHORT_PHASES, "短协议阶段集合")
        # 时长按档校正：短协议的 SART 不能还写着完整协议的 420s
        sart_short = next(row for row in phase_module.as_list("short") if row["key"] == "sart")
        sart_full = next(row for row in phase_module.as_list("full") if row["key"] == "sart")
        self.assertLess(sart_short["duration_sec"], sart_full["duration_sec"],
                        "短协议的 SART 预计时长必须跟着缩短")

    def test_unknown_profile_falls_back_and_bad_name_is_rejected(self) -> None:
        from ningsi_studio.core import phases as phase_module
        self.assertEqual(phase_module.normalize_profile(None), "full", "缺省必须是完整协议")
        self.assertEqual(phase_module.normalize_profile(""), "full")
        self.assertEqual(phase_module.normalize_profile("SHORT"), "short", "大小写不敏感")
        self.assertEqual(phase_module.normalize_profile("shrot"), "full",
                         "拼错的档名绝不能当成短协议")
        response = self.post("/api/sessions", {"participant": "pf01", "device": "sim-bsense",
                                               "time_scale": QUICK_TIME_SCALE,
                                               "protocol": "shrot", "create_subject": True})
        self.assert_error(response, 422, "validation_failed", "非法协议档应 422（不是静默回退）")

    def test_sart_sequence_scales_with_profile(self) -> None:
        from ningsi.behavior import sart as sart_module
        short = sart_module.build_sequence("pf01", "01", "001",
                                           trials=90, nogo_trials=10, practice_trials=6)
        self.assertEqual(short.trials, 90)
        self.assertEqual(len(short.nogo_positions), 10)
        self.assertEqual(len(short.practice), 6)
        self.assertEqual(sum(1 for digit in short.digits if digit == sart_module.NOGO_DIGIT), 10,
                         "No-Go 数字必须恰好 10 个")
        full = sart_module.build_sequence("pf01", "01", "001")
        self.assertEqual((full.trials, len(full.nogo_positions), len(full.practice)), (180, 20, 12),
                         "不传参数时必须还是完整协议（历史行为不能变）")


class ShortProtocolSessionTests(StudioTestCase):
    """真跑一次短协议会话：9 个阶段、无训练/模型、行为任务减半、报告有标注。"""

    port_base = 18932

    def test_short_session_end_to_end(self) -> None:
        created = self.json_body(self.post("/api/sessions", {
            "participant": "sp01", "device": "sim-bsense",
            "time_scale": QUICK_TIME_SCALE, "protocol": "short", "create_subject": True,
        }), 201, "短协议会话应能创建")
        uuid = created["session"]["uuid"]
        self.assertEqual(created["session"]["protocol"], "short")
        self.assertEqual(created["session"]["protocol_label"], "短协议")
        keys = [row["key"] for row in created["phases"]]
        self.assertEqual(tuple(keys), SHORT_PHASES, "建会话响应里的阶段表必须是 9 步")

        detail = self.wait_done(uuid)
        self.assertEqual(detail["status"], "done", f"短协议会话应跑完（实际 {detail['status']}）")
        run_phases = [row["phase"] for row in detail.get("runs", [])]
        for key in DROPPED_PHASES:
            self.assertNotIn(key, run_phases, f"短协议不应跑 {key}")
        for key in SHORT_PHASES:
            self.assertIn(key, run_phases, f"短协议应跑 {key}")

        report = self.json_body(self.get(f"/api/sessions/{uuid}/report"), 200, "报告应可读")
        markdown = report.get("report_markdown") or ""
        self.assertIn("检测协议：短协议", markdown, "报告表头必须写明本次是短协议")
        self.assertIn("协议差异提醒", markdown, "短协议必须带差异提醒（否则会被与完整协议直接比较）")
        behavior = report.get("behavior") or {}
        sart = behavior.get("sart") or {}
        pvt = behavior.get("pvt") or {}
        self.assertEqual(sart.get("trials"), 90, "短协议 SART 应为 90 试次")
        self.assertLessEqual(pvt.get("trials", 999), 60, "短协议 PVT 应按 120 秒生成（约 50 余试次）")
        extras = report.get("extras") or {}
        self.assertEqual(extras.get("protocol_label"), "短协议")
        self.assertIn("神经反馈训练", "、".join(extras.get("skipped_phases") or []),
                      "报告 extras 要列出被跳过的阶段")

    def wait_done(self, uuid: str) -> dict:
        deadline = time.time() + WAIT_DONE_SECONDS
        detail: dict = {}
        while time.time() < deadline:
            detail = self.json_body(self.get(f"/api/sessions/{uuid}"), 200, "会话详情应可读")
            if detail.get("status") in ("done", "failed", "cancelled"):
                return detail
            time.sleep(2.0)
        self.fail(f"会话 {uuid[:8]} 在 {WAIT_DONE_SECONDS:.0f}s 内没有结束：{json.dumps(detail)[:200]}")

    def test_default_session_stays_full_protocol(self) -> None:
        """不传 protocol ⇒ 完整协议（默认档不能被动过）。"""
        created = self.json_body(self.post("/api/sessions", {
            "participant": "sp02", "device": "sim-bsense",
            "time_scale": QUICK_TIME_SCALE, "create_subject": True,
        }), 201, "缺省协议会话应能创建")
        self.assertEqual(created["session"]["protocol"], "full", "缺省必须是完整协议")
        self.assertEqual(len(created["phases"]), 11, "缺省协议应是 11 步")
        self.delete(f"/api/sessions/{created['session']['uuid']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
