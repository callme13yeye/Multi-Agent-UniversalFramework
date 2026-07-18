"""Journal Hook — 工具调用和执行决策的结构化日志。

替代 TaskExecutor._write_journal_from_messages() 的硬编码逻辑。
通过 @wrap_tool_call 和 @after_model 在 Agent 内部自动记录，
新增 SubAgent 时零配置生效。

Journal 仅对 Executor Agent 生效（通过 runtime.context.task_id 判断）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.agents.middleware import wrap_tool_call, after_model
from langchain_core.messages import AIMessage, ToolMessage

if TYPE_CHECKING:
    from app.hooks.types import HookDependencies

logger = logging.getLogger(__name__)


def create_journal_hook(deps: "HookDependencies") -> list:
    """创建 journal 写入 Hook 列表。

    返回两个 AgentMiddleware:
        - tool_audit: 每次工具调用后写入 specialist_result 日志
        - decision_log: 每次 LLM 输出后写入 decision/error 日志

    仅在 runtime.context.task_id 存在时写入（Executor Agent 的后台任务）。
    Triage Agent 的同步请求自动跳过。
    """

    # ── 步骤计数器（Store 中持久化，重启不丢失） ──
    async def _next_step(store, task_id: str) -> int:
        """原子递增并返回步骤编号。"""
        try:
            item = await store.aget(("task_journal_meta",), task_id)
            current = item.value.get("step", 0) if item and item.value else 0
        except Exception:
            current = 0
        next_val = current + 1
        try:
            await store.aput(("task_journal_meta",), task_id, {"step": next_val})
        except Exception:
            pass
        return next_val

    # ── 工具调用审计 ────────────────────────────────────
    @wrap_tool_call
    async def tool_audit(request, handler):
        """每次工具调用后写入 journal，记录 specialist 执行结果。"""
        result = handler(request)

        ctx = getattr(request.runtime, "context", None)
        task_id = getattr(ctx, "task_id", "") if ctx else ""
        store = getattr(request.runtime, "store", None)

        if task_id and store and deps.context_manager:
            tool_name = getattr(request.tool_call, "name", "unknown")
            # 提取 ToolMessage 结果内容
            content = ""
            if isinstance(result, ToolMessage):
                content = result.content if isinstance(result.content, str) else str(result.content)
            elif hasattr(result, "content"):
                content = str(getattr(result, "content", ""))
            else:
                content = str(result) if result else ""

            from app.harness.task_context import JournalEntry

            step = await _next_step(store, task_id)
            entry = JournalEntry(
                step=step,
                event="specialist_result",
                description=f"委托 {tool_name} 完成",
                detail={
                    "specialist": tool_name,
                    "result_summary": content[:300] if content else "(无输出)",
                },
            )
            try:
                await deps.context_manager.write_journal_entry(task_id, entry)
                logger.debug("[Journal] %s #%d specialist_result: %s", task_id, step, tool_name)
            except Exception as e:
                logger.warning("[Journal] 写入失败: %s", e)

        return result

    # ── 决策/错误日志 ───────────────────────────────────
    @after_model
    async def decision_log(state, runtime):
        """每次 LLM 输出后检测关键决策或错误，写入 journal。"""
        ctx = getattr(runtime, "context", None)
        task_id = getattr(ctx, "task_id", "") if ctx else ""
        store = getattr(runtime, "store", None)

        if not task_id or not store or not deps.context_manager:
            return None

        messages = state.get("messages", [])
        if not messages:
            return None

        for msg in reversed(messages):
            if not isinstance(msg, AIMessage) or not msg.content:
                continue
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if not content.strip() or len(content.strip()) < 100:
                continue

            # 判断事件类型
            error_keywords = ("失败", "错误", "异常", "❌", "failed", "error", "exception")
            is_error = any(kw in content[:200] for kw in error_keywords)
            event_type = "error" if is_error else "decision"

            from app.harness.task_context import JournalEntry

            step = await _next_step(store, task_id)
            entry = JournalEntry(
                step=step,
                event=event_type,
                description=content.strip()[:200],
                detail={
                    "is_error": is_error,
                    "full_length": len(content.strip()),
                },
            )
            try:
                await deps.context_manager.write_journal_entry(task_id, entry)
                logger.debug(
                    "[Journal] %s #%d %s: %.80s", task_id, step, event_type, content,
                )
            except Exception as e:
                logger.warning("[Journal] 写入失败: %s", e)

            break  # 每次模型输出只记录一条

        return None

    return [tool_audit, decision_log]
