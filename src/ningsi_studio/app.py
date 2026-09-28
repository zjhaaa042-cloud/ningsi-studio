"""应用装配：建库、建路由、起服务，并提供退出时的清场逻辑。"""

from __future__ import annotations

import logging
import sys
import threading
import webbrowser
from dataclasses import dataclass

from ningsi_studio import bootstrap
from ningsi_studio.api.routes import build_router
from ningsi_studio.core.runtime import SessionManager
from ningsi_studio.db import sqlite_store as store
from ningsi_studio.http.server import AppServer
from ningsi_studio.settings import Settings

LOGGER = logging.getLogger("ningsi_studio.app")


@dataclass
class Application:
    settings: Settings
    server: AppServer
    manager: SessionManager
    db_path: str

    @property
    def url(self) -> str:
        return self.server.url

    def start_background(self) -> None:
        self.server.start_background()

    def serve_forever(self) -> None:
        self.server.serve_forever()

    def stop(self) -> None:
        self.manager.shutdown()
        self.server.stop()

    def open_browser(self) -> None:
        """在后台线程打开浏览器，避免阻塞服务。"""
        def _open() -> None:
            try:
                webbrowser.open(self.url)
            except Exception as exc:  # noqa: BLE001 - 打开失败不影响服务
                LOGGER.warning("打开浏览器失败：%s", exc)

        threading.Thread(target=_open, name="open-browser", daemon=True).start()


def create_app(settings: Settings | None = None) -> Application:
    settings = settings or Settings()
    settings.ensure_dirs()
    db_path = store.initialize(settings.db_path)
    router = build_router(settings)
    server = AppServer(router, settings)
    server.bind()
    return Application(settings=settings, server=server,
                       manager=getattr(router, "manager"), db_path=str(db_path))


def run(settings: Settings | None = None, *, serve: bool = True) -> Application:
    """建应用并启动；`serve=False` 时只装配不监听（测试用）。"""
    app = create_app(settings)
    banner = (
        "\n=== 凝思 Studio（A09 便携脑电专注力训练与心理状态评估）===\n"
        f"  算法引擎    : {bootstrap.ENGINE_PATH}\n"
        f"  界面地址    : {app.url}\n"
        f"  接口地址    : {app.url}api/health\n"
        f"  接口说明    : {app.url}api/openapi.json（中文契约见 docs/API.md）\n"
        f"  设备列表    : {app.url}api/devices（仿真源 + 扫描到的真实 LSL 流）\n"
        f"  数据目录    : {app.settings.data_dir}\n"
        f"  数据库      : {app.db_path}\n"
        "  停止服务    : 按 Ctrl+C，或回到启动器按回车\n"
    )
    print(banner, flush=True)
    if not app.server.static.available():
        print(f"  [!] 前端资源缺失：{app.settings.web_dir}", flush=True)
    if settings and settings.open_browser:
        app.open_browser()
    if serve:
        try:
            app.serve_forever()
        except KeyboardInterrupt:
            print("\n  已停止服务", flush=True)
        finally:
            app.stop()
    return app


def main(argv=None) -> int:
    from ningsi_studio.__main__ import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
