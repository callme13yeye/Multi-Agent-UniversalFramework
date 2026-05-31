def get_config():
    return {
        "embed_model_path": r'D:\agentrag\models\Qwen3-Embedding-0.6B',
        "rerank_model_path": r'D:\agentrag\models\bge-reranker-v2-m3',
        "langchain_chat_model_name": "deepseek-v4-flash", # deepseek专用 "deepseek-chat"
        "llama_chat_model_name": "deepseek-v4-flash", # 魔塔使用 "Qwen/Qwen3-235B-A22B-Instruct-2507" "deepseek-ai/DeepSeek-V3.2"
        "fallback_model_name": "Qwen/Qwen3-235B-A22B-Instruct-2507", # 魔塔使用 "Qwen/Qwen3-235B-A22B-Instruct-2507" "deepseek-ai/DeepSeek-V3.2"
    }