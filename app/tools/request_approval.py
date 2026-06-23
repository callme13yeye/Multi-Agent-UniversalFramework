# request_approval.py — Executor DeepAgent 的人审暂停工具
#
# 这是 HumanInTheLoop 审批流的关键工具。与 async_request_approval（Specialist 侧）
# 的分工：
#   - async_request_approval: Specialist 调用，写 Store，返回标记
#   - request_approval:      Executor 调用，触发 interrupt() 暂停任务
#
# 工作流：
#   1. Specialist 调 async_request_approval → 写 Store，返回 [HUMAN_APPROVAL_REQUIRED]
#   2. Executor LLM 看到 task 结果中的标记
#   3. Executor LLM 调 request_approval(approval_id="apr-xxx")
#   4. HumanInTheLoopMiddleware 拦截 → interrupt() → 任务挂起
#   5. 人审决策 → resume → 工具执行（读 Store，更新决策）
#   6. Executor 根据决策继续

import json
import logging

from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

from app.tools._registry import register_tool

logger = logging.getLogger(__name__)


@register_tool
@tool
async def request_approval(
    approval_id: str,
    title: str = "",
    config: RunnableConfig = None,
) -> str:
    """暂停任务并等待人类审批决策。

    当 Specialist 的输出中包含 ``[HUMAN_APPROVAL_REQUIRED]`` 标记时，
    Executor 必须调用此工具暂停任务，等待指定角色的审批人做出决策。

    审批完成后任务自动恢复执行，此工具返回审批人的决策。

    **使用方式**：
    1. 从 Specialist 返回的 JSON 中提取 ``approval_id``
    2. 调用此工具，传入 approval_id
    3. 此工具会暂停任务，人类做出决策后自动恢复
    4. 根据返回的决策继续执行或调整计划

    Args:
        approval_id: 审批请求 ID（从 Specialist 的 async_request_approval 输出中提取）
        title: 审批标题（可选，用于展示给审批人）
    """
    # ── 从 Store 读取审批请求 ──
    store = None
    if config and hasattr(config, "get"):
        configurable = config.get("configurable", {})
        store = configurable.get("store")

    # ── 读取已有审批数据 ──
    approval_data = None
    if store is not None:
        try:
            existing = await store.aget(("approval_requests",), approval_id)
            if existing and existing.value:
                approval_data = existing.value
                logger.info(
                    "[request_approval] 读取审批请求: %s → %.60s",
                    approval_id, approval_data.get("title", ""),
                )
        except Exception as e:
            logger.warning("[request_approval] Store 读取异常: %s", e)

    if approval_data is None:
        logger.warning(
            "[request_approval] 未找到审批请求 %s，返回默认决策", approval_id
        )
        return json.dumps({
            "status": "not_found",
            "approval_id": approval_id,
            "message": f"审批请求 {approval_id} 未找到，可能已过期或被删除。",
        }, ensure_ascii=False)

    # ── 获取当前决策状态 ──
    # 如果 human 审批通过 → status 已由 resume 流程更新
    # 此函数在 resume 后执行，此时 Store 中的 decision 已设置
    decision = approval_data.get("decision")
    decision_comment = approval_data.get("comment", "")
    decided_by = approval_data.get("decided_by", "审批人")
    decided_at = approval_data.get("decided_at", "")

    if decision == "approved":
        logger.info("[request_approval] 审批通过: %s", approval_id)
        return json.dumps({
            "status": "approved",
            "approval_id": approval_id,
            "title": approval_data.get("title", title),
            "decided_by": decided_by,
            "decided_at": decided_at,
            "comment": decision_comment,
            "message": f"审批已通过。审批人: {decided_by}。可以继续执行后续步骤。",
        }, ensure_ascii=False)
    elif decision == "rejected":
        logger.info("[request_approval] 审批拒绝: %s", approval_id)
        return json.dumps({
            "status": "rejected",
            "approval_id": approval_id,
            "title": approval_data.get("title", title),
            "decided_by": decided_by,
            "decided_at": decided_at,
            "comment": decision_comment,
            "message": (
                f"审批被 {decided_by} 拒绝。"
                + (f"原因: {decision_comment}" if decision_comment else "")
                + "请根据拒绝原因调整方案，或终止任务。"
            ),
        }, ensure_ascii=False)
    else:
        # pending 状态 — 不应该到达这里（interrupt 保证 resume 后 decision 已设置）
        logger.warning("[request_approval] 审批状态异常: %s → status=%s", approval_id, decision)
        return json.dumps({
            "status": "pending",
            "approval_id": approval_id,
            "title": approval_data.get("title", title),
            "message": "审批仍在等待中，请稍后重试。",
        }, ensure_ascii=False)
