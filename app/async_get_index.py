# async_get_index.py
import os
import logging

from llama_index.vector_stores.milvus import MilvusVectorStore
from llama_index.vector_stores.milvus.utils import BM25BuiltInFunction
from llama_index.core import VectorStoreIndex
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from app.stores.pg_database import pg_db_manager

logger = logging.getLogger(__name__)
milvus_url = os.getenv("MILVUS_URL")



# 用于知识库检索使用
async def async_get_milvus_index(user_id: int, embed_model: HuggingFaceEmbedding) -> VectorStoreIndex:
    user = await pg_db_manager.get_user_by_id(user_id)
    if not user:
        raise ValueError(f"用户不存在: user_id={user_id}")
    username = user["username"]
    password = await pg_db_manager.get_milvus_password(user_id)
    if not password:
        raise ValueError(f"未获取到用户{username}的Milvus密码")
    collection_name = f"col_{username}"

    vector_store = MilvusVectorStore(
        uri=milvus_url,
        token=f"{username}:{password}",
        collection_name=collection_name,
        dim=1024,
        overwrite=False,     # 是否覆盖
        # 检索时返回的字段包含文本用于回答生成
        output_fields=["text", "file_name", "doc_id"],
        # 开启BM25
        # enable_sparse=True,
        # sparse_embedding_function=BM25BuiltInFunction(),
        # hybrid_ranker="RRFRanker",
        # hybrid_ranker_params={"k": 60},
    )
    index = VectorStoreIndex.from_vector_store(
        vector_store=vector_store,
        embed_model=embed_model,
        show_progress=True,
        use_async=True
    )
    return index