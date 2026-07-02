"""后台任务执行器 — 让 Agent 脱离 HTTP 请求-响应循环自主运行。

核心设计：
    - 每个"任务"是一个独立的 LangGraph thread
    - Executor 用 asyncio.Task 在后台驱动 Executor DeepAgent 执行
    - Agent 通过 interrupt() 挂起时，Executor 将状态持久化并休眠
    - 外部事件（用户回复、审批完成）通过 EventBus 唤醒
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph
    from langgraph.store.postgres.aio import AsyncPostgresStore
    from app.harness.task_context import TaskContextManager

from app.harness.event_bus import EventBus
from app.harness.task_context import JournalEntry

logger = logging.getLogger(__name__)


class ApprovalNotHandledError(Exception):
    """Executor LLM 在连续多轮中未处理审批标记时触发。

    这是 P0 兜底机制：正常情况下 Executor LLM 应在看到
    ``[HUMAN_APPROVAL_REQUIRED]`` 后立即调用 ``request_approval``。
    如果 LLM 因幻觉/上下文压缩/推理偏差连续忽略该标记，
    此异常强制将任务转入 WAITING_HUMAN 状态。
    """
    def __init__(self, task_id: str, rounds: int, approval_id: str = ""):
        self.task_id = task_id
        self.rounds = rounds
        self.approval_id = approval_id
        super().__init__(
            f"任务 {task_id}: 已连续 {rounds} 轮未处理审批标记 "
            f"[HUMAN_APPROVAL_REQUIRED]，强制中断"
        )




class TaskStatus(str, Enum):
    CREATED = "created"
    EXECUTING = "executing"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskHandle:
    """后台任务的句柄 — 对外暴露的任务状态。"""
    task_id: str
    thread_id: str
    goal: str
    user_id: str = ""                # 任务所属用户（快照恢复需要）
    session_id: str = ""             # 任务所属对话 session（完成后回写结果）
    status: TaskStatus = TaskStatus.CREATED
    plan: list[dict] = field(default_factory=list)
    progress: str = ""               # 当前进度描述
    result_summary: str = ""         # 完成后的总结
    error_message: str = ""
    approval_id: str = ""            # 当前挂起的审批请求 ID（HITL 时使用）
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "goal": self.goal,
            "user_id": self.user_id,
            "status": self.status.value,
            "plan": self.plan,
            "progress": self.progress,
            "result_summary": self.result_summary,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TaskExecutor:
    """后台任务执行器。

    职责：
        1. 接收任务（goal）→ 创建执行线程
        2. 在后台 asyncio.Task 中驱动 Supervisor 执行循环
        3. 处理 interrupt 挂起 → 持久化状态 → 等待外部 resume
        4. 任务生命周期事件通过 EventBus 广播

    使用方式::

        executor = TaskExecutor(checkpointer=cp, store=store, event_bus=bus, executor_agent=agent)
        handle = await executor.submit_task(
            goal="帮产品部招一个高级后端工程师",
            user_id="42",
        )
        # → 立即返回 TaskHandle，任务在后台执行
        # → 通过 GET /tasks/{task_id} 查询进度
    """

    def __init__(
        self,
        store: "AsyncPostgresStore",
        event_bus: EventBus,
        executor_agent: "CompiledStateGraph",
        context_manager: "TaskContextManager | None" = None,
    ):
        self.store = store
        self.event_bus = event_bus
        self.executor_agent = executor_agent  # Executor DeepAgent — LLM 驱动的执行引擎
        self.context_manager = context_manager    # 可选 — 用于快照持久化

        # 运行时状态
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._handles: dict[str, TaskHandle] = {}
        self._draining: bool = False  # 排干模式 — 拒绝新任务
        self._msg_counts: dict[str, int] = {}  # 每个任务已处理的消息数（用于 journal diff）
        self._journal_steps: dict[str, int] = {}  # 每个任务的 journal step 计数器
        self._unhandled_approval_rounds: dict[str, int] = {}  # 每个任务连续未处理审批标记的轮数

    # ── 对外接口 ──────────────────────────────────────────

    async def submit_task(
        self,
        goal: str,
        user_id: str,
        session_id: str = "",
        context: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> TaskHandle:
        """提交一个后台任务。立即返回 TaskHandle。

        Args:
            goal: 任务目标
            user_id: 用户 ID
            context: 附加上下文
            task_id: 幂等 key — 传入则复用，不传则自动生成

        Raises:
            RuntimeError: 如果执行器处于排干模式（正在优雅关闭）
        """
        if self._draining:
            raise RuntimeError(
                "任务执行器正在关闭，不再接受新任务。请稍后重试。"
            )

        import uuid

        if task_id is None:
            task_id = f"task-{uuid.uuid4().hex[:8]}"

        # ── 幂等检查：如果任务已存在，直接返回已有 handle ──
        existing = self._handles.get(task_id)
        if existing is not None:
            logger.info("[TaskExecutor] 幂等命中 — 任务已存在: %s", task_id)
            return existing

        thread_id = task_id

        handle = TaskHandle(
            task_id=task_id,
            thread_id=thread_id,
            goal=goal,
            user_id=user_id,
            session_id=session_id,
            status=TaskStatus.CREATED,
        )
        self._handles[task_id] = handle

        # 后台启动
        bg_task = asyncio.create_task(
            self._execute_loop(handle, user_id, context)
        )
        self._running_tasks[task_id] = bg_task

        logger.info("[TaskExecutor] 任务已提交: %s → %.60s", task_id, goal)

        await self.event_bus.publish("task.created", handle.to_dict())
        return handle

    async def resume_task(
        self,
        task_id: str,
        resume_data: dict[str, Any],
    ) -> TaskHandle:
        """恢复被挂起的任务（如 Human-in-the-Loop 审批完成）。

        支持两种恢复模式：
        1. 普通 HITL 审批 → Command(resume=...) 恢复 LangGraph 执行
        """
        handle = self._handles.get(task_id)
        if not handle:
            raise ValueError(f"任务不存在: {task_id}")

        if handle.status != TaskStatus.WAITING_HUMAN:
            raise ValueError(f"任务 {task_id} 状态为 {handle.status.value}，不能恢复")

        action = resume_data.get("action", "approved")
        comment = resume_data.get("comment", "")

        # ── 普通 HITL 恢复 ──
        logger.info("[TaskExecutor] 恢复任务: %s ← %s", task_id, resume_data)

        from langgraph.types import Command

        bg_task = asyncio.create_task(
            self._resume_loop(handle, resume_data)
        )
        self._running_tasks[task_id] = bg_task

        await self.event_bus.publish("task.resumed", handle.to_dict())
        return handle

    async def cancel_task(self, task_id: str) -> bool:
        """取消任务。"""
        handle = self._handles.get(task_id)
        if not handle:
            return False

        bg_task = self._running_tasks.pop(task_id, None)
        if bg_task and not bg_task.done():
            bg_task.cancel()
            try:
                await bg_task
            except asyncio.CancelledError:
                pass

        handle.status = TaskStatus.CANCELLED
        handle.updated_at = datetime.now().isoformat()
        self._msg_counts.pop(task_id, None)
        self._journal_steps.pop(task_id, None)
        self._unhandled_approval_rounds.pop(task_id, None)
        # ── 清理快照（终端状态） ──
        if self.context_manager is not None:
            await self.context_manager.delete_snapshot(task_id)
        await self.event_bus.publish("task.cancelled", handle.to_dict())
        logger.info("[TaskExecutor] 任务已取消: %s", task_id)
        return True

    async def get_task(self, task_id: str) -> TaskHandle | None:
        """获取任务状态。"""
        return self._handles.get(task_id)

    async def list_tasks(
        self,
        status_filter: TaskStatus | None = None,
    ) -> list[TaskHandle]:
        """列出任务。可筛选。"""
        handles = list(self._handles.values())
        if status_filter:
            handles = [h for h in handles if h.status == status_filter]
        return handles

    # ── 启动恢复 ──────────────────────────────────────────

    async def recover_tasks(self):
        """启动时扫描 Store 中未完成的快照，自动重建 asyncio.Task。

        应对场景：服务器重启 / 崩溃后，未完成的后台任务需要自动恢复执行。

        恢复策略：
        - ``waiting_human`` → 只注册 TaskHandle，不启动 asyncio.Task（等待人审恢复）
        - ``executing`` / ``created`` / ``planning`` → 重建 asyncio.Task 继续执行
        - ``completed`` / ``failed`` / ``cancelled`` → 跳过（终端状态，仅清理快照）
        """
        if self.store is None or self.context_manager is None:
            return

        try:
            items = await self.store.asearch(("task_snapshots",), limit=500)
        except Exception as e:
            logger.warning("[TaskExecutor] 快照扫描失败，跳过恢复: %s", e)
            return

        recovered = 0
        cleaned = 0

        for item in items:
            if not item.value:
                continue

            snap = item.value
            task_id = snap.get("task_id", "")
            goal = snap.get("goal", "")
            user_id = snap.get("user_id", "")
            session_id = snap.get("session_id", "")
            status = snap.get("status", "")
            approval_id = snap.get("approval_id", "")

            if not task_id or not goal:
                continue

            # 终端状态 — 清理残留快照
            if status in ("completed", "failed", "cancelled"):
                await self.context_manager.delete_snapshot(task_id)
                cleaned += 1
                continue

            logger.info(
                "[TaskExecutor] 恢复任务: %s status=%s goal=%.60s",
                task_id, status, goal,
            )

            # 映射状态
            try:
                task_status = TaskStatus(status) if status else TaskStatus.CREATED
            except ValueError:
                task_status = TaskStatus.CREATED

            handle = TaskHandle(
                task_id=task_id,
                thread_id=task_id,
                goal=goal,
                user_id=user_id,
                session_id=session_id,
                status=task_status,
                approval_id=approval_id,
            )
            self._handles[task_id] = handle

            if task_status == TaskStatus.WAITING_HUMAN:
                # 正在等人审批 — 只注册，不启动 asyncio.Task
                # 人类通过 POST /tasks/{id}/resume 恢复
                handle.progress = snap.get("recovery_hint", "等待人类审批决策")
                await self.event_bus.publish("task.recovered", handle.to_dict())
                recovered += 1
            elif task_status in (
                TaskStatus.EXECUTING,
                TaskStatus.CREATED,
            ):
                # 执行中被打断 — 重启 asyncio.Task
                bg_task = asyncio.create_task(
                    self._execute_loop(handle, user_id, None)
                )
                self._running_tasks[task_id] = bg_task
                recovered += 1

        if recovered > 0:
            logger.info(
                "[TaskExecutor] 启动恢复完成: %d 个任务已恢复%s",
                recovered,
                f"（清理 {cleaned} 个终端快照）" if cleaned > 0 else "",
            )
        elif cleaned > 0:
            logger.info(
                "[TaskExecutor] 启动时清理 %d 个终端状态残留快照", cleaned,
            )

    # ── 优雅关闭 ──────────────────────────────────────────

    async def drain(self):
        """排干模式 — 停止接受新任务，不影响已有任务。"""
        self._draining = True
        logger.info("[TaskExecutor] 进入排干模式 — 拒绝新任务，等待运行中任务完成")

    async def shutdown(self, timeout: float = 30.0):
        """优雅关闭执行器。

        关闭顺序：
        1. 排干 — 拒绝新任务
        2. 等待运行中任务完成（有超时）
        3. 超时未完成的 → 打快照后 cancel
        """
        await self.drain()

        running = list(self._running_tasks.items())
        if not running:
            logger.info("[TaskExecutor] 无运行中任务，关闭完成")
            return

        logger.info(
            "[TaskExecutor] 等待 %d 个运行中任务完成（超时 %.0fs）",
            len(running), timeout,
        )

        # 等待所有任务完成或超时
        done, pending = await asyncio.wait(
            [t for _, t in running],
            timeout=timeout,
        )

        # 超时未完成的任务 — 保存快照后强制取消
        if pending:
            logger.warning(
                "[TaskExecutor] %d 个任务未在 %.0fs 内完成，保存快照后取消",
                len(pending), timeout,
            )
            for task_id, bg_task in running:
                if bg_task in pending:
                    handle = self._handles.get(task_id)
                    if handle and handle.status not in (
                        TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED
                    ):
                        # ── 真正持久化快照到 Store，重启后可恢复 ──
                        if self.context_manager is not None:
                            from app.harness.task_context import TaskSnapshot

                            snapshot = TaskSnapshot(
                                task_id=task_id,
                                goal=handle.goal,
                                user_id=handle.user_id,
                                session_id=handle.session_id,
                                current_node=handle.status.value,
                                current_step_index=0,
                                plan=[],
                                status=handle.status.value,
                                approval_id=handle.approval_id,
                                recovery_hint=f"服务器关闭时保存 — 原状态: {handle.status.value}",
                            )
                            try:
                                await self.context_manager.save_snapshot(snapshot)
                                logger.info(
                                    "[TaskExecutor] 任务 %s 快照已持久化 (status=%s)",
                                    task_id, handle.status.value,
                                )
                            except Exception as e:
                                logger.error(
                                    "[TaskExecutor] 任务 %s 快照保存失败: %s", task_id, e
                                )

                        handle.progress = "服务器关闭中 — 已保存快照"
                        handle.updated_at = datetime.now().isoformat()

                    bg_task.cancel()
                    try:
                        await bg_task
                    except (asyncio.CancelledError, Exception):
                        pass

        logger.info("[TaskExecutor] 关闭完成")

    # ── 内部执行循环 ──────────────────────────────────────

    async def _execute_loop(
        self,
        handle: TaskHandle,
        user_id: str,
        context: dict[str, Any] | None,
    ):
        """后台执行主循环。

        这是 Harness 层的核心 — 驱动 Executor DeepAgent 在后台运行。
        Executor DeepAgent 是 LLM 驱动的：它自主规划、逐步委托 Specialist、
        根据结果动态调整、需要审批时通过 request_approval 工具暂停。
        """
        # ── 继承请求 trace_id，追加 task_id 形成子链路 ──
        from app.harness.trace_context import trace_context
        trace_context.set_task_context(handle.task_id)

        config = {
            "configurable": {
                "thread_id": handle.thread_id,
                "user_id": user_id,
                "task_id": handle.task_id,
                "store": self.store,  # 供 request_approval 工具读取审批数据
            }
        }

        try:
            handle.status = TaskStatus.EXECUTING
            handle.updated_at = datetime.now().isoformat()
            await self.event_bus.publish("task.executing", handle.to_dict())

            # ── 构造执行消息（区分新任务和恢复任务） ──
            is_recovery = False
            snapshot_info = ""
            try:
                if self.store is not None:
                    snap_item = await self.store.aget(("task_snapshots",), handle.task_id)
                    if snap_item and snap_item.value:
                        snap = snap_item.value
                        is_recovery = True
                        snapshot_info = (
                            f"## 任务恢复\n\n"
                            f"服务器重启后，此任务从快照恢复。请检查当前进度并继续执行未完成的工作。\n\n"
                            f"恢复信息：\n"
                            f"- 之前状态: {snap.get('status', 'unknown')}\n"
                            f"- 已完成步骤数: {snap.get('current_step_index', 0)}\n"
                            f"- 计划: {snap.get('plan', [])}\n"
                        )
                        logger.info(
                            "[TaskExecutor] 检测到快照 — 恢复任务: %s (status=%s)",
                            handle.task_id, snap.get('status', 'unknown'),
                        )
            except Exception:
                pass

            # ── 注入 journal 摘要（有 journal 时优先，比快照信息更结构化） ──
            journal_summary = ""
            if self.context_manager is not None:
                try:
                    journal_summary = await self.context_manager.build_journal_summary(
                        handle.task_id
                    )
                except Exception:
                    pass

            from langchain_core.messages import HumanMessage

            if is_recovery:
                execution_prompt = snapshot_info
                if journal_summary:
                    execution_prompt += "\n\n" + journal_summary
            else:
                execution_prompt = self._build_execution_prompt(handle.goal, context)

            initial_state = {
                "messages": [
                    HumanMessage(content=execution_prompt)
                ],
            }

            async for event in self.executor_agent.astream(
                initial_state,
                config=config,
                stream_mode="updates",
            ):
                # 检查 interrupt（Human-in-the-Loop）
                if "__interrupt__" in event:
                    interrupt_data = event["__interrupt__"]
                    await self._handle_interrupt(handle, interrupt_data)
                    return  # 挂起，等待外部 resume

                # ── 将 state 中的 progress 同步回 handle + journal ──
                self._process_event(handle, event)

            # 正常完成
            handle.status = TaskStatus.COMPLETED
            handle.updated_at = datetime.now().isoformat()
            # 提取最终结果作为总结
            if handle.progress and not handle.result_summary:
                handle.result_summary = handle.progress
            # ── 写入最终 journal 条目 ──
            await self._write_completion_journal(handle)
            # ── 清理快照（任务已完成，不再需要恢复） ──
            if self.context_manager is not None:
                await self.context_manager.delete_snapshot(handle.task_id)
            # ── 回写任务结果到对话 Store（供 Triage DeepAgent 引用） ──
            await self._write_task_result(handle)
            await self.event_bus.publish("task.completed", handle.to_dict())
            logger.info("[TaskExecutor] 任务完成: %s", handle.task_id)

        except asyncio.CancelledError:
            handle.status = TaskStatus.CANCELLED
            handle.updated_at = datetime.now().isoformat()
            self._msg_counts.pop(handle.task_id, None)
            self._journal_steps.pop(handle.task_id, None)
            self._unhandled_approval_rounds.pop(handle.task_id, None)
            raise
        except ApprovalNotHandledError as e:
            # ── P0 兜底：LLM 连续忽略审批标记，强制转入 WAITING_HUMAN ──
            logger.error(
                "[TaskExecutor] 任务 %s 触发审批兜底中断: %s", handle.task_id, e,
            )
            await self._force_approval_interrupt(handle, e)
        except Exception as e:
            logger.error("[TaskExecutor] 任务 %s 异常: %s", handle.task_id, e, exc_info=True)
            handle.status = TaskStatus.FAILED
            handle.error_message = str(e)
            handle.updated_at = datetime.now().isoformat()
            self._msg_counts.pop(handle.task_id, None)
            self._journal_steps.pop(handle.task_id, None)
            self._unhandled_approval_rounds.pop(handle.task_id, None)
            # ── 清理快照（终端状态，不再恢复） ──
            if self.context_manager is not None:
                await self.context_manager.delete_snapshot(handle.task_id)
            await self.event_bus.publish("task.failed", handle.to_dict())

    async def _resume_loop(
        self,
        handle: TaskHandle,
        resume_data: dict[str, Any],
    ):
        """恢复执行循环。

        HumanInTheLoopMiddleware 期望的 resume 格式是 HITLResponse:
        {"decisions": [{"type": "approve"}]} 或
        {"decisions": [{"type": "reject", "message": "..."}]}

        当前 API 接口格式是 {"action": "approved"|"rejected", "comment": "..."}，
        在此做格式映射。
        """
        from langgraph.types import Command

        config = {
            "configurable": {
                "thread_id": handle.thread_id,
                "store": self.store,  # 供 request_approval 工具读取审批数据
            }
        }

        # ── 映射 resume_data 到 HITLResponse 格式 ──────
        action = resume_data.get("action", "approved")
        comment = resume_data.get("comment", "")

        if action in ("approved", "approve"):
            hitl_response = {
                "decisions": [{"type": "approve"}]
            }
        elif action in ("rejected", "reject", "cancel"):
            hitl_response = {
                "decisions": [{
                    "type": "reject",
                    "message": comment or "审批人拒绝了此操作",
                }]
            }
        else:
            # 未知 action，默认通过
            logger.warning("[TaskExecutor] 未知 resume action: %s，默认 approve", action)
            hitl_response = {
                "decisions": [{"type": "approve"}]
            }

        # 如果 resume_data 已经包含 decisions（来自其他 resume 路径），直接使用
        if "decisions" in resume_data:
            hitl_response = resume_data

        logger.info(
            "[TaskExecutor] 恢复任务: %s action=%s", handle.task_id, action,
        )

        try:
            handle.status = TaskStatus.EXECUTING
            handle.updated_at = datetime.now().isoformat()

            # 处理审批决策的副作用 — 更新 Store 中的审批状态
            if action in ("approved", "rejected"):
                await self._update_approval_store(handle, action, comment)

            async for event in self.executor_agent.astream(
                Command(resume=hitl_response),
                config=config,
                stream_mode="updates",
            ):
                if "__interrupt__" in event:
                    await self._handle_interrupt(handle, event["__interrupt__"])
                    return

                # ── 将 state 中的 progress 同步回 handle + journal ──
                self._process_event(handle, event)

            handle.status = TaskStatus.COMPLETED
            handle.updated_at = datetime.now().isoformat()
            # ── 写入最终 journal 条目 ──
            await self._write_completion_journal(handle)
            # ── 清理快照（任务已完成） ──
            if self.context_manager is not None:
                await self.context_manager.delete_snapshot(handle.task_id)
            # ── 回写任务结果到对话 Store（供 Triage DeepAgent 引用） ──
            await self._write_task_result(handle)
            await self.event_bus.publish("task.completed", handle.to_dict())

        except asyncio.CancelledError:
            handle.status = TaskStatus.CANCELLED
            self._msg_counts.pop(handle.task_id, None)
            self._journal_steps.pop(handle.task_id, None)
            self._unhandled_approval_rounds.pop(handle.task_id, None)
            raise
        except ApprovalNotHandledError as e:
            # ── P0 兜底：LLM 在恢复后仍然忽略审批标记 ──
            logger.error(
                "[TaskExecutor] 任务 %s 恢复后触发审批兜底中断: %s", handle.task_id, e,
            )
            await self._force_approval_interrupt(handle, e)
        except Exception as e:
            logger.error("[TaskExecutor] 恢复任务 %s 异常: %s", handle.task_id, e, exc_info=True)
            handle.status = TaskStatus.FAILED
            handle.error_message = str(e)
            handle.updated_at = datetime.now().isoformat()
            self._msg_counts.pop(handle.task_id, None)
            self._journal_steps.pop(handle.task_id, None)
            self._unhandled_approval_rounds.pop(handle.task_id, None)
            await self.event_bus.publish("task.failed", handle.to_dict())

    async def _handle_interrupt(self, handle: TaskHandle, interrupt_data: Any):
        """处理 Agent 的 interrupt 事件。

        HumanInTheLoopMiddleware 产生 HITLRequest:
          {"action_requests": [...], "review_configs": [...]}
        """
        interrupt_info = self._extract_interrupt_info(interrupt_data)

        # ── 处理 HITLRequest ──
        action_requests = interrupt_info.get("action_requests", [])
        descriptions = []
        for req in action_requests:
            desc = req.get("description", "")
            tool_name = req.get("name", "unknown")
            if desc:
                descriptions.append(desc)
            else:
                descriptions.append(f"等待审批: {tool_name}")
            # 提取 approval_id（从 request_approval 工具的 args 中）
            args = req.get("args", {})
            if args.get("approval_id"):
                handle.approval_id = args["approval_id"]

        handle.status = TaskStatus.WAITING_HUMAN
        handle.progress = "\n".join(descriptions) if descriptions else "等待人类审批决策"
        handle.updated_at = datetime.now().isoformat()

        # ── 更新快照（确保重启后可恢复） ──
        await self._save_interrupt_snapshot(handle, interrupt_info)
        # ── 写入 journal ──
        await self._write_interrupt_journal(handle, interrupt_info)

        await self.event_bus.publish("task.interrupted", {
            **handle.to_dict(),
            "interrupt": interrupt_info,
        })
        logger.info(
            "[TaskExecutor] 任务中断 (HITL): %s actions=%s approval_id=%s",
            handle.task_id,
            [r.get("name") for r in action_requests],
            handle.approval_id,
        )

    @staticmethod
    def _extract_interrupt_info(data: Any) -> dict:
        """从 interrupt 数据中提取信息字典。"""
        if isinstance(data, dict):
            return data
        return {"raw": str(data)}

    def _process_event(self, handle: TaskHandle, event: dict) -> None:
        """将 Executor DeepAgent 的输出同步回 TaskHandle + 写入 journal。

        DeepAgent 的 astream(stream_mode="updates") 每个 event 是 {node_name: state_update}。
        同时做四件事：
        1. 提取最新 AI 消息 → handle.progress（对外可观测性）
        2. 检测新增消息 → 写入 task_journal（内部执行记忆）
        3. 检测未处理的审批标记 → 兜底中断（P0 安全机制）
        """
        from langchain_core.messages import AIMessage, ToolMessage

        for node_output in event.values():
            if not isinstance(node_output, dict):
                continue

            messages = node_output.get("messages")
            if not messages or not isinstance(messages, list) or len(messages) == 0:
                continue

            # ── 1. progress 同步（保持原有行为） ──
            for msg in reversed(messages):
                if isinstance(msg, AIMessage) and msg.content:
                    content = msg.content
                    if isinstance(content, str) and content.strip():
                        handle.progress = content[:500]
                        handle.updated_at = datetime.now().isoformat()
                        break

            # ── 2. journal diff: 检测新消息并写入 ──
            prev_count = self._msg_counts.get(handle.task_id, 0)
            new_count = len(messages)
            if new_count > prev_count:
                new_messages = messages[prev_count:]
                self._msg_counts[handle.task_id] = new_count
                # 异步写 journal — 不阻塞事件处理
                asyncio.create_task(
                    self._write_journal_from_messages(handle, new_messages)
                )

                # ── 3. 审批标记兜底检测（P0 安全机制） ──
                self._check_approval_marker_handled(handle, new_messages)

    def _check_approval_marker_handled(
        self,
        handle: TaskHandle,
        new_messages: list,
    ) -> None:
        """检测 Executor LLM 是否正确处理了审批标记。

        P0 兜底机制：正常情况下 Executor LLM 在 Specialist 返回
        ``[HUMAN_APPROVAL_REQUIRED]`` 后的下一轮就会调用 ``request_approval``。
        但如果 LLM 因幻觉/上下文压缩/推理偏差连续忽略该标记，此方法在
        3 轮后强制抛 ``ApprovalNotHandledError``，将任务转入 WAITING_HUMAN。

        计数器重置条件：检测到 ``request_approval`` 工具调用（说明 LLM 正确处理了）
        计数器递增条件：本轮消息包含 ``[HUMAN_APPROVAL_REQUIRED]`` 但没有 ``request_approval`` 调用
        """
        from langchain_core.messages import ToolMessage

        APPROVAL_MARKER = "[HUMAN_APPROVAL_REQUIRED]"
        task_id = handle.task_id

        # ── 检查本轮是否有 request_approval 调用 ──
        has_request_approval = any(
            isinstance(msg, ToolMessage) and getattr(msg, "name", "") == "request_approval"
            for msg in new_messages
        )

        if has_request_approval:
            # LLM 正确处理了审批 — 重置计数器
            if self._unhandled_approval_rounds.get(task_id, 0) > 0:
                logger.info(
                    "[ApprovalGuard] %s: request_approval 已调用，计数器重置 (之前=%d)",
                    task_id, self._unhandled_approval_rounds[task_id],
                )
            self._unhandled_approval_rounds[task_id] = 0
            return

        # ── 检查本轮是否有 [HUMAN_APPROVAL_REQUIRED] 标记 ──
        has_approval_marker = any(
            APPROVAL_MARKER in str(getattr(msg, "content", ""))
            for msg in new_messages
        )

        if not has_approval_marker:
            # 本轮无审批标记 — 不增加也不重置计数器（可能还在等 Specialist 返回）
            return

        # ── 有标记但没 request_approval → 累积 ──
        current = self._unhandled_approval_rounds.get(task_id, 0) + 1
        self._unhandled_approval_rounds[task_id] = current

        # 尝试从消息中提取 approval_id
        approval_id = ""
        for msg in new_messages:
            content = str(getattr(msg, "content", ""))
            if APPROVAL_MARKER in content:
                # 尝试从 JSON 中提取 approval_id
                import json as _json
                try:
                    data = _json.loads(content)
                    approval_id = data.get("approval_id", "")
                except (_json.JSONDecodeError, TypeError):
                    pass
                if approval_id:
                    break

        logger.warning(
            "[ApprovalGuard] %s: 第 %d 轮未处理审批标记 approval_id=%s",
            task_id, current, approval_id or "N/A",
        )

        if current >= 3:
            raise ApprovalNotHandledError(
                task_id=task_id,
                rounds=current,
                approval_id=approval_id,
            )

    # ── Journal 写入辅助 ──────────────────────────────────

    async def _write_journal_from_messages(
        self,
        handle: TaskHandle,
        new_messages: list,
    ) -> None:
        """从新增消息中提取关键事件，写入 task_journal。

        检测规则：
        - ToolMessage → Specialist 委托完成 → "specialist_result"
        - AIMessage（> 100 字符）→ 可能是关键决策 → "decision"
        - AIMessage 含错误关键词 → "error"
        """
        if self.context_manager is None:
            return

        from langchain_core.messages import AIMessage, ToolMessage

        for msg in new_messages:
            step = self._journal_steps.get(handle.task_id, 0) + 1

            if isinstance(msg, ToolMessage):
                # Specialist 委托返回结果
                tool_name = getattr(msg, "name", "unknown")
                content = getattr(msg, "content", "")
                result_text = content if isinstance(content, str) else str(content)[:500]

                entry = JournalEntry(
                    step=step,
                    event="specialist_result",
                    description=f"委托 {tool_name} 完成",
                    detail={
                        "specialist": tool_name,
                        "result_summary": result_text[:300],
                    },
                )
                await self.context_manager.write_journal_entry(handle.task_id, entry)
                self._journal_steps[handle.task_id] = step
                logger.debug(
                    "[Journal] %s #%d specialist_result: %s",
                    handle.task_id, step, tool_name,
                )

            elif isinstance(msg, AIMessage):
                content = getattr(msg, "content", "")
                if not content or not isinstance(content, str):
                    continue
                content = content.strip()
                if len(content) < 100:
                    continue

                # 判断事件类型
                error_keywords = ("失败", "错误", "异常", "❌", "failed", "error", "exception")
                is_error = any(kw in content[:200] for kw in error_keywords)
                event_type = "error" if is_error else "decision"

                entry = JournalEntry(
                    step=step,
                    event=event_type,
                    description=content[:200],
                    detail={
                        "is_error": is_error,
                        "full_length": len(content),
                    },
                )
                await self.context_manager.write_journal_entry(handle.task_id, entry)
                self._journal_steps[handle.task_id] = step
                logger.debug(
                    "[Journal] %s #%d %s: %.80s",
                    handle.task_id, step, event_type, content,
                )

    async def _write_completion_journal(self, handle: TaskHandle) -> None:
        """任务完成时写入最终 journal 条目。"""
        if self.context_manager is None:
            return

        step = self._journal_steps.get(handle.task_id, 0) + 1
        entry = JournalEntry(
            step=step,
            event="completed",
            description=f"任务完成: {handle.result_summary or handle.progress}"[:200],
            detail={
                "status": handle.status.value,
                "result_summary": handle.result_summary or handle.progress,
            },
        )
        await self.context_manager.write_journal_entry(handle.task_id, entry)
        self._journal_steps[handle.task_id] = step
        # 清理运行时跟踪状态
        self._msg_counts.pop(handle.task_id, None)
        self._journal_steps.pop(handle.task_id, None)

    async def _write_interrupt_journal(
        self,
        handle: TaskHandle,
        interrupt_info: dict,
    ) -> None:
        """任务挂起时写入 journal 条目。"""
        if self.context_manager is None:
            return

        step = self._journal_steps.get(handle.task_id, 0) + 1
        descriptions = []
        for req in interrupt_info.get("action_requests", []):
            desc = req.get("description", "") or f"等待审批: {req.get('name', 'unknown')}"
            descriptions.append(desc)

        entry = JournalEntry(
            step=step,
            event="approval_requested",
            description="; ".join(descriptions) if descriptions else "任务挂起，等待人工审批",
            detail={
                "action_requests": interrupt_info.get("action_requests", []),
                "approval_id": handle.approval_id,
            },
        )
        await self.context_manager.write_journal_entry(handle.task_id, entry)
        self._journal_steps[handle.task_id] = step
        # 中断时不清除 _msg_counts — 恢复后继续 diff

    @staticmethod
    def _build_execution_prompt(goal: str, context: dict[str, Any] | None) -> str:
        """构造 Executor DeepAgent 的执行消息。

        传入任务目标，Executor DeepAgent 自主规划并逐步执行。
        """
        import json as _json

        parts = [
            "## 新任务",
            "",
            goal,
        ]
        if context:
            parts.append(
                f"\n## 附加上下文\n```json\n"
                f"{_json.dumps(context, ensure_ascii=False, indent=2)}\n```"
            )

        return "\n".join(parts)

    async def _update_approval_store(
        self,
        handle: TaskHandle,
        action: str,
        comment: str,
    ) -> None:
        """更新 Store 中的审批请求状态。

        当人审做出决策后，更新 Store 中对应审批请求的 decision 字段。
        request_approval 工具在 resume 后执行时会读取这些字段。

        使用 handle.approval_id（在 interrupt 时从 action_requests 中提取）
        直接定位审批请求，无需遍历 Store。
        """
        if self.store is None or not handle.approval_id:
            return

        try:
            existing = await self.store.aget(
                ("approval_requests",), handle.approval_id
            )
            if existing and existing.value:
                approval = existing.value
                approval["status"] = "approved" if action == "approved" else "rejected"
                approval["decision"] = "approved" if action == "approved" else "rejected"
                approval["comment"] = comment
                approval["decided_at"] = datetime.now().isoformat()

                await self.store.aput(
                    ("approval_requests",), handle.approval_id, approval
                )
                logger.info(
                    "[TaskExecutor] 审批状态已更新: %s → %s",
                    handle.approval_id, approval["status"],
                )
        except Exception as e:
            logger.warning("[TaskExecutor] 更新审批状态异常: %s", e)

    async def _write_task_result(self, handle: TaskHandle) -> None:
        """将任务完成结果写入对话 Store，供 Triage DeepAgent 在后续对话中引用。

        写入 namespace ``("task_results", session_id)``，key = task_id。
        每次写入覆盖同一任务，标记 ``read: false`` 表示 Triage 尚未引用。
        Triage 在下一次 /chat 时读取并标记为已读。
        """
        if not handle.session_id or self.store is None:
            return

        result = {
            "task_id": handle.task_id,
            "goal": handle.goal,
            "status": handle.status.value,
            "result_summary": handle.result_summary or handle.progress,
            "error_message": handle.error_message,
            "completed_at": datetime.now().isoformat(),
            "read": False,
        }
        try:
            await self.store.aput(
                ("task_results", handle.session_id),
                handle.task_id,
                result,
            )
            logger.info(
                "[TaskExecutor] 任务结果已回写: %s → session=%s",
                handle.task_id, handle.session_id,
            )
        except Exception as e:
            logger.warning("[TaskExecutor] 任务结果回写失败（非致命）: %s", e)

    async def _force_approval_interrupt(
        self,
        handle: TaskHandle,
        error: ApprovalNotHandledError,
    ) -> None:
        """将因审批标记未处理而强制中断的任务转入 WAITING_HUMAN。

        P0 兜底机制的最终执行步骤：
        1. 构造合成 interrupt_info（模拟 HITLRequest 格式）
        2. 设置 handle 状态和行为
        3. 写入 journal 记录
        4. 保存快照（确保重启后可恢复）
        5. 发布事件通知前端

        与正常的 _handle_interrupt 不同，这里没有真正的 HITLRequest —
        interrupt_info 是从错误上下文中合成的。
        """
        # ── 构造合成 interrupt_info ──
        synthetic_interrupt: dict = {
            "action_requests": [
                {
                    "name": "request_approval",
                    "args": {"approval_id": error.approval_id or "unknown"},
                    "description": (
                        f"⚠️ 系统兜底中断：Executor LLM 连续 {error.rounds} 轮"
                        f"未处理审批标记 [HUMAN_APPROVAL_REQUIRED]。"
                        f"任务已自动挂起，请人工检查审批请求并做出决策。"
                    ),
                }
            ],
            "review_configs": [],
            "_synthetic": True,
            "_reason": (
                f"ApprovalNotHandledError: {error.rounds} consecutive rounds "
                f"without request_approval call"
            ),
        }

        if error.approval_id:
            handle.approval_id = error.approval_id
            synthetic_interrupt["action_requests"][0]["args"]["approval_id"] = error.approval_id

        # ── 更新 handle 状态 ──
        handle.status = TaskStatus.WAITING_HUMAN
        handle.progress = (
            f"⚠️ 系统自动挂起：检测到任务在执行过程中连续 {error.rounds} 轮"
            f"未处理审批标记。请人工检查是否需要审批决策。"
        )
        handle.updated_at = datetime.now().isoformat()

        # ── 写入 journal ──
        if self.context_manager is not None:
            step = self._journal_steps.get(handle.task_id, 0) + 1
            entry = JournalEntry(
                step=step,
                event="approval_requested",
                description=(
                    f"系统兜底中断：连续 {error.rounds} 轮未处理审批标记"
                ),
                detail={
                    "synthetic": True,
                    "rounds_ignored": error.rounds,
                    "approval_id": error.approval_id or "unknown",
                },
            )
            try:
                await self.context_manager.write_journal_entry(handle.task_id, entry)
                self._journal_steps[handle.task_id] = step
            except Exception as journal_err:
                logger.warning("[TaskExecutor] 强制中断 journal 写入失败: %s", journal_err)

        # ── 保存快照 ──
        await self._save_interrupt_snapshot(handle, synthetic_interrupt)

        # ── 发布事件 ──
        await self.event_bus.publish("task.interrupted", {
            **handle.to_dict(),
            "interrupt": synthetic_interrupt,
        })

        # ── 清理跟踪状态 ──
        self._unhandled_approval_rounds.pop(handle.task_id, None)

        logger.warning(
            "[TaskExecutor] 审批兜底中断完成: %s rounds=%d approval_id=%s",
            handle.task_id, error.rounds, error.approval_id or "N/A",
        )

    async def _save_interrupt_snapshot(
        self,
        handle: TaskHandle,
        interrupt_info: dict,
    ) -> None:
        """在任务中断时更新快照，确保重启后可恢复。

        与 shutdown 时的快照保存不同，此方法保存的是中断点信息，
        包含 approval_id 和完整的中断上下文。
        """
        if self.context_manager is None:
            return

        from app.harness.task_context import TaskSnapshot

        snapshot = TaskSnapshot(
            task_id=handle.task_id,
            goal=handle.goal,
            user_id=handle.user_id,
            session_id=handle.session_id,
            current_node=handle.status.value,
            status=handle.status.value,
            approval_id=handle.approval_id,
            interrupt_info=interrupt_info,
            recovery_hint=handle.progress or "等待外部输入",
        )
        try:
            await self.context_manager.save_snapshot(snapshot)
            logger.info(
                "[TaskExecutor] 中断快照已保存: %s approval_id=%s",
                handle.task_id, handle.approval_id,
            )
        except Exception as e:
            logger.error("[TaskExecutor] 中断快照保存失败: %s", e)


