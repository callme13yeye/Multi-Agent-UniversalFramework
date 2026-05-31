"""app/index_manager.py — 索引管理器

管理文档从上传到索引的完整生命周期：
1. 文件哈希计算（SHA-256）与去重检测（委托 pg_database）
2. 编排处理管线（process → index → track）
3. 文档查询、删除与重建
"""
import io
import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from miniopy_async import Minio

from app.async_get_index import async_get_milvus_index
from app.document_processor import process_document
from app.milvus_manager import milvus_db_manager
from app.node_parser_factory import resolve_parser_strategy
from app.pg_database import DatabaseManager, pg_db_manager
from app.utils.file_hash import compute_file_hash

logger = logging.getLogger(__name__)

BUCKET_NAME = "user-documents"


class IndexManager:
    """索引管理器——文档生命周期管理"""

    def __init__(self, pg_manager: Optional[DatabaseManager] = None):
        self.pg = pg_manager or pg_db_manager

    # ───────── Milvus 节点删除 ─────────

    async def _delete_milvus_nodes(self, user_id: int, file_hash: str):
        """从 Milvus 删除属于某文件哈希的所有节点

        使用 milvus_db_manager 的异步连接池，避免每次创建独立连接。
        """
        user = await self.pg.get_user_by_id(user_id)
        if not user:
            logger.warning("用户 %s 不存在，跳过 Milvus 节点删除", user_id)
            return
        collection_name = f"col_{user['username']}"

        try:
            client = await milvus_db_manager.get_client_by_user_id(user_id)

            if not await client.has_collection(collection_name):
                logger.info("Milvus 集合 %s 尚不存在，跳过节点删除", collection_name)
                return

            await client.delete(
                collection_name=collection_name,
                filter=f'file_hash == "{file_hash}"',
            )
            logger.info(
                "已从 Milvus 集合 %s 删除哈希 %s 的节点",
                collection_name, file_hash[:16],
            )
        except Exception as e:
            logger.error(
                "从 Milvus 删除节点失败（collection=%s, hash=%s）: %s",
                collection_name, file_hash[:16], e,
            )
            raise

    async def delete_document(self, user_id: int, doc_id: int) -> bool:
        """删除文档记录及对应的 Milvus 节点"""
        doc = await self.pg.get_document(doc_id)
        if not doc or doc["user_id"] != user_id:
            return False

        if doc["file_hash"]:
            await self._delete_milvus_nodes(user_id, doc["file_hash"])
        await self.pg.delete_document_record(doc_id)

        logger.info("已删除文档 %s (%s)", doc_id, doc["original_filename"])
        return True

    # ───────── 编排 ─────────

    async def prepare_upload(
        self,
        user_id: int,
        file_data: bytes,
        original_filename: str,
        minio_client: Minio,
        parser_strategy: Optional[str] = None,
        on_duplicate: str = "skip",
    ) -> Dict[str, Any]:
        """
        上传准备阶段：哈希 → 去重检查 → 上传 MinIO → 创建文档记录

        去重逻辑（按优先级）:
          1. 哈希相同 + 文件名相同 + 切分策略相同 → 跳过（真正的一模一样）
          2. 哈希相同 + 文件名不同        → 更新文件名，重建（内容相同，但需新元数据）
          3. 哈希相同 + 切分策略不同      → 重建（内容相同，但需不同分块）
          4. 文件名相同 + 哈希不同        → 重建（文件已被替换为新内容）

        Args:
            on_duplicate: 发现重复时的处理方式
                - "skip":   智能跳过（默认）：上述逻辑按需处理
                - "rebuild": 强制删除旧数据，重建索引
                - "error":   哈希冲突时抛出 ValueError

        Returns:
            {"status", "doc_id", "file_hash", "object_path", "file_size", "message"}
        """
        file_hash = compute_file_hash(file_data)
        file_ext = Path(original_filename).suffix.lower()
        file_size = len(file_data)

        # 解析实际使用的 parser 策略名（None → 根据文件类型自动选择）
        parser_strategy = resolve_parser_strategy(file_ext, file_size=file_size, user_choice=parser_strategy)

        # ── 去重检查 ──
        existing_by_hash = await self.pg.get_document_by_hash(user_id, file_hash)
        existing_by_filename = await self.pg.get_document_by_filename(
            user_id, original_filename
        )

        needs_rebuild = False

        if existing_by_hash:
            same_filename = existing_by_hash["original_filename"] == original_filename
            same_parser = existing_by_hash.get("parser_strategy") == parser_strategy

            if on_duplicate == "error":
                raise ValueError(
                    f"文件哈希冲突: '{original_filename}' 与文档 "
                    f"{existing_by_hash['id']} ('{existing_by_hash['original_filename']}') "
                    "内容完全相同，请设置 on_duplicate='rebuild' 或 'skip'"
                )
            elif on_duplicate == "rebuild":
                needs_rebuild = True
            elif same_filename and same_parser:
                # 场景 1：真正的一模一样
                logger.info(
                    "文件 %s 已存在且完全一致（哈希=%s, 策略=%s），跳过",
                    original_filename, file_hash[:16], parser_strategy,
                )
                return {
                    "status": "skipped",
                    "doc_id": existing_by_hash["id"],
                    "file_hash": file_hash,
                    "message": "文件已存在，无需重复添加",
                }
            else:
                # 场景 2（文件名不同）或场景 3（切分策略不同）
                needs_rebuild = True
                action = "文件名已更新" if not same_filename else "切分策略已变更"
                logger.info(
                    "文件 %s 哈希匹配但%s，重建索引",
                    original_filename, action,
                )

            if needs_rebuild:
                await self._delete_milvus_nodes(user_id, file_hash)
                await self.pg.delete_document_record(existing_by_hash["id"])

        elif existing_by_filename:
            # 场景 4：文件名相同但内容不同，文件已被替换
            old_hash = existing_by_filename["file_hash"]
            logger.info(
                "文件 %s 已被替换（旧哈希=%s, 新哈希=%s），重建索引",
                original_filename, old_hash[:16], file_hash[:16],
            )
            await self._delete_milvus_nodes(user_id, old_hash)
            await self.pg.delete_document_record(existing_by_filename["id"])

        # ── 上传 MinIO ──
        object_name = f"{user_id}/{original_filename}"
        await minio_client.put_object(
            BUCKET_NAME,
            object_name,
            io.BytesIO(file_data),
            length=file_size,
            content_type="application/octet-stream",
        )

        # ── 创建文档记录 ──
        doc_id = await self.pg.create_document(
            user_id=user_id,
            file_hash=file_hash,
            original_filename=original_filename,
            file_size=file_size,
            file_type=file_ext,
            object_path=object_name,
            parser_strategy=parser_strategy,
        )

        logger.info(
            "新文件上传: %s (哈希=%s, 策略=%s, doc_id=%s)",
            original_filename, file_hash[:16], parser_strategy, doc_id,
        )

        return {
            "status": "processing",
            "doc_id": doc_id,
            "file_hash": file_hash,
            "object_path": object_name,
            "file_size": file_size,
        }

    async def process_and_index(
        self,
        user_id: int,
        doc_id: int,
        original_filename: str,
        minio_client: Minio,
        embed_model,
        llm,
        parser_strategy: Optional[str] = None,
    ):
        """
        后台阶段：从 MinIO 下载 → 处理管道 → 写入 Milvus → 更新状态
        """
        tmp_path = None
        try:
            await self.pg.update_document_status(doc_id, status="processing")

            object_name = f"{user_id}/{original_filename}"
            suffix = Path(original_filename).suffix
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = Path(tmp.name)
            await minio_client.fget_object(BUCKET_NAME, object_name, str(tmp_path))

            doc = await self.pg.get_document(doc_id)
            file_hash = doc["file_hash"] if doc else None

            nodes = await process_document(
                file_path=tmp_path,
                embed_model=embed_model,
                llm=llm,
                parser_strategy=parser_strategy,
                original_filename=original_filename,
                file_hash=file_hash,
            )
            if not nodes:
                await self.pg.update_document_status(
                    doc_id, status="indexed", chunk_count=0,
                )
                logger.warning("文件 %s 无可提取内容", original_filename)
                return

            index = await async_get_milvus_index(user_id=user_id, embed_model=embed_model)
            await index.ainsert_nodes(nodes)

            await self.pg.update_document_status(
                doc_id, status="indexed", chunk_count=len(nodes),
            )
            logger.info("文件 %s 索引完成：%s 个节点", original_filename, len(nodes))
        except Exception as e:
            logger.exception("索引文件 %s 失败: %s", original_filename, e)
            await self.pg.update_document_status(
                doc_id, status="failed", error_message=str(e),
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)


index_manager = IndexManager()
