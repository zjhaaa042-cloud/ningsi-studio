"""JSONL 审计台账的双写。

权威数据在 SQLite；台账是给评审与人工审计用的逐行副本，与上游
`ningsi.monitoring.history` / `ningsi.scales.store` 的格式保持一致，因此
`ningsi demo` 产出的记录与 Studio 的记录可以被同一套趋势逻辑读取。

写台账失败只记警告，不阻断会话（台账是可重建的副本）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ningsi.monitoring import history as history_module
from ningsi.scales import store as scale_store

LOGGER = logging.getLogger("ningsi_studio.ledger")


def append_history_record(root, record: dict) -> Path | None:
    try:
        return history_module.append_record(root, record)
    except Exception as exc:  # noqa: BLE001 - 台账失败不阻断
        LOGGER.warning("写历史台账失败：%s", exc)
        return None


def append_scale_record(root, participant: str, session: str, run: str, result,
                        responses=None) -> Path | None:
    try:
        return scale_store.save_scale_run(root, participant, session, run, result, responses,
                                          scale_code=getattr(result, "code", None))
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("写量表台账失败：%s", exc)
        return None


def read_history(root) -> list:
    try:
        path = Path(root) / "history" / "sessions.jsonl"
        return history_module.load_records(path) if path.exists() else []
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("读历史台账失败：%s", exc)
        return []


def rebuild_from_records(root, records) -> Path:
    """从库中记录重放台账（用于 --rebuild-ledger 修复审计副本）。"""
    target = Path(root) / "history" / "sessions.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return target


def trend_points(root, field: str = "focus", period: str = "week") -> list:
    records = read_history(root)
    if not records:
        return []
    try:
        device = records[-1].get("device")
        srate = records[-1].get("srate")
        channels = records[-1].get("channels")
        usable, rejected = history_module.comparable(records, device, srate, channels)
        points = history_module.aggregate(usable, field, period)
        for point in points:
            point["rejected"] = len(rejected)
        return points
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("趋势聚合失败：%s", exc)
        return []
