# app/gateway/gateway_middleware.py — 智能网关中间件
"""替换 ModelFallbackMiddleware，实现健康感知智能路由。

与 ModelFallbackMiddleware 的关键区别:
    - 每次模型调用都从 Gateway 动态获取模型链（而非静态 fallback 列表）
    - 自动跳过已熔断（OPEN）的模型
    - 记录每次调用的延迟和成败到 Gateway
    - 模型链顺序由 Gateway 实时决定（受 Admin API 热切换影响）
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain_core.messages import AIMessage

    from app.gateway.model_gateway import ModelGateway
    from app.gateway.types import ModelRole

logger = logging.getLogger(__name__)


class GatewayMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """基于 ModelGateway 的智能模型路由中间件。

    每次模型调用时:
        1. 从 Gateway 获取健康排序的模型链
        2. 跳过已熔断的模型
        3. 逐个尝试，直到成功
        4. 记录延迟和成败

    Usage:
        # 替换原来的 ModelFallbackMiddleware
        middleware = [
            GatewayMiddleware(gateway=gateway, role=ModelRole.CHAT),
        ]
    """

    def __init__(self, gateway: ModelGateway, role: ModelRole) -> None:
        """初始化智能路由中间件。

        Args:
            gateway: 模型网关单例。
            role: 此中间件服务的角色（决定使用哪个模型链）。
        """
        super().__init__()
        self.gateway = gateway
        self.role = role

    # ── 同步版本（未使用，但必须实现以避免 NotImplementedError） ──

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage:
        """同步版本 — 直接委托给 handler（代理在 async 路径上）。"""
        return handler(request)

    # ── 异步版本（实际使用） ──────────────────────────────────

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage:
        """健康感知模型路由。

        从 Gateway 获取模型链，逐个尝试直到成功或全部失败。
        通过 current_handler contextvar 向 StatusCallbackHandler 推送降级/恢复事件。
        """
        # 延迟导入避免循环依赖
        from app.harness.status_handler import current_handler

        status_handler = current_handler.get()
        model_chain = self.gateway.get_model_chain(self.role)

        if not model_chain:
            logger.warning("[GatewayMiddleware] 无可用模型链，使用原始模型")
            return await handler(request)

        last_error: Exception | None = None

        for i, (model_name, model_instance) in enumerate(model_chain):
            try:
                start = time.monotonic()
                result = await handler(request.override(model=model_instance))
                latency = (time.monotonic() - start) * 1000
                await self.gateway.record_success(model_name, latency)
                # 主模型恢复（仅当之前降级过才实际推送）
                if i == 0 and status_handler is not None:
                    await status_handler.emit_restored()
                return result
            except Exception as e:
                await self.gateway.record_failure(model_name, str(e))
                last_error = e
                logger.warning(
                    "[GatewayMiddleware] %s 调用失败: %s，尝试下一个",
                    model_name,
                    e,
                )
                # 降级到备用模型（仅首次降级才推送）
                if status_handler is not None:
                    await status_handler.emit_degraded()

        # 所有模型都失败
        if last_error is not None:
            raise last_error
        return await handler(request)
