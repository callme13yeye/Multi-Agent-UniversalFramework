# app/routes/session_routes.py
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status, Request
from typing import List

from app.pydantic_models import (
    SessionInfo, RenameSessionRequest, CreateSessionRequest,
    SessionMessagesResponse, MessageItem)
from app.auth import get_current_user
from app.pg_database import pg_db_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["Sessions"])

@router.get("", response_model=List[SessionInfo])
async def list_sessions(current_user: int = Depends(get_current_user)):
    """获取当前用户的所有会话列表"""
    sessions = await pg_db_manager.list_user_sessions(current_user)
    return [
        SessionInfo(
            session_id=session["session_id"],
            title=session["title"],
            created_at=session["created_at"].isoformat() + "Z",
            last_used=session["last_used"].isoformat() + "Z",
        )
        for session in sessions
    ]
    
@router.post("", response_model=SessionInfo)
async def create_session(
    data: CreateSessionRequest,
    current_user: int = Depends(get_current_user)
):
    """创建新会话，返回会话信息"""
    session_id = str(uuid.uuid4())
    await pg_db_manager.create_user_session(current_user, session_id, data.title)
    # 从数据库获取刚创建的记录（时间戳等）
    sessions = await pg_db_manager.list_user_sessions(current_user)
    for session in sessions:
        if session["session_id"] == session_id:
            return SessionInfo(
                session_id=session["session_id"],
                title=session["title"],
                created_at=str(session["created_at"]),
                last_used=str(session["last_used"])
            )
    raise HTTPException(status_code=500, detail="创建失败")

@router.delete("/{session_id}")
async def delete_session(session_id: str, current_user: int = Depends(get_current_user)):
    """删除指定会话（仅所有者）"""
    owner = await pg_db_manager.get_session_owner(session_id)
    if owner != current_user:
        raise HTTPException(status_code=403, detail="无权操作此会话")
    
    await pg_db_manager.delete_session(session_id)
    # 注意：LangGraph 的 checkpoint 数据仍保留在 auth_db 中，可根据需要决定是否清理
    return {"status": "success", "message": f"会话 {session_id} 已删除"}

@router.patch("/{session_id}/rename")
async def rename_session(
    session_id: str,
    data: RenameSessionRequest,
    current_user: int = Depends(get_current_user)
):
    """重命名会话标题"""
    owner = await pg_db_manager.get_session_owner(session_id)
    if owner != current_user:
        raise HTTPException(status_code=403, detail="无权操作此会话")

    await pg_db_manager.rename_session(session_id, data.title)
    return {"status": "success", "title": data.title}

@router.get("/{session_id}/messages", response_model=SessionMessagesResponse)
async def get_session_messages(
    session_id: str,
    request: Request,
    current_user: int = Depends(get_current_user)
):
    """
    获取指定会话的历史消息。
    从 LangGraph 的 checkpointer 中读取状态，提取 messages 字段。
    """
    # 权限验证
    owner = await pg_db_manager.get_session_owner(session_id)
    if owner != current_user:
        raise HTTPException(status_code=403, detail="无权访问此会话")
    
    checkpointer = pg_db_manager.checkpointer
    if not checkpointer:
        raise HTTPException(status_code=500, detail="Checkpointer 未初始化")
    
    config = {
        "configurable": {
            "thread_id": session_id,    # 这里键名不能更改，langgraph必须需要这个键名
            "user_id": str(current_user),
            "session_id": session_id
        }
    }
    try:
        checkpoint = await checkpointer.aget(config)
    except Exception as e:
        logger.error(f"读取 checkpoint 失败: {e}")
        raise HTTPException(status_code=500, detail="读取对话历史失败")
    
    if not checkpoint:
        return SessionMessagesResponse(messages=[])

    messages = []
    if isinstance(checkpoint, dict):
        channel_values = checkpoint.get('channel_values', {})
        raw_messages = channel_values.get('messages', [])
        for message in raw_messages:
            # 处理 LangChain 消息对象
            if not (hasattr(message, 'type')) and hasattr(message, 'content'):
                continue
            message_type = message.type
            content = message.content
            if not content:
                continue
            if message_type in ('human', 'user'):
                messages.append(MessageItem(role="user", content=content))
            elif message_type == 'ai' and not getattr(message, 'tool_calls', None):
                messages.append(MessageItem(role="assistant", content=content))

    return SessionMessagesResponse(messages=messages)