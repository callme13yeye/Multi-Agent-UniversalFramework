"""速率限制中间件 — 基于 Redis 滑动窗口的请求级限流。

在 FastAPI 层对传入请求进行速率限制，防止单个用户/IP 滥用 API。

算法：滑动窗口计数器
    - 将窗口（如 60s）切分为 N 个桶（如 6 个 10s 桶）
    - 每次请求时统计当前窗口内所有桶的请求数
    - 超过阈值返回 429 Too Many Requests

降级策略：
    如果 Redis 不可用，降级为内存计数（单进程级限流，跨进程不一致但好于无限流）
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from config import get_config

logger = logging.getLogger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """请求级速率限制中间件。

    使用滑动窗口算法，按 user_id 和 IP 两个维度限流。

    使用方式::

        app.add_middleware(
            RateLimitMiddleware,
            redis_client=redis_manager.client,
            user_per_minute=60,
            ip_per_minute=300,
        )
    """

    # Redis key 前缀
    _PREFIX = "ratelimit"

    def __init__(
        self,
        app,
        redis_client=None,
        user_per_minute: int | None = None,
        ip_per_minute: int | None = None,
        window_seconds: int = 60,
        bucket_count: int = 6,
    ):
        """初始化速率限制中间件。

        Args:
            redis_client: Redis 客户端（可为 None，降级为内存模式）
            user_per_minute: 每用户每分钟最大请求数
            ip_per_minute: 每IP每分钟最大请求数
            window_seconds: 滑动窗口大小（秒）
            bucket_count: 窗口内的桶数量（越多越精确但 Redis 调用越多）
        """
        super().__init__(app)
        self._redis = redis_client
        self._redis_available = redis_client is not None

        rl_config = get_config().get("rate_limits", {})
        self.user_limit = user_per_minute or rl_config.get("user_per_minute", 60)
        self.ip_limit = ip_per_minute or rl_config.get("ip_per_minute", 300)
        self.window = window_seconds or rl_config.get("window_seconds", 60)
        self.bucket_count = bucket_count
        self.bucket_width = self.window / self.bucket_count

        # ── 内存降级（非线程安全，asyncio 单线程足够） ──
        self._memory_window: dict[str, list[float]] = {}

        logger.info(
            "[RateLimit] 初始化: user=%d/min ip=%d/min window=%ds buckets=%d redis=%s",
            self.user_limit, self.ip_limit, self.window, self.bucket_count,
            self._redis_available,
        )

    async def dispatch(self, request: Request, call_next):
        """FastAPI 中间件入口。"""
        # 跳过健康检查等非业务端点
        if self._is_exempt_path(request.url.path):
            return await call_next(request)

        # 提取限流标识
        user_id = self._extract_user_id(request)
        client_ip = request.client.host if request.client else "unknown"

        # 检查速率限制（先 IP 后 User，先命中的直接拒绝）
        ip_allowed = await self._check_and_increment(f"ip:{client_ip}", self.ip_limit)
        if not ip_allowed:
            return self._build_429_response(client_ip, "per-IP")

        if user_id:
            user_allowed = await self._check_and_increment(
                f"user:{user_id}", self.user_limit
            )
            if not user_allowed:
                return self._build_429_response(user_id, "per-user")

        # 通过 — 正常处理
        response = await call_next(request)

        # 注入速率限制 header
        response.headers["X-RateLimit-Limit-User"] = str(self.user_limit)
        response.headers["X-RateLimit-Limit-IP"] = str(self.ip_limit)

        return response

    # ── 核心算法 ──────────────────────────────────────────

    async def _check_and_increment(self, key: str, limit: int) -> bool:
        """检查并递增滑动窗口计数器。返回 True = 允许，False = 拒绝。

        优先使用 Redis 实现，Redis 不可用时降级为内存模式。
        """
        if self._redis_available:
            try:
                return await self._redis_check(key, limit)
            except Exception as e:
                logger.warning(
                    "[RateLimit] Redis 不可用，降级为内存模式: %s", e
                )
                self._redis_available = False

        return self._memory_check(key, limit)

    async def _redis_check(self, key: str, limit: int) -> bool:
        """Redis 滑动窗口实现。

        使用 sorted set，每个请求作为 member，score 为纳秒时间戳。
        """
        now_ns = time.monotonic_ns()
        window_ns = self.window * 1_000_000_000
        cutoff = now_ns - window_ns
        full_key = f"{self._PREFIX}:{key}"

        # 原子操作：删除过期成员 + 添加当前请求 + 计数
        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(full_key, 0, cutoff)
        pipe.zadd(full_key, {str(now_ns): now_ns})
        pipe.zcard(full_key)
        pipe.expire(full_key, int(self.window * 2))  # TTL 两倍窗口，冗余
        _, _, count, _ = await pipe.execute()

        return count <= limit

    def _memory_check(self, key: str, limit: int) -> bool:
        """内存降级实现 — 非线程安全但 asyncio 单线程足够。"""
        now = time.monotonic()
        cutoff = now - self.window

        if key not in self._memory_window:
            self._memory_window[key] = []

        # 清理过期请求
        self._memory_window[key] = [
            t for t in self._memory_window[key] if t > cutoff
        ]

        if len(self._memory_window[key]) >= limit:
            return False

        self._memory_window[key].append(now)
        return True

    # ── Helper ────────────────────────────────────────────

    @staticmethod
    def _extract_user_id(request: Request) -> str | None:
        """从 JWT token 提取 user_id。"""
        try:
            # 尝试从授权 header 提取
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:]
                # 简单 JWT 解码（不验证签名，只取 payload）
                import base64
                import json
                payload = token.split(".")[1]
                # 补齐 base64 padding
                payload += "=" * (4 - len(payload) % 4) if len(payload) % 4 else ""
                decoded = json.loads(base64.urlsafe_b64decode(payload).decode())
                return str(decoded.get("sub", decoded.get("user_id", ""))) or None
        except Exception:
            pass
        return None

    @staticmethod
    def _is_exempt_path(path: str) -> bool:
        """跳过不需要限流的端点。"""
        exempt = ["/health", "/ping", "/metrics", "/docs", "/openapi.json", "/redoc"]
        return any(path.startswith(e) for e in exempt)

    @staticmethod
    def _build_429_response(identifier: str, scope: str) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={
                "detail": f"请求过于频繁 ({scope})，请稍后重试",
                "scope": scope,
            },
            headers={
                "Retry-After": "30",
            },
        )
