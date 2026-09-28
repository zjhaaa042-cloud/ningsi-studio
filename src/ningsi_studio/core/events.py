"""会话核心：阶段定义、事件总线、运行状态机、台账双写与数据源选择。

事件总线实现只有一个（`ningsi_studio.http.sse.EventBus`），此处按领域语义转出，
避免同一种机制出现两份实现。
"""

from ningsi_studio.core import phases  # noqa: F401
from ningsi_studio.http.sse import EventBus, encode  # noqa: F401

__all__ = ["EventBus", "encode", "phases"]
