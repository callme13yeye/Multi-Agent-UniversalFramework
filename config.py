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

        # ── 超时配置（子智能体执行 & 工具调用） ────────────
        "timeouts": {
            "step_execution_seconds": 300.0,   # 单步骤整体超时（5分钟）
            "tool_call_seconds": 60.0,         # 单个工具调用超时（1分钟）
            "approval_wait_hours": 72.0,       # 审批最长等待时间（72小时后告警）
        },

        # ── 清理配置 ────────────────────────────────────
        "cleanup": {
            "child_thread_ttl_seconds": 3600.0,  # 子Thread checkpointer TTL（1小时）
        },

        # ── 速率限制 ────────────────────────────────────
        "rate_limits": {
            "user_per_minute": 60,            # 每用户每分钟最大请求数
            "ip_per_minute": 300,             # 每IP每分钟最大请求数
            "window_seconds": 60,             # 滑动窗口大小
        },

        # ── 知识图谱配置 ────────────────────────────────
        "knowledge_graph": {
            "enabled": True,                    # 是否启用知识图谱
            "graph_rag_enabled": True,          # 是否在检索管道中启用 GraphRAG
            "max_chunks_per_doc": 10,           # 每个文档最多处理的文本块数
            "chunk_size": 2000,                 # 实体抽取的文本块大小（字符数）
            "max_entities_per_chunk": 15,       # 每块文本最多抽取的实体数
            "max_graph_hops": 2,                # GraphRAG 最大跳数
            "max_graph_entities": 15,           # GraphRAG 最大返回实体数
            "max_graph_relations": 30,          # GraphRAG 最大返回关系数
        },

        # ── MinerU PDF 智能解析配置 ──────────────────────
        "mineru": {
            "enabled": True,                       # 是否启用 MinerU（关闭则降级 PyMuPDF）
            "models_dir": "D:/agentrag/models/mineru-pipeline",
            "device_mode": "cpu",                  # cpu | cuda | mps
            "backend": "pipeline",                 # 解析后端：pipeline（CPU/GPU通用）
            "parse_method": "auto",                # auto | txt | ocr
            "lang": "ch",                          # OCR 语言
            "formula_enable": True,                # 是否启用公式识别
            "table_enable": True,                  # 是否启用表格识别
            "timeout_seconds": 300.0,              # 单次解析超时（秒）
        },

        # ── 熔断配置（子智能体级别） ────────────────────
        "specialist_circuit_breaker": {
            "failure_threshold": 3,            # 同一Specialist连续失败N次后熔断
            "cooldown_seconds": 60.0,          # 熔断冷却时间
        },

        # ── 优雅关闭配置 ────────────────────────────────
        "graceful_shutdown": {
            "drain_timeout_seconds": 30.0,     # 等待运行中任务完成的超时
            "global_timeout_seconds": 15.0,    # 全局关闭超时
        },

        # ── 自进化系统配置 ────────────────────────────────
        "evolution": {
            "enabled": True,                    # 是否启用自进化系统
            "scan_interval_hours": 6.0,         # 自动扫描间隔（小时）
            "analysis_lookback_hours": 24,      # 每次分析回溯多久的任务
            "max_gaps_per_scan": 5,             # 每次扫描最多生成多少缺口报告
            "min_tasks_for_analysis": 10,       # 最少需要多少已完成任务才触发分析
            "auto_approve_threshold": 0.0,      # 自动化审批阈值（0.0=永远人工审批, 1.0=完全自动）
            "validation_min_pass_rate": 0.7,    # 验证通过率下限
            "llm_model": "deepseek-v4-flash",   # 进化系统使用的 LLM
        },
    }