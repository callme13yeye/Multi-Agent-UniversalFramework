# app/pg_database.py — 双数据库管理器
# ============= 读写分离双库架构 ==========
# auth_db: 用户认证 (asyncpg 连接池) — 独立扩容
# conversations_db: LangGraph 状态持久化 (psycopg AsyncConnectionPool)
#   - AsyncPostgresSaver: 对话断点续传 (checkpointer)
#   - AsyncPostgresStore: 记忆/技能 KV 存储 (store)
# 双库分离避免认证流量影响对话性能，各自可独立优化连接池参数
# =========================================================
import os
import logging
import asyncpg

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg_pool import AsyncConnectionPool # langgraph组件需要
from psycopg.rows import dict_row
from dotenv import load_dotenv
from pathlib import Path
from typing import Optional, Dict, Any, List
from app.utils.credentials_encryption_decrypt import decrypt_credential
from app.document_event_bus import document_event_bus

PROJECT_ROOT = Path(__file__).parent.parent
ENV_PATH = PROJECT_ROOT / "key.env"

load_dotenv(dotenv_path=ENV_PATH)

logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self):
        # Postgres 连接配置
        self.auth_db_url = os.getenv("AUTH_DB_URL")
        self.conversations_db_url = os.getenv("CONVERSATIONS_DB_URL")
        if not self.auth_db_url and not self.conversations_db_url:
            raise ValueError("未设置auth_db_url/conversations_db_url")
        # Langgraph 组件
        self.checkpointer: Optional[AsyncPostgresSaver] = None
        self.store: Optional[AsyncPostgresStore] = None
        # Postgresql 连接池
        self.auth_pool: Optional[asyncpg.Pool] = None
        self.conversations_pool: Optional[asyncpg.Pool] = None

    async def initialize(self):
        try:
            logger.info("初始化LangGraph专用连接池")
            self.conversations_pool = AsyncConnectionPool(
                self.conversations_db_url, 
                min_size=2, 
                max_size=10,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row, 
                },
                open=False,
            )
            await self.conversations_pool.open()
            await self.conversations_pool.wait()  # 等待连接池准备就绪

            logger.info("初始化 PostgresSaver and PostgresStore")
            self.checkpointer = AsyncPostgresSaver(conn=self.conversations_pool)
            await self.checkpointer.setup()
            self.store = AsyncPostgresStore(conn=self.conversations_pool)
            await self.store.setup()

            logger.info("创建 auth_db 连接池")
            self.auth_pool = await asyncpg.create_pool(
                self.auth_db_url,
                min_size=2,
                max_size=10,
                max_queries=50000,
                max_inactive_connection_lifetime=300
            )
            await self.create_feedback_table()
            logger.info("数据库连接池初始化完成")
        except Exception as e:
            logger.error(f"数据库初始化失败")
            await self.close()
            raise

    async def close(self):
        if self.conversations_pool:
            await self.conversations_pool.close()
            logger.info("LangGraph专用连接池已关闭")
        if self.auth_pool:
            await self.auth_pool.close()
            logger.info("auth_db 连接池已关闭")


    # ================= 用户相关操作 =================
    async def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """根据用户名查询用户, 返回字典或None"""
        async with self.auth_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, username, password_hash, is_active, created_at, last_login "
                "FROM users WHERE username = $1",
                username
            )
            return dict(row) if row else None
    
    async def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        """根据用户ID查询用户, 返回字典或None"""
        async with self.auth_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, username, password_hash, is_active, created_at, last_login "
                "FROM users WHERE id = $1",
                user_id
            )
            return dict(row) if row else None
    
    async def create_user(self, username: str, password_hash: str, milvus_password: str) -> int:
        """
        创建新用户，返回用户 ID。
        若用户名已存在，抛出 ValueError。
        """
        async with self.auth_pool.acquire() as conn:
            try:
                user_id = await conn.fetchval(
                    "INSERT INTO users (username, password_hash, milvus_password) VALUES ($1, $2, $3) RETURNING id",
                    username, password_hash, milvus_password
                )
                return user_id
            except asyncpg.exceptions.UniqueViolationError:
                raise ValueError(f"用户名 '{username}' 已存在")
    
    # 从postgresql获取用户的milvus加密密码并解密返回    
    async def get_milvus_password(self, user_id: int) -> str | None:
        """获取用户的 Milvus 密码"""
        async with self.auth_pool.acquire() as conn:
            milvus_password = await conn.fetchval(
                "SELECT milvus_password FROM users WHERE id = $1",
                user_id
            )
            return decrypt_credential(milvus_password) if milvus_password else None
    
    async def update_user_last_login(self, user_id: int) -> None:
        """更新用户最后登录时间为当前时间"""
        async with self.auth_pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET last_login = NOW() WHERE id = $1",
                user_id
            )

    async def is_user_active(self, user_id: int) -> bool:
        """检查用户是否处于活跃状态"""
        async with self.auth_pool.acquire() as conn:
            active = await conn.fetchval(
                "SELECT is_active FROM users WHERE id = $1",
                user_id
            )
            return active is True
    
    async def delete_user(self, user_id: int) -> None:
        async with self.auth_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM users WHERE id = $1",
                user_id
            )
    # ================= 会话相关操作 =================
    async def create_user_session(self, user_id: int, session_id: str, title: Optional[str] = None) -> None:
        """记录用户与 session 的关联"""
        async with self.auth_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_sessions (user_id, session_id, title) VALUES ($1, $2, $3)",
                user_id, session_id, title
            )

    async def get_session_owner(self, session_id: str) -> Optional[int]:
        """获取 session 对应的用户 ID，用于权限验证"""
        async with self.auth_pool.acquire() as conn:
            user_id = await conn.fetchval(
                "SELECT user_id FROM user_sessions WHERE session_id = $1",
                session_id
            )
            return user_id

    async def update_session_last_used(self, session_id: str) -> None:
        """更新 session 的最后使用时间"""
        async with self.auth_pool.acquire() as conn:
            await conn.execute(
                "UPDATE user_sessions SET last_used = NOW() WHERE session_id = $1",
                session_id
            )

    async def list_user_sessions(self, user_id: int) -> List[Dict[str, Any]]:
        """获取用户的所有会话，按最后使用时间倒序排列"""
        async with self.auth_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT session_id, title, created_at, last_used
                FROM user_sessions
                WHERE user_id = $1
                ORDER BY last_used DESC
                """,
                user_id
            )
            return [dict(row) for row in rows]

    async def delete_session(self, session_id: str) -> None:
        """删除指定会话记录（仅删除映射，不删除 LangGraph 中的对话数据）"""
        async with self.auth_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_sessions WHERE session_id = $1",
                session_id
            )

    async def rename_session(self, session_id: str, new_title: str) -> None:
        """重命名会话标题"""
        async with self.auth_pool.acquire() as conn:
            await conn.execute(
                "UPDATE user_sessions SET title = $1 WHERE session_id = $2",
                new_title, session_id
            )

    
    # ================= 文档相关操作 =================
    async def create_document(
        self,
        user_id: int,
        file_hash: str,
        original_filename: str,
        file_size: int,
        file_type: str,
        object_path: str,
        parser_strategy: str | None = None,
    ) -> int:
        """创建文档跟踪记录，返回文档 ID"""
        async with self.auth_pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO user_documents
                    (user_id, file_hash, original_filename, file_size, file_type,
                     object_path, parser_strategy, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'uploaded')
                RETURNING id
                """,
                user_id, file_hash, original_filename, file_size, file_type,
                object_path, parser_strategy,
            )

    async def get_document_by_hash(self, user_id: int, file_hash: str) -> dict | None:
        """按用户 + 文件哈希查询文档"""
        async with self.auth_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM user_documents WHERE user_id = $1 AND file_hash = $2",
                user_id, file_hash,
            )
            return dict(row) if row else None

    async def get_document_by_filename(self, user_id: int, original_filename: str) -> dict | None:
        """按用户 + 文件名查询文档（用于检测同名文件替换）"""
        async with self.auth_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM user_documents WHERE user_id = $1 AND original_filename = $2",
                user_id, original_filename,
            )
            return dict(row) if row else None

    async def update_document_filename(self, doc_id: int, new_filename: str, new_object_path: str):
        """更新文档的文件名和对象路径（哈希相同但文件名不同时）"""
        async with self.auth_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE user_documents
                SET original_filename = $1, object_path = $2, updated_at = NOW()
                WHERE id = $3
                """,
                new_filename, new_object_path, doc_id,
            )

    async def get_document(self, doc_id: int) -> dict | None:
        """获取单个文档记录"""
        async with self.auth_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM user_documents WHERE id = $1",
                doc_id,
            )
            return dict(row) if row else None

    async def update_document_status(
        self,
        doc_id: int,
        status: str,
        chunk_count: int | None = None,
        error_message: str | None = None,
    ):
        """更新文档状态（同时通过事件总线通知 SSE 订阅者）"""
        async with self.auth_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE user_documents
                SET status = $1, updated_at = NOW(),
                    chunk_count = COALESCE($2, chunk_count),
                    error_message = $3
                WHERE id = $4
                """,
                status, chunk_count, error_message, doc_id,
            )
            row = await conn.fetchrow(
                "SELECT user_id FROM user_documents WHERE id = $1", doc_id,
            )
        if row:
            await document_event_bus.publish(doc_id, status, row["user_id"])

    async def list_user_documents(self, user_id: int) -> list[dict]:
        """列出用户的所有文档"""
        async with self.auth_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM user_documents WHERE user_id = $1 ORDER BY created_at DESC",
                user_id,
            )
            return [dict(row) for row in rows]

    async def search_user_documents(
        self, user_id: int, search: str | None = None, limit: int = 20, offset: int = 0
    ) -> tuple[list[dict], int]:
        """搜索用户文档，支持模糊搜索文件名，返回 (文档列表, 总数)"""
        async with self.auth_pool.acquire() as conn:
            if search:
                pattern = f"%{search}%"
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM user_documents WHERE user_id = $1 AND original_filename ILIKE $2",
                    user_id, pattern,
                )
                rows = await conn.fetch(
                    "SELECT * FROM user_documents WHERE user_id = $1 AND original_filename ILIKE $2 ORDER BY created_at DESC LIMIT $3 OFFSET $4",
                    user_id, pattern, limit, offset,
                )
            else:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM user_documents WHERE user_id = $1",
                    user_id,
                )
                rows = await conn.fetch(
                    "SELECT * FROM user_documents WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                    user_id, limit, offset,
                )
            return [dict(row) for row in rows], count

    async def delete_document_record(self, doc_id: int):
        """删除文档跟踪记录"""
        async with self.auth_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_documents WHERE id = $1",
                doc_id,
            )

    # ================= 反馈相关操作 =================
    async def create_feedback_table(self):
        """创建反馈表（如不存在）"""
        async with self.auth_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_feedback (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    rating INTEGER NOT NULL CHECK (rating IN (1, -1)),
                    comment TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

    async def save_feedback(
        self,
        user_id: int,
        session_id: str,
        rating: int,
        comment: Optional[str] = None,
    ):
        """保存用户对回答的反馈"""
        async with self.auth_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_feedback (user_id, session_id, rating, comment) VALUES ($1, $2, $3, $4)",
                user_id, session_id, rating, comment,
            )


pg_db_manager = DatabaseManager()