"""会话运行时：把八阶段流程放进后台线程，逐窗推进并通过 SSE 实时推送。

设计约定：
- 运行线程只碰 SQLite（短连接）与事件总线，HTTP 线程只读；
- 每个阶段用 `_phase()` 上下文管理器包裹：写 runs 表、广播 phase 事件、异常隔离；
- 交互阶段（量表 / SART / PVT）等待 API 注入作答，带超时；
- `time_scale < 1` 时压缩阶段时长（算法口径不变），`auto=True` 时由服务端生成
  确定性作答，使端到端演示与自动化测试无需浏览器。
"""

from __future__ import annotations

import logging
import random
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from ningsi import config
from ningsi.monitoring import history as history_module
from ningsi.monitoring.heatmap import render_svg as heatmap_svg

from ningsi_studio.core import paired_ledger, phases as phase_module
from ningsi_studio.core.live_source import build_source
from ningsi_studio.db import repository as repo
from ningsi_studio.db import sqlite_store as store
from ningsi_studio.domain import assessment as assessment_domain
from ningsi_studio.domain import behavior as behavior_domain
from ningsi_studio.domain import export as export_domain
from ningsi_studio.domain import indicators as indicators_domain
from ningsi_studio.domain import model_training, scales as scales_domain
from ningsi_studio.domain import training as training_domain
from ningsi_studio.http.sse import EventBus
from ningsi_studio.settings import Settings

LOGGER = logging.getLogger("ningsi_studio.runtime")

INPUT_TIMEOUT_SEC = 900.0        # 交互阶段（量表）等待作答的上限（15 分钟）

#: 行为任务每个试次的**作答窗口**（秒）：窗口结束仍未按键，就按"未作答/正确抑制"记账。
#:
#: 为什么必须有：SART 的 No-Go 试次（数字 3）**正确做法就是不按键**，而试次是"服务端发一个、
#: 前端答一个"驱动的——前端不提交，服务端就会一直 `wait_for_input()` 等到 INPUT_TIMEOUT_SEC
#: （900 秒），现场看到的就是"显示 3 之后整个任务不动了"。
#: 窗口同时决定了刺激节拍（固定 SOA）：服务端按窗口补齐间隔，Go 试次与 No-Go 试次间隔一致，
#: 反应时与变异系数才可比。
SART_TRIAL_WINDOW = {"practice": 1.6, "main": 2.2}
PVT_TRIAL_WINDOW = 3.0
#: 服务端比前端多等这么多：给前端定时器与网络留抖动余量
TRIAL_GRACE_SEC = 0.6
#: 折算时间倍率后，作答窗口不得小于这个真实秒数（再快就不可能有人答得上了）
MIN_TRIAL_WINDOW = 0.35
#: 连续这么多个试次都没收到作答 ⇒ 判定前端已掉线/关页，让该阶段明确失败（而不是空转 15 分钟）
BEHAVIOR_MISS_LIMIT = 5


class SessionCancelled(RuntimeError):
    """会话被外部取消。"""


def schemas_bool(value, default: bool = False) -> bool:
    """把前端可能传来的真值（true/"true"/1/"1"）统一成布尔。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class SessionRuntime:
    def __init__(self, session_uuid: str, *, settings: Settings | None = None,
                 bus: EventBus | None = None) -> None:
        self.settings = settings or Settings()
        self.uuid = session_uuid
        self.bus = bus or EventBus(session_uuid)
        self.cancel_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._input_events: dict[str, threading.Event] = {}
        self._input_payload: dict[str, dict] = {}
        self._input_lock = threading.Lock()
        self._lock = threading.Lock()
        self._state = threading.Event()          # 临时存放的进度/阶段快照

        # 由 _load 填充
        self.session = None
        self.subject = None
        self.db_path = Path(self.settings.db_path)
        self.source = None
        self.engine = None
        self.heatmap_series: list[tuple[float, float | None]] = []
        self.artifacts: dict[str, Path] = {}
        self.summary: dict = {}

        # 交互状态
        self.sart_task = None
        self.pvt_task = None
        self.scale_answers: dict[str, list[int]] = {}
        self.scale_results: dict[str, dict] = {}
        self.baselines: dict = {}
        self.device_error: str | None = None
        # 行为任务连续"没拿到有效作答"的计数（见 _wait_trial）
        self._behavior_misses = 0

    # ------------------------------------------------------------------ 线程
    def start(self) -> None:
        """同步完成装载（含数据源选择），再起线程，保证创建接口能立刻读到 source。"""
        self._load()
        self._thread = threading.Thread(target=self._guarded_run,
                                        name=f"session-{self.uuid[:8]}", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._input_lock:
            names = list(self._input_events)
        for name in names:
            self.provide_input(name, None)
        try:
            self.bus.publish("cancelled", {"message": "会话已被用户取消"})
        except Exception:  # noqa: BLE001
            pass

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    # ------------------------------------------------------------- 外部注入
    def wait_for_input(self, name: str, timeout: float = INPUT_TIMEOUT_SEC) -> dict:
        """等待前端提交的作答。

        分段等待（每段 0.5 秒）而不是一次 `wait(timeout)`：这样会话被取消时能在半秒内
        立即响应，而不是傻等到总超时；无论成功、取消还是超时都清理该交互点，避免事件泄漏。
        """
        event = self._register_input(name)
        waited = 0.0
        try:
            while waited < timeout:
                self._check_cancel()
                if event.wait(min(0.5, max(0.05, timeout - waited))):
                    break
                waited += 0.5
        finally:
            with self._input_lock:
                payload = self._input_payload.pop(name, {}) or {}
                self._input_events.pop(name, None)
        return payload

    def provide_input(self, name: str, payload) -> bool:
        """把作答交给当前试次；若上一个试次的作答尚未被消费，则拒绝（返回 False）。

        拒绝而不是覆盖：覆盖会让两次提交塌缩成一次，试次序号随即对不上，
        上游的顺序校验会直接判失败；明确拒绝能让前端立刻知道"这一帧没进去"。
        """
        with self._input_lock:
            event = self._input_events.get(name)
            if event is None:
                event = threading.Event()
                self._input_events[name] = event
            elif not event.is_set() and name in self._input_payload:
                return False                       # 已有待消费作答，避免静默覆盖
            self._input_payload[name] = payload or {}
            event.set()
        return True

    def _register_input(self, name: str) -> threading.Event:
        with self._input_lock:
            event = self._input_events.get(name)
            if event is None:
                event = threading.Event()
                self._input_events[name] = event
            return event

    def awaiting(self) -> list[str]:
        with self._input_lock:
            return [name for name, event in self._input_events.items() if not event.is_set()]

    # ------------------------------------------------------------------ 执行
    def _guarded_run(self) -> None:
        try:
            self._run()
        except SessionCancelled:
            self._finish("cancelled", "会话已取消")
        except Exception as exc:  # noqa: BLE001 - 运行线程必须兜底
            LOGGER.exception("session %s failed", self.uuid)
            detail = traceback.format_exc(limit=3)
            self.bus.publish("error", {"message": str(exc), "detail": detail})
            self._finish("failed", str(exc))
        finally:
            self.bus.close()

    def _load(self) -> None:
        """装载会话与数据源；幂等（`start()` 与运行线程各调用一次）。"""
        if self.session is not None:
            return
        with store.connect(self.db_path) as conn:
            self.session = repo.get_session(conn, self.uuid)
            if self.session is None:
                raise RuntimeError(f"会话不存在：{self.uuid}")
            self.subject = repo.get_subject_by_pk(conn, self.session["subject_id"])
        device = self.session["device"] or "sim-bsense"
        try:
            self.source = build_source(
                device,
                channels=int(self.session["channels"] or 1),
                srate=float(self.session["srate"] or 250.0),
                seed=int(abs(hash(self.uuid)) % 10000),
            )
        except Exception as exc:  # noqa: BLE001 - 真实设备不可用时降级而不是让会话失败
            self.source = build_source("sim-bsense",
                                       channels=int(self.session["channels"] or 1),
                                       srate=float(self.session["srate"] or 250.0),
                                       seed=int(abs(hash(self.uuid)) % 10000))
            self.source.note = (f"真实设备 {device} 不可用（{exc}），已降级为仿真源；"
                                f"数据来源已在界面与报告中标注")
            self.device_error = str(exc)
        else:
            self.device_error = None

    @property
    def scale(self) -> float:
        try:
            value = float(self.session["time_scale"])
        except (TypeError, ValueError, KeyError):
            value = 1.0
        return max(0.01, min(1.0, value))

    def _sleep(self, seconds: float) -> None:
        """按时间倍率休眠；倍率越小演示越快（算法参数不变）。"""
        self._sleep_real(seconds * self.scale)

    def _sleep_real(self, seconds: float) -> None:
        """按真实秒休眠（已经是折算过的量，不再乘 time_scale）。"""
        if seconds <= 0:
            return
        remaining = seconds
        while remaining > 0:
            if self.cancel_event.is_set():
                raise SessionCancelled()
            chunk = min(0.1, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise SessionCancelled()

    # -------------------------------------------------------------- 阶段管理
    @contextmanager
    def _phase(self, key: str, run_kind: str = None):
        label = phase_module.PHASE_BY_KEY[key].label
        started = time.time()
        run_id = None
        with store.connect(self.db_path) as conn:
            run_id = repo.start_run(conn, self.session["id"], key)
            repo.update_session(conn, self.uuid, phase=key,
                                progress=phase_module.progress_for(key, 0.0))
        self.bus.publish("phase", {"key": key, "label": label, "state": "running",
                                   "progress": phase_module.progress_for(key, 0.0)})
        context = {"run_id": run_id, "key": key}
        try:
            yield context
        except SessionCancelled:
            self._close_run(run_id, "cancelled", started, "会话取消")
            raise
        except Exception as exc:
            self._close_run(run_id, "failed", started, str(exc))
            raise
        else:
            self._close_run(run_id, "done", started, None)
            self.bus.publish("phase", {"key": key, "label": label, "state": "done",
                                       "progress": phase_module.progress_for(key, 1.0)})

    def _close_run(self, run_id, status: str, started: float, error) -> None:
        """收尾一个阶段：只更新状态/耗时，**保留**阶段过程中已写入的中间结果。

        之前这里直接传 `payload={"status": status}` 会把 `_merge_run_payload` 累积的
        基线、表计分、行为结果、评估结论等全部覆盖掉，导致
        `/assessment`、`/training`、`/report` 读不到数据。
        """
        try:
            with store.connect(self.db_path) as conn:
                repo.finish_run(conn, run_id, status=status,
                                duration_ms=int((time.time() - started) * 1000),
                                error=error)
                repo.update_run_payload(conn, run_id, {"status": status})
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("写 runs 失败：%s", exc)

    def _progress(self, key: str, within: float, **extra) -> None:
        payload = {"key": key, "progress": phase_module.progress_for(key, within), **extra}
        self.bus.publish("progress", payload)
        try:
            with store.connect(self.db_path) as conn:
                repo.update_session(conn, self.uuid, progress=payload["progress"])
        except Exception:  # noqa: BLE001
            pass

    def _publish_window(self, step, *, phase: str, include_signal: bool) -> None:
        """推送一窗结果。总进度由 `_progress()` 按窗序号推进，这里不再重复计算。"""
        payload = step.payload(include_signal=include_signal)
        payload["phase"] = phase
        self.bus.publish("window", payload)
        try:
            with store.connect(self.db_path) as conn:
                if not step.scores:
                    # 该窗有信号但没有可用指标（例如基线无效或该窗被判为伪迹）：
                    # 补一条空值占位，热力图才能如实画出"缺失"而不是整段空洞。
                    repo.add_metric(conn, self.session["id"], "indicator", "focus",
                                    t_sec=step.t_end, value=None,
                                    valid_ratio=1.0 if step.usable else 0.0)
                for name, score in (step.scores or {}).items():
                    repo.add_metric(conn, self.session["id"], "indicator", name,
                                    t_sec=step.t_end, value=score,
                                    valid_ratio=1.0 if step.usable else 0.0)
                for event in step.alerts:
                    repo.add_alert(conn, self.session["id"], event["kind"], event["state"],
                                   t_sec=event["t"], value=event["value"],
                                   sustained_sec=event["sustained_sec"], message=event["message"])
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("写窗级指标失败：%s", exc)

    # ------------------------------------------------------------------ 主流程
    def _run(self) -> None:
        self._load()
        time_scale = self.scale
        # 快速演示模式：时间倍率小于 0.2 即认为"无人值守演示"，交互阶段由服务端生成
        # 确定性作答。判断只看归整后的 self.scale，不解析原始字段，避免两套开关打架。
        auto = time_scale < 0.2
        self.bus.publish("started", {
            "uuid": self.uuid,
            "participant": self.subject["public_id"],
            "device": self.source.device,
            "source_kind": self.source.kind,
            "source_note": self.source.note,
            "time_scale": time_scale,
            "auto": auto,
            "phases": phase_module.as_list(),
            "engine_versions": {"spectrum": config.SPECTRUM_SPEC,
                                "indicator": config.INDICATOR_SPEC,
                                "baseline": config.BASELINE_SPEC,
                                "assessment": config.LABEL_SPEC},
        })
        if self.source.kind != "lsl":
            self.bus.publish("notice", {
                "level": "warning",
                "message": self.source.note or "当前使用仿真脑电源，数据来源已在界面与报告中标注。",
                "device_error": self.device_error,
            })

        self.engine = indicators_domain.WindowEngine(
            self.source.engine, self.source.srate, device=self.source.device
        )

        # 1) 设备质检
        with self._phase("qc") as context:
            qc = self.engine.device_qc()
            self._merge_run_payload(context["run_id"], qc)
            self.bus.publish("quality", qc)
            if not qc.get("passed"):
                self.bus.publish("notice", {
                    "level": "warning",
                    "message": f"质检通过窗 {qc['usable']}/{qc['windows']}，未达门槛，结果解释需谨慎。",
                })

        # 2) 睁眼 / 闭眼基线
        baselines = {}
        for key, state, seconds in (
            ("baseline_open", "rest", config.BASELINE_PROTOCOL["eyes_open_sec"]),
            ("baseline_closed", "eyes_closed", config.BASELINE_PROTOCOL["eyes_closed_sec"]),
        ):
            with self._phase(key) as context:
                baseline, steps = self._collect_baseline(state, seconds, key)
                baselines[key] = baseline
                payload = baseline.as_dict()
                self._merge_run_payload(context["run_id"], {
                    "baseline": payload, "windows": len(steps),
                    "valid_ratio": self.engine.summarize(steps).get("valid_ratio"),
                })
                self.bus.publish("baseline", {"key": key, "label": phase_module.PHASE_BY_KEY[key].label,
                                              "baseline": payload})
                self._persist_summary_metrics(self.engine.summarize(steps), phase=key)
        self.baselines = baselines
        baseline = baselines.get("baseline_open") or baselines.get("baseline_closed")
        self.engine.baseline = baseline

        # 3) 量表
        with self._phase("scales") as context:
            scales_payload, scale_objects = self._run_scales(auto)
            self._merge_run_payload(context["run_id"], {"scales": list(scales_payload)})
            self.bus.publish("scales", {"results": list(scales_payload.values())})

        # 4) SART
        with self._phase("sart") as context:
            sart_result = self._run_sart(auto)
            self._merge_run_payload(context["run_id"], sart_result)
            self.bus.publish("behavior", {"task": "sart", "result": sart_result})

        # 5) PVT-B
        with self._phase("pvt") as context:
            pvt_result = self._run_pvt(auto)
            self._merge_run_payload(context["run_id"], pvt_result)
            self.bus.publish("behavior", {"task": "pvt", "result": pvt_result})

        # 6) 任务态监测（heatmap 数据源）
        with self._phase("monitor") as context:
            monitor = self._run_monitor()
            self._merge_run_payload(context["run_id"], {"quality": monitor["quality"]})
            self.bus.publish("monitor", {"summary": monitor["summary"], "quality": monitor["quality"]})

        # 7) 神经反馈训练
        with self._phase("training") as context:
            training_result = self._run_training(baseline, auto)
            self._merge_run_payload(context["run_id"], {"training": training_result})

        # 8) 联合评估
        with self._phase("assessment") as context:
            assessment_result = self._run_assessment(baseline, scale_objects,
                                                     {"sart": sart_result, "pvt": pvt_result},
                                                     monitor["summary"], monitor["quality"])
            self._merge_run_payload(context["run_id"], assessment_result.as_dict())
            self.bus.publish("assessment", assessment_result.as_dict())

        # 9) 模型训练
        with self._phase("model") as context:
            model_metrics = model_training.train_and_save(
                self.runs_root / "models" / "classifier.json")
            self.artifacts["model"] = Path(model_metrics["model_path"])
            self._merge_run_payload(context["run_id"], model_metrics)
            self.bus.publish("model", model_metrics)

        # 10) 报告与产物
        with self._phase("report") as context:
            paths = self._write_report(baseline, scale_objects, sart_result, pvt_result,
                                       monitor, training_result, assessment_result)
            self._merge_run_payload(context["run_id"], {k: str(v) for k, v in paths.items()})
            self.bus.publish("artifacts", {k: str(v) for k, v in self.artifacts.items()})

        self.summary = {
            "participant": self.subject["public_id"],
            "quality": monitor["quality"],
            "indicators": monitor["summary"],
            "assessment": assessment_result.as_dict(),
            "training": training_result,
            "model": model_metrics,
            "artifacts": {k: str(v) for k, v in self.artifacts.items()},
        }
        self._finish("done", None, summary=self.summary)

    # -------------------------------------------------------------- 各阶段实现
    # -------------------------------------------------------------- 各阶段实现
    @property
    def runs_root(self) -> Path:
        """本次会话的产物目录：<data>/runs/<uuid>/。"""
        return Path(self.settings.runs_root) / self.uuid

    def _collect_baseline(self, state: str, seconds: float, key: str):
        """按时间倍率逐窗采集静息基线；返回 (Baseline, steps)。"""
        total = max(1, int(round(float(seconds) / config.STEP_SEC)))
        steps = []
        for index in range(total):
            self._check_cancel()
            step = self.engine.step(state)
            steps.append(step)
            self._publish_window(step, phase=key, include_signal=(index % 5 == 0))
            if index % 5 == 0:
                self._progress(key, (index + 1) / total, windows_done=index + 1, windows_total=total)
            self._sleep(config.STEP_SEC)
        windows = [step.window for step in steps if step.window is not None]
        from ningsi.signal.baseline import build_baseline as engine_baseline
        baseline = engine_baseline(windows, device=self.source.device, srate=self.source.srate,
                                   min_windows=config.BASELINE_PROTOCOL["min_windows"])
        self.engine.baseline = baseline
        return baseline, steps

    def _run_scales(self, auto: bool):
        results = {}
        scale_objects = {}
        for code in ("SAS", "SDS"):
            definition = scales_domain.define(code)
            self.bus.publish("scale_request", {"code": code, "label": definition["name"],
                                               "size": definition["size"],
                                               "instruction": "请按最近一周的实际感受作答；量表结果只作提示。"})
            if code in self.scale_answers:
                responses = self.scale_answers[code]
            elif auto:
                responses = self._auto_scale_answers(code)
                self.scale_answers[code] = responses
            else:
                payload = self.wait_for_input(f"scales:{code}")
                responses = payload.get("responses") or self.scale_answers.get(code)
                if not responses:
                    raise RuntimeError(f"等待 {code} 作答超时或未提交")
                self.scale_answers[code] = scales_domain.normalize_responses(responses)
            try:
                scored = scales_domain.score(code, responses)
            except ValueError as exc:
                raise RuntimeError(f"{code} 作答不合法：{exc}") from exc
            scale_objects[code] = scales_domain.score_objects({code: responses})[code]
            results[code] = scored
            self.scale_results[code] = scored
            self._persist_scale(code, scored)
            paired_ledger.append_scale_record(self.runs_root, self.subject["public_id"], "01",
                                             "001", scale_objects[code], scored["responses"])
            self.bus.publish("scale_scored", {"code": code, "raw_score": scored["raw_score"],
                                              "standard_score": scored["standard_score"],
                                              "level": scored["level"]})
            self._sleep(1.0)
        return results, scale_objects

    @staticmethod
    def _auto_scale_answers(code: str) -> list[int]:
        """快速模式的确定性作答（seed 取自量表码，可复现）。"""
        rng = random.Random(f"auto-scale-{code}")
        return [rng.choice((1, 2, 2, 3)) for _ in range(20)]

    def _wait_trial(self, name: str, payload: dict, *, auto: bool, fallback: dict,
                    window: float) -> dict:
        """交互试次：随机（真实）模式等待前端作答，快速模式用确定性模拟作答。

        `window` 是该试次的**作答窗口**（秒），会随 `trial` 事件一起下发，前端据此在窗口结束时
        自动补一笔"未作答"（SART 的 No-Go 试次本来就不该按键，否则服务端会一直等到 900 秒）。

        服务端只等 `window + TRIAL_GRACE_SEC`，并且：
        - 拿到的作答**必须带匹配的 index**：前端定时器与下一次 `trial` 事件存在竞态，
          晚到的作答如果对不上当前试次，就按"未作答"记账，绝不能算到下一个试次头上（错记会污染指标）；
        - 单次超时按"未作答"记账继续跑（丢一帧不该让整段评估失败）；
        - 连续 `BEHAVIOR_MISS_LIMIT` 次超时才判定前端已掉线并抛错，
          这样页面关掉时是"约 10 秒后明确失败"，而不是把会话空转 15 分钟。
        """
        expected_index = payload.get("index")
        payload = {**payload, "response_window": round(float(window), 3)}
        self.bus.publish("trial", payload)
        if auto:
            self._behavior_misses = 0
            return dict(fallback)
        started = time.monotonic()
        answer = self.wait_for_input(name, timeout=float(window) + TRIAL_GRACE_SEC)
        if answer and (expected_index is None or answer.get("index") in (None, expected_index)):
            self._behavior_misses = 0
            answer["__elapsed"] = time.monotonic() - started
            return answer
        self._behavior_misses = getattr(self, "_behavior_misses", 0) + 1
        if self._behavior_misses >= BEHAVIOR_MISS_LIMIT:
            raise RuntimeError(
                f"{payload.get('task')} 连续 {self._behavior_misses} 个试次没有收到有效作答"
                f"（前端可能已关闭或掉线；最近一次 index={answer.get('index')!r}，期望 {expected_index!r}）")
        return {"responded": False, "rt": None, "__elapsed": time.monotonic() - started}

    def _run_sart(self, auto: bool):
        task = behavior_domain.SartTask(self.subject["public_id"], "01", "001")
        self.sart_task = task
        self.bus.publish("behavior_request", {"task": "sart", **task.sequence_payload()})
        rng = random.Random(f"auto-sart-{self.uuid}")
        total = task.sequence.trials
        while True:
            self._check_cancel()
            nxt = task.next_trial()
            if nxt.get("phase") == "done":
                break
            phase = "practice" if nxt.get("phase") == "practice" else "main"
            # 下发给前端的是**折算过时间倍率的真实秒数**：前端定时器走的是真实时间，
            # 而演示模式（time_scale<0.2）希望整体节奏跟着快起来，两边必须同一口径。
            window = max(MIN_TRIAL_WINDOW, SART_TRIAL_WINDOW[phase] * self.scale)
            responded, rt = self._auto_sart_response(nxt["digit"], rng,
                                                     practice=(nxt["phase"] == "practice"))
            answer = self._wait_trial("sart", {"task": "sart", **nxt}, auto=auto,
                                      fallback={"responded": responded, "rt": rt}, window=window)
            task.submit(phase=nxt["phase"], index=nxt["index"],
                        responded=schemas_bool(answer.get("responded")), rt=answer.get("rt"))
            if nxt["phase"] == "main" and (nxt["index"] + 1) % 45 == 0:
                self._progress("sart", (nxt["index"] + 1) / total,
                               trials_done=nxt["index"] + 1, trials_total=total)
            # 固定 SOA：下一个刺激在"本试次发出后 window 秒"出现，与是否按键无关。
            # 这样 Go 与 No-Go 的刺激间隔一致（反应时/变异系数才可比），
            # 也不会出现"No-Go 试次把节拍拖长一倍"的问题。window 已折算，故用 _sleep_real。
            if not auto:
                self._sleep_real(max(0.0, window - float(answer.get("__elapsed") or 0.0)))
        result = task.result()
        self._persist_behavior("sart", result, task.trials_payload())
        return result

    @staticmethod
    def _auto_sart_response(digit: int, rng: random.Random, *, practice: bool):
        """快速模式：Go 试次高命中、No-Go 偶发虚报，反应时带抖动。"""
        is_nogo = int(digit) == 3
        if practice:
            if is_nogo:
                return (rng.random() < 0.08, None if rng.random() > 0.08 else round(rng.uniform(0.18, 0.45), 3))
            return (rng.random() < 0.97, round(rng.uniform(0.24, 0.52), 3))
        if is_nogo:
            responded = rng.random() < 0.07
            return (responded, round(rng.uniform(0.2, 0.45), 3) if responded else None)
        responded = rng.random() < 0.955
        return (responded, round(rng.uniform(0.26, 0.62), 3) if responded else None)

    def _run_pvt(self, auto: bool):
        task = behavior_domain.PvtTask(seed=abs(hash(self.uuid + "pvt")) % 100000)
        self.pvt_task = task
        payload = task.sequence_payload()
        self.bus.publish("behavior_request", {"task": "pvt", **payload})
        rng = random.Random(f"auto-pvt-{self.uuid}")
        previous = 0.0
        onsets = list(payload["onsets"])
        for position, onset in enumerate(onsets):
            self._check_cancel()
            if not auto:
                self._sleep(max(0.0, onset - previous))
            previous = onset
            nxt = task.next_trial()
            # PVT 的作答窗口 = 到下一个刺激的间隔（标准 PVT：可答到下一个刺激出现为止），
            # 留 0.3s 余量避免与下一次 trial 事件抢跑；最后一个试次用固定窗口。
            gap = (onsets[position + 1] - onset) if position + 1 < len(onsets) else PVT_TRIAL_WINDOW
            window = max(MIN_TRIAL_WINDOW, max(0.8, min(PVT_TRIAL_WINDOW, gap - 0.3)) * self.scale)
            responded = rng.random() < 0.99
            rt = round(max(0.18, rng.gauss(0.33, 0.06)), 3) if responded else None
            answer = self._wait_trial("pvt", {"task": "pvt", **nxt}, auto=auto,
                                      fallback={"responded": responded, "rt": rt}, window=window)
            task.submit(index=nxt["index"], responded=schemas_bool(answer.get("responded")),
                        rt=answer.get("rt"), false_start=schemas_bool(answer.get("false_start")))
        result = task.result()
        self._persist_behavior("pvt", result, task.trials_payload())
        return result

    def _run_monitor(self):
        plan = (("rest", 8), ("focused", 12), ("drowsy", 15))
        total = sum(count for _, count in plan)
        done = 0
        steps = []
        for state, count in plan:
            for _ in range(count):
                self._check_cancel()
                step = self.engine.step(state)
                steps.append(step)
                done += 1
                self._publish_window(step, phase="monitor", include_signal=True)
                self._progress("monitor", done / total, windows_done=done, windows_total=total)
                self._sleep(config.STEP_SEC)
        quality = self.engine.summarize(steps)
        summary = indicators_domain.WindowEngine.summarize(steps).get("indicators", {})
        # 热力图只画"有评分"的窗；不可用窗或无基线时不填分，前端会画成缺失色块
        self.heatmap_series = [
            (step.t_end, step.focus if (step.usable and step.scores) else None)
            for step in steps
        ]
        self._persist_summary_metrics(quality, phase="monitor")
        return {"quality": quality, "summary": summary, "steps": steps}

    def _run_training(self, baseline, auto: bool):
        """训练闭环。`auto` 不参与时序（等待由 self.scale 决定），保留参数以便未来区分展示策略。"""
        trainer = training_domain.StudioTraining(
            self.subject["public_id"], "01", "001", baseline,
            mode=self.session["training_mode"] or "quick",
        )
        describe = trainer.describe()
        self.engine.baseline = baseline
        self.bus.publish("training_start", describe)
        self._progress("training", 0.0, segments_total=describe["segments"])
        wall_segment = max(4.0, float(describe["segment_sec"]) * self.scale)
        for index in range(int(describe["segments"])):
            self._check_cancel()
            states = ("focused", "drowsy")
            deadline = time.time() + wall_segment
            step_index = 0
            while time.time() < deadline:
                step = self.engine.step(states[index % len(states)])
                step_index += 1
                score = step.focus
                trainer.feed(score, step.t_end, usable=step.usable)
                self.bus.publish("feedback", {
                    "segment": index + 1,
                    "target": trainer.target,
                    "score": None if score is None else round(score, 4),
                    "on_target": bool(score is not None and score >= trainer.target),
                    "usable": step.usable,
                    "t": round(step.t_end, 3),
                })
                self._sleep(config.STEP_SEC)
            segment = trainer.close_segment()
            self._persist_training_segment(segment)
            self.bus.publish("segment", segment)
            self._progress("training", (index + 1) / max(1, describe["segments"]),
                           segments_done=index + 1, segments_total=describe["segments"])
        summary = trainer.summary(self.engine, seconds_after=30.0)
        summary["describe"] = describe
        return summary

    def _run_assessment(self, baseline, scale_objects, behavior, monitor_summary, monitor_quality):
        eeg = assessment_domain.eeg_summary(baseline=baseline, quality=monitor_quality,
                                            indicator_summary=monitor_summary)
        result = assessment_domain.assess(eeg, scale_objects, behavior)
        return result

    def _conditioning_record(self):
        """真机信号的调理口径；仿真源没有这一步，返回 None。"""
        engine = getattr(self.source, "engine", None) if self.source is not None else None
        getter = getattr(engine, "conditioning", None)
        if not callable(getter):
            return None
        record = getter()
        return record or None

    def _write_report(self, baseline, scale_objects, sart_result, pvt_result, monitor,
                      training_result, assessment_result):
        stem = f"sub-{self.subject['public_id']}_ses-01_run-001"
        root = self.runs_root
        reports = root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        band_z = {}
        for step in reversed(monitor["steps"]):
            candidate = step.payload().get("band_z") or {}
            if candidate:
                band_z = candidate
                break
        report = assessment_domain.compose_report(
            participant=self.subject["public_id"], session="01", run="001",
            indicators={"summary": monitor["summary"], "band_z": band_z,
                        "quality": monitor["quality"]},
            quality=monitor["quality"],
            scales={code: dict(value) for code, value in self.scale_results.items()},
            behavior={"sart": sart_result, "pvt": pvt_result},
            assessment=assessment_result,
            extras={
                "device": self.source.device,
                "source_kind": self.source.kind,
                "source_note": self.source.note,
                "srate": self.source.srate,
                "channels": self.source.channels,
                # 真机调理口径（逐通道去直流 + 文档 8.2 四级链）留在报告里，便于复算与审计；
                # 仿真源没有这一步，取到 None 就不写。
                "signal_conditioning": self._conditioning_record(),
                "time_scale": self.scale,
                "alerts": self.engine.fired_alerts,
                "baseline_protocol": dict(config.BASELINE_PROTOCOL),
                "baseline_reference": config.BASELINE_PROTOCOL["task_reference"],
                "baseline_eyes_open": (self.baselines.get("baseline_open").as_dict()
                                       if self.baselines.get("baseline_open") is not None else None),
                "baseline_eyes_closed": (self.baselines.get("baseline_closed").as_dict()
                                         if self.baselines.get("baseline_closed") is not None else None),
                "training": training_result,
                "engine_versions": {"spectrum": config.SPECTRUM_SPEC,
                                    "indicator": config.INDICATOR_SPEC,
                                    "baseline": config.BASELINE_SPEC,
                                    "assessment": config.LABEL_SPEC},
            },
        )
        paths = assessment_domain.write_report(report, reports, stem)
        self.artifacts.update(paths)

        heatmap_path = reports / f"{stem}_heatmap.svg"
        heatmap_path.write_text(
            heatmap_svg(self.heatmap_series, title=f"状态热力图 sub-{self.subject['public_id']}"),
            encoding="utf-8")
        self.artifacts["heatmap_svg"] = heatmap_path

        # 历史台账 + 趋势
        self._append_history(monitor, assessment_result, sart_result, pvt_result)
        history_path = root / "history" / "sessions.jsonl"
        self.artifacts["history"] = history_path
        records = paired_ledger.read_history(root) or history_module.load_records(history_path)
        try:
            usable, rejected = history_module.comparable(
                records, self.source.device, self.source.srate, self.source.channels)
            points = history_module.aggregate(usable, "focus", "week")
            for point in points:
                point["rejected"] = len(rejected)
        except Exception:  # noqa: BLE001
            points = []
        trend_path = reports / "trend.svg"
        trend_path.write_text(history_module.render_svg_trend(points, title="专注度周趋势"),
                              encoding="utf-8")
        self.artifacts["trend_svg"] = trend_path

        self._record_artifacts()
        return {"report_md": paths["report_md"], "report_json": paths["report_json"],
                "heatmap_svg": heatmap_path, "trend_svg": trend_path}

    # ------------------------------------------------------------------ 持久化
    def _merge_run_payload(self, run_id: int, payload: dict) -> None:
        """把阶段中间结果并入 runs.payload（不改状态，便于中断后仍可追溯）。"""
        try:
            with store.connect(self.db_path) as conn:
                repo.update_run_payload(conn, run_id, payload)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("阶段中间结果写入失败：%s", exc)

    def _persist_scale(self, code: str, scored: dict) -> None:
        try:
            with store.connect(self.db_path) as conn:
                repo.add_scale_run(
                    conn, self.session["id"], code=code, version=scored.get("version"),
                    raw_score=scored.get("raw_score"), standard_score=scored.get("standard_score"),
                    level=scored.get("level"), answered=scored.get("answered"),
                    missing=scored.get("missing_items"), responses=scored.get("responses"),
                    items=scored.get("items"),
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("量表结果入库失败：%s", exc)

    def _persist_behavior(self, task: str, result: dict, trials: dict) -> None:
        """行为结果入库。任务名与上游口径一致（SART 用 `sart`、PVT-B 用 `pvt-b`），
        这样 `GET .../behaviors/pvt/result` 在服务重启后也能从库里读到。"""
        stored_task = "pvt-b" if task.lower() in ("pvt", "pvt-b") else task.lower()
        try:
            with store.connect(self.db_path) as conn:
                repo.add_behavior_run(conn, self.session["id"], task=stored_task,
                                      spec=result.get("spec"), metrics=result, trials=trials)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("行为结果入库失败：%s", exc)

    def _persist_summary_metrics(self, summary: dict, *, phase: str) -> None:
        indicators = (summary or {}).get("indicators") or {}
        if not indicators:
            return
        try:
            with store.connect(self.db_path) as conn:
                for name, stat in indicators.items():
                    repo.add_metric(conn, self.session["id"], "indicator_summary", name,
                                    mean=stat.get("mean"), std=stat.get("std"), n=stat.get("n"),
                                    valid_ratio=(summary or {}).get("valid_ratio"),
                                    quality={"phase": phase})
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("指标汇总入库失败：%s", exc)

    def _persist_training_segment(self, segment: dict) -> None:
        """训练分段入库。把完整 payload 存进 samples 字段，接口再原样回给前端：

        前端要画达标占比柱状与逐窗曲线，需要 stats 与 samples，
        而数据库列只保留索引用的标量字段，因此明细整体序列化保存。
        """
        try:
            with store.connect(self.db_path) as conn:
                repo.add_training_segment(
                    conn, self.session["id"], int(segment["seq"]),
                    target=segment.get("target"), mean_score=segment.get("mean_score"),
                    on_target_ratio=segment.get("on_target_ratio"),
                    hold_sec=segment.get("hold_sec"),
                    excluded_windows=segment.get("excluded_windows"),
                    samples=segment,
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("训练分段入库失败：%s", exc)

    def _append_history(self, monitor, assessment_result, sart_result, pvt_result) -> None:
        record = {
            "participant": self.subject["public_id"],
            "session": "01",
            "run": "001",
            "device": self.source.device,
            "srate": self.source.srate,
            "channels": self.source.channels,
            "indicators": {name: stat.get("mean") for name, stat in (monitor["summary"] or {}).items()},
            "quality": {"valid_ratio": monitor["quality"].get("valid_ratio"), "passed": True},
            "behavior": {"sart_rt_mean": sart_result.get("rt_mean"),
                         "pvt_rt_median": pvt_result.get("rt_median")},
            "assessment": {"conclusion": assessment_result.conclusion},
            "session_uuid": self.uuid,
        }
        paired_ledger.append_history_record(self.runs_root, record)
        try:
            with store.connect(self.db_path) as conn:
                repo.update_session(conn, self.uuid, progress=1.0)
        except Exception:  # noqa: BLE001
            pass

    def _record_artifacts(self) -> None:
        try:
            with store.connect(self.db_path) as conn:
                for kind, path in self.artifacts.items():
                    candidate = Path(path)
                    if not candidate.exists():
                        continue
                    repo.add_artifact(conn, self.session["id"], kind, str(candidate),
                                      size=candidate.stat().st_size,
                                      sha256=export_domain.sha256_of(candidate))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("产物入库失败：%s", exc)

    # ------------------------------------------------------------------ 收尾
    def _finish(self, status: str, error, summary: dict | None = None) -> None:
        # 释放常驻采集资源（LSL 线程/缓冲），否则会话结束还会占着一个采集线程
        engine = getattr(self.source, "engine", None) if self.source is not None else None
        if engine is not None and hasattr(engine, "stop"):
            try:
                engine.stop()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("释放采集资源失败：%s", exc)
        try:
            with store.connect(self.db_path) as conn:
                repo.update_session(conn, self.uuid, status=status,
                                    ended_at=store.utcnow(), error=error,
                                    progress=1.0 if status == "done" else 0.0,
                                    # 终态如实入 phase：done/failed/cancelled。曾经把非 done
                                    # 一律写成 "error"，于是取消的会话出现「状态=已取消 /
                                    # 阶段=失败」自相矛盾（前端 phaseText 把 error 译成「失败」）。
                                    phase=status)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("会话收尾写库失败：%s", exc)
        try:
            self.bus.publish("finished", {"status": status, "error": error,
                                          "summary": summary or self.summary})
        except Exception:  # noqa: BLE001
            pass


class SessionManager:
    """会话运行线程的注册表：限制并发、按 uuid 查找、取消与关闭。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._runtimes: dict[str, SessionRuntime] = {}
        self._lock = threading.Lock()

    def active_uuids(self) -> list[str]:
        with self._lock:
            return [uuid for uuid, runtime in self._runtimes.items() if runtime.alive]

    def ensure_capacity(self) -> None:
        """并发上限预检：在落库之前调用，避免 429 留下"没有运行线程的幽灵会话"。"""
        with self._lock:
            active = sum(1 for runtime in self._runtimes.values() if runtime.alive)
            limit = self.settings.max_active_sessions
        if active >= limit:
            from ningsi_studio.http.router import TooManyRequests
            raise TooManyRequests(f"同时运行的会话已达上限（{limit} 个），请稍后再试")

    def start(self, session_uuid: str) -> SessionRuntime:
        self.ensure_capacity()
        with self._lock:
            bus = EventBus(session_uuid)
            runtime = SessionRuntime(session_uuid, settings=self.settings, bus=bus)
            self._runtimes[session_uuid] = runtime
        runtime.start()
        return runtime

    def start_with_rollback(self, session_uuid: str, on_failure) -> SessionRuntime:
        """启动会话；启动失败时回调 `on_failure(reason)` 收尾（例如把库里那行标记为 failed）。"""
        try:
            return self.start(session_uuid)
        except Exception as exc:
            try:
                on_failure(str(exc))
            except Exception:  # noqa: BLE001
                LOGGER.warning("会话启动失败后的回滚也失败了：%s", exc)
            raise

    def get(self, session_uuid: str) -> SessionRuntime | None:
        with self._lock:
            return self._runtimes.get(session_uuid)

    def bus(self, session_uuid: str) -> EventBus | None:
        runtime = self.get(session_uuid)
        return runtime.bus if runtime else None

    def cancel(self, session_uuid: str) -> bool:
        runtime = self.get(session_uuid)
        if runtime is None or not runtime.alive:
            return False
        runtime.cancel()
        return True

    def discard(self, session_uuid: str) -> None:
        with self._lock:
            self._runtimes.pop(session_uuid, None)

    def shutdown(self, timeout: float = 5.0) -> None:
        """取消所有会话并等待运行线程退出（等待有上限，避免进程无法结束）。"""
        with self._lock:
            runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            runtime.cancel()
        deadline = time.time() + max(1.0, timeout)
        for runtime in runtimes:
            remaining = max(0.1, deadline - time.time())
            runtime.join(timeout=remaining)
