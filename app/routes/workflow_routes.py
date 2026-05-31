"""工作流 REST API — 创建/审批/查询工单"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import get_current_user
from app.workflow.engine import WorkflowEngine
from app.workflow.models import (
    WorkflowActionRequest,
    WorkflowCreateRequest,
    WorkflowListResponse,
    WorkflowTicketResponse,
)

logger = logging.getLogger(__name__)


def create_workflow_router(engine: WorkflowEngine) -> APIRouter:
    """创建工作流路由（注入 engine 实例）"""
    router = APIRouter(prefix="/workflow", tags=["workflow"])

    @router.post("/create", response_model=WorkflowTicketResponse)
    async def create_ticket(
        req: WorkflowCreateRequest,
        current_user: int = Depends(get_current_user),
    ):
        """创建报销工单"""
        try:
            return await engine.create_ticket(
                workflow_type=req.workflow_type,
                user_id=current_user,
                title=req.title,
                form_data={
                    "amount": req.amount,
                    "category": req.category,
                    "description": req.description or req.title,
                },
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/{ticket_id}/action", response_model=WorkflowTicketResponse)
    async def take_action(
        ticket_id: str,
        action_req: WorkflowActionRequest,
        current_user: int = Depends(get_current_user),
    ):
        """审批/拒绝工单"""
        try:
            return await engine.take_action(
                ticket_id=ticket_id,
                user_id=current_user,
                action=action_req.action,
                comment=action_req.comment,
                actor=action_req.actor,
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @router.get("/{ticket_id}", response_model=WorkflowTicketResponse)
    async def get_ticket(
        ticket_id: str,
        current_user: int = Depends(get_current_user),
    ):
        """获取工单详情"""
        ticket = await engine.get_ticket(ticket_id, current_user)
        if not ticket:
            raise HTTPException(status_code=404, detail="工单不存在")
        return ticket

    @router.get("/list", response_model=WorkflowListResponse)
    async def list_tickets(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        current_user: int = Depends(get_current_user),
    ):
        """列出我的工单"""
        offset = (page - 1) * page_size
        tickets = await engine.list_tickets(
            user_id=current_user, limit=page_size, offset=offset
        )
        return WorkflowListResponse(tickets=tickets, total=len(tickets))

    return router
