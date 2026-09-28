"""持久化：关停服务后用同一 data_dir 重开，被试/会话/指标/量表/行为/训练/产物仍在；
schema_version 幂等；repository 的 update_run_payload / finish_run 行为。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

try:                                              # 支持直接以脚本方式运行本文件
    from tests.helpers import (StudioTestCase, read_alerts, read_artifacts, read_behavior_run,
                               read_behavior_runs, read_metrics, read_runs, read_scale_runs,
                               read_training_segments)
except ModuleNotFoundError:                       # pragma: no cover - 仅脚本运行场景
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.helpers import (StudioTestCase, read_alerts, read_artifacts, read_behavior_run,
                               read_behavior_runs, read_metrics, read_runs, read_scale_runs,
                               read_training_segments)

from ningsi import config
from ningsi_studio.db import repository as repo
from ningsi_studio.db import sqlite_store as store


class PersistenceTests(StudioTestCase):
    """真实落盘 + 关停重开。"""

    port_base = 18960

    def test_data_survives_service_restart(self) -> None:
        self.create_subject(public_id="d01", auto_id=False, label="持久化被试", note="重启后应仍在")
        session = self.create_session("d01")
        uuid = session["uuid"]
        detail = self.wait_session_done(uuid)
        self.assertEqual(detail["status"], "done", "会话应先正常跑完")

        # 重启前后都应存在的产物文件（重启后按库里的路径校验）
        self.assertTrue(read_artifacts(self.db_path, uuid), "重启前应已登记产物")

        self.restart_service()                            # 使用同一 data_dir 重开服务

        subjects = self.json_body(self.get("/api/subjects?query=d01"), 200, "重启后列表应返回 200")
        self.assertEqual(subjects["total"], 1, "重启后被试应仍在（同库同目录）")
        self.assertEqual(subjects["items"][0]["public_id"], "d01", "重启后被试编号应一致")

        after = self.session_detail(uuid)
        self.assertEqual(after["status"], "done", "重启后会话状态应保持 done")
        self.assertEqual(after["participant"], "d01", "重启后应仍能关联到被试")
        self.assertEqual(after["phase"], "done", "重启后 phase 应保持 done")
        self.assertEqual([item["phase"] for item in after["runs"]], list(_expected_phases()),
                         "重启后 runs 的阶段顺序应完整")
        self.assertTrue(after["indicator_summary"], "重启后指标汇总应仍在")
        self.assertTrue(after["artifacts"], "重启后产物登记应仍在")
        for item in after["artifacts"]:
            self.assertTrue(Path(item["path"]).exists(), f"产物文件应仍在磁盘：{item['path']}")
        self.assertEqual(len(read_artifacts(self.db_path, uuid)), len(after["artifacts"]),
                         "artifacts 表与接口读数应一致")
        self.assertEqual(len(read_alerts(self.db_path, uuid)), len(after["alerts"]),
                         "alerts 表与接口读数应一致")

        report = self.json_body(self.get(f"/api/sessions/{uuid}/report"), 200,
                                "重启后报告应可直接读出")
        self.assertEqual(report["spec"], config.LABEL_SPEC, "报告口径应为 joint-assessment-v1")
        self.assertEqual(sorted(report["scales"]), ["SAS", "SDS"], "重启后量表结果应仍在")
        self.assertEqual(sorted(report["behavior"]), ["pvt", "sart"], "重启后行为结果应仍在")
        self.assertIn("report_markdown", report, "报告应附带 Markdown 原文")
        self.assertTrue(Path(report["report_markdown_path"]).exists(),
                        "报告 Markdown 文件应仍在磁盘")

        training = self.json_body(self.get(f"/api/sessions/{uuid}/training"), 200,
                                  "重启后训练记录应可读出")
        self.assertEqual(len(training["segments"]), 2, "quick 模式应有 2 个训练分段")
        self.assertTrue(training["summary"], "重启后训练汇总应非空")

        # 直接读库核对（真实 SQLite，不做任何替换）
        self.assertEqual(store.schema_versions(Path(self.db_path)), ["0001-init"],
                         "重启后 schema_version 应仍只有一条")
        self.assertTrue(read_runs(self.db_path, uuid), "runs 表应仍可读")
        self.assertGreaterEqual(len(read_metrics(self.db_path, uuid, kind="indicator")), 30,
                                "逐窗指标应落库（监测阶段 35 窗）")
        self.assertEqual(len(read_scale_runs(self.db_path, uuid)), 2, "SAS/SDS 各一行量表记录")
        self.assertEqual(len(read_behavior_runs(self.db_path, uuid)), 2, "SART/PVT 各一行行为记录")
        self.assertEqual(len(read_training_segments(self.db_path, uuid)), 2, "训练分段应落库 2 行")
        self.assertTrue(read_behavior_run(self.db_path, uuid, "sart"), "SART 结果应以 sart 为键落库")
        self.assertTrue(read_behavior_run(self.db_path, uuid, "pvt-b"),
                        "PVT 结果应以 pvt-b 为键落库（与上游口径一致）")

        # 重启后不再有运行器，但落库结果仍应能通过接口读到
        sart = self.json_body(self.get(f"/api/sessions/{uuid}/behaviors/sart/result"), 200,
                              "重启后 SART 结果应能从库中读出")
        self.assertEqual(sart["result"]["trials"], 180, "重启后 SART 试次数应为 180")
        pvt = self.json_body(self.get(f"/api/sessions/{uuid}/behaviors/pvt/result"), 200,
                             "重启后 PVT 结果应能从库中读出")
        self.assertEqual(pvt["result"]["task"], "pvt-b", "重启后 PVT 结果应可读")

    def test_schema_version_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ningsi-schema-",
                                         ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "studio.sqlite3"
            for _ in range(3):
                store.initialize(db_path)
            self.assertEqual(store.schema_versions(db_path), ["0001-init"],
                             "重复 initialize 应只写入一条 schema_version")
            with store.read_only(db_path) as conn:
                tables = {row["name"] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")}
            for table in ("subjects", "sessions", "runs", "metrics", "alerts", "scale_runs",
                          "behavior_runs", "training_segments", "artifacts", "schema_version"):
                self.assertIn(table, tables, f"表 {table} 应存在")

    def test_repository_run_payload_and_finish(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ningsi-repo-",
                                         ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "studio.sqlite3"
            store.initialize(db_path)
            with store.connect(db_path) as conn:
                subject_id = repo.create_subject(conn, "r01", label="仓储用例")
                session = repo.create_session(conn, subject_id, time_scale=0.05)
                run_id = repo.start_run(conn, session["id"], "qc")

            with store.read_only(db_path) as conn:
                run = repo.list_runs(conn, session["id"])[0]
            self.assertEqual(run["status"], "running", "start_run 应写入 running 状态")
            self.assertEqual(json.loads(run["payload"]), {}, "start_run 的 payload 应为空对象")

            # update_run_payload：JSON 合并，不改状态
            with store.connect(db_path) as conn:
                repo.update_run_payload(conn, run_id, {"quality": {"passed": True}, "windows": 5})
            with store.connect(db_path) as conn:
                repo.update_run_payload(conn, run_id, {"quality": {"valid_ratio": 0.75},
                                                       "note": "二次写入"})
            with store.read_only(db_path) as conn:
                run = repo.list_runs(conn, session["id"])[0]
            payload = json.loads(run["payload"])
            self.assertEqual(run["status"], "running", "update_run_payload 不应改变状态")
            self.assertEqual(payload["quality"], {"passed": True, "valid_ratio": 0.75},
                             "多次 update_run_payload 应做嵌套合并")
            self.assertEqual(payload["windows"], 5, "未涉及的键应保留")
            self.assertEqual(payload["note"], "二次写入", "新增键应写入")

            # payload 非法 JSON 时退化为整体写入
            with store.connect(db_path) as conn:
                conn.execute("UPDATE runs SET payload = ? WHERE id = ?", ("not-json", run_id))
                repo.update_run_payload(conn, run_id, {"recovered": True})
            with store.read_only(db_path) as conn:
                run = repo.list_runs(conn, session["id"])[0]
            self.assertEqual(json.loads(run["payload"]), {"recovered": True},
                             "payload 非 JSON 时应被整体替换为 patch")

            # finish_run：写入终态、耗时、可用窗比例与错误，并整体覆盖 payload
            with store.connect(db_path) as conn:
                affected = repo.finish_run(conn, run_id, status="done", duration_ms=1234,
                                           valid_ratio=0.8, error=None,
                                           payload={"status": "done"})
            self.assertEqual(affected, 1, "finish_run 应更新 1 行")
            with store.read_only(db_path) as conn:
                run = repo.list_runs(conn, session["id"])[0]
            self.assertEqual(run["status"], "done", "finish_run 应写入状态")
            self.assertEqual(run["duration_ms"], 1234, "finish_run 应写入耗时")
            self.assertEqual(run["valid_ratio"], 0.8, "finish_run 应写入可用窗比例")
            self.assertIsNone(run["error"], "错误为空时应写入 NULL")
            self.assertTrue(run["ended_at"], "finish_run 应写入结束时间")
            self.assertEqual(json.loads(run["payload"]), {"status": "done"},
                             "finish_run 应整体覆盖 payload")


def _expected_phases() -> tuple:
    """完整流程的阶段顺序（与 ningsi_studio.core.phases 一致）。"""
    return ("qc", "baseline_open", "baseline_closed", "scales", "sart", "pvt",
            "monitor", "training", "assessment", "model", "report")


if __name__ == "__main__":                        # pragma: no cover - 便于单文件调试
    import unittest

    unittest.main()
