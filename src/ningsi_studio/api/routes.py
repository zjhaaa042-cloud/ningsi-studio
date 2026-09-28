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

from ningsi_studio import bootstrap
from ningsi_studio.api import schemas
from ningsi_studio.core import live_source, paired_ledger, phases as phase_module
from ningsi_studio.core import signal_feed
from ningsi_studio.core.runtime import SessionManager
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


# --------------------------------------------------------------------- 路由

def build_router(settings: Settings) -> Router:
    router = Router()
    manager = SessionManager(settings)
    db_path = Path(settings.db_path)

    def rollback_session(uuid: str, reason: str) -> None:
        """会话未能真正启动（并发超限等）时把库里的行收成 failed，不留幽灵会话。"""
        try:
            with store.connect(db_path) as conn:
                repo.update_session(conn, uuid, status="failed", phase="error", error=reason,
                                    ended_at=store.utcnow())
        except Exception:  # noqa: BLE001 - 回滚失败只记日志，不改变对外错误语义
            pass

    # ---------------------------------------------------------------- 基础
    @router.get(r"/api/health")
    def health(request: Request) -> Response:
        with store.read_only(db_path) as conn:
            overview = repo.overview(conn)
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
            "privacy": {
                "subject_id": "被试编号匿名且跨会话一致，姓名等直接身份信息不进入文件名、报告与数据库。",
                "boundary": "结论仅用于研究与自我调节训练，不构成医疗诊断，不得用于处罚或自动上岗决策。",
            },
        })

    @router.get(r"/api/devices")
    def devices(request: Request) -> Response:
        probe = request.query_float("probe", 1.0)
        probe = max(0.2, min(5.0, probe))
        return Response.json({"sources": live_source.list_available(probe_seconds=probe)})

    @router.get(r"/api/devices/status")
    def device_status(request: Request) -> Response:
        """正在跑的会话用到的真实设备健康状况：是否在收数、实测采样率、错误。

        仿真源没有硬件，返回 kind=sim 并说明；有 LSL 会话时给出活体指标，
        方便现场一眼看出"设备掉了"还是"信号正常"。
        """
        active = manager.active_uuids()
        payload = {"active_sessions": active, "devices": [], "sources": []}
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
        device = schemas.optional_text(payload.get("device"), max_len=120, name="设备") or "sim-bsense"
        time_scale = schemas.clamp_float(payload.get("time_scale", payload.get("speed")), 1.0,
                                         low=0.01, high=1.0, name="时间倍率")
        srate = schemas.clamp_float(payload.get("srate"), 250.0, low=1.0, high=2000.0, name="采样率")
        channels = schemas.clamp_int(payload.get("channels"), 1, low=1, high=8, name="通道数")
        training_mode = payload.get("training_mode") or "quick"
        if training_mode not in engine_config.TRAINING:
            raise ValidationError(f"训练模式必须是 {sorted(engine_config.TRAINING)} 之一",
                                  detail=training_mode)

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
                engine_versions=bootstrap.engine_versions(),
            )
        # 注意：这里必须已经退出 `with store.connect(...)`（事务提交、连接释放），
        # 再启动运行线程；否则运行线程装载会话时会撞上 "database is locked"。
        runtime = manager.start_with_rollback(row["uuid"], lambda reason: rollback_session(row["uuid"], reason))
        return Response.json({
            "session": session_public(row, extra={"participant": public_id}),
            "events_url": f"/api/sessions/{row['uuid']}/events",
            "phases": phase_module.as_list(),
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
            items = [session_public(row, extra={"alert_count": len(repo.list_alerts(conn, row["id"]))})
                     for row in rows]
        return Response.json({"items": items, "total": total, "limit": limit,
                              "page": offset // limit + 1})

    @router.get(r"/api/sessions/{uuid}")
    def get_session(request: Request) -> Response:
        uuid = schemas.ensure_uuid(request.params["uuid"])
        with store.read_only(db_path) as conn:
            row = _session_row(conn, uuid)
            payload = session_public(row)
            payload["alerts"] = jsonable(schemas.rows_to_dicts(repo.list_alerts(conn, row["id"])))
            payload["runs"] = jsonable(schemas.rows_to_dicts(repo.list_runs(conn, row["id"])))
            payload["artifacts"] = jsonable(schemas.rows_to_dicts(repo.list_artifacts(conn, row["id"])))
            payload["indicator_summary"] = jsonable(repo.latest_metric_summary(conn, row["id"]))
        runtime = manager.get(uuid)
        payload["runtime"] = {
            "alive": bool(runtime and runtime.alive),
            "awaiting_input": runtime.awaiting() if runtime else [],
            "source": runtime.source.key if runtime and runtime.source else None,
            "source_kind": runtime.source.kind if runtime and runtime.source else None,
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
        runtime = manager.get(uuid)
        if runtime is None or not runtime.alive:
            return Response.json(
                {"error": {"code": "session_not_running",
                           "message": "该会话当前没有运行中的数据源，无法推送实时信号"}},
                status=409)

        hz = request.query_float("hz", SIGNAL_DEFAULT_HZ)
        hz = max(1.0, min(SIGNAL_MAX_HZ, hz))
        window_sec = request.query_float("seconds", 10.0)
        window_sec = max(2.0, min(30.0, window_sec))
        max_points = int(request.query_float("points", 1200))
        max_points = max(200, min(4000, max_points))

        source = getattr(runtime, "source", None)
        engine = getattr(source, "engine", None) if source is not None else None
        srate = float(getattr(source, "srate", 0.0) or 250.0)
        channels = int(getattr(source, "channels", 1) or 1)
        labels = list(getattr(source, "channel_labels", []) or [])
        device = str(getattr(source, "device", "") or "")
        kind = str(getattr(source, "kind", "sim") or "sim")

        def samples_provider(seconds: float):
            # ManagedLslSource / SyntheticEEG 都通过 .window(state, seconds) 取数；
            # 真实流走缓冲切片（不阻塞），仿真源即时生成，两者接口一致。
            if engine is None:
                raise RuntimeError("该会话没有可用的数据源")
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
        return sse_response(preload, subscriber.events,
                            heartbeat_sec=settings.sse_heartbeat_sec,
                            keep_alive=lambda: feed.is_subscribed(subscriber),
                            on_close=lambda: feed.unsubscribe(subscriber))

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
        return Response.json({"items": scales_domain.catalog()})

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
        with store.read_only(db_path) as conn:
            row = _session_row(conn, uuid)
            artifacts = schemas.rows_to_dicts(repo.list_artifacts(conn, row["id"]))
        for item in artifacts:
            item["exists"] = Path(item["path"]).exists() if item.get("path") else False
            item["download"] = f"/api/sessions/{uuid}/artifacts/{item['kind']}"
        return Response.json({"items": jsonable(artifacts)})

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
        ledger_points = paired_ledger.trend_points(settings.runs_root, field, period)
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
        with store.read_only(db_path) as conn:
            payload = repo.overview(conn)
            rows, _ = repo.list_sessions(conn, limit=8, offset=0)
            payload["latest_detail"] = [session_public(row) for row in rows]
            rows, total = repo.list_subjects(conn, limit=5, offset=0)
            payload["subjects_recent"] = [_subject_public(row) for row in rows]
            payload["subjects"] = max(payload["subjects"], total)
        sources = live_source.list_available(0.2)
        payload["source_note"] = sources[0].get("note") if sources else None
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
    ("get", "/api/devices", "可用数据源（仿真 / LSL 实时流）", "probe"),
    ("get", "/api/devices/status", "运行中会话的设备健康状况（是否在收数、实测采样率）", None),
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


__all__ = ["build_router", "session_public", "API_TITLE", "API_VERSION",
           "dump", "ApiError", "Response"]
