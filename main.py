# main.py
import asyncio
import os
import sys
# 猴子补丁，官方BUG
from langgraph.store.base.batch import AsyncBatchedBaseStore
_original_del = AsyncBatchedBaseStore.__del__
def safe_del(self):
    if hasattr(self, '_task'):
        _original_del(self)
AsyncBatchedBaseStore.__del__ = safe_del

from app.tools import (
    register_knowledge_resource,
    register_task_executor,
    SOURCES_KEY_PREFIX,
    PENDING_QA_KEY_PREFIX,
)
from app.retrieval import RetrievalPipeline
from app.agent_definitions import discover_specialist_agents
from app.prompts import build_triage_prompt, build_executor_prompt
from app.harness import EventBus, TaskExecutor, TaskHandle, TaskStatus, DeadLetterQueue
from app.task_context import TaskContextManager, JournalEntry
from langchain.messages import AIMessageChunk
from app.async_create_agent import async_create_agent
from app.async_load_model import AsyncLoadModel
from app.pg_database import pg_db_manager
from app.milvus_manager import milvus_db_manager
from app.redis_manager import redis_manager
from app.neo4j_manager import neo4j_manager
from app.knowledge_graph import knowledge_graph_service
from app.index_manager import index_manager
from app.auth import get_current_user, get_current_user_sse
from app.pydantic_models import ChatRequest, ChatContext, FeedbackRequest, FeedbackResponse, SourcesResponse
from app.routes import auth_routes, session_routes, upload_routes, document_routes
from app.async_ensure_user_skills_init import ensure_user_skills_init
from app.routes.admin_routes import router as admin_router
from app.evolution.admin_router import router as evolution_router
from app.status_handler import StatusCallbackHandler
from app.gateway import ModelGateway, ModelRole, ModelSpec
from app.gateway.rate_limit_middleware import RateLimitMiddleware
from app.trace_context import trace_context, TraceIdFilter
from miniopy_async import Minio
from config import get_config

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from typing import AsyncGenerator
from sse_starlette.sse import EventSourceResponse
from contextlib import asynccontextmanager
from langfuse.langchain import CallbackHandler

import uvicorn
import uuid
import json
import logging


# 日志配置 — 注入 trace_id 过滤器
logging.getLogger().addFilter(TraceIdFilter())
logger = logging.getLogger("uvicorn")

# 初始化模型和索引
config = get_config()
embed_model_path_dir = config.get("embed_model_path")
rerank_model_path_dir = config.get("rerank_model_path")
langchain_chat_model_name = config.get("langchain_chat_model_name")
llama_chat_model_name = config.get("llama_chat_model_name")
fallback_model_name = config.get("fallback_model_name")

tools: list = []

# ── 构建 Triage Agent 工具集 ──────────────────────────────────
# Triage 直接持有通用工具，简单问题（时间/搜索/知识库）无需委托 Specialist
# 复杂招聘任务仍通过 task 工具委托给 Specialist，或通过 create_background_task 转为后台
from app.tools import TOOL_REGISTRY as _TOOL_REGISTRY
if "create_background_task" in _TOOL_REGISTRY:
    tools.append(_TOOL_REGISTRY["create_background_task"])
if "get_task_status" in _TOOL_REGISTRY:
    tools.append(_TOOL_REGISTRY["get_task_status"])
if "async_get_current_time" in _TOOL_REGISTRY:
    tools.append(_TOOL_REGISTRY["async_get_current_time"])
if "async_web_search" in _TOOL_REGISTRY:
    tools.append(_TOOL_REGISTRY["async_web_search"])
if "async_knowledge_query_ask" in _TOOL_REGISTRY:
    tools.append(_TOOL_REGISTRY["async_knowledge_query_ask"])
if "async_graph_query" in _TOOL_REGISTRY:
    tools.append(_TOOL_REGISTRY["async_graph_query"])
if "async_graph_search" in _TOOL_REGISTRY:
    tools.append(_TOOL_REGISTRY["async_graph_search"])


async def initialize_model():
    embed_model = await AsyncLoadModel.async_local_load_embed_model(embed_model_path_dir)
    rerank_model = await AsyncLoadModel.async_local_load_rerank_model(rerank_model_path_dir)
    langchain_chat_llm = await AsyncLoadModel.async_langchain_api_model(langchain_chat_model_name)
    llama_chat_llm = await AsyncLoadModel.async_llama_index_api_model(llama_chat_model_name)
    langchain_fallback_chat_llm = await AsyncLoadModel.async_langchain_fallback_api_model(fallback_model_name)
    llama_fallback_chat_llm = await AsyncLoadModel.async_llama_fallback_api_model(fallback_model_name)

    # ── 启动时校验主备模型联通性 ──
    try:
        test_resp = await langchain_chat_llm.ainvoke(
            [{"role": "user", "content": "ping"}]
        )
        logger.info(f"[主模型] ✅ {langchain_chat_model_name} 联通正常")
    except Exception as e:
        logger.warning(f"[主模型] ❌ {langchain_chat_model_name} 不可用: {e}")

    try:
        test_resp = await langchain_fallback_chat_llm.ainvoke(
            [{"role": "user", "content": "ping"}]
        )
        logger.info(f"[备用模型] ✅ {fallback_model_name} 联通正常")
    except Exception as e:
        logger.warning(f"[备用模型] ❌ {fallback_model_name} 不可用: {e}")

    return {
        "embed_model": embed_model,
        "rerank_model": rerank_model,
        "langchain_chat_llm": langchain_chat_llm,
        "llama_chat_llm": llama_chat_llm,
        "langchain_fallback_chat_llm": langchain_fallback_chat_llm,
        "llama_fallback_chat_llm": llama_fallback_chat_llm,
    }


async def _load_model_for_role(name: str, provider: str, api_key_env: str, base_url_env: str, interface: str):
    """根据 provider + interface 加载模型实例。

    此函数封装了不同 provider 的加载差异，
    使 ModelGateway 注册时不需要关心具体 API 类型。
    """
    if provider == "deepseek":
        if interface == "langchain":
            return await AsyncLoadModel.async_langchain_api_model(name)
        else:
            return await AsyncLoadModel.async_llama_index_api_model(name)
    elif provider == "bailian":
        if interface == "langchain":
            return await AsyncLoadModel.async_langchain_fallback_api_model(name)
        else:
            return await AsyncLoadModel.async_llama_fallback_api_model(name)
    else:
        raise ValueError(f"未知 provider: {provider}")
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("应用启动中...")
    try:
        await pg_db_manager.initialize()
        app.state.pg_db_manager = pg_db_manager
        app.state.checkpointer = pg_db_manager.checkpointer
        app.state.store = pg_db_manager.store
    except Exception as e:
        logger.critical(f"数据库初始化失败，应用退出: {e}", exc_info=True)
        raise e

    # Milvus 管理员连接初始化（用于用户注册时自动置备 Milvus 资源）
    try:
        await milvus_db_manager.initialize()
    except Exception as e:
        logger.critical(f"Milvus 初始化失败，应用退出: {e}", exc_info=True)
        raise e

    # Redis 缓存连接（非致命—缓存不可用不影响核心功能）
    await redis_manager.initialize()

    # ── Neo4j 知识图谱（非致命—图谱不可用不影响核心 RAG） ──
    kg_config = config.get("knowledge_graph", {})
    if kg_config.get("enabled", True):
        try:
            await neo4j_manager.initialize()
            # 注入 gateway 引用（此时 gateway 尚未初始化，后续通过方法设置）
            app.state.neo4j_manager = neo4j_manager
            logger.info("[main] Neo4j 知识图谱连接成功")
        except Exception as e:
            logger.warning("[main] Neo4j 初始化失败（非致命，知识图谱功能降级）: %s", e)
            app.state.neo4j_manager = None
    else:
        app.state.neo4j_manager = None
        logger.info("[main] 知识图谱已禁用 (knowledge_graph.enabled=false)")

    # ── 审批/人审已统一至 Supervisor 后台任务 ────────────────
    # async_request_approval 工具通过 Store 存储审批请求，
    # Supervisor 在 execute_node 中检测 [HUMAN_APPROVAL_REQUIRED]
    # 标记后由 await_approval 节点调用 interrupt() 挂起，
    # 人类通过 POST /tasks/{id}/resume 恢复。
    # 详见 app/tools/approval.py 和 app/supervisor/graph.py

    try:
        # ── 初始化模型智能网关 ────────────────────────────
        model_config = config.get("models", {})
        cb_config = config.get("circuit_breaker", {})
        probe_config = config.get("health_probe", {})
        fallback_chains = config.get("fallback_chains", {})

        gateway = ModelGateway()

        # 注册每个模型（LangChain + LlamaIndex 双接口）
        for name, model_cfg in model_config.items():
            provider = model_cfg["provider"]
            api_key_env = model_cfg["api_key_env"]
            base_url_env = model_cfg["base_url_env"]
            is_primary = model_cfg.get("is_primary", False)

            for role_name in model_cfg["roles"]:
                role = ModelRole(role_name)
                # 根据角色决定使用哪个接口加载
                if role in (ModelRole.CHAT, ModelRole.FALLBACK_CHAT):
                    # LangChain 接口（用于 Agent）
                    instance = await _load_model_for_role(name, provider, api_key_env, base_url_env, "langchain")
                else:
                    # LlamaIndex 接口（用于检索）
                    instance = await _load_model_for_role(name, provider, api_key_env, base_url_env, "llama_index")

                spec = ModelSpec(
                    name=name,
                    provider=provider,
                    roles=[role],
                    api_key_env=api_key_env,
                    base_url_env=base_url_env,
                    is_primary=is_primary,
                )
                await gateway.register_model(spec, instance)

                # 连通性检查（仅主模型启动时验证）
                if is_primary and role == ModelRole.CHAT:
                    try:
                        await instance.ainvoke([{"role": "user", "content": "ping"}])
                        logger.info(f"[主模型] ✅ {name} 联通正常")
                    except Exception as e:
                        logger.warning(f"[主模型] ❌ {name} 不可用: {e}")

        # 设置降级链
        for role_name, chain in fallback_chains.items():
            gateway.set_fallback_chain(ModelRole(role_name), chain)

        # 启动后台健康探活
        await gateway.start_probe(interval_seconds=probe_config.get("interval_seconds", 30.0))

        app.state.model_gateway = gateway

        # ── 本地模型（embedding + rerank，不受 gateway 管理） ──
        embed_model = await AsyncLoadModel.async_local_load_embed_model(embed_model_path_dir)
        rerank_model = await AsyncLoadModel.async_local_load_rerank_model(rerank_model_path_dir)

        # ── 向后兼容：保留旧 knowledge_resources 结构 ──────
        app.state.knowledge_resources = {
            "embed_model": embed_model,
            "rerank_model": rerank_model,
            "langchain_chat_llm": None,   # 由 gateway 管理
            "llama_chat_llm": None,       # 由 gateway 管理
            "langchain_fallback_chat_llm": None,  # 由 gateway 管理
            "llama_fallback_chat_llm": None,      # 由 gateway 管理
        }

        # 初始化知识图谱服务（注入 gateway）
        kg_enabled = kg_config.get("enabled", True) and neo4j_manager.available
        if kg_enabled:
            knowledge_graph_service._gateway = gateway
            app.state.kg_service = knowledge_graph_service
            logger.info("[main] KnowledgeGraphService 已初始化")
        else:
            app.state.kg_service = None

        # 初始化统一检索管道（传入 gateway 和 kg_service）
        pipeline = RetrievalPipeline(
            embed_model=embed_model,
            rerank_model=rerank_model,
            fallback_llm=None,  # gateway 动态提供 rewriter LLM
            enable_rewriter=True,
            enable_fusion=True,
            enable_graph_rag=kg_config.get("graph_rag_enabled", True) and kg_enabled,
            top_k=10,
            gateway=gateway,
            knowledge_graph_service=knowledge_graph_service if kg_enabled else None,
        )
        # 注册资源到工具模块（传入 gateway 和 kg_service）
        register_knowledge_resource(
            embed_model=embed_model,
            rerank_model=rerank_model,
            llama_chat_model=None,  # gateway 动态提供
            retrieval_pipeline=pipeline,
            gateway=gateway,
            knowledge_graph_service=knowledge_graph_service if kg_enabled else None,
        )
    except Exception as e:
        logger.critical(f"知识库资源初始化失败: {e}", exc_info=True)

    # ── MinerU PDF 智能解析引擎 ──────────────────────────
    mineru_config = config.get("mineru", {})
    app.state.mineru_available = False
    if mineru_config.get("enabled", False):
        try:
            from app.readers.mineru_reader import MinerUReader

            mineru_reader = MinerUReader(
                backend=mineru_config.get("backend", "pipeline"),
                parse_method=mineru_config.get("parse_method", "auto"),
                lang=mineru_config.get("lang", "ch"),
                formula_enable=mineru_config.get("formula_enable", True),
                table_enable=mineru_config.get("table_enable", True),
                timeout_seconds=mineru_config.get("timeout_seconds", 300.0),
            )
            if mineru_reader.is_available():
                app.state.mineru_reader = mineru_reader
                app.state.mineru_available = True
                index_manager.mineru_reader = mineru_reader
                logger.info("[main] MinerU PDF 解析引擎已启用 (pipeline, cpu)")
            else:
                logger.warning("[main] MinerU 模型未就绪，降级 PyMuPDF")
        except Exception as e:
            logger.warning("[main] MinerU 初始化失败，降级 PyMuPDF: %s", e)

    # 发现 Specialist Sub-Agents（用于多 Agent 编排）
    try:
        subagents = discover_specialist_agents()
        app.state.specialist_subagents = subagents
        logger.info("[main] 发现 %d 个 Specialist Agent", len(subagents))
    except Exception as e:
        logger.warning("[main] Specialist Agent 发现失败（非致命）: %s", e)
        app.state.specialist_subagents = []

    # ── 构建动态 System Prompt ────────────────────────────
    subagents = app.state.specialist_subagents
    BASE_SYSTEM_PROMPT = build_triage_prompt(subagents)

    # ── 创建 Triage DeepAgent（第一层：分流判断） ──────────
    try:
        app.state.agent = await async_create_agent(
            langchain_chat_model_name,
            fallback_model_name,
            tools,
            system_prompt=BASE_SYSTEM_PROMPT,
            checkpointer=app.state.checkpointer,
            store=app.state.store,
            context_schema=ChatContext,
            subagents=subagents,
            gateway=gateway,
        )
        logger.info(
            "[main] Triage DeepAgent 创建完成 (subagents=%d, gateway=enabled)",
            len(subagents),
        )
    except Exception as e:
        logger.critical("Triage DeepAgent 创建失败: %s", e, exc_info=True)
        raise

    # ── 创建 Executor DeepAgent（第二层：后台任务执行） ────
    # 与 Triage 同类型，换 system prompt 和工具配置。
    # 关键差异：
    #   - 有 request_approval（触发 HITL interrupt）
    #   - interrupt_on 配置让 HumanInTheLoopMiddleware 在调用
    #     request_approval 时自动挂起任务
    try:
        executor_system_prompt = build_executor_prompt(subagents)
        executor_tools: list = []
        if "request_approval" in _TOOL_REGISTRY:
            executor_tools.append(_TOOL_REGISTRY["request_approval"])
        if "read_task_journal" in _TOOL_REGISTRY:
            executor_tools.append(_TOOL_REGISTRY["read_task_journal"])
        app.state.executor_agent = await async_create_agent(
            langchain_chat_model_name,
            fallback_model_name,
            executor_tools,
            system_prompt=executor_system_prompt,
            checkpointer=app.state.checkpointer,
            store=app.state.store,
            context_schema=ChatContext,
            subagents=subagents,
            gateway=gateway,
            interrupt_on={"request_approval": True},
        )
        logger.info(
            "[main] Executor DeepAgent 创建完成 (subagents=%d, interrupt_on=request_approval)",
            len(subagents),
        )
    except Exception as e:
        logger.critical("Executor DeepAgent 创建失败: %s", e, exc_info=True)
        raise

    # ── 初始化 Harness + Context 层 ────────────────────────
    # Harness 层：事件总线 + 后台任务执行器
    # Context 层：任务上下文管理器

    event_bus = EventBus(redis_client=redis_manager.client if redis_manager.available else None)
    app.state.event_bus = event_bus
    logger.info("[main] EventBus 初始化完成 (redis=%s)", redis_manager.available)

    context_manager = TaskContextManager(store=app.state.store)
    app.state.context_manager = context_manager
    logger.info("[main] TaskContextManager 初始化完成")

    task_executor = TaskExecutor(
        store=app.state.store,
        event_bus=event_bus,
        executor_agent=app.state.executor_agent,
        context_manager=context_manager,
    )
    app.state.task_executor = task_executor
    register_task_executor(task_executor)  # 注册到工具层，供 create_background_task 使用
    logger.info("[main] TaskExecutor 初始化完成")

    # ── 恢复未完成的后台任务（服务器重启后自动恢复） ──
    await task_executor.recover_tasks()

    # 死信队列 — 失败操作的可靠存储与重试
    dead_letter_queue = DeadLetterQueue(store=app.state.store)
    app.state.dead_letter_queue = dead_letter_queue

    # ── 注册死信队列到工具层（供 approval 等工具在异常时写入） ──
    from app.tools.resources import register_dead_letter_queue
    register_dead_letter_queue(dead_letter_queue)

    # ── 注册关键操作的重试处理器 ──
    async def retry_approval_store_write(operation_args: dict) -> bool:
        """重试审批请求的 Store 写入。

        当 async_request_approval 写入 Store 失败时，审批数据被写入死信队列。
        此 handler 在后台扫描到可重试死信时被调用，重新尝试写入 Store。
        """
        approval_id = operation_args.get("approval_id", "")
        approval_data = operation_args.get("approval_data", {})
        if not approval_id or not approval_data:
            logger.error("[DeadLetter] 审批重试参数无效: id=%s data_keys=%s",
                         approval_id, list(approval_data.keys()) if approval_data else [])
            return False
        try:
            await app.state.store.aput(("approval_requests",), approval_id, approval_data)
            logger.info("[DeadLetter] 审批 Store 写入重试成功: %s", approval_id)
            return True
        except Exception as e:
            logger.error("[DeadLetter] 审批 Store 写入重试失败: %s → %s", approval_id, e)
            return False

    dead_letter_queue.register_retry_handler(
        "async_request_approval", retry_approval_store_write
    )

    # 后台扫描 — 每 2 分钟尝试重试可重试的死信
    await dead_letter_queue.start_scanner(interval_seconds=120.0)
    logger.info("[main] DeadLetterQueue 初始化完成（后台扫描已启动 + 审批重试处理器已注册）")
    # ── 两层架构初始化结束 ─────────────────────────────

    # ── 自进化系统初始化 ─────────────────────────────────
    evo_config = config.get("evolution", {})
    if evo_config.get("enabled", True):
        try:
            from app.evolution import EvolutionManager, evolution_state

            evolution_state.set_store(app.state.store)

            evolution_manager = EvolutionManager(
                store=app.state.store,
                event_bus=event_bus,
                context_manager=context_manager,
                app_state=app.state,
            )
            app.state.evolution_manager = evolution_manager

            # 从 Store 恢复持久化状态
            await evolution_manager.load_state()

            # 启动定时扫描
            scan_interval = evo_config.get("scan_interval_hours", 6.0)
            await evolution_manager.start_scheduled_scan(interval_hours=scan_interval)

            logger.info(
                "[main] EvolutionManager 初始化完成 (scan_interval=%.1fh, lookback=%dh)",
                scan_interval,
                evo_config.get("analysis_lookback_hours", 24),
            )
        except Exception as e:
            logger.warning("[main] 自进化系统初始化失败（非致命）: %s", e, exc_info=True)
            app.state.evolution_manager = None
    else:
        app.state.evolution_manager = None
        logger.info("[main] 自进化系统已禁用 (evolution.enabled=false)")

    try:
        minio_client = Minio(
            endpoint=os.getenv("MINIO_ENDPOINT", "localhost:9002"),
            access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
            secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
            secure=os.getenv("MINIO_SECURE", "False").lower() == "true",
        )
        bucket = os.getenv("MINIO_BUCKET", "user-documents")
        if not await minio_client.bucket_exists(bucket):
            await minio_client.make_bucket(bucket)
        app.state.minio_client = minio_client
        app.state.minio_bucket = bucket
        logger.info(f"MinIO 客户端初始化完成 (bucket={bucket})")
    except Exception as e:
        logger.warning(f"MinIO 初始化失败，上传功能将不可用: {e}", exc_info=True)
        app.state.minio_client = None
        app.state.minio_bucket = None
    yield
    # 关闭时清理资源
    print("应用关闭中...")
    shutdown_timeout = config.get("graceful_shutdown", {}).get("global_timeout_seconds", 15.0)
    drain_timeout = config.get("graceful_shutdown", {}).get("drain_timeout_seconds", 30.0)
    try:
        async with asyncio.timeout(shutdown_timeout):
            # ── 优雅关闭顺序（关键：先保护任务，再关连接） ──
            # Step 1: 停止后台任务执行器
            #         等待运行中任务完成 → 超时未完成的打快照后取消
            if hasattr(app.state, 'task_executor'):
                task_executor = app.state.task_executor
                await task_executor.shutdown(timeout=drain_timeout)
                logger.info("[shutdown] TaskExecutor 优雅关闭完成")

            # Step 2: 停止死信扫描器
            if hasattr(app.state, 'dead_letter_queue'):
                await app.state.dead_letter_queue.stop_scanner()

            # Step 2.5: 停止自进化定时扫描
            if hasattr(app.state, 'evolution_manager') and app.state.evolution_manager:
                await app.state.evolution_manager.stop_scheduled_scan()
                logger.info("[shutdown] EvolutionManager 定时扫描已停止")

            # Step 3: 停止网关健康探活（已优化为 1 秒内响应取消）
            if hasattr(app.state, 'model_gateway'):
                await app.state.model_gateway.stop_probe()

            # Step 4: 并行关闭数据库连接池
            #         放在最后 — 确保没有任务还在写入
            await asyncio.gather(
                pg_db_manager.close(),
                milvus_db_manager.close(),
                redis_manager.close(),
                neo4j_manager.close(),
            )
            print("资源已清理")
    except asyncio.TimeoutError:
        print(f"警告: 资源清理超时（{shutdown_timeout}s），强制退出")


# 初始化FastAPI应用
app = FastAPI(title="TKAgent API", version="1.0.0", lifespan=lifespan)

# 添加CORS中间件
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# 速率限制中间件（在 CORS 之后，路由之前）
app.add_middleware(
    RateLimitMiddleware,
    redis_client=redis_manager.client if redis_manager.available else None,
)


# 注册路由
app.include_router(auth_routes.router)
app.include_router(session_routes.router)
app.include_router(upload_routes.router)
app.include_router(document_routes.router)
app.include_router(admin_router)
# 自进化系统管理路由（仅在启用时注册）
if config.get("evolution", {}).get("enabled", True):
    app.include_router(evolution_router)

async def generate_sessions_title(user_message: str, assistant_message: str, llm) -> str:
    if not assistant_message:
        return f"新对话"
    prompt = f"""请根据以下对话内容生成一个5-15个字的简短标题，只输出标题本身，不要解释，不要引号，不要多余标点。
             用户问题: {user_message[:200]}
             助手回答: {assistant_message[:200]}
             标题:
             """
    try:
        response = await llm.ainvoke(prompt)
        title = response.content.strip().replace("\n", "")[:15]
        return title if title else "新标题"
    except Exception as e:
        logger.warning(f"生成标题错误: {e}", exc_info=True)
        return f"新对话"

async def _inject_task_results(
    store,
    session_id: str,
) -> list[dict]:
    """从 Store 读取本会话中已完成但 Triage 尚未引用的任务结果。

    将结果包装为 system 消息，标记为已读后返回。
    如果没有未读结果，返回空列表。

    Args:
        store: LangGraph AsyncPostgresStore
        session_id: 当前对话 session ID

    Returns:
        可注入到 messages 列表中的消息对象列表（空列表表示无未读结果）
    """
    if store is None or not session_id:
        return []

    try:
        items = await store.asearch(("task_results", session_id), limit=50)
    except Exception:
        return []

    unread = []
    for item in items:
        if item.value and not item.value.get("read", False):
            unread.append((item.key, item.value))

    if not unread:
        return []

    # 构建注入消息 + 标记已读
    parts = [
        "## 📋 本会话中后台任务已完成",
        "",
        "以下任务在后台执行完成，用户可能会询问相关进展：",
        "",
    ]
    import json as _json_inject

    for _key, result in unread[-5:]:  # 最多注入最近 5 个
        status = result.get("status", "unknown")
        icon = "✅" if status == "completed" else "❌" if status == "failed" else "📌"
        parts.append(f"- {icon} **{result.get('task_id', '?')}**: {result.get('goal', '')}")
        summary = result.get("result_summary", "")
        if summary:
            parts.append(f"  └ 结果摘要: {summary[:300]}")
        error = result.get("error_message", "")
        if error:
            parts.append(f"  └ 错误: {error[:200]}")

        # 标记已读
        result["read"] = True
        try:
            await store.aput(("task_results", session_id), _key, result)
        except Exception:
            pass

    return [{
        "role": "system",
        "content": "\n".join(parts),
    }]


async def generate_response_stream(
        session_id: str,
        message: str,
        checkpointer,
        store,
        context_data,
        langfuse_handler: CallbackHandler,
        agent,
) -> AsyncGenerator[str, None]:
    # 配置参数
    status_queue: asyncio.Queue = asyncio.Queue()
    status_handler = StatusCallbackHandler(status_queue)
    # 注入 contextvar，供 GatewayMiddleware 推送降级/恢复状态
    from app.status_handler import current_handler
    current_handler.set(status_handler)
    configurable = {
        "configurable": {
            "thread_id": session_id,    # 包含thread_id因为langgraph的键名必须是thread_id
            "user_id": context_data["user_id"],
            "session_id": context_data["session_id"],
            "trace_id": trace_context.current,
        },
        "callbacks": [langfuse_handler, status_handler],
        "metadata": {
            "langfuse_user_id": context_data["user_id"],
            "langfuse_session_id": session_id,
        }
    }

    # ── 注入本会话中已完成的后台任务结果 ─────────────
    injected_messages = await _inject_task_results(store, session_id)
    messages_for_agent = injected_messages + [{"role": "user", "content": message}]

    async def _agent_consumer():
        """消费 agent.astream 产出，推 content 事件到队列。"""
        normal_exit = False
        # ── 推送执行中状态 ──────────────────────────
        await status_handler.emit_executing()
        try:
            async for chunk in agent.astream(
                {"messages": messages_for_agent},
                config=configurable,
                context=ChatContext(
                    user_id=context_data["user_id"],
                    session_id=context_data["session_id"],
                ),
                stream_mode="messages"
            ):
                if isinstance(chunk, (tuple, list)) and len(chunk) > 0:
                    msg_chunk = chunk[0]
                    if isinstance(msg_chunk, AIMessageChunk) and msg_chunk.content:
                        await status_queue.put(json.dumps({
                            "_type": "content",
                            "content": msg_chunk.content,
                            "session_id": session_id,
                        }))
            normal_exit = True
        except asyncio.CancelledError:
            await status_handler.emit_cancelled()
        except asyncio.TimeoutError:
            await status_handler.emit_timeout()
        except Exception as e:
            logger.error(f"Agent流式响应错误 (session={session_id}): {e}", exc_info=True)
            await status_handler.emit_error(f"异常: {e}")
            await status_queue.put(json.dumps({
                "_type": "error",
                "error": str(e),
                "session_id": session_id,
            }))
        finally:
            # 正常结束推送 completed；异常路径已推送对应状态，不重复
            if normal_exit:
                await status_handler.emit_completed()
            # 发送结束哨兵
            await status_queue.put(None)

    # 启动后台消费者任务
    agent_task = asyncio.create_task(_agent_consumer())

    # 从队列读取，自然交错 content 和 status 事件
    try:
        while True:
            item = await status_queue.get()
            if item is None:
                break  # agent 流结束
            yield item
    finally:
        # 确保后台任务被清理
        if not agent_task.done():
            agent_task.cancel()
            try:
                await agent_task
            except (asyncio.CancelledError, Exception):
                pass


@app.post("/chat")
async def chat_endpoint(request: ChatRequest, current_user: int = Depends(get_current_user)):
    # ── 生成 trace_id — 全链路追踪起点 ──
    trace_id = trace_context.start_trace()

    session_id = request.session_id
    is_new_session = False
    if not session_id:
        session_id = str(uuid.uuid4())
        is_new_session = True
    else:
        # 验证session_id是否属于当前用户
        owner_id = await pg_db_manager.get_session_owner(session_id)
        if owner_id != current_user:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问此会话")
        await pg_db_manager.update_session_last_used(session_id)
    checkpointer = pg_db_manager.checkpointer
    store = pg_db_manager.store
    context_data = {
        "user_id": str(current_user),
        "session_id": str(session_id)
    }
    await ensure_user_skills_init(store, str(current_user))
    langfuse_handler = CallbackHandler()
    async def event_generator():
        # 清除上一个问题的引用来源，避免泄漏到本次回答
        await redis_manager.delete(f"{SOURCES_KEY_PREFIX}{current_user}:{session_id}")
        assistant_message = ""
        try:
            async for event in generate_response_stream(
                session_id,
                request.message,
                checkpointer,
                store,
                context_data,
                langfuse_handler,
                agent=app.state.agent,
            ):
                try:
                    event_data = json.loads(event)
                    event_type = event_data.get("_type", "content")
                    if event_type == "content":
                        assistant_message += event_data.get("content", "")
                        yield {
                            "event": "message",
                            "data": event,
                        }
                    elif event_type == "error":
                        yield {
                            "event": "error",
                            "data": event,
                        }
                    # event_type == "status" → 推 status 事件，不记入 assistant_message
                    else:
                        yield {
                            "event": "status",
                            "data": event,
                        }
                except json.JSONDecodeError:
                    pass
        finally:
            # 流结束后发送引用来源（从 Redis 读取，跨进程共享）
            sources_data = await redis_manager.get(f"{SOURCES_KEY_PREFIX}{current_user}:{session_id}")
            sources = sources_data.get("sources", []) if sources_data else []
            if sources:
                try:
                    yield {
                        "event": "sources",
                        "data": json.dumps({"sources": sources})
                    }
                except Exception as e:
                    logger.warning(f"序列化 sources 失败: {e}")
            if is_new_session:
                try:
                    # 从 gateway 获取标题生成用 LLM（优先回退链中的模型）
                    llm = None
                    gateway = getattr(app.state, 'model_gateway', None)
                    if gateway is not None:
                        chain = gateway.get_model_chain(ModelRole.FALLBACK_CHAT)
                        if chain:
                            llm = chain[0][1]
                    if llm is None:
                        llm = app.state.knowledge_resources.get("langchain_fallback_chat_llm")
                    title = await generate_sessions_title(request.message, assistant_message, llm)
                    await pg_db_manager.create_user_session(current_user, session_id, title)
                except Exception as e:
                    logger.error(f"标题生成失败: {e}", exc_info=True)
                    await pg_db_manager.create_user_session(current_user, session_id, "新对话")
    return EventSourceResponse(
        event_generator(),
        headers={
            "X-Session-ID": session_id,
            "X-Trace-Id": trace_id,
        }
    )


# ── 引用溯源 ───────────────────────────────────
@app.get("/sources/{session_id}", response_model=SourcesResponse)
async def get_sources(
    session_id: str,
    current_user: int = Depends(get_current_user),
):
    """获取指定会话的最新检索来源文件列表。"""
    owner_id = await pg_db_manager.get_session_owner(session_id)
    if owner_id != current_user:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问")
    sources_data = await redis_manager.get(f"{SOURCES_KEY_PREFIX}{current_user}:{session_id}")
    sources = sources_data.get("sources", []) if sources_data else []
    return SourcesResponse(sources=sources)


# ── 用户反馈 ───────────────────────────────────
@app.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    feedback: FeedbackRequest,
    current_user: int = Depends(get_current_user),
):
    """用户对回答进行评价（点赞写缓存 / 点踩不缓存）。"""
    owner_id = await pg_db_manager.get_session_owner(feedback.session_id)
    if owner_id != current_user:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问")
    await pg_db_manager.save_feedback(
        user_id=current_user,
        session_id=feedback.session_id,
        rating=feedback.rating,
        comment=feedback.comment,
    )

    # ── 点赞写入 Redis 缓存 / 点踩丢弃 ──────────────────
    pending_key = f"{PENDING_QA_KEY_PREFIX}{current_user}:{feedback.session_id}"
    pending = await redis_manager.get(pending_key)
    if pending:
        await redis_manager.delete(pending_key)
    if pending and feedback.rating == 1:
        cache_key = redis_manager.build_cache_key(
            pending["question"], user_id=current_user,
        )
        await redis_manager.set(
            cache_key,
            {"answer": pending["answer"], "sources": pending["sources"]},
        )
        logger.info(
            "点赞写缓存: session=%s question=%s",
            feedback.session_id, pending["question"][:40],
        )
    elif pending and feedback.rating == -1:
        logger.info(
            "点踩不缓存: session=%s question=%s",
            feedback.session_id, pending["question"][:40],
        )

    logger.info(
        "反馈记录: user=%d session=%s rating=%d",
        current_user, feedback.session_id, feedback.rating,
    )
    return FeedbackResponse(status="ok")


# ── 后台任务管理 ───────────────────────────────────

from pydantic import BaseModel as PydanticBase, Field


class TaskResponse(PydanticBase):
    task_id: str
    thread_id: str
    goal: str
    status: str
    plan: list[dict] = []
    progress: str = ""
    result_summary: str = ""
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""


class TaskResumeRequest(PydanticBase):
    action: str = Field(..., description="操作: approved / rejected / provide_info")
    comment: str | None = Field(None, description="备注说明")
    data: dict | None = Field(None, description="附加数据")


# ── 后台任务由 Supervisor 在 POST /chat 内部通过 create_background_task 创建 ──
# 用户不直接调用 POST /tasks — 任务仅通过对话产生


@app.get("/tasks", response_model=list[TaskResponse])
async def list_tasks(
    status_filter: str | None = None,
    current_user: int = Depends(get_current_user),
):
    """列出当前用户的后台任务。

    Args:
        status_filter: 可选状态筛选 (created/executing/waiting_human/completed/failed)
    """
    executor = getattr(app.state, "task_executor", None)
    if executor is None:
        return []

    filter_enum = None
    if status_filter:
        try:
            filter_enum = TaskStatus(status_filter)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"无效状态: {status_filter}")

    handles = await executor.list_tasks(status_filter=filter_enum)

    return [
        TaskResponse(
            task_id=h.task_id,
            thread_id=h.thread_id,
            goal=h.goal,
            status=h.status.value,
            plan=h.plan,
            progress=h.progress,
            result_summary=h.result_summary,
            error_message=h.error_message,
            created_at=h.created_at,
            updated_at=h.updated_at,
        )
        for h in handles
    ]


@app.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    current_user: int = Depends(get_current_user),
):
    """查询任务状态与进度。"""
    executor = getattr(app.state, "task_executor", None)
    if executor is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    handle = await executor.get_task(task_id)
    if handle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在: {task_id}")

    return TaskResponse(
        task_id=handle.task_id,
        thread_id=handle.thread_id,
        goal=handle.goal,
        status=handle.status.value,
        plan=handle.plan,
        progress=handle.progress,
        result_summary=handle.result_summary,
        error_message=handle.error_message,
        created_at=handle.created_at,
        updated_at=handle.updated_at,
    )


@app.post("/tasks/{task_id}/resume", response_model=TaskResponse)
async def resume_task(
    task_id: str,
    request: TaskResumeRequest,
    current_user: int = Depends(get_current_user),
):
    """恢复被挂起的任务（如审批决策、补充信息）。

    当任务状态为 waiting_human 时，使用此端点提供决策或信息，
    Agent 会从上次中断处继续执行。
    """
    executor = getattr(app.state, "task_executor", None)
    if executor is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    handle = await executor.get_task(task_id)
    if handle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在: {task_id}")

    resume_data = {"action": request.action}
    if request.comment:
        resume_data["comment"] = request.comment
    if request.data:
        resume_data.update(request.data)

    handle = await executor.resume_task(task_id, resume_data)

    return TaskResponse(
        task_id=handle.task_id,
        thread_id=handle.thread_id,
        goal=handle.goal,
        status=handle.status.value,
        plan=handle.plan,
        progress=handle.progress,
        result_summary=handle.result_summary,
        error_message=handle.error_message,
        created_at=handle.created_at,
        updated_at=handle.updated_at,
    )


@app.delete("/tasks/{task_id}")
async def cancel_task(
    task_id: str,
    current_user: int = Depends(get_current_user),
):
    """取消一个正在执行或挂起的任务。"""
    executor = getattr(app.state, "task_executor", None)
    if executor is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    cancelled = await executor.cancel_task(task_id)
    if not cancelled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在: {task_id}")

    return {"status": "cancelled", "task_id": task_id}


@app.get("/tasks/{task_id}/events")
async def task_events(
    task_id: str,
    current_user: int = Depends(get_current_user_sse),
):
    """SSE 端点 — 实时推送任务状态变更事件。

    前端可以通过 EventSource 订阅此端点获取任务进度更新。
    """
    executor = getattr(app.state, "task_executor", None)
    if executor is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    event_bus = getattr(app.state, "event_bus", None)
    if event_bus is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    from sse_starlette.sse import EventSourceResponse as SSE

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def handler(data: dict):
            if data.get("task_id") == task_id:
                await queue.put(data)

        # 注册事件处理器并持有取消函数，确保连接断开时清理
        unsubs = [
            event_bus.subscribe("task.executing", handler),
            event_bus.subscribe("task.interrupted", handler),
            event_bus.subscribe("task.completed", handler),
            event_bus.subscribe("task.failed", handler),
        ]

        try:
            while True:
                try:
                    event_data = await asyncio.wait_for(queue.get(), timeout=30)
                    yield {
                        "event": event_data.get("type", "task_update"),
                        "data": json.dumps(event_data, ensure_ascii=False, default=str),
                    }
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": "{}"}
        except asyncio.CancelledError:
            pass
        finally:
            for unsub in unsubs:
                unsub()

    return SSE(event_stream())


# ── 任务执行日志（Journal） ─────────────────────────

@app.get("/tasks/{task_id}/journal")
async def get_task_journal(
    task_id: str,
    limit: int = 50,
    current_user: int = Depends(get_current_user),
):
    """获取任务的执行日志（journal）。

    Journal 是任务执行过程的结构化记录，每条记录包含时间戳、事件类型、
    人类可读摘要和结构化详情。与 progress 不同，journal 不受
    SummarizationMiddleware 压缩影响，提供完整的执行链路可观测性。

    Args:
        task_id: 任务 ID
        limit: 返回最近 N 条记录（默认 50，传 0 返回全部）
    """
    executor = getattr(app.state, "task_executor", None)
    if executor is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    handle = await executor.get_task(task_id)
    if handle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在: {task_id}")

    context_manager: TaskContextManager = getattr(app.state, "context_manager", None)
    if context_manager is None:
        return {"task_id": task_id, "journal": [], "count": 0}

    entries = await context_manager.read_journal(task_id, limit=limit)
    return {
        "task_id": task_id,
        "goal": handle.goal,
        "status": handle.status.value,
        "journal": [e.to_dict() for e in entries],
        "count": len(entries),
    }


if __name__ == "__main__":
    SHUTDOWN_TIMEOUT = 10  # 优雅关闭超时（秒），超时后强制终止
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        uvicorn_config = uvicorn.Config(
            app, host="0.0.0.0", port=8000, loop="asyncio",
            timeout_graceful_shutdown=SHUTDOWN_TIMEOUT,
        )
        server = uvicorn.Server(uvicorn_config)
        loop.run_until_complete(server.serve())
    else:
        uvicorn.run(
            app, host="0.0.0.0", port=8000,
            timeout_graceful_shutdown=SHUTDOWN_TIMEOUT,
        )