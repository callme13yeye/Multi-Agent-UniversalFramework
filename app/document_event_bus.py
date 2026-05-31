"""app/document_event_bus.py — 文档事件总线

用于 SSE 推送：文档状态变更时，通知所有 SSE 订阅者。
"""
import asyncio
import logging
from typing import AsyncGenerator

logger = logging.getLogger(__name__)


class DocumentEventBus:
    """文档事件总线——pub/sub 模式"""

    def __init__(self):
        self._subscribers: dict[int, asyncio.Queue] = {}
        self._counter = 0

    def subscribe(self) -> int:
        """订阅事件，返回 subscriber_id"""
        self._counter += 1
        self._subscribers[self._counter] = asyncio.Queue()
        return self._counter

    def unsubscribe(self, subscriber_id: int):
        self._subscribers.pop(subscriber_id, None)

    async def publish(self, doc_id: int, status: str, user_id: int):
        """发布事件到所有订阅者"""
        event = {"doc_id": doc_id, "status": status, "user_id": user_id}
        for sub_id, queue in list(self._subscribers.items()):
            await queue.put(event)

    async def events(self, subscriber_id: int) -> AsyncGenerator[dict, None]:
        """获取订阅者的事件流（含 30s 心跳）"""
        queue = self._subscribers.get(subscriber_id)
        if not queue:
            return
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield event
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat"}
        except asyncio.CancelledError:
            pass
        finally:
            self.unsubscribe(subscriber_id)


document_event_bus = DocumentEventBus()
