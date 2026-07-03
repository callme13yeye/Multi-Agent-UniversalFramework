# app/documents — 文档全生命周期管理
#
# 负责文档从上传到索引的完整管线：
#   upload → MinIO → process → index → retrieval
#
# 组件：
#   document_processor.py   — 文档处理管线（load → clean → parse → split → metadata）
#   index_manager.py        — 索引管理器（文档生命周期编排）
#   node_parser_factory.py  — 按文件类型自动选择 Node Parser
#   datacleaning.py         — 数据清洗组件
#   document_event_bus.py   — 文档状态变更 SSE 推送（pub/sub）
#   retrieval.py            — 统一检索管道（QueryRewriter → MultiRecall+RRF → Rerank → DynamicTopK）

from app.documents.document_processor import process_document
from app.documents.document_status import DocumentStatus
from app.documents.index_manager import IndexManager, index_manager
from app.documents.node_parser_factory import get_node_parser, resolve_parser_strategy
from app.documents.datacleaning import DataCleaningComponent
from app.documents.document_event_bus import DocumentEventBus, document_event_bus
from app.documents.retrieval import RetrievalPipeline, QueryRewriter

__all__ = [
    "process_document",
    "DocumentStatus",
    "IndexManager", "index_manager",
    "get_node_parser", "resolve_parser_strategy",
    "DataCleaningComponent",
    "DocumentEventBus", "document_event_bus",
    "RetrievalPipeline", "QueryRewriter",
]
