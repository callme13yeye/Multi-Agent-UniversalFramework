# knowledge.py — 知识库检索工具
# 企业知识库 RAG 查询（检索管线 → LLM 生成回答 → 缓存）。

import hashlib
import logging

from langchain.tools import tool
from langchain_core.runnables import RunnableConfig
from langfuse import observe

from app.tools._registry import register_tool
from app.tools.resources import (
    SOURCES_KEY_PREFIX,
    PENDING_QA_KEY_PREFIX,
    SOURCES_TTL,
    PENDING_QA_TTL,
    knowledge_resources,
    _get_llm_for_retrieval,
)
from app.redis_manager import redis_manager

logger = logging.getLogger(__name__)


@register_tool
@tool
@observe()
async def async_knowledge_query_ask(
        question: str,
        config: RunnableConfig = None,
) -> str:
    """
    查询企业知识库，获取文档内容、专业知识、公司信息等。

    仅在用户明确询问公司内部文档/政策/制度/规定时使用此工具。
    通用知识问答请直接回答，不需要调用此工具。

    如果知识库有相关内容，返回基于参考资料的答案。
    如果知识库没有相关文档，会返回"未找到相关内容"的提示。

    Args:
        question: 要查询的问题
    """
    logger.info(f"[工具调用] async_knowledge_query_ask 被调用，问题: {question}")
    try:
        user_id = config.get("configurable", {}).get("user_id")
        session_id = config.get("configurable", {}).get("session_id")
    except Exception as e:
        return f"无法获取用户上下文信息, 请重新发起会话。"
    logger.info(f"[知识库查询] 获取到用户信息: user_id={user_id}, session_id={session_id}")

    pipeline = knowledge_resources.get("retrieval_pipeline")
    if pipeline is None:
        return "检索管道未初始化，请稍后再试。"

    # ── 缓存查询：命中则直接返回 ──────────────────────────
    cache_key = redis_manager.build_cache_key(question, user_id=user_id or 0)
    cached = await redis_manager.get(cache_key)
    if cached is not None:
        # 恢复来源文件供前端引用
        sources_key = f"{SOURCES_KEY_PREFIX}{user_id}:{session_id}"
        await redis_manager.set(sources_key, {"sources": cached.get("sources", [])}, ttl=SOURCES_TTL)
        logger.info("[缓存命中] question=%s", question[:40])
        return cached["answer"]

    # ── Step 1-4: RetrievalPipeline 统一检索（含图谱增强） ──
    result = await pipeline.retrieve_with_graph(
        question=question,
        user_id=user_id,
        session_id=session_id,
    )
    nodes = result.get("nodes", [])
    graph_context = result.get("graph_context")

    if not nodes:
        logger.info(f"[知识库查询] 管道检索未返回结果")
        await redis_manager.delete(f"{SOURCES_KEY_PREFIX}{user_id}:{session_id}")
        return "知识库中未找到相关信息。"

    # ── 相关性兜底检查：最高分低于阈值则视为无相关内容 ──
    MIN_RELEVANCE_SCORE = 0.75
    top_score = nodes[0].score or 0.0
    if top_score < MIN_RELEVANCE_SCORE:
        logger.info(
            "[知识库查询] 最高相关度 %.4f 低于阈值 %.2f，视为无相关内容",
            top_score, MIN_RELEVANCE_SCORE,
        )
        await redis_manager.delete(f"{SOURCES_KEY_PREFIX}{user_id}:{session_id}")
        return "知识库中未找到与您的问题相关的内容。"

    # ── 记录来源（含精确位置，供前端定位和引用展示）──
    top_k = min(len(nodes), pipeline.top_k)
    seen_content = set()
    sources_list = []
    SOURCE_MIN_SCORE = MIN_RELEVANCE_SCORE
    for n in nodes[:top_k]:
        if n.score is not None and n.score < SOURCE_MIN_SCORE:
            continue
        content = n.node.get_content()
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        if content_hash in seen_content:
            continue
        seen_content.add(content_hash)
        sources_list.append({
            "file_name": n.node.metadata.get("file_name", "未知来源"),
            "chunk_index": n.node.metadata.get("chunk_index"),
            "chunk_total": n.node.metadata.get("chunk_total"),
            "page_label": n.node.metadata.get("page_label", ""),
            "heading": n.node.metadata.get("heading", ""),
            "chunk_summary": n.node.metadata.get("chunk_summary", content[:80].replace("\n", " ")),
            "snippet": content[:300],
            "score": round(float(n.score or 0), 4),
            "node_id": n.node.node_id,
        })

    # 写入 Redis
    sources_key = f"{SOURCES_KEY_PREFIX}{user_id}:{session_id}"
    if sources_list:
        await redis_manager.set(sources_key, {"sources": sources_list}, ttl=SOURCES_TTL)
    else:
        await redis_manager.delete(sources_key)

    # ── Step 5: 基于检索结果 + 图谱上下文生成回答 ────────────
    llama_model = _get_llm_for_retrieval()
    if llama_model is None:
        return "语言模型未初始化"

    context_parts = []
    context_idx = 0
    for n in nodes[:top_k]:
        if n.score is not None and n.score < SOURCE_MIN_SCORE:
            continue
        context_idx += 1

        # ── 精确位置标签（文档名 + 页码 + 段落，而非仅文件名）──
        file_name = n.node.metadata.get("file_name", "未知来源")
        page_label = n.node.metadata.get("page_label", "")
        heading = n.node.metadata.get("heading", "")
        chunk_idx = n.node.metadata.get("chunk_index")
        chunk_total = n.node.metadata.get("chunk_total")

        location_parts = [f"《{file_name}》"]
        if page_label:
            location_parts.append(f"第{page_label}页")
        if heading:
            location_parts.append(f"「{heading}」")
        elif chunk_idx is not None and chunk_total is not None and chunk_total > 1:
            location_parts.append(f"第{chunk_idx + 1}/{chunk_total}段")
        location_str = " · ".join(location_parts)

        content = n.node.get_content()
        context_parts.append(f"[{context_idx}] {location_str}:\n{content}")
    context_text = "\n\n".join(context_parts)

    # 图谱上下文（知识图谱中的实体和关系）
    graph_text = ""
    if graph_context is not None and hasattr(graph_context, "to_context_text"):
        graph_text = graph_context.to_context_text()
    if graph_text:
        logger.info("[知识库查询] 图谱上下文已注入 (%d 字符)", len(graph_text))

    system_prompt = (
        "你是一个企业知识库助手。请严格基于以下参考资料回答问题。\n"
        "要求:\n"
        "- 每个事实断言必须标注引用出处，格式为 [N]，N 对应参考资料的编号\n"
        "- 引用放在被引内容之后，如：年假为 5 个工作日 [3]\n"
        "- 一段引用可同时引用多个编号，如 [1][4]\n"
        "- 如果参考资料不足以回答问题，请明确告知用户\"当前知识库暂无相关内容\"\n"
        "- 禁止编造资料中不存在的信息\n"
        "- 使用中文回答\n"
    )

    try:
        logger.info(f"[知识库查询] 检索到 {len(nodes)} 个节点，取 top-{top_k} 生成回答……")
        # 将图谱上下文拼接到参考资料之后
        graph_section = f"\n\n知识图谱（实体关系参考）:\n{graph_text}" if graph_text else ""
        prompt = (
            f"{system_prompt}\n\n"
            f"参考资料:\n{context_text}{graph_section}\n\n"
            f"问题: {question}\n回答:"
        )
        llm_response = await llama_model.acomplete(prompt)
        full_response = llm_response.text.strip()
        if not full_response:
            logger.warning("[知识库查询] LLM 生成回答为空")
            return "基于知识库无法生成有效回答。"
        logger.info(f"[知识库查询] 回答生成完成 ({len(full_response)} 字符)")

        # ── 暂存 Q&A，等用户点赞确认后再写 Redis ────────
        pending_key = f"{PENDING_QA_KEY_PREFIX}{user_id}:{session_id}"
        await redis_manager.set(pending_key, {
            "question": question,
            "answer": full_response,
            "sources": sources_list,
            "user_id": user_id,
        }, ttl=PENDING_QA_TTL)
        logger.info("[知识库查询] Q&A 已暂存，等待用户反馈确认缓存: session=%s", session_id)
        return full_response

    except Exception as e:
        logger.error(f"[知识库查询] 回答生成失败: {e}", exc_info=True)
        return f"生成回答时发生错误: {str(e)}"
