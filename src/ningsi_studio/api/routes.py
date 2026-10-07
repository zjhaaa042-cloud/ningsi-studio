"""全部 /api 端点：把数据层、领域层与运行时暴露成 REST 接口。

约定：
- 路径一律以 `/api` 开头（静态前端与之同端口共存）；
- 错误统一走 `ApiError`（400/401/404/409/422/429/500），响应体为 `{"error": {...}}`；
- 运行中会话的实时数据走 SSE（`/api/sessions/{uuid}/events`），其余端点幂等可轮询。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ningsi import config as engine_config
from ningsi.behavior import pvt as pvt_module
from ningsi.behavior import sart as sart_module

from ningsi_studio import bootstrap
from ningsi_studio.api import schemas
from ningsi_studio.core import live_source, paired_ledger, phases as phase_module
from ningsi_studio.core import signal_feed
# 注意：**不要**写 `from ningsi_studio.core import runtime`——`core/__init__.py` 用
# `__getattr__` 惰性导入 runtime，在包初始化过程中再走一次那条路径会无限递归
# （实测 RecursionError，服务起不来）。直接导入子模块属性没有这个问题。
from ningsi_studio.core.runtime import SART_TRIAL_WINDOW, SessionManager
from ningsi_studio.db import repository as repo
from ningsi_studio.db import sqlite_store as store
from ningsi_studio.domain import export as export_domain
from ningsi_studio.domain import model_training, scales as scales_domain
from ningsi_studio.http import sse
from ningsi_studio.http.router import (ApiError, Conflict, NotFound, Request, Response, Router,
                                       ValidationError, dump, jsonable)
from ningsi_studio.http.server import sse_response
from ningsi_studio.settings import Settings

API_TITLE = "凝思 Studio API"
API_VERSION = "0.1.0"

LOGGER = logging.getLogger("ningsi_studio.api")

# 实时信号通道的刷新参数：参考工程 bsense 的监控界面是 5 FPS，这里默认 10 FPS（更顺滑），
# 上限 25 FPS 以免把浏览器与服务端线程压满。
SIGNAL_DEFAULT_HZ = 10.0
SIGNAL_MAX_HZ = 25.0


# --------------------------------------------------------------------- 工具

def _session_row(conn, uuid: str):
    row = repo.get_session(conn, uuid)
    if row is None:
        raise NotFound(f"会话不存在：{uuid}")
    return row


def _subject_row(conn, public_id: str):
    row = repo.find_subject(conn, schemas.normalize_public_id(public_id))
    if row is None:
        raise NotFound(f"被试不存在：{public_id}")
    return row


def _source_kind_from_key(source) -> str | None:
    """从落库的 source 键反推来源种类，供运行器已回收的历史会话使用。

    会话结束后运行器可能已被清理，此时 `runtime.source_kind` 为空，
    前端顶栏就会从「数据来源：仿真」退化成「数据来源：sim-bsense」，
    而"仿真数据必须显式标注"是产品承诺，不能因为会话结束了就丢。
    只按命名约定判定（与 core/live_source.py 的 kind 保持一致），不做猜测：
    `lsl:` 前缀 ⇒ 实时设备；仿真源键（`sim-bsense`）⇒ 仿真。
    """
    key = str(source or "")
    if key.startswith("lsl:"):
        return "lsl"
    if key == live_source.SIM_SOURCE or key.startswith("sim"):
        return "sim"
    return None


def _decode_payload(value):
    """把库里存的 JSON 文本还原成对象。

    `runs.payload` 在 SQLite 里是 TEXT，直接回给前端会变成字符串，
    于是"从 runs 里取质检结果"这类读法永远拿不到值（界面上表现为关键指标空白）。
    空串/非法 JSON 原样返回，保持"有原始文本可追溯"。
    """
    if not isinstance(value, str) or not value.strip():
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def session_public(row, *, extra: dict | None = None) -> dict:
    payload = {
        "uuid": row["uuid"],
        "participant": row["public_id"] if "public_id" in row.keys() else None,
        "subject_id": row["subject_id"],
        "label": row["label"],
        "device": row["device"],
        "srate": row["srate"],
        "channels": row["channels"],
        "source": row["source"],
        "time_scale": row["time_scale"],
        "training_mode": row["training_mode"],
        # 协议档（full / short）：老库的行在迁移里补成 'full'；未知值按完整协议展示
        "protocol": phase_module.normalize_profile(
            row["protocol"] if "protocol" in row.keys() else None),
        "protocol_label": phase_module.PROFILES[
            phase_module.normalize_profile(row["protocol"] if "protocol" in row.keys() else None)]["label"],
        "status": row["status"],
        "phase": row["phase"],
        "phase_label": phase_module.PHASE_BY_KEY.get(row["phase"]).label
        if row["phase"] in phase_module.PHASE_BY_KEY else row["phase"],
        "progress": row["progress"],
        "error": row["error"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "created_at": row["created_at"],
        "engine_versions": json.loads(row["engine_versions"] or "{}"),
    }
    if extra:
        payload.update(extra)
    return jsonable(payload)


def _subject_public(row, counts: dict | None = None) -> dict:
    return jsonable({
        "public_id": row["public_id"],
        "label": row["label"],
        "age_band": row["age_band"],
        "sex": row["sex"],
        "handedness": row["handedness"],
        "consent_version": row["consent_version"],
        "consent_at": row["consent_at"],
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "sessions": counts or {},
    })


def _collection_page(items: list, *, limit: int, offset: int) -> dict:
    """列表类响应的统一信封：`{items,total,limit,page}`（见 docs/API.md「分页约定」）。

    `limit/offset` 由 `schemas.pagination(request)` 解析（默认 limit=50、page 从 1 起、
    limit 上限 200）。这里做**真实切片**而不是只回填 `limit=len(items)/page=1`：
    后者会让 `?page=2` 静默返回第 1 页的数据，比缺字段更糟。
    """
    total = len(items)
    page = offset // limit + 1
    return jsonable({
        "items": items[offset:offset + limit],
        "total": total,
        "limit": limit,
        "page": page,
    })


# --------------------------------------------------------------------- 路由

class StudioSessionManager(SessionManager):
    """会话管理器：进程收尾时顺带清掉实时信号注册表。

    `signal_feed.REGISTRY` 是模块级单例，按会话持有 `SignalFeed`。只靠订阅结束时
    `unsubscribe()` 只会停线程，注册表条目仍会随"开过实时监测的会话数"一直增长。
    应用停止路径（`app.Application.stop()` 与 `python -m ningsi_studio demo` 的收尾）
    都走 `SessionManager.shutdown()`，因此在这里补一次 `stop_all()`。
    """

    def shutdown(self, timeout: float = 5.0) -> None:
        try:
            super().shutdown(timeout)
        finally:
            signal_feed.REGISTRY.stop_all()


def build_router(settings: Settings) -> Router:
    router = Router()
    manager = StudioSessionManager(settings)
    db_path = Path(settings.db_path)

    def sweep_signal_feeds() -> int:
        """回收已结束会话遗留的实时信号 feed（幂等，可在任意读接口触发）。

        判据是运行器的**实际存活状态**：`manager.get(uuid)` 存在且 `alive` 才算活着，
        不看库里的 status（库值可能滞后，按它判会误清正在推流的 feed）。
        """
        def is_live(session_uuid: str) -> bool:
            runtime = manager.get(session_uuid)
            return bool(runtime and runtime.alive)

        return signal_feed.REGISTRY.sweep(is_live)

    def rollback_session(uuid: str, reason: str) -> None:
        """会话未能真正启动（并发超限等）时把库里的行收成 failed，不留幽灵会话。

        phase 与 status 保持同一个词（都用 failed）：终态阶段名直接给前端本地化，
        不再写历史遗留的 "error"（那是早期把 failed/cancelled 混在一起时的写法）。
        """
        try:
            with store.connect(db_path) as conn:
                repo.update_session(conn, uuid, status="failed", phase="failed", error=reason,
                                    ended_at=store.utcnow())
        except Exception:  # noqa: BLE001 - 回滚失败只记日志，不改变对外错误语义
            pass

    # ---------------------------------------------------------------- 基础
    @router.get(r"/api/health")
    def health(request: Request) -> Response:
        with store.read_only(db_path) as conn:
            overview = repo.overview(conn)
        # 数据来源三件套 = **当前检测到的实时源**；检测不到就是 None（顶栏写"数据来源：无信号"）。
        # 绝不能回落成"新会话将使用仿真源"——没有脑机信号就不使用仿真，这是产品规则。
        # LSL 扫描结果有 3 秒缓存（见 live_source.cached_sources），health 不会被拖慢。
        overview.update(live_source.detected_source_fields())
        return Response.json({
            "status": "ok",
            "service": "ningsi-studio",
            "version": API_VERSION,
            "product": engine_config.VERSION,
            "engine": bootstrap.engine_versions(),
            "engine_path": bootstrap.ENGINE_PATH,
            "db": str(db_path),
            "data_dir": str(settings.data_dir),
            "runs_root": str(settings.runs_root),
            "active_sessions": manager.active_uuids(),
            "max_active_sessions": settings.max_active_sessions,
            "overview": overview,
        })

    @router.get(r"/api/config")
    def config_endpoint(request: Request) -> Response:
        return Response.json({
            "version": engine_config.VERSION,
            "specs": bootstrap.engine_versions(),
            "window_sec": engine_config.WINDOW_SEC,
            "step_sec": engine_config.STEP_SEC,
            "welch": engine_config.WELCH,
            "bands": {key: list(value) for key, value in engine_config.BANDS.items()},
            "total_band": list(engine_config.TOTAL_BAND),
            "indicators": {key: list(value) for key, value in engine_config.INDICATORS.items()},
            "score_gain": engine_config.SCORE_GAIN,
            "baseline_protocol": engine_config.BASELINE_PROTOCOL,
            "quality": engine_config.QUALITY,
            "alerts": engine_config.ALERTS,
            "assess": engine_config.ASSESS,
            "training": engine_config.TRAINING,
            "heatmap_bands": [{"low": low, "high": high, "label": label, "color": color}
                              for low, high, label, color in engine_config.HEATMAP_BANDS],
            "scale_boundary": engine_config.SCALE_BOUNDARY,
            "phases": phase_module.as_list(),
            # 协议档：向导据此展示"完整 / 短协议"与各自负担（阶段数、时长、行为任务试次数）
            "profiles": phase_module.profiles_as_list(),
            # 行为任务的协议参数：界面（向导的"本次检测要你做多少次"）不该自己写死试次数量，
            # 口径统一从引擎常量取，改协议时两边不会漂移。
            "behavior": {
                "sart": {"practice_trials": sart_module.PRACTICE_TRIALS, "trials": sart_module.TRIALS,
                         "nogo_trials": sart_module.NOGO_TRIALS, "nogo_digit": sart_module.NOGO_DIGIT,
                         "window": dict(SART_TRIAL_WINDOW)},
                "pvt": {"duration_sec": pvt_module.DURATION_SEC, "isi_sec": list(pvt_module.ISI_RANGE),
                        "lapse_sec": pvt_module.LAPSE_SEC},
                "scales": {"codes": ["SAS", "SDS"], "items_per_scale": 20},
            },
            "privacy": {
                "subject_id": "被试编号匿名且跨会话一致，姓名等直接身份信息不进入文件名、报告与数据库。",
                "boundary": "结论仅用于研究与自我调节训练，不构成医疗诊断，不得用于处罚或自动上岗决策。",
            },
        })

    @router.get(r"/api/devices")
    def devices(request: Request) -> Response:
        probe = request.query_float("probe", 1.0)
        probe = max(0.2, min(5.0, probe))
        sources = live_source.list_available(probe_seconds=probe)
        # has_real 让界面能一眼判断"现在到底有没有脑机信号"，不用自己筛 real 字段
        return Response.json({
            "sources": sources,
            **live_source.detected_source_fields(sources),
            "has_real_source": any(row.get("real") and not row.get("simulated") for row in sources),
        })

    # ------------------------------------------------- 无会话的设备实时预览
    @router.get(r"/api/devices/preview")
    def preview_status(request: Request) -> Response:
        """预览状态；`?window=1` 时顺带返回一窗真实波形（前端按 1~2 秒轮询）。"""
        preview = live_source.preview_stream()
        if request.query_int("window", 0, low=0, high=1) == 1:
            return Response.json(preview.window())
        return Response.json(preview.status())

    @router.post(r"/api/devices/preview")
    def preview_start(request: Request) -> Response:
        """启动预览：不传 source 时自动用当前检测到的实时源；没有信号就 409（不回落仿真）。"""
        payload = schemas.typed(request.json())
        device = schemas.optional_text(payload.get("source") or payload.get("device"),
                                      max_len=120, name="数据源")
        device = (device or "").strip() or None
        if device and device != live_source.SIM_SOURCE and not device.startswith("lsl:"):
            raise ValidationError(
                f"数据源只能是 lsl:<流名称> 或 {live_source.SIM_SOURCE}，收到 {device!r}", detail=device)
        try:
            status = live_source.preview_stream().start(device)
        except RuntimeError as exc:
            raise Conflict(str(exc)) from exc
        return Response.json(status)

    @router.delete(r"/api/devices/preview")
    def preview_stop(request: Request) -> Response:
        live_source.preview_stream().stop()
        return Response.json({"active": False, "stopped": True})

    @router.get(r"/api/devices/status")
    def device_status(request: Request) -> Response:
        """正在跑的会话用到的真实设备健康状况：是否在收数、实测采样率、错误。

        仿真源没有硬件，返回 kind=sim 并说明；有 LSL 会话时给出活体指标，
        方便现场一眼看出"设备掉了"还是"信号正常"。
        """
        active = manager.active_uuids()
        payload = {"active_sessions": active, "devices": [], "sources": [],
                   # 调理口径统一放在这里显示一次（原来逐条数据源重复，界面里同一句话出现 6 遍）
                   "condition_note": live_source.CONDITION_NOTE}
        try:
            payload["sources"] = live_source.list_available(probe_seconds=0.3)
        except Exception as exc:  # noqa: BLE001 - 诊断接口不应失败
            payload["sources_error"] = str(exc)
        for uuid in active:
            runtime = manager.get(uuid)
            source = getattr(runtime, "source", None) if runtime else None
            if source is None:
                continue
            item = {
                "session": uuid,
                "key": getattr(source, "key", None),
                "kind": getattr(source, "kind", None),
                "device": getattr(source, "device", None),
                "srate": getattr(source, "srate", None),
                "channels": getattr(source, "channels", None),
                "note": getattr(source, "note", None),
            }
            engine = getattr(source, "engine", None)
            if engine is not None and hasattr(engine, "status"):
                try:
                    status = engine.status()
                except Exception as exc:  # noqa: BLE001
                    status = {"error": str(exc)}
                streams = status.get("streams", {}) or {}
                eeg = streams.get("eeg", {}) if isinstance(streams, dict) else {}
                item.update({
                    "live": eeg.get("live"),
                    "seconds_since_last": eeg.get("seconds_since_last"),
                    "observed_srate": eeg.get("observed_srate"),
                    "buffered_samples": eeg.get("buffered_samples"),
                    "total_samples": eeg.get("total_samples"),
                    "stream_errors": status.get("errors", {}),
                    "descriptor": (status.get("descriptors", {}) or {}).get("eeg"),
                })
            else:
                item["live"] = source.kind == "sim"
            payload["devices"].append(jsonable(item))
        return Response.json(payload)

    @router.get(r"/api/openapi.json")
    def openapi(request: Request) -> Response:
        return Response.json(_openapi_spec())

    # ---------------------------------------------------------------- 被试
    @router.get(r"/api/subjects")
    def list_subjects(request: Request) -> Response:
        limit, offset = schemas.pagination(request)
        query = (request.query.get("query") or "").strip()
        with store.read_only(db_path) as conn:
            rows, total = repo.list_subjects(conn, query=query, limit=limit, offset=offset)
            items = [_subject_public(row, repo.subject_session_counts(conn, row["id"])) for row in rows]
        return Response.json({"items": items, "total": total, "limit": limit,
                              "page": offset // limit + 1})

    @router.post(r"/api/subjects")
    def create_subject(request: Request) -> Response:
        payload = schemas.typed(request.json())
        with store.connect(db_path) as conn:
            public_id = payload.get("public_id")
            if payload.get("auto_id", True) and not public_id:
                public_id = repo.next_public_id(conn)
            public_id = schemas.normalize_public_id(public_id or "")
            if repo.find_subject(conn, public_id):
                raise Conflict(f"被试编号已存在：sub-{public_id}", detail={"public_id": public_id})
            subject_id = repo.create_subject(
                conn, public_id,
                label=schemas.optional_text(payload.get("label"), name="别名"),
                age_band=schemas.optional_text(payload.get("age_band"), max_len=20, name="年龄段"),
                sex=schemas.optional_text(payload.get("sex"), max_len=10, name="性别"),
                handedness=schemas.optional_text(payload.get("handedness"), max_len=10, name="利手"),
                consent_version=schemas.optional_text(payload.get("consent_version"), max_len=40,
                                                     name="同意版本") or "consent-v1",
                consent_at=schemas.optional_text(payload.get("consent_at"), max_len=40, name="同意时间"),
                note=schemas.optional_text(payload.get("note"), max_len=500, name="备注"),
            )
            row = repo.get_subject_by_pk(conn, subject_id)
        return Response.json(_subject_public(row, {"total": 0}), status=201)

    @router.get(r"/api/subjects/{public_id}")
    def get_subject(request: Request) -> Response:
        with store.read_only(db_path) as conn:
            row = _subject_row(conn, request.params["public_id"])
            counts = repo.subject_session_counts(conn, row["id"])
        return Response.json(_subject_public(row, counts))

    @router.patch(r"/api/subjects/{public_id}")
    def patch_subject(request: Request) -> Response:
        """局部更新：只写请求里出现过的字段，未提供的字段保持原值。"""
        payload = schemas.typed(request.json())
        spec = {
            "label": {"max_len": 200, "name": "别名"},
            "age_band": {"max_len": 20, "name": "年龄段"},
            "sex": {"max_len": 10, "name": "性别"},
            "handedness": {"max_len": 10, "name": "利手"},
            "note": {"max_len": 500, "name": "备注"},
            "consent_version": {"max_len": 40, "name": "同意版本"},
            "consent_at": {"max_len": 40, "name": "同意时间"},
        }
        updates = {
            key: schemas.optional_text(payload[key], max_len=rule["max_len"], name=rule["name"])
            for key, rule in spec.items()
            if key in payload
        }
        if not updates:
            raise ValidationError("没有可更新的字段", detail={"allowed": sorted(spec)})
        with store.connect(db_path) as conn:
            row = _subject_row(conn, request.params["public_id"])
            repo.update_subject(conn, row["public_id"], **updates)
            updated = repo.find_subject(conn, row["public_id"])
            counts = repo.subject_session_counts(conn, row["id"])
        return Response.json(_subject_public(updated, counts))

    @router.get(r"/api/subjects/{public_id}/sessions")
    def subject_sessions(request: Request) -> Response:
        limit, offset = schemas.pagination(request)
        with store.read_only(db_path) as conn:
            row = _subject_row(conn, request.params["public_id"])
            rows, total = repo.list_sessions(conn, public_id=row["public_id"], limit=limit, offset=offset)
            items = [session_public(item, extra={"alert_count": len(repo.list_alerts(conn, item["id"]))})
                     for item in rows]
        return Response.json({"items": items, "total": total, "participant": row["public_id"],
                              "limit": limit, "page": offset // limit + 1})

    # ---------------------------------------------------------------- 会话
    @router.post(r"/api/sessions")
    def create_session(request: Request) -> Response:
        payload = schemas.typed(request.json())
        schemas.require_fields(payload, "participant")
        # 先做并发上限预检：避免 429 之后在库里留下"没有运行线程的幽灵会话"
        manager.ensure_capacity()
        public_id = schemas.normalize_public_id(payload["participant"])
        # 数据源解析（2026-10-07 起收紧）：**不再有"缺省仿真"**。
        #   · 不传 device / 传 auto ⇒ 用当前检测到的实时源；一个都没有就 409，让用户去接设备；
        #   · 传 lsl:<流名> ⇒ 先确认这条流现在真的可见，避免"建好会话才发现没信号"；
        #   · 传 sim-bsense ⇒ 允许，但这是**显式**选择（演示/自测），界面与报告都会标注仿真。
        device_raw = schemas.optional_text(payload.get("device"), max_len=120, name="设备")
        device = (device_raw or "").strip()
        if not device or device.lower() in ("auto", "default", "any"):
            sources = live_source.list_available(1.0)
            device = live_source.default_device(sources) or ""
            if not device:
                raise Conflict(
                    "未检测到脑电信号（LSL）：请先启动采集端（如 BioMultiLite / BSense-R）"
                    "并确认在推流；仅做演示或自测时，请在“设备 / 数据源”里显式选择仿真源 sim-bsense")
        elif device.startswith("lsl:"):
            stream_name = device.split(":", 1)[1]
            rows = live_source.list_available(1.0)
            keys = {row.get("key") for row in rows}
            if device not in keys:
                visible = sorted(key for key in keys if str(key).startswith("lsl:"))
                raise Conflict(
                    f"未发现 LSL 流 {stream_name}（当前可见：{visible or '无'}）；"
                    f"请确认采集端正在推流（GET /api/devices 可列出可见流）")
            # 必须是脑电流：一台设备同时推 EEG / FNIRS / Metric / HeartRate / Motion …，
            # 选错流照样能算出"看着像"的数字，但报告毫无意义（用户 2026-10-07 就卡在这里问
            # "这六个实时数据应该怎么选"）。已知非脑电的类型直接 409，并说清为什么。
            row = next((item for item in rows if item.get("key") == device), {})
            bad_kind = live_source.rejected_kind(row)
            if bad_kind and not schemas.as_bool(payload.get("allow_non_eeg"), False):
                raise Conflict(
                    f"{stream_name} 不是脑电流（类型 {live_source.stream_kind_label(bad_kind)}）："
                    f"本产品的质检、频谱、专注/放松/负荷指标与 SART/PVT 判定都基于脑电通道，"
                    f"用这条流跑出来的报告没有意义。请改选 EEG 流"
                    f"（通常形如 lsl:<设备名> EEG-…）；确实要用它采集时显式传 allow_non_eeg=true。",
                    detail={"device": device, "stream_kind": bad_kind,
                            "eeg_candidates": sorted(key for key in keys
                                                     if str(key).startswith("lsl:")
                                                     and live_source.is_eeg_stream(
                                                         next((r for r in rows if r.get("key") == key), {})))})
        elif device != live_source.SIM_SOURCE:
            raise ValidationError(
                f"设备 / 数据源只能是 lsl:<流名称>（实时设备）或 {live_source.SIM_SOURCE}"
                f"（显式选择仿真，仅演示/自测用），收到 {device!r}",
                detail=device)
        time_scale = schemas.clamp_float(payload.get("time_scale", payload.get("speed")), 1.0,
                                         low=0.01, high=1.0, name="时间倍率")
        srate = schemas.clamp_float(payload.get("srate"), 250.0, low=1.0, high=2000.0, name="采样率")
        channels = schemas.clamp_int(payload.get("channels"), 1, low=1, high=8, name="通道数")
        training_mode = payload.get("training_mode") or "quick"
        if training_mode not in engine_config.TRAINING:
            raise ValidationError(f"训练模式必须是 {sorted(engine_config.TRAINING)} 之一",
                                  detail=training_mode)
        # 协议档：full（11 步，默认）/ short（9 步：去掉训练与模型，SART/PVT 减半）。
        # 未知值一律回落完整协议——**绝不把拼错的档名当成短协议**。
        protocol_raw = schemas.optional_text(payload.get("protocol"), max_len=16, name="协议档")
        protocol = phase_module.normalize_profile(protocol_raw)
        if protocol_raw and protocol_raw.strip().lower() not in phase_module.PROFILES:
            raise ValidationError(
                f"协议档只能是 {sorted(phase_module.PROFILES)} 之一（收到 {protocol_raw!r}）",
                detail=protocol_raw)

        with store.connect(db_path) as conn:
            subject = repo.find_subject(conn, public_id)
            if subject is None:
                if not schemas.as_bool(payload.get("create_subject"), True):
                    raise NotFound(f"被试不存在：sub-{public_id}")
                subject_id = repo.create_subject(conn, public_id,
                                                 label=schemas.optional_text(payload.get("label"),
                                                                             name="别名"))
            else:
                subject_id = subject["id"]
            row = repo.create_session(
                conn, subject_id,
                label=schemas.optional_text(payload.get("label"), name="会话标签"),
                device=device, srate=srate, channels=channels,
                source="lsl" if device.startswith("lsl:") else "sim-bsense",
                time_scale=time_scale, training_mode=training_mode,
                protocol=protocol,
                engine_versions=bootstrap.engine_versions(),
            )
        # 注意：这里必须已经退出 `with store.connect(...)`（事务提交、连接释放），
        # 再启动运行线程；否则运行线程装载会话时会撞上 "database is locked"。
        try:
            runtime = manager.start_with_rollback(row["uuid"], lambda reason: rollback_session(row["uuid"], reason))
        except Exception as exc:  # noqa: BLE001
            # 数据源打不开（例如"流可见但 8 秒内没有样本"）→ 409 + 明确原因，而不是 500。
            # 会话已在 rollback 回调里被标记为 failed，这里只负责把 HTTP 语义说清楚。
            raise Conflict(f"会话未能启动：{exc}") from exc
        # 运行期会按"真实可用性"选数据源：`lsl:` 流没数据时运行时会退回仿真源，而建会话时
        # 只按设备名推断 source。若不回写，同一响应里就会出现 source="lsl" 而
        # runtime.source_kind="sim" 的自相矛盾，等于把"当前是仿真"这件事藏了起来
        # （live_source.py 顶部与 docs/API.md 都要求如实标注、不做静默替换）。
        actual_source = getattr(getattr(runtime, "source", None), "key", None)
        if actual_source and actual_source != row["source"]:
            with store.connect(db_path) as conn:
                repo.update_session(conn, row["uuid"], source=actual_source)
                row = repo.get_session(conn, row["uuid"])
        # 采样率与通道数同样要以**真实流描述符**为准：建会话时调用方只知道设备名，
        # `channels` 默认 1、`srate` 默认 250；而真实流声明的可能是 2 通道/500 Hz。
        # 不回写就会出现"库里 1 通道、界面按 2 通道画、报告写 1 通道"的三套口径
        # （真机实测 BioMulti Lite 声明 2 通道，见 _analysis/lsl-hardware/units.json）。
        runtime_source = getattr(runtime, "source", None)
        if runtime_source is not None and getattr(runtime_source, "kind", None) == "lsl":
            actual_srate = float(getattr(runtime_source, "srate", 0.0) or 0.0)
            actual_channels = int(getattr(runtime_source, "channels", 0) or 0)
            changed = (actual_channels > 0 and actual_channels != int(row["channels"] or 0)) or (
                actual_srate > 0 and abs(actual_srate - float(row["srate"] or 0.0)) > 1e-6)
            if changed:
                with store.connect(db_path) as conn:
                    repo.update_session(conn, row["uuid"], srate=actual_srate or row["srate"],
                                        channels=actual_channels or row["channels"])
                    row = repo.get_session(conn, row["uuid"])
        return Response.json({
            "session": session_public(row, extra={"participant": public_id}),
            "events_url": f"/api/sessions/{row['uuid']}/events",
            # 阶段列表按本次会话的协议档给：短协议只有 9 步，界面步进条据此渲染
            "phases": phase_module.as_list(row["protocol"] if "protocol" in row.keys() else None),
            "protocol": protocol,
            "protocol_label": phase_module.PROFILES[protocol]["label"],
            "protocol_note": phase_module.PROFILES[protocol]["caveat"],
            "runtime": {"alive": runtime.alive, "source": runtime.source.key if runtime.source else None},
        }, status=201)

    @router.get(r"/api/sessions")
    def list_sessions(request: Request) -> Response:
        limit, offset = schemas.pagination(request)
        with store.read_only(db_path) as conn:
            rows, total = repo.list_sessions(
                conn,
                public_id=(schemas.normalize_public_id(request.query["participant"])
                           if request.query.get("participant") else None),
                status=request.query.get("status") or None,
                limit=limit, offset=offset,
            )
            items = [session_public(row, extra={
                "alert_count": len(repo.list_alerts(conn, row["id"])),
                # 列表页也要中文阶段名：详情接口早就带 phase_label，列表只回英文键，
                # 于是仪表盘/历史/被试页的「阶段」列显示 done/error（见前端 phaseText）
                "phase_label": (phase_module.PHASE_BY_KEY.get(row["phase"]).label
                                if row["phase"] in phase_module.PHASE_BY_KEY else None),
            }) for row in rows]
        return Response.json({"items": items, "total": total, "limit": limit,
                              "page": offset // limit + 1})

    @router.get(r"/api/sessions/{uuid}")
    def get_session(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        with store.read_only(db_path) as conn:
            row = _session_row(conn, uuid)
            payload = session_public(row)
            payload["alerts"] = jsonable(schemas.rows_to_dicts(repo.list_alerts(conn, row["id"])))
            runs = jsonable(schemas.rows_to_dicts(repo.list_runs(conn, row["id"])))
            # runs[].payload 一定是对象：质检 / 基线 / 训练等阶段中间结果直接可读，
            # 前端才能用 run.payload.passed 这类读法取到值（见 dashboard.qualityFromRuns）
            for item in runs:
                if "payload" in item:
                    item["payload"] = _decode_payload(item["payload"])
            payload["runs"] = runs
            payload["artifacts"] = jsonable(schemas.rows_to_dicts(repo.list_artifacts(conn, row["id"])))
            payload["indicator_summary"] = jsonable(repo.latest_metric_summary(conn, row["id"]))
            # 本次会话的阶段表**按它自己的协议档**给出：短协议是 9 步。
            # 不返回它的话，直接打开 `#/flow?session=…` 的页面只能退回前端写死的 11 步兜底表，
            # 细条就会显示"第 5 / 11 步"（实测踩到）。
            payload["phases"] = phase_module.as_list(payload.get("protocol"))
        runtime = manager.get(uuid)
        actual_source = getattr(getattr(runtime, "source", None), "key", None)
        if actual_source:
            # 运行器还在本进程：source 以运行期实际选择为准，
            # 保证同一响应里 source 与 runtime.source_kind 同源（不会一个 lsl 一个 sim）。
            payload["source"] = actual_source
        payload["runtime"] = {
            "alive": bool(runtime and runtime.alive),
            "awaiting_input": runtime.awaiting() if runtime else [],
            "source": runtime.source.key if runtime and runtime.source else payload.get("source"),
            "source_kind": (runtime.source.kind if runtime and runtime.source
                            # 运行器已回收（会话结束后被清理）时按落库的 source 键反推，
                            # 否则历史会话的"仿真"标注会消失（见 _source_kind_from_key）
                            else _source_kind_from_key(payload.get("source"))),
        }
        return Response.json(payload)

    @router.delete(r"/api/sessions/{uuid}")
    def cancel_session(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        with store.read_only(db_path) as conn:
            _session_row(conn, uuid)
        cancelled = manager.cancel(uuid)
        if not cancelled:
            raise Conflict("会话不在运行中，无需取消")
        return Response.json({"cancelled": True, "uuid": uuid})

    @router.get(r"/api/sessions/{uuid}/events")
    def session_events(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        raw_last = request.headers.get("last-event-id") or request.query.get("last_event_id")
        last_id = None
        if raw_last:
            try:
                last_id = int(raw_last)
            except ValueError:
                last_id = None
        runtime = manager.get(uuid)
        bus = runtime.bus if runtime and runtime.alive else None
        if bus is None:
            with store.read_only(db_path) as conn:
                row = _session_row(conn, uuid)
            bus = sse.EventBus(uuid)
            bus.publish("phase", {"key": row["phase"], "label": session_public(row)["phase_label"],
                                  "state": row["status"], "progress": row["progress"]})
            bus.publish("finished", {"status": row["status"], "error": row["error"],
                                     "summary": None, "snapshot": True})
            bus.close()
        subscriber, preload = bus.subscribe(last_id)
        return sse_response(preload, subscriber.events, heartbeat_sec=settings.sse_heartbeat_sec,
                            keep_alive=None)

    # ------------------------------------------------------------ 实时数据
    @router.get(r"/api/sessions/{uuid}/signal")
    def session_signal(request: Request) -> Response:
        """高频原始信号 SSE：按显示刷新率推多通道波形 + 频谱（与窗分析解耦）。

        为什么单独做一条通道：`events` 上的 `window` 事件每 4 秒（或快速模式 0.4 秒）
        才有一帧，波形会"一跳一跳"。这里按 `?hz=`（默认 10 FPS）直接读采集缓冲的
        最新样本，只用于显示，不参与指标与计时，因此不影响报告口径。
        """
        uuid = schemas.ensure_uuid(request.params["uuid"])
        # 顺手回收已结束会话遗留的 feed：非运行会话请求这条流是常态（前端会重连），
        # 因此把清扫放在存活检查之前，任何一次 /signal 都会顺带做一遍幂等回收。
        sweep_signal_feeds()
        runtime = manager.get(uuid)
        if runtime is None or not runtime.alive:
            # 统一错误体：409 一律走 Conflict（error.code = "conflict"），
            # 不手拼响应体绕过 router 的错误体约定（docs/API.md 的状态码表）。
            raise Conflict("该会话当前没有运行中的数据源，无法推送实时信号")

        hz = request.query_float("hz", SIGNAL_DEFAULT_HZ)
        hz = max(1.0, min(SIGNAL_MAX_HZ, hz))
        window_sec = request.query_float("seconds", 10.0)
        window_sec = max(2.0, min(30.0, window_sec))
        max_points = int(request.query_float("points", 1200))
        max_points = max(200, min(4000, max_points))

        source = getattr(runtime, "source", None)
        engine = getattr(source, "engine", None) if source is not None else None
        # 真机调理链是纯 Python 双二阶，成本与窗长线性相关（2 通道 10 秒窗 ≈ 30 ms/帧，
        # 见 `ningsi/_analysis/lead_bench_preprocess.py`）；10 FPS 的显示通道按长窗调理会把
        # 推帧线程吃满。这里把显示跨度压到调理上下文长度内，帧里回报的 window_sec 同步变小
        # （前端按 frame.window_sec 定标，见 charts.js），因此不会出现"标签 10 秒、实际 6 秒"。
        condition_seconds = float(getattr(engine, "condition_seconds", 0.0) or 0.0)
        if condition_seconds > 0:
            window_sec = min(window_sec, condition_seconds)
        srate = float(getattr(source, "srate", 0.0) or 250.0)
        channels = int(getattr(source, "channels", 1) or 1)
        labels = list(getattr(source, "channel_labels", []) or [])
        device = str(getattr(source, "device", "") or "")
        kind = str(getattr(source, "kind", "sim") or "sim")

        def samples_provider(seconds: float):
            # ManagedLslSource / SyntheticEEG 都通过 .window(state, seconds) 取数；
            # 真实流走缓冲切片（不阻塞），仿真源即时生成，两者接口一致。
            # 显示通道关掉"有限等待"：真机缓冲未填满时 window() 最多阻塞 fill_timeout(2 秒)，
            # 会把 10 FPS 的推帧拖到 0.5 FPS（指标链路仍走带等待的默认行为）。
            if engine is None:
                raise RuntimeError("该会话没有可用的数据源")
            if hasattr(engine, "condition_seconds"):
                return engine.window("rest", seconds=seconds, wait=False)
            return engine.window("rest", seconds=seconds)

        def window_provider():
            """给实时频谱面板用的紧凑频谱（welch-v1 口径，独立线程 1 秒刷新一次）。

            注意 `source.engine` 是**数据源本身**（SyntheticEEG / ManagedLslSource），
            不是 WindowEngine——`spectrum_probe` 定义在 WindowEngine 上，
            所以要取 `runtime.engine`（最初这里取错了对象，频谱一直是空的）。
            """
            window_engine = getattr(runtime, "engine", None)
            probe = getattr(window_engine, "spectrum_probe", None)
            if probe is None:
                return None
            try:
                data = probe(seconds=engine_config.WINDOW_SEC)
            except Exception as exc:  # noqa: BLE001 - 频谱失败不影响波形
                LOGGER.debug("实时频谱计算失败：%s", exc)
                return None
            if not data or not data.get("usable"):
                return None
            return {
                "spectrum": {"freqs": data.get("freqs", []), "power_db": data.get("power_db", []),
                             "segments": data.get("segments", 0),
                             "delta_f_hz": data.get("delta_f_hz", 0.0)},
                "band_peaks": data.get("peaks", {}),
                "bands": data.get("rel", {}),
            }

        def factory() -> signal_feed.SignalFeed:
            return signal_feed.SignalFeed(
                uuid, samples_provider,
                srate=srate, channels=channels, channel_labels=labels,
                window_provider=window_provider, device=device, source_kind=kind,
                config=signal_feed.SignalConfig(window_sec=window_sec, refresh_hz=hz,
                                                max_points_per_channel=max_points),
            )

        feed = signal_feed.REGISTRY.get_or_create(uuid, factory)
        # 同一会话的 feed 会被复用：参数变了（例如界面切换到 20 FPS）必须重新调参，
        # 否则浏览器再怎么请求 hz=20 也还是按第一次的刷新率推。
        feed.retune(signal_feed.SignalConfig(window_sec=window_sec, refresh_hz=hz,
                                             max_points_per_channel=max_points))
        subscriber, preload = feed.subscribe()

        def release_signal_feed() -> None:
            """流结束时释放订阅；会话已结束且无人订阅时连注册表条目一起回收。

            只在这个条件下 `drop()`：会话还活着时不能动注册表条目，
            否则会把正在推流的 feed 关掉（同一会话可能有多个订阅者/重连）。
            """
            feed.unsubscribe(subscriber)
            if feed.subscriber_count == 0 and not (runtime and runtime.alive):
                signal_feed.REGISTRY.drop(uuid)

        return sse_response(preload, subscriber.events,
                            heartbeat_sec=settings.sse_heartbeat_sec,
                            # 会话结束后这条流就没有数据源了：主动收流，而不是继续推"停摆后"的帧。
                            # 收流会触发 on_close，在那里按「会话不存活 + 订阅者归零」回收注册表条目。
                            keep_alive=lambda: feed.is_subscribed(subscriber) and runtime.alive,
                            on_close=release_signal_feed)

    @router.get(r"/api/sessions/{uuid}/live")
    def session_live(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        runtime = manager.get(uuid)
        with store.read_only(db_path) as conn:
            row = _session_row(conn, uuid)
            payload = {
                "uuid": uuid,
                "status": row["status"],
                "phase": row["phase"],
                "progress": row["progress"],
                "indicator_summary": jsonable(repo.latest_metric_summary(conn, row["id"])),
                "alerts": jsonable(schemas.rows_to_dicts(repo.list_alerts(conn, row["id"]))),
            }
        payload["awaiting_input"] = runtime.awaiting() if runtime else []
        payload["runtime_alive"] = bool(runtime and runtime.alive)
        return Response.json(payload)

    @router.get(r"/api/sessions/{uuid}/heatmap")
    def session_heatmap(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        runtime = manager.get(uuid)
        series = list(runtime.heatmap_series) if runtime and runtime.heatmap_series else []
        if not series:
            with store.read_only(db_path) as conn:
                row = _session_row(conn, uuid)
                metrics = repo.list_metrics(conn, row["id"], kind="indicator", name="focus")
                series = [(item["t_sec"], item["value"]) for item in metrics
                          if item["t_sec"] is not None]
        from ningsi.monitoring.heatmap import MISSING_COLOR, band_of, legend

        cells = []
        for t_end, score in series:
            band = band_of(score)
            cells.append({
                "index": band["index"],
                "label": band["label"],
                "color": band["color"],
                "score": band["score"],          # 缺失窗为 null，前端据此画灰色块
                "low": band["low"],
                "high": band["high"],
                "t": round(float(t_end or 0.0), 3),
            })
        return Response.json({
            "uuid": uuid,
            "columns": 30,
            "cells": jsonable(cells),
            "legend": jsonable(legend()),
            "missing_color": MISSING_COLOR,
            "scored": sum(1 for cell in cells if cell["index"] >= 0),
            "missing": sum(1 for cell in cells if cell["index"] < 0),
            "note": "低质量窗不参与指标与预警计时，单独用缺失色块表示。",
        })

    @router.get(r"/api/sessions/{uuid}/trend")
    def session_trend(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        field = request.query.get("field") or "focus"
        period = request.query.get("period") or "week"
        if field not in ("focus", "relax", "load"):
            raise ValidationError("field 仅支持 focus / relax / load", detail=field)
        if period not in ("week", "month"):
            raise ValidationError("period 仅支持 week / month", detail=period)
        with store.read_only(db_path) as conn:
            row = _session_row(conn, uuid)
            records = repo.session_points(conn, public_id=row["public_id"])
        from ningsi.monitoring import history as history_module
        usable, rejected = _comparable(records, history_module)
        points = history_module.aggregate(usable, field, period)
        return Response.json({"field": field, "period": period, "points": jsonable(points),
                              "sessions": len(records), "comparable": len(usable),
                              "rejected": len(rejected),
                              "rejected_reasons": _reason_counts(rejected)})

    # ------------------------------------------------------------ 量表作答
    @router.get(r"/api/scales")
    def scale_catalog(request: Request) -> Response:
        limit, offset = schemas.pagination(request)
        return Response.json(_collection_page(scales_domain.catalog(), limit=limit, offset=offset))

    @router.get(r"/api/scales/{code}")
    def scale_definition(request: Request) -> Response:
        code = (request.params["code"] or "").upper()
        if code not in ("SAS", "SDS"):
            raise NotFound(f"未支持的量表：{code}")
        return Response.json(scales_domain.define(code))

    @router.post(r"/api/sessions/{uuid}/scales/{code}")
    def submit_scale(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        code = (request.params["code"] or "").upper()
        if code not in ("SAS", "SDS"):
            raise NotFound(f"未支持的量表：{code}")
        payload = schemas.typed(request.json())
        responses = payload.get("responses")
        try:
            scored = scales_domain.score(code, responses)
        except ValueError as exc:
            from ningsi_studio.http.router import ValidationError
            raise ValidationError(str(exc)) from exc
        runtime = manager.get(uuid)
        delivered = bool(runtime and runtime.provide_input(f"scales:{code}",
                                                           {"responses": scored["responses"]}))
        return Response.json({"code": code, "delivered_to_runtime": delivered,
                              "raw_score": scored["raw_score"],
                              "standard_score": scored["standard_score"], "level": scored["level"],
                              "answered": scored["answered"], "missing": scored["missing_items"]})

    # ------------------------------------------------------------ 行为任务
    @router.get(r"/api/sessions/{uuid}/behaviors/sart/sequence")
    def sart_sequence(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        runtime = _require_runtime(uuid, manager)
        if runtime.sart_task is None:
            with store.read_only(db_path) as conn:
                row = _session_row(conn, uuid)
                runtime.sart_task = _rebuild_sart(row)
        return Response.json(runtime.sart_task.sequence_payload())
    @router.post(r"/api/sessions/{uuid}/behaviors/sart/trial")
    def sart_trial(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        runtime = _require_runtime(uuid, manager)
        payload = schemas.typed(request.json())
        rt = payload.get("rt")
        if rt is not None:
            rt = schemas.clamp_float(rt, 0.0, low=0.05, high=10.0, name="反应时")
        delivered = runtime.provide_input("sart", {
            "phase": payload.get("phase") or "main",
            "index": schemas.clamp_int(payload.get("index"), -1, low=0, high=1000, name="试次序号"),
            "responded": schemas.as_bool(payload.get("responded"), False),
            "rt": rt,
        })
        if not delivered:
            raise Conflict("当前会话不处于 SART 阶段或已结束")
        return Response.json({"accepted": True})

    @router.get(r"/api/sessions/{uuid}/behaviors/pvt/sequence")
    def pvt_sequence(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        runtime = _require_runtime(uuid, manager)
        if runtime.pvt_task is None:
            from ningsi_studio.domain.behavior import PvtTask
            runtime.pvt_task = PvtTask(seed=abs(hash(uuid)) % 100000)
        return Response.json(runtime.pvt_task.sequence_payload())

    @router.post(r"/api/sessions/{uuid}/behaviors/pvt/trial")
    def pvt_trial(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        runtime = _require_runtime(uuid, manager)
        payload = schemas.typed(request.json())
        rt = payload.get("rt")
        if rt is not None:
            rt = schemas.clamp_float(rt, 0.0, low=0.05, high=10.0, name="反应时")
        delivered = runtime.provide_input("pvt", {
            "index": schemas.clamp_int(payload.get("index"), -1, low=0, high=1000, name="试次序号"),
            "responded": schemas.as_bool(payload.get("responded"), False),
            "rt": rt,
            "false_start": schemas.as_bool(payload.get("false_start"), False),
        })
        if not delivered:
            raise Conflict("当前会话不处于 PVT 阶段或已结束")
        return Response.json({"accepted": True})

    @router.get(r"/api/sessions/{uuid}/behaviors/{task}/result")
    def behavior_result(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        task = (request.params["task"] or "").lower()
        if task not in ("sart", "pvt"):
            raise NotFound(f"未支持的行为任务：{task}")
        with store.read_only(db_path) as conn:
            row = _session_row(conn, uuid)
            stored = repo.behavior_run_for(conn, row["id"], "sart" if task == "sart" else "pvt-b")
        runtime = manager.get(uuid)
        if stored is None:
            if runtime is None:
                raise NotFound("该任务的结果尚未落库，且运行器已不在本进程中")
            if runtime.alive:
                raise NotFound("该任务尚未完成或结果尚未落库")
            current = runtime.sart_task if task == "sart" else runtime.pvt_task
            if current is None:
                raise NotFound("该任务尚未完成或结果尚未落库")
            try:
                return Response.json({"result": current.result(), "trials": current.trials_payload()})
            except ValueError as exc:
                raise Conflict(str(exc)) from exc
        return Response.json({"result": json.loads(stored["metrics"] or "{}"),
                              "trials": json.loads(stored["trials"] or "{}")})

    # ------------------------------------------------------------ 评估与训练
    @router.get(r"/api/sessions/{uuid}/assessment")
    def session_assessment(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        with store.read_only(db_path) as conn:
            row = _session_row(conn, uuid)
            runs = repo.list_runs(conn, row["id"])
        for item in reversed(runs):
            if item["phase"] == "assessment" and item["status"] == "done":
                return Response.json(json.loads(item["payload"] or "{}"))
        runtime = manager.get(uuid)
        if runtime and runtime.alive:
            raise Conflict("联合评估尚未完成，请等待 assessment 阶段结束")
        raise NotFound("该会话没有可用的联合评估结果")

    @router.get(r"/api/sessions/{uuid}/training")
    def session_training(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        with store.read_only(db_path) as conn:
            row = _session_row(conn, uuid)
            segments = schemas.rows_to_dicts(repo.list_training_segments(conn, row["id"]))
            runs = repo.list_runs(conn, row["id"])
        summary = {}
        for item in reversed(runs):
            if item["phase"] == "training" and item["status"] == "done":
                summary = json.loads(item["payload"] or "{}").get("training", {})
                break
        if not segments and not summary:
            raise NotFound("该会话没有训练记录")
        for segment in segments:
            stored = json.loads(segment.get("samples") or "null")
            if isinstance(stored, dict):
                # 完整分段明细（含 stats 与 samples）由运行期原样保存
                stored.update({key: value for key, value in segment.items() if key != "samples"})
                segment.clear()
                segment.update(stored)
            else:
                segment["samples"] = stored or []
        return Response.json({"segments": jsonable(segments), "summary": jsonable(summary)})

    @router.get(r"/api/sessions/{uuid}/report")
    def session_report(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        with store.read_only(db_path) as conn:
            row = _session_row(conn, uuid)
            artifact = repo.artifact_for(conn, row["id"], "report_json")
            markdown_artifact = repo.artifact_for(conn, row["id"], "report_md")
            runs = repo.list_runs(conn, row["id"])
        if artifact is not None and Path(artifact["path"]).exists():
            payload = json.loads(Path(artifact["path"]).read_text(encoding="utf-8"))
            # 同时带上报告原文（Markdown），保证"报告内容"可复算、可直接引用
            if markdown_artifact is not None and Path(markdown_artifact["path"]).exists():
                payload["report_markdown"] = Path(markdown_artifact["path"]).read_text(
                    encoding="utf-8")
                payload["report_markdown_path"] = markdown_artifact["path"]
            return Response.json(payload)
        # 未落盘时给出可解释的部分结果，而不是空响应
        partial = {"uuid": uuid, "status": row["status"], "phase": row["phase"],
                   "progress": row["progress"], "partial": True,
                   "runs": jsonable([{"phase": item["phase"], "status": item["status"],
                                      "error": item["error"]} for item in runs])}
        with store.read_only(db_path) as conn:
            partial["indicator_summary"] = jsonable(repo.latest_metric_summary(conn, row["id"]))
            partial["alerts"] = jsonable(schemas.rows_to_dicts(repo.list_alerts(conn, row["id"])))
        return Response.json(partial, status=202)

    @router.get(r"/api/sessions/{uuid}/artifacts")
    def session_artifacts(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        limit, offset = schemas.pagination(request)
        with store.read_only(db_path) as conn:
            row = _session_row(conn, uuid)
            artifacts = schemas.rows_to_dicts(repo.list_artifacts(conn, row["id"]))
        for item in artifacts:
            item["exists"] = Path(item["path"]).exists() if item.get("path") else False
            item["download"] = f"/api/sessions/{uuid}/artifacts/{item['kind']}"
        return Response.json(_collection_page(artifacts, limit=limit, offset=offset))

    @router.get(r"/api/sessions/{uuid}/artifacts/{kind}")
    def download_artifact(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        kind = request.params["kind"]
        with store.read_only(db_path) as conn:
            row = _session_row(conn, uuid)
            artifact = repo.artifact_for(conn, row["id"], kind)
        if artifact is None:
            if kind == "zip":
                return _build_zip_response(uuid, db_path, settings, manager)
            raise NotFound(f"该会话没有产物：{kind}")
        path = Path(artifact["path"])
        if not path.exists():
            raise NotFound(f"产物文件已不存在：{path}")
        content_type = {
            "report_md": "text/markdown; charset=utf-8",
            "report_json": "application/json; charset=utf-8",
            "heatmap_svg": "image/svg+xml",
            "trend_svg": "image/svg+xml",
            "model": "application/json; charset=utf-8",
            "history": "application/x-ndjson; charset=utf-8",
            "zip": "application/zip",
        }.get(kind, "application/octet-stream")

        body = path.read_bytes()
        if kind == "report_json":
            # 让下载到的报告与 /report 接口内容等价：附上 Markdown 原文，
            # 这样评审拿到的单个 JSON 文件本身就是自洽、可直接引用的。
            try:
                payload = json.loads(body.decode("utf-8"))
                with store.read_only(db_path) as conn:
                    markdown_artifact = repo.artifact_for(conn, row["id"], "report_md")
                if markdown_artifact is not None and Path(markdown_artifact["path"]).exists():
                    payload["report_markdown"] = Path(markdown_artifact["path"]).read_text(
                        encoding="utf-8")
                    payload["report_markdown_path"] = markdown_artifact["path"]
                    body = dump(payload)
            except (ValueError, UnicodeDecodeError, OSError):
                pass                                  # 解析失败就按原文件返回

        return Response(status=200, body=body, content_type=content_type,
                        headers={"Content-Disposition": f'attachment; filename="{path.name}"'})

    @router.get(r"/api/sessions/{uuid}/export.zip")
    def export_zip(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        return _build_zip_response(uuid, db_path, settings, manager)

    # ------------------------------------------------------------ 模型与趋势
    @router.post(r"/api/models/train")
    def train_model(request: Request) -> Response:
        payload = schemas.typed(request.json())
        subjects = schemas.clamp_int(payload.get("subjects"), 6, low=2, high=40, name="被试数")
        per_state = schemas.clamp_int(payload.get("windows_per_state"), 8, low=1, high=200,
                                      name="每状态窗数")
        model_path = Path(settings.data_dir) / "models" / "classifier.json"
        metrics = model_training.train_and_save(model_path, subjects=subjects,
                                                windows_per_state=per_state)
        return Response.json(metrics)

    @router.get(r"/api/reports/trend")
    def global_trend(request: Request) -> Response:
        field = request.query.get("field") or "focus"
        period = request.query.get("period") or "week"
        if field not in ("focus", "relax", "load"):
            raise ValidationError("field 仅支持 focus / relax / load", detail=field)
        if period not in ("week", "month"):
            raise ValidationError("period 仅支持 week / month", detail=period)
        public_id = request.query.get("participant")
        with store.read_only(db_path) as conn:
            records = repo.session_points(
                conn, public_id=schemas.normalize_public_id(public_id) if public_id else None)
        from ningsi.monitoring import history as history_module
        usable, rejected = _comparable(records, history_module)
        points = history_module.aggregate(usable, field, period)
        # 台账按会话分别写在 <runs_root>/<uuid>/history/sessions.jsonl，
        # 这里汇总全部会话的台账再聚合（旧实现读 <runs_root>/history 那个不存在的共享文件）。
        ledger_points = _ledger_trend(_ledger_records(settings), field, period)
        return Response.json({
            "field": field, "period": period,
            "points": jsonable(points),
            "ledger_points": jsonable(ledger_points),
            "sessions": len(records),
            "comparable": len(usable),
            "rejected": len(rejected),
            "rejected_reasons": _reason_counts(rejected),
            "note": "设备、采样率或通道数变化的记录不参与趋势聚合（可比性保护）。",
        })

    # ------------------------------------------------------------ 总览
    @router.get(r"/api/overview")
    def overview(request: Request) -> Response:
        # 首页是界面里最常轮询的读接口：在这里顺带回收已结束会话遗留的实时信号 feed，
        # 这样"开过实时监测又结束了的会话"不会永久占着注册表条目与对象。
        sweep_signal_feeds()
        with store.read_only(db_path) as conn:
            payload = repo.overview(conn)
            rows, _ = repo.list_sessions(conn, limit=8, offset=0)
            payload["latest_detail"] = [session_public(row) for row in rows]
            rows, total = repo.list_subjects(conn, limit=5, offset=0)
            payload["subjects_recent"] = [_subject_public(row) for row in rows]
            payload["subjects"] = max(payload["subjects"], total)
        sources = live_source.list_available(0.4)
        # 只报"检测到的实时源"（真机优先，其次内置仿真 outlet）；没有就是 None → 顶栏"无信号"。
        # 仿真脑电源 sim-bsense **不参与**这个回退（它必须被显式选择）。
        payload.update(live_source.detected_source_fields(sources))
        payload["has_sim_source"] = True          # 仿真始终可用，但只能显式选择
        payload["active_sessions"] = manager.active_uuids()
        return Response.json(payload)

    router.manager = manager          # type: ignore[attr-defined]
    return router


# --------------------------------------------------------------------- 辅助

def _require_runtime(uuid: str, manager: SessionManager):
    runtime = manager.get(uuid)
    if runtime is None or not runtime.alive:
        raise Conflict("会话不在运行中：运行中才可提交作答")
    return runtime


def _comparable(records, history_module) -> tuple[list, list]:
    """趋势聚合前先做可比性过滤：设备、采样率或通道数变化即排除。"""
    if not records:
        return [], []
    reference = records[0]
    try:
        return history_module.comparable(
            records,
            reference.get("device"),
            reference.get("srate"),
            reference.get("channels"),
        )
    except Exception:  # noqa: BLE001 - 聚合失败不应让接口 500
        return list(records), []


def _reason_counts(rejected) -> dict:
    counts: dict = {}
    for item in rejected or []:
        reason = ""
        if isinstance(item, (list, tuple)) and len(item) > 1:
            reason = str(item[1])
        elif isinstance(item, dict):
            reason = str(item.get("reason", ""))
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _ledger_records(settings: Settings) -> list:
    """汇总 JSONL 台账记录（按会话去重）。

    运行时把台账写在**每个会话自己的目录**：`<runs_root>/<uuid>/history/sessions.jsonl`
    （见 `core/runtime.py` 的 `runs_root` 属性与 README「数据目录」一节），
    而 `export-ledger`（`--rebuild-ledger`）重放到 `<data_dir>/history/sessions.jsonl`。
    两个位置都要读：只读 `<runs_root>/history/sessions.jsonl` 会永远拿到空数组，
    于是 `/api/reports/trend` 的 `ledger_points` 恒为空。
    """
    roots = [path.parent.parent
             for path in sorted(Path(settings.runs_root).glob("*/history/sessions.jsonl"))]
    shared = Path(settings.data_dir) / "history" / "sessions.jsonl"
    if shared.exists():
        roots.append(shared.parent.parent)
    records: list = []
    seen: set = set()
    for root in roots:
        for record in paired_ledger.read_history(root):
            key = record.get("session_uuid") or (
                record.get("participant"), record.get("session"), record.get("run"),
                record.get("recorded_at"))
            if key in seen:                       # 重放过的台账会与按会话台账重合
                continue
            seen.add(key)
            records.append(record)
    return records


def _ledger_trend(records, field: str, period: str) -> list:
    """把台账记录聚合成趋势点（与 `paired_ledger.trend_points` 同一口径）。"""
    if not records:
        return []
    from ningsi.monitoring import history as history_module

    try:
        reference = records[-1]
        usable, rejected = history_module.comparable(
            records, reference.get("device"), reference.get("srate"), reference.get("channels"))
        points = history_module.aggregate(usable, field, period)
    except Exception as exc:  # noqa: BLE001 - 聚合失败不应让接口 500
        LOGGER.warning("台账趋势聚合失败：%s", exc)
        return []
    for point in points:
        point["rejected"] = len(rejected)
    return jsonable(points)


def _rebuild_sart(row):
    from ningsi_studio.domain.behavior import SartTask

    public_id = row["public_id"] if "public_id" in row.keys() else "p01"
    return SartTask(public_id, "01", "001")


def _build_zip_response(uuid: str, db_path: Path, settings: Settings, manager) -> Response:
    with store.read_only(db_path) as conn:
        row = _session_row(conn, uuid)
        artifacts = {item["kind"]: item["path"] for item in repo.list_artifacts(conn, row["id"])}
    runtime = manager.get(uuid)
    if runtime is not None and getattr(runtime, "artifacts", None):
        artifacts.update({kind: str(path) for kind, path in runtime.artifacts.items()})
    if not artifacts:
        raise NotFound("该会话还没有可打包的产物")
    target = Path(settings.runs_root) / uuid / "export.zip"
    meta = {"uuid": uuid, "status": row["status"], "phase": row["phase"],
            "device": row["device"], "source": row["source"],
            "engine_versions": json.loads(row["engine_versions"] or "{}")}
    export_domain.build_zip(target, {kind: Path(path) for kind, path in artifacts.items()}, meta)
    return Response(status=200, body=target.read_bytes(), content_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="ningsi-{uuid[:8]}.zip"'})


# ---------------------------------------------------------------- OpenAPI 描述

_PATH_SPECS = [
    ("get", "/api/health", "服务健康与运行概况", None),
    ("get", "/api/config", "引擎口径与阈值全量定义", None),
    ("get", "/api/devices", "可用数据源（实时 LSL 流在前，仿真源可显式选择）", "probe"),
    ("get", "/api/devices/status", "运行中会话的设备健康状况（是否在收数、实测采样率）", None),
    ("get", "/api/devices/preview", "无会话的设备实时预览状态与一窗波形（?window=1）", "window"),
    ("post", "/api/devices/preview", "启动设备实时预览（不传 source 时自动选检测到的实时源）", None),
    ("delete", "/api/devices/preview", "停止设备实时预览", None),
    ("get", "/api/overview", "首页总览统计", None),
    ("get", "/api/subjects", "被试列表（分页 / 搜索）", "query,page,limit"),
    ("post", "/api/subjects", "新建被试（匿名编号 + 同意记录）", None),
    ("get", "/api/subjects/{public_id}", "被试明细", None),
    ("patch", "/api/subjects/{public_id}", "修改被试信息", None),
    ("get", "/api/subjects/{public_id}/sessions", "某被试的会话列表", "page,limit"),
    ("post", "/api/sessions", "创建并启动一次会话", None),
    ("get", "/api/sessions", "会话列表（被试 / 状态过滤）", "participant,status,page,limit"),
    ("get", "/api/sessions/{uuid}", "会话详情（含阶段、预警、产物）", None),
    ("delete", "/api/sessions/{uuid}", "取消运行中的会话", None),
    ("get", "/api/sessions/{uuid}/events", "SSE 实时事件流", "last_event_id"),
    ("get", "/api/sessions/{uuid}/live", "轮询式实时快照（SSE 兜底）", None),
    ("get", "/api/sessions/{uuid}/signal",
     "高频原始信号 SSE：多通道波形 + 频谱（hz/seconds/points 可调，默认 10 FPS）", "hz"),
    ("get", "/api/sessions/{uuid}/heatmap", "状态热力图矩阵（五档 + 缺失色）", None),
    ("get", "/api/sessions/{uuid}/trend", "该被试的周/月趋势", "field,period"),
    ("get", "/api/scales", "量表目录", None),
    ("get", "/api/scales/{code}", "量表题干与选项", None),
    ("post", "/api/sessions/{uuid}/scales/{code}", "提交量表作答", None),
    ("get", "/api/sessions/{uuid}/behaviors/sart/sequence", "SART 序列", None),
    ("post", "/api/sessions/{uuid}/behaviors/sart/trial", "提交 SART 单试次", None),
    ("get", "/api/sessions/{uuid}/behaviors/pvt/sequence", "PVT-B 刺激时刻表", None),
    ("post", "/api/sessions/{uuid}/behaviors/pvt/trial", "提交 PVT-B 单试次", None),
    ("get", "/api/sessions/{uuid}/behaviors/{task}/result", "行为任务结果", None),
    ("get", "/api/sessions/{uuid}/assessment", "联合评估结果", None),
    ("get", "/api/sessions/{uuid}/training", "训练分段与总览", None),
    ("get", "/api/sessions/{uuid}/report", "结构化评估报告", None),
    ("get", "/api/sessions/{uuid}/artifacts", "产物清单", None),
    ("get", "/api/sessions/{uuid}/artifacts/{kind}", "下载单个产物", None),
    ("get", "/api/sessions/{uuid}/export.zip", "打包下载全部产物", None),
    ("post", "/api/models/train", "训练基线模型", None),
    ("get", "/api/reports/trend", "跨会话趋势（含不可比计数）", "participant,field,period"),
    ("get", "/api/openapi.json", "本描述文件", None),
]


def _openapi_spec() -> dict:
    paths: dict = {}
    for method, path, summary, params in _PATH_SPECS:
        item = {
            "summary": summary,
            "tags": [path.split("/")[2] if path.count("/") > 2 else "base"],
            "responses": {
                "200": {"description": "成功"},
                "400": {"description": "请求不合法"},
                "404": {"description": "对象不存在"},
                "409": {"description": "状态冲突（如任务未处于该阶段）"},
                "422": {"description": "字段校验失败"},
                "429": {"description": "并发会话超限"},
            },
        }
        if params:
            item["parameters"] = [
                {"name": name, "in": "query", "required": False, "schema": {"type": "string"}}
                for name in params.split(",")
            ]
        paths.setdefault(path, {})[method] = item
    return {
        "openapi": "3.0.3",
        "info": {"title": API_TITLE, "version": API_VERSION,
                 "description": "凝思 Studio：便携脑电专注力训练与心理状态评估的 Web 接口。"
                                "算法口径全部来自 ningsi 引擎（welch-v1 / indicator-v1 / "
                                "baseline-v1 / neurofeedback-v1）。"},
        "servers": [{"url": "/", "description": "同端口静态前端与 API"}],
        "paths": paths,
        "components": {
            "schemas": {
                "Error": {"type": "object", "properties": {
                    "error": {"type": "object", "properties": {
                        "code": {"type": "string"}, "message": {"type": "string"},
                        "detail": {}}}}},
                "Session": {"type": "object", "properties": {
                    "uuid": {"type": "string"}, "participant": {"type": "string"},
                    "status": {"type": "string"}, "phase": {"type": "string"},
                    "progress": {"type": "number"}}},
            },
        },
        "x-phases": phase_module.as_list(),
        "x-note": "前端与文档同源；接口只允许通过 /api 前缀访问。",
    }


__all__ = ["build_router", "session_public", "StudioSessionManager", "API_TITLE", "API_VERSION",
           "dump", "ApiError", "Response"]
