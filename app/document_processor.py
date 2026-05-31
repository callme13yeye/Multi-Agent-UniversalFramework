# document_processor.py — 文档加载、分析、切分、元数据注入管线
# 职责：接收文件 → 加载文档 → 动态选 parser → 切分 → 注入元数据 → 返回 nodes
# 不关心持久化（写入向量库由调用方负责）
import asyncio
import hashlib
import logging
from pathlib import Path
from typing import List, Optional

from llama_index.core import SimpleDirectoryReader
from llama_index.core.schema import BaseNode
from llama_index.readers.file import PyMuPDFReader, PandasExcelReader, DocxReader
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.core.ingestion import IngestionPipeline
# TODO: 可配置元数据提取器，由用户决定是否开启以提升检索精度
# from llama_index.core.extractors import QuestionsAnsweredExtractor, TitleExtractor

from app.node_parser_factory import get_node_parser
from app.datacleaning import DataCleaningComponent

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
) -> List[BaseNode]:
    """
    完整的文档处理管线: 加载 → 分析 → 清洗 → 选策略 → 切分 → 注入元数据

    Args:
        file_path: 已保存文件的路径
        embed_model: 用于语义切分的嵌入模型
        llm: 大语言模型实例（部分 parser 需要）
        parser_strategy: 手动指定切分策略（覆盖自动选择）
        original_filename: 原始文件名（用于 metadata 记录）
        file_hash: 文件哈希值（用于 metadata 记录）
    Returns:
        处理后的节点列表，为空表示文件无可提取内容
    """
    # 加载文档（根据后缀选择 extractor）特别是原生PDF和扫描PDF可以通过官方的 LlamaParse 来区分处理，扫描PDF可以配合 OCR 来提取文本
    def _load():
        return SimpleDirectoryReader(
            input_files=[str(file_path)],
            file_extractor=_FILE_EXTRACTORS,
        ).load_data()

    documents = await asyncio.to_thread(_load)
    if not documents:
        logger.warning(f"文件 {original_filename or file_path.name} 未提取到任何内容")
        return []
    
    file_ext = file_path.suffix.lower()
    parser = get_node_parser(
        file_ext=file_ext,
        file_path=file_path,
        embed_model=embed_model,
        llm=llm,
        user_choice=parser_strategy,
    )
    logger.info(f"使用切分策略: {parser.__class__.__name__}")

    # 创建数据管道
    pipeline = IngestionPipeline(
        transformations=[
            DataCleaningComponent(),
            parser,
            # TODO: 可配置元数据提取，由用户决定是否开启
            # QuestionsAnsweredExtractor(questions=3, llm=llm),
            # TitleExtractor(llm=llm),
        ]
    )
    
    nodes = await pipeline.arun(documents=documents)

    # 设置确定性 node_id = 文件哈希_内容哈希（同名内容幂等，Milvus 写入自动去重）
    for node in nodes:
        content_hash = hashlib.sha256(node.get_content().encode()).hexdigest()[:16]
        node.node_id = f"{file_hash}_{content_hash}" if file_hash else content_hash

    # 注入业务元数据（供后续过滤和溯源使用）
    for node in nodes:
        node.metadata["file_name"] = original_filename or file_path.name
        if file_hash:
            node.metadata["file_hash"] = file_hash

    logger.info(f"文件 {original_filename or file_path.name} 切分为 {len(nodes)} 个节点")
    return nodes
