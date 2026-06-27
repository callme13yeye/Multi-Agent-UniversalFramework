# graph_query.py — 知识图谱查询工具
# 供 Agent 调用，从 Neo4j 知识图谱中检索实体、关系和上下文。

import logging

from langchain.tools import tool
from langchain_core.runnables import RunnableConfig
from langfuse import observe

from app.tools._registry import register_tool
from app.tools.resources import knowledge_resources

logger = logging.getLogger(__name__)


def _get_kg_service():
    """从 knowledge_resources 获取 KnowledgeGraphService 实例。"""
    return knowledge_resources.get("knowledge_graph_service")


@register_tool
@tool
@observe()
async def async_graph_query(
    query: str,
    config: RunnableConfig = None,
) -> str:
    """
    查询知识图谱，获取实体间的关联信息。

    适用场景：
    - "A 和 B 有什么关系？"
    - "哪些政策与 XX 相关？"
    - "这个流程涉及哪些部门？"
    - 需要发现实体间隐含关系的查询

    知识图谱包含从企业文档中抽取的人员、部门、政策、流程、概念等实体，
    以及它们之间的从属、关联、依赖关系。

    Args:
        query: 要查询的问题或关键词（比如实体名称、概念、政策名等）
    """
    kg = _get_kg_service()
    if kg is None or not kg.available:
        return "知识图谱服务当前不可用。"

    logger.info("[工具调用] async_graph_query 被调用，问题: %s", query)

    # Step 1: 用 LLM 从问题中提取关键词
    keywords = await _extract_keywords(query, kg)
    if not keywords:
        return "未能从问题中提取到关键词，请尝试使用更具体的问题。"

    logger.info("[图谱查询] 关键词: %s", keywords)

    # Step 2: GraphRAG 检索
    ctx = await kg.graph_rag(
        keywords=keywords,
        question=query,
        max_entities=15,
        max_relations=30,
        max_hops=2,
    )

    if ctx.is_empty():
        return f"知识图谱中未找到与「{query}」相关的实体或关系。"

    context_text = ctx.to_context_text()
    return context_text


async def _extract_keywords(query: str, kg) -> list[str]:
    """用 LLM 从查询中提取关键词（用于图谱搜索）。"""
    llm = kg._get_llm()
    if llm is None:
        # 简单回退：直接按空格/标点分割
        import re
        return [w for w in re.split(r'[，。、；\s,.;]+', query) if len(w) >= 2][:5]

    prompt = f"""从以下问题中提取用于知识图谱搜索的关键词。只输出关键词，每行一个，最多 5 个。

规则：
- 提取实体名称（人名、部门名、政策名、概念等）
- 去掉疑问词和停用词
- 如果没有明确实体，提取核心概念词

问题: {query}

关键词（每行一个）:"""

    try:
        response = await llm.acomplete(prompt)
        keywords = [
            line.strip().lstrip("- ").strip()
            for line in response.text.strip().split("\n")
            if line.strip() and len(line.strip()) >= 2
        ]
        return keywords[:5]
    except Exception:
        import re
        return [w for w in re.split(r'[，。、；\s,.;]+', query) if len(w) >= 2][:5]


@register_tool
@tool
@observe()
async def async_graph_search(
    entity_name: str,
    config: RunnableConfig = None,
) -> str:
    """
    在知识图谱中搜索特定实体，查看其属性和关联。

    可以查找：
    - 某个人物的相关信息
    - 某个部门的职责和关联
    - 某个政策/制度的详情
    - 某个概念的关联实体

    Args:
        entity_name: 要搜索的实体名称（如 "薪酬制度"、"人力资源部"、"张三"）
    """
    kg = _get_kg_service()
    if kg is None or not kg.available:
        return "知识图谱服务当前不可用。"

    logger.info("[工具调用] async_graph_search 被调用，实体: %s", entity_name)

    # 精确搜索实体
    entities = await kg.search_entities(entity_name, limit=10)
    if not entities:
        return f"未找到与「{entity_name}」相关的实体。"

    # GraphRAG 扩展
    ctx = await kg.graph_rag(
        keywords=[entity_name],
        question=f"请介绍 {entity_name} 的详细信息",
        max_entities=10,
        max_relations=20,
        max_hops=1,
    )

    # 组装结果
    parts = [f"📌 **{entity_name}** 的知识图谱信息:\n"]

    if entities:
        parts.append("**直接匹配的实体:**")
        for e in entities[:5]:
            parts.append(
                f"- [{e.get('type', '?')}] **{e.get('name', entity_name)}**"
                f"{': ' + e.get('description', '') if e.get('description') else ''}"
            )

    if not ctx.is_empty():
        parts.append("\n" + ctx.to_context_text())

    return "\n".join(parts)
