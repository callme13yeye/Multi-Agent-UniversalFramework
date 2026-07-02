# app/redis_manager.py — Redis 缓存管理器
# ============== 热点问答缓存 ==========
# 常用问题直接返回缓存的答案，跳过 Milvus 检索 + LLM 生成。
# 非致命组件：Redis 不可用时服务降级，不影响核心功能。
# ========================================
import json
import logging
import re
import os

from dotenv import load_dotenv
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent
ENV_PATH = PROJECT_ROOT / "key.env"
load_dotenv(ENV_PATH)

logger = logging.getLogger(__name__)

CACHE_PREFIX = "qa:"          # 问答缓存 key 前缀
HOT_THRESHOLD = 5             # 命中次数超过此值视为热点
DEFAULT_TTL = 3600             # 默认缓存 TTL（秒）
HOT_TTL = 7200                 # 热点缓存延长 TTL（秒）


class RedisManager:
    """Redis 缓存管理器（单例）"""

    def __init__(self):
        self.client: Optional["redis.asyncio.Redis"] = None
        self._available = False

    async def initialize(self):
        url = os.getenv("REDIS_URL", "redis://localhost:6380/0")
        try:
            import redis.asyncio as redis
            self.client = redis.from_url(url, decode_responses=True)
            await self.client.ping()
            self._available = True
            logger.info("Redis 缓存连接成功: %s", url)
        except ImportError:
            logger.warning("redis 包未安装，缓存功能不可用")
        except Exception as e:
            logger.warning("Redis 连接失败，缓存功能将不可用: %s", e)
            self._available = False

    async def close(self):
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass
            self._available = False
            logger.info("Redis 连接已关闭")

    @property
    def available(self) -> bool:
        return self._available and self.client is not None

    # ---------- 缓存读写 ----------

    async def get(self, key: str) -> Optional[dict]:
        """获取缓存，命中后自动增加热度计数。"""
        if not self.available:
            return None
        try:
            data = await self.client.get(key)
            if data is not None:
                # 命中计数 +1
                await self.client.hincrby(f"{key}:stats", "hit", 1)
                # 检查是否达到热点阈值，自动延长 TTL
                hit_count = await self.client.hget(f"{key}:stats", "hit")
                if hit_count is not None and int(hit_count) >= HOT_THRESHOLD:
                    await self.client.expire(key, HOT_TTL)
                    await self.client.expire(f"{key}:stats", HOT_TTL)
                return json.loads(data)
            return None
        except Exception as e:
            logger.warning("Redis get 失败: %s", e)
            return None

    async def set(self, key: str, value: dict, ttl: int = DEFAULT_TTL):
        """写入缓存。"""
        if not self.available:
            return
        try:
            await self.client.setex(key, ttl, json.dumps(value, ensure_ascii=False))
            await self.client.delete(f"{key}:stats")  # 重置热度计数
        except Exception as e:
            logger.warning("Redis set 失败: %s", e)

    async def delete(self, key: str):
        """删除指定 key（幂等：key 不存在时不报错）。"""
        if not self.available:
            return
        try:
            await self.client.delete(key)
        except Exception as e:
            logger.warning("Redis delete 失败: %s", e)

    # ---------- 工具方法 ----------

    @staticmethod
    def normalize_question(question: str) -> str:
        """归一化问题文本（小写、去标点、合并空格），用于缓存 key。"""
        text = question.lower().strip()
        text = re.sub(r'[^\w\s]', '', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def build_cache_key(self, question: str, user_id: int = 0) -> str:
        """构建缓存 key: qa:{user_id}:{归一化问题}"""
        norm = self.normalize_question(question)
        return f"{CACHE_PREFIX}{user_id}:{norm}"


redis_manager = RedisManager()
