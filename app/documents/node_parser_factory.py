# node_parser_factory.py — 文档解析策略工厂
# ============== 策略模式文档解析 ==========
# 根据文件类型自动选择最优 Node Parser：
# .md → MarkdownElementNodeParser (保留标题层级)
# .html → HTMLNodeParser (解析 DOM 结构)
# .xlsx → HierarchicalNodeParser (表格结构)
# 其他 → SemanticSplitterNodeParser (语义切分)
# 工厂封装了创建逻辑，新增策略不改调用方代码。
# ===========================================

import logging
from pathlib import Path
from typing import Optional

from llama_index.core.node_parser import (
    SemanticSplitterNodeParser,
    MarkdownElementNodeParser,
    HTMLNodeParser,
    HierarchicalNodeParser,
    SentenceWindowNodeParser,
    UnstructuredElementNodeParser,
    SentenceSplitter,
    SemanticDoubleMergingSplitterNodeParser
)
from llama_index.node_parser.topic import TopicNodeParser
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

logger = logging.getLogger(__name__)

# 文件扩展名到默认 parser 策略名的映射
_EXTENSION_PARSER_MAP: dict[str, str] = {
    ".md": "markdown",
    ".html": "html",
    ".xlsx": "unstructured",
    ".txt": "sentence_window",
}

def resolve_parser_strategy(
    file_ext: str,
    file_size: Optional[int] = None,
    user_choice: Optional[str] = None,
) -> str:
    """解析最终会使用的 parser 策略名称（与 get_node_parser 逻辑一致）"""
    if user_choice:
        return user_choice
    if file_ext in _EXTENSION_PARSER_MAP:
        return _EXTENSION_PARSER_MAP[file_ext]
    if file_ext in (".pdf", ".docx") and file_size and file_size > 5 * 1024 * 1024:
        return "unstructured"
    return "semantic"

def _make_semantic_parser(embed_model: HuggingFaceEmbedding):
    """创建默认的语义切分 parser（不依赖 LLM）。"""
    return SemanticSplitterNodeParser(
        embed_model=embed_model,
        buffer_size=3,
        breakpoint_percentile_threshold=95,
        include_metadata=True,
    )


def get_node_parser(
        file_ext: str,
        file_path: Path,
        embed_model: HuggingFaceEmbedding,
        user_choice: Optional[str] = None,
        llm: Optional[object] = None,
):
    """
    根据文件的扩展名和内容特征自动选择合适的NodeParser
    后期如果用户想手动控制则指定user_choice覆盖自动匹配
    """
    if user_choice:
        return _create_node_parser_by_user(user_choice, embed_model, llm)
    if file_ext == ".md":
        if llm is not None:
            return MarkdownElementNodeParser(llm=llm, include_metadata=True)
        logger.warning("LLM 不可用，MarkdownElementNodeParser 降级为语义切分")
        return _make_semantic_parser(embed_model)
    if file_ext == ".html":
        return HTMLNodeParser(include_metadata=True)
    if file_ext == ".xlsx":
        if llm is not None:
            return UnstructuredElementNodeParser(llm=llm)
        logger.warning("LLM 不可用，UnstructuredElementNodeParser 降级为语义切分")
        return _make_semantic_parser(embed_model)
    if file_ext in (".pdf", ".docx"):
        if file_path.stat().st_size > 5 * 1024 * 1024:
            if llm is not None:
                return UnstructuredElementNodeParser(llm=llm)
            logger.warning("LLM 不可用，UnstructuredElementNodeParser 降级为语义切分")
            return _make_semantic_parser(embed_model)
        else:
            return _make_semantic_parser(embed_model)
    if file_ext == ".txt":
        return SentenceWindowNodeParser(
            window_size=3,
            include_metadata=True,
        )
    return _make_semantic_parser(embed_model)

def _create_node_parser_by_user(user_choice: str, embed_model: HuggingFaceEmbedding, llm):
    """根据名称创建 parser（用于手动覆盖）"""
    if user_choice == "semantic":
        return _make_semantic_parser(embed_model)
    if user_choice == "markdown":
        if llm is not None:
            return MarkdownElementNodeParser(llm=llm, include_metadata=True)
        logger.warning("LLM 不可用，MarkdownElementNodeParser 降级为语义切分")
        return _make_semantic_parser(embed_model)
    if user_choice == "html":
        return HTMLNodeParser(include_metadata=True)
    if user_choice == "sentence_window":
        return SentenceWindowNodeParser(window_size=3, include_metadata=True)
    if user_choice == "unstructured":
        if llm is not None:
            return UnstructuredElementNodeParser(llm=llm)
        logger.warning("LLM 不可用，UnstructuredElementNodeParser 降级为语义切分")
        return _make_semantic_parser(embed_model)
    if user_choice == "sentence_splitter":
        return SentenceSplitter(chunk_size=512, chunk_overlap=50, include_metadata=True)
    raise ValueError(f"未知的 parser 名称: {user_choice}")
