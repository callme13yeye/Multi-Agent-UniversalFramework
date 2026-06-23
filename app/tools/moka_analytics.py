# moka_analytics.py — 招聘数据分析工具
#
# 工具:
#   async_moka_get_recruitment_funnel — 招聘漏斗分析

from __future__ import annotations

import logging

from langchain.tools import tool

from app.tools._registry import register_tool
from app.tools.moka_client import _get_client

logger = logging.getLogger(__name__)


@register_tool
@tool
async def async_moka_get_recruitment_funnel(
    date_from: str = "",
    date_to: str = "",
    job_id: str = "",
) -> str:
    """获取招聘漏斗分析数据。

    返回招聘各阶段的转化数据：简历投递 → 初筛通过 → 面试 → Offer → 入职。
    帮助分析招聘效率、发现瓶颈环节。当用户询问招聘数据、转化率、
    招聘效率分析时使用。

    Args:
        date_from: 统计起始日期（格式: YYYY-MM-DD），默认 30 天前
        date_to: 统计截止日期（格式: YYYY-MM-DD），默认今天
        job_id: 按职位筛选，为空则统计全部职位
    """
    client = _get_client()
    try:
        funnel = await client.get_recruitment_funnel(
            date_from=date_from, date_to=date_to, job_id=job_id,
        )
    except Exception as e:
        logger.error("获取招聘漏斗失败: %s", e)
        return f"获取招聘漏斗时发生错误: {e}"

    mode = "Demo 仿真" if client.demo_mode else "Moka"
    stages = [
        ("resume_received", "简历投递"),
        ("resume_screened", "初筛通过"),
        ("interview_scheduled", "面试安排"),
        ("interview_passed", "面试通过"),
        ("offer_sent", "Offer 发放"),
        ("offer_accepted", "Offer 接受"),
        ("onboarded", "已入职"),
    ]

    lines = [f"[{mode}] 招聘漏斗分析\n"]
    prev_count = None
    for key, label in stages:
        count = funnel.get(key, 0)
        if prev_count and prev_count > 0:
            rate = f"(转化率 {count/prev_count*100:.1f}%)"
        else:
            rate = ""
        lines.append(f"  {label}: {count} 人 {rate}")
        prev_count = count

    # 整体转化率
    total_in = funnel.get("resume_received", 0)
    total_out = funnel.get("onboarded", 0)
    if total_in > 0:
        lines.append(f"\n📊 整体转化率: {total_out/total_in*100:.1f}% ({total_out}/{total_in})")

    if client.demo_mode:
        lines.append("\n⚠️ 当前为 Demo 模式，数据为仿真数据。")
    return "\n".join(lines)
