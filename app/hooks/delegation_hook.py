"""委托追踪 Hook — Agent 间委派链路的可观测性。

通过 EventBus 发布 ``agent.delegation`` 事件，记录：
    - Triage → Executor（create_background_task 工具调用）
    - Executor → Specialist（task 工具调用）

对所有 Agent 角色生效（Triage 和 Executor）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.agents.middleware import wrap_tool_call

if TYPE_CHECKING:
    from app.hooks.types import HookDependencies

logger = logging.getLogger(__name__)


def create_delegation_hook(deps: "HookDependencies"):
    """创建 Agent 委托追踪 Hook。

    追踪两种委派类型：
        - ``create_background_task``: Triage → Executor
        - ``task``: Executor/Triage → Specialist
    """

    @wrap_tool_call
    async def delegation_tracker(request, handler):
        """记录 Agent 间的每次委托。"""
        tool_name = getattr(request.tool_call, "name", "")

        # 执行原工具
        result = handler(request)

        ctx = getattr(request.runtime, "context", None)
        task_id = getattr(ctx, "task_id", "") if ctx else ""
        args = getattr(request.tool_call, "args", {}) or {}

        if tool_name == "create_background_task":
            # Triage → Executor 委派
            goal = args.get("goal", "")[:200]
            logger.info("[Delegation] Triage → Executor: %.100s", goal)
            if deps.event_bus:
                await deps.event_bus.publish("agent.delegation", {
                    "from_agent": "triage",
                    "to_agent": "executor",
                    "goal": goal,
                    "source_task_id": task_id,
                })

        elif tool_name == "task":
            # Executor/Triage → Specialist 委派
            subagent_type = args.get("subagent_type", "unknown")
            description = args.get("description", "")[:200]
            logger.info("[Delegation] → Specialist %s: %.100s", subagent_type, description)
            if deps.event_bus:
                await deps.event_bus.publish("agent.delegation", {
                    "from_agent": "executor" if task_id else "triage",
                    "to_agent": subagent_type,
                    "description": description,
                    "task_id": task_id,
                })

        return result

    return delegation_tracker
