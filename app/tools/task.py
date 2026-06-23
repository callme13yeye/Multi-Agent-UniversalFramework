# task.py — 后台任务工具
# Agent 通过此工具将复杂目标转化为后台异步执行的任务。
# 这是 "AI 同事" 长周期执行能力的核心入口。

import hashlib
import logging

from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

from app.tools._registry import register_tool
from app.tools.resources import get_task_executor

logger = logging.getLogger(__name__)


def _make_task_idempotency_key(user_id: str, goal: str) -> str:
    """为后台任务生成幂等 key — 同一用户 + 同一目标的组合始终产生相同 key。

    防止 Agent 因重试/超时对同一个 goal 创建多个重复的后台任务。
    """
    raw = f"bg-task:{user_id}:{goal}".encode("utf-8")
    return f"task-{hashlib.sha256(raw).hexdigest()[:16]}"


@register_tool
@tool
async def create_background_task(
    goal: str,
    idempotency_key: str = "",
    config: RunnableConfig = None,
) -> str:
    """将复杂目标创建为后台任务，在后台自主规划并逐步执行。

    当用户的目标需要多步操作、跨多个 Specialist 协作、或涉及长时间等待时，
    使用此工具将任务转为后台执行。任务创建后立即返回，用户无需等待完成。

    **幂等保证**：同一用户 + 同一 goal 的组合只会创建一个后台任务。
    如果任务已存在，返回已有任务的信息而非创建新任务。

    **适用场景（必须使用此工具）：**
    - 招聘全流程："帮产品部招一个高级后端工程师"
    - 批量操作："筛选本月所有符合P7标准的候选人"
    - 长周期跟踪："跟进张三 Offer 审批，卡住了帮我催"
    - 多步骤分析："分析各部门招聘效率，找出瓶颈"

    **不适用场景（直接回答即可，不要创建任务）：**
    - 简单问答："现在几点"、"这个 JD 的要点是什么"
    - 单步查询："查一下张三的简历"、"这个职位有多少候选人"
    - 知识检索："公司报销流程是什么"

    Args:
        goal: 任务目标描述，越具体越好（包含部门、岗位、预算等关键约束）
        idempotency_key: 幂等键（可选，不传则自动从 user_id + goal 生成）
    """
    executor = get_task_executor()
    if executor is None:
        return (
            "❌ 任务执行器未初始化，无法创建后台任务。"
            "请联系管理员检查服务状态。"
        )

    try:
        user_id = config.get("configurable", {}).get("user_id", "unknown")
        session_id = config.get("configurable", {}).get("session_id", "")
    except Exception:
        user_id = "unknown"
        session_id = ""

    # ── 幂等检查 ──
    if not idempotency_key:
        idempotency_key = _make_task_idempotency_key(str(user_id), goal)

    existing = await executor.get_task(idempotency_key)
    if existing is not None:
        logger.info(
            "[TaskTool] 幂等命中 — 任务已存在: %s → %.80s",
            idempotency_key, goal,
        )
        return (
            f"⚠️ 相同的后台任务已存在！\n"
            f"\n📋 任务编号：`{existing.task_id}`"
            f"\n🎯 目标：{existing.goal}"
            f"\n📊 状态：{existing.status.value}"
            f"\n\n无需重复创建。你可以：\n"
            f"1. 问我「任务 {existing.task_id} 进展如何」来查询当前进度\n"
            f"2. 如果之前的任务已完成，我可以帮你查看结果"
        )

    try:
        handle = await executor.submit_task(
            goal=goal,
            user_id=str(user_id),
            session_id=str(session_id),
            task_id=idempotency_key,
        )

        logger.info(
            "[TaskTool] 后台任务已创建: %s (user=%s) → %.80s",
            handle.task_id, user_id, goal,
        )

        return (
            f"✅ 后台任务已创建！\n"
            f"\n📋 任务编号：`{handle.task_id}`"
            f"\n🎯 目标：{goal}"
            f"\n📊 状态：{handle.status.value}"
            f"\n\n任务将在后台自动拆解为执行计划并逐步推进。"
            f"你可以：\n"
            f"1. 继续和我聊其他事情，任务会独立执行\n"
            f"2. 随时问我「任务 {handle.task_id} 进展如何」来查询进度\n"
            f"3. 当任务需要审批时，我会主动通知你"
        )
    except Exception as e:
        logger.error("[TaskTool] 创建后台任务失败: %s", e, exc_info=True)
        return f"❌ 创建后台任务失败: {e}"
