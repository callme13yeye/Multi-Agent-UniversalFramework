"""报销审批工作流 — LangGraph 状态机

流程::

    START → validate
      → (无效) END
      → manager_approve [⚡INTERRUPT]
        → (拒绝) END
        → (≤1000) process_payment
        → (>1000) finance_review [⚡INTERRUPT]
          → (拒绝) END
          → (>5000) ceo_approve [⚡INTERRUPT]
            → (拒绝) END
            → process_payment
          → (≤5000) process_payment
        → process_payment → END
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

logger = logging.getLogger(__name__)


# ── 状态定义 ──────────────────────────────────────────────

class ReimbursementState(TypedDict):
    """报销工单状态"""
    ticket_id: str
    status: str                    # pending → approved/rejected → paid
    applicant_id: int
    amount: float
    category: str
    description: str
    rejection_reason: str | None
    approval_history: list[dict]
    pending_notification: dict | None  # 中断时给调用方的提示


# ── 节点函数 ──────────────────────────────────────────────

def validate(state: ReimbursementState) -> dict:
    """Step 1: 自动校验"""
    logger.info("[报销] 校验工单 %s", state["ticket_id"])
    if state["amount"] <= 0:
        return {
            "status": "rejected",
            "rejection_reason": "金额必须大于0",
            "approval_history": state.get("approval_history", []) + [{
                "step": "validate",
                "action": "rejected",
                "reason": "金额必须大于0",
                "timestamp": datetime.now().isoformat(),
            }],
        }
    # 校验通过，继续
    return {
        "status": "pending",
        "approval_history": state.get("approval_history", []) + [{
            "step": "validate",
            "action": "passed",
            "timestamp": datetime.now().isoformat(),
        }],
    }


def manager_approve(state: ReimbursementState) -> dict:
    """Step 2: 经理审批 — INTERRUPT 节点"""
    history = list(state.get("approval_history", []))
    # 防重入：如果已经审批过，跳过
    if any(h["step"] == "manager_approve" and h["action"] != "pending" for h in history):
        return {}

    pending = {
        "step": "manager_approve",
        "type": "approval",
        "role": "经理",
        "question": f"是否同意 #{state['ticket_id']} {state.get('description','')}（{state['amount']}元）?",
        "amount": state["amount"],
        "category": state["category"],
    }
    # ⚡ 在此中断，等待人工审批
    resume = interrupt(pending)

    action = resume.get("action", "rejected")
    comment = resume.get("comment", "") or (
        "经理审批通过" if action == "approved" else "经理拒绝"
    )
    now = datetime.now().isoformat()
    entry = {
        "step": "manager_approve",
        "action": action,
        "comment": comment,
        "actor": resume.get("actor", "manager"),
        "timestamp": now,
    }
    updates: dict[str, Any] = {
        "approval_history": history + [entry],
    }
    if action == "rejected":
        updates["status"] = "rejected"
        updates["rejection_reason"] = comment
    else:
        updates["status"] = "approved"
    return updates


def finance_review(state: ReimbursementState) -> dict:
    """Step 3: 财务审核 — INTERRUPT 节点（仅金额>1000时触发）"""
    history = list(state.get("approval_history", []))
    if any(h["step"] == "finance_review" and h["action"] != "pending" for h in history):
        return {}

    pending = {
        "step": "finance_review",
        "type": "approval",
        "role": "财务",
        "question": f"财务审核 #{state['ticket_id']}（{state['amount']}元），请核对票据和预算",
        "amount": state["amount"],
    }
    resume = interrupt(pending)

    action = resume.get("action", "rejected")
    comment = resume.get("comment", "") or (
        "财务审核通过" if action == "approved" else "财务拒绝"
    )
    now = datetime.now().isoformat()
    entry = {
        "step": "finance_review",
        "action": action,
        "comment": comment,
        "actor": resume.get("actor", "finance"),
        "timestamp": now,
    }
    updates: dict[str, Any] = {
        "approval_history": history + [entry],
    }
    if action == "rejected":
        updates["status"] = "rejected"
        updates["rejection_reason"] = comment
    else:
        updates["status"] = "approved"
    return updates


def ceo_approve(state: ReimbursementState) -> dict:
    """Step 4: CEO 审批 — INTERRUPT 节点（仅金额>5000时触发）"""
    history = list(state.get("approval_history", []))
    if any(h["step"] == "ceo_approve" and h["action"] != "pending" for h in history):
        return {}

    pending = {
        "step": "ceo_approve",
        "type": "approval",
        "role": "总经理",
        "question": f"总经理审批 #{state['ticket_id']}（{state['amount']}元），请确认",
        "amount": state["amount"],
    }
    resume = interrupt(pending)

    action = resume.get("action", "rejected")
    comment = resume.get("comment", "") or (
        "总经理审批通过" if action == "approved" else "总经理拒绝"
    )
    now = datetime.now().isoformat()
    entry = {
        "step": "ceo_approve",
        "action": action,
        "comment": comment,
        "actor": resume.get("actor", "ceo"),
        "timestamp": now,
    }
    updates: dict[str, Any] = {
        "approval_history": history + [entry],
    }
    if action == "rejected":
        updates["status"] = "rejected"
        updates["rejection_reason"] = comment
    else:
        updates["status"] = "approved"
    return updates


def process_payment(state: ReimbursementState) -> dict:
    """Step 5: 自动打款（终态）"""
    logger.info("[报销] 打款完成: %s 金额=%.2f", state["ticket_id"], state["amount"])
    return {
        "status": "paid",
        "approval_history": state.get("approval_history", []) + [{
            "step": "process_payment",
            "action": "auto_paid",
            "amount": state["amount"],
            "timestamp": datetime.now().isoformat(),
        }],
    }


# ── 条件路由 ──────────────────────────────────────────────

def after_validate(state: ReimbursementState) -> str:
    if state.get("status") == "rejected":
        return END
    return "manager_approve"


def after_manager(state: ReimbursementState) -> str:
    if state.get("status") == "rejected":
        return END
    if state["amount"] <= 1000:
        return "process_payment"
    return "finance_review"


def after_finance(state: ReimbursementState) -> str:
    if state.get("status") == "rejected":
        return END
    if state["amount"] > 5000:
        return "ceo_approve"
    return "process_payment"


def after_ceo(state: ReimbursementState) -> str:
    if state.get("status") == "rejected":
        return END
    return "process_payment"


# ── 构建图 ────────────────────────────────────────────────

def build_reimbursement_graph(checkpointer):
    """构建并编译报销工作流图"""
    builder = StateGraph(ReimbursementState)

    builder.add_node("validate", validate)
    builder.add_node("manager_approve", manager_approve)
    builder.add_node("finance_review", finance_review)
    builder.add_node("ceo_approve", ceo_approve)
    builder.add_node("process_payment", process_payment)

    builder.add_edge(START, "validate")
    builder.add_conditional_edges("validate", after_validate)
    builder.add_conditional_edges("manager_approve", after_manager)
    builder.add_conditional_edges("finance_review", after_finance)
    builder.add_conditional_edges("ceo_approve", after_ceo)
    builder.add_edge("process_payment", END)

    return builder.compile(checkpointer=checkpointer)
