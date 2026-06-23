# moka_job.py — 职位管理工具
#
# 工具:
#   async_moka_list_jobs      — 列出在招职位
#   async_moka_get_job_detail — 获取职位 JD 详情

from __future__ import annotations

import logging

from langchain.tools import tool

from app.tools._registry import register_tool
from app.tools.moka_client import _get_client

logger = logging.getLogger(__name__)


@register_tool
@tool
async def async_moka_list_jobs(
    status: str = "active",
    department: str = "",
    limit: int = 10,
) -> str:
    """列出企业在招职位。

    返回当前在招的职位列表，包含职位名称、部门、招聘人数、发布时间等信息。
    当用户想了解有哪些职位在招、查看招聘需求时使用。

    Args:
        status: 职位状态，可选值: active（招聘中）、closed（已关闭）、all（全部）
        department: 按部门筛选（如 "技术部"、"市场部"），为空则列出所有部门
        limit: 返回结果数量上限，默认 10
    """
    client = _get_client()
    try:
        jobs = await client.list_jobs(status=status, department=department, limit=limit)
    except Exception as e:
        logger.error("获取职位列表失败: %s", e)
        return f"获取职位列表时发生错误: {e}"

    if not jobs:
        dep_info = f"「{department}」部门的" if department else ""
        return f"当前没有{status}状态的{dep_info}在招职位。"

    mode = "Demo 仿真" if client.demo_mode else "Moka"
    lines = [f"[{mode}] 在招职位列表 ({len(jobs)} 个)：\n"]
    for i, j in enumerate(jobs, 1):
        title = j.get("title", j.get("jobTitle", "未知"))
        dept = j.get("department", j.get("departmentName", "未知"))
        count = j.get("headcount", j.get("hcCount", "未知"))
        pub_date = j.get("publishDate", j.get("createdAt", "未知"))
        lines.append(
            f"  [{i}] {title} | {dept} | HC: {count} | 发布: {pub_date}"
        )
    return "\n".join(lines)


@register_tool
@tool
async def async_moka_get_job_detail(
    job_id: str,
) -> str:
    """获取指定职位的详细 JD（职位描述）。

    返回职位的完整信息，包括岗位职责、任职要求、薪资范围、工作地点、
    所属部门、招聘人数等。当用户想了解某个职位的具体要求时使用。

    Args:
        job_id: 职位 ID（从 async_moka_list_jobs 的结果中获取）
    """
    client = _get_client()
    try:
        detail = await client.get_job_detail(job_id)
    except Exception as e:
        logger.error("获取职位详情失败: %s", e)
        return f"获取职位详情时发生错误: {e}"

    mode = "Demo 仿真" if client.demo_mode else "Moka"
    title = detail.get("title", detail.get("jobTitle", "未知"))
    dept = detail.get("department", detail.get("departmentName", "未知"))
    location = detail.get("location", detail.get("workCity", "未知"))
    salary = detail.get("salaryRange", detail.get("salary", "面议"))
    hc = detail.get("headcount", detail.get("hcCount", "未知"))
    responsibilities = detail.get("responsibilities", detail.get("description", "暂无描述"))
    requirements = detail.get("requirements", detail.get("qualification", "暂无要求"))

    if isinstance(responsibilities, list):
        responsibilities = "\n".join(f"  - {r}" for r in responsibilities)
    if isinstance(requirements, list):
        requirements = "\n".join(f"  - {r}" for r in requirements)

    return (
        f"[{mode}] 职位详情 — {title}\n"
        f"\n📌 基本信息"
        f"\n  职位名称: {title}"
        f"\n  所属部门: {dept}"
        f"\n  工作地点: {location}"
        f"\n  薪资范围: {salary}"
        f"\n  招聘人数: {hc}"
        f"\n📝 岗位职责:\n{responsibilities}"
        f"\n✅ 任职要求:\n{requirements}"
    )
