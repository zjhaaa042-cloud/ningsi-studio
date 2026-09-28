"""全局设置：服务端口、数据目录、运行限制。

所有可调项都可用命令行参数覆盖，命令行优先；不做隐式写盘位置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def default_data_dir() -> Path:
    """默认数据目录：仓库内 var/studio（可用 NINGSI_STUDIO_DATA 覆盖）。"""
    override = os.environ.get("NINGSI_STUDIO_DATA")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "var" / "studio"


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    port_scan: int = 5                 # 端口占用时的递增探测次数
    data_dir: Path = field(default_factory=default_data_dir)
    web_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2] / "web")
    max_active_sessions: int = 2       # 同时运行的会话上限（超出返回 429）
    sse_backlog: int = 200             # 每个会话保留的事件条数（供 Last-Event-ID 重放）
    sse_heartbeat_sec: float = 20.0
    api_token: str = ""                # 非空时校验 X-API-Token
    open_browser: bool = False

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "studio.sqlite3"

    @property
    def runs_root(self) -> Path:
        """每次会话的产物目录（与引擎 CLI 的 var/session 布局保持一致）。"""
        return Path(self.data_dir) / "runs"

    @property
    def log_dir(self) -> Path:
        return Path(self.data_dir) / "logs"

    def ensure_dirs(self) -> None:
        for target in (self.data_dir, self.runs_root, self.log_dir):
            Path(target).mkdir(parents=True, exist_ok=True)
