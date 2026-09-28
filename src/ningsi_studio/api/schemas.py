"""入参校验与行对象转换：所有外部输入在这里收敛成明确类型。"""

from __future__ import annotations

import re
from typing import Any

from ningsi_studio.http.router import ValidationError

PUBLIC_ID_RE = re.compile(r"^(sub-)?[A-Za-z]{0,3}\d{1,4}$")
UUID_RE = re.compile(r"^[0-9a-f]{32}$")


def as_dict(row) -> dict:
    """sqlite3.Row -> dict。"""
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows) -> list[dict]:
    return [as_dict(row) for row in rows or []]


def require_fields(payload: dict, *fields: str) -> None:
    missing = [field for field in fields if payload.get(field) in (None, "", [])]
    if missing:
        raise ValidationError(f"缺少必填字段：{', '.join(missing)}", detail={"missing": missing})


def normalize_public_id(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValidationError("被试编号不能为空")
    if text.startswith("sub-"):
        text = text[4:]
    if not PUBLIC_ID_RE.match(text):
        raise ValidationError("被试编号格式应为 p01 / sub-p01（字母 + 数字，最长 8 位）", detail=text)
    return text


def ensure_uuid(value: str) -> str:
    if not value or not UUID_RE.match(value):
        raise ValidationError("会话 id 必须是 32 位十六进制字符串", detail=value)
    return value


def clamp_float(value, default: float, *, low: float, high: float, name: str) -> float:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} 必须是数字", detail=value) from exc
    if number < low or number > high:
        raise ValidationError(f"{name} 应在 [{low}, {high}] 之间", detail=value)
    return number


def clamp_int(value, default: int, *, low: int, high: int, name: str) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} 必须是整数", detail=value) from exc
    if number < low or number > high:
        raise ValidationError(f"{name} 应在 [{low}, {high}] 之间", detail=value)
    return number


def optional_text(value, *, max_len: int = 200, name: str = "字段") -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if len(text) > max_len:
        raise ValidationError(f"{name} 过长（上限 {max_len} 字）")
    return text


def as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def pagination(request) -> tuple[int, int]:
    limit = request.query_int("limit", 50, low=1, high=200)
    page = max(1, request.query_int("page", 1, low=1, high=100000))
    return limit, (page - 1) * limit


def typed(payload: Any) -> dict:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    return payload
