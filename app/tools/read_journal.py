# read_journal.py — Executor DeepAgent 的执行日志读取工具
#
# Executor 在长周期执行过程中可能因 SummarizationMiddleware 压缩丢失
# 早期的执行上下文。此工具让 Executor 能主动查阅自己的 task_journal，
# 了解"已经做了什么、做到哪了"，而不只依赖被动注入的恢复摘要。
#
# 与 task_journal 的关系：
#   - TaskExecutor._write_journal_from_messages() → 写入 journal
#   - read_task_journal 工具 → Executor LLM 主动读取 journal
#   - GET /tasks/{id}/journal → 人类通过 API 查看 journal

import logging

from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

from app.tools._registry import register_tool

logger = logging.getLogger(__name__)


@register_tool
@tool
async def read_task_journal(
    limit: int = 20,
    event_filter: str = "",
    config: RunnableConfig = None,
) -> str:
    """读取当前任务的执行日志，了解已经完成了哪些步骤。

    当你在长周期执行中需要回顾"已经做了什么、做到哪了"时，使用此工具。
    执行日志是结构化的永久记录，不受上下文压缩影响。

    **适用场景：**
    - 上下文被压缩后，需要回顾早期步骤的结果
    - 执行到一半不确定某步骤是否已完成
    - 需要根据历史决策调整当前策略
    - 任务恢复后需要快速了解进度

    **不适用场景：**
    - 查看当前这轮对话中刚刚发生的事情（上下文窗口中有）
    - 查询其他任务（只能读自己的日志）

    Args:
        limit: 返回最近 N 条记录（默认 20，传 0 返回全部）
        event_filter: 可选事件类型过滤，如 "specialist_result"、"approval_requested"、
                      "decision"、"error"、"completed"。留空返回全部。
    """
    store = None
    task_id = ""
    if config and hasattr(config, "get"):
        configurable = config.get("configurable", {})
        store = configurable.get("store")
        task_id = configurable.get("task_id", "")

    if not store:
        return (
            "⚠️ 无法读取执行日志：Store 不可用。"
            "请检查服务状态或联系管理员。"
        )

    if not task_id:
        return (
            "⚠️ 无法读取执行日志：当前不在后台任务上下文中（缺少 task_id）。"
            "此工具只能在 Executor 后台任务中使用。"
        )

    try:
        items = await store.asearch(
            ("task_journal", task_id),
            limit=limit if limit > 0 else 200,
        )
    except Exception as e:
        logger.error("[read_task_journal] Store 查询失败: %s", e)
        return f"❌ 读取执行日志失败: {e}"

    if not items:
        return "📋 当前任务还没有执行日志记录。这是任务的初始阶段，尚未委托任何 Specialist。"

    # ── 解析并排序 ──
    entries = []
    for item in items:
        if item.value:
            entries.append(item.value)

    entries.sort(key=lambda e: e.get("step", 0))

    # ── 过滤 ──
    if event_filter:
        entries = [e for e in entries if e.get("event") == event_filter]
        if not entries:
            return f"📋 没有找到事件类型为 '{event_filter}' 的日志记录。"

    # ── 应用 limit ──
    if limit > 0 and len(entries) > limit:
        entries = entries[-limit:]

    # ── 格式化输出 ──
    lines = [f"## 📋 执行日志（共 {len(entries)} 条）", ""]

    for e in entries:
        step = e.get("step", "?")
        event = e.get("event", "unknown")
        ts = e.get("timestamp", "")[:19]
        desc = e.get("description", "")

        icon = {
            "specialist_result": "✅",
            "approval_requested": "⏳",
            "decision": "🧭",
            "error": "⚠️",
            "completed": "🏁",
        }.get(event, "📌")

        lines.append(f"{icon} **#{step}** [{ts}] {event}")
        if desc:
            lines.append(f"   {desc[:300]}")

        detail = e.get("detail") or {}
        if detail.get("specialist"):
            lines.append(f"   └ 委托: {detail['specialist']}")
        if detail.get("result_summary"):
            lines.append(f"   └ 结果: {detail['result_summary'][:200]}")
        if detail.get("approval_id"):
            lines.append(f"   └ 审批ID: {detail['approval_id']}")

    return "\n".join(lines)
