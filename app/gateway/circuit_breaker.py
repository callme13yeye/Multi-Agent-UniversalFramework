# app/gateway/circuit_breaker.py — 每模型熔断器状态机
"""CLOSED → OPEN → HALF_OPEN → CLOSED 标准三态熔断器。

每个模型拥有独立的 CircuitBreaker 实例，由 ModelGateway 管理。
"""

from __future__ import annotations

import asyncio
import time
import logging

from app.gateway.types import CircuitState

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """标准三态熔断器，并发安全（asyncio.Lock 保护状态转换）。

    状态转换规则:
        CLOSED ──连续失败≥threshold──→ OPEN
        OPEN   ──冷却时间到──────────→ HALF_OPEN（允许探测）
        HALF_OPEN ──探测成功─────────→ CLOSED
        HALF_OPEN ──探测失败─────────→ OPEN（重新计时）

    Attributes:
        failure_threshold: 连续失败多少次后触发熔断（默认 5）。
        cooldown_seconds: 熔断后冷却多久进入 HALF_OPEN（默认 30 秒）。
        half_open_max_requests: HALF_OPEN 状态下最多允许几次探测（默认 1）。
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        half_open_max_requests: int = 1,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_max_requests = half_open_max_requests

        self.state: CircuitState = CircuitState.CLOSED
        self._state_changed_at: float = time.time()
        self._half_open_count: int = 0
        self._lock = asyncio.Lock()

    # ── 查询方法（无锁） ──────────────────────────────────────

    def is_open(self) -> bool:
        """快速查询熔断器是否处于 OPEN 状态。"""
        return self.state == CircuitState.OPEN

    # ── 请求前检查 ────────────────────────────────────────────

    async def before_request(self) -> bool:
        """每次模型调用前调用。

        Returns:
            True 表示放行，False 表示拒绝（熔断中）。
        """
        async with self._lock:
            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.OPEN:
                elapsed = time.time() - self._state_changed_at
                if elapsed >= self.cooldown_seconds:
                    self.state = CircuitState.HALF_OPEN
                    self._state_changed_at = time.time()
                    self._half_open_count = 0
                    logger.info(
                        "熔断器进入 HALF_OPEN 状态（冷却 %.1fs 到期）",
                        elapsed,
                    )
                    return True
                return False

            if self.state == CircuitState.HALF_OPEN:
                if self._half_open_count < self.half_open_max_requests:
                    self._half_open_count += 1
                    return True
                return False

            return False

    # ── 结果回调 ──────────────────────────────────────────────

    async def on_success(self) -> None:
        """模型调用成功后调用。HALF_OPEN → CLOSED。"""
        async with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.CLOSED
                self._state_changed_at = time.time()
                logger.info("熔断器恢复: HALF_OPEN → CLOSED")

    async def on_failure(self) -> None:
        """模型调用失败后调用。HALF_OPEN → OPEN。"""
        async with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                self._state_changed_at = time.time()
                logger.warning("熔断器探测失败: HALF_OPEN → OPEN")

    # ── 管理接口 ──────────────────────────────────────────────

    async def trip(self) -> None:
        """手动触发熔断（管理员操作）。"""
        async with self._lock:
            self.state = CircuitState.OPEN
            self._state_changed_at = time.time()
            logger.warning("熔断器手动触发: → OPEN")

    async def reset(self) -> None:
        """手动重置熔断器到 CLOSED（管理员操作）。"""
        async with self._lock:
            self.state = CircuitState.CLOSED
            self._state_changed_at = time.time()
            self._half_open_count = 0
            logger.info("熔断器手动重置: → CLOSED")
