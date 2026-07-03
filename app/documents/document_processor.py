# document_processor.py — 文档加载、分析、切分、元数据注入管线
# 职责：接收文件 → 加载文档 → 动态选 parser → 切分 → 注入元数据 → 返回 nodes
# 不关心持久化（写入向量库由调用方负责）
import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Callable, List, Optional

from llama_index.core import SimpleDirectoryReader
from llama_index.core.schema import BaseNode
from llama_index.readers.file import PyMuPDFReader, PandasExcelReader, DocxReader
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.core.ingestion import IngestionPipeline

from app.documents.node_parser_factory import get_node_parser
from app.documents.datacleaning import DataCleaningComponent
from app.documents.document_status import DocumentStatus

logger = logging.getLogger(__name__)

_FILE_EXTRACTORS = {
    ".pdf": PyMuPDFReader(),
    ".xlsx": PandasExcelReader(),
    ".docx": DocxReader(),
}

async def process_document(
    file_path: Path,
    embed_model: HuggingFaceEmbedding,
    llm: Optional[object] = None,
    parser_strategy: Optional[str] = None,
    original_filename: Optional[str] = None,
    file_hash: Optional[str] = None,
    status_callback: Optional[Callable[[str], None]] = None,
    mineru_reader: Optional[object] = None,
    gateway: Optional[object] = None,
) -> List[BaseNode]:
    """
    完整的文档处理管线: 加载 → 分析 → 清洗 → 选策略 → 切分 → 注入元数据

    Args:
        file_path: 已保存文件的路径
        embed_model: 用于语义切分的嵌入模型
        llm: 大语言模型实例（部分 parser 需要）。为 None 时尝试从 gateway 获取
        parser_strategy: 手动指定切分策略（覆盖自动选择）
        original_filename: 原始文件名（用于 metadata 记录）
        file_hash: 文件哈希值（用于 metadata 记录）
        status_callback: 可选的回调函数，用于推清洗/切分等中间状态
        gateway: 智能模型网关（llm 不可用时动态获取）
    Returns:
        处理后的节点列表，为空表示文件无可提取内容
    """
    # ── 如果 llm 未提供，从 gateway 动态获取 ──
    if llm is None and gateway is not None:
        try:
            from app.gateway.types import ModelRole
            chain = gateway.get_model_chain(ModelRole.RETRIEVAL_LLM)
            if chain:
                llm = chain[0][1]
                logger.debug("从 gateway 获取 RETRIEVAL_LLM 用于文档处理")
        except Exception:
            pass

    # 加载文档（PDF 优先走 MinerU，失败回退 PyMuPDF）
    file_ext = file_path.suffix.lower()

    def _load():
        if file_ext == ".pdf" and mineru_reader is not None:
            try:
                logger.info("使用 MinerU 解析 PDF: %s", file_path.name)
                return mineru_reader.load_data(file_path)
            except Exception:
                logger.warning(
                    "MinerU 解析失败，回退 PyMuPDF: %s",
                    file_path.name, exc_info=True,
                )

        return SimpleDirectoryReader(
            input_files=[str(file_path)],
            file_extractor=_FILE_EXTRACTORS,
        ).load_data()

    documents = await asyncio.to_thread(_load)
    if not documents:
        logger.warning(f"文件 {original_filename or file_path.name} 未提取到任何内容")
        return []

    if status_callback:
        await status_callback(DocumentStatus.LOADING)

    # ── MinerU 成功后强制 Markdown 切分策略（保留标题层级）──
    actual_parser_strategy = parser_strategy
    if mineru_reader is not None and file_ext == ".pdf" and documents:
        if documents[0].metadata.get("parser") == "mineru":
            actual_parser_strategy = parser_strategy or "markdown"

    parser = get_node_parser(
        file_ext=file_ext,
        file_path=file_path,
        embed_model=embed_model,
        llm=llm,
        user_choice=actual_parser_strategy,
    )
    logger.info(f"使用切分策略: {parser.__class__.__name__}")

    # ── 清洗阶段 ──
    if status_callback:
        await status_callback(DocumentStatus.CLEANING)
    clean_pipeline = IngestionPipeline(transformations=[DataCleaningComponent()])
    cleaned_docs = await clean_pipeline.arun(documents=documents)

    # ── 切分阶段 ──
    if status_callback:
        await status_callback(DocumentStatus.SPLITTING)
    split_pipeline = IngestionPipeline(transformations=[parser])
    nodes = await split_pipeline.arun(documents=cleaned_docs)

    # 设置确定性 node_id = 文件哈希_内容哈希（同名内容幂等，Milvus 写入自动去重）
    # 同时注入位置元数据（供精确引用溯源使用）
    total = len(nodes)
    for idx, node in enumerate(nodes):
        content_hash = hashlib.sha256(node.get_content().encode()).hexdigest()[:16]
        node.node_id = f"{file_hash}_{content_hash}" if file_hash else content_hash

        # ── 业务元数据 ──
        node.metadata["file_name"] = original_filename or file_path.name
        if file_hash:
            node.metadata["file_hash"] = file_hash

        # ── 精确位置元数据（供 LLM 引用和前端定位）──
        node.metadata["chunk_index"] = idx
        node.metadata["chunk_total"] = total

        # 如果 parser 提供了页码，保留（PyMuPDF 会设置 page_label）
        if "page_label" in node.metadata and node.metadata["page_label"]:
            node.metadata["_has_page"] = True

        # 内容摘要（前 80 字），方便前端预览
        content_text = node.get_content()
        node.metadata["chunk_summary"] = content_text[:80].replace("\n", " ")

    logger.info(f"文件 {original_filename or file_path.name} 切分为 {len(nodes)} 个节点")
    return nodes
