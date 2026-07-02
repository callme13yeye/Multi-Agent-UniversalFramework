# graph_query.py — 知识图谱查询工具（双通道：Neo4j 图谱 + Milvus 向量）
# 供 Agent 调用，同时从 Neo4j 知识图谱和 Milvus 向量库检索信息。
#
# 设计原则：无论 Agent 选择哪个知识工具，都同时走双通道，
# 避免"选图谱工具就漏了文档原文，选 RAG 工具就漏了实体关系"的问题。

import hashlib
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


def _get_pipeline():
    """从 knowledge_resources 获取 RetrievalPipeline 实例。"""
    return knowledge_resources.get("retrieval_pipeline")


def _extract_user_context(config: RunnableConfig = None):
    """从 RunnableConfig 中提取 user_id 和 session_id。"""
    user_id = None
    session_id = None
    try:
        if config and "configurable" in config:
            user_id = config["configurable"].get("user_id")
            session_id = config["configurable"].get("session_id")
    except Exception:
        pass
    return user_id, session_id


# ═══════════════════════════════════════════════════════════════
# 向量检索辅助
# ═══════════════════════════════════════════════════════════════

async def _vector_search(
    question: str,
    config: RunnableConfig = None,
    top_k: int = 5,
) -> str:
    """执行 Milvus 向量检索，返回格式化的文档片段文本。

    如果管道未初始化或检索失败，返回空字符串。
    """
    pipeline = _get_pipeline()
    if pipeline is None:
        logger.warning("[图谱工具] 检索管道未初始化，跳过向量检索")
        return ""

    user_id, session_id = _extract_user_context(config)
    if user_id is None:
        logger.warning("[图谱工具] 无法获取 user_id，跳过向量检索")
        return ""

    try:
        nodes = await pipeline.retrieve(
            question=question,
            user_id=user_id,
            session_id=session_id,
        )
    except Exception as e:
        logger.warning("[图谱工具] 向量检索失败: %s", e)
        return ""

    if not nodes:
        return ""

    # 格式化向量检索结果
    parts = []
    seen = set()
    count = 0
    for n in nodes[:top_k]:
        if n.score is not None and n.score < 0.75:
            continue
        content = n.node.get_content()
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        if content_hash in seen:
            continue
        seen.add(content_hash)
        count += 1
        source = n.node.metadata.get("file_name", "未知来源")
        parts.append(f"[V{count}] 来源({source}):\n{content[:500]}")

    if not parts:
        return ""

    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# 工具 1: async_graph_query — 图谱关系查询 + 向量检索
# ═══════════════════════════════════════════════════════════════

@register_tool
@tool
@observe()
async def async_graph_query(
    query: str,
    config: RunnableConfig = None,
) -> str:
    """
    查询知识图谱，获取实体间的关联信息，同时也会检索相关文档原文。

    适用场景：
    - "A 和 B 有什么关系？"
    - "哪些政策与 XX 相关？"
    - "这个流程涉及哪些部门？"
    - 需要发现实体间隐含关系的查询

    知识图谱包含从企业文档中抽取的人员、部门、政策、流程、概念等实体，
    以及它们之间的从属、关联、依赖关系。同时返回相关文档原文作为补充。

    Args:
        query: 要查询的问题或关键词（比如实体名称、概念、政策名等）
    """
    kg = _get_kg_service()
    if kg is None or not kg.available:
        return "知识图谱服务当前不可用。"

    logger.info("[工具调用] async_graph_query 被调用，问题: %s", query)

    import asyncio as _asyncio

    # ── 通道 1: Neo4j 图谱检索 ──────────────────────────────
    async def _do_graph():
        keywords = await kg.extract_keywords(query)
        if not keywords:
            return ""
        logger.info("[图谱查询] 关键词: %s", keywords)
        ctx = await kg.graph_rag(
            keywords=keywords,
            question=query,
            max_entities=15,
            max_relations=30,
            max_hops=2,
        )
        return ctx.to_context_text() if not ctx.is_empty() else ""

    # ── 通道 2: Milvus 向量检索（与图谱检索并行，互不依赖） ─
    graph_section, vector_section = await _asyncio.gather(
        _do_graph(), _vector_search(query, config, top_k=5),
    )

    # ── 组装双通道结果 ──────────────────────────────────────
    if not graph_section and not vector_section:
        return f"知识库中未找到与「{query}」相关的实体或文档。"

    parts = []
    if graph_section:
        parts.append(graph_section)
    if vector_section:
        parts.append(f"\n📄 **相关文档原文:**\n{vector_section}")

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# 工具 2: async_graph_search — 实体搜索 + 向量检索
# ═══════════════════════════════════════════════════════════════

@register_tool
@tool
@observe()
async def async_graph_search(
    entity_name: str,
    config: RunnableConfig = None,
) -> str:
    """
    在知识图谱中搜索特定实体，查看其属性和关联，同时检索相关文档原文。

    可以查找：
    - 某个人物的相关信息 + 相关文档
    - 某个部门的职责和关联 + 相关文档
    - 某个政策/制度的详情 + 相关文档
    - 某个概念的关联实体 + 相关文档

    Args:
        entity_name: 要搜索的实体名称（如 "薪酬制度"、"人力资源部"、"张三"）
    """
    kg = _get_kg_service()
    if kg is None or not kg.available:
        return "知识图谱服务当前不可用。"

    logger.info("[工具调用] async_graph_search 被调用，实体: %s", entity_name)

    import asyncio as _asyncio

    # ── 通道 1: Neo4j 实体搜索 + 图谱扩展 ───────────────────
    async def _do_graph():
        entities = await kg.search_entities(entity_name, limit=10)
        ctx = await kg.graph_rag(
            keywords=[entity_name],
            question=f"请介绍 {entity_name} 的详细信息",
            max_entities=10,
            max_relations=20,
            max_hops=1,
        )

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
        return "\n".join(parts) if len(parts) > 1 else ""

    # ── 通道 2: Milvus 向量检索（与图谱检索并行，互不依赖） ─
    vector_question = f"关于 {entity_name} 的详细信息"
    graph_section, vector_section = await _asyncio.gather(
        _do_graph(), _vector_search(vector_question, config, top_k=5),
    )

    # ── 组装双通道结果 ──────────────────────────────────────
    if not graph_section and not vector_section:
        return f"未找到与「{entity_name}」相关的实体或文档。"

    parts = []
    if graph_section:
        parts.append(graph_section)
    if vector_section:
        parts.append(f"\n📄 **相关文档原文:**\n{vector_section}")

    return "\n".join(parts)
