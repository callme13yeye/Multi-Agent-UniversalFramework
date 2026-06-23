# app/routes/admin_routes.py — 模型网关管理 API
"""管理端点: 查询模型状态、热切换模型、管理熔断器。

所有端点要求管理员权限（通过 X-Admin-Token 头验证）。
"""

from __future__ import annotations

import os
import logging

from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel

from app.gateway.types import ModelRole

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])

# 简单的管理令牌验证（从环境变量读取，未设置则允许所有）
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _verify_admin(request: Request) -> None:
    """验证管理员权限。"""
    if not ADMIN_TOKEN:
        return  # 未设置令牌，允许所有
    token = request.headers.get("X-Admin-Token", "")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="需要管理员权限")


# ── Pydantic 模型 ────────────────────────────────────────


class ModelStatusResponse(BaseModel):
    """模型状态响应。"""
    models: dict


class ActivateResponse(BaseModel):
    """热切换响应。"""
    status: str
    role: str
    active_model: str


class CircuitResponse(BaseModel):
    """熔断器操作响应。"""
    status: str
    model: str
    circuit_state: str


# ── 端点 ──────────────────────────────────────────────────


@router.get("/models")
async def list_models(request: Request) -> ModelStatusResponse:
    """GET /admin/models — 查询所有已注册模型的状态。

    Returns:
        每个模型的规格、健康统计、熔断器状态、活跃角色。
    """
    _verify_admin(request)
    gateway = request.app.state.model_gateway
    return ModelStatusResponse(models=gateway.get_all_status())


@router.put("/models/{name}/activate")
async def activate_model(
    request: Request,
    name: str,
    role: str = Query(..., description="角色: chat / fallback_chat / retrieval_llm / retrieval_rewriter"),
) -> ActivateResponse:
    """PUT /admin/models/{name}/activate?role=chat — 热切换模型（零停机）。

    将指定角色的活跃模型立即切换为 name。所有进行中的请求不受影响，
    下一个请求将使用新模型。
    """
    _verify_admin(request)

    # 验证 role
    try:
        model_role = ModelRole(role)
    except ValueError:
        valid = [r.value for r in ModelRole]
        raise HTTPException(
            status_code=400,
            detail=f"无效的角色: {role}，有效值: {valid}",
        )

    gateway = request.app.state.model_gateway

    # 验证模型存在
    if name not in gateway._models:
        available = list(gateway._models.keys())
        raise HTTPException(
            status_code=404,
            detail=f"未知模型: {name}，可用模型: {available}",
        )

    try:
        await gateway.set_active_model(model_role, name)
        logger.info("[Admin] 管理员热切换: role=%s → %s", role, name)
        return ActivateResponse(
            status="ok",
            role=role,
            active_model=name,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/models/{name}/circuit")
async def manage_circuit(
    request: Request,
    name: str,
    action: str = Query(..., description="reset 或 trip"),
) -> CircuitResponse:
    """PUT /admin/models/{name}/circuit?action=reset|trip — 手动管理熔断器。

    - ``reset``: 强制将熔断器重置为 CLOSED（恢复使用）
    - ``trip``:  强制触发熔断器为 OPEN（停止使用）
    """
    _verify_admin(request)

    if action not in ("reset", "trip"):
        raise HTTPException(
            status_code=400,
            detail="action 必须为 'reset' 或 'trip'",
        )

    gateway = request.app.state.model_gateway
    breaker = gateway._breakers.get(name)
    if breaker is None:
        raise HTTPException(status_code=404, detail=f"未知模型: {name}")

    if action == "reset":
        await breaker.reset()
    else:
        await breaker.trip()

    logger.info("[Admin] 熔断器操作: model=%s action=%s → %s", name, action, breaker.state.value)
    return CircuitResponse(
        status="ok",
        model=name,
        circuit_state=breaker.state.value,
    )
