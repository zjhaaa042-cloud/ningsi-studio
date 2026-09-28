"""数据库访问层：只做增删查改，不含业务判断。"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterable

from ningsi_studio.db import sqlite_store as store
from ningsi_studio.db.sqlite_store import read_only, utcnow


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _load(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


# --------------------------------------------------------------------- subjects

def create_subject(conn, public_id: str, *, label=None, age_band=None, sex=None,
                   handedness=None, consent_version="consent-v1", consent_at=None,
                   note=None) -> int:
    stamp = utcnow()
    cursor = conn.execute(
        """INSERT INTO subjects (public_id, label, age_band, sex, handedness,
                                 consent_version, consent_at, note, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (public_id, label, age_band, sex, handedness, consent_version,
         consent_at or stamp, note, stamp, stamp),
    )
    return int(cursor.lastrowid)


def next_public_id(conn) -> str:
    """下一个自动编号：在现有 `p<数字>` 编号上取最大值 +1。

    不用"记录条数 +1"是因为历史里可能存在显式编号（q01 等），
    按条数递增会与之撞号或跳过编号，看起来像丢数据。
    """
    rows = conn.execute(
        "SELECT public_id FROM subjects WHERE public_id GLOB 'p[0-9]*'"
    ).fetchall()
    numbers = []
    for row in rows:
        digits = str(row["public_id"])[1:]
        if digits.isdigit():
            numbers.append(int(digits))
    return f"p{(max(numbers) + 1) if numbers else 1:02d}"


def find_subject(conn, public_id: str):
    return conn.execute("SELECT * FROM subjects WHERE public_id = ?", (public_id,)).fetchone()


def get_subject_by_pk(conn, subject_id: int):
    return conn.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()


def list_subjects(conn, query: str = "", limit: int = 50, offset: int = 0) -> tuple[list, int]:
    clause, params = "", []
    if query:
        clause = "WHERE public_id LIKE ? OR IFNULL(label,'') LIKE ? OR IFNULL(note,'') LIKE ?"
        params = [f"%{query}%"] * 3
    total = conn.execute(f"SELECT COUNT(*) AS n FROM subjects {clause}", params).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT * FROM subjects {clause}
            ORDER BY id DESC LIMIT ? OFFSET ?""", params + [limit, offset]
    ).fetchall()
    return rows, int(total)


def update_subject(conn, public_id: str, **fields) -> int:
    allowed = {"label", "age_band", "sex", "handedness", "note", "consent_version", "consent_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return 0
    updates["updated_at"] = utcnow()
    clause = ", ".join(f"{key} = ?" for key in updates)
    cursor = conn.execute(
        f"UPDATE subjects SET {clause} WHERE public_id = ?",
        list(updates.values()) + [public_id],
    )
    return cursor.rowcount


def subject_session_counts(conn, subject_id: int) -> dict:
    rows = conn.execute(
        """SELECT status, COUNT(*) AS n FROM sessions WHERE subject_id = ? GROUP BY status""",
        (subject_id,),
    ).fetchall()
    counts = {row["status"]: row["n"] for row in rows}
    counts["total"] = sum(counts.values())
    return counts


# --------------------------------------------------------------------- sessions

def create_session(conn, subject_id: int, *, label=None, device="sim-bsense", srate=250.0,
                   channels=1, source="sim-bsense", time_scale=1.0,
                   training_mode="quick", engine_versions=None) -> dict:
    session_uuid = uuid.uuid4().hex
    stamp = utcnow()
    cursor = conn.execute(
        """INSERT INTO sessions (uuid, subject_id, label, device, srate, channels, source,
                                 time_scale, training_mode, status, phase, progress,
                                 engine_versions, started_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 'qc', 0.0, ?, ?, ?)""",
        (session_uuid, subject_id, label, device, srate, channels, source,
         float(time_scale), training_mode, _dump(engine_versions), stamp, stamp),
    )
    return get_session_pk(conn, int(cursor.lastrowid))


def get_session_pk(conn, session_pk: int):
    return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_pk,)).fetchone()


def get_session(conn, session_uuid: str):
    """按 uuid 取会话；连带被试编号，便于上层直接展示（不额外 join）。"""
    return conn.execute(
        """SELECT sessions.*, s.public_id AS public_id, s.label AS subject_label
           FROM sessions JOIN subjects s ON s.id = sessions.subject_id
           WHERE sessions.uuid = ?""",
        (session_uuid,),
    ).fetchone()


def list_sessions(conn, *, public_id=None, status=None, limit=50, offset=0) -> tuple[list, int]:
    joins = "JOIN subjects s ON s.id = sessions.subject_id"
    where, params = [], []
    if public_id:
        where.append("s.public_id = ?")
        params.append(public_id)
    if status:
        where.append("sessions.status = ?")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM sessions {joins} {clause}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT sessions.*, s.public_id AS public_id, s.label AS subject_label
            FROM sessions {joins} {clause}
            ORDER BY sessions.id DESC LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()
    return rows, int(total)


def update_session(conn, session_uuid: str, **fields) -> int:
    allowed = {"status", "phase", "progress", "error", "ended_at", "started_at",
               "device", "srate", "channels", "source"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return 0
    clause = ", ".join(f"{key} = ?" for key in updates)
    cursor = conn.execute(
        f"UPDATE sessions SET {clause} WHERE uuid = ?", list(updates.values()) + [session_uuid]
    )
    return cursor.rowcount


def running_sessions(conn) -> list:
    return conn.execute(
        "SELECT * FROM sessions WHERE status IN ('running','queued') ORDER BY id"
    ).fetchall()


# ------------------------------------------------------------------------- runs

def start_run(conn, session_id: int, phase: str) -> int:
    cursor = conn.execute(
        """INSERT INTO runs (session_id, phase, status, started_at, payload)
           VALUES (?, ?, 'running', ?, '{}')""",
        (session_id, phase, utcnow()),
    )
    return int(cursor.lastrowid)


def finish_run(conn, run_id: int, *, status="done", duration_ms=None, valid_ratio=None,
               error=None, payload=None) -> int:
    """收尾一个阶段。`payload` 省略时**不动**已有 payload（避免覆盖阶段中间结果）。"""
    columns = ["status = ?", "ended_at = ?", "duration_ms = ?", "valid_ratio = ?", "error = ?"]
    values = [status, utcnow(), duration_ms, valid_ratio, error]
    if payload is not None:
        columns.append("payload = ?")
        values.append(_dump(payload))
    values.append(run_id)
    cursor = conn.execute(f"UPDATE runs SET {', '.join(columns)} WHERE id = ?", values)
    return cursor.rowcount


def update_run_payload(conn, run_id: int, patch: dict) -> int:
    """合并阶段中间结果（不改变 status，供运行期留痕）。"""
    cursor = conn.execute(
        """UPDATE runs
           SET payload = CASE
                 WHEN json_valid(payload) THEN json_patch(payload, ?)
                 ELSE ?
               END
           WHERE id = ?""",
        (_dump(patch), _dump(patch), run_id),
    )
    return cursor.rowcount


def list_runs(conn, session_id: int) -> list:
    return conn.execute(
        "SELECT * FROM runs WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()


# ---------------------------------------------------------------------- metrics

def add_metric(conn, session_id: int, kind: str, name: str, **fields) -> int:
    cursor = conn.execute(
        """INSERT INTO metrics (session_id, kind, name, t_sec, value, mean, std, n,
                                valid_ratio, quality, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, kind, name, fields.get("t_sec"), fields.get("value"),
         fields.get("mean"), fields.get("std"), fields.get("n"),
         fields.get("valid_ratio"), _dump(fields.get("quality")), utcnow()),
    )
    return int(cursor.lastrowid)


def add_metrics_many(conn, rows: Iterable[tuple]) -> int:
    """rows: (session_id, kind, name, t_sec, value, mean, std, n, valid_ratio, quality)"""
    stamp = utcnow()
    payload = []
    for row in rows:
        values = list(row)
        while len(values) < 10:
            values.append(None)
        values[9] = _dump(values[9])
        payload.append((*values[:10], stamp))
    conn.executemany(
        """INSERT INTO metrics (session_id, kind, name, t_sec, value, mean, std, n,
                                valid_ratio, quality, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        payload,
    )
    return len(payload)


def list_metrics(conn, session_id: int, kind=None, name=None) -> list:
    where, params = ["session_id = ?"], [session_id]
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if name:
        where.append("name = ?")
        params.append(name)
    return conn.execute(
        f"SELECT * FROM metrics WHERE {' AND '.join(where)} ORDER BY id", params
    ).fetchall()


def latest_metric_summary(conn, session_id: int) -> dict:
    rows = conn.execute(
        """SELECT name, mean, std, n FROM metrics
           WHERE session_id = ? AND kind = 'indicator_summary'""",
        (session_id,),
    ).fetchall()
    return {row["name"]: {"mean": row["mean"], "std": row["std"], "n": row["n"]} for row in rows}


# ----------------------------------------------------------------------- alerts

def add_alert(conn, session_id: int, kind: str, state: str, **fields) -> int:
    cursor = conn.execute(
        """INSERT INTO alerts (session_id, kind, state, t_sec, value, sustained_sec,
                               message, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, kind, state, fields.get("t_sec"), fields.get("value"),
         fields.get("sustained_sec"), fields.get("message"), utcnow()),
    )
    return int(cursor.lastrowid)


def list_alerts(conn, session_id: int) -> list:
    return conn.execute(
        "SELECT * FROM alerts WHERE session_id = ? ORDER BY t_sec, id", (session_id,)
    ).fetchall()


# ------------------------------------------------------------------ scale runs

def add_scale_run(conn, session_id: int, *, code: str, version=None, raw_score=None,
                  standard_score=None, level=None, answered=None, missing=None,
                  responses=None, items=None) -> int:
    cursor = conn.execute(
        """INSERT INTO scale_runs (session_id, code, version, raw_score, standard_score,
                                   level, answered, missing, responses, items, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, code, version, raw_score, standard_score, level, answered,
         _dump(missing or []), _dump(responses or []), _dump(items or {}), utcnow()),
    )
    return int(cursor.lastrowid)


def list_scale_runs(conn, session_id: int) -> list:
    return conn.execute(
        "SELECT * FROM scale_runs WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()


def scale_run_for(conn, session_id: int, code: str):
    return conn.execute(
        "SELECT * FROM scale_runs WHERE session_id = ? AND code = ? ORDER BY id DESC LIMIT 1",
        (session_id, code),
    ).fetchone()


# --------------------------------------------------------------- behavior runs

def add_behavior_run(conn, session_id: int, *, task: str, spec=None, metrics=None,
                     trials=None) -> int:
    cursor = conn.execute(
        """INSERT INTO behavior_runs (session_id, task, spec, metrics, trials, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, task, spec, _dump(metrics), _dump(trials), utcnow()),
    )
    return int(cursor.lastrowid)


def list_behavior_runs(conn, session_id: int) -> list:
    return conn.execute(
        "SELECT * FROM behavior_runs WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()


def behavior_run_for(conn, session_id: int, task: str):
    return conn.execute(
        "SELECT * FROM behavior_runs WHERE session_id = ? AND task = ? ORDER BY id DESC LIMIT 1",
        (session_id, task),
    ).fetchone()


# ------------------------------------------------------------ training segments

def add_training_segment(conn, session_id: int, seq: int, *, target=None, mean_score=None,
                         on_target_ratio=None, hold_sec=None, excluded_windows=None,
                         samples=None, started_at=None, ended_at=None) -> int:
    cursor = conn.execute(
        """INSERT INTO training_segments (session_id, seq, target, mean_score, on_target_ratio,
                                          hold_sec, excluded_windows, samples, started_at, ended_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, seq, target, mean_score, on_target_ratio, hold_sec,
         excluded_windows, _dump(samples or []), started_at, ended_at or utcnow()),
    )
    return int(cursor.lastrowid)


def list_training_segments(conn, session_id: int) -> list:
    return conn.execute(
        "SELECT * FROM training_segments WHERE session_id = ? ORDER BY seq", (session_id,)
    ).fetchall()


# ------------------------------------------------------------------- artifacts

def add_artifact(conn, session_id: int, kind: str, path: str, *, size=None, sha256=None) -> int:
    cursor = conn.execute(
        """INSERT INTO artifacts (session_id, kind, path, bytes, sha256, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, kind, str(path), size, sha256, utcnow()),
    )
    return int(cursor.lastrowid)


def list_artifacts(conn, session_id: int) -> list:
    return conn.execute(
        "SELECT * FROM artifacts WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()


def artifact_for(conn, session_id: int, kind: str):
    return conn.execute(
        "SELECT * FROM artifacts WHERE session_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (session_id, kind),
    ).fetchone()


# ---------------------------------------------------------------- history/trend

def session_points(conn, public_id=None, limit=500) -> list:
    """趋势数据源：已完成的会话，按被试返回指标均值。"""
    joins = "JOIN subjects s ON s.id = sessions.subject_id"
    where = ["sessions.status = 'done'"]
    params: list = []
    if public_id:
        where.append("s.public_id = ?")
        params.append(public_id)
    rows = conn.execute(
        f"""SELECT sessions.uuid, sessions.created_at, sessions.device, sessions.srate,
                   sessions.channels, s.public_id AS public_id,
                   m.name AS metric, m.mean AS value
            FROM sessions {joins}
            LEFT JOIN metrics m ON m.session_id = sessions.id AND m.kind = 'indicator_summary'
            WHERE {' AND '.join(where)}
            ORDER BY sessions.id DESC LIMIT ?""",
        params + [limit],
    ).fetchall()
    grouped: dict[str, dict] = {}
    for row in rows:
        item = grouped.setdefault(row["uuid"], {
            "uuid": row["uuid"],
            "public_id": row["public_id"],
            "recorded_at": row["created_at"],
            "device": row["device"],
            "srate": row["srate"],
            "channels": row["channels"],
            "indicators": {},
        })
        if row["metric"]:
            item["indicators"][row["metric"]] = row["value"]
    return list(grouped.values())


def overview(conn) -> dict:
    subjects = conn.execute("SELECT COUNT(*) AS n FROM subjects").fetchone()["n"]
    sessions = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
    done = conn.execute("SELECT COUNT(*) AS n FROM sessions WHERE status = 'done'").fetchone()["n"]
    alerts = conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]
    latest = conn.execute(
        """SELECT sessions.uuid, sessions.created_at, sessions.status, sessions.phase,
                  s.public_id AS public_id
           FROM sessions JOIN subjects s ON s.id = sessions.subject_id
           ORDER BY sessions.id DESC LIMIT 5"""
    ).fetchall()
    return {
        "subjects": int(subjects),
        "sessions": int(sessions),
        "sessions_done": int(done),
        "alerts": int(alerts),
        "latest": [dict(row) for row in latest],
    }


# ------------------------------------------------------------------ ledger 重放

def all_sessions_for_ledger(conn) -> list:
    rows = conn.execute(
        """SELECT sessions.*, s.public_id AS public_id FROM sessions
           JOIN subjects s ON s.id = sessions.subject_id
           WHERE sessions.status = 'done' ORDER BY sessions.id"""
    ).fetchall()
    out = []
    for row in rows:
        summary = latest_metric_summary(conn, row["id"])
        alerts = list_alerts(conn, row["id"])
        out.append({
            "participant": row["public_id"],
            "session": "01",
            "run": f"{row['id']:03d}",
            "device": row["device"],
            "srate": row["srate"],
            "channels": row["channels"],
            "indicators": {name: value["mean"] for name, value in summary.items()},
            "quality": {"valid_ratio": None, "passed": True},
            "alerts": [dict(item) for item in alerts],
            "session_uuid": row["uuid"],
            "recorded_at": row["created_at"],
        })
    return out


def db_path_of(conn) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    return row["file"] if row else ""


__all__ = [
    "store", "read_only", "utcnow",
    "create_subject", "next_public_id", "find_subject", "get_subject_by_pk",
    "list_subjects", "update_subject", "subject_session_counts",
    "create_session", "get_session", "get_session_pk", "list_sessions", "update_session",
    "running_sessions",
    "start_run", "finish_run", "update_run_payload", "list_runs",
    "add_metric", "add_metrics_many", "list_metrics", "latest_metric_summary",
    "add_alert", "list_alerts",
    "add_scale_run", "list_scale_runs", "scale_run_for",
    "add_behavior_run", "list_behavior_runs", "behavior_run_for",
    "add_training_segment", "list_training_segments",
    "add_artifact", "list_artifacts", "artifact_for",
    "session_points", "overview", "all_sessions_for_ledger", "db_path_of",
]
