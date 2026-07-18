"""Hook 基础设施类型定义。

Hook 通过闭包持有非可序列化的基础设施依赖（context_manager、event_bus 等），
在 Agent 的 middleware 管线中运行。与传统 Middleware 不同，Hook 专注于横切关注点
（日志、审计、监控）而非业务行为修改。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.harness.task_context import TaskContextManager
    from app.harness.event_bus import EventBus
    from langgraph.store.postgres.aio import AsyncPostgresStore


@dataclass
class HookDependencies:
    """Hook 依赖的基础设施组件（闭包注入，不序列化）。"""
    context_manager: "TaskContextManager | None" = None
    event_bus: "EventBus | None" = None
    store: "AsyncPostgresStore | None" = None


class HookRole:
    """Hook 适用的 Agent 角色。"""
    TRIAGE = "triage"
    EXECUTOR = "executor"
    ALL = "all"
