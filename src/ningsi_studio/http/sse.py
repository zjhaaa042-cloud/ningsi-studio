"""SSE（Server-Sent Events）编码与订阅总线。

- 每个会话一个 EventBus：`publish()` 追加事件并推给所有订阅队列；
- 保留最近 `backlog` 条事件，支持浏览器带 `Last-Event-ID` 重连时重放；
- 队列满时丢弃最旧事件（宁可丢帧也不能把运行线程卡住）；
- 事件 id 为自增整数，前端据此续订。
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass, field
from typing import Any

from ningsi_studio.settings import Settings


def encode(event: dict) -> bytes:
    """把事件字典编码为一段 SSE 消息。"""
    payload = json.dumps(event, ensure_ascii=False)
    event_id = event.get("id")
    name = event.get("type", "message")
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {name}")
    for line in payload.splitlines() or [""]:
        lines.append(f"data: {line}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def comment(text: str = "ping") -> bytes:
    return f": {text}\n\n".encode("utf-8")


@dataclass(eq=False)                    # eq=False 保留对象身份哈希，才能放进订阅者集合
class _Subscriber:
    events: "queue.Queue[dict]" = field(default_factory=lambda: queue.Queue(maxsize=512))

    def put(self, event: dict) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            try:
                self.events.put_nowait(event)
            except queue.Full:
                pass


class EventBus:
    def __init__(self, session_uuid: str, backlog: int | None = None) -> None:
        settings = Settings()
        self.session_uuid = session_uuid
        self.backlog_size = int(backlog if backlog is not None else settings.sse_backlog)
        self._lock = threading.Lock()
        self._counter = 0
        self._backlog: list[dict] = []
        self._subscribers: set[_Subscriber] = set()
        self.closed = False

    # ---------------------------------------------------------------- 发布
    def publish(self, event_type: str, payload: Any = None, **extra) -> dict:
        """发布事件：追加到 backlog 并推给所有订阅队列。"""
        with self._lock:
            if self.closed:
                return {}
            self._counter += 1
            event = {"id": self._counter, "type": event_type, "session": self.session_uuid}
            if payload is not None:
                event["data"] = payload
            if extra:
                event.update(extra)
            self._backlog.append(event)
            if len(self._backlog) > self.backlog_size:
                self._backlog = self._backlog[-self.backlog_size:]
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)
        return event

    # ---------------------------------------------------------------- 订阅
    def subscribe(self, last_event_id: int | None = None) -> tuple[_Subscriber, int]:
        """订阅并返回 `(订阅者, 队列中已推入的补发条数)`。

        补发事件**先写入订阅者队列再返回**，因此不存在"快照与实时流乱序"；
        已关闭的总线会立刻收到一条 `closed`，避免长连接永久挂住线程。
        """
        with self._lock:
            if last_event_id is None:
                # 新订阅：只补"快照类"事件（阶段与最终状态），避免重放整段历史
                replay = [event for event in self._backlog
                          if event["type"] in ("phase", "finished")]
            else:
                replay = [event for event in self._backlog if event["id"] > last_event_id]
            subscriber = _Subscriber()
            self._subscribers.add(subscriber)
            closed = self.closed
            counter = self._counter
        for event in replay:
            subscriber.put(event)
        if closed:
            subscriber.put({"id": counter, "type": "closed", "session": self.session_uuid})
        return subscriber, len(replay)

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def close(self) -> None:
        with self._lock:
            self.closed = True
            subscribers = list(self._subscribers)
            self._subscribers.clear()
            counter = self._counter
        for subscriber in subscribers:
            subscriber.put({"id": counter, "type": "closed", "session": self.session_uuid})

    # ---------------------------------------------------------------- 辅助
    def last_id(self) -> int:
        with self._lock:
            return self._counter

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._backlog)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


def events_after(counter: int, last_event_id: int) -> bool:
    """客户端持有的 id 是否落后于服务端（用于判断是否需要重放）。"""
    return last_event_id < counter
