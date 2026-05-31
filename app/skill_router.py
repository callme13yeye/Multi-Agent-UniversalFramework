"""Skill routing middleware — 实现渐进式披露 (Progressive Disclosure)。

通过分析用户 query，仅激活相关的技能（skills）注入 system prompt，
减少无关技能占用的上下文窗口，提高模型聚焦能力。

用法：在 async_create_agent.py 中放置在 SkillsMiddleware 之前。

==============渐进式披露 ============
传统做法: 所有技能元数据全部注入 system prompt → token 浪费 + 模型注意力分散。
本方案: 实时分析用户 query → 关键词匹配 → 仅激活相关技能 →
        SkillsMiddleware 只渲染相关技能 metadata → 上下文高效利用。
可升级: 关键词匹配 → embedding 语义匹配 → LLM 路由 (架构不变)
=====================================
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.middleware import TurnAwareMiddleware
from langchain.agents.middleware.types import (
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

logger = logging.getLogger(__name__)

# ── 技能领域关键词映射 ──────────────────────────────────────────────
# 每个技能关联一组领域关键词（中文 + 英文），用于快速匹配用户 query。
# 匹配到的关键词越多，该技能的 relevance 分数越高。
SKILL_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "engineering": [
        "代码",
        "架构",
        "技术选型",
        "开发",
        "编程",
        "部署",
        "代码审查",
        "技术方案",
        "系统设计",
        "API",
        "接口",
        "数据库",
        "前端",
        "后端",
        "code",
        "architecture",
        "development",
        "programming",
        "deploy",
        "technical",
        "system design",
        "api",
        "database",
        "frontend",
        "backend",
        "git",
        "ci/cd",
        "测试",
        "test",
        "bug",
        "重构",
        "refactor",
        "微服务",
        "microservice",
        "docker",
        "kubernetes",
        "k8s",
    ],
    "finance": [
        "财务",
        "报销",
        "会计",
        "预算",
        "税务",
        "审计",
        "发票",
        "工资",
        "finance",
        "accounting",
        "budget",
        "tax",
        "audit",
        "invoice",
        "payroll",
        "reimbursement",
        "expense",
        "revenue",
        "财务报表",
        "利润",
        "成本",
        "资产",
        "负债",
        "现金流",
        "cfa",
        "会计准则",
    ],
    "hr": [
        "招聘",
        "面试",
        "薪酬",
        "福利",
        "培训",
        "绩效",
        "考勤",
        "员工关系",
        "hr",
        "human resources",
        "recruiting",
        "interview",
        "compensation",
        "benefits",
        "training",
        "performance",
        "attendance",
        "employee",
        "入职",
        "离职",
        "社保",
        "公积金",
        "劳动合同",
        "休假",
        "加班",
        "企业文化",
        "组织发展",
        "od",
        "人才发展",
        "td",
    ],
    "business": [
        "销售",
        "市场",
        "客户",
        "营销",
        "推广",
        "商机",
        "合同",
        "订单",
        "business",
        "sales",
        "marketing",
        "customer",
        "promotion",
        "leads",
        "contract",
        "order",
        "渠道",
        "合作伙伴",
        "报价",
        "投标",
        "bid",
        "proposal",
        "增长",
        "增长策略",
        "gmv",
        "roi",
        "转化率",
        "品牌",
        "竞品分析",
    ],
    "operations": [
        "运维",
        "服务器",
        "网络",
        "监控",
        "告警",
        "防火墙",
        "operations",
        "server",
        "network",
        "monitoring",
        "alert",
        "firewall",
        "IP",
        "空闲IP",
        "子网",
        "DNS",
        "VPN",
        "机房",
        "带宽",
        "负载均衡",
        "ping",
        "ssh",
        "linux",
        "windows server",
        "容器",
        "k8s",
        "nginx",
        "证书",
        "备份",
        "容灾",
        "sla",
    ],
    "web_scraper": [
        "爬虫",
        "爬取",
        "抓取",
        "网页",
        "页面",
        "采集",
        "网站",
        "crawl",
        "scrape",
        "spider",
        "web scraping",
        "页面解析",
        "数据提取",
        "字段提取",
        "notice_list",
        "notice_detail",
        "zc-paimai",
        "淘宝拍卖",
        "资产拍卖",
        "阿里拍卖",
        "taobao",
        "paimai",
        "公告列表",
        "公告详情",
        "自动采集",
        "翻页",
        "分页",
        "pagination",
        "HTML提取",
        "html",
    ],
}

# 每次最多激活的技能数（防止过度裁剪）
MAX_ACTIVE_SKILLS = 3

# 兜底：当 query 无任何关键词匹配时，保留所有技能（安全 fallback）
FALLBACK_TO_ALL = True


def _score_skill(query: str, skill_name: str, keywords: list[str]) -> float:
    """计算 query 与某个技能的相关性分数。"""
    q = query.lower()
    score = 0.0
    for kw in keywords:
        if kw.lower() in q:
            score += 1.0
    return score


def route_query_to_skills(
    query: str,
    available_skills: list[dict[str, Any]],
) -> list[str]:
    """根据用户 query 分析并选出最相关的技能名称列表。

    Args:
        query: 用户当前输入（取最后一条消息的文本）。
        available_skills: 从 SkillsMiddleware 加载的完整技能 metadata。

    Returns:
        排序后的相关技能名称列表（最多 MAX_ACTIVE_SKILLS 个）。
        若没有任何匹配且 FALLBACK_TO_ALL=True，返回全部技能名。
    """
    if not query or not query.strip():
        # 空 query 时保留全部（首次进入等场景）
        return [s["name"] for s in available_skills]

    skill_names = {s["name"] for s in available_skills}
    scores: dict[str, float] = {}

    for skill in available_skills:
        name = skill["name"]
        if name not in SKILL_DOMAIN_KEYWORDS:
            continue
        score = _score_skill(query, name, SKILL_DOMAIN_KEYWORDS[name])
        if score > 0:
            scores[name] = score

    # 按分数降序排列，取 top N
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    active = [name for name, _ in ranked[:MAX_ACTIVE_SKILLS]]

    # 兜底
    if not active:
        return [s["name"] for s in available_skills] if FALLBACK_TO_ALL else []

    return active


class SkillRouterMiddleware(TurnAwareMiddleware):
    """技能路由中间件。

    在 SkillsMiddleware 之前运行，分析用户最新 query 并过滤
    ``skills_metadata``，使得 SkillsMiddleware 只将相关技能的
    摘要注入 system prompt，实现渐进式披露。
    """

    state_schema: type[AgentState[Any]] = AgentState  # type: ignore[type-arg]

    def _filter_skills(
        self,
        state: AgentState[Any],
        query: str,
    ) -> list[dict[str, Any]] | None:
        """过滤 skills_metadata，返回新列表；若无需过滤则返回 None。"""
        all_skills = state.get("skills_metadata")
        if not all_skills:
            return None

        active_names = route_query_to_skills(query, all_skills)
        filtered = [s for s in all_skills if s["name"] in active_names]

        # 若过滤结果为空或与原始一致，跳过
        if not filtered or len(filtered) == len(all_skills):
            return None

        logger.info(
            "技能路由: query=%.50s | 全部=%s 激活=%s",
            query,
            [s["name"] for s in all_skills],
            [s["name"] for s in filtered],
        )
        return filtered

    def _apply_filter(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """同步版本的过滤逻辑。"""
        query = self._extract_query(request.messages)
        filtered = self._filter_skills(request.state, query)
        if filtered is not None:
            new_state = {**request.state, "skills_metadata": filtered}
            request = request.override(state=new_state)
        return handler(request)

    async def _aapply_filter(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """异步版本的过滤逻辑。"""
        query = self._extract_query(request.messages)
        filtered = self._filter_skills(request.state, query)
        if filtered is not None:
            new_state = {**request.state, "skills_metadata": filtered}
            request = request.override(state=new_state)
        return await handler(request)

    # ── AgentMiddleware hooks ──────────────────────────────────────

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """在模型调用前，按需过滤 skills_metadata（仅首次调用）。"""
        if not self._is_first_call_after_human_input(request.messages):
            return handler(request)
        return self._apply_filter(request, handler)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """(async) 在模型调用前，按需过滤 skills_metadata（仅首次调用）。"""
        if not self._is_first_call_after_human_input(request.messages):
            return await handler(request)
        return await self._aapply_filter(request, handler)
