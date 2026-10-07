"""SQLite 连接与迁移：WAL 模式、忙等待、幂等建表。

设计要点：
- 每个请求/线程用短连接（`connect()` 是上下文管理器），避免 sqlite3 的跨线程限制；
- WAL + busy_timeout 让"运行线程写、HTTP 线程读"能并行；
- `schema.sql` 为权威 DDL，`schema_version` 表记录已应用的版本号，重复执行安全。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "0001-init"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # journal_mode=WAL 是持久化设置：建库时设置一次即可。
    # 每个连接都执行 PRAGMA journal_mode 会尝试拿一次排它锁，
    # 在"运行线程写 + HTTP 线程读"的并发下正是 database is locked 的常见来源。
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _migrate(conn) -> None:
    """幂等列迁移。

    `schema.sql` 用 `CREATE TABLE IF NOT EXISTS`，所以**给已有库加列必须显式 ALTER**，
    否则老库（用户已经存了几百条会话）会缺列、查询直接报 no such column。
    目前只有一条：
    - `sessions.protocol`（'full' | 'short'）：短协议（去掉训练与模型、SART/PVT 减半）需要按会话记住，
      运行线程、报告与界面都要读它。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "protocol" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN protocol TEXT NOT NULL DEFAULT 'full'")


def initialize(path) -> Path:
    """建库建表（幂等），返回数据库路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(target)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        _migrate(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utcnow()),
        )
    finally:
        conn.close()
    return target


@contextmanager
def connect(path, *, retries: int = 5, base_delay: float = 0.05):
    """短连接 + 写事务。

    两个细节是为并发场景专门加的：
    - `BEGIN IMMEDIATE` 一开始就拿写锁，避免"先读后升级"造成的 SQLITE_BUSY 死锁；
    - 遇到 database is locked 自动退避重试（最多 `retries` 次）。
      事务在失败时会完整回滚，因此重试不会产生重复写入。
    """
    attempt = 0
    while True:
        attempt += 1
        conn = _connect(Path(path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
            return
        except sqlite3.OperationalError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if "locked" not in str(exc).lower() or attempt >= retries:
                raise
            time.sleep(base_delay * attempt)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


@contextmanager
def read_only(path):
    """只读场景：不开显式事务，交给 SQLite 自动提交。"""
    conn = _connect(Path(path))
    try:
        yield conn
    finally:
        conn.close()


def schema_versions(path) -> list[str]:
    with read_only(path) as conn:
        rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    return [row["version"] for row in rows]
