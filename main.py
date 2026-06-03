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

from app.async_tools import (
    async_get_current_time,
    async_web_search,
    async_knowledge_query_ask,
    async_create_reimbursement_ticket,
    register_knowledge_resource,
    register_workflow_engine,
    SOURCES_KEY_PREFIX,
    PENDING_QA_KEY_PREFIX,
)
from app.retrieval import RetrievalPipeline
from app.agent_definitions import discover_specialist_agents
from langchain.messages import AIMessageChunk
from app.async_create_agent import async_create_agent
from app.async_load_model import AsyncLoadModel
from app.pg_database import pg_db_manager
from app.milvus_manager import milvus_db_manager
from app.redis_manager import redis_manager
from app.auth import get_current_user
from app.pydantic_models import ChatRequest, ChatContext, FeedbackRequest, FeedbackResponse, SourcesResponse
from app.routes import auth_routes, session_routes, upload_routes, document_routes
from app.async_ensure_user_skills_init import ensure_user_skills_init
from app.routes.workflow_routes import create_workflow_router
from app.workflow.engine import WorkflowEngine, register_workflow
from app.workflow.reimbursement import build_reimbursement_graph
from app.admin_routes import router as admin_router
from app.gateway import ModelGateway, ModelRole, ModelSpec
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


# 日志配置
logger = logging.getLogger("uvicorn")

# 初始化模型和索引
config = get_config()
embed_model_path_dir = config.get("embed_model_path")
rerank_model_path_dir = config.get("rerank_model_path")
langchain_chat_model_name = config.get("langchain_chat_model_name")
llama_chat_model_name = config.get("llama_chat_model_name")
fallback_model_name = config.get("fallback_model_name")

BASE_SYSTEM_PROMPT = """你是一个企业智能助手。你拥有 Router/Supervisor 架构——你可以自己回答问题，也可以将领域任务通过 ``task`` 工具委托给 Specialist Agent。

通用规则：
1. 涉及生产环境变更的操作（重启、下线、修改配置），必须先向用户确认。
2. 对于多步操作任务（如查空闲 IP、排查故障、配置检查），必须先查阅对应的 SKILL.md 文件，严格按工作流执行。
3. 请始终使用中文进行回答。

**知识库查询规则（重要）：**
- 当 async_knowledge_query_ask 返回"未找到相关内容"，说明知识库没有可用的参考资料，你必须：
  1. 明确告知用户：知识库中暂无与此问题相关的文档
  2. 建议用户使用 async_web_search 工具进行联网搜索，或上传相关文档到知识库
  3. **严禁**在知识库无结果时使用模型自身知识编造答案——这会造成误导
- 只有当 async_knowledge_query_ask 返回了实际内容时，才能基于其内容回答
"""
tools = [async_get_current_time, async_web_search, async_knowledge_query_ask, async_create_reimbursement_ticket]

# 工具名 → 工具对象映射，供 discover_specialist_agents 按 AGENT.md 的 allowed_tools 分组
tools_map = {
    "async_get_current_time": async_get_current_time,
    "async_web_search": async_web_search,
    "async_knowledge_query_ask": async_knowledge_query_ask,
    "async_create_reimbursement_ticket": async_create_reimbursement_ticket,
}

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

    # 初始化工作流引擎
    try:
        engine = WorkflowEngine(
            checkpointer=pg_db_manager.checkpointer,
            store=pg_db_manager.store,
        )
        register_workflow("reimbursement", build_reimbursement_graph)
        register_workflow_engine(engine)  # 给 async_tools 注册，供 Agent 工具使用
        app.state.workflow_engine = engine
        app.include_router(create_workflow_router(engine))
        logger.info("[main] 工作流引擎初始化完成")
    except Exception as e:
        logger.warning(f"工作流引擎初始化失败（非致命）: {e}", exc_info=True)

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

        # 初始化统一检索管道（传入 gateway）
        pipeline = RetrievalPipeline(
            embed_model=embed_model,
            rerank_model=rerank_model,
            fallback_llm=None,  # gateway 动态提供 rewriter LLM
            enable_rewriter=True,
            enable_fusion=True,
            top_k=10,
            gateway=gateway,
        )
        # 注册资源到工具模块（传入 gateway）
        register_knowledge_resource(
            embed_model=embed_model,
            rerank_model=rerank_model,
            llama_chat_model=None,  # gateway 动态提供
            retrieval_pipeline=pipeline,
            gateway=gateway,
        )
    except Exception as e:
        logger.critical(f"知识库资源初始化失败: {e}", exc_info=True)

    # 发现 Specialist Sub-Agents（用于多 Agent 编排）
    try:
        subagents = discover_specialist_agents(tools_map=tools_map)
        app.state.specialist_subagents = subagents
        logger.info("[main] 发现 %d 个 Specialist Agent", len(subagents))
    except Exception as e:
        logger.warning("[main] Specialist Agent 发现失败（非致命）: %s", e)
        app.state.specialist_subagents = []

    # ── 创建单例 Agent（共享 CompiledStateGraph，所有会话复用） ──
    try:
        subagents = app.state.specialist_subagents
        app.state.agent = await async_create_agent(
            langchain_chat_model_name,
            fallback_model_name,
            tools,
            system_prompt=BASE_SYSTEM_PROMPT,
            checkpointer=app.state.checkpointer,
            store=app.state.store,
            context_schema=ChatContext,
            subagents=subagents,
            gateway=gateway,  # 注入智能网关
        )
        logger.info(
            "[main] 单例 Agent 创建完成 (subagents=%d, gateway=enabled)",
            len(subagents),
        )
    except Exception as e:
        logger.critical("Agent 创建失败: %s", e, exc_info=True)
        raise

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
    # 停止网关健康探活
    if hasattr(app.state, 'model_gateway'):
        await app.state.model_gateway.stop_probe()
    await pg_db_manager.close()
    await milvus_db_manager.close()
    await redis_manager.close()
    print("资源已清理")


# 初始化FastAPI应用
app = FastAPI(title="TKAgent API", version="1.0.0", lifespan=lifespan)

# 添加CORS中间件
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# 注册路由
app.include_router(auth_routes.router)
app.include_router(session_routes.router)
app.include_router(upload_routes.router)
app.include_router(document_routes.router)
app.include_router(admin_router)

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
    configurable = {
        "configurable": {
            "thread_id": session_id,    # 包含thread_id因为langgraph的键名必须是thread_id
            "user_id": context_data["user_id"],
            "session_id": context_data["session_id"]
        },
        "callbacks": [langfuse_handler],
        "metadata": {
            "langfuse_user_id": context_data["user_id"],
            "langfuse_session_id": session_id,
        }
    }

    # 流式响应
    try:
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": message}]},
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
                    # 使用SSE格式发送数据
                    yield json.dumps({"content": msg_chunk.content, "session_id": session_id})
    except Exception as e:
        # 发送错误信息
        logger.error(f"Agent流式响应错误 (session={session_id}): {e}", exc_info=True)
        yield json.dumps({"error": str(e), "session_id": session_id})


@app.post("/chat")
async def chat_endpoint(request: ChatRequest, current_user: int = Depends(get_current_user)):
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
                    if "content" in event_data:
                        assistant_message += event_data["content"]
                except Exception as e:
                    pass
                yield {
                    "event": "message",
                    "data": event
                }
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
        headers={"X-Session-ID": session_id}
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


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        uvicorn_config = uvicorn.Config(app, host="0.0.0.0", port=8000, loop="asyncio")
        server = uvicorn.Server(uvicorn_config)
        loop.run_until_complete(server.serve())
    else:
        uvicorn.run(app, host="0.0.0.0", port=8000)