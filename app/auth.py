# app/auth.py — 双数据源用户认证
# ============== Postgres + Milvus 双写注册 ==========
# 注册时同时在 Postgres (认证信息) 和 Milvus (向量数据库权限) 创建用户。
# 任何一步失败则完整回滚，保证分布式资源的一致性。
# 用户 Milvus 密码加密存储 (credentials_encryption_decrypt)，明文不落盘。
# =========================================================
import logging

from typing import Optional, Dict, Any
from fastapi import HTTPException, status, Depends, Request, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.pg_database import pg_db_manager
from app.utils.argon2id import hash_password, verify_password
from app.milvus_manager import milvus_db_manager
from app.utils.credentials_encryption_decrypt import create_encrypt_credential
from app.utils.jwt import decode_access_token

logger = logging.getLogger(__name__)


# ==================== 用户认证业务逻辑 ====================
async def register_user(username: str, password: str) -> int:
    """
    postgresql和milvus同时注册新用户
    成功返回用户 ID；若用户名已存在则抛出 HTTPException 400。
    """
    password_hash = hash_password(password)
    milvus_password = create_encrypt_credential(password)
    try:
        user_id = await pg_db_manager.create_user(username, password_hash, milvus_password)
        milvus_user_id = await milvus_db_manager.provision_user(username, password)
        if not milvus_user_id:
            # 回滚数据库用户创建
            await pg_db_manager.delete_user(user_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Milvus 用户资源创建失败，正在回滚……"
            )
        logger.info(f"新用户注册: {username} (id={user_id})")
        return user_id
    except HTTPException:
        raise
    except ValueError as e:
        logger.exception("注册过程发生未知错误")
        if user_id:
            await pg_db_manager.delete_user(user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


async def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """
    验证用户凭证。
    成功返回用户信息字典（不含密码哈希）；失败返回 None。
    """
    user = await pg_db_manager.get_user_by_username(username)
    if not user:
        return None
    if not user.get("is_active", False):
        logger.warning(f"非活跃用户尝试登录: {username}")
        return None
    if not verify_password(password, user["password_hash"]):
        return None

    # 更新最后登录时间（异步，不阻塞返回）
    await pg_db_manager.update_user_last_login(user["id"])

    # 返回时移除敏感字段
    return {
        "id": user["id"],
        "username": user["username"],
        "is_active": user["is_active"],
        "created_at": user["created_at"],
        "last_login": user["last_login"],
    }


# ==================== FastAPI 依赖项：获取当前用户 ====================
security = HTTPBearer(auto_error=False)

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> int:
    """
    依赖项：从 Authorization 头解析 JWT，返回当前登录用户的 user_id。
    若未提供令牌、令牌无效或用户已被禁用，则抛出 401。
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或过期的认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌格式错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = int(user_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌内容无效",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 检查用户是否仍然活跃（防止令牌有效期间账户被禁用）
    if not await pg_db_manager.is_user_active(user_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户账户已被禁用",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user_id


async def get_current_user_sse(token: str = Query(...)) -> int:
    """SSE 端点专用认证依赖：EventSource 不支持自定义 header，通过查询参数 token 认证"""
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或过期的认证令牌",
        )
    user_id_str = payload.get("sub")
    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌格式错误",
        )
    try:
        user_id = int(user_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌内容无效",
        )
    if not await pg_db_manager.is_user_active(user_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户账户已被禁用",
        )
    return user_id