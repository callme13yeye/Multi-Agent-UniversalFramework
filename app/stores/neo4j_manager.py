# neo4j_manager.py — Neo4j 图数据库连接管理器
# 管理 Neo4j 驱动的生命周期，提供连接健康检查和图操作接口。

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from neo4j import AsyncGraphDatabase, AsyncDriver

logger = logging.getLogger(__name__)


class Neo4jManager:
    """Neo4j 图数据库管理器（单例模式）。

    职责：
    - 管理 AsyncDriver 生命周期（初始化 / 健康检查 / 关闭）
    - 提供 Cypher 查询执行接口
    - 管理约束和索引的初始化

    使用方式：
        neo4j_manager = Neo4jManager()
        await neo4j_manager.initialize()
        records = await neo4j_manager.run_query("MATCH (n) RETURN n LIMIT 5")
        await neo4j_manager.close()
    """

    def __init__(self):
        self._driver: Optional[AsyncDriver] = None
        self._initialized: bool = False
        self._uri: str = ""
        self._username: str = ""
        self._password: str = ""
        self._database: str = "neo4j"

    # ── 属性 ──────────────────────────────────────────────────

    @property
    def driver(self) -> Optional[AsyncDriver]:
        return self._driver

    @property
    def available(self) -> bool:
        """Neo4j 是否可用（已初始化 + driver 存在 + 连接已验证）。"""
        return self._initialized and self._driver is not None

    # ── 生命周期管理 ──────────────────────────────────────────

    async def initialize(
        self,
        uri: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
    ) -> None:
        """初始化 Neo4j 驱动连接。

        参数可以从环境变量读取，也可以显式传入。
        优先级：显式参数 > 环境变量 > 默认值。

        Raises:
            RuntimeError: 如果 Neo4j 连接失败且为必需服务。
        """
        if self._initialized:
            logger.warning("[Neo4j] 已经初始化，跳过重复初始化")
            return

        self._uri = uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self._username = username or os.getenv("NEO4J_USERNAME", "neo4j")
        self._password = password or os.getenv("NEO4J_PASSWORD", "neo4jadmin")
        self._database = database or os.getenv("NEO4J_DATABASE", "neo4j")

        logger.info(
            "[Neo4j] 正在连接 %s (user=%s, db=%s)…",
            self._uri, self._username, self._database,
        )

        try:
            self._driver = AsyncGraphDatabase.driver(
                self._uri,
                auth=(self._username, self._password),
                max_connection_lifetime=3600,
                max_connection_pool_size=10,
                connection_acquisition_timeout=10,
            )

            # 验证连接
            await self._driver.verify_connectivity()
            self._initialized = True
            logger.info("[Neo4j] ✅ 连接成功 (uri=%s)", self._uri)

            # 初始化必要的约束和索引
            await self._init_constraints()

        except Exception as e:
            self._driver = None
            self._initialized = False
            logger.error("[Neo4j] ❌ 连接失败: %s", e)
            # Neo4j 为非致命依赖 — 知识图谱功能降级，但核心 RAG 不受影响
            raise RuntimeError(f"Neo4j 连接失败: {e}") from e

    async def close(self) -> None:
        """关闭驱动连接。"""
        if self._driver:
            await self._driver.close()
            self._driver = None
        self._initialized = False
        logger.info("[Neo4j] 连接已关闭")

    async def health_check(self) -> bool:
        """健康检查 — 验证 Neo4j 连接是否存活。

        Returns:
            True 如果连接正常，False 否则。
        """
        if not self._driver:
            return False
        try:
            await self._driver.verify_connectivity()
            return True
        except Exception:
            return False

    # ── 约束和索引初始化 ─────────────────────────────────────

    async def _init_constraints(self) -> None:
        """初始化知识图谱所需的约束和索引。

        为实体节点创建唯一性约束（按 name + type 去重），
        为常用查询字段创建索引加速检索。
        """
        constraints = [
            # 实体唯一性约束 — 同名同类型实体去重
            "CREATE CONSTRAINT entity_unique IF NOT EXISTS "
            "FOR (e:Entity) REQUIRE (e.name, e.type) IS UNIQUE",

            # 文档节点唯一性约束
            "CREATE CONSTRAINT document_unique IF NOT EXISTS "
            "FOR (d:Document) REQUIRE d.doc_id IS UNIQUE",

            # Chunk 节点唯一性约束 — 与向量侧 node_id 一一对应
            "CREATE CONSTRAINT chunk_unique IF NOT EXISTS "
            "FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
        ]

        indexes = [
            # 实体名称全文索引（用于关键词搜索）
            "CREATE FULLTEXT INDEX entity_name_ft IF NOT EXISTS "
            "FOR (e:Entity) ON EACH [e.name, e.description]",

            # Chunk 文件哈希索引 — 快速查找某文件的所有 chunk
            "CREATE INDEX chunk_file_hash_idx IF NOT EXISTS "
            "FOR (c:Chunk) ON (c.file_hash)",

            # Chunk 内容哈希索引 — 跨文件检测相同内容
            "CREATE INDEX chunk_content_hash_idx IF NOT EXISTS "
            "FOR (c:Chunk) ON (c.content_hash)",
        ]

        for stmt in constraints + indexes:
            try:
                await self.run_write(stmt)
            except Exception as e:
                # 约束/索引已存在时忽略，其他错误记录但不阻塞
                logger.debug("[Neo4j] 约束/索引创建（可能已存在）: %s", e)

        logger.info("[Neo4j] 约束和索引初始化完成")

    # ── 查询执行接口 ─────────────────────────────────────────

    async def run_query(
        self,
        cypher: str,
        params: Optional[dict[str, Any]] = None,
        database: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """执行只读 Cypher 查询，返回记录列表。

        Args:
            cypher: Cypher 查询语句
            params: 查询参数
            database: 目标数据库（默认使用初始化时的 database）

        Returns:
            记录列表，每条记录是一个 dict
        """
        if not self._driver:
            logger.warning("[Neo4j] 驱动未初始化，跳过查询")
            return []

        db = database or self._database
        params = params or {}

        try:
            records, _, _ = await self._driver.execute_query(
                cypher, params, database_=db, routing_="r",
            )
            return [dict(r) for r in records]
        except Exception as e:
            logger.error("[Neo4j] 查询失败: %s | cypher=%s", e, cypher[:100])
            return []

    async def run_write(
        self,
        cypher: str,
        params: Optional[dict[str, Any]] = None,
        database: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """执行写入 Cypher 查询，返回记录列表。

        Args:
            cypher: Cypher 写入语句
            params: 查询参数
            database: 目标数据库

        Returns:
            记录列表
        """
        if not self._driver:
            logger.warning("[Neo4j] 驱动未初始化，跳过写入")
            return []

        db = database or self._database
        params = params or {}

        try:
            records, _, _ = await self._driver.execute_query(
                cypher, params, database_=db, routing_="w",
            )
            return [dict(r) for r in records]
        except Exception as e:
            logger.error("[Neo4j] 写入失败: %s | cypher=%s", e, cypher[:100])
            raise


# ── 全局单例 ──────────────────────────────────────────────────

neo4j_manager = Neo4jManager()
