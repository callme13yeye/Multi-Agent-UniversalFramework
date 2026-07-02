# app/stores — 外部存储管理器
#
# 统一管理所有外部存储连接（单例模式）：
#   - PostgreSQL（auth_db + conversations_db）
#   - Milvus（RBAC 多租户向量数据库）
#   - Neo4j（知识图谱）
#   - Redis（缓存 + 限流）
#
# 组件：
#   pg_database.py    — PostgreSQL 双数据库管理器
#   milvus_manager.py — Milvus RBAC 多租户管理
#   neo4j_manager.py  — Neo4j 连接管理
#   redis_manager.py  — Redis 缓存管理

from app.stores.pg_database import DatabaseManager, pg_db_manager
from app.stores.milvus_manager import milvus_db_manager
from app.stores.neo4j_manager import neo4j_manager
from app.stores.redis_manager import redis_manager

__all__ = [
    "DatabaseManager", "pg_db_manager",
    "milvus_db_manager",
    "neo4j_manager",
    "redis_manager",
]
