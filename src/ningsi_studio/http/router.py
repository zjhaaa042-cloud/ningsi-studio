"""极简路由：正则匹配 + JSON 响应封装 + 统一错误体。

不引入 Web 框架是刻意选择：让"一条命令即可演示"成为硬约束（零安装摩擦）。
接口按 REST 设计，后续若要迁移到 FastAPI，只需替换本文件与 server.py。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

JSON_TYPE = "application/json; charset=utf-8"

# 路由模板占位符：{uuid} → (?P<uuid>[^/]+)
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


# ----------------------------------------------------------------- 异常与错误体

class ApiError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str, *, detail: Any = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        if status is not None:
            self.status = status

    def as_body(self) -> dict:
        body: dict = {"error": {"code": self.code, "message": self.message}}
        if self.detail is not None:
            body["error"]["detail"] = self.detail
        return body


class BadRequest(ApiError):
    status, code = 400, "bad_request"


class Unauthorized(ApiError):
    status, code = 401, "unauthorized"


class NotFound(ApiError):
    status, code = 404, "not_found"


class Conflict(ApiError):
    status, code = 409, "conflict"


class ValidationError(ApiError):
    status, code = 422, "validation_failed"


class TooManyRequests(ApiError):
    status, code = 429, "too_many_requests"


class ServerError(ApiError):
    status, code = 500, "internal_error"


# --------------------------------------------------------------------- 数据封装

def jsonable(value: Any) -> Any:
    """把 numpy / Path / Row / 集合递归转成可 JSON 序列化的值。"""
    import sqlite3
    from pathlib import Path

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None  # NaN/Inf -> null
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, sqlite3.Row):
        return {key: jsonable(value[key]) for key in value.keys()}
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    # numpy 标量与数组
    item_method = getattr(value, "item", None)
    if callable(item_method) and getattr(value, "shape", None) == ():
        return jsonable(item_method())
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return jsonable(tolist())
    return str(value)


def dump(payload: Any) -> bytes:
    return json.dumps(jsonable(payload), ensure_ascii=False).encode("utf-8")


@dataclass
class Request:
    method: str
    path: str
    query: dict
    headers: dict
    body: bytes = b""
    params: dict = field(default_factory=dict)
    remote: str = ""

    def json(self, default=None) -> Any:
        if not self.body:
            return default if default is not None else {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadRequest(f"请求体不是合法 JSON：{exc}") from exc

    def query_int(self, key: str, default: int, *, low=1, high=1000) -> int:
        raw = self.query.get(key)
        if raw in (None, ""):
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValidationError(f"参数 {key} 必须是整数", detail=raw) from exc
        return max(low, min(high, value))

    def query_float(self, key: str, default: float) -> float:
        raw = self.query.get(key)
        if raw in (None, ""):
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise ValidationError(f"参数 {key} 必须是数字", detail=raw) from exc


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = JSON_TYPE
    headers: dict = field(default_factory=dict)

    @classmethod
    def json(cls, payload, status: int = 200, headers: dict | None = None) -> "Response":
        return cls(status=status, body=dump(payload), content_type=JSON_TYPE,
                   headers=dict(headers or {}))


# ------------------------------------------------------------------------ 路由表

Handler = Callable[[Request], Response]


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern, Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        """注册路由：把 `{name}` 占位符编译成命名分组，供 dispatch 填入 `request.params`。

        `[^/]+` 而不是 `.*`：路径参数不应跨越 `/`，否则 `/api/sessions/{uuid}/events`
        会被 `/api/sessions/{uuid}` 抢先匹配到。
        """
        regex = re.compile("^" + PLACEHOLDER.sub(r"(?P<\1>[^/]+)", pattern) + "$")
        self._routes.append((method.upper(), regex, handler))

    def route(self, method: str, pattern: str):
        def decorator(func: Handler) -> Handler:
            self.add(method, pattern, func)
            return func
        return decorator

    def get(self, pattern: str):
        return self.route("GET", pattern)

    def post(self, pattern: str):
        return self.route("POST", pattern)

    def patch(self, pattern: str):
        return self.route("PATCH", pattern)

    def delete(self, pattern: str):
        return self.route("DELETE", pattern)

    def dispatch(self, request: Request) -> Response:
        path = request.path.rstrip("/") or "/"
        allowed: set[str] = set()
        for method, regex, handler in self._routes:
            match = regex.match(path)
            if not match:
                continue
            allowed.add(method)
            if method == request.method:
                request.params = {key: value for key, value in match.groupdict().items()}
                return handler(request)
        if allowed:
            raise ApiError(
                f"{request.method} 不被支持", status=405, detail={"allow": sorted(allowed)}
            )
        raise NotFound(f"没有该接口：{request.method} {path}")

    def patterns(self) -> Iterable[str]:
        for method, regex, _ in self._routes:
            yield f"{method} {regex.pattern}"
