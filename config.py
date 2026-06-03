def get_config():
    return {
        # ── 本地模型路径 ──────────────────────────────────
        "embed_model_path": r'D:\agentrag\models\Qwen3-Embedding-0.6B',
        "rerank_model_path": r'D:\agentrag\models\bge-reranker-v2-m3',

        # ── 兼容旧的模型名称引用 ──────────────────────────
        "langchain_chat_model_name": "deepseek-v4-flash",
        "llama_chat_model_name": "deepseek-v4-flash",
        "fallback_model_name": "qwen-turbo",

        # ── 模型注册表（智能网关使用） ────────────────────
        "models": {
            "deepseek-v4-flash": {
                "provider": "deepseek",
                "api_key_env": "DEEPSEEK_API_KEY",
                "base_url_env": "DEEPSEEK_BASE_URL",
                "roles": ["chat", "retrieval_llm"],
                "is_primary": True,
            },
            "qwen-turbo": {
                "provider": "bailian",
                "api_key_env": "BAILIAN_API_KEY",
                "base_url_env": "BAILIAN_BASE_URL",
                "roles": ["fallback_chat", "retrieval_rewriter"],
            },
        },

        # ── 降级链（按顺序尝试） ──────────────────────────
        "fallback_chains": {
            "chat": ["deepseek-v4-flash", "qwen-turbo"],
            "fallback_chat": ["qwen-turbo"],
            "retrieval_llm": ["deepseek-v4-flash", "qwen-turbo"],
            "retrieval_rewriter": ["qwen-turbo"],
        },

        # ── 熔断器配置 ────────────────────────────────────
        "circuit_breaker": {
            "failure_threshold": 5,       # 连续失败 N 次后熔断
            "cooldown_seconds": 30.0,     # 熔断后冷却 N 秒进入 HALF_OPEN
        },

        # ── 健康探活配置 ──────────────────────────────────
        "health_probe": {
            "interval_seconds": 30.0,     # 探活间隔
        },
    }