"""全局设置：服务端口、数据目录、运行限制。

所有可调项都可用命令行参数覆盖，命令行优先；不做隐式写盘位置。

打包（PyInstaller）说明：打包后 `__file__` 落在解包目录 `sys._MEIPASS` 里，而且 onefile
模式每次运行都换一个临时目录，因此
  - **只读资源**（`web/` 前端）从 `_MEIPASS` 读；
  - **可写数据**（SQLite、报告、日志）优先放在 exe 同级的 `var/studio`（便携版），
    该目录不可写时退到 `%LOCALAPPDATA%\\ningsi-studio\\var\\studio`。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: 打包后的可执行文件名（用于提示文案与退路目录名）
APP_DIR_NAME = "ningsi-studio"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打好的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path | None:
    """打包解包目录（onefile 下是临时目录）；源码运行时返回 None。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def executable_dir() -> Path:
    """exe 所在目录；源码运行时是 python 解释器目录（调用方需先判断 is_frozen）。"""
    return Path(sys.executable).resolve().parent


def source_root() -> Path:
    """源码仓库根目录（settings.py -> ningsi_studio/ -> src/ -> 仓库根）。"""
    return Path(__file__).resolve().parents[2]


def _writable(path: Path) -> bool:
    """真的写一个文件来判断可写：只看 os.access 在 Windows 上会漏掉 ACL 拒绝。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".ningsi-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def fallback_data_dir() -> Path:
    """用户级退路数据目录（exe 同级不可写时使用）。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_DIR_NAME / "var" / "studio"


def default_data_dir() -> Path:
    """默认数据目录：源码运行是仓库内 `var/studio`；打包后是可写的用户目录。

    可用环境变量 `NINGSI_STUDIO_DATA` 或命令行 `--data` 覆盖（优先级最高）。
    """
    override = os.environ.get("NINGSI_STUDIO_DATA")
    if override:
        return Path(override).expanduser()
    if not is_frozen():
        return source_root() / "var" / "studio"
    portable = executable_dir() / "var" / "studio"
    if _writable(portable):
        return portable
    fallback = fallback_data_dir()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def default_web_dir() -> Path:
    """默认前端目录：打包后取包内 `web/`，源码运行取仓库 `web/`。"""
    if is_frozen():
        bundle = bundle_dir()
        if bundle is not None:
            return bundle / "web"
    return source_root() / "web"


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    port_scan: int = 5                 # 端口占用时的递增探测次数
    data_dir: Path = field(default_factory=default_data_dir)
    web_dir: Path = field(default_factory=default_web_dir)
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
