# app/routes/task_routes.py
"""后台任务管理路由 — 任务查询、审批恢复、取消、事件流、执行日志。"""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse as SSE

from app.auth import get_current_user, get_current_user_sse
from app.harness import TaskStatus
from app.harness.task_context import TaskContextManager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tasks", tags=["Tasks"])


# ── Pydantic 模型 ───────────────────────────────────

class TaskResponse(BaseModel):
    task_id: str
    thread_id: str
    goal: str
    status: str
    plan: list[dict] = []
    progress: str = ""
    result_summary: str = ""
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""


class TaskResumeRequest(BaseModel):
    action: str = Field(..., description="操作: approved / rejected / provide_info")
    comment: str | None = Field(None, description="备注说明")
    data: dict | None = Field(None, description="附加数据")


# ── 辅助函数 ─────────────────────────────────────

def _get_executor(req: Request):
    """获取 TaskExecutor，未就绪时抛出 503。"""
    executor = getattr(req.app.state, "task_executor", None)
    if executor is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return executor


def _handle_to_response(h) -> TaskResponse:
    """将 TaskHandle 转为 API 响应模型。"""
    return TaskResponse(
        task_id=h.task_id,
        thread_id=h.thread_id,
        goal=h.goal,
        status=h.status.value,
        plan=h.plan,
        progress=h.progress,
        result_summary=h.result_summary,
        error_message=h.error_message,
        created_at=h.created_at,
        updated_at=h.updated_at,
    )


# ── 端点 ─────────────────────────────────────────

@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    req: Request,
    status_filter: str | None = None,
    current_user: int = Depends(get_current_user),
):
    """列出当前用户的后台任务。

    Args:
        status_filter: 可选状态筛选 (created/executing/waiting_human/completed/failed)
    """
    executor = _get_executor(req)

    filter_enum = None
    if status_filter:
        try:
            filter_enum = TaskStatus(status_filter)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"无效状态: {status_filter}")

    handles = await executor.list_tasks(status_filter=filter_enum)
    return [_handle_to_response(h) for h in handles]


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    req: Request,
    current_user: int = Depends(get_current_user),
):
    """查询任务状态与进度。"""
    executor = _get_executor(req)

    handle = await executor.get_task(task_id)
    if handle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在: {task_id}")

    return _handle_to_response(handle)


@router.post("/{task_id}/resume", response_model=TaskResponse)
async def resume_task(
    task_id: str,
    request: TaskResumeRequest,
    req: Request,
    current_user: int = Depends(get_current_user),
):
    """恢复被挂起的任务（如审批决策、补充信息）。

    当任务状态为 waiting_human 时，使用此端点提供决策或信息，
    Agent 会从上次中断处继续执行。
    """
    executor = _get_executor(req)

    handle = await executor.get_task(task_id)
    if handle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在: {task_id}")

    resume_data = {"action": request.action}
    if request.comment:
        resume_data["comment"] = request.comment
    if request.data:
        resume_data.update(request.data)

    handle = await executor.resume_task(task_id, resume_data)
    return _handle_to_response(handle)


@router.delete("/{task_id}")
async def cancel_task(
    task_id: str,
    req: Request,
    current_user: int = Depends(get_current_user),
):
    """取消一个正在执行或挂起的任务。"""
    executor = _get_executor(req)

    cancelled = await executor.cancel_task(task_id)
    if not cancelled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在: {task_id}")

    return {"status": "cancelled", "task_id": task_id}


@router.get("/{task_id}/events")
async def task_events(
    task_id: str,
    req: Request,
    current_user: int = Depends(get_current_user_sse),
):
    """SSE 端点 — 实时推送任务状态变更事件。

    前端可以通过 EventSource 订阅此端点获取任务进度更新。
    """
    executor = _get_executor(req)

    event_bus = getattr(req.app.state, "event_bus", None)
    if event_bus is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def handler(data: dict):
            if data.get("task_id") == task_id:
                await queue.put(data)

        # 注册事件处理器并持有取消函数，确保连接断开时清理
        unsubs = [
            event_bus.subscribe("task.executing", handler),
            event_bus.subscribe("task.interrupted", handler),
            event_bus.subscribe("task.completed", handler),
            event_bus.subscribe("task.failed", handler),
        ]

        try:
            while True:
                try:
                    event_data = await asyncio.wait_for(queue.get(), timeout=30)
                    yield {
                        "event": event_data.get("type", "task_update"),
                        "data": json.dumps(event_data, ensure_ascii=False, default=str),
                    }
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": "{}"}
        except asyncio.CancelledError:
            pass
        finally:
            for unsub in unsubs:
                unsub()

    return SSE(event_stream())


# ── 任务执行日志（Journal） ─────────────────────────

@router.get("/{task_id}/journal")
async def get_task_journal(
    task_id: str,
    req: Request,
    limit: int = 50,
    current_user: int = Depends(get_current_user),
):
    """获取任务的执行日志（journal）。

    Journal 是任务执行过程的结构化记录，每条记录包含时间戳、事件类型、
    人类可读摘要和结构化详情。与 progress 不同，journal 不受
    SummarizationMiddleware 压缩影响，提供完整的执行链路可观测性。

    Args:
        task_id: 任务 ID
        limit: 返回最近 N 条记录（默认 50，传 0 返回全部）
    """
    executor = _get_executor(req)

    handle = await executor.get_task(task_id)
    if handle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在: {task_id}")

    context_manager: TaskContextManager = getattr(req.app.state, "context_manager", None)
    if context_manager is None:
        return {"task_id": task_id, "journal": [], "count": 0}

    entries = await context_manager.read_journal(task_id, limit=limit)
    return {
        "task_id": task_id,
        "goal": handle.goal,
        "status": handle.status.value,
        "journal": [e.to_dict() for e in entries],
        "count": len(entries),
    }
