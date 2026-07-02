"""事件总线 — Agent 间异步通信基础设施。

当前使用 Redis PubSub 作为跨进程传输层，本地 handler 作为进程内快速通道。
如果 Redis 不可用，降级为纯内存模式（单进程内可用）。

事件类型约定：
    task.{status}     — 任务状态变更 (created/executing/waiting_human/completed/failed)
    workflow.{event}  — 工作流事件 (step_completed/approved/rejected)
    agent.{event}     — Agent 内部事件
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None] | None]


class EventBus:
    """轻量事件总线 — 解耦 Agent / Workflow / Harness 之间的通信。

    使用方式::

        bus = EventBus(redis_client=redis)

        # 注册处理器
        @bus.on("task.completed")
        async def on_task_done(data):
            await notify_user(data["task_id"])

        # 发布事件
        await bus.publish("task.completed", {"task_id": "task-abc"})
    """

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._handlers: dict[str, list[Handler]] = {}          # 精确匹配: "task.created"
        self._pattern_handlers: dict[str, list[Handler]] = {}  # 前缀匹配: "task."
        self._redis_available = redis_client is not None

    # ── 注册 ──────────────────────────────────────────────

    def on(self, event_type: str):
        """装饰器：注册事件处理器。

        .. code-block:: python

            @bus.on("task.completed")
            async def handle(data):
                print(f"任务完成: {data['task_id']}")
        """
        def decorator(handler: Handler) -> Handler:
            self.subscribe(event_type, handler)
            return handler
        return decorator

    def subscribe(self, event_type: str, handler: Handler) -> Callable[[], None]:
        """注册事件处理器（非装饰器形式）。

        Returns:
            取消订阅的可调用对象。调用后 handler 从该事件类型中移除。
            用于 SSE 端点等长连接场景的清理：::

                unsub = bus.subscribe("task.completed", my_handler)
                try:
                    ...
                finally:
                    unsub()
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        logger.debug("[EventBus] 注册处理器: %s → %s", event_type, handler.__name__)

        # ── 返回取消订阅的闭包 ──
        def unsubscribe():
            try:
                self._handlers.get(event_type, []).remove(handler)
                logger.debug("[EventBus] 已取消注册: %s → %s", event_type, handler.__name__)
            except ValueError:
                pass  # handler 可能已被移除

        return unsubscribe

    # ── 发布 ──────────────────────────────────────────────

    async def publish(self, event_type: str, data: dict[str, Any]):
        """发布事件到所有订阅者。

        - 本地处理器立即执行（同一 event loop）
        - Redis 发布用于跨进程通知
        - 自动注入 trace_id（如果 data 中不存在）
        """
        # ── 自动注入 trace_id ──
        if "trace_id" not in data:
            from app.harness.trace_context import _trace_id
            tid = _trace_id.get()
            if tid:
                data["trace_id"] = tid

        payload = {
            "type": event_type,
            "data": data,
        }

        # 本地处理器并发执行（精确匹配 + 前缀通配符匹配）
        handlers = list(self._handlers.get(event_type, []))
        for prefix, pat_handlers in self._pattern_handlers.items():
            if event_type.startswith(prefix):
                handlers.extend(pat_handlers)
        if handlers:
            tasks = []
            for handler in handlers:
                try:
                    result = handler(data)
                    if asyncio.iscoroutine(result):
                        tasks.append(result)
                except Exception:
                    logger.exception("[EventBus] 处理器 %s 异常", handler.__name__)

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, r in enumerate(results):
                    if isinstance(r, Exception):
                        logger.error("[EventBus] 异步处理器异常: %s", r)

        # Redis PubSub（跨进程/跨服务）
        if self._redis_available:
            try:
                await self._redis.publish(
                    f"agentrag:event:{event_type}",
                    json.dumps(payload, ensure_ascii=False, default=str),
                )
            except Exception:
                logger.warning("[EventBus] Redis 发布失败，降级为纯内存模式")
                self._redis_available = False

    # ── 通配符 ────────────────────────────────────────────

    def subscribe_pattern(self, pattern: str, handler: Handler) -> Callable[[], None]:
        """注册通配符处理器。pattern 如 'task.*' 匹配所有 task 事件。

        注意：当前实现仅在本地匹配，不依赖 Redis 的 PSUBSCRIBE。

        Returns:
            取消订阅的可调用对象。
        """
        if pattern.endswith("*"):
            prefix = pattern[:-1]  # "task.*" → "task."
            if prefix not in self._pattern_handlers:
                self._pattern_handlers[prefix] = []
            self._pattern_handlers[prefix].append(handler)
            logger.debug("[EventBus] 注册通配符处理器: %s → %s", pattern, handler.__name__)

            def unsubscribe():
                try:
                    self._pattern_handlers.get(prefix, []).remove(handler)
                    logger.debug("[EventBus] 已取消通配符注册: %s → %s", pattern, handler.__name__)
                except ValueError:
                    pass

            return unsubscribe
        else:
            # 无通配符 → 退化为精确匹配
            return self.subscribe(pattern, handler)

    async def publish_and_wait(
        self,
        event_type: str,
        data: dict[str, Any],
        response_event: str,
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        """发布事件并等待响应事件。

        用于 Supervisor 等待 Specialist 完成等场景。
        """
        response_future: asyncio.Future = asyncio.get_event_loop().create_future()

        async def _on_response(resp_data: dict[str, Any]):
            if not response_future.done():
                response_future.set_result(resp_data)

        unsub = self.subscribe(response_event, _on_response)
        try:
            await self.publish(event_type, data)
            return await asyncio.wait_for(response_future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[EventBus] 等待响应超时: %s → %s (%.0fs)", event_type, response_event, timeout)
            return None
        finally:
            unsub()
