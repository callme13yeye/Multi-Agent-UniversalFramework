# app/evolution/ — 自进化系统
#
# EvolutionManager 是自进化系统的顶层入口，协调缺口检测、Agent 生成、
# 验证、审批和热加载的完整生命周期。
#
# 核心组件:
#   EvolutionManager — 编排器，协调所有子组件 + 后台定时扫描
#   GapDetector      — 从任务日志中检测能力缺口
#   SubAgentGenerator — 基于缺口生成 AGENT.md
#   Validator        — 用历史数据回归验证新 Agent
#   HotReloader      — 动态注册 Agent/重建运行时，无需重启
#
# 使用方式:
#   在 main.py lifespan 中初始化:
#       from app.evolution import EvolutionManager, evolution_state
#       evo_mgr = EvolutionManager(store, event_bus, context_manager, app.state)
#       await evo_mgr.load_state()
#       await evo_mgr.start_scheduled_scan(interval_hours=6)
#
# Admin API:
#   在 main.py 中注册路由:
#       from app.evolution.admin_router import router as evolution_router
#       app.include_router(evolution_router)
#
# 配置:
#   在 config.py 的 get_config() 返回值中新增 evolution 节:
#       "evolution": {
#           "enabled": True,
#           "scan_interval_hours": 6,
#           "analysis_lookback_hours": 24,
#           "min_tasks_for_analysis": 10,
#           "validation_min_pass_rate": 0.7,
#           "llm_model": "deepseek-v4-flash",
#       }

from app.evolution.evolution_manager import EvolutionManager
from app.evolution._state import evolution_state, EvolutionState
from app.evolution.types import (
    GapReport,
    EvolutionProposal,
    ValidationResult,
    ProposalStatus,
    ProposalType,
)
from app.evolution.gap_detector import GapDetector
from app.evolution.agent_generator import SubAgentGenerator
from app.evolution.validator import Validator
from app.evolution.hot_reloader import HotReloader

__all__ = [
    "EvolutionManager",
    "EvolutionState",
    "evolution_state",
    "GapReport",
    "EvolutionProposal",
    "ValidationResult",
    "ProposalStatus",
    "ProposalType",
    "GapDetector",
    "SubAgentGenerator",
    "Validator",
    "HotReloader",
]
