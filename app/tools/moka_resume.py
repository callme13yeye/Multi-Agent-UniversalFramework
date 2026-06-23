# moka_resume.py — 简历推送工具
#
# 工具:
#   async_moka_push_resume — 推送候选人简历到职位

from __future__ import annotations

import logging

from langchain.tools import tool

from app.tools._registry import register_tool
from app.tools.moka_client import _get_client

logger = logging.getLogger(__name__)


@register_tool
@tool
async def async_moka_push_resume(
    job_id: str,
    candidate_name: str,
    email: str = "",
    phone: str = "",
    resume_summary: str = "",
    education: str = "",
    experience: str = "",
) -> str:
    """推送候选人简历到指定职位。

    将候选人的简历信息和基本资料提交到 Moka 系统的指定职位下，
    系统会自动触发简历解析服务。候选人信息会进入该职位的招聘流程。
    当用户要求推荐候选人、投递简历、发起招聘流程时使用。

    ⚠️ 此操作为**写操作**，会实际修改 Moka 数据，执行前应确认用户意图。

    Args:
        job_id: 目标职位 ID
        candidate_name: 候选人姓名
        email: 候选人邮箱
        phone: 候选人电话
        resume_summary: 简历摘要 / 核心能力概述
        education: 最高学历及学校
        experience: 相关工作经历简述
    """
    client = _get_client()
    try:
        result = await client.push_resume(
            job_id=job_id,
            candidate_name=candidate_name,
            email=email,
            phone=phone,
            resume_summary=resume_summary,
            education=education,
            experience=experience,
        )
    except Exception as e:
        logger.error("推送简历失败: %s", e)
        return f"推送简历失败: {e}"

    mode = "Demo 仿真" if client.demo_mode else "Moka"
    application_id = result.get("applicationId", result.get("id", "DEMO-APP-001"))
    return (
        f"[{mode}] ✅ 简历推送成功！\n"
        f"\n候选人: {candidate_name}"
        f"\n目标职位 ID: {job_id}"
        f"\n申请编号: {application_id}"
        f"\n状态: 已进入初筛阶段"
        f"\n\n📌 后续步骤: 简历解析 → HR 初筛 → 面试安排"
        + ("\n⚠️ 当前为 Demo 模式，数据未实际写入 Moka 系统。" if client.demo_mode else "")
    )
