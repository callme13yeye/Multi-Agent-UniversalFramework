# app/routes/chat_routes.py
"""聊天、引用溯源、用户反馈相关路由。"""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status, Request
from sse_starlette.sse import EventSourceResponse
from langfuse.langchain import CallbackHandler
from langchain.messages import AIMessageChunk

from app.pydantic_models import ChatRequest, ChatContext, FeedbackRequest, FeedbackResponse, SourcesResponse
from app.auth import get_current_user
from app.stores import pg_db_manager
from app.stores import redis_manager
from app.harness.trace_context import trace_context
from app.harness.status_handler import StatusCallbackHandler, current_handler
from app.gateway.types import ModelRole
from app.agents import ensure_user_skills_init
from config import get_config
from app.tools import SOURCES_KEY_PREFIX, PENDING_QA_KEY_PREFIX

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Chat"])


async def _generate_sessions_title(user_message: str, assistant_message: str, llm) -> str:
    if not assistant_message:
        return "新对话"
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
        return "新对话"


async def _inject_task_results(store, session_id: str) -> list[dict]:
    """从 Store 读取本会话中已完成但 Triage 尚未引用的任务结果。

    将结果包装为 system 消息，标记为已读后返回。
    如果没有未读结果，返回空列表。
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


async def _generate_response_stream(
    session_id: str,
    message: str,
    checkpointer,
    store,
    context_data,
    langfuse_handler: CallbackHandler,
    agent,
) -> "AsyncGenerator[str, None]":
    # 配置参数
    status_queue: asyncio.Queue = asyncio.Queue()
    status_handler = StatusCallbackHandler(status_queue)
    # 注入 contextvar，供 GatewayMiddleware 推送降级/恢复状态
    current_handler.set(status_handler)
    configurable = {
        "configurable": {
            "thread_id": session_id,
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
        # ── Triage 时间围栏：防止 LLM 误判导致 HTTP 请求无限挂起 ──
        timeout_seconds = get_config().get("timeouts", {}).get(
            "triage_run_timeout_seconds", 180.0
        )
        try:
            async with asyncio.timeout(timeout_seconds):
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
        except TimeoutError:
            # asyncio.timeout() 触发 — Triage 执行超时
            logger.warning(
                "Triage 执行超时 (session=%s, timeout=%.0fs)，HTTP 时间围栏触发",
                session_id, timeout_seconds,
            )
            await status_handler.emit_timeout()
            await status_queue.put(json.dumps({
                "_type": "error",
                "error": f"请求处理超时（{timeout_seconds:.0f}秒），请尝试简化问题或创建后台任务",
                "session_id": session_id,
            }))
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


@router.post("/chat")
async def chat_endpoint(
    request: ChatRequest,
    req: Request,
    current_user: int = Depends(get_current_user),
):
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
            async for event in _generate_response_stream(
                session_id,
                request.message,
                checkpointer,
                store,
                context_data,
                langfuse_handler,
                agent=req.app.state.agent,
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
                    gateway = getattr(req.app.state, 'model_gateway', None)
                    if gateway is not None:
                        chain = gateway.get_model_chain(ModelRole.FALLBACK_CHAT)
                        if chain:
                            llm = chain[0][1]
                    if llm is None:
                        llm = req.app.state.knowledge_resources.get("langchain_fallback_chat_llm")
                    title = await _generate_sessions_title(request.message, assistant_message, llm)
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

@router.get("/sources/{session_id}", response_model=SourcesResponse)
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

@router.post("/feedback", response_model=FeedbackResponse)
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
