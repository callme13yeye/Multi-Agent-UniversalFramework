# app/gateway/__init__.py — 模型智能网关公开 API
"""模型智能网关 — 零停机模型切换 + 健康感知路由 + 熔断保护。

核心组件:
    - ModelGateway: 中心单例，管理所有模型生命周期
    - GatewayMiddleware: 替换 ModelFallbackMiddleware，每次调用动态路由
    - CircuitBreaker: 每模型独立熔断器
    - HealthProbe: 后台定期探活

Usage::

    from app.gateway import ModelGateway, GatewayMiddleware, ModelRole, CircuitState

    gateway = ModelGateway()
    await gateway.register_model(spec, instance)
    await gateway.start_probe()
    agent = create_agent(
        model=primary_model,
        middleware=[GatewayMiddleware(gateway=gateway, role=ModelRole.CHAT)],
    )
"""

from app.gateway.types import CircuitState, HealthRecord, ModelRole, ModelSpec
from app.gateway.circuit_breaker import CircuitBreaker
from app.gateway.model_gateway import ModelGateway
from app.gateway.health_probe import HealthProbe
from app.gateway.gateway_middleware import GatewayMiddleware

__all__ = [
    "CircuitBreaker",
    "CircuitState",
    "GatewayMiddleware",
    "HealthProbe",
    "HealthRecord",
    "ModelGateway",
    "ModelRole",
    "ModelSpec",
]
