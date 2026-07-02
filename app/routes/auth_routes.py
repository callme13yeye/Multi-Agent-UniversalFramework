# app/routes/auth_routes.py
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.pydantic_models import UserRegister, TokenResponse, UserInfo, LoginRequest
from app.auth import register_user, authenticate_user, get_current_user
from app.utils.jwt import create_access_token
from app.stores import pg_db_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/register", response_model=TokenResponse)
async def register(user_data: UserRegister):
    """用户注册，成功后返回 JWT 令牌"""
    try:
        user_id = await register_user(user_data.username, user_data.password)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Registration error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="注册失败，请稍后重试"
        )
    access_token = create_access_token(data={"sub": str(user_id)})
    return TokenResponse(access_token=access_token, user_id=user_id)

@router.post("/login-form", response_model=TokenResponse)
async def login_form(form_data: OAuth2PasswordRequestForm = Depends()):
    """用户登录，返回 JWT 令牌"""
    user = await authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token(data={"sub": str(user["id"])})
    return TokenResponse(access_token=access_token, user_id=user["id"])

@router.post("/login", response_model=TokenResponse)
async def login_json(login_data: LoginRequest):
    """用户登录，返回 JWT 令牌"""
    user = await authenticate_user(login_data.username, login_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token(data={"sub": str(user["id"])})
    return TokenResponse(access_token=access_token, user_id=user["id"])

@router.get("/me", response_model=UserInfo)
async def get_me(current_user: int = Depends(get_current_user)):
    """获取当前登录用户的信息"""
    user = await pg_db_manager.get_user_by_id(current_user)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return UserInfo(
        id=user["id"],
        username=user["username"],
        created_at=str(user["created_at"]),
        last_login=str(user["last_login"]),
        is_active=user["is_active"]
    )