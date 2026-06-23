"""死信队列 — 失败操作的可重试持久化存储。

当关键操作（审批写入、任务创建、外部 API 调用）在重试全部失败后，
不静默丢弃，而是写入死信队列等待后续处理。

核心设计：
    - 死信持久化到 Store namespace ("dead_letter",)，服务重启不丢失
    - 支持指数退避重试（1m → 2m → 4m → ... 最大 1h）
    - 支持最大重试次数（达到后标记为 abandoned，触发告警）
    - 后台定期扫描可重试的死信

使用方式::

    dlq = DeadLetterQueue(store)

    # 包装一个操作
    result = await dlq.with_dead_letter(
        operation_name="async_request_approval",
        operation_args={"title": "...", "approver_role": "用人经理"},
        max_retries=5,
    )(my_async_operation)()
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Awaitable, TYPE_CHECKING

if TYPE_CHECKING:
    from langgraph.store.postgres.aio import AsyncPostgresStore

logger = logging.getLogger(__name__)

DEAD_LETTER_NS = ("dead_letter",)


@dataclass
class DeadLetterEntry:
    """死信条目 — 记录一次失败的操作。"""

    message_id: str                    # 幂等 key
    operation_name: str                # 操作名（如 "async_request_approval"）
    operation_args: dict[str, Any]     # 操作参数
    error_message: str                 # 最后一次失败的错误信息
    retry_count: int = 0               # 已重试次数
    max_retries: int = 5               # 最大重试次数
    next_retry_at: float = 0.0           # 下次可重试时间（Unix timestamp）
    status: str = "pending"            # pending | retrying | abandoned
    trace_id: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "operation_name": self.operation_name,
            "operation_args": self.operation_args,
            "error_message": self.error_message,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "next_retry_at": self.next_retry_at,
            "status": self.status,
            "trace_id": self.trace_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeadLetterEntry":
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid}
        # 向后兼容：旧数据 next_retry_at 可能是字符串
        if "next_retry_at" in filtered and isinstance(filtered["next_retry_at"], str):
            try:
                filtered["next_retry_at"] = float(filtered["next_retry_at"])
            except (ValueError, TypeError):
                filtered["next_retry_at"] = 0.0
        return cls(**filtered)


class DeadLetterQueue:
    """死信队列 — 失败操作的可靠存储与重试管理。

    使用方式::

        dlq = DeadLetterQueue(store)

        # 注册重试处理器
        dlq.register_retry_handler("my_op", my_retry_function)

        # 启动后台扫描（定期重试可重试的死信）
        await dlq.start_scanner(interval_seconds=120)

        async def my_operation():
            ...

        result = await dlq.with_dead_letter(
            operation_name="my_op",
            operation_args={"key": "value"},
        )(my_operation)()
    """

    def __init__(self, store: "AsyncPostgresStore"):
        self.store = store
        self._retry_handlers: dict[str, Callable[..., Awaitable[Any]]] = {}
        self._scan_task: asyncio.Task | None = None

    # ── 写入死信 ──────────────────────────────────────────

    async def enqueue(
        self,
        operation_name: str,
        operation_args: dict[str, Any],
        error_message: str,
        max_retries: int = 5,
    ) -> str:
        """将失败操作写入死信队列。

        Args:
            operation_name: 操作名称
            operation_args: 操作参数
            error_message: 失败原因
            max_retries: 最大重试次数（达到后标记为 abandoned）

        Returns:
            message_id: 死信消息 ID（幂等 key）
        """
        # ── 幂等 key：同一操作 + 相同参数的组合产生相同 ID ──
        raw = f"{operation_name}:{json.dumps(operation_args, sort_keys=True)}"
        message_id = f"dlq-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"

        # 检查是否已存在（幂等）
        try:
            existing = await self.store.aget(DEAD_LETTER_NS, message_id)
            if existing and existing.value:
                existing_entry = DeadLetterEntry.from_dict(existing.value)
                if existing_entry.status == "abandoned":
                    logger.warning(
                        "[DeadLetter] 幂等命中 — 死信已放弃: %s", message_id
                    )
                    return message_id
                # 更新重试计数
                existing_entry.retry_count += 1
                existing_entry.error_message = error_message
                existing_entry.updated_at = datetime.now().isoformat()

                if existing_entry.retry_count >= max_retries:
                    existing_entry.status = "abandoned"
                    logger.error(
                        "[DeadLetter] 死信已达最大重试次数 %d → 放弃: %s (op=%s)",
                        max_retries, message_id, operation_name,
                    )
                else:
                    # 指数退避下次重试时间
                    delay = min(60 * (2 ** (existing_entry.retry_count - 1)), 3600)
                    existing_entry.next_retry_at = (
                        datetime.now().timestamp() + delay
                    )

                await self.store.aput(
                    DEAD_LETTER_NS, message_id, existing_entry.to_dict(),
                )
                return message_id
        except Exception as e:
            logger.debug("[DeadLetter] 幂等检查异常（非致命）: %s", e)

        # 计算首次退避时间
        from app.trace_context import _trace_id
        trace_id = _trace_id.get()

        entry = DeadLetterEntry(
            message_id=message_id,
            operation_name=operation_name,
            operation_args=operation_args,
            error_message=error_message,
            max_retries=max_retries,
            next_retry_at=datetime.now().timestamp() + 60,  # 首次退避 1 分钟
            trace_id=trace_id,
        )

        await self.store.aput(DEAD_LETTER_NS, message_id, entry.to_dict())
        logger.info(
            "[DeadLetter] 死信已入队: %s (op=%s retry=0/%d)",
            message_id, operation_name, max_retries,
        )
        return message_id

    # ── 重试处理 ──────────────────────────────────────────

    async def get_retryable(self, limit: int = 10) -> list[DeadLetterEntry]:
        """获取可以重试的死信列表。"""
        retryable = []
        now = datetime.now().timestamp()

        try:
            items = await self.store.asearch(
                DEAD_LETTER_NS,
                limit=100,
                filter={"status": "pending"},
            )
            for item in items:
                if item.value:
                    entry = DeadLetterEntry.from_dict(item.value)
                    if entry.next_retry_at <= now:
                        retryable.append(entry)
        except Exception as e:
            logger.warning("[DeadLetter] 查询可重试死信失败: %s", e)

        return retryable[:limit]

    async def mark_retried(self, message_id: str):
        """标记死信已成功重试 — 从队列删除。"""
        try:
            await self.store.adelete(DEAD_LETTER_NS, message_id)
            logger.info("[DeadLetter] 死信已清除: %s", message_id)
        except Exception as e:
            logger.warning("[DeadLetter] 清除死信失败: %s", e)

    async def mark_abandoned(self, message_id: str):
        """标记死信为放弃 — 保留记录供告警。"""
        try:
            existing = await self.store.aget(DEAD_LETTER_NS, message_id)
            if existing and existing.value:
                entry = DeadLetterEntry.from_dict(existing.value)
                entry.status = "abandoned"
                entry.updated_at = datetime.now().isoformat()
                await self.store.aput(DEAD_LETTER_NS, message_id, entry.to_dict())
                logger.error("[DeadLetter] 死信已放弃: %s", message_id)
        except Exception as e:
            logger.warning("[DeadLetter] 标记放弃失败: %s", e)

    # ── 重试处理器注册 ────────────────────────────────────

    def register_retry_handler(
        self,
        operation_name: str,
        handler: Callable[..., Awaitable[Any]],
    ):
        """注册操作的重试处理器。

        当后台扫描器发现可重试的死信时，调用对应的 handler 重新执行操作。
        handler 签名为 ``async def handler(operation_args: dict) -> bool``，
        返回 True 表示成功，False 表示仍失败（会重新入队）。

        Args:
            operation_name: 操作名（与 enqueue/with_dead_letter 一致）
            handler: 重试函数，接收 operation_args，返回是否成功
        """
        self._retry_handlers[operation_name] = handler
        logger.info(
            "[DeadLetter] 重试处理器已注册: %s → %s",
            operation_name, handler.__name__,
        )

    # ── 后台扫描器 ────────────────────────────────────────

    async def start_scanner(self, interval_seconds: float = 120.0):
        """启动后台死信扫描任务。

        定期扫描可重试的死信，调用已注册的 handler 尝试重试。
        每个 operation_name 需要先通过 register_retry_handler 注册处理器。

        Args:
            interval_seconds: 扫描间隔（秒），默认 2 分钟
        """
        if self._scan_task is not None:
            return
        self._scan_task = asyncio.create_task(self._scan_loop(interval_seconds))
        logger.info(
            "[DeadLetter] 后台扫描已启动 (interval=%ss)", interval_seconds,
        )

    async def stop_scanner(self):
        """停止后台死信扫描任务。"""
        if self._scan_task is not None:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
            self._scan_task = None
            logger.info("[DeadLetter] 后台扫描已停止")

    async def _scan_loop(self, interval_seconds: float):
        """后台扫描主循环。"""
        while True:
            # 分片 sleep，响应取消信号
            for _ in range(int(interval_seconds)):
                await asyncio.sleep(1)
            await self._scan_once()

    async def _scan_once(self):
        """执行一次死信扫描和重试。"""
        retryable = await self.get_retryable(limit=10)
        if not retryable:
            return

        logger.info("[DeadLetter] 扫描到 %d 条可重试死信", len(retryable))
        for entry in retryable:
            handler = self._retry_handlers.get(entry.operation_name)
            if handler is None:
                logger.debug(
                    "[DeadLetter] 死信 %s (op=%s) 无注册处理器，跳过",
                    entry.message_id, entry.operation_name,
                )
                continue

            try:
                success = await handler(entry.operation_args)
                if success:
                    await self.mark_retried(entry.message_id)
                    logger.info(
                        "[DeadLetter] 死信重试成功: %s (op=%s)",
                        entry.message_id, entry.operation_name,
                    )
                else:
                    # 重试仍失败 → 重新入队（enqueue 内部处理计数+退避）
                    await self.enqueue(
                        operation_name=entry.operation_name,
                        operation_args=entry.operation_args,
                        error_message="重试处理器返回失败",
                        max_retries=entry.max_retries,
                    )
            except Exception as e:
                logger.error(
                    "[DeadLetter] 死信重试异常: %s (op=%s): %s",
                    entry.message_id, entry.operation_name, e,
                )
                await self.enqueue(
                    operation_name=entry.operation_name,
                    operation_args=entry.operation_args,
                    error_message=str(e),
                    max_retries=entry.max_retries,
                )

    # ── 装饰器 — 自动包装操作 ────────────────────────────

    def with_dead_letter(
        self,
        operation_name: str,
        operation_args: dict[str, Any],
        max_retries: int = 5,
    ):
        """装饰器：自动将失败操作写入死信队列。

        使用方式::

            @dlq.with_dead_letter("my_op", {"key": val})
            async def my_operation():
                ...

            result = await my_operation()
        """
        dlq = self

        def decorator(func: Callable[..., Awaitable[Any]]):
            async def wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    logger.error(
                        "[DeadLetter] 操作失败，写入死信: %s → %s",
                        operation_name, e,
                    )
                    await dlq.enqueue(
                        operation_name=operation_name,
                        operation_args=operation_args,
                        error_message=str(e),
                        max_retries=max_retries,
                    )
                    raise
            wrapper.__name__ = func.__name__
            return wrapper
        return decorator
