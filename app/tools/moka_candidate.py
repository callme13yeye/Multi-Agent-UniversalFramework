# moka_candidate.py — 人才搜索工具
#
# 工具:
#   async_moka_search_candidates     — 搜索人才库/候选人
#   async_moka_get_candidate_detail  — 获取候选人详细档案

from __future__ import annotations

import logging

from langchain.tools import tool

from app.tools._registry import register_tool
from app.tools.moka_client import _get_client

logger = logging.getLogger(__name__)


@register_tool
@tool
async def async_moka_search_candidates(
    keywords: str = "",
    experience_years: int | None = None,
    skills: str = "",
    education: str = "",
    limit: int = 10,
) -> str:
    """搜索企业人才库中的候选人。

    根据关键词、工作经验、技能、学历等条件搜索候选人，返回匹配的候选人列表。
    当用户需要找某类人才、筛选简历、或在人才库中搜索时使用此工具。

    如果系统运行在 Demo 模式（无 Moka API Key），返回仿真数据用于演示。

    Args:
        keywords: 搜索关键词（如 "Java"、"产品经理"），支持多个关键词空格分隔
        experience_years: 要求工作经验年限（如 3 表示 3年以上）
        skills: 技能要求（如 "Python,Machine Learning"）
        education: 学历要求（如 "本科"、"硕士"）
        limit: 返回结果数量上限，默认 10
    """
    client = _get_client()
    try:
        candidates = await client.search_candidates(
            keywords=keywords,
            experience_years=experience_years,
            skills=skills,
            education=education,
            limit=limit,
        )
    except Exception as e:
        logger.error("搜索候选人失败: %s", e)
        return f"搜索候选人时发生错误: {e}"

    if not candidates:
        return f"未找到匹配 '{keywords}' 的候选人。建议调整搜索条件或尝试其他关键词。"

    mode = "Demo 仿真" if client.demo_mode else "Moka"
    lines = [f"[{mode}] 搜索 '{keywords}' 找到 {len(candidates)} 位候选人：\n"]
    for i, c in enumerate(candidates, 1):
        name = c.get("name") or c.get("basicInfo", {}).get("name", "未知")
        edu = c.get("highestEducation", c.get("education", "未知"))
        exp = c.get("experienceYears", c.get("experience", "未知"))
        skills_str = c.get("skills", "")
        if isinstance(skills_str, list):
            skills_str = ", ".join(skills_str)
        lines.append(
            f"  [{i}] {name} | 学历: {edu} | 经验: {exp}年"
            f"{' | 技能: ' + skills_str if skills_str else ''}"
        )
    return "\n".join(lines)


@register_tool
@tool
async def async_moka_get_candidate_detail(
    candidate_id: str,
) -> str:
    """获取指定候选人的详细档案。

    返回候选人的完整信息，包括基本信息、教育经历、工作经历、技能标签、
    项目经验、当前招聘阶段等。当用户想深入了解某个候选人时使用。

    Args:
        candidate_id: 候选人 ID（从 async_moka_search_candidates 的结果中获取）
    """
    client = _get_client()
    try:
        detail = await client.get_candidate_detail(candidate_id)
    except Exception as e:
        logger.error("获取候选人详情失败: %s", e)
        return f"获取候选人详情时发生错误: {e}"

    mode = "Demo 仿真" if client.demo_mode else "Moka"
    basic = detail.get("basicInfo", detail)
    name = basic.get("name", "未知")
    email = basic.get("email", "未知")
    phone = basic.get("phone", "未知")
    gender = basic.get("gender", "未知")

    education_list = detail.get("educationInfo", [])
    edu_str = ""
    for edu in education_list[:3]:
        school = edu.get("schoolName", "")
        major = edu.get("major", "")
        degree = edu.get("educationName", "")
        edu_str += f"\n    - {school} | {major} | {degree}"

    experience_list = detail.get("experienceInfo", [])
    exp_str = ""
    for exp in experience_list[:3]:
        company = exp.get("companyName", "")
        position = exp.get("positionName", "")
        duration = exp.get("duration", "")
        exp_str += f"\n    - {company} | {position} | {duration}"

    stage = detail.get("stage", detail.get("currentStage", "未知"))

    return (
        f"[{mode}] 候选人详情 — {name}\n"
        f"\n📋 基本信息"
        f"\n  姓名: {name}"
        f"\n  邮箱: {email}"
        f"\n  电话: {phone}"
        f"\n  性别: {gender}"
        f"\n  当前阶段: {stage}"
        f"\n🎓 教育经历{edu_str or '  无'}"
        f"\n💼 工作经历{exp_str or '  无'}"
    )
