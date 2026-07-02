# resources.py — 非工具基础设施
# 知识库资源、缓存 Key 前缀等，供工具模块和 main.py 使用。

import logging

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 缓存 Key 常量
# ═══════════════════════════════════════════════════════════════

SOURCES_KEY_PREFIX = "sources:"
PENDING_QA_KEY_PREFIX = "pending_qa:"
SOURCES_TTL = 1800       # 30 分钟 — 检索来源引用
PENDING_QA_TTL = 300     # 5 分钟 — 待反馈确认的 Q&A

# ═══════════════════════════════════════════════════════════════
# 知识库资源
# ═══════════════════════════════════════════════════════════════

knowledge_resources = {}


def register_knowledge_resource(
    embed_model, rerank_model, llama_chat_model,
    retrieval_pipeline=None,
    gateway=None,
    knowledge_graph_service=None,
):
    """注册知识库检索所需的模型和管道（由 main.py lifespan 调用）。"""
    knowledge_resources["embed_model"] = embed_model
    knowledge_resources["rerank_model"] = rerank_model
    knowledge_resources["llama_chat_model"] = llama_chat_model
    knowledge_resources["retrieval_pipeline"] = retrieval_pipeline
    if gateway is not None:
        knowledge_resources["gateway"] = gateway
    if knowledge_graph_service is not None:
        knowledge_resources["knowledge_graph_service"] = knowledge_graph_service


def _get_llm_for_retrieval():
    """获取检索答案生成的 LLM（优先从 gateway 动态获取）。"""
    gateway = knowledge_resources.get("gateway")
    if gateway is not None:
        from app.gateway.types import ModelRole
        chain = gateway.get_model_chain(ModelRole.RETRIEVAL_LLM)
        if chain:
            return chain[0][1]
    return knowledge_resources.get("llama_chat_model")


# ═══════════════════════════════════════════════════════════════
# 任务执行器引用（AI 同事后台任务）
# ═══════════════════════════════════════════════════════════════

_task_executor = None


def register_task_executor(executor):
    """注册任务执行器实例（由 main.py lifespan 调用）。"""
    global _task_executor
    _task_executor = executor


def get_task_executor():
    """获取已注册的任务执行器实例（供 create_background_task 工具使用）。"""
    return _task_executor


# ═══════════════════════════════════════════════════════════════
# 死信队列引用
# ═══════════════════════════════════════════════════════════════

_dead_letter_queue = None


def register_dead_letter_queue(dlq):
    """注册死信队列实例（由 main.py lifespan 调用）。"""
    global _dead_letter_queue
    _dead_letter_queue = dlq


def get_dead_letter_queue():
    """获取已注册的死信队列实例（供工具在异常时写入死信）。"""
    return _dead_letter_queue


