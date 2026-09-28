"""被试接口：创建 / 自动编号 / 重复 409 / 格式 422 / 列表分页搜索 / 详情 / PATCH / 会话列表。"""

from __future__ import annotations

try:                                              # 支持直接以脚本方式运行本文件
    from tests.helpers import StudioTestCase
except ModuleNotFoundError:                       # pragma: no cover - 仅脚本运行场景
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.helpers import StudioTestCase


class SubjectCreateTests(StudioTestCase):
    """创建被试与编号规则。"""

    port_base = 18910

    def test_create_subject_with_auto_id(self) -> None:
        first = self.create_subject(label="演示被试", age_band="18-25", sex="女",
                                    handedness="右", note="仅用于演示")
        self.assertRegex(first["public_id"], r"^p\d{2}$", "自动编号应形如 p01")
        self.assertEqual(first["label"], "演示被试", "别名应回显")
        self.assertEqual(first["age_band"], "18-25", "年龄段应回显")
        self.assertEqual(first["sex"], "女", "性别应回显")
        self.assertEqual(first["handedness"], "右", "利手应回显")
        self.assertEqual(first["note"], "仅用于演示", "备注应回显")
        self.assertEqual(first["consent_version"], "consent-v1", "未指定同意版本时应写入默认值")
        self.assertTrue(first["consent_at"], "同意时间应自动落库")
        self.assertTrue(first["created_at"], "created_at 不应为空")
        self.assertTrue(first["updated_at"], "updated_at 不应为空")
        self.assertEqual(first["sessions"], {"total": 0}, "新建被试的会话计数应为 0")

        second = self.create_subject(label="第二位")
        self.assertEqual(int(second["public_id"][1:]), int(first["public_id"][1:]) + 1,
                         "自动编号应逐个递增")

    def test_create_subject_with_explicit_id(self) -> None:
        item = self.create_subject(public_id="q01", auto_id=False)
        self.assertEqual(item["public_id"], "q01", "显式编号应原样保存")
        detail = self.json_body(self.get("/api/subjects/q01"), 200, "被试明细应可读回")
        self.assertEqual(detail["public_id"], "q01", "按编号应能查到被试")
        # 带 sub- 前缀的编号会被正规化成同一份记录
        self.assertEqual(detail["consent_version"], "consent-v1", "同意版本应有默认值")

    def test_duplicate_public_id_returns_409(self) -> None:
        self.create_subject(public_id="q02")
        error = self.assert_error(self.post("/api/subjects", {"public_id": "q02"}),
                                  409, "conflict", "编号重复应返回 409")
        self.assertEqual((error.get("detail") or {}).get("public_id"), "q02",
                         "409 响应应带重复的编号")

    def test_invalid_public_id_returns_422(self) -> None:
        self.assert_error(self.post("/api/subjects", {"auto_id": False, "public_id": "p12345"}),
                          422, "validation_failed", "超长数字编号应返回 422")
        self.assert_error(self.post("/api/subjects", {"auto_id": False, "public_id": ""}),
                          422, "validation_failed", "空编号应返回 422")
        self.assert_error(self.post("/api/subjects", {"auto_id": False, "public_id": "!!"}),
                          422, "validation_failed", "非法字符编号应返回 422")

    def test_unknown_subject_returns_404(self) -> None:
        self.assert_error(self.get("/api/subjects/p77"), 404, "not_found",
                          "不存在的被试应返回 404")


class SubjectListTests(StudioTestCase):
    """列表分页与搜索。"""

    port_base = 18912

    def test_pagination_and_search(self) -> None:
        for index in range(3):
            self.create_subject(public_id=f"q1{index}", auto_id=False,
                                label=f"分页测试{index}", note="列表用例")

        page = self.json_body(self.get("/api/subjects?limit=2&page=1"), 200, "被试列表应返回 200")
        self.assertEqual(page["limit"], 2, "limit 应回显请求值")
        self.assertEqual(page["page"], 1, "page 应从 1 开始")
        self.assertGreaterEqual(page["total"], 3, "总数应覆盖已创建的被试")
        self.assertEqual(len(page["items"]), 2, "limit=2 时每页最多 2 条")
        for item in page["items"]:
            for key in ("public_id", "label", "consent_version", "created_at", "sessions"):
                self.assertIn(key, item, f"列表项应含 {key}")

        second = self.json_body(self.get("/api/subjects?limit=2&page=2"), 200, "第二页应返回 200")
        self.assertEqual(second["page"], 2, "第二页的 page 应为 2")
        self.assertNotEqual(page["items"][0]["public_id"], second["items"][0]["public_id"],
                            "分页结果不应重复")

        by_id = self.json_body(self.get("/api/subjects?query=q11"), 200, "按编号搜索应返回 200")
        self.assertEqual(by_id["total"], 1, "按编号搜索应命中 1 条")
        self.assertEqual(by_id["items"][0]["public_id"], "q11", "命中的应是目标被试")

        by_label = self.json_body(self.get("/api/subjects?query=分页测试2"), 200, "按别名搜索应返回 200")
        self.assertEqual(by_label["total"], 1, "按别名搜索应命中 1 条")
        self.assertEqual(by_label["items"][0]["label"], "分页测试2", "命中的应是目标别名")

        empty = self.json_body(self.get("/api/subjects?query=不存在的关键字xyz"), 200,
                               "无结果也应返回 200")
        self.assertEqual(empty["total"], 0, "无匹配时应返回 0 条")
        self.assertEqual(empty["items"], [], "无匹配时 items 应为空数组")

    def test_invalid_page_parameter_returns_422(self) -> None:
        self.assert_error(self.get("/api/subjects?page=abc"), 422, "validation_failed",
                          "page 非整数应返回 422")


class SubjectDetailTests(StudioTestCase):
    """详情与 PATCH。"""

    port_base = 18914

    def test_detail_and_patch(self) -> None:
        self.create_subject(public_id="q21", auto_id=False, label="原别名", age_band="18-25",
                            sex="男", handedness="左", consent_version="consent-v2",
                            note="原始备注")

        detail = self.json_body(self.get("/api/subjects/q21"), 200, "被试明细应返回 200")
        self.assertEqual(detail["label"], "原别名", "明细应回显别名")
        self.assertEqual(detail["consent_version"], "consent-v2", "明细应回显同意版本")
        self.assertEqual(detail["sessions"], {"total": 0}, "未建会话时应只有 total")

        patched = self.json_body(self.patch("/api/subjects/q21", {
            "label": "新别名", "age_band": "26-35", "sex": "女", "handedness": "右",
            "note": "更新后的备注", "consent_version": "consent-v2",
            "consent_at": "2026-01-01T00:00:00+00:00",
        }), 200, "PATCH 应返回 200 与更新后的对象")
        self.assertEqual(patched["public_id"], "q21", "PATCH 不应改变被试编号")
        self.assertEqual(patched["label"], "新别名", "PATCH 应更新别名")
        self.assertEqual(patched["age_band"], "26-35", "PATCH 应更新年龄段")
        self.assertEqual(patched["sex"], "女", "PATCH 应更新性别")
        self.assertEqual(patched["handedness"], "右", "PATCH 应更新利手")
        self.assertEqual(patched["note"], "更新后的备注", "PATCH 应更新备注")
        self.assertEqual(patched["consent_at"], "2026-01-01T00:00:00+00:00", "PATCH 应更新同意时间")

        again = self.json_body(self.get("/api/subjects/q21"), 200, "PATCH 后应能读回")
        self.assertEqual(again["label"], "新别名", "PATCH 结果应已持久化")

    def test_patch_unknown_subject_returns_404(self) -> None:
        self.assert_error(self.patch("/api/subjects/p78", {"label": "x"}), 404, "not_found",
                          "PATCH 不存在的被试应返回 404")

    def test_patch_is_partial(self) -> None:
        """局部更新：未提供的字段应保持原值。"""
        self.create_subject(public_id="q22", auto_id=False, label="仅改别名前",
                            age_band="18-25", sex="女", note="原备注")
        patched = self.json_body(self.patch("/api/subjects/q22", {"label": "仅改别名后"}),
                                 200, "只改别名的 PATCH 应返回 200")
        self.assertEqual(patched["label"], "仅改别名后", "别名应被更新")
        self.assertEqual(patched["public_id"], "q22", "编号不应变化")
        self.assertEqual(patched["age_band"], "18-25", "未提供的年龄段应保持原值")
        self.assertEqual(patched["sex"], "女", "未提供的性别应保持原值")
        self.assertEqual(patched["note"], "原备注", "未提供的备注应保持原值")

        detail = self.json_body(self.get("/api/subjects/q22"), 200, "PATCH 后应能读回")
        self.assertEqual(detail["age_band"], "18-25", "局部更新结果应已持久化")

    def test_patch_without_fields_returns_422(self) -> None:
        self.create_subject(public_id="q23", auto_id=False, label="空更新")
        error = self.assert_error(self.patch("/api/subjects/q23", {}), 422, "validation_failed",
                                  "没有任何可更新字段的 PATCH 应返回 422")
        self.assertIn("allowed", error.get("detail") or {}, "422 响应应列出可更新字段")


class SubjectSessionTests(StudioTestCase):
    """被试会话列表。"""

    port_base = 18916

    def test_subject_sessions_list(self) -> None:
        self.create_subject(public_id="q31", auto_id=False, label="有会话的被试")
        session = self.create_session("q31", time_scale=1.0)   # 真实节奏：本用例内不会跑完

        payload = self.json_body(self.get("/api/subjects/q31/sessions"), 200,
                                 "被试会话列表应返回 200")
        self.assertEqual(payload["participant"], "q31", "应回显被试编号")
        self.assertGreaterEqual(payload["total"], 1, "应至少有 1 条会话")
        self.assertEqual(len(payload["items"]), payload["total"], "未分页时 items 应等于 total")
        self.assertEqual(payload["limit"], 50, "列表应回显 limit（与 /api/sessions 同构）")
        self.assertEqual(payload["page"], 1, "列表应回显 page")
        item = payload["items"][0]
        self.assertEqual(item["uuid"], session["uuid"], "应列出刚创建的会话")
        self.assertIn("alert_count", item, "列表项应含 alert_count")
        self.assertIn("status", item, "列表项应含 status")

        detail = self.json_body(self.get("/api/subjects/q31"), 200, "被试明细应返回 200")
        self.assertGreaterEqual(detail["sessions"]["total"], 1, "被试明细应统计会话数")

    def test_subject_sessions_unknown_subject_returns_404(self) -> None:
        self.assert_error(self.get("/api/subjects/p79/sessions"), 404, "not_found",
                          "不存在的被试的会话列表应返回 404")


if __name__ == "__main__":                        # pragma: no cover - 便于单文件调试
    import unittest

    unittest.main()
