# credentials_encryption_decrypt.py — 凭证加密存储
# ============== Milvus 密码加密存储 ==========
# 用户 Milvus 密码使用 Fernet (对称加密) 加密后存入 Postgres，
# 即使数据库泄露，攻击者也无法获取明文密码来访问向量数据库。
# 加密密钥由环境变量 CREDENTIALS_ENCRYPTION_KEY 注入，不落盘。
# ================================================
import os
import logging

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
ENV_PATH = PROJECT_ROOT / "key.env"

load_dotenv(dotenv_path=ENV_PATH)

# 从环境变量读取 CREDENTIALS_ENCRYPTION 加密证书配置
MILVUS_CREDENTIALS_ENCRYPTION_KEY = os.getenv("MILVUS_CREDENTIALS_ENCRYPTION_KEY")
if not MILVUS_CREDENTIALS_ENCRYPTION_KEY:
    raise ValueError("环境变量 MILVUS_CREDENTIALS_ENCRYPTION_KEY 必须设置")

# ==================== Milvus 凭证加密工具 =================
try:
    _fernet = Fernet(MILVUS_CREDENTIALS_ENCRYPTION_KEY.encode())
except Exception as e:
    raise ValueError("无效的加密密钥，必须是 32 字节 base64 编码的 Fernet 密钥") from e


def create_encrypt_credential(plaintext: str) -> str:
    """加密明文密码，返回密文字符串"""
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_credential(ciphertext: str) -> str | None:
    """解密密文密码，失败返回 None"""
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.error("解密 Milvus 凭证失败：密钥不匹配或数据损坏")
        return None
    except Exception as e:
        logger.error(f"解密 Milvus 凭证发生未知错误: {e}")
        return None
