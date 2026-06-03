# node_parser_factory.py — 文档解析策略工厂
# ============== 策略模式文档解析 ==========
# 根据文件类型自动选择最优 Node Parser：
# .md → MarkdownElementNodeParser (保留标题层级)
# .html → HTMLNodeParser (解析 DOM 结构)
# .xlsx → HierarchicalNodeParser (表格结构)
# 其他 → SemanticSplitterNodeParser (语义切分)
# 工厂封装了创建逻辑，新增策略不改调用方代码。
# ===========================================

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
from pathlib import Path
from typing import Optional

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
        return MarkdownElementNodeParser(include_metadata=True)
    if file_ext == ".html":
        return HTMLNodeParser(include_metadata=True)
    if file_ext == ".xlsx":
        return UnstructuredElementNodeParser(llm=llm)
    if file_ext in (".pdf", ".docx"):
        if file_path.stat().st_size > 5 * 1024 * 1024:
            return UnstructuredElementNodeParser(llm=llm)
        else:
            return SemanticSplitterNodeParser(
                # 本质上按句子切分，后续再根据相似度进行合并
                embed_model=embed_model,
                buffer_size=3,  # 单次计算相似度的句子数量 默认为1 逻辑越严密设置越高，也可以称为滑动窗口
                breakpoint_percentile_threshold=95, # 阈值越高，对语义变化越不敏感，块越大
                include_metadata=True,
            )
    if file_ext == ".txt":
        return SentenceWindowNodeParser(
            window_size=3,
            include_metadata=True,
        )
    return SemanticSplitterNodeParser(
        embed_model=embed_model,
        buffer_size=3,
        breakpoint_percentile_threshold=95,
        include_metadata=True,
    )

def _create_node_parser_by_user(user_choice: str, embed_model: HuggingFaceEmbedding, llm):
    """根据名称创建 parser（用于手动覆盖）"""
    if user_choice == "semantic":
        return SemanticSplitterNodeParser(
            embed_model=embed_model,
            buffer_size=3,
            breakpoint_percentile_threshold=95,
            include_metadata=True,
        )
    if user_choice == "markdown":
        return MarkdownElementNodeParser(include_metadata=True)
    if user_choice == "html":
        return HTMLNodeParser(include_metadata=True)
    if user_choice == "sentence_window":
        return SentenceWindowNodeParser(window_size=3, include_metadata=True)
    if user_choice == "unstructured":
        return UnstructuredElementNodeParser(llm=llm)
    if user_choice == "sentence_splitter":
        return SentenceSplitter(chunk_size=512, chunk_overlap=50, include_metadata=True)
    raise ValueError(f"未知的 parser 名称: {user_choice}")
