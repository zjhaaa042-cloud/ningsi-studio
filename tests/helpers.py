"""测试公共设施：真实服务、HTTP 封装、SSE 读取、等待与数据库核对辅助。

约定：
- 只依赖标准库（unittest / urllib / http.client / sqlite3 / tempfile），不引入第三方测试库；
- 每个测试类在 `setUpClass` 起一个真实 `http.server`，数据目录用临时目录，类结束时关停清理；
- 全部走仿真数据源，无浏览器、无真实设备、无外网也可通过；
- 断言消息一律中文，失败时能直接定位到具体一步。
"""

from __future__ import annotations

import http.client
import json
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:            # 直接以脚本方式运行单文件时兜底
    sys.path.insert(0, str(SRC_DIR))

from ningsi_studio.app import Application, create_app      # noqa: E402
from ningsi_studio.db import repository as repo            # noqa: E402
from ningsi_studio.db import sqlite_store as store         # noqa: E402
from ningsi_studio.http.server import AppServer            # noqa: E402
from ningsi_studio.settings import Settings                # noqa: E402

# `time_scale < 0.2` 时后端进入快速演示模式：量表 / SART / PVT 由服务端自动作答
QUICK_TIME_SCALE = 0.05
# 一次完整快速流程约 60–90 秒，留足超时余量
WAIT_DONE_SECONDS = 180.0
# 阶段顺序（与 ningsi_studio.core.phases.PHASES 一致，接口契约同步）
PHASE_KEYS = (
    "qc", "baseline_open", "baseline_closed", "scales", "sart", "pvt",
    "monitor", "training", "assessment", "model", "report",
)


@dataclass
class ApiResponse:
    """一次 HTTP 调用的结果；非 2xx 也返回该对象，便于断言错误体。"""

    status: int
    headers: dict
    body: bytes = b""
    url: str = ""

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type", "") or ""

    @property
    def content_disposition(self) -> str:
        return self.headers.get("Content-Disposition", "") or ""

    def error_code(self) -> str | None:
        payload = self.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        return error.get("code") if isinstance(error, dict) else None


def _decode_sse_data(lines: list[str]) -> Any:
    """SSE 的 data 段还原：优先按 JSON 解析，失败时退回原始字符串。"""
    if not lines:
        return None
    text = "\n".join(lines)
    try:
        return json.loads(text)
    except ValueError:
        return text


# --------------------------------------------------------------------- 读库辅助

def _session_rows(db_path, session_uuid: str, fetcher: Callable[[Any, int], Any]) -> list[dict]:
    """按会话 uuid 读真实 SQLite（不做任何替换或 mock）。"""
    with store.read_only(db_path) as conn:
        row = repo.get_session(conn, session_uuid)
        if row is None:
            return []
        return [dict(item) for item in fetcher(conn, row["id"])]


def read_runs(db_path, session_uuid: str) -> list[dict]:
    """阶段运行记录。"""
    return _session_rows(db_path, session_uuid, repo.list_runs)


def read_metrics(db_path, session_uuid: str, kind: str | None = None,
                 name: str | None = None) -> list[dict]:
    """指标行（kind=indicator 为逐窗，indicator_summary 为阶段汇总）。"""
    return _session_rows(
        db_path, session_uuid, lambda conn, sid: repo.list_metrics(conn, sid, kind=kind, name=name))


def read_alerts(db_path, session_uuid: str) -> list[dict]:
    """预警事件行。"""
    return _session_rows(db_path, session_uuid, repo.list_alerts)


def read_scale_runs(db_path, session_uuid: str) -> list[dict]:
    """量表作答与计分行。"""
    return _session_rows(db_path, session_uuid, repo.list_scale_runs)


def read_behavior_runs(db_path, session_uuid: str) -> list[dict]:
    """行为任务结果行。"""
    return _session_rows(db_path, session_uuid, repo.list_behavior_runs)


def read_training_segments(db_path, session_uuid: str) -> list[dict]:
    """训练分段行。"""
    return _session_rows(db_path, session_uuid, repo.list_training_segments)


def read_artifacts(db_path, session_uuid: str) -> list[dict]:
    """产物登记行。"""
    return _session_rows(db_path, session_uuid, repo.list_artifacts)


def read_behavior_run(db_path, session_uuid: str, task: str) -> dict:
    """按任务名读取最近一条行为结果行（task 为库内键：sart / pvt）。"""
    with store.read_only(db_path) as conn:
        row = repo.get_session(conn, session_uuid)
        if row is None:
            return {}
        stored = repo.behavior_run_for(conn, row["id"], task)
    return dict(stored) if stored is not None else {}


# --------------------------------------------------------------------- 测试基类

class StudioTestCase(unittest.TestCase):
    """公共基类：起真实服务 + 临时数据目录，并提供请求、断言与等待辅助。"""

    port_base = 18900                # 子类覆盖，避免同类服务端口冲突
    max_active_sessions = 2          # 与 Settings 默认值一致，便于断言 429
    request_timeout = 30.0

    # ------------------------------------------------------------ 服务生命周期
    @classmethod
    def make_settings(cls, data_dir) -> Settings:
        """构造注入式设置：临时数据目录 + 探测到的空闲端口。"""
        return Settings(
            data_dir=Path(data_dir),
            port=AppServer.free_port(cls.port_base),
            max_active_sessions=cls.max_active_sessions,
            open_browser=False,
        )

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp_dir = tempfile.TemporaryDirectory(prefix="ningsi-studio-test-",
                                                   ignore_cleanup_errors=True)
        cls.data_dir = Path(cls._tmp_dir.name)
        cls.app = create_app(cls.make_settings(cls.data_dir))
        cls.app.start_background()
        cls.db_path = cls.app.db_path
        cls.port = cls.app.server.port
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            app = getattr(cls, "app", None)
            if app is not None:
                app.stop()
        finally:
            tmp = getattr(cls, "_tmp_dir", None)
            if tmp is not None:
                tmp.cleanup()

    @classmethod
    def restart_service(cls) -> None:
        """关停当前服务并用**同一 data_dir** 重开（用于持久化验证）。"""
        if getattr(cls, "app", None) is not None:
            cls.app.stop()
        cls.app = create_app(cls.make_settings(cls.data_dir))
        cls.app.start_background()
        cls.db_path = cls.app.db_path
        cls.port = cls.app.server.port
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    def setUp(self) -> None:
        # 清掉上一个用例遗留的运行中会话，避免并发上限影响本用例断言
        self.cancel_active_sessions()

    # ------------------------------------------------------------------ 请求
    def request(self, method: str, path: str, payload: Any = None, *,
                headers: dict | None = None, raw_body: bytes | None = None,
                content_type: str = "application/json; charset=utf-8",
                timeout: float | None = None) -> ApiResponse:
        """发一次 HTTP 请求；非 2xx 不抛异常，统一返回 ApiResponse。"""
        # 查询串里带中文（例如按别名搜索）时必须先做百分号编码，
        # 否则 urllib 会在 putrequest 阶段抛 ascii 编码错误。
        safe_path = urllib.parse.quote(path, safe="/?=&%:,.+-_@[](){}|*$!~'")
        url = safe_path if safe_path.startswith("http") else f"{self.base_url}{safe_path}"
        body = raw_body
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, method=method.upper())
        if body is not None:
            request.add_header("Content-Type", content_type)
        request.add_header("Accept", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, str(value))
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.request_timeout) as response:
                return ApiResponse(response.status, dict(response.headers.items()),
                                   response.read(), url)
        except urllib.error.HTTPError as exc:
            received = dict(exc.headers.items()) if exc.headers else {}
            return ApiResponse(exc.code, received, exc.read(), url)

    def get(self, path: str, **kwargs) -> ApiResponse:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, payload: Any = None, **kwargs) -> ApiResponse:
        return self.request("POST", path, payload=payload, **kwargs)

    def patch(self, path: str, payload: Any = None, **kwargs) -> ApiResponse:
        return self.request("PATCH", path, payload=payload, **kwargs)

    def delete(self, path: str, **kwargs) -> ApiResponse:
        return self.request("DELETE", path, **kwargs)

    # ------------------------------------------------------------------ 断言
    def assert_status(self, response: ApiResponse, expected: int, message: str = "") -> ApiResponse:
        """断言 HTTP 状态码；失败时附带响应体片段。"""
        self.assertEqual(
            response.status, expected,
            f"{message or '接口状态码不符'}：期望 {expected}，实际 {response.status}；"
            f"响应体 {response.text()[:400]}")
        return response

    def json_body(self, response: ApiResponse, expected: int = 200, message: str = "") -> Any:
        """断言状态码并解析 JSON 响应体。"""
        self.assert_status(response, expected, message)
        self.assertTrue(response.body, f"{message}：响应体不应为空")
        return response.json()

    def assert_error(self, response: ApiResponse, expected: int, code: str | None = None,
                     message: str = "") -> dict:
        """断言统一错误体 `{"error": {"code", "message", ...}}`。"""
        self.assert_status(response, expected, message)
        payload = response.json()
        self.assertIsInstance(payload, dict, f"{message}：错误响应应为 JSON 对象")
        self.assertIn("error", payload, f"{message}：错误响应应含 error 字段")
        error = payload["error"]
        self.assertIsInstance(error, dict, f"{message}：error 应为对象")
        self.assertIn("code", error, f"{message}：error.code 缺失")
        self.assertIn("message", error, f"{message}：error.message 缺失")
        if code is not None:
            self.assertEqual(error["code"], code,
                             f"{message}：错误码应为 {code}，实际 {error.get('code')}")
        return error

    # ------------------------------------------------------------------ 领域
    def create_subject(self, **fields) -> dict:
        """通过 API 创建被试（默认自动编号）。"""
        payload = {"auto_id": True, **fields}
        return self.json_body(self.post("/api/subjects", payload), 201, "创建被试应返回 201")

    def create_session(self, participant: str, **fields) -> dict:
        """通过 API 创建会话；返回该会话对象（响应里的 session 字段）。

        默认走快速演示模式（time_scale=0.05，服务端自动作答量表/按键任务）。
        调用方直接取 uuid / status / phase 等字段即可。
        """
        payload = {"participant": participant, "device": "sim-bsense",
                   "time_scale": QUICK_TIME_SCALE, "training_mode": "quick", **fields}
        response = self.json_body(self.post("/api/sessions", payload), 201, "创建会话应返回 201")
        return response["session"]

    def session_detail(self, session_uuid: str) -> dict:
        """读取会话详情。"""
        return self.json_body(self.get(f"/api/sessions/{session_uuid}"), 200,
                              "读取会话详情应返回 200")

    def wait_for(self, fetcher: Callable[[], Any], predicate: Callable[[Any], bool], *,
                 timeout: float = 20.0, interval: float = 0.2,
                 message: str = "条件未在超时内满足") -> Any:
        """轮询等待某个条件成立；超时用 fail 报出最后一次结果。"""
        deadline = time.time() + timeout
        last: Any = None
        while time.time() < deadline:
            last = fetcher()
            if predicate(last):
                return last
            time.sleep(interval)
        self.fail(f"{message}：等待 {timeout}s 仍未满足；最后一次结果 {str(last)[:500]}")

    def wait_for_phase(self, session_uuid: str, phases, timeout: float = 90.0) -> dict:
        """等待会话进入某个阶段（可传单个 key 或集合）。"""
        wanted = {phases} if isinstance(phases, str) else set(phases)
        return self.wait_for(lambda: self.session_detail(session_uuid),
                             lambda item: item.get("phase") in wanted,
                             timeout=timeout, message=f"会话应进入阶段 {sorted(wanted)}")

    def wait_session_done(self, session_uuid: str, timeout: float = WAIT_DONE_SECONDS,
                          expect: tuple = ("done",)) -> dict:
        """轮询会话直到终态；超时则失败并打印 runs 里的 error 便于定位。"""
        deadline = time.time() + timeout
        detail = self.session_detail(session_uuid)
        while time.time() < deadline and detail.get("status") not in ("done", "failed", "cancelled"):
            time.sleep(0.5)
            detail = self.session_detail(session_uuid)
        status = detail.get("status")
        if status not in expect:
            runs = [(item.get("phase"), item.get("status"), item.get("error"))
                    for item in detail.get("runs", [])]
            self.fail(f"会话 {session_uuid} 未在 {timeout}s 内达到 {expect}；"
                      f"当前 status={status} phase={detail.get('phase')} "
                      f"progress={detail.get('progress')}；runs={runs}")
        return detail

    def cancel_active_sessions(self, timeout: float = 10.0) -> None:
        """取消当前全部运行中会话，并等待运行线程退出。"""
        for session_uuid in list(self.app.manager.active_uuids()):
            self.app.manager.cancel(session_uuid)
        deadline = time.time() + timeout
        while time.time() < deadline and self.app.manager.active_uuids():
            time.sleep(0.1)

    def cancel_session_via_api(self, session_uuid: str) -> dict:
        """调用 DELETE 取消会话并返回响应体。"""
        payload = self.json_body(self.delete(f"/api/sessions/{session_uuid}"), 200,
                                 "取消运行中会话应返回 200")
        self.assertTrue(payload.get("cancelled"), "取消响应应含 cancelled=true")
        self.assertEqual(payload.get("uuid"), session_uuid, "取消响应应回显 uuid")
        return payload

    # -------------------------------------------------------------------- SSE
    def open_event_stream(self, session_uuid: str, *, last_event_id: int | None = None,
                          timeout: float = 8.0):
        """建立 SSE 连接；返回 (connection, response)，由调用方负责关闭。"""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        headers = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        connection.request("GET", f"/api/sessions/{session_uuid}/events", headers=headers)
        return connection, connection.getresponse()

    def read_event_stream(self, response, *, stop: Callable[[list], bool] | None = None,
                          count: int | None = None, timeout: float = 20.0,
                          max_events: int = 500) -> list[dict]:
        """读取 SSE 事件：`stop(events)` 为 True 或累计到 `count` 条时停止。

        读取循环带超时上限，避免测试挂死；事件形如 `{"id":?, "type":"phase", "data":{...}}`。
        """
        events: list[dict] = []
        deadline = time.time() + timeout
        current: dict = {"id": None, "type": None}
        data_lines: list[str] = []

        def flush() -> None:
            if current["type"] is None:
                return
            # data 段是完整的事件信封 JSON（{id,type,session,data}），解析后平铺使用，
            # 这样测试可以直接断言 event["session"] / event["data"]["key"]。
            decoded = _decode_sse_data(data_lines)
            if isinstance(decoded, dict):
                events.append(decoded)
            else:
                events.append({"id": current["id"], "type": current["type"], "data": decoded})
            current["id"] = None
            current["type"] = None
            data_lines.clear()

        while len(events) < max_events and time.time() < deadline:
            try:
                raw = response.readline()
            except (socket.timeout, TimeoutError):
                continue
            except Exception:                      # noqa: BLE001 - 连接被服务端关闭
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith(":"):               # 心跳 / ready 注释
                continue
            if line == "":
                flush()
                if count is not None and len(events) >= count:
                    break
                if stop is not None and stop(events):
                    break
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "id":
                try:
                    current["id"] = int(value)
                except ValueError:
                    current["id"] = value
            elif field == "event":
                current["type"] = value
            elif field == "data":
                data_lines.append(value)
        return events

    @staticmethod
    def close_event_stream(connection) -> None:
        """关闭 SSE 连接（服务端会捕获 BrokenPipe/ConnectionReset）。"""
        try:
            connection.close()
        except Exception:                          # noqa: BLE001 - 关闭失败不影响断言
            pass


__all__ = [
    "ApiResponse", "StudioTestCase", "PHASE_KEYS", "QUICK_TIME_SCALE", "WAIT_DONE_SECONDS",
    "read_runs", "read_metrics", "read_alerts", "read_scale_runs", "read_behavior_runs",
    "read_training_segments", "read_artifacts", "read_behavior_run",
]
