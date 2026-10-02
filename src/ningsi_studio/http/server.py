"""HTTP 服务：把 Router + 静态资源 + SSE 组装成一个可运行的 ThreadingHTTPServer。

同一端口同时提供：
- `/api/*`  → JSON 接口（Router）
- `/api/sessions/{id}/events` → SSE 长连接
- 其余路径 → `web/` 下的静态前端
"""

from __future__ import annotations

import json
import logging
import queue as queue_module
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ningsi_studio.http import sse
from ningsi_studio.http.router import (ApiError, Request, Response, Router, Unauthorized,
                                       jsonable)
from ningsi_studio.http.static_handler import StaticHandler
from ningsi_studio.settings import Settings

LOGGER = logging.getLogger("ningsi_studio.http")

SSE_TYPE = "text/event-stream; charset=utf-8"
NO_CACHE = "no-store, no-cache, must-revalidate"


class StudioRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ningsi-studio"
    sys_version = ""

    # 由 AppServer 注入
    router: Router = None            # type: ignore[assignment]
    static: StaticHandler = None     # type: ignore[assignment]
    settings: Settings = None        # type: ignore[assignment]

    # ------------------------------------------------------------- 日志
    def log_message(self, fmt: str, *args) -> None:
        stream = getattr(self.server, "log_stream", sys.stderr)
        if stream is None:
            return
        try:
            stream.write("%s - %s\n" % (self.address_string(), fmt % args))
        except Exception:  # 日志不能影响服务
            pass

    def log_error(self, fmt: str, *args) -> None:
        self.log_message(fmt, *args)

    def handle(self) -> None:
        """屏蔽"客户端提前断开"导致的 traceback 噪声。

        浏览器刷新页面、切换路由、关闭标签页时会直接掐断 TCP 连接，
        socketserver 默认会把 `ConnectionAbortedError/ConnectionResetError`
        连同完整 traceback 打到控制台——一次页面操作能刷十几行，把真正的信息埋掉。
        这里视为正常现象，只留一条 DEBUG 日志。
        """
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as exc:
            LOGGER.debug("客户端提前断开连接：%s", exc)
            self.close_connection = True
        except OSError as exc:                        # 例如 Windows 上的 WinError 10053/10054
            if getattr(exc, "winerror", None) in (10038, 10053, 10054):
                LOGGER.debug("连接被中断：%s", exc)
                self.close_connection = True
                return
            raise

    # ------------------------------------------------------------- 方法
    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_PUT(self) -> None:
        # 路由表没有 PUT 接口；交给 dispatch 统一返回 405 并带上允许的方法
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._common_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Token, Last-Event-ID")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------- 核心
    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            size = int(length)
        except ValueError:
            return b""
        return self.rfile.read(size) if size > 0 else b""

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        body = self._read_body() if method in ("POST", "PATCH", "PUT") else b""
        request = Request(
            method=method,
            path=path.rstrip("/") or "/",
            query=query,
            headers={key.lower(): value for key, value in self.headers.items()},
            body=body,
            remote=self.client_address[0] if self.client_address else "",
        )

        try:
            if path == "/api" or path.startswith("/api/"):
                # token 只守接口，不拦静态资源：`--token` 的 help 与 Settings.api_token 的
                # 注释都只承诺"所有 /api 请求需带令牌"，且静态页面本身不含数据。
                # 校验放在 /api 分支内（而不是 _handle 开头）是必须的：否则带 --token 启动时
                # GET /index.html 会 401，前端拿不到 SPA 入口，页面永远 boot 不起来。
                self._check_token(request)
                response = self.router.dispatch(request)
                if getattr(response, "stream", False):
                    return self.send_event_stream(
                        getattr(response, "preload", 0),
                        getattr(response, "queue", None),
                        getattr(response, "heartbeat_sec", 20.0),
                        getattr(response, "keep_alive", None),
                        getattr(response, "on_close", None),
                    )
                if method == "HEAD" or self.command == "HEAD":
                    return self._send(Response(status=response.status, body=b"",
                                               content_type=response.content_type,
                                               headers=dict(response.headers)))
                self._send(response)
                return
            self._serve_static(path)
        except ApiError as exc:
            self._send(Response.json(exc.as_body(), status=exc.status))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as exc:
            # 浏览器关页面/刷新/切路由时会直接掐断连接（SSE 尤其常见）。
            # 这是正常现象，不该在控制台刷一大段 traceback 把真正的信息埋掉。
            LOGGER.debug("客户端提前断开连接：%s", exc)
            self.close_connection = True
        except Exception as exc:                      # noqa: BLE001 - 兜底，服务不能崩
            self.log_error("unhandled error: %r", exc)
            try:
                self._send(Response.json(
                    {"error": {"code": "internal_error", "message": str(exc)}}, status=500))
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                self.close_connection = True

    def _serve_static(self, path: str) -> None:
        if not self.static.available():
            self._send(Response.json(
                {"error": {"code": "frontend_missing",
                           "message": f"前端资源缺失：{self.static.web_dir}",
                           "detail": "请确认仓库内存在 web/index.html"}}, status=500))
            return
        target = self.static.resolve(path.rstrip("/"))
        if target is None:
            # 单页应用回退：非静态资源路径交给前端路由
            fallback = self.static.resolve("/index.html")
            if fallback is None or path.startswith("/api"):
                self._send(Response.json(
                    {"error": {"code": "not_found", "message": f"资源不存在：{path}"}}, status=404))
                return
            target = fallback
        # 前端脚本可能还在迭代：禁止缓存，保证普通刷新就能拿到最新 JS/CSS
        # （这类本地演示工具没有配缓存指纹，缓存带来的"改了没生效"比省流量更糟）
        cache = "no-store"
        self._send(Response(status=200, body=self.static.load(target),
                            content_type=self.static.content_type(target),
                            headers={"Cache-Control": cache}))

    def _check_token(self, request: Request) -> None:
        token = self.settings.api_token
        if not token:
            return
        provided = request.headers.get("x-api-token") or request.query.get("token")
        if provided != token:
            # 用 Unauthorized（401/unauthorized）而不是裸 ApiError：后者的默认 code 是
            # internal_error，前端会把"令牌错误"当成服务端故障，与 docs/API.md 的
            # 错误体约定（401 → unauthorized）也不一致。
            raise Unauthorized("缺少或错误的 API Token")

    def _common_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        self._common_headers()
        self.send_header("Content-Type", response.content_type)
        self.send_header("Cache-Control", response.headers.get("Cache-Control", NO_CACHE))
        for key, value in response.headers.items():
            if key.lower() in ("cache-control", "content-type"):
                continue
            self.send_header(key, str(value))
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if self.command != "HEAD" and response.body:
            self.wfile.write(response.body)

    # ------------------------------------------------------------- SSE
    def send_event_stream(self, preload: int, queue, heartbeat_sec: float, keep_alive=None,
                          on_close=None) -> None:
        """SSE 长连接：补发事件已在订阅时写入队列，这里只管按序写出并按时发心跳。"""
        if queue is None:
            self._send(Response.json(
                {"error": {"code": "internal_error", "message": "SSE 订阅未正确初始化"}},
                status=500))
            return
        self.send_response(200)
        self._common_headers()
        self.send_header("Content-Type", SSE_TYPE)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            if preload:
                self.wfile.write(sse.comment(f"replay:{preload}"))
            self.wfile.write(sse.comment("ready"))
            self.wfile.flush()
            while True:
                if keep_alive is not None and not keep_alive():
                    break
                try:
                    event = queue.get(timeout=heartbeat_sec)
                except queue_module.Empty:
                    self.wfile.write(sse.comment("hb"))
                    self.wfile.flush()
                    continue
                # 事件负载必须可 JSON 序列化；单条失败不能污染整条流
                try:
                    chunk = sse.encode(event)
                except (TypeError, ValueError):
                    self.log_error("skip unserializable SSE event: %r", event.get("type"))
                    continue
                self.wfile.write(chunk)
                self.wfile.flush()
                if event.get("type") == "closed":
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as exc:                       # noqa: BLE001 - 响应已发出，不能再发错误体
            self.log_error("sse stream error: %r", exc)
        finally:
            try:
                self.wfile.flush()
            except Exception:
                pass
            # 流真正结束时才释放订阅资源（高频信号通道靠这个停掉后台推帧线程）
            if on_close is not None:
                try:
                    on_close()
                except Exception as exc:               # noqa: BLE001
                    self.log_error("sse on_close error: %r", exc)


class AppServer:
    """封装 ThreadingHTTPServer：支持端口探测、优雅关闭与后台线程运行。"""

    def __init__(self, router: Router, settings: Settings, *, log_stream=None) -> None:
        self.router = router
        self.settings = settings
        self.static = StaticHandler(settings.web_dir)
        self.log_stream = log_stream
        self.httpd: ThreadingHTTPServer | None = None
        self.port = settings.port
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 生命周期
    def _make_handler_class(self):
        router, static, settings, log_stream = self.router, self.static, self.settings, self.log_stream

        class BoundHandler(StudioRequestHandler):
            pass

        BoundHandler.router = router
        BoundHandler.static = static
        BoundHandler.settings = settings
        return BoundHandler

    def bind(self) -> int:
        handler_class = self._make_handler_class()
        last_error: Exception | None = None
        for offset in range(max(1, self.settings.port_scan)):
            port = self.settings.port + offset
            try:
                self.httpd = ThreadingHTTPServer((self.settings.host, port), handler_class)
                self.httpd.daemon_threads = True
                self.httpd.log_stream = self.log_stream
                self.port = port
                return port
            except OSError as exc:
                last_error = exc
                continue
        raise RuntimeError(
            f"端口 {self.settings.port}–{self.settings.port + self.settings.port_scan - 1} 均不可用：{last_error}"
        )

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.settings.host in ("0.0.0.0", "") else self.settings.host
        return f"http://{host}:{self.port}/"

    def serve_forever(self) -> None:
        if self.httpd is None:
            self.bind()
        assert self.httpd is not None
        self.httpd.serve_forever(poll_interval=0.2)

    def start_background(self) -> None:
        if self.httpd is None:
            self.bind()
        self._thread = threading.Thread(target=self.serve_forever, name="ningsi-studio-http",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # --------------------------------------------------------------- 工具
    @staticmethod
    def free_port(preferred: int, span: int = 5) -> int:
        for offset in range(max(1, span)):
            port = preferred + offset
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise RuntimeError(f"{preferred} 起 {span} 个端口都被占用")


def sse_response(preload: int, queue, *, heartbeat_sec: float, keep_alive=None,
                 on_close=None) -> Response:
    """把 SSE 连接打包成带 stream 标记的 Response；补发事件已在订阅时进入队列。

    `on_close` 会在**流真正结束**（客户端断开 / keep_alive 转假）时调用一次，
    用于释放订阅资源。注意不能放在路由函数的 finally 里——路由返回响应时流还没开始，
    那时清理会把刚建立的订阅立刻拆掉（真实踩过的坑：连接建立却一帧都收不到）。
    """
    response = Response(status=200, body=b"", content_type=SSE_TYPE)
    response.stream = True          # type: ignore[attr-defined]
    response.preload = int(preload)  # type: ignore[attr-defined]
    response.queue = queue                    # type: ignore[attr-defined]
    response.heartbeat_sec = heartbeat_sec    # type: ignore[attr-defined]
    response.keep_alive = keep_alive          # type: ignore[attr-defined]
    response.on_close = on_close              # type: ignore[attr-defined]
    return response


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def jsonable_or_str(value) -> str:
    return json.dumps(jsonable(value), ensure_ascii=False)
