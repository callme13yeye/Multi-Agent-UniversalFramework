# moka_interview.py — 面试管理工具
#
# 工具:
#   async_moka_get_interviews — 获取面试日程

from __future__ import annotations

import logging

from langchain.tools import tool

from app.tools._registry import register_tool
from app.tools.moka_client import _get_client

logger = logging.getLogger(__name__)


@register_tool
@tool
async def async_moka_get_interviews(
    date_from: str = "",
    date_to: str = "",
    status: str = "",
    limit: int = 10,
) -> str:
    """获取面试日程安排。

    返回指定时间范围内的面试列表，包含候选人信息、面试时间、面试官、面试类型、
    面试状态等。当用户想了解面试安排、查看面试日程、确认面试时间时使用。

    Args:
        date_from: 开始日期（格式: YYYY-MM-DD），默认为今天
        date_to: 结束日期（格式: YYYY-MM-DD），默认为 7 天后
        status: 面试状态，可选值: scheduled（已安排）、completed（已完成）、cancelled（已取消）
        limit: 返回结果数量上限，默认 10
    """
    client = _get_client()
    try:
        interviews = await client.get_interviews(
            date_from=date_from, date_to=date_to, status=status, limit=limit,
        )
    except Exception as e:
        logger.error("获取面试日程失败: %s", e)
        return f"获取面试日程时发生错误: {e}"

    if not interviews:
        return "当前没有找到符合条件的面试安排。"

    mode = "Demo 仿真" if client.demo_mode else "Moka"
    lines = [f"[{mode}] 面试日程 ({len(interviews)} 场)：\n"]
    for i, iv in enumerate(interviews, 1):
        candidate = iv.get("candidateName", iv.get("candidateInfo", {}).get("name", "未知"))
        job = iv.get("jobTitle", iv.get("jobInfo", {}).get("title", "未知"))
        time = iv.get("interviewTime", iv.get("scheduledAt", "待定"))
        interviewers = iv.get("interviewers", [])
        if isinstance(interviewers, list) and interviewers and isinstance(interviewers[0], dict):
            interviewer_names = ", ".join(
                ir.get("name", "") for ir in interviewers[:3]
            )
        else:
            interviewer_names = ", ".join(str(ir) for ir in interviewers[:3]) if interviewers else "待定"
        iv_status = iv.get("status", "待确认")
        iv_type = iv.get("type", iv.get("interviewType", "视频面试"))
        lines.append(
            f"  [{i}] {candidate} → {job} | {time} | {iv_type}"
            f" | 面试官: {interviewer_names} | {iv_status}"
        )
    return "\n".join(lines)
