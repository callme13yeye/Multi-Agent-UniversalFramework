"""工作流 Pydantic 模型"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ── 请求模型 ──────────────────────────────────────────────

class WorkflowCreateRequest(BaseModel):
    """创建工单请求"""
    workflow_type: str = Field(default="reimbursement", description="工作流类型")
    title: str = Field(..., description="工单标题")
    amount: float = Field(..., gt=0, description="报销金额（元）")
    category: Literal["差旅", "办公用品", "招待", "交通", "其他"] = Field(
        default="差旅", description="费用类别"
    )
    description: str = Field(default="", description="报销说明")


class WorkflowActionRequest(BaseModel):
    """对工单执行操作请求"""
    action: Literal["approved", "rejected"] = Field(..., description="审批动作")
    comment: Optional[str] = Field(default=None, description="审批意见")
    actor: Optional[str] = Field(default=None, description="审批人角色")


# ── 响应模型 ──────────────────────────────────────────────

class ApprovalRecord(BaseModel):
    """审批记录"""
    step: str
    action: str
    comment: Optional[str] = None
    actor: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class WorkflowTicketResponse(BaseModel):
    """工单详情响应"""
    ticket_id: str
    workflow_type: str
    title: str
    status: str
    current_step: str
    form_data: dict
    approval_history: list[ApprovalRecord]
    pending_action: Optional[dict] = None
    created_by: int
    created_at: str
    updated_at: str


class WorkflowListResponse(BaseModel):
    """工单列表响应"""
    tickets: list[WorkflowTicketResponse]
    total: int
