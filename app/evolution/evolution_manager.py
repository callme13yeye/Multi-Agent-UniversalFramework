"""evolution/evolution_manager.py — 自进化系统编排器。

协调所有子组件完成完整的进化周期:
    检测 → 生成 → 验证 → 提案 → 审批 → 激活/回滚

与 haras 层的 TaskExecutor 类似，EvolutionManager 是 evolution 域的顶层入口。
通过后台 asyncio.Task 驱动定时扫描循环（不阻塞用户请求）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, TYPE_CHECKING

from app.evolution.types import (
    EvolutionProposal,
    GapReport,
    ProposalStatus,
    ProposalType,
    ValidationResult,
)
from app.evolution._state import evolution_state
from app.evolution.gap_detector import GapDetector
from app.evolution.agent_generator import SubAgentGenerator
from app.evolution.validator import Validator
from app.evolution.hot_reloader import HotReloader

if TYPE_CHECKING:
    from langgraph.store.postgres.aio import AsyncPostgresStore
    from app.harness.event_bus import EventBus
    from app.task_context import TaskContextManager

logger = logging.getLogger(__name__)


class EvolutionManager:
    """自进化系统编排器。

    职责:
        1. 定时/手动触发能力缺口检测
        2. 基于缺口生成 AGENT.md 提案
        3. 回归验证新提案
        4. 审批工作流管理
        5. 激活/回滚已审批提案
        6. 通过 EventBus 发布进化事件

    集成方式:
        在 main.py lifespan 中初始化，注入 app.state。
        后台扫描通过 asyncio.Task 运行，不影响 HTTP 请求。
    """

    def __init__(
        self,
        store: "AsyncPostgresStore",
        event_bus: "EventBus",
        context_manager: "TaskContextManager",
        app_state: Any,
    ):
        self.store = store
        self.event_bus = event_bus
        self.context_manager = context_manager

        # 子组件
        self.gap_detector = GapDetector(store, event_bus, context_manager)
        self.agent_generator = SubAgentGenerator()
        self.validator = Validator(store, event_bus)
        self.hot_reloader = HotReloader(app_state)

        # 后台扫描
        self._scanner_task: asyncio.Task | None = None
        self._scan_interval = 21600  # 默认 6 小时
        self._analysis_lookback = 24  # 默认分析 24h 内的任务
        self._min_tasks = 10
        self._max_gaps = 5
        self._min_pass_rate = 0.7  # 默认 70% 通过率（可被 config 覆盖）

        # 从 config 读取覆盖默认值
        from config import get_config
        evo_cfg = get_config().get("evolution", {})
        if evo_cfg:
            self._scan_interval = int(evo_cfg.get("scan_interval_hours", 6.0) * 3600)
            self._analysis_lookback = evo_cfg.get("analysis_lookback_hours", 24)
            self._min_tasks = evo_cfg.get("min_tasks_for_analysis", 10)
            self._max_gaps = evo_cfg.get("max_gaps_per_scan", 5)
            self._min_pass_rate = evo_cfg.get("validation_min_pass_rate", 0.7)

    # ── 完整进化周期 ────────────────────────────────────

    async def run_full_cycle(self) -> list[EvolutionProposal]:
        """运行一次完整的进化周期。

        流程:
        1. GapDetector 扫描 → gaps
        2. 去重
        3. 对每个 gap: 生成 AGENT.md → 验证 → 创建提案
        4. 持久化所有提案
        5. 通过 EventBus 通知

        Returns:
            新创建的提案列表（状态为 pending_review）
        """
        logger.info("[EvolutionManager] 开始完整进化周期……")

        # 1. 检测缺口
        gaps = await self.gap_detector.analyze_recent_tasks(
            lookback_hours=self._analysis_lookback,
            min_tasks=self._min_tasks,
            max_gaps=self._max_gaps,
        )

        if not gaps:
            logger.info("[EvolutionManager] 未发现能力缺口，周期结束")
            return []

        # 2. 生成提案
        proposals = await self.generate_from_gaps(gaps)

        # 3. 更新扫描时间
        evolution_state.last_scan_at = datetime.now().isoformat()

        # 4. 发布事件
        if proposals:
            await self.event_bus.publish("evolution.proposals_created", {
                "count": len(proposals),
                "proposal_ids": [p.id for p in proposals],
            })

        logger.info(
            "[EvolutionManager] 进化周期完成 — %d 个缺口 → %d 个提案",
            len(gaps), len(proposals),
        )
        return proposals

    async def generate_from_gaps(self, gaps: list[GapReport]) -> list[EvolutionProposal]:
        """对一批缺口生成提案（包含生成 + 验证）。

        Args:
            gaps: 缺口报告列表

        Returns:
            新创建的提案列表
        """
        proposals = []

        for gap in gaps:
            try:
                # 3a. 生成 AGENT.md
                agent_md = await self.agent_generator.generate(gap)

                # 3b. 创建提案（写入暂存目录）
                proposal = await self.agent_generator.create_proposal(gap, agent_md)

                # 3c. 回归验证
                validation = await self.validator.validate_agent(proposal)
                proposal.validation_results = validation.to_dict()

                # 3d. 检查验证通过率
                if validation.pass_rate >= self._min_pass_rate:
                    proposal.status = ProposalStatus.PENDING_REVIEW
                else:
                    proposal.status = ProposalStatus.DRAFT
                    logger.warning(
                        "[EvolutionManager] 提案 %s 验证通过率 %.0f%% < %.0f%%，标记为 draft",
                        proposal.id, validation.pass_rate * 100, self._min_pass_rate * 100,
                    )

                # 3e. 持久化
                await evolution_state.persist_proposal(proposal)
                proposals.append(proposal)

            except Exception as e:
                logger.error(
                    "[EvolutionManager] 处理缺口 %s 失败: %s",
                    gap.id, e, exc_info=True,
                )
                continue

        return proposals

    async def generate_from_gap(self, gap_id: str) -> EvolutionProposal:
        """对单个缺口生成提案（Admin API 手动触发）。"""
        gap = evolution_state.gap_reports.get(gap_id)
        if not gap:
            raise ValueError(f"缺口不存在: {gap_id}")

        proposals = await self.generate_from_gaps([gap])
        if not proposals:
            raise RuntimeError(f"无法为缺口 {gap_id} 生成提案")

        return proposals[0]

    # ── 审批工作流 ───────────────────────────────────────

    async def approve_proposal(
        self,
        proposal_id: str,
        reviewer: str,
        comment: str = "",
    ) -> EvolutionProposal:
        """审批通过一个提案。"""
        proposal = evolution_state.proposals.get(proposal_id)
        if not proposal:
            raise ValueError(f"提案不存在: {proposal_id}")

        if proposal.status not in (ProposalStatus.PENDING_REVIEW, ProposalStatus.DRAFT):
            raise ValueError(f"提案 {proposal_id} 状态为 {proposal.status.value}，不能审批")

        proposal.status = ProposalStatus.APPROVED
        proposal.reviewed_by = reviewer
        proposal.review_comment = comment
        await evolution_state.persist_proposal(proposal)

        await self.event_bus.publish("evolution.proposal_approved", {
            "proposal_id": proposal_id,
            "agent_name": proposal.agent_name,
            "reviewer": reviewer,
        })

        logger.info("[EvolutionManager] 提案已审批通过: %s", proposal_id)
        return proposal

    async def reject_proposal(
        self,
        proposal_id: str,
        reviewer: str,
        comment: str = "",
    ) -> EvolutionProposal:
        """驳回一个提案。"""
        proposal = evolution_state.proposals.get(proposal_id)
        if not proposal:
            raise ValueError(f"提案不存在: {proposal_id}")

        proposal.status = ProposalStatus.REJECTED
        proposal.reviewed_by = reviewer
        proposal.review_comment = comment
        await evolution_state.persist_proposal(proposal)

        await self.event_bus.publish("evolution.proposal_rejected", {
            "proposal_id": proposal_id,
            "agent_name": proposal.agent_name,
            "reviewer": reviewer,
        })

        logger.info("[EvolutionManager] 提案已驳回: %s", proposal_id)
        return proposal

    # ── 激活 / 回滚 ──────────────────────────────────────

    async def activate_proposal(self, proposal_id: str) -> EvolutionProposal:
        """激活一个已审批的提案。"""
        proposal = evolution_state.proposals.get(proposal_id)
        if not proposal:
            raise ValueError(f"提案不存在: {proposal_id}")

        if proposal.status != ProposalStatus.APPROVED:
            raise ValueError(
                f"提案 {proposal_id} 状态为 {proposal.status.value}，只有 approved 才能激活"
            )

        if proposal.type == ProposalType.NEW_AGENT:
            await self.hot_reloader.activate_agent(proposal)
        else:
            raise NotImplementedError(f"不支持的提案类型: {proposal.type.value}")

        await evolution_state.persist_proposal(proposal)

        await self.event_bus.publish("evolution.activated", {
            "proposal_id": proposal_id,
            "agent_name": proposal.agent_name,
        })

        logger.info("[EvolutionManager] 提案已激活: %s → %s", proposal_id, proposal.agent_name)
        return proposal

    async def rollback_proposal(self, proposal_id: str) -> EvolutionProposal:
        """回滚一个已激活的提案。"""
        proposal = evolution_state.proposals.get(proposal_id)
        if not proposal:
            raise ValueError(f"提案不存在: {proposal_id}")

        if proposal.status != ProposalStatus.ACTIVE:
            raise ValueError(f"提案 {proposal_id} 状态为 {proposal.status.value}，只有 active 才能回滚")

        if proposal.type == ProposalType.NEW_AGENT:
            await self.hot_reloader.rollback_agent(proposal)
        else:
            raise NotImplementedError(f"不支持的提案类型: {proposal.type.value}")

        await evolution_state.persist_proposal(proposal)

        await self.event_bus.publish("evolution.rolled_back", {
            "proposal_id": proposal_id,
            "agent_name": proposal.agent_name,
        })

        logger.info("[EvolutionManager] 提案已回滚: %s → %s", proposal_id, proposal.agent_name)
        return proposal

    # ── 定时扫描 ─────────────────────────────────────────

    async def start_scheduled_scan(self, interval_hours: float = 6.0) -> None:
        """启动定时扫描（后台 asyncio.Task）。

        Args:
            interval_hours: 扫描间隔（小时）
        """
        if self._scanner_task is not None:
            logger.warning("[EvolutionManager] 定时扫描已在运行中")
            return

        self._scan_interval = int(interval_hours * 3600)
        self._scanner_task = asyncio.create_task(self._scan_loop())
        logger.info(
            "[EvolutionManager] 定时扫描已启动 — 间隔 %.1fh, 回溯 %dh",
            interval_hours, self._analysis_lookback,
        )

    async def stop_scheduled_scan(self) -> None:
        """停止定时扫描。"""
        if self._scanner_task is None:
            return

        self._scanner_task.cancel()
        try:
            await self._scanner_task
        except asyncio.CancelledError:
            pass
        self._scanner_task = None
        logger.info("[EvolutionManager] 定时扫描已停止")

    async def _scan_loop(self) -> None:
        """后台扫描循环。"""
        logger.info("[EvolutionManager] 扫描循环开始 — 首次延迟 60s 启动")
        # 首次延迟 60 秒，让服务充分初始化
        await asyncio.sleep(60)

        while True:
            try:
                logger.info("[EvolutionManager] 定时扫描触发")
                await self.run_full_cycle()
            except asyncio.CancelledError:
                logger.info("[EvolutionManager] 扫描循环被取消")
                return
            except Exception as e:
                logger.error("[EvolutionManager] 扫描异常: %s", e, exc_info=True)

            try:
                await self.event_bus.publish("evolution.scan_completed", {
                    "timestamp": datetime.now().isoformat(),
                    "next_scan_seconds": self._scan_interval,
                })
            except Exception:
                pass

            # 等待下一次扫描
            try:
                await asyncio.sleep(self._scan_interval)
            except asyncio.CancelledError:
                return

    # ── 查询接口 ─────────────────────────────────────────

    async def get_all_gaps(self) -> list[GapReport]:
        """获取所有缺口报告。"""
        return list(evolution_state.gap_reports.values())

    async def get_gap(self, gap_id: str) -> GapReport | None:
        """获取单个缺口报告。"""
        return evolution_state.gap_reports.get(gap_id)

    async def get_all_proposals(
        self,
        status_filter: str | None = None,
    ) -> list[EvolutionProposal]:
        """获取所有提案，支持按状态筛选。"""
        proposals = list(evolution_state.proposals.values())
        if status_filter:
            proposals = [
                p for p in proposals
                if p.status.value == status_filter
            ]
        return proposals

    async def get_proposal(self, proposal_id: str) -> EvolutionProposal | None:
        """获取单个提案。"""
        return evolution_state.proposals.get(proposal_id)

    async def manual_analyze(
        self,
        task_ids: list[str] | None = None,
        lookback_hours: int = 24,
    ) -> list[GapReport]:
        """手动触发缺口分析（Admin API 使用）。

        Args:
            task_ids: 指定分析的任务 ID（可选，不传则扫描最近N小时）
            lookback_hours: 回溯时间窗口
        """
        if task_ids:
            gaps = []
            for tid in task_ids:
                gap = await self.gap_detector.analyze_single_task(tid)
                if gap:
                    gaps.append(gap)
                    await evolution_state.persist_gap_report(gap)
            return gaps
        else:
            return await self.gap_detector.analyze_recent_tasks(
                lookback_hours=lookback_hours,
                min_tasks=1,  # 手动触发降低阈值
                max_gaps=self._max_gaps,
            )

    async def get_status(self) -> dict[str, Any]:
        """获取进化系统整体状态。"""
        return evolution_state.get_status()

    async def load_state(self) -> None:
        """从 Store 恢复持久化状态（启动时调用）。"""
        await evolution_state.load_proposals()
        await evolution_state.load_gap_reports()
        logger.info(
            "[EvolutionManager] 状态恢复完成 — %d 提案, %d 缺口",
            len(evolution_state.proposals),
            len(evolution_state.gap_reports),
        )
