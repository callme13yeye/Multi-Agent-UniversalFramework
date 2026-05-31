import os
import logging

from jose import jwt, JWTError
from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
ENV_PATH = PROJECT_ROOT / "key.env"

load_dotenv(dotenv_path=ENV_PATH)

# 从环境变量读取 JWT 配置
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", 1440))
if not JWT_SECRET_KEY:
    raise ValueError("JWT_SECRET_KEY 环境变量必须设置")


# ==================== JWT 工具函数 ====================
def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """生成 JWT 访问令牌，data 中应包含 'sub' 字段（用户标识）"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=JWT_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """解析 JWT 令牌，返回负载字典；若无效或过期则返回 None"""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        return None