# task_query.py — Triage 层查询后台任务状态和结果的工具
#
# 这是第二层→第一层"回传闭环"的关键组件。
#
# 工作流：
#   1. Triage 调用 create_background_task 创建后台任务
#   2. 用户在后续对话中问"任务进展如何"
#   3. Triage 调用 get_task_status 查询状态 → 用自然语言回复
#   4. 如果任务已完成，_inject_task_results 也会在下次 /chat 自动注入结果
#
# 与 get_task_status 互补：
#   - get_task_status: Triage 主动查询（用户询问时）
#   - _inject_task_results: 被动注入（下次对话时自动推送已完成结果）

import logging

from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

from app.tools._registry import register_tool
from app.tools.resources import get_task_executor

logger = logging.getLogger(__name__)


@register_tool
@tool
async def get_task_status(
    task_id: str = "",
    config: RunnableConfig = None,
) -> str:
    """查询后台任务的当前状态、进度和结果。

    当用户询问「我之前创建的任务怎么样了」「任务进展如何」「帮我看看
    后台任务的执行情况」时使用此工具。

    **两种用法：**
    1. 指定 task_id → 返回该任务的完整状态（进度、计划、结果摘要）
    2. 不传 task_id → 列出当前会话中所有后台任务的状态概览

    **返回信息包括：**
    - 任务状态（created/executing/waiting_human/completed/failed/cancelled）
    - 当前进度描述
    - 执行计划步骤
    - 结果摘要（如果已完成）
    - 错误信息（如果失败）
    - 审批状态（如果等待人审）

    Args:
        task_id: 任务编号（可选，不传则列出当前会话所有任务）
    """
    executor = get_task_executor()
    if executor is None:
        return (
            "⚠️ 任务执行器未初始化，无法查询任务状态。"
            "请联系管理员检查服务状态。"
        )

    try:
        user_id = config.get("configurable", {}).get("user_id", "")
        session_id = config.get("configurable", {}).get("session_id", "")
    except Exception:
        user_id = ""
        session_id = ""

    if not user_id and not session_id:
        return "⚠️ 无法确定当前用户或会话，无法查询任务。"

    # ── 模式 1: 查询指定任务 ──
    if task_id:
        handle = await executor.get_task(task_id)
        if handle is None:
            return (
                f"❌ 未找到任务 `{task_id}`。\n\n"
                f"可能的原因：\n"
                f"1. 任务编号输入有误\n"
                f"2. 该任务不属于当前会话\n"
                f"3. 任务已过期被清理\n\n"
                f"建议：不传 task_id 查看当前会话的所有任务列表。"
            )

        # 权限检查：只允许查询自己的任务
        if user_id and handle.user_id and handle.user_id != user_id:
            return f"❌ 任务 `{task_id}` 不属于当前用户，无权查看。"

        return _format_single_task(handle)

    # ── 模式 2: 列出当前会话所有任务 ──
    all_handles = await executor.list_tasks()

    # 按 user_id + session_id 筛选
    my_tasks = [
        h for h in all_handles
        if (not user_id or h.user_id == user_id) and
           (not session_id or h.session_id == session_id)
    ]

    if not my_tasks:
        return (
            "📋 当前会话中没有后台任务。\n\n"
            "如果你需要创建一个新任务，告诉我具体目标即可，"
            "我会判断是否需要转为后台执行。"
        )

    return _format_task_list(my_tasks)


def _format_single_task(handle) -> str:
    """格式化单个任务的详细信息。"""
    from app.harness.task_executor import TaskStatus

    status = handle.status
    icon = {
        TaskStatus.CREATED: "🆕",
        TaskStatus.EXECUTING: "🔄",
        TaskStatus.WAITING_HUMAN: "⏳",
        TaskStatus.COMPLETED: "✅",
        TaskStatus.FAILED: "❌",
        TaskStatus.CANCELLED: "🚫",
    }.get(status, "📌")

    lines = [
        f"## {icon} 任务 `{handle.task_id}`",
        "",
        f"**目标**: {handle.goal}",
        f"**状态**: {status.value}",
    ]

    if handle.progress:
        lines.append(f"**当前进度**: {handle.progress}")

    if handle.result_summary:
        lines.append(f"**结果摘要**: {handle.result_summary}")

    if handle.error_message:
        lines.append(f"**错误信息**: {handle.error_message}")

    if handle.plan:
        lines.append("")
        lines.append("### 📋 执行计划")
        for i, step in enumerate(handle.plan, 1):
            if isinstance(step, dict):
                step_desc = step.get("description", step.get("id", f"步骤 {i}"))
                step_status = step.get("status", "pending")
                step_icon = {
                    "completed": "✅",
                    "in_progress": "🔄",
                    "failed": "❌",
                    "skipped": "⏭️",
                }.get(step_status, "⬜")
                lines.append(f"{step_icon} {step_desc}")
            else:
                lines.append(f"⬜ {step}")

    if status == TaskStatus.WAITING_HUMAN:
        lines.append("")
        lines.append("⏳ **等待人工审批** — 此任务需要审批人做出决策后才能继续。")

    if status == TaskStatus.COMPLETED:
        lines.append("")
        lines.append("✅ 任务已完成。如需查看详细执行日志，可以使用 `/tasks/{task_id}/journal` 接口。")

    if status == TaskStatus.FAILED:
        lines.append("")
        lines.append("❌ 任务执行失败。你可以让我重新创建任务，或联系管理员排查。")

    lines.append(f"\n📅 创建: {handle.created_at[:19]} | 更新: {handle.updated_at[:19]}")

    return "\n".join(lines)


def _format_task_list(handles: list) -> str:
    """格式化任务列表概览。"""
    from app.harness.task_executor import TaskStatus

    status_order = {
        TaskStatus.WAITING_HUMAN: 0,
        TaskStatus.EXECUTING: 1,
        TaskStatus.CREATED: 2,
        TaskStatus.COMPLETED: 3,
        TaskStatus.FAILED: 4,
        TaskStatus.CANCELLED: 5,
    }

    handles.sort(key=lambda h: status_order.get(h.status, 99))

    lines = [f"## 📋 后台任务概览（共 {len(handles)} 个）", ""]

    for h in handles:
        status = h.status
        icon = {
            TaskStatus.CREATED: "🆕",
            TaskStatus.EXECUTING: "🔄",
            TaskStatus.WAITING_HUMAN: "⏳",
            TaskStatus.COMPLETED: "✅",
            TaskStatus.FAILED: "❌",
            TaskStatus.CANCELLED: "🚫",
        }.get(status, "📌")

        goal_preview = h.goal[:60] + ("..." if len(h.goal) > 60 else "")
        lines.append(f"{icon} **`{h.task_id}`** — {status.value}")
        lines.append(f"   🎯 {goal_preview}")

        if h.progress:
            progress_preview = h.progress[:100]
            lines.append(f"   📝 {progress_preview}")

        if h.result_summary:
            summary_preview = h.result_summary[:100]
            lines.append(f"   📊 {summary_preview}")

    lines.append("")
    lines.append("如需查看某个任务的详细信息，请告诉我任务编号。")

    return "\n".join(lines)
