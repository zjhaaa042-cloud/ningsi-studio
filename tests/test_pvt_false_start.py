"""PVT-B 抢答（false start）真上报 —— 机制级单测（毫秒级、确定性）。

背景（2026-10-07 用户确认）：原来抢答只在界面上提示"不计入"，数据库里 `false_starts` 恒为 0。
现在前端在等待期 POST `{responded: false, rt: null, false_start: true}`；服务端
`provide_input` 把作答**预置**在事件上，紧接着的 `_wait_trial("pvt", ...)` 立刻消费它 ——
该试次被记为抢答，且**不伪造反应时**（`responded=false` / `rt=None`）。

为什么这里测**运行时机制**而不是跑一整场会话：`time_scale=0.2` 时试次窗口只有
0.35–0.44 秒，HTTP 轮询式的"机器人作答"必然错过窗口（实测序号会漂移、SART 被误判失败）；
而这条机制（预置 → 下个等待点消费 → false_start 落账）与时间尺度无关，直接测更准。
端到端那一段由浏览器探针 `_analysis/lead_verify_trial_stage.py` 覆盖：它在真实 PVT 等待期
按空格，然后断言页面出现**服务端**发布的「抢答已记账」通知。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ningsi_studio.core.runtime import SessionRuntime  # noqa: E402


class PvtFalseStartMechanismTests(unittest.TestCase):
    """不连数据库、不起线程：只验 provide_input / _wait_trial 的抢答语义。"""

    def setUp(self) -> None:
        self.runtime = SessionRuntime("0" * 32)

    def test_early_press_is_delivered_as_false_start(self) -> None:
        # 1) 刺激还没出现就按：此时没有任何等待点，provide_input 必须接受并预置
        accepted = self.runtime.provide_input("pvt", {"index": 0, "responded": False,
                                                      "rt": None, "false_start": True})
        self.assertTrue(accepted, "抢答必须被接受（不能 409）")
        # 2) 紧接着的试次等待点立刻消费它（不等待窗口，说明它已预置）
        started = time.monotonic()
        answer = self.runtime._wait_trial("pvt", {"task": "pvt", "index": 0},
                                          auto=False, fallback={"responded": False, "rt": None},
                                          window=1.5)
        elapsed = time.monotonic() - started
        self.assertTrue(answer.get("false_start"), f"应记为抢答：{answer}")
        self.assertFalse(answer.get("responded"), "抢答不算有效作答")
        self.assertIsNone(answer.get("rt"), "抢答不能伪造反应时")
        self.assertLess(elapsed, 0.5, f"预置的抢答应被立即消费（实测 {elapsed:.3f}s）")

    def test_normal_answer_is_not_marked_false_start(self) -> None:
        # 对照组：刺激出现后按键 → false_start 必须为 False，且带反应时
        self.runtime.provide_input("pvt", {"index": 0, "responded": True, "rt": 0.42,
                                           "false_start": False})
        answer = self.runtime._wait_trial("pvt", {"task": "pvt", "index": 0},
                                          auto=False, fallback={"responded": False, "rt": None},
                                          window=1.5)
        self.assertFalse(answer.get("false_start"))
        self.assertTrue(answer.get("responded"))
        self.assertAlmostEqual(float(answer.get("rt")), 0.42, places=3)

    def test_index_mismatch_is_not_counted_as_that_trial(self) -> None:
        """错位作答（前端定时器晚到）按"未作答"记账，不会被算到下一个试次头上。"""
        self.runtime.provide_input("pvt", {"index": 99, "responded": True, "rt": 0.3,
                                           "false_start": False})
        answer = self.runtime._wait_trial("pvt", {"task": "pvt", "index": 4},
                                          auto=False, fallback={"responded": False, "rt": None},
                                          window=0.05)
        self.assertFalse(answer.get("responded"), "index 不匹配时不能当成有效作答")
        self.assertIsNone(answer.get("rt"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
