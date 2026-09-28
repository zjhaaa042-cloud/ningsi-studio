"""会话核心层：事件总线按领域语义转出，运行状态机按需导入以避免循环依赖。"""

from ningsi_studio.core import live_source, paired_ledger, phases  # noqa: F401
from ningsi_studio.http.sse import EventBus, encode  # noqa: F401

__all__ = ["phases", "live_source", "paired_ledger", "EventBus", "encode", "runtime"]


def __getattr__(name: str):
    """惰性暴露 SessionManager / SessionRuntime（避免 core ↔ runtime 循环导入）。"""
    if name in {"SessionManager", "SessionRuntime"}:
        from ningsi_studio.core import runtime

        return getattr(runtime, name)
    if name == "runtime":
        from ningsi_studio.core import runtime

        return runtime
    raise AttributeError(name)
