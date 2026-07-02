# main.py
import asyncio
import os
import sys
import time
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
)
from app.tools import TOOL_REGISTRY as _TOOL_REGISTRY
from app.harness import ToolHotReloader, SubAgentHotReloader
from app.documents import RetrievalPipeline, index_manager
from app.agents import discover_specialist_agents, async_create_agent
from app.prompts import build_triage_prompt, build_executor_prompt
from app.harness import EventBus, TaskExecutor, DeadLetterQueue, TaskContextManager
from app.async_load_model import AsyncLoadModel
from app.stores import pg_db_manager, milvus_db_manager, redis_manager, neo4j_manager
from app.knowledge_graph import knowledge_graph_service
from app.pydantic_models import ChatContext
from app.routes import auth_routes, session_routes, upload_routes, document_routes, chat_routes, task_routes
from app.routes.admin_routes import router as admin_router
from app.gateway import ModelGateway, ModelRole, ModelSpec
from app.gateway.rate_limit_middleware import RateLimitMiddleware
from app.harness.trace_context import TraceIdFilter
from miniopy_async import Minio
from config import get_config

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

import uvicorn
import logging


# 日志配置 — 注入 trace_id 过滤器
logging.getLogger().addFilter(TraceIdFilter())
logger = logging.getLogger("uvicorn")

# 初始化模型和索引
config = get_config()
embed_model_path_dir = config.get("embed_model_path")
rerank_model_path_dir = config.get("rerank_model_path")
langchain_chat_model_name = config.get("langchain_chat_model_name")
fallback_model_name = config.get("fallback_model_name")

def _build_triage_tools() -> list:
    """从 TOOL_REGISTRY 收集 Triage Agent 的全部可用工具。

    Triage 层持有所有工具，system prompt 引导 LLM 根据场景选择合适的工具。
    新增工具放入 app/tools/ 后由热加载器自动重载并重建 Agent，无需重启。
    """
    return list(_TOOL_REGISTRY.values())


def _build_executor_tools() -> list:
    """从 TOOL_REGISTRY 收集 Executor Agent 的专用工具。"""
    tools: list = []
    if "request_approval" in _TOOL_REGISTRY:
        tools.append(_TOOL_REGISTRY["request_approval"])
    if "read_task_journal" in _TOOL_REGISTRY:
        tools.append(_TOOL_REGISTRY["read_task_journal"])
    return tools


_rebuild_lock = asyncio.Lock()
_last_rebuild_time: float = 0.0
_REBUILD_COOLDOWN: float = 3.0  # 秒 — 短防抖窗口，避免 Tool + SubAgent 同时变更触发两次完整重建


async def _rebuild_agents(app) -> None:
    """热重载回调：重新发现 SubAgent + 重建 Triage/Executor Agent，原子替换 app.state。

    使用 asyncio.Lock 防止并发热重载（工具 + SubAgent 同时变更时只重建一次）。
    使用短防抖窗口（3 秒）避免两个热加载器在毫秒级间隔内触发两次重建。
    若重建中途失败，回滚保留旧 Agent，避免新旧 Agent 混用。
    """
    global _last_rebuild_time
    logger = logging.getLogger("uvicorn")

    async with _rebuild_lock:
        # ── 短防抖：如果距离上次重建不足冷却时间，跳过（锁内检查，避免 TOCTOU）──
        now = time.monotonic()
        if now - _last_rebuild_time < _REBUILD_COOLDOWN:
            logger.debug("[HotReload] 防抖跳过（距上次重建 %.1fs）", now - _last_rebuild_time)
            return
        # ── 保存旧状态（用于失败回滚）──
        old_agent = getattr(app.state, "agent", None)
        old_subagents = getattr(app.state, "specialist_subagents", None)

        # 重新发现 Specialist Sub-Agents（支持新增/修改 AGENT.md 后无需重启）
        try:
            subagents = discover_specialist_agents()
            app.state.specialist_subagents = subagents
            logger.info("[HotReload] 重新发现 %d 个 Specialist Agent", len(subagents))
        except Exception as e:
            logger.warning("[HotReload] SubAgent 重新发现失败，沿用旧列表: %s", e)
            subagents = old_subagents if old_subagents is not None else []

        triage_prompt = build_triage_prompt(subagents)
        executor_prompt = build_executor_prompt(subagents)
        gateway = app.state.model_gateway

        # 重建 Triage Agent
        triage_tools = _build_triage_tools()
        try:
            new_triage = await async_create_agent(
                langchain_chat_model_name,
                fallback_model_name,
                triage_tools,
                system_prompt=triage_prompt,
                checkpointer=app.state.checkpointer,
                store=app.state.store,
                context_schema=ChatContext,
                subagents=subagents,
                gateway=gateway,
            )
        except Exception:
            logger.exception("[HotReload] ❌ Triage Agent 重建失败，回滚")
            if old_subagents is not None:
                app.state.specialist_subagents = old_subagents
            return

        # 重建 Executor Agent
        executor_tools = _build_executor_tools()
        try:
            new_executor = await async_create_agent(
                langchain_chat_model_name,
                fallback_model_name,
                executor_tools,
                system_prompt=executor_prompt,
                checkpointer=app.state.checkpointer,
                store=app.state.store,
                context_schema=ChatContext,
                subagents=subagents,
                gateway=gateway,
                interrupt_on={"request_approval": True},
            )
        except Exception:
            logger.exception("[HotReload] ❌ Executor Agent 重建失败，回滚 Triage")
            # 回滚：若 Triage 已更新，恢复旧的
            if old_agent is not None:
                app.state.agent = old_agent
            if old_subagents is not None:
                app.state.specialist_subagents = old_subagents
            return

        # 原子替换
        app.state.agent = new_triage
        app.state.executor_agent = new_executor
        if hasattr(app.state, "task_executor"):
            app.state.task_executor.executor_agent = new_executor

        _last_rebuild_time = time.monotonic()
        logger.info(
            "[HotReload] ✅ Agent 已重建 (triage_tools=%d, executor_tools=%d, subagents=%d)",
            len(triage_tools), len(executor_tools), len(subagents),
        )


# ── 构建初始 Triage Agent 工具集 ──────────────────────────────
tools = _build_triage_tools()


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

    # ── 启动 Tool 热加载器 ──────────────────────────────
    # 监听 app/tools/ 目录，文件新增/修改时自动重载并重建 Agent
    from pathlib import Path as _Path
    _tools_dir = _Path(__file__).parent / "app" / "tools"
    _hot_reloader = ToolHotReloader(
        tools_dir=_tools_dir,
        on_reload=lambda: _rebuild_agents(app),
    )
    await _hot_reloader.start()
    app.state._hot_reloader = _hot_reloader

    # ── 启动 SubAgent 热加载器 ───────────────────────────
    # 监听 app/subagents/ 目录，AGENT.md 新增/修改/删除时自动重建 Agent
    _subagents_dir = _Path(__file__).parent / "app" / "subagents"
    _subagent_hot_reloader = SubAgentHotReloader(
        subagents_dir=_subagents_dir,
        on_reload=lambda: _rebuild_agents(app),
    )
    await _subagent_hot_reloader.start()
    app.state._subagent_hot_reloader = _subagent_hot_reloader

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

            # Step 3: 停止工具热加载器
            if hasattr(app.state, '_hot_reloader'):
                await app.state._hot_reloader.stop()

            # Step 3b: 停止 SubAgent 热加载器
            if hasattr(app.state, '_subagent_hot_reloader'):
                await app.state._subagent_hot_reloader.stop()

            # Step 4: 停止网关健康探活（已优化为 1 秒内响应取消）
            if hasattr(app.state, 'model_gateway'):
                await app.state.model_gateway.stop_probe()

            # Step 5: 并行关闭数据库连接池
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
app.include_router(chat_routes.router)
app.include_router(task_routes.router)


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