"""工作流引擎 — 创建/恢复/查询长周期业务流程"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from langgraph.types import Command

from app.workflow.models import ApprovalRecord, WorkflowTicketResponse

logger = logging.getLogger(__name__)

# 已注册的工作流构建器
_WORKFLOW_REGISTRY: dict[str, Any] = {}


def register_workflow(workflow_type: str, graph_builder_fn):
    """注册一个工作流类型

    Args:
        workflow_type: 唯一标识，如 "reimbursement"
        graph_builder_fn: 接收 checkpointer 返回 CompiledStateGraph 的函数
    """
    _WORKFLOW_REGISTRY[workflow_type] = graph_builder_fn
    logger.info("工作流注册: %s", workflow_type)


def get_workflow_types() -> list[str]:
    return list(_WORKFLOW_REGISTRY.keys())


class WorkflowEngine:
    """工作流引擎 — 管理长周期业务流程的生命周期"""

    def __init__(
        self,
        checkpointer: AsyncPostgresSaver,
        store: AsyncPostgresStore,
    ):
        self.checkpointer = checkpointer
        self.store = store
        # 懒编译: workflow_type → CompiledStateGraph
        self._graphs: dict[str, Any] = {}

    def _get_graph(self, workflow_type: str):
        if workflow_type not in self._graphs:
            builder = _WORKFLOW_REGISTRY.get(workflow_type)
            if not builder:
                raise ValueError(f"未知工作流类型: {workflow_type}，可用: {list(_WORKFLOW_REGISTRY.keys())}")
            self._graphs[workflow_type] = builder(self.checkpointer)
        return self._graphs[workflow_type]

    # ── 元数据存储 ──────────────────────────────────────
    _META_NS = ("workflows",)

    @staticmethod
    def _user_ns(user_id: int) -> tuple[str, ...]:
        return ("workflows", str(user_id))

    async def _save_meta(self, user_id: int, ticket_id: str, data: dict):
        await self.store.aput(self._user_ns(user_id), ticket_id, data)

    async def _get_meta(self, user_id: int, ticket_id: str) -> dict | None:
        item = await self.store.aget(self._user_ns(user_id), ticket_id)
        return item.value if item else None

    # ── 对外接口 ──────────────────────────────────────────

    async def create_ticket(
        self,
        workflow_type: str,
        user_id: int,
        title: str,
        form_data: dict,
    ) -> WorkflowTicketResponse:
        """创建工单并执行到第一个中断点"""
        import uuid

        ticket_id = f"wf-{workflow_type}-{uuid.uuid4().hex[:8]}"
        graph = self._get_graph(workflow_type)

        initial: dict[str, Any] = {
            "ticket_id": ticket_id,
            "status": "pending",
            "applicant_id": user_id,
            "amount": form_data["amount"],
            "category": form_data.get("category", "其他"),
            "description": form_data.get("description", ""),
            "rejection_reason": None,
            "approval_history": [],
            "pending_notification": None,
        }

        config = {"configurable": {"thread_id": ticket_id}}

        try:
            result = await graph.ainvoke(initial, config)
        except Exception as e:
            logger.error("[工作流] 首次执行异常 %s: %s", ticket_id, e)
            raise

        # 保存元数据
        now = datetime.now().isoformat()
        await self._save_meta(user_id, ticket_id, {
            "ticket_id": ticket_id,
            "workflow_type": workflow_type,
            "title": title,
            "status": result.get("status", "pending"),
            "created_by": user_id,
            "created_at": now,
            "updated_at": now,
        })

        return await self._build_response(user_id, ticket_id, graph, config)

    async def take_action(
        self,
        ticket_id: str,
        user_id: int,
        action: str,
        comment: str | None = None,
        actor: str | None = None,
    ) -> WorkflowTicketResponse:
        """对处于中断状态的工单执行审批/拒绝操作"""
        meta = await self._get_meta(user_id, ticket_id)
        if not meta:
            raise ValueError(f"工单不存在: {ticket_id}")

        workflow_type = meta["workflow_type"]
        graph = self._get_graph(workflow_type)
        config = {"configurable": {"thread_id": ticket_id}}

        resume_data: dict[str, Any] = {"action": action}
        if comment:
            resume_data["comment"] = comment
        if actor:
            resume_data["actor"] = actor

        try:
            result = await graph.ainvoke(Command(resume=resume_data), config)
        except Exception as e:
            logger.error("[工作流] 恢复执行异常 %s: %s", ticket_id, e)
            raise

        # 更新元数据
        await self._save_meta(user_id, ticket_id, {
            **meta,
            "status": result.get("status", meta["status"]),
            "updated_at": datetime.now().isoformat(),
        })

        return await self._build_response(user_id, ticket_id, graph, config)

    async def get_ticket(
        self, ticket_id: str, user_id: int
    ) -> WorkflowTicketResponse | None:
        """获取工单最新状态"""
        meta = await self._get_meta(user_id, ticket_id)
        if not meta:
            return None

        workflow_type = meta["workflow_type"]
        graph = self._get_graph(workflow_type)
        config = {"configurable": {"thread_id": ticket_id}}

        return await self._build_response(user_id, ticket_id, graph, config)

    async def list_tickets(
        self, user_id: int, limit: int = 50, offset: int = 0
    ) -> list[WorkflowTicketResponse]:
        """列出用户的所有工单"""
        items = await self.store.asearch(
            self._user_ns(user_id),
            limit=limit,
            offset=offset,
        )
        results = []
        for item in items:
            meta = item.value
            ticket_id = meta.get("ticket_id", item.key)
            try:
                resp = await self.get_ticket(ticket_id, user_id)
                if resp:
                    results.append(resp)
            except Exception as e:
                logger.warning("加载工单 %s 失败: %s", ticket_id, e)
                # 降级：至少返回元数据
                results.append(WorkflowTicketResponse(
                    ticket_id=ticket_id,
                    workflow_type=meta.get("workflow_type", "unknown"),
                    title=meta.get("title", ""),
                    status=meta.get("status", "unknown"),
                    current_step="unknown",
                    form_data={},
                    approval_history=[],
                    created_by=user_id,
                    created_at=meta.get("created_at", ""),
                    updated_at=meta.get("updated_at", ""),
                ))
        return results

    async def _build_response(
        self,
        user_id: int,
        ticket_id: str,
        graph: Any,
        config: dict,
    ) -> WorkflowTicketResponse:
        """从图状态构建 API 响应"""
        state = await graph.aget_state(config)
        values = state.values if state else {}
        meta = await self._get_meta(user_id, ticket_id) or {}

        # 判断是否处于中断状态
        pending_action = None
        if state and state.next:
            pending_action = values.get("pending_notification") or {
                "step": next(iter(state.next), None),
                "type": "approval",
            }

        history = values.get("approval_history", [])
        approval_records = []
        for h in history:
            if isinstance(h, dict):
                approval_records.append(ApprovalRecord(
                    step=h.get("step", ""),
                    action=h.get("action", ""),
                    comment=h.get("comment"),
                    actor=h.get("actor"),
                    timestamp=h.get("timestamp", ""),
                ))

        return WorkflowTicketResponse(
            ticket_id=ticket_id,
            workflow_type=meta.get("workflow_type", "reimbursement"),
            title=meta.get("title", ""),
            status=values.get("status", meta.get("status", "unknown")),
            current_step=str(next(iter(state.next))) if state and state.next else "completed",
            form_data={
                "amount": values.get("amount", meta.get("amount")),
                "category": values.get("category", ""),
                "description": values.get("description", ""),
            },
            approval_history=approval_records,
            pending_action=pending_action,
            created_by=user_id,
            created_at=meta.get("created_at", ""),
            updated_at=meta.get("updated_at", ""),
        )
