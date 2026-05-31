# async_tools.py — 核心工具（全局通用）
import hashlib
import json
import pytz
import os
import logging

from datetime import datetime
from dotenv import load_dotenv
from langchain.tools import tool
from tavily import AsyncTavilyClient
from langchain_core.runnables import RunnableConfig
from langfuse import observe
from app.redis_manager import redis_manager

# 加载环境变量
load_dotenv("key.env")
tavily_api_key = os.environ.get("TAVILY_API_KEY")
logger = logging.getLogger(__name__)

knowledge_resources = {}

SOURCES_KEY_PREFIX = "sources:"
PENDING_QA_KEY_PREFIX = "pending_qa:"
SOURCES_TTL = 1800       # 30 分钟 — 检索来源引用
PENDING_QA_TTL = 300     # 5 分钟 — 待反馈确认的 Q&A


def register_knowledge_resource(embed_model, rerank_model, llama_chat_model, retrieval_pipeline=None):
    knowledge_resources["embed_model"] = embed_model
    knowledge_resources["rerank_model"] = rerank_model
    knowledge_resources["llama_chat_model"] = llama_chat_model
    knowledge_resources["retrieval_pipeline"] = retrieval_pipeline

@tool
async def async_get_current_time() -> str:
    """获取当前精确时间，当时区、日期、时间相关问题时使用此工具"""
    tz = pytz.timezone('Asia/Shanghai')
    current_time = datetime.now(tz)
    return f"当前北京时间：{current_time.strftime('%Y年%m月%d日 %H时%M分%S秒')}， 星期" \
        f"{['一', '二', '三', '四', '五', '六', '日'][current_time.weekday()]}"


@tool
async def async_web_search(question: str) -> str:
    """当问题涉及新闻、天气、实时消息、最新消息或需要联网查询时使用此工具"""
    client = AsyncTavilyClient(tavily_api_key)
    response = await client.search(query=question)
    return  response


# ═══════════════════════════════════════════════════════════════
@tool
@observe()
async def async_knowledge_query_ask(
        question: str,
        config: RunnableConfig = None,
) -> str:
    """
    查询企业知识库，获取文档内容、专业知识、公司信息等。

    如果知识库有相关内容，返回基于参考资料的答案。
    如果知识库**没有**相关文档（相关度 < 70%），会返回"未找到相关内容"的提示。
    当收到"未找到相关内容"时，你应该告知用户并建议使用 async_web_search 联网搜索。

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

    # ── Step 1-4: RetrievalPipeline 统一检索 ────────────────
    # 内部执行: 自适应改写 → 多路并行检索 + RRF 融合
    #         → 精排 → 动态 Top-K 裁剪
    nodes = await pipeline.retrieve(
        question=question,
        user_id=user_id,
        session_id=session_id,
    )

    if not nodes:
        logger.info(f"[知识库查询] 管道检索未返回结果")
        await redis_manager.delete(f"{SOURCES_KEY_PREFIX}{user_id}:{session_id}")
        return "知识库中未找到相关信息，请尝试其他关键词或上传相关文档。**你必须告知用户此结果，并建议使用 async_web_search 联网搜索。**"

    # ── 相关性兜底检查：最高分低于阈值则视为无相关内容 ──
    MIN_RELEVANCE_SCORE = 0.70  # bge-reranker 分数阈值，低于此值视为不相关
    top_score = nodes[0].score or 0.0
    if top_score < MIN_RELEVANCE_SCORE:
        logger.info(
            "[知识库查询] 最高相关度 %.4f 低于阈值 %.2f，视为无相关内容",
            top_score, MIN_RELEVANCE_SCORE,
        )
        await redis_manager.delete(f"{SOURCES_KEY_PREFIX}{user_id}:{session_id}")
        return "知识库中未找到与您的问题相关的内容（最高相关度低于 70%），建议上传相关文档或尝试其他关键词。**你必须告知用户此结果，并建议使用 async_web_search 联网搜索。**"

    # ── 记录来源文件（按内容去重 + 低分过滤，供前端引用展示） ─
    top_k = min(len(nodes), pipeline.top_k)
    seen_content = set()
    sources_list = []
    SOURCE_MIN_SCORE = MIN_RELEVANCE_SCORE  # 与兜底阈值一致，低于此分不展示来源
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
            "snippet": content[:300],
            "score": round(float(n.score or 0), 4),
            "node_id": n.node.node_id,
        })

    # 写入 Redis（跨进程共享）
    sources_key = f"{SOURCES_KEY_PREFIX}{user_id}:{session_id}"
    if sources_list:
        await redis_manager.set(sources_key, {"sources": sources_list}, ttl=SOURCES_TTL)
    else:
        await redis_manager.delete(sources_key)

    # ── Step 5: 基于检索结果生成回答 ────────────────────────
    llama_model = knowledge_resources.get("llama_chat_model")
    if llama_model is None:
        return "语言模型未初始化"

    # 格式化检索结果为上下文（仅使用通过分数阈值的节点）
    context_parts = []
    context_idx = 0
    for n in nodes[:top_k]:
        if n.score is not None and n.score < SOURCE_MIN_SCORE:
            continue
        context_idx += 1
        source = n.node.metadata.get("file_name", "未知来源")
        content = n.node.get_content()
        context_parts.append(f"[{context_idx}] 来源({source}):\n{content}")
    context_text = "\n\n".join(context_parts)

    system_prompt = (
        "你是一个企业知识库助手。请严格基于以下参考资料回答问题。\n"
        "要求:\n"
        "- 优先使用参考资料回答，必须标注引用编号如 [1][2]\n"
        "- 如果参考资料不足以回答问题，请明确告知用户\"当前知识库暂无相关内容\"\n"
        "- 禁止编造资料中不存在的信息\n"
        "- 使用中文回答\n"
    )

    try:
        logger.info(f"[知识库查询] 检索到 {len(nodes)} 个节点，取 top-{top_k} 生成回答……")
        llm_response = await llama_model.acomplete(
            f"{system_prompt}\n\n"
            f"参考资料:\n{context_text}\n\n"
            f"问题: {question}\n回答:"
        )

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


# ── 工作流引擎集成 ─────────────────────────────────────
_workflow_engine = None


def register_workflow_engine(engine):
    """注册工作流引擎实例（由 main.py lifespan 调用）"""
    global _workflow_engine
    _workflow_engine = engine


@tool
async def async_create_reimbursement_ticket(
    amount: float,
    category: str = "其他",
    description: str = "",
    config: RunnableConfig = None,
) -> str:
    """创建报销审批工单。当用户提出报销申请时使用此工具创建正式的审批流程。

    Args:
        amount: 报销金额（元），必须大于0
        category: 费用类别，可选值：差旅、办公用品、招待、交通、其他
        description: 报销事由说明
    """
    engine = _workflow_engine
    if engine is None:
        return "工作流引擎未初始化，无法创建工单。"

    try:
        user_id = config.get("configurable", {}).get("user_id")
    except Exception:
        return "无法获取用户信息。"

    try:
        ticket = await engine.create_ticket(
            workflow_type="reimbursement",
            user_id=int(user_id),
            title=description or f"{category}报销",
            form_data={
                "amount": amount,
                "category": category,
                "description": description,
            },
        )
        return (
            f"[OK] 报销工单已创建！\n"
            f"工单编号：{ticket.ticket_id}\n"
            f"报销金额：{ticket.form_data.get('amount', amount)}元\n"
            f"费用类别：{category}\n"
            f"当前状态：{ticket.current_step}\n"
            f"待办步骤：{ticket.current_step}\n\n"
            f"审批通过后即可完成打款。请耐心等待审批结果。"
        )
    except Exception as e:
        logger.error(f"创建报销工单失败: {e}", exc_info=True)
        return f"创建工单失败: {e}"
