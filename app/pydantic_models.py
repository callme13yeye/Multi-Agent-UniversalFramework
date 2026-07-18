# app/pydantic_models.py
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


# ---------- 认证相关 ----------
class UserRegister(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int

class LoginRequest(BaseModel):
    username: str
    password: str

class UserInfo(BaseModel):
    id: int
    username: str
    created_at: Optional[str] = None
    last_login: Optional[str] = None
    is_active: bool

# ---------- 聊天相关 ----------
class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None

# ---------- 上下文相关 ----------
class ChatContext(BaseModel):
    user_id: str
    session_id: str
    task_id: str = ""  # 后台任务 ID（Executor Agent 运行时设置，Triage 为空）

# ---------- 会话管理 ----------
class SessionInfo(BaseModel):
    session_id: str
    title: Optional[str] = None
    created_at: str
    last_used: str

class RenameSessionRequest(BaseModel):
    title: str

class CreateSessionRequest(BaseModel):
    title: Optional[str] = "新对话"

class MessageItem(BaseModel):
    role: str
    content: str

class SessionMessagesResponse(BaseModel):
    messages: list[MessageItem]

# ---------- 引用溯源 ----------
class SourceInfo(BaseModel):
    file_name: str
    snippet: str
    score: float
    node_id: str

class SourcesResponse(BaseModel):
    sources: list[SourceInfo]

# ---------- 用户反馈 ----------
class FeedbackRequest(BaseModel):
    session_id: str
    rating: int  # 1 = 有用, -1 = 没用
    comment: Optional[str] = None

class FeedbackResponse(BaseModel):
    status: str

# ---------- 文件上传 ----------
class UploadResponse(BaseModel):
    filename: str
    status: str
    message: Optional[str] = None

# ---------- 文档管理 ----------
class DocumentInfo(BaseModel):
    id: int
    filename: str
    file_type: str
    file_size: int
    status: str
    chunk_count: Optional[int] = None
    created_at: str
    updated_at: Optional[str] = None

class DocumentListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    documents: list[DocumentInfo]

class DocumentReplaceResponse(BaseModel):
    status: str
    message: str
    new_doc_id: int