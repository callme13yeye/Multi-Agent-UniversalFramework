"""evolution/_state.py — 进化系统运行时状态管理。

EvolutionState 是进化系统的内存状态单例，负责：
1. 追踪所有提案（proposals）和缺口报告（gap_reports）
2. 追踪当前已激活的 Agent 和 Tool
3. 将状态持久化到 AsyncPostgresStore（服务重启后恢复）

Store namespace:
    ("evolution", "proposals", proposal_id)    — 进化提案
    ("evolution", "gap_reports", gap_id)       — 缺口报告
    ("evolution", "regression_tests")          — 回归测试数据集
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, TYPE_CHECKING

from app.evolution.types import EvolutionProposal, GapReport, ProposalStatus

if TYPE_CHECKING:
    from langgraph.store.postgres.aio import AsyncPostgresStore

logger = logging.getLogger(__name__)

# Store namespace 常量
PROPOSALS_NS = "evolution"
PROPOSALS_KEY = "proposals"
GAP_REPORTS_KEY = "gap_reports"
REGRESSION_TESTS_KEY = "regression_tests"


class EvolutionState:
    """进化系统运行时状态 — 全局单例。

    Store 持久化确保服务重启后提案和缺口报告不丢失。
    内存索引提供 O(1) 查询，避免每次请求都访问 Store。
    """

    def __init__(self):
        self.proposals: dict[str, EvolutionProposal] = {}
        self.gap_reports: dict[str, GapReport] = {}
        self.active_agents: set[str] = set()   # 当前已热加载的 agent name
        self.active_tools: set[str] = set()    # 当前已热加载的 tool name
        self.store: "AsyncPostgresStore | None" = None
        self.last_scan_at: str = ""            # 上次扫描时间

    # ── Store 绑定 ───────────────────────────────────────

    def set_store(self, store: "AsyncPostgresStore") -> None:
        """绑定 Store 实例（在 lifespan 中调用）。"""
        self.store = store

    # ── 提案持久化 ───────────────────────────────────────

    async def persist_proposal(self, proposal: EvolutionProposal) -> None:
        """将提案写入 Store。"""
        if self.store is None:
            logger.warning("[EvolutionState] Store 未绑定，跳过持久化: %s", proposal.id)
            return

        proposal.updated_at = datetime.now().isoformat()
        try:
            await self.store.aput(
                (PROPOSALS_NS, PROPOSALS_KEY),
                proposal.id,
                proposal.to_dict(),
            )
            self.proposals[proposal.id] = proposal
            logger.debug("[EvolutionState] 提案已持久化: %s status=%s", proposal.id, proposal.status.value)
        except Exception as e:
            logger.error("[EvolutionState] 提案持久化失败: %s — %s", proposal.id, e)

    async def load_proposals(self) -> list[EvolutionProposal]:
        """从 Store 恢复所有提案。"""
        if self.store is None:
            logger.warning("[EvolutionState] Store 未绑定，无法恢复提案")
            return []

        try:
            items = await self.store.asearch((PROPOSALS_NS, PROPOSALS_KEY), limit=500)
        except Exception as e:
            logger.warning("[EvolutionState] 提案恢复扫描失败: %s", e)
            return []

        loaded = 0
        for item in items:
            if not item.value:
                continue
            try:
                proposal = EvolutionProposal.from_dict(item.value)
                self.proposals[proposal.id] = proposal
                if proposal.status == ProposalStatus.ACTIVE:
                    self.active_agents.add(proposal.agent_name)
                loaded += 1
            except Exception as e:
                logger.warning("[EvolutionState] 解析提案失败 key=%s: %s", item.key, e)

        if loaded > 0:
            logger.info("[EvolutionState] 从 Store 恢复 %d 个提案 (%d active)", loaded, len(self.active_agents))
        return list(self.proposals.values())

    async def delete_proposal(self, proposal_id: str) -> bool:
        """从 Store 删除提案。"""
        if self.store is None:
            return False
        try:
            await self.store.adelete((PROPOSALS_NS, PROPOSALS_KEY), proposal_id)
            self.proposals.pop(proposal_id, None)
            return True
        except Exception as e:
            logger.warning("[EvolutionState] 提案删除失败: %s — %s", proposal_id, e)
            return False

    # ── 缺口报告持久化 ──────────────────────────────────

    async def persist_gap_report(self, gap: GapReport) -> None:
        """将缺口报告写入 Store。"""
        if self.store is None:
            return
        try:
            await self.store.aput(
                (PROPOSALS_NS, GAP_REPORTS_KEY),
                gap.id,
                gap.to_dict(),
            )
            self.gap_reports[gap.id] = gap
        except Exception as e:
            logger.error("[EvolutionState] 缺口报告持久化失败: %s — %s", gap.id, e)

    async def load_gap_reports(self) -> list[GapReport]:
        """从 Store 恢复所有缺口报告。"""
        if self.store is None:
            return []

        try:
            items = await self.store.asearch((PROPOSALS_NS, GAP_REPORTS_KEY), limit=500)
        except Exception as e:
            logger.warning("[EvolutionState] 缺口报告恢复扫描失败: %s", e)
            return []

        loaded = 0
        for item in items:
            if not item.value:
                continue
            try:
                gap = GapReport.from_dict(item.value)
                self.gap_reports[gap.id] = gap
                loaded += 1
            except Exception as e:
                logger.warning("[EvolutionState] 解析缺口报告失败: %s", e)

        if loaded > 0:
            logger.info("[EvolutionState] 从 Store 恢复 %d 个缺口报告", loaded)
        return list(self.gap_reports.values())

    # ── 回归测试数据集 ──────────────────────────────────

    async def save_regression_tests(self, tests: list[dict]) -> None:
        """保存回归测试数据集到 Store。"""
        if self.store is None:
            return
        try:
            await self.store.aput(
                (PROPOSALS_NS, REGRESSION_TESTS_KEY),
                "default",
                {"tests": tests, "updated_at": datetime.now().isoformat()},
            )
        except Exception as e:
            logger.warning("[EvolutionState] 回归测试保存失败: %s", e)

    async def load_regression_tests(self) -> list[dict]:
        """从 Store 加载回归测试数据集。"""
        if self.store is None:
            return []
        try:
            item = await self.store.aget((PROPOSALS_NS, REGRESSION_TESTS_KEY), "default")
            if item and item.value:
                return item.value.get("tests", [])
        except Exception as e:
            logger.warning("[EvolutionState] 回归测试加载失败: %s", e)
        return []

    # ── 活跃追踪 ─────────────────────────────────────────

    def mark_active(self, agent_name: str, tool_name: str = "") -> None:
        """标记 Agent/Tool 已激活。"""
        if agent_name:
            self.active_agents.add(agent_name)
        if tool_name:
            self.active_tools.add(tool_name)

    def mark_inactive(self, agent_name: str, tool_name: str = "") -> None:
        """标记 Agent/Tool 已停用。"""
        self.active_agents.discard(agent_name)
        self.active_tools.discard(tool_name)

    # ── 状态快照 ─────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """获取进化系统整体状态（供 Admin API 使用）。"""
        proposals_by_status: dict[str, int] = {}
        for p in self.proposals.values():
            key = p.status.value
            proposals_by_status[key] = proposals_by_status.get(key, 0) + 1

        return {
            "total_gaps": len(self.gap_reports),
            "total_proposals": len(self.proposals),
            "proposals_by_status": proposals_by_status,
            "active_agents": sorted(self.active_agents),
            "active_tools": sorted(self.active_tools),
            "last_scan_at": self.last_scan_at,
            "store_bound": self.store is not None,
        }


# ── 全局单例 ─────────────────────────────────────────────

evolution_state = EvolutionState()
