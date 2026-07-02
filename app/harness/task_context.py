"""任务上下文管理器 — 为长周期任务管理记忆和上下文。

三层记忆模型：
    Hot  — 在 context window 中（当前步骤 + 最近的工具调用结果）
    Warm — 可快速检索（用户偏好、执行计划、已完成步骤摘要）
    Cold — 归档存储（历史任务完整记录，按需搜索）

与现有 ``CompositeBackend`` 的关系：
    CompositeBackend 提供了底层存储路由（"/memories/" → StoreBackend），
    TaskContextManager 在其上构建语义层的记忆管理。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from langgraph.store.postgres.aio import AsyncPostgresStore

logger = logging.getLogger(__name__)


# ── Journal 数据模型 ─────────────────────────────────────

@dataclass
class JournalEntry:
    """任务执行日志中的一条记录。

    与 messages（会被 SummarizationMiddleware 压缩）不同，
    journal 是永久结构化记录，不随上下文窗口变化而丢失。

    每条记录对应执行过程中的一个关键事件：
    - Specialist 委托完成
    - 审批请求
    - 关键决策
    - 异常/错误
    - 任务完成
    """
    step: int
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    event: str = ""                    # "specialist_result" | "approval_requested" | "decision" | "error" | "completed"
    description: str = ""              # 人类可读摘要
    detail: dict | None = None         # 结构化详情（specialist 名、参数、结果摘要等）

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "timestamp": self.timestamp,
            "event": self.event,
            "description": self.description,
            "detail": self.detail or {},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JournalEntry":
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in valid_fields})


# ── 数据模型 ────────────────────────────────────────────

@dataclass
class TaskSnapshot:
    """任务执行快照 — 存"恢复到哪"的定位信息。

    不同于 checkpointer（存全量 state），快照只存恢复定位：
    - 当前执行到哪个节点（plan/execute/evaluate/await_approval）
    - 哪个步骤（current_step_index）
    - 中断信息（如果挂起中）
    - 时间戳

    使用场景：
    1. 审批挂起前 — 确保恢复时知道从 await_approval 继续
    2. 优雅关闭时 — 给未完成任务打快照，重启后恢复
    3. 崩溃恢复时 — 重启后读取快照定位
    """
    task_id: str
    goal: str = ""
    user_id: str = ""
    session_id: str = ""               # 任务所属的对话 session（用于完成后回写结果）
    current_node: str = ""            # 当前图节点名
    current_step_index: int = 0
    plan: list[dict] = field(default_factory=list)
    interrupt_info: dict | None = None  # 挂起时的中断信息
    status: str = ""                   # 对应 SupervisorState.status
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    recovery_hint: str = ""            # 人类可读的恢复提示
    approval_id: str = ""              # 当前挂起的审批请求 ID（WAITING_HUMAN 恢复时需要）

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "current_node": self.current_node,
            "current_step_index": self.current_step_index,
            "plan": self.plan,
            "interrupt_info": self.interrupt_info,
            "status": self.status,
            "created_at": self.created_at,
            "recovery_hint": self.recovery_hint,
            "approval_id": self.approval_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskSnapshot":
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in valid_fields})


@dataclass
class PlanStep:
    """执行计划中的一个步骤。"""
    id: str
    description: str
    specialist: str                # 委托给哪个 Specialist
    depends_on: list[str] = field(default_factory=list)
    input_summary: str = ""        # 从前面步骤提炼的输入
    status: str = "pending"        # pending → in_progress → completed → skipped → failed
    result_summary: str = ""       # 完成后的一句话摘要（人类阅读）
    tool_calls_made: list[str] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""
    output_data: dict | None = None    # 结构化输出（JSON Schema 验证后）
    output_schema_name: str = ""       # 使用的 Schema 名称

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "specialist": self.specialist,
            "depends_on": self.depends_on,
            "input_summary": self.input_summary,
            "status": self.status,
            "result_summary": self.result_summary,
            "tool_calls_made": self.tool_calls_made,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "output_data": self.output_data,
            "output_schema_name": self.output_schema_name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlanStep":
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in valid_fields})


@dataclass
class TaskMemory:
    """一个任务的完整记忆结构。存储在 Store 中，跨会话持久化。"""
    task_id: str
    goal: str
    user_id: str = ""
    plan: list[dict] = field(default_factory=list)
    key_findings: list[str] = field(default_factory=list)
    human_decisions: list[dict] = field(default_factory=list)
    errors_and_retries: list[dict] = field(default_factory=list)
    final_summary: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "user_id": self.user_id,
            "plan": self.plan,
            "key_findings": self.key_findings,
            "human_decisions": self.human_decisions,
            "errors_and_retries": self.errors_and_retries,
            "final_summary": self.final_summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ── 上下文管理器 ─────────────────────────────────────────

class TaskContextManager:
    """为长周期任务管理上下文生命周期。

    核心职责：
        1. 任务启动时：组装初始上下文（目标 + 偏好 + 历史参考）
        2. 步骤完成时：保存语义摘要（不是完整结果）
        3. 任务恢复时：重建最小必要上下文（计划进度 + 关键发现 + 当前阻塞）
        4. 用户偏好学习：从审批决策和反馈中提取长期偏好

    使用方式::

        ctx_mgr = TaskContextManager(store=pg_db_manager.store)

        # 任务开始时
        context = await ctx_mgr.assemble_initial_context(goal, user_id)

        # 每步完成后
        await ctx_mgr.save_step_result(task_id, step_id, result, specialist)

        # 任务恢复时
        resume_context = await ctx_mgr.build_resumption_context(task_id)
    """

    # Store namespace
    TASK_MEMORY_NS = ("task_memory",)
    USER_PREFS_NS = ("user_preferences",)
    SNAPSHOT_NS = ("task_snapshots",)
    JOURNAL_NS = ("task_journal",)

    def __init__(self, store: "AsyncPostgresStore"):
        self.store = store

    # ── 快照管理 ────────────────────────────────────────

    async def save_snapshot(self, snapshot: TaskSnapshot):
        """保存任务执行快照。

        在审批挂起前、优雅关闭时调用。
        快照覆盖写入 — 每个任务只保留最新快照。
        """
        await self.store.aput(
            self.SNAPSHOT_NS,
            snapshot.task_id,
            snapshot.to_dict(),
        )
        logger.info(
            "[Snapshot] 快照已保存: %s node=%s step=%d",
            snapshot.task_id, snapshot.current_node, snapshot.current_step_index,
        )

    async def load_snapshot(self, task_id: str) -> TaskSnapshot | None:
        """加载任务的最新快照。"""
        item = await self.store.aget(self.SNAPSHOT_NS, task_id)
        if item and item.value:
            return TaskSnapshot.from_dict(item.value)
        return None

    async def delete_snapshot(self, task_id: str):
        """任务完成后删除快照。"""
        try:
            await self.store.adelete(self.SNAPSHOT_NS, task_id)
            logger.debug("[Snapshot] 快照已删除: %s", task_id)
        except Exception as e:
            logger.debug("[Snapshot] 删除快照失败（非致命）: %s", e)

    # ── Journal 管理 ───────────────────────────────────

    async def write_journal_entry(
        self,
        task_id: str,
        entry: JournalEntry,
    ) -> None:
        """追加一条执行日志到任务的 journal。

        key 使用零填充的 step 编号，保证按序排列。
        """
        key = f"{entry.step:04d}"
        await self.store.aput(
            (*self.JOURNAL_NS, task_id),
            key,
            entry.to_dict(),
        )
        logger.debug(
            "[Journal] %s #%d %s: %.80s",
            task_id, entry.step, entry.event, entry.description,
        )

    async def read_journal(
        self,
        task_id: str,
        limit: int = 50,
    ) -> list[JournalEntry]:
        """读取任务 journal，按 step 升序返回最近 N 条。"""
        try:
            items = await self.store.asearch(
                (*self.JOURNAL_NS, task_id),
                limit=limit,
            )
        except Exception:
            return []

        entries = []
        for item in items:
            if item.value:
                entries.append(JournalEntry.from_dict(item.value))

        entries.sort(key=lambda e: e.step)
        return entries[-limit:] if limit > 0 else entries

    async def build_journal_summary(
        self,
        task_id: str,
    ) -> str:
        """从 journal 生成人类可读的执行摘要。

        用于恢复上下文注入 — Executor 恢复时先看到这个，
        就知道已经做了什么、做到哪了，不需要从压缩后的 messages 里猜。
        """
        entries = await self.read_journal(task_id, limit=0)
        if not entries:
            return ""

        lines = ["## 📋 执行日志", ""]
        for e in entries:
            icon = {
                "specialist_result": "✅",
                "approval_requested": "⏳",
                "decision": "🧭",
                "error": "⚠️",
                "completed": "🏁",
            }.get(e.event, "📌")

            ts = e.timestamp[:19]  # 仅日期时间部分
            lines.append(f"{icon} **[{ts}]** {e.description}")

            detail = e.detail or {}
            if detail.get("specialist"):
                lines.append(f"   └ 委托: {detail['specialist']}")
            if detail.get("result_summary"):
                lines.append(f"   └ 结果: {detail['result_summary'][:200]}")

        return "\n".join(lines)

    # ── 初始上下文组装 ──────────────────────────────────

    async def assemble_initial_context(
        self,
        goal: str,
        user_id: str,
    ) -> str:
        """为新任务组装初始上下文。

        注入：
        - 当前任务目标
        - 用户长期偏好（从 warm memory）
        - 相关历史任务摘要（从 cold memory）
        """
        parts = [f"## 当前任务\n{goal}\n"]

        # 加载用户偏好
        prefs = await self._load_user_preferences(user_id)
        if prefs:
            parts.append(f"## 用户长期偏好\n{prefs}")

        # 检索相关历史任务
        related = await self._search_related_tasks(goal, user_id)
        if related:
            parts.append(f"## 相关历史任务（供参考）\n{related}")

        return "\n\n".join(parts)

    # ── 步骤结果保存 ────────────────────────────────────

    async def save_step_result(
        self,
        task_id: str,
        step_id: str,
        result: str,
        specialist: str,
        output_data: dict | None = None,
        output_schema_name: str = "",
    ):
        """保存一个步骤的执行结果。

        核心原则：不存完整结果，存**语义摘要** + **结构化数据**。
        完整结果在 Agent 的 checkpointer state 中已有，
        这里只需要保留供后续步骤决策使用的关键信息。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
            result: 步骤结果文本（人类阅读）
            specialist: 执行的 Specialist 名称
            output_data: 结构化输出数据（JSON Schema 验证后）
            output_schema_name: 使用的 Schema 名称
        """
        memory = await self._load_task_memory(task_id)

        # 更新对应步骤
        for step_dict in memory.plan:
            if step_dict.get("id") == step_id:
                step_dict["status"] = "completed"
                step_dict["result_summary"] = self._extract_summary(result)
                step_dict["completed_at"] = datetime.now().isoformat()
                if output_data is not None:
                    step_dict["output_data"] = output_data
                    step_dict["output_schema_name"] = output_schema_name
                break

        # 提取关键发现
        key_info = self._extract_key_info(result)
        memory.key_findings.extend(key_info)
        memory.updated_at = datetime.now().isoformat()

        await self._save_task_memory(task_id, memory)
        logger.debug(
            "[TaskContext] 步骤 %s/%s 完成: %d 条关键发现, structured=%s",
            task_id, step_id, len(key_info), bool(output_data),
        )

    async def save_human_decision(
        self,
        task_id: str,
        decision: dict[str, Any],
    ):
        """记录人类的审批决策 — 这是偏好学习的数据源。"""
        memory = await self._load_task_memory(task_id)
        memory.human_decisions.append({
            **decision,
            "timestamp": datetime.now().isoformat(),
        })
        memory.updated_at = datetime.now().isoformat()
        await self._save_task_memory(task_id, memory)

        # 学习偏好 — 如果用户反复做相似选择
        await self._learn_from_decisions(memory.user_id, memory.human_decisions)

    # ── 恢复上下文组装 ──────────────────────────────────

    async def build_resumption_context(self, task_id: str) -> str:
        """为任务恢复组装最小必要上下文。

        恢复时需要的信息：
        1. 任务目标和计划（做到哪了）
        2. 已完成步骤的摘要（不是完整历史）
        3. 关键发现（候选人评估结论等）
        4. 当前阻塞点
        """
        memory = await self._load_task_memory(task_id)

        parts = [
            f"## 恢复任务\n**目标**: {memory.goal}\n",
            f"## 执行进度\n{self._format_plan_progress(memory.plan)}\n",
        ]

        if memory.key_findings:
            parts.append(
                "## 已获得的关键信息\n" +
                "\n".join(f"- {f}" for f in memory.key_findings[-10:])
            )

        if memory.human_decisions:
            recent = memory.human_decisions[-3:]
            parts.append(
                "## 最近的审批决策\n" +
                "\n".join(
                    f"- [{d.get('step', '')}] {d.get('action', '')}: {d.get('comment', '')}"
                    for d in recent
                )
            )

        return "\n\n".join(parts)

    async def build_step_context(
        self,
        task_id: str,
        step_id: str,
    ) -> str:
        """为单个步骤组装执行上下文。

        从已完成步骤的 result_summary 中提炼该步骤需要的输入。
        这是 Supervisor "中转"能力的工程实现。

        如果有结构化数据，以 JSON 代码块形式注入，方便 LLM 精确引用。
        """
        memory = await self._load_task_memory(task_id)

        # 找到当前步骤
        current_step = None
        for s in memory.plan:
            if s.get("id") == step_id:
                current_step = s
                break

        if not current_step:
            return ""

        parts = []

        # 从依赖步骤中组装上下文
        for dep_id in current_step.get("depends_on", []):
            for s in memory.plan:
                if s.get("id") == dep_id:
                    # ── 结构化数据优先 ──
                    s_output_data = s.get("output_data")
                    if s_output_data:
                        import json as _json
                        parts.append(
                            f"[步骤 {dep_id} 结构化结果]\n"
                            f"```json\n{_json.dumps(s_output_data, ensure_ascii=False, indent=2)}\n```"
                        )
                    # 文本摘要作为补充
                    if s.get("result_summary"):
                        parts.append(f"[步骤 {dep_id} 文本摘要] {s['result_summary']}")
                    break

        # 如果没有显式依赖，包含上一步结果
        if not parts:
            step_ids = [s.get("id") for s in memory.plan]
            try:
                idx = step_ids.index(step_id)
                if idx > 0:
                    prev = memory.plan[idx - 1]
                    prev_output = prev.get("output_data")
                    if prev_output:
                        import json as _json
                        parts.append(
                            f"[上一步结构化结果]\n"
                            f"```json\n{_json.dumps(prev_output, ensure_ascii=False, indent=2)}\n```"
                        )
                    if prev.get("result_summary"):
                        parts.append(f"[上一步结果] {prev['result_summary']}")
            except ValueError:
                pass

        if not parts:
            parts.append("（无前置步骤 — 这是独立的第一步）")

        # 附加关键发现
        if memory.key_findings:
            parts.append(f"\n## 关键参考信息\n" + "\n".join(f"- {f}" for f in memory.key_findings[-5:]))

        return "\n".join(parts)

    async def get_upstream_typed_data(
        self,
        task_id: str,
        step_id: str,
    ) -> dict[str, Any]:
        """获取当前步骤所需的上游结构化数据。

        按 depends_on 聚合所有上游步骤的 output_data，
        返回 {step_id: output_data} 的映射。

        Args:
            task_id: 任务 ID
            step_id: 当前步骤 ID

        Returns:
            {step_id: output_data} 字典，按依赖顺序排列
        """
        memory = await self._load_task_memory(task_id)

        # 找到当前步骤
        current_step = None
        for s in memory.plan:
            if s.get("id") == step_id:
                current_step = s
                break

        if not current_step:
            return {}

        deps = current_step.get("depends_on", [])
        result: dict[str, Any] = {}

        for dep_id in deps:
            for s in memory.plan:
                if s.get("id") == dep_id and s.get("output_data"):
                    result[dep_id] = s["output_data"]
                    break

        return result

    # ── 计划管理 ────────────────────────────────────────

    async def save_plan(self, task_id: str, plan: list[dict], goal: str, user_id: str):
        """保存 Supervisor 生成的执行计划。"""
        memory = TaskMemory(
            task_id=task_id,
            goal=goal,
            user_id=user_id,
            plan=plan,
        )
        await self._save_task_memory(task_id, memory)
        logger.info("[TaskContext] 计划已保存: %s (%d 步)", task_id, len(plan))

    async def update_step_status(
        self,
        task_id: str,
        step_id: str,
        status: str,
        result_summary: str = "",
    ):
        """更新步骤状态。"""
        memory = await self._load_task_memory(task_id)
        for step_dict in memory.plan:
            if step_dict.get("id") == step_id:
                step_dict["status"] = status
                if result_summary:
                    step_dict["result_summary"] = result_summary
                if status == "in_progress":
                    step_dict["started_at"] = datetime.now().isoformat()
                break
        memory.updated_at = datetime.now().isoformat()
        await self._save_task_memory(task_id, memory)

    # ── 偏好学习 ────────────────────────────────────────

    async def _learn_from_decisions(self, user_id: str, decisions: list[dict]):
        """从审批决策中提取长期偏好。

        例如：用户连续三次拒了薪资 > 40K 的 offer →
             记录偏好 "用户倾向于控制薪资在 40K 以下"。
        """
        # 简化实现 — 实际应调用轻量 LLM 做模式提取
        if len(decisions) < 3:
            return

        # 提取审批模式
        rejected = [d for d in decisions if d.get("action") in ("rejected", "reject")]
        if len(rejected) >= 3:
            reasons = [d.get("comment", "") for d in rejected]
            # 存储偏好摘要（实际应更智能）
            await self._save_user_preference(
                user_id,
                "approval_pattern",
                f"用户倾向于拒绝以下类型的请求: {', '.join(reasons[-3:])}",
            )

    # ── 内部辅助 ────────────────────────────────────────

    async def _load_task_memory(self, task_id: str) -> TaskMemory:
        item = await self.store.aget(self.TASK_MEMORY_NS, task_id)
        if item and item.value:
            return TaskMemory(**item.value)
        return TaskMemory(task_id=task_id, goal="")

    async def _save_task_memory(self, task_id: str, memory: TaskMemory):
        await self.store.aput(
            self.TASK_MEMORY_NS,
            task_id,
            memory.to_dict(),
        )

    async def _load_user_preferences(self, user_id: str) -> str:
        """加载用户长期偏好。"""
        item = await self.store.aget(self.USER_PREFS_NS, user_id)
        if item and item.value:
            prefs = item.value
            if isinstance(prefs, dict):
                return "\n".join(f"- {k}: {v}" for k, v in prefs.items())
        return ""

    async def _save_user_preference(self, user_id: str, key: str, value: str):
        """保存一条用户偏好。"""
        item = await self.store.aget(self.USER_PREFS_NS, user_id)
        prefs = item.value if item and item.value else {}
        if isinstance(prefs, dict):
            prefs[key] = value
        await self.store.aput(self.USER_PREFS_NS, user_id, prefs)

    async def _search_related_tasks(self, goal: str, user_id: str) -> str:
        """从历史任务中检索相关记录。

        当前实现：返回最近 3 个已完成任务的摘要。
        后续可升级为 embedding 相似度检索。
        """
        items = await self.store.asearch(
            self.TASK_MEMORY_NS,
            limit=10,
            filter={"user_id": user_id},
        )
        related = []
        for item in items:
            if item.value:
                summary = item.value.get("final_summary", "")
                goal_text = item.value.get("goal", "")
                if summary:
                    related.append(f"- {goal_text}: {summary[:200]}")

        return "\n".join(related[:3]) if related else ""

    @staticmethod
    def _extract_summary(result: str, max_length: int = 300) -> str:
        """从步骤结果中提取摘要。

        当前实现：简单的截断 + 保留首段。
        后续可升级为 LLM 摘要。
        """
        if not result:
            return ""
        # 取第一段或前 N 个字符
        lines = result.strip().split("\n")
        summary_lines = []
        total_len = 0
        for line in lines:
            if total_len + len(line) > max_length:
                break
            summary_lines.append(line)
            total_len += len(line)
        return "\n".join(summary_lines)

    @staticmethod
    def _extract_key_info(result: str) -> list[str]:
        """从步骤结果中提取关键信息点。

        当前实现：提取带关键标记的行。
        后续可升级为 LLM 提取。
        """
        if not result:
            return []
        key_markers = ["候选人", "薪资", "匹配", "经验", "状态", "审批", "建议"]
        findings = []
        for line in result.split("\n"):
            line = line.strip()
            if any(marker in line for marker in key_markers) and len(line) < 200:
                findings.append(line)
        return findings[:5]

    @staticmethod
    def _format_plan_progress(plan: list[dict]) -> str:
        """格式化计划进度为可读字符串。"""
        if not plan:
            return "（无计划）"

        lines = []
        for step in plan:
            status = step.get("status", "pending")
            icon = {
                "completed": "✅",
                "in_progress": "🔄",
                "failed": "❌",
                "skipped": "⏭️",
            }.get(status, "⬜")

            desc = step.get("description", step.get("id", "?"))
            lines.append(f"{icon} {desc}")

            summary = step.get("result_summary", "")
            if summary:
                lines.append(f"   → {summary[:120]}")

        return "\n".join(lines)
