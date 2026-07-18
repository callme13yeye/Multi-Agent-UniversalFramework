"""Hook 组装工厂 — 根据 Agent 角色组装对应的 Hook 列表。

每个工厂函数返回 AgentMiddleware 实例列表，可直接通过
``async_create_agent(extra_middleware=hooks)`` 注入。
"""

from __future__ import annotations

import logging

from app.hooks.types import HookDependencies, HookRole
from app.hooks.journal_hook import create_journal_hook
from app.hooks.approval_guard_hook import create_approval_guard_hook
from app.hooks.delegation_hook import create_delegation_hook

logger = logging.getLogger(__name__)


def assemble_hooks(deps: HookDependencies, role: str) -> list:
    """为指定 Agent 角色组装 Hook middleware 列表。

    Args:
        deps: 基础设施依赖（context_manager、event_bus、store）。
        role: Agent 角色（HookRole.TRIAGE / HookRole.EXECUTOR）。

    Returns:
        AgentMiddleware 实例列表，可直接传给
        ``async_create_agent(extra_middleware=...)``。

    角色分配规则：
        - Executor：全部 hook（journal、审批兜底、委托追踪）
        - Triage：仅委托追踪（Triage 不写 journal、不处理审批）
    """
    hooks: list = []

    if role == HookRole.EXECUTOR:
        # Journal 写入 — 仅 Executor（记录后台执行过程）
        if deps.context_manager and deps.store:
            hooks.extend(create_journal_hook(deps))
            logger.info("[Hooks] Journal hook 已注册 (Executor)")

        # 审批标记兜底 — 仅 Executor（只有 Executor 处理审批）
        hooks.append(create_approval_guard_hook(max_rounds=3))
        logger.info("[Hooks] ApprovalGuard hook 已注册 (Executor)")

    # 委托追踪 — Triage 和 Executor 都需要
    if deps.event_bus:
        hooks.append(create_delegation_hook(deps))
        logger.info("[Hooks] Delegation hook 已注册 (%s)", role)

    return hooks
