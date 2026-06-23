# approval.py — 审批工具
# SubAgent 通过此工具发起人审（Human-in-the-Loop）请求。
#
# 工具将审批请求写入 LangGraph Store 并返回标记字符串。
# Supervisor 的 execute_node 检测标记后设置 pending_approval,
# 然后由专用 await_approval 节点调用 interrupt() 挂起任务。
# 人类通过 POST /tasks/{id}/resume 做出决策后，任务恢复执行。
#
# 架构决策：
# - 工具不调用 interrupt() — 嵌套 graph 的 interrupt 不可靠传播
# - 工具只写 Store + 返回标记 — Supervisor 负责 suspend/resume
# - Store namespace ("approval_requests",) — 服务重启不丢失

import hashlib
import json
import logging

from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

from app.tools._registry import register_tool

logger = logging.getLogger(__name__)

APPROVAL_MARKER = "[HUMAN_APPROVAL_REQUIRED]"


def _make_approval_key(task_id: str, step_id: str, title: str) -> str:
    """生成确定性幂等 key — 同一 task/step/title 的组合始终产生相同 key。

    解决三个场景：
    1. 重试不重复 — 同一步骤因超时重试不会创建第二个审批工单
    2. 恢复不重复 — 任务从快照恢复后不会创建重复审批
    3. 超时-实际成功不重复 — 网络超时但实际已写入的审批不会被覆盖
    """
    raw = f"{task_id}:{step_id}:{title}".encode("utf-8")
    return f"apr-{hashlib.sha256(raw).hexdigest()[:16]}"


@register_tool
@tool
async def async_request_approval(
    title: str,
    approver_role: str,
    context: str,
    task_id: str = "",
    step_id: str = "",
    config: RunnableConfig = None,
) -> str:
    """发起人审（Human-in-the-Loop）审批请求。

    当任务执行到需要人类决策的节点时（如 Offer 审批、敏感操作确认），
    使用此工具提交审批请求。系统会暂停任务执行，等待指定角色的审批人做出决策。

    审批完成后任务自动恢复执行，无需重新发起。

    **幂等保证**：同一 task_id + step_id + title 的组合只会创建一个审批工单。
    重复调用（重试/恢复）会直接返回已有审批，不会创建重复工单。

    **薪资分级审批规则（Offer 审批场景）：**
    - 薪资 ≤ 30,000元/月：approver_role="用人经理" — 单级审批
    - 薪资 30,001-50,000元/月：approver_role="部门负责人" — 需加签
    - 薪资 > 50,000元/月：approver_role="CEO" — 需最终审批

    **使用示例：**
    - Offer 审批：title="张明远 → AI大模型应用工程师 35K/月 Offer审批",
      approver_role="用人经理",
      context="候选人: 张明远\n职位: AI大模型应用工程师\n部门: 技术部\n薪资: 35000元/月\n备注: 面试表现优秀"

    - 敏感操作：title="批量删除候选人确认",
      approver_role="HR主管",
      context="即将删除 15 条候选人记录..."

    Args:
        title: 审批标题（简明扼要，会展示给审批人）
        approver_role: 审批人角色（如"用人经理"、"部门负责人"、"CEO"、"HR主管"）
        context: 审批上下文详细信息（候选人信息、薪资方案、操作说明等）
        task_id: 任务ID（用于幂等key，Agent 调用时传入）
        step_id: 步骤ID（用于幂等key，Agent 调用时传入）
    """
    # ── 从 config 获取 Store 和幂等信息 ──
    store = None
    if config and hasattr(config, "get"):
        configurable = config.get("configurable", {})
        store = configurable.get("store")
        # 如果调用方没传 task_id/step_id，尝试从 config 提取
        if not task_id:
            task_id = configurable.get("task_id", "")
        if not step_id:
            step_id = configurable.get("step_id", "")

    # ── 幂等 key 生成 ──
    if task_id and step_id:
        approval_id = _make_approval_key(task_id, step_id, title)
    else:
        # 降级：无幂等上下文时用随机ID（应尽量避免，但不阻塞功能）
        import uuid
        approval_id = f"apr-{uuid.uuid4().hex[:8]}"
        logger.warning(
            "[审批工具] 缺少 task_id/step_id，降级为非幂等随机ID: %s", approval_id
        )

    # ── 幂等检查：如果已存在同 key 的审批，直接返回已有结果 ──
    if store is not None:
        try:
            existing = await store.aget(("approval_requests",), approval_id)
            if existing and existing.value:
                logger.info(
                    "[审批工具] 幂等命中 — 审批已存在: %s → %.60s", approval_id, title
                )
                existing_data = existing.value
                return json.dumps(
                    {
                        "marker": APPROVAL_MARKER,
                        "approval_id": approval_id,
                        "title": title,
                        "approver_role": approver_role,
                        "context": context,
                        "status": existing_data.get("status", "pending"),
                        "idempotent": True,
                        "message": (
                            f"审批请求已存在（幂等返回）。状态: {existing_data.get('status')}。"
                            f"任务将暂停，等待 {approver_role} 审批。"
                        ),
                    },
                    ensure_ascii=False,
                )
        except Exception as e:
            logger.warning("[审批工具] 幂等检查异常（非致命，继续创建）: %s", e)

    approval_data = {
        "approval_id": approval_id,
        "title": title,
        "approver_role": approver_role,
        "context": context,
        "task_id": task_id,
        "step_id": step_id,
        "status": "pending",
        "decision": None,
        "comment": None,
        "decided_by": None,
        "decided_at": None,
    }

    # ── 持久化到 Store（如果可用） ──
    if store is not None:
        try:
            await store.aput(("approval_requests",), approval_id, approval_data)
            logger.info("[审批工具] 审批请求已存储: %s → %.60s", approval_id, title)
        except Exception as e:
            logger.error("[审批工具] Store 写入失败，入队死信: %s", e, exc_info=True)
            # ── 写入死信队列，后台定时重试 ──
            try:
                from app.tools.resources import get_dead_letter_queue
                dlq = get_dead_letter_queue()
                if dlq is not None:
                    await dlq.enqueue(
                        operation_name="async_request_approval",
                        operation_args={
                            "approval_id": approval_id,
                            "approval_data": approval_data,
                        },
                        error_message=str(e),
                        max_retries=5,
                    )
                    logger.info(
                        "[审批工具] 审批数据已写入死信队列等待重试: %s", approval_id
                    )
                else:
                    logger.warning(
                        "[审批工具] 死信队列未初始化，审批数据丢失: %s", approval_id
                    )
            except Exception as dlq_err:
                logger.error(
                    "[审批工具] 死信入队也失败，审批数据彻底丢失: %s → %s",
                    approval_id, dlq_err,
                )
    else:
        logger.warning("[审批工具] Store 不可用，审批数据仅存在于标记字符串中")

    # ── 返回标记字符串 — Supervisor 检测此标记后挂起任务 ──
    return json.dumps(
        {
            "marker": APPROVAL_MARKER,
            "approval_id": approval_id,
            "title": title,
            "approver_role": approver_role,
            "context": context,
            "idempotent": bool(task_id and step_id),
            "status": "pending",
            "message": (
                f"审批请求已提交。任务将暂停，等待 {approver_role} 审批。"
                f"审批完成后任务会自动恢复执行。请不要再执行任何操作，等待审批结果。"
            ),
        },
        ensure_ascii=False,
    )
