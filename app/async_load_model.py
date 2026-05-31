# async_load_model.py — 异步单例模型加载器
# ============== 异步单例 + 双重检查锁 ==========
# 大型模型 (embedding/rerank) 首次加载可能耗时 10s+，且只能加载一次。
# 此设计：
# 1. asyncio.Lock + double-check 避免并发请求时重复加载
# 2. local model 用 run_in_executor 放到线程池，不阻塞事件循环
# 3. 结果缓存到类级别 dict，整个应用生命周期不再重复加载
# 4. 同时支持 LangChain ChatModel + LlamaIndex LLM 两套接口
# =========================================================
from __future__ import annotations

import asyncio
import os
from typing import Dict
from dotenv import load_dotenv

from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline

from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.core.postprocessor import SentenceTransformerRerank
from llama_index.llms.openai_like import OpenAILike
from llama_index.llms.deepseek import DeepSeek

from langchain_huggingface import HuggingFacePipeline, ChatHuggingFace
from langchain.chat_models import init_chat_model
from langchain_deepseek.chat_models import ChatDeepSeek

load_dotenv("key.env")

# 异步调用
class AsyncLoadModel:
    _langchain_chat_models: Dict[str, any] = {}
    _llama_chat_models: Dict[str, any] = {}
    _fallback_model: Dict[str, any] = {}
    _embed_models: Dict[str, any] = {}
    _rerank_models: Dict[str, any] = {}
    
    _langchain_chat_locks: Dict[str, asyncio.Lock] = {}
    _llama_chat_locks: Dict[str, asyncio.Lock] = {}
    _fallback_locks: Dict[str, asyncio.Lock] = {}
    _embed_locks: Dict[str, asyncio.Lock] = {}
    _rerank_locks: Dict[str, asyncio.Lock] = {}

    
    @classmethod
    async def async_fallback_api_model(cls, model: str):
        if model in cls._fallback_model:
            return cls._fallback_model[model]
        async with cls._fallback_locks.setdefault(model, asyncio.Lock()):
            if model in cls._fallback_model:
                return cls._fallback_model[model]
            modelscope_api_key = os.getenv("MODELSCOPE_API_KEY")
            modelscope_base_url = os.getenv("MODELSCOPE_BASE_URL")
            model_instance = init_chat_model(
                model,
                model_provider="openai",
                api_key=modelscope_api_key,
                base_url=modelscope_base_url,
            )
            cls._fallback_model[model] = model_instance
            return model_instance
    
    @classmethod
    async def async_langchain_api_model(cls, model: str):
        if model in cls._langchain_chat_models:
            return cls._langchain_chat_models[model]
        async with cls._langchain_chat_locks.setdefault(model, asyncio.Lock()):
            if model in cls._langchain_chat_models:
                return cls._langchain_chat_models[model]
            deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
            deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL")
            model_instance = ChatDeepSeek(
                model=model,
                api_key=deepseek_api_key,
                api_base=deepseek_base_url,
                extra_body={"thinking": {"type": "disabled"}},
            )
            cls._langchain_chat_models[model] = model_instance
            return model_instance

    # # llamaindex openailike api
    # @classmethod
    # async def async_llama_index_api_model(cls, model: str) -> OpenAILike:
    #     if model in cls._llama_chat_models:
    #         return cls._llama_chat_models[model]
    #     async with cls._llama_chat_locks.setdefault(model, asyncio.Lock()):
    #         if model in cls._llama_chat_models:
    #             return cls._llama_chat_models[model]
    #         modelscope_api_key = os.environ.get("MODELSCOPE_API_KEY")
    #         modelscope_base_url = os.environ.get("MODELSCOPE_BASE_URL")
    #         model_instance = OpenAILike(
    #             model=model,
    #             api_key=modelscope_api_key,
    #             api_base=modelscope_base_url,
    #             is_chat_model=True,
    #         )
    #         cls._llama_chat_models[model] = model_instance
    #         return model_instance

    @classmethod
    async def async_llama_index_api_model(cls, model: str) -> DeepSeek:
        if model in cls._llama_chat_models:
            return cls._llama_chat_models[model]
        async with cls._llama_chat_locks.setdefault(model, asyncio.Lock()):
            if model in cls._llama_chat_models:
                return cls._llama_chat_models[model]
            deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
            deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL")
            model_instance = DeepSeek(
                model=model,
                api_key=deepseek_api_key,
                api_base=deepseek_base_url,
                is_chat_model=True,
            )
            cls._llama_chat_models[model] = model_instance
            return model_instance

    # 本地加载embedding模型
    @classmethod
    async def async_local_load_embed_model(cls, embed_model_path: str) -> HuggingFaceEmbedding:
        if embed_model_path in cls._embed_models:
            return cls._embed_models[embed_model_path]
        async with cls._embed_locks.setdefault(embed_model_path, asyncio.Lock()):
            if embed_model_path in cls._embed_models:
                return cls._embed_models[embed_model_path]
            loop = asyncio.get_running_loop()
            embed_model = await loop.run_in_executor(
                None,
                cls._load_embed_model_sync,
                embed_model_path,
            )
            cls._embed_models[embed_model_path] = embed_model
            return embed_model
    @staticmethod
    def _load_embed_model_sync(embed_model_path: str):
        return HuggingFaceEmbedding(
            model_name=embed_model_path,
            device="cpu",
            embed_batch_size=4,
            max_length=1024
        )
    
    # 本地加载rerank模型
    @classmethod
    async def async_local_load_rerank_model(cls, rerank_model_path: str) -> SentenceTransformerRerank:
        if rerank_model_path in cls._rerank_models:
            return cls._rerank_models[rerank_model_path]
        async with cls._rerank_locks.setdefault(rerank_model_path, asyncio.Lock()):
            if rerank_model_path in cls._rerank_models:
                return cls._rerank_models[rerank_model_path]
            loop = asyncio.get_running_loop()
            rerank_model = await loop.run_in_executor(
                None,
                cls._load_rerank_model_sync,
                rerank_model_path,
            )
            cls._rerank_models[rerank_model_path] = rerank_model
            return rerank_model
    @staticmethod
    def _load_rerank_model_sync(rerank_model_path: str):
        return SentenceTransformerRerank(
            top_n=20,   # 设大兜底值，运行时由 RetrievalPipeline.top_k 覆盖
            model=rerank_model_path,
            device="cpu"
        )