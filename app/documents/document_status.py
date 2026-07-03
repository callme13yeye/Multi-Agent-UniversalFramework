"""文档处理状态枚举 — 覆盖从上传到索引的完整生命周期"""

from enum import Enum


class DocumentStatus(str, Enum):
    """文档处理管线状态

    状态流转: UPLOADED → STORING → QUEUING → DOWNLOADING → LOADING
              → CLEANING → SPLITTING → INDEXING → INDEXED
              (GRAPH_EXTRACTING 与 INDEXING 并行)
              → FAILED (任意阶段异常)
    """

    UPLOADED = "uploaded"                # 数据库记录已创建
    STORING = "storing"                  # 正在上传 MinIO
    QUEUING = "queuing"                  # MinIO 完成，等待后台处理
    DOWNLOADING = "downloading"          # 后台任务从 MinIO 下载文件
    LOADING = "loading"                  # 正在加载文档内容
    CLEANING = "cleaning"               # 数据清洗
    SPLITTING = "splitting"             # 文档切分
    INDEXING = "indexing"               # 正在写入 Milvus 向量库
    GRAPH_EXTRACTING = "graph_extracting"  # 知识图谱实体抽取（与 INDEXING 并行）
    INDEXED = "indexed"                 # 处理完成
    FAILED = "failed"                   # 处理失败（终态）

    # API 层专用（不写入 DB）
    SKIPPED = "skipped"                 # 哈希去重，跳过上传
