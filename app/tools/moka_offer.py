# moka_offer.py — Offer 管理工具
#
# 工具:
#   async_moka_get_offer_status — 查询候选人 Offer 状态

from __future__ import annotations

import logging

from langchain.tools import tool

from app.tools._registry import register_tool
from app.tools.moka_client import _get_client

logger = logging.getLogger(__name__)


@register_tool
@tool
async def async_moka_get_offer_status(
    candidate_id: str,
) -> str:
    """查询候选人的 Offer 状态。

    返回候选人的 Offer 审批进度，包括审批阶段、薪资信息、预计入职日期等。
    当用户询问 Offer 进度、审批状态、薪资审批情况时使用。

    Args:
        candidate_id: 候选人 ID
    """
    client = _get_client()
    try:
        offer = await client.get_offer_status(candidate_id)
    except Exception as e:
        logger.error("获取 Offer 状态失败: %s", e)
        return f"获取 Offer 状态时发生错误: {e}"

    mode = "Demo 仿真" if client.demo_mode else "Moka"
    basic = offer.get("basicInfo", offer)
    name = basic.get("name", "未知")
    stage = offer.get("stage", offer.get("currentStage", "未知"))
    offer_data = offer.get("offer", {})
    approval = offer_data.get("approvalStatus", "待审批")
    salary = offer_data.get("salaryNumber", "待定")
    checkin = offer_data.get("checkinDate", "待定")
    department = offer_data.get("departmentName", "未知")

    return (
        f"[{mode}] Offer 状态 — {name}\n"
        f"\n📋 候选人: {name}"
        f"\n🏢 部门: {department}"
        f"\n📊 招聘阶段: {stage}"
        f"\n💰 薪资方案: {salary}"
        f"\n📝 审批状态: {approval}"
        f"\n📅 预计入职: {checkin}"
        + ("\n⚠️ 当前为 Demo 模式，数据为仿真数据。" if client.demo_mode else "")
    )
