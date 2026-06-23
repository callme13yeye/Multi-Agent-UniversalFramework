# moka_client.py — Moka API 客户端（共享基础设施）
#
# 本模块提供 MokaClient 类、Demo 仿真数据、客户端懒加载等基础设施，
# 供 moka_candidate / moka_job / moka_resume / moka_interview /
# moka_offer / moka_analytics 等工具模块使用。
#
# 本模块不注册任何工具 — 仅导出 MokaClient 和 _get_client()。

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from dotenv import load_dotenv

load_dotenv("key.env")
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

MOKA_BASE_URL = "https://api.mokahr.com"
MOKA_API_PREFIX = "/api-platform/v1"


# ═══════════════════════════════════════════════════════════════
# Moka API 客户端
# ═══════════════════════════════════════════════════════════════

class MokaClient:
    """Moka Open API 异步客户端。

    封装 HTTP Basic Auth 认证和常用 API 调用。
    未配置 API Key 时自动进入 Demo 模式，返回仿真数据。
    """

    def __init__(
        self,
        api_key: str | None = None,
        org_id: str | None = None,
        base_url: str = MOKA_BASE_URL,
    ):
        self.api_key = api_key
        self.org_id = org_id
        self.base_url = base_url.rstrip("/")
        self._demo_mode = not api_key

        if self._demo_mode:
            logger.warning(
                "⚠️  MOKA_API_KEY 未配置 — moka 工具运行在 Demo 模式，"
                "所有 API 调用返回仿真数据，仅用于演示。"
            )
        else:
            logger.info("Moka 客户端已初始化: base_url=%s org_id=%s", self.base_url, self.org_id)

    @property
    def demo_mode(self) -> bool:
        return self._demo_mode

    def _auth_header(self) -> str:
        """构建 Basic Auth header。"""
        credentials = base64.b64encode(f"{self.api_key}:".encode()).decode()
        return f"Basic {credentials}"

    async def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_data: dict | None = None,
    ) -> dict[str, Any]:
        """发送 HTTP 请求到 Moka API。"""
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, url,
                params=params, json=json_data, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    logger.error("Moka API 错误 [%s] %s: %s", resp.status, path, text[:500])
                    raise RuntimeError(f"Moka API 返回 {resp.status}: {text[:300]}")
                return await resp.json() if text else {}

    # ── API 方法 ─────────────────────────────────────────────

    async def search_candidates(
        self,
        keywords: str = "",
        experience_years: int | None = None,
        skills: str = "",
        education: str = "",
        limit: int = 10,
    ) -> list[dict]:
        """搜索人才库候选人。"""
        if self._demo_mode:
            return _demo_search_candidates(keywords, experience_years, skills, education, limit)

        params = {
            "keyword": keywords,
            "limit": min(limit, 50),
        }
        if experience_years is not None:
            params["experienceYears"] = experience_years
        if skills:
            params["skills"] = skills
        if education:
            params["education"] = education

        result = await self._request("GET", f"{MOKA_API_PREFIX}/data/ehrApplications", params=params)
        return result.get("data", [])[:limit]

    async def get_candidate_detail(self, candidate_id: str) -> dict:
        """获取候选人详细信息。"""
        if self._demo_mode:
            return _demo_candidate_detail(candidate_id)

        result = await self._request(
            "GET",
            f"{MOKA_API_PREFIX}/data/ehrApplications",
            params={"applicationId": candidate_id},
        )
        data = result.get("data", [])
        if not data:
            raise RuntimeError(f"未找到候选人: {candidate_id}")
        return data[0]

    async def list_jobs(
        self,
        status: str = "active",
        department: str = "",
        limit: int = 10,
    ) -> list[dict]:
        """获取在招职位列表。"""
        if self._demo_mode:
            return _demo_list_jobs(status, department, limit)

        params: dict[str, Any] = {"limit": min(limit, 50)}
        if status:
            params["status"] = status
        if department:
            params["department"] = department

        result = await self._request("GET", f"{MOKA_API_PREFIX}/jobs", params=params)
        return result.get("data", [])[:limit]

    async def get_job_detail(self, job_id: str) -> dict:
        """获取职位详情。"""
        if self._demo_mode:
            return _demo_job_detail(job_id)

        result = await self._request("GET", f"{MOKA_API_PREFIX}/jobs/{job_id}")
        return result.get("data", {})

    async def push_resume(
        self,
        job_id: str,
        candidate_name: str,
        email: str,
        phone: str,
        resume_summary: str = "",
        education: str = "",
        experience: str = "",
    ) -> dict:
        """推送候选人简历到指定职位。"""
        if self._demo_mode:
            return _demo_push_resume(job_id, candidate_name, email, phone)

        json_data = {
            "basicInfo": {
                "name": candidate_name,
                "email": email,
                "phone": phone,
            },
            "jobIntention": {
                "jobId": job_id,
            },
        }
        if resume_summary:
            json_data["basicInfo"]["summary"] = resume_summary
        if education:
            json_data["educationInfo"] = [{"schoolName": education}]
        if experience:
            json_data["experienceInfo"] = [{"description": experience}]

        result = await self._request(
            "POST",
            f"{MOKA_API_PREFIX}/jobs/{self.org_id}/{job_id}/apply",
            json_data=json_data,
        )
        return result.get("data", result)

    async def get_interviews(
        self,
        date_from: str = "",
        date_to: str = "",
        status: str = "",
        limit: int = 10,
    ) -> list[dict]:
        """获取面试日程。"""
        if self._demo_mode:
            return _demo_interviews(date_from, date_to, status, limit)

        params: dict[str, Any] = {"limit": min(limit, 50)}
        if date_from:
            params["startDate"] = date_from
        if date_to:
            params["endDate"] = date_to
        if status:
            params["status"] = status

        result = await self._request("GET", f"{MOKA_API_PREFIX}/interviews", params=params)
        return result.get("data", [])[:limit]

    async def get_recruitment_funnel(
        self,
        date_from: str = "",
        date_to: str = "",
        job_id: str = "",
    ) -> dict:
        """获取招聘漏斗数据。"""
        if self._demo_mode:
            return _demo_funnel(date_from, date_to, job_id)

        params: dict[str, Any] = {}
        if date_from:
            params["startDate"] = date_from
        if date_to:
            params["endDate"] = date_to
        if job_id:
            params["jobId"] = job_id

        result = await self._request(
            "GET", f"{MOKA_API_PREFIX}/bi/recruitment-funnel", params=params,
        )
        return result.get("data", result)

    async def get_offer_status(self, candidate_id: str) -> dict:
        """查询候选人 Offer 状态。"""
        if self._demo_mode:
            return _demo_offer_status(candidate_id)

        result = await self._request(
            "GET",
            f"{MOKA_API_PREFIX}/data/ehrApplications",
            params={"applicationId": candidate_id, "stage": "offer"},
        )
        data = result.get("data", [])
        if not data:
            raise RuntimeError(f"未找到该候选人的 Offer 记录: {candidate_id}")
        return data[0]


# ═══════════════════════════════════════════════════════════════
# 客户端懒加载
# ═══════════════════════════════════════════════════════════════

_client: MokaClient | None = None


def _get_client() -> MokaClient:
    """获取 Moka 客户端（优先从 resources 获取，回退到环境变量初始化）。"""
    global _client
    if _client is not None:
        return _client

    # 尝试从 resources 获取（main.py lifespan 中注册的实例）
    from app.tools.resources import get_moka_client as _get_registered
    registered = _get_registered()
    if registered is not None:
        _client = registered
        return _client

    # 回退：从环境变量创建
    api_key = os.environ.get("MOKA_API_KEY")
    org_id = os.environ.get("MOKA_ORG_ID")
    _client = MokaClient(api_key=api_key, org_id=org_id)
    return _client


# ═══════════════════════════════════════════════════════════════
# Demo 模式 — 仿真数据
# ═══════════════════════════════════════════════════════════════

def _demo_search_candidates(
    keywords: str,
    experience_years: int | None,
    skills: str,
    education: str,
    limit: int,
) -> list[dict]:
    """Demo 模式：返回仿真候选人搜索数据。"""
    pool = [
        {
            "name": "张明远", "highestEducation": "硕士", "experienceYears": 5,
            "skills": ["Java", "Spring Boot", "Microservices", "Kubernetes"],
            "currentStage": "初筛",
        },
        {
            "name": "李雪华", "highestEducation": "本科", "experienceYears": 3,
            "skills": ["Python", "Machine Learning", "PyTorch", "NLP"],
            "currentStage": "面试",
        },
        {
            "name": "王子涵", "highestEducation": "硕士", "experienceYears": 7,
            "skills": ["Golang", "Distributed Systems", "gRPC", "Redis"],
            "currentStage": "Offer",
        },
        {
            "name": "陈思雨", "highestEducation": "本科", "experienceYears": 4,
            "skills": ["React", "TypeScript", "Node.js", "GraphQL"],
            "currentStage": "初筛",
        },
        {
            "name": "刘浩然", "highestEducation": "博士", "experienceYears": 2,
            "skills": ["Deep Learning", "Computer Vision", "TensorFlow"],
            "currentStage": "简历筛选",
        },
        {
            "name": "赵雅琪", "highestEducation": "硕士", "experienceYears": 6,
            "skills": ["Product Management", "Data Analysis", "SQL", "Agile"],
            "currentStage": "初筛",
        },
        {
            "name": "孙伟杰", "highestEducation": "本科", "experienceYears": 8,
            "skills": ["Java", "Spring Cloud", "MySQL", "Elasticsearch"],
            "currentStage": "面试",
        },
        {
            "name": "周雨萱", "highestEducation": "硕士", "experienceYears": 3,
            "skills": ["UI Design", "Figma", "Design System", "User Research"],
            "currentStage": "初筛",
        },
    ]

    kw_lower = keywords.lower() if keywords else ""
    filtered = []
    for c in pool:
        if kw_lower:
            name_match = kw_lower in c["name"]
            skill_match = any(kw_lower in s.lower() for s in c["skills"])
            if not (name_match or skill_match):
                continue
        if experience_years and c["experienceYears"] < experience_years:
            continue
        if education and education not in c["highestEducation"]:
            continue
        filtered.append(c)

    return filtered[:limit]


def _demo_candidate_detail(candidate_id: str) -> dict:
    """Demo 模式：返回仿真候选人详情。"""
    return {
        "basicInfo": {
            "name": "张明远",
            "email": "zhangmingyuan@example.com",
            "phone": "138****5678",
            "gender": "男",
        },
        "educationInfo": [
            {"schoolName": "电子科技大学", "major": "计算机科学与技术", "educationName": "硕士"},
            {"schoolName": "四川大学", "major": "软件工程", "educationName": "本科"},
        ],
        "experienceInfo": [
            {"companyName": "字节跳动", "positionName": "高级后端工程师", "duration": "2021-2024"},
            {"companyName": "美团", "positionName": "Java 开发工程师", "duration": "2019-2021"},
        ],
        "skills": ["Java", "Spring Boot", "Microservices", "Kubernetes", "MySQL", "Redis"],
        "currentStage": "初筛",
    }


def _demo_list_jobs(status: str, department: str, limit: int) -> list[dict]:
    """Demo 模式：返回仿真职位列表。"""
    all_jobs = [
        {"title": "高级 Java 工程师", "department": "技术部", "headcount": 3, "publishDate": "2026-05-20"},
        {"title": "AI 大模型应用工程师", "department": "技术部", "headcount": 2, "publishDate": "2026-06-01"},
        {"title": "高级产品经理", "department": "产品部", "headcount": 1, "publishDate": "2026-05-15"},
        {"title": "前端开发工程师", "department": "技术部", "headcount": 2, "publishDate": "2026-05-25"},
        {"title": "数据分析师", "department": "数据部", "headcount": 1, "publishDate": "2026-04-10"},
        {"title": "DevOps 工程师", "department": "技术部", "headcount": 1, "publishDate": "2026-05-28"},
        {"title": "HRBP", "department": "人力资源部", "headcount": 1, "publishDate": "2026-06-03"},
        {"title": "市场运营经理", "department": "市场部", "headcount": 1, "publishDate": "2026-05-18"},
    ]
    filtered = []
    for j in all_jobs:
        if department and department not in j["department"]:
            continue
        filtered.append(j)
    return filtered[:limit]


def _demo_job_detail(job_id: str) -> dict:
    """Demo 模式：返回仿真 JD 详情。"""
    return {
        "title": "AI 大模型应用工程师",
        "department": "技术部",
        "location": "成都",
        "salaryRange": "30K-50K·14薪",
        "headcount": 2,
        "description": (
            "1. 参与公司核心 AI Agent 方向的应用架构设计与开发\n"
            "2. 负责大模型应用层的构建，包括 Prompt 工程、RAG 检索增强生成、Agent 编排等\n"
            "3. 基于 LlamaIndex/LangChain 等框架搭建企业级多 Agent 协作系统\n"
            "4. 与产品团队、前后端工程师紧密协作，推动大模型能力在产品中落地\n"
            "5. 构建和优化高质量行业数据集，提升模型在招聘领域的泛化能力\n"
            "6. 跟踪前沿技术（Multi-Agent、GraphRAG、长上下文优化等），推动技术落地"
        ),
        "requirements": (
            "1. 本科及以上学历，计算机、人工智能、数学等相关专业优先\n"
            "2. 至少 1 年大模型相关项目经验，熟悉 NLP 常见任务\n"
            "3. 熟练掌握 Prompt 设计与调优，熟悉模型微调与评估方法\n"
            "4. 精通 Python，熟悉 PyTorch 或 TensorFlow\n"
            "5. 熟悉 LlamaIndex、LangChain、LangGraph 等框架\n"
            "6. 有 Multi-Agent 系统开发经验者优先\n"
            "7. 有开源项目贡献或竞赛获奖经历者加分"
        ),
    }


def _demo_push_resume(
    job_id: str, candidate_name: str, email: str, phone: str
) -> dict:
    """Demo 模式：返回仿真推送结果。"""
    import random
    return {
        "applicationId": f"DEMO-APP-{random.randint(1000, 9999)}",
        "jobId": job_id,
        "candidateName": candidate_name,
        "status": "resume_screening",
        "createdAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _demo_interviews(
    date_from: str, date_to: str, _status: str, limit: int
) -> list[dict]:
    """Demo 模式：返回仿真面试日程。"""
    today = datetime.now()
    interviews = [
        {
            "candidateName": "李雪华", "jobTitle": "AI 大模型应用工程师",
            "interviewTime": (today + timedelta(days=1)).strftime("%Y-%m-%d 14:00"),
            "interviewType": "视频面试", "interviewers": [{"name": "王技术总监"}, {"name": "张HR"}],
            "status": "scheduled",
        },
        {
            "candidateName": "孙伟杰", "jobTitle": "高级 Java 工程师",
            "interviewTime": (today + timedelta(days=2)).strftime("%Y-%m-%d 10:00"),
            "interviewType": "现场面试", "interviewers": [{"name": "李架构师"}, {"name": "赵经理"}],
            "status": "scheduled",
        },
        {
            "candidateName": "王子涵", "jobTitle": "DevOps 工程师",
            "interviewTime": (today - timedelta(days=2)).strftime("%Y-%m-%d 15:30"),
            "interviewType": "视频面试", "interviewers": [{"name": "陈技术总监"}],
            "status": "completed",
        },
        {
            "candidateName": "刘浩然", "jobTitle": "AI 大模型应用工程师",
            "interviewTime": (today + timedelta(days=3)).strftime("%Y-%m-%d 09:30"),
            "interviewType": "视频面试", "interviewers": [{"name": "王技术总监"}, {"name": "张HR"}],
            "status": "scheduled",
        },
    ]
    return interviews[:limit]


def _demo_funnel(date_from: str, date_to: str, job_id: str) -> dict:
    """Demo 模式：返回仿真招聘漏斗数据。"""
    return {
        "resume_received": 245,
        "resume_screened": 86,
        "interview_scheduled": 42,
        "interview_passed": 18,
        "offer_sent": 8,
        "offer_accepted": 6,
        "onboarded": 5,
    }


def _demo_offer_status(candidate_id: str) -> dict:
    """Demo 模式：返回仿真 Offer 状态。"""
    return {
        "basicInfo": {"name": "王子涵"},
        "currentStage": "Offer",
        "offer": {
            "approvalStatus": "部门负责人审批中",
            "salaryNumber": "45K·14薪",
            "checkinDate": "2026-07-01",
            "departmentName": "技术部",
        },
    }
