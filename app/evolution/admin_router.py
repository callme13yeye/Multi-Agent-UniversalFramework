"""evolution/admin_router.py — 进化系统 Admin API。

提供缺口检测、提案管理、审批、激活、回滚的管理端点。
复用现有 admin_routes.py 的 _verify_admin 认证模式。

所有端点前缀: /admin/evolution
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.evolution.types import ProposalStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/evolution", tags=["Evolution"])


# ═══════════════════════════════════════════════════════════════
# 请求/响应模型
# ═══════════════════════════════════════════════════════════════

class TriggerAnalysisRequest(BaseModel):
    task_ids: list[str] | None = Field(None, description="指定分析的任务 ID 列表，不传则扫描最近 N 小时")
    lookback_hours: int = Field(24, description="回溯时间窗口（小时）")


class ReviewRequest(BaseModel):
    action: str = Field(..., description="approve 或 reject")
    comment: str = Field("", description="审批意见")


class EvolutionSettings(BaseModel):
    scan_interval_hours: float | None = Field(None, description="扫描间隔（小时）")
    analysis_lookback_hours: int | None = Field(None, description="分析回溯（小时）")
    min_tasks_for_analysis: int | None = Field(None, description="最少任务数")
    max_gaps_per_scan: int | None = Field(None, description="每次扫描最大缺口数")
    validation_min_pass_rate: float | None = Field(None, description="验证通过率下限 (0.0-1.0)")


# ═══════════════════════════════════════════════════════════════
# 认证
# ═══════════════════════════════════════════════════════════════

def _verify_admin(request: Request) -> None:
    """验证管理员 Token。复用现有 admin_routes 的逻辑。"""
    # 尝试从现有 admin_routes 导入
    try:
        from app.routes.admin_routes import _verify_admin as _admin_verify
        _admin_verify(request)
    except (ImportError, AttributeError):
        # 回退：自行检查 X-Admin-Token
        import os
        admin_token = os.environ.get("ADMIN_TOKEN", "")
        if admin_token:
            req_token = request.headers.get("X-Admin-Token", "")
            if req_token != admin_token:
                raise HTTPException(status_code=403, detail="无效的管理员 Token")


def _get_evo_mgr(request: Request):
    """从 app.state 获取 EvolutionManager。"""
    evo_mgr = getattr(request.app.state, "evolution_manager", None)
    if evo_mgr is None:
        raise HTTPException(
            status_code=503,
            detail="自进化系统未启用或初始化失败",
        )
    return evo_mgr


# ═══════════════════════════════════════════════════════════════
# 缺口检测
# ═══════════════════════════════════════════════════════════════

@router.get("/gaps")
async def list_gaps(request: Request) -> dict[str, Any]:
    """GET /admin/evolution/gaps — 列出所有检测到的能力缺口。"""
    _verify_admin(request)
    evo_mgr = _get_evo_mgr(request)
    gaps = await evo_mgr.get_all_gaps()

    return {
        "gaps": [g.to_dict() for g in gaps],
        "count": len(gaps),
    }


@router.get("/gaps/{gap_id}")
async def get_gap(request: Request, gap_id: str) -> dict[str, Any]:
    """GET /admin/evolution/gaps/{gap_id} — 查看单个缺口详情。"""
    _verify_admin(request)
    evo_mgr = _get_evo_mgr(request)
    gap = await evo_mgr.get_gap(gap_id)
    if gap is None:
        raise HTTPException(status_code=404, detail=f"缺口不存在: {gap_id}")
    return {"gap": gap.to_dict()}


@router.post("/gaps/analyze")
async def trigger_analysis(
    request: Request,
    body: TriggerAnalysisRequest = TriggerAnalysisRequest(),
) -> dict[str, Any]:
    """POST /admin/evolution/gaps/analyze — 手动触发缺口分析。

    body:
        task_ids: 指定分析的任务 ID（可选）
        lookback_hours: 回溯时间窗口，默认 24h
    """
    _verify_admin(request)
    evo_mgr = _get_evo_mgr(request)

    gaps = await evo_mgr.manual_analyze(
        task_ids=body.task_ids,
        lookback_hours=body.lookback_hours,
    )

    return {
        "gaps": [g.to_dict() for g in gaps],
        "count": len(gaps),
        "message": f"分析完成，发现 {len(gaps)} 个能力缺口" if gaps else "未发现明显能力缺口",
    }


@router.post("/gaps/{gap_id}/generate")
async def generate_proposal(request: Request, gap_id: str) -> dict[str, Any]:
    """POST /admin/evolution/gaps/{gap_id}/generate — 基于缺口生成进化提案。"""
    _verify_admin(request)
    evo_mgr = _get_evo_mgr(request)

    try:
        proposal = await evo_mgr.generate_from_gap(gap_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "proposal": proposal.to_dict(),
        "message": f"提案已生成: {proposal.id}",
    }


# ═══════════════════════════════════════════════════════════════
# 提案管理
# ═══════════════════════════════════════════════════════════════

@router.get("/proposals")
async def list_proposals(
    request: Request,
    status: str | None = None,
) -> dict[str, Any]:
    """GET /admin/evolution/proposals?status=pending_review — 列出所有提案。

    status 可选值: draft, pending_review, approved, rejected, active, rolled_back
    """
    _verify_admin(request)
    evo_mgr = _get_evo_mgr(request)

    # 验证 status 参数
    if status:
        valid_statuses = {s.value for s in ProposalStatus}
        if status not in valid_statuses:
            raise HTTPException(
                status_code=400,
                detail=f"无效的状态值: {status}。有效值: {', '.join(valid_statuses)}",
            )

    proposals = await evo_mgr.get_all_proposals(status_filter=status)
    return {
        "proposals": [p.to_dict() for p in proposals],
        "count": len(proposals),
    }


@router.get("/proposals/{proposal_id}")
async def get_proposal(request: Request, proposal_id: str) -> dict[str, Any]:
    """GET /admin/evolution/proposals/{proposal_id} — 查看提案详情（含 AGENT.md 全文）。"""
    _verify_admin(request)
    evo_mgr = _get_evo_mgr(request)
    proposal = await evo_mgr.get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"提案不存在: {proposal_id}")
    return {"proposal": proposal.to_dict()}


@router.get("/proposals/{proposal_id}/preview")
async def preview_agent_md(request: Request, proposal_id: str) -> dict[str, Any]:
    """GET /admin/evolution/proposals/{proposal_id}/preview — 预览生成的 AGENT.md。

    返回渲染友好的格式，包含原始 markdown 和解析后的结构化信息。
    """
    _verify_admin(request)
    evo_mgr = _get_evo_mgr(request)
    proposal = await evo_mgr.get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"提案不存在: {proposal_id}")

    # 尝试解析 AGENT.md
    parsed = {}
    if proposal.agent_md_content:
        from pathlib import Path
        import tempfile
        try:
            from app.agent_definitions import _parse_agent_md
            # 写入临时文件再解析
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".md", delete=False, encoding="utf-8",
            ) as f:
                f.write(proposal.agent_md_content)
                tmp_path = f.name
            spec = _parse_agent_md(Path(tmp_path))
            Path(tmp_path).unlink(missing_ok=True)
            parsed = {
                "name": spec.get("name", ""),
                "description": spec.get("description", ""),
                "allowed_tools": spec.get("allowed_tools", []),
                "system_prompt": spec.get("system_prompt", ""),
                "output_schema_name": spec.get("output_schema_name", ""),
            }
        except Exception:
            parsed = {"error": "解析失败"}

    return {
        "proposal_id": proposal_id,
        "agent_name": proposal.agent_name,
        "agent_md_raw": proposal.agent_md_content,
        "agent_md_parsed": parsed,
        "validation_results": proposal.validation_results,
    }


# ═══════════════════════════════════════════════════════════════
# 审批
# ═══════════════════════════════════════════════════════════════

@router.post("/proposals/{proposal_id}/review")
async def review_proposal(
    request: Request,
    proposal_id: str,
    body: ReviewRequest,
) -> dict[str, Any]:
    """POST /admin/evolution/proposals/{proposal_id}/review — 审批提案。

    body:
        action: "approve" 或 "reject"
        comment: 审批意见
    """
    _verify_admin(request)
    evo_mgr = _get_evo_mgr(request)

    if body.action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action 必须是 approve 或 reject")

    reviewer = request.headers.get("X-Admin-User", "admin")

    try:
        if body.action == "approve":
            proposal = await evo_mgr.approve_proposal(proposal_id, reviewer, body.comment)
            message = f"提案 {proposal_id} 已审批通过"
        else:
            proposal = await evo_mgr.reject_proposal(proposal_id, reviewer, body.comment)
            message = f"提案 {proposal_id} 已驳回"
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "proposal": proposal.to_dict(),
        "message": message,
    }


# ═══════════════════════════════════════════════════════════════
# 激活 / 回滚
# ═══════════════════════════════════════════════════════════════

@router.post("/proposals/{proposal_id}/activate")
async def activate_proposal(request: Request, proposal_id: str) -> dict[str, Any]:
    """POST /admin/evolution/proposals/{proposal_id}/activate — 激活已审批的提案。

    激活操作:
    1. AGENT.md 从暂存目录迁移到正式目录
    2. Git commit
    3. 重新扫描 subagents + 重建 Triage/Executor agent
    4. 更新 TaskExecutor 引用
    """
    _verify_admin(request)
    evo_mgr = _get_evo_mgr(request)

    try:
        proposal = await evo_mgr.activate_proposal(proposal_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error("[EvolutionAdmin] 激活异常: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"激活失败: {e}")

    return {
        "proposal": proposal.to_dict(),
        "message": f"Agent '{proposal.agent_name}' 已激活，Triage + Executor Agent 已重建",
    }


@router.post("/proposals/{proposal_id}/rollback")
async def rollback_proposal(request: Request, proposal_id: str) -> dict[str, Any]:
    """POST /admin/evolution/proposals/{proposal_id}/rollback — 回滚已激活的提案。

    回滚操作:
    1. Git checkout 恢复 / 删除 Agent 目录
    2. 重新扫描 subagents + 重建 Triage/Executor agent
    """
    _verify_admin(request)
    evo_mgr = _get_evo_mgr(request)

    try:
        proposal = await evo_mgr.rollback_proposal(proposal_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("[EvolutionAdmin] 回滚异常: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"回滚失败: {e}")

    return {
        "proposal": proposal.to_dict(),
        "message": f"Agent '{proposal.agent_name}' 已回滚，Triage + Executor Agent 已重建",
    }


# ═══════════════════════════════════════════════════════════════
# 系统状态
# ═══════════════════════════════════════════════════════════════

@router.get("/status")
async def evolution_status(request: Request) -> dict[str, Any]:
    """GET /admin/evolution/status — 进化系统整体状态。

    返回: 缺口数、提案数（按状态分布）、已激活 Agent 列表、最近扫描时间等。
    """
    _verify_admin(request)
    evo_mgr = _get_evo_mgr(request)
    return await evo_mgr.get_status()


@router.put("/settings")
async def update_settings(
    request: Request,
    body: EvolutionSettings,
) -> dict[str, Any]:
    """PUT /admin/evolution/settings — 更新进化配置（运行时，不持久化到 config）。"""
    _verify_admin(request)
    evo_mgr = _get_evo_mgr(request)

    changes = {}
    if body.scan_interval_hours is not None:
        evo_mgr._scan_interval = int(body.scan_interval_hours * 3600)
        changes["scan_interval_hours"] = body.scan_interval_hours
    if body.analysis_lookback_hours is not None:
        evo_mgr._analysis_lookback = body.analysis_lookback_hours
        changes["analysis_lookback_hours"] = body.analysis_lookback_hours
    if body.min_tasks_for_analysis is not None:
        evo_mgr._min_tasks = body.min_tasks_for_analysis
        changes["min_tasks_for_analysis"] = body.min_tasks_for_analysis
    if body.max_gaps_per_scan is not None:
        evo_mgr._max_gaps = body.max_gaps_per_scan
        changes["max_gaps_per_scan"] = body.max_gaps_per_scan
    if body.validation_min_pass_rate is not None:
        evo_mgr._min_pass_rate = body.validation_min_pass_rate
        changes["validation_min_pass_rate"] = body.validation_min_pass_rate

    return {
        "message": "配置已更新",
        "changes": changes,
    }
