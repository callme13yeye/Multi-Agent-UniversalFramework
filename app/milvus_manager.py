# app/milvus_database.py — Milvus 向量数据库管理
# =============== Milvus RBAC 多租户隔离 ==================
# 每个用户注册时自动创建独立的 Milvus 用户 + 角色 + Collection。
# 通过 RBAC 分配最小权限集 (Search/Insert/Delete/Query 等)，
# 用户间数据物理隔离，即使一个用户的凭证泄露也无法访问他人数据。
# =========================================================
import asyncio
import logging
import os
from dotenv import load_dotenv
from pathlib import Path

from typing import Dict, Optional
from pymilvus import AsyncMilvusClient, exceptions

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
ENV_PATH = PROJECT_ROOT / "key.env"
load_dotenv(ENV_PATH)

class MilvusDatabaseManager:
    """管理 Milvus 连接池、用户和 Collection 生命周期"""

    def __init__(self):
        self.milvus_uri = os.getenv("MILVUS_URL")
        self.admin_token = os.getenv("MILVUS_ADMIN_TOKEN")

        self.admin_client: Optional[AsyncMilvusClient] = None
        self._user_pool: Dict[str, AsyncMilvusClient] = {}
        self._pool_lock = asyncio.Lock()

    async def initialize(self):
        """初始化 Milvus 连接（启动时调用）"""
        try:
            admin = await self._get_admin_client()
            await admin.list_collections()
            logger.info("Milvus 管理员连接成功，URI: %s", self.milvus_uri)
        except Exception as e:
            logger.error("Milvus 初始化失败: %s", e)
            raise

    async def close(self):
        """关闭所有连接"""
        async with self._pool_lock:
            # 并行关闭所有用户客户端，加速退出
            close_tasks = [
                client.close() for client in self._user_pool.values()
            ]
            if close_tasks:
                await asyncio.gather(*close_tasks, return_exceptions=True)
            self._user_pool.clear()
        if self.admin_client:
            await self.admin_client.close()
            self.admin_client = None
        logger.info("Milvus 所有连接已关闭")

    async def _get_admin_client(self) -> AsyncMilvusClient:
        if self.admin_client is None:
            self.admin_client = AsyncMilvusClient(
                uri=self.milvus_uri, token=self.admin_token
            )
        return self.admin_client

    # ---------- 普通用户资源置备 ----------
    async def provision_user(self, username: str, password: str) -> bool:
        """
        为新用户创建 Milvus 用户、角色 并授权。
        返回 True 表示成功。
        """
        admin = await self._get_admin_client()
        collection_name = f"col_{username}"
        role_name = f"role_{username}"

        try:
            await asyncio.gather(
                self._create_user(admin, username, password),
                self._create_role(admin, role_name),
            )
            await self._grant_privilege(admin, role_name, collection_name)
            await self._grant_role(admin, username, role_name)

            logger.info("Milvus 资源已为用户 %s 置备完成", username)
            return True
        except Exception as e:
            logger.error("为用户 %s 置备 Milvus 资源失败: %s", username, e)
            await self._rollback(username, collection_name, role_name)
            return False

    async def _create_user(self, client: AsyncMilvusClient, username: str, password: str):
        try:
            await client.create_user(user_name=username, password=password)
            logger.info("Milvus 用户 %s 已创建", username)
        except exceptions.MilvusException as e:
            if "already exists" in str(e).lower():
                logger.warning("Milvus 用户 %s 已存在", username)
            else:
                raise

    async def _create_role(self, client: AsyncMilvusClient, role_name: str):
        try:
            await client.create_role(role_name=role_name)
            logger.info("Milvus 角色 %s 已创建", role_name)
        except exceptions.MilvusException as e:
            if "already exists" in str(e).lower():
                logger.warning("Milvus 角色 %s 已存在", role_name)
            else:
                raise

    async def _grant_privilege(self, client: AsyncMilvusClient, role_name: str, collection_name: str):
        privileges = [
            # 集合/索引创建权限（自动创建所需）
            "CreateCollection", "CreateIndex",
            # 数据操作权限
            "Insert", "Delete", "Upsert", "Search", "Query",
            # 集合管理
            "Load", "Release", "Flush", "GetStatistics",
            "DescribeCollection", "GetLoadState", "GetLoadingProgress",
            # 索引管理
            "DropIndex", "IndexDetail",
            # 分区管理
            "CreatePartition", "DropPartition", "HasPartition", "ShowPartitions",
        ]
        for privilege in privileges:
            try:
                await client.grant_privilege_v2(
                    role_name=role_name,
                    privilege=privilege,
                    collection_name=collection_name,
                    db_name="default"
                )
                logger.info(f"已授予角色 {role_name} 对 {collection_name} 的 {privilege} 权限")
            except exceptions.MilvusException as e:
                logger.warning("授予权限失败（可能已存在）: %s", e)

    async def _grant_role(self, client: AsyncMilvusClient, username: str, role_name: str):
        try:
            await client.grant_role(user_name=username, role_name=role_name)
            logger.info("已将角色 %s 授予用户 %s", role_name, username)
        except exceptions.MilvusException as e:
            logger.warning("授予角色失败（可能已存在）: %s", e)

    async def _rollback(self, username: str, collection_name: str, role_name: str):
        admin = await self._get_admin_client()
        try:
            await admin.drop_collection(collection_name)
        except Exception:
            pass
        try:
            await admin.drop_role(role_name)
        except Exception:
            pass
        try:
            await admin.drop_user(username)
        except Exception:
            pass
        logger.warning("已尝试回滚用户 %s 的 Milvus 资源", username)

    # ---------- 用户连接池 ----------
    async def get_user_client(self, username: str, password: str) -> AsyncMilvusClient:
        async with self._pool_lock:
            if username not in self._user_pool:
                token = f"{username}:{password}"
                client = AsyncMilvusClient(uri=self.milvus_uri, token=token)
                self._user_pool[username] = client
                logger.info("为用户 %s 创建 Milvus 客户端并加入连接池", username)
            return self._user_pool[username]

    async def get_client_by_user_id(self, user_id: int) -> AsyncMilvusClient:
        """根据用户 ID 获取共享的 AsyncMilvusClient（连接池自动管理复用），
        调用方无需关心连接创建/销毁。

        依赖 pg_db_manager 查询用户凭证（延迟导入避免循环依赖）。
        """
        from app.pg_database import pg_db_manager

        user = await pg_db_manager.get_user_by_id(user_id)
        if not user:
            raise ValueError(f"用户不存在: user_id={user_id}")
        password = await pg_db_manager.get_milvus_password(user_id)
        if not password:
            raise ValueError(f"用户 {user['username']} 的 Milvus 密码未找到")
        return await self.get_user_client(user["username"], password)


milvus_db_manager = MilvusDatabaseManager()