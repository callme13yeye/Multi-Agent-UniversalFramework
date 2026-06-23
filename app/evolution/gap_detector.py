"""evolution/gap_detector.py — 能力缺口检测器。

从任务执行日志（journal）、任务结果（task_results）和人工反馈中发现系统能力缺口。
核心思路：分析 Executor DeepAgent 的执行轨迹，找出"没有合适 Specialist"、
"工具返回质量低下"、"审批流程缺失"等模式。

触发方式:
    - 定时触发（EvolutionManager 后台 cron）
    - 手动触发（Admin API）
    - 事件触发（task.completed / task.failed）
"""

from __future__ import annotations

import json as _json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING

from app.evolution.types import GapReport
from app.evolution._state import evolution_state
from app.prompts.gap_detection import BATCH_ANALYSIS_PROMPT, SINGLE_TASK_ANALYSIS_PROMPT
from app.tools import TOOL_REGISTRY
from app.agent_definitions import discover_specialist_agents

if TYPE_CHECKING:
    from langgraph.store.postgres.aio import AsyncPostgresStore
    from app.harness.event_bus import EventBus
    from app.task_context import TaskContextManager

logger = logging.getLogger(__name__)


class GapDetector:
    """从 journal、task_results、人类反馈中检测能力缺口。

    分析策略：
    1. 单任务分析 — 扫描单个任务的完整 journal，用 LLM 判断是否存在缺口
    2. 批量分析 — 跨多个任务的聚合视角，发现共性模式
    3. 信号聚合 — 综合 journal + feedback + langfuse traces 等多种信号源
    """

    def __init__(
        self,
        store: "AsyncPostgresStore",
        event_bus: "EventBus",
        context_manager: "TaskContextManager",
    ):
        self.store = store
        self.event_bus = event_bus
        self.context_manager = context_manager

    # ── 主入口 ───────────────────────────────────────────

    async def analyze_recent_tasks(
        self,
        lookback_hours: int = 24,
        min_tasks: int = 10,
        max_gaps: int = 5,
    ) -> list[GapReport]:
        """分析最近 N 小时的所有任务，生成缺口报告。

        Args:
            lookback_hours: 回溯时间窗口（小时）
            min_tasks: 最少需要多少已完成任务才触发分析
            max_gaps: 每次扫描最多生成多少缺口报告

        Returns:
            GapReport 列表（可能为空）
        """
        # 1. 收集任务摘要
        task_summaries = await self._collect_task_summaries(lookback_hours)
        if len(task_summaries) < min_tasks:
            logger.info(
                "[GapDetector] 任务数量不足: %d < %d，跳过分析",
                len(task_summaries), min_tasks,
            )
            return []

        logger.info("[GapDetector] 开始分析 %d 个任务……", len(task_summaries))

        # 2. 构建 LLM 分析上下文
        available_tools = self._build_available_tools_text()
        existing_agents = self._build_existing_agents_text()
        task_texts = "\n\n---\n\n".join(task_summaries)

        # 3. 调用 LLM 分析
        raw_gaps = await self._llm_analyze_batch(
            task_summaries=task_texts,
            task_count=len(task_summaries),
            available_tools=available_tools,
            existing_agents=existing_agents,
        )

        # 4. 转换为 GapReport + 去重
        gaps = []
        for raw in raw_gaps:
            try:
                gap = self._raw_to_gap_report(raw)
                gaps.append(gap)
            except Exception as e:
                logger.warning("[GapDetector] 解析 LLM 输出失败: %s", e)

        gaps = self._deduplicate_gaps(gaps)
        gaps = gaps[:max_gaps]

        # 5. 持久化到 Store + 内存
        for gap in gaps:
            await evolution_state.persist_gap_report(gap)

        # 6. 发布事件
        if gaps:
            await self.event_bus.publish("evolution.gap_detected", {
                "count": len(gaps),
                "gap_ids": [g.id for g in gaps],
                "lookback_hours": lookback_hours,
            })
            logger.info(
                "[GapDetector] 发现 %d 个能力缺口: %s",
                len(gaps), [g.suggested_name for g in gaps],
            )
        else:
            logger.info("[GapDetector] 未发现明显能力缺口")

        return gaps

    async def analyze_single_task(self, task_id: str) -> GapReport | None:
        """分析单个任务，判断是否存在缺口。

        用于实时分析：每当 task.completed 时就触发一次轻量级检测。
        """
        # 读取 journal
        journal_entries = await self.context_manager.read_journal(task_id, limit=100)
        if not journal_entries:
            return None

        # 快速过滤：只分析有 error / 有至少一次 general_assistant 委托的任务
        has_signal = any(
            e.event in ("error",) or
            ("general_assistant" in str(e.detail.get("specialist", "")))
            for e in journal_entries
        )
        if not has_signal:
            return None

        # 构建 journal 文本
        journal_lines = []
        for e in journal_entries:
            journal_lines.append(
                f"#{e.step} [{e.event}] {e.description[:300]}"
            )

        # 构建任务信息
        task_info = f"任务 ID: {task_id}\n"

        # 读 task result — 遍历所有 session 找到该任务
        # 任务结果存储在 ("task_results", session_id) namespace 下，
        # asearch 按前缀匹配，可跨 session 搜索
        try:
            items = await self.store.asearch(("task_results",), limit=500)
            for item in items:
                if item.value and item.value.get("task_id") == task_id:
                    task_info += f"目标: {item.value.get('goal', '')}\n"
                    task_info += f"状态: {item.value.get('status', '')}\n"
                    task_info += f"结果: {item.value.get('result_summary', '')[:200]}\n"
                    break
        except Exception:
            pass

        # LLM 分析
        prompt = SINGLE_TASK_ANALYSIS_PROMPT.format(
            task_info=task_info,
            journal_context="\n".join(journal_lines),
        )

        result = await self._call_llm(prompt)
        if not result:
            return None

        try:
            data = _json.loads(result)
            if not data or not data.get("has_gap"):
                return None

            gaps = data.get("gaps", [])
            if not gaps:
                return None

            raw = gaps[0]  # 单任务分析只取第一个缺口
            return self._raw_to_gap_report(raw)
        except (_json.JSONDecodeError, KeyError):
            return None

    # ── 内部方法 ─────────────────────────────────────────

    async def _collect_task_summaries(self, lookback_hours: int) -> list[str]:
        """从 Store 收集最近的任务摘要（journal summary + 基本结果）。"""
        summaries = []
        cutoff = datetime.now() - timedelta(hours=lookback_hours)

        try:
            # 扫描 task_results namespace
            items = await self.store.asearch(("task_results",), limit=200)
        except Exception as e:
            logger.warning("[GapDetector] 扫描 task_results 失败: %s", e)
            return summaries

        for item in items:
            if not item.value:
                continue
            data = item.value

            # 跳过太旧的任务
            completed_at = data.get("completed_at", "")
            if completed_at and completed_at < cutoff.isoformat():
                continue

            task_id = data.get("task_id", "")
            goal = data.get("goal", "")[:150]
            status = data.get("status", "")
            result = data.get("result_summary", "")[:200]
            error = data.get("error_message", "")

            # 读 journal summary
            journal_summary = ""
            try:
                journal_summary = await self.context_manager.build_journal_summary(task_id)
            except Exception:
                pass

            summary = (
                f"任务 {task_id} (状态: {status})\n"
                f"目标: {goal}\n"
            )
            if result:
                summary += f"结果: {result}\n"
            if error:
                summary += f"错误: {error[:200]}\n"
            if journal_summary:
                summary += f"执行摘要:\n{journal_summary[:500]}\n"

            summaries.append(summary)

        return summaries

    async def _llm_analyze_batch(
        self,
        task_summaries: str,
        task_count: int,
        available_tools: str,
        existing_agents: str,
    ) -> list[dict]:
        """用 LLM 批量分析任务摘要。"""
        prompt = BATCH_ANALYSIS_PROMPT.format(
            task_count=task_count,
            task_summaries=task_summaries,
            available_tools=available_tools,
            existing_agents=existing_agents,
            min_occurrences=max(2, task_count // 5),  # 至少出现在 20% 的任务中
        )

        result = await self._call_llm(prompt)
        if not result:
            return []

        try:
            return _json.loads(result)
        except _json.JSONDecodeError as e:
            logger.warning("[GapDetector] LLM 返回非 JSON: %s", str(e)[:200])
            # 尝试提取 JSON 数组
            return self._extract_json_array(result)

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 执行分析。"""
        from app.async_load_model import AsyncLoadModel
        from langchain_core.messages import HumanMessage

        try:
            llm = await AsyncLoadModel.async_langchain_api_model("deepseek-v4-flash")
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            content = response.content
            if isinstance(content, list):
                content = "".join(
                    c.get("text", "") if isinstance(c, dict) else str(c)
                    for c in content
                )
            return str(content).strip()
        except Exception as e:
            logger.error("[GapDetector] LLM 调用失败: %s", e)
            return ""

    @staticmethod
    def _extract_json_array(text: str) -> list[dict]:
        """从 LLM 输出中提取 JSON 数组（容错处理）。"""
        # 找第一个 [ 和最后一个 ]
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                return _json.loads(text[start:end + 1])
            except _json.JSONDecodeError:
                pass
        return []

    @staticmethod
    def _raw_to_gap_report(raw: dict) -> GapReport:
        """将 LLM 输出转换为 GapReport。"""
        gap_id = f"gap-{uuid.uuid4().hex[:8]}"
        return GapReport(
            id=gap_id,
            domain=raw.get("domain", "general"),
            gap_type=raw.get("gap_type", "missing_specialist"),
            description=raw.get("description", ""),
            severity=raw.get("severity", "medium"),
            suggested_action=raw.get("suggested_action", "create_agent"),
            suggested_name=raw.get("suggested_name", ""),
            suggested_spec=raw.get("suggested_spec"),
        )

    @staticmethod
    def _build_available_tools_text() -> str:
        """构建可用工具列表文本。"""
        lines = []
        for name, func in sorted(TOOL_REGISTRY.items()):
            doc = getattr(func, "description", "") or ""
            doc = doc.strip().split("\n")[0][:100] if doc else "（无描述）"
            lines.append(f"- `{name}`: {doc}")
        return "\n".join(lines) if lines else "（无可用工具）"

    @staticmethod
    def _build_existing_agents_text() -> str:
        """构建现有 Specialist 列表文本。"""
        try:
            agents = discover_specialist_agents()
        except Exception:
            agents = []

        if not agents:
            return "（无现有 Specialist）"

        lines = []
        for a in agents:
            name = a.get("name", "unknown")
            desc = a.get("description", "")
            lines.append(f"- `{name}`: {desc}")
        return "\n".join(lines)

    @staticmethod
    def _deduplicate_gaps(gaps: list[GapReport]) -> list[GapReport]:
        """合并相似的缺口报告。按 suggested_name 去重。"""
        seen_names: set[str] = set()
        unique: list[GapReport] = []
        for gap in gaps:
            name = gap.suggested_name.lower()
            if name and name not in seen_names:
                seen_names.add(name)
                unique.append(gap)
            elif not name:
                # 如果没名字，用描述的前50字做模糊去重
                key = gap.description[:50]
                if key not in seen_names:
                    seen_names.add(key)
                    unique.append(gap)
        return unique
