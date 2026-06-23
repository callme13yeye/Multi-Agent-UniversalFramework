"""evolution/types.py — 自进化系统共享数据模型。

定义整个进化系统的核心数据结构：缺口报告、进化提案、验证结果等。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ═══════════════════════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════════════════════

class ProposalStatus(str, Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    ACTIVE = "active"
    ROLLED_BACK = "rolled_back"


class ProposalType(str, Enum):
    NEW_AGENT = "new_agent"
    NEW_TOOL = "new_tool"
    UPDATE_AGENT = "update_agent"


# ═══════════════════════════════════════════════════════════════
# 核心数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class GapReport:
    """GapDetector 产出的能力缺口报告。

    从任务执行日志、用户反馈、评估数据中聚合而成的能力缺口描述。
    每个 GapReport 代表一个可被自进化系统修复的能力缺失。
    """
    id: str                                    # gap-{uuid[:8]}
    detected_at: str = field(default_factory=lambda: datetime.now().isoformat())
    domain: str = ""                           # 缺口所属领域，如 "recruitment"、"finance"
    gap_type: str = ""                         # "missing_specialist" | "missing_tool" | "insufficient_capability"
    description: str = ""                      # 人类可读的缺口描述
    evidence: list[dict] = field(default_factory=list)  # [{source: "task_journal", task_id, entry, ...}]
    severity: str = "medium"                   # "low" | "medium" | "high" | "critical"
    suggested_action: str = "create_agent"     # "create_agent" | "create_tool" | "update_agent"
    suggested_name: str = ""                   # 建议的 SubAgent/Tool 名称
    suggested_spec: dict | None = None         # 建议的规格参数

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "detected_at": self.detected_at,
            "domain": self.domain,
            "gap_type": self.gap_type,
            "description": self.description,
            "evidence": self.evidence,
            "severity": self.severity,
            "suggested_action": self.suggested_action,
            "suggested_name": self.suggested_name,
            "suggested_spec": self.suggested_spec,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GapReport":
        return cls(
            id=data.get("id", ""),
            detected_at=data.get("detected_at", ""),
            domain=data.get("domain", ""),
            gap_type=data.get("gap_type", ""),
            description=data.get("description", ""),
            evidence=data.get("evidence", []),
            severity=data.get("severity", "medium"),
            suggested_action=data.get("suggested_action", "create_agent"),
            suggested_name=data.get("suggested_name", ""),
            suggested_spec=data.get("suggested_spec"),
        )


@dataclass
class EvolutionProposal:
    """进化提案 — 从生成到审批到激活的全生命周期追踪。

    一个提案代表一次自进化操作：新增 SubAgent、新增工具、或更新现有 Agent。
    """
    id: str                                    # evo-{uuid[:8]}
    gap_id: str = ""                           # 关联的 GapReport ID
    type: ProposalType = ProposalType.NEW_AGENT
    status: ProposalStatus = ProposalStatus.DRAFT
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # 生成内容
    agent_name: str = ""                       # 目标 SubAgent 名称
    agent_md_content: str = ""                 # 生成的 AGENT.md 全文
    tool_code: str = ""                        # 生成的工具 Python 代码 (NEW_TOOL 时)
    tool_name: str = ""                        # 工具函数名 (NEW_TOOL 时)

    # 校验结果
    validation_results: dict | None = None     # Validator 产出的回归测试结果

    # 审批信息
    reviewed_by: str = ""
    review_comment: str = ""

    # Git 回滚
    git_commit_hash: str = ""                  # 激活时的 Git commit
    git_prev_commit: str = ""                  # 激活前的 Git commit（用于回滚）

    # 运行时状态 (仅 ACTIVE / ROLLED_BACK)
    activated_at: str = ""
    deactivated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "gap_id": self.gap_id,
            "type": self.type.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "agent_name": self.agent_name,
            "agent_md_content": self.agent_md_content,
            "tool_code": self.tool_code,
            "tool_name": self.tool_name,
            "validation_results": self.validation_results,
            "reviewed_by": self.reviewed_by,
            "review_comment": self.review_comment,
            "git_commit_hash": self.git_commit_hash,
            "git_prev_commit": self.git_prev_commit,
            "activated_at": self.activated_at,
            "deactivated_at": self.deactivated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvolutionProposal":
        status = data.get("status", "draft")
        if isinstance(status, str):
            try:
                status = ProposalStatus(status)
            except ValueError:
                status = ProposalStatus.DRAFT

        ptype = data.get("type", "new_agent")
        if isinstance(ptype, str):
            try:
                ptype = ProposalType(ptype)
            except ValueError:
                ptype = ProposalType.NEW_AGENT

        return cls(
            id=data.get("id", ""),
            gap_id=data.get("gap_id", ""),
            type=ptype,
            status=status,
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            agent_name=data.get("agent_name", ""),
            agent_md_content=data.get("agent_md_content", ""),
            tool_code=data.get("tool_code", ""),
            tool_name=data.get("tool_name", ""),
            validation_results=data.get("validation_results"),
            reviewed_by=data.get("reviewed_by", ""),
            review_comment=data.get("review_comment", ""),
            git_commit_hash=data.get("git_commit_hash", ""),
            git_prev_commit=data.get("git_prev_commit", ""),
            activated_at=data.get("activated_at", ""),
            deactivated_at=data.get("deactivated_at", ""),
        )


@dataclass
class ValidationResult:
    """Validator 产出的回归验证结果。"""
    passed: bool = False
    total_tests: int = 0
    passed_tests: int = 0
    failed_tests: int = 0
    pass_rate: float = 0.0
    summary: str = ""
    details: list[dict] = field(default_factory=list)
    # details: [{test_id, input, expected_behavior, actual_behavior, passed, score, reason}]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "total_tests": self.total_tests,
            "passed_tests": self.passed_tests,
            "failed_tests": self.failed_tests,
            "pass_rate": self.pass_rate,
            "summary": self.summary,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValidationResult":
        return cls(
            passed=data.get("passed", False),
            total_tests=data.get("total_tests", 0),
            passed_tests=data.get("passed_tests", 0),
            failed_tests=data.get("failed_tests", 0),
            pass_rate=data.get("pass_rate", 0.0),
            summary=data.get("summary", ""),
            details=data.get("details", []),
        )
