# CLAUDE.md — TKAgent 企业级 RAG 项目

## 项目概览

**TKAgent** — 企业级 RAG 智能助手，基于 FastAPI + LlamaIndex + LangGraph + DeepAgents 构建。

核心能力：
- 多 Agent 编排（Router/Supervisor → Specialist Sub-Agent）
- 企业知识库 RAG（自适应重查 × 多路检索 × RRF 融合 × 精排 × 动态裁剪）
- 文档全生命周期管理（上传 → MinIO → 处理管线 → Milvus 索引）
- 工作流引擎（报销审批等长周期业务流程）
- 在线/离线评估（Langfuse + deepEval）

## 启动与运行

```bash
# 安装依赖使用 uv
uv sync

# 下载模型
python download_model/download_model.py

# 启动服务
python main.py
# 服务监听 0.0.0.0:8000
```

## 外部依赖

| 服务 | 默认端口 | 用途 |
|------|---------|------|
| PostgreSQL (x2) | 5433 | auth_db（用户/会话/文档元数据）+ conversations_db（LangGraph checkpointer/store） |
| Milvus | 19530 | 向量数据库，RBAC 多租户 |
| Redis | 6380 | 缓存（非致命依赖） |
| MinIO | 9002 | 文档对象存储 |

配置项在 `key.env`（不提交），模板参见 `key.env`。

## 模型

本地模型放在 `models/` 目录（通过 `download_model/download_model.py` 下载）：
- Qwen3-Embedding-0.6B — 文本嵌入
- bge-reranker-v2-m3 — 精排重排序

API 模型在 `config.py` 中配置（DeepSeek 主模型 / Qwen 备用降级模型）。

## 评估

```bash
# 在线评估 — 从 Langfuse 拉取 traces
python -m app.evaluation.online_eval --since 24h --limit 100

# RAG 离线评估
python -m app.evaluation.offline_eval_rag

# Agent 离线评估
python -m app.evaluation.offline_eval_agent
```

## API 端点

| 端点 | 方法 | 用途 |
|------|------|------|
| `/auth/register` | POST | 注册（自动创建 Milvus 租户） |
| `/auth/login` | POST | 登录（返回 JWT） |
| `/chat` | POST | SSE 流式聊天 |
| `/sources/{session_id}` | GET | 获取检索来源文件列表 |
| `/feedback` | POST | 点赞/点踩反馈 |
| `/sessions` | GET | 会话列表 |
| `/upload` | POST | 文件上传（→ MinIO → 处理管线 → Milvus） |
| `/documents` | GET | 文档列表/搜索 |
| `/documents/{file_hash}` | DELETE | 删除文档 |
| `/documents/{file_hash}` | PUT | 替换文档 |
| `/workflow/...` | - | 工作流工单 CRUD |

## 架构总览

```
main.py                       # FastAPI 入口 — lifespan 初始化所有资源
└── 资源初始化顺序:
    1. pg_db_manager.initialize()       # 双数据库连接池
    2. milvus_db_manager.initialize()   # Milvus 管理员连接
    3. redis_manager.initialize()       # Redis 缓存（非致命）
    4. WorkflowEngine                   # LangGraph 工作流引擎
    5. initialize_model()               # 嵌入模型/reranker/LLM 加载
    6. RetrievalPipeline                # 统一检索管道
    7. discover_specialist_agents()     # 扫描 skills/*/AGENT.md
    8. MinIO 客户端初始化

app/
├── async_create_agent.py    # Agent 工厂 — 组装 deepagents 中间件管线
├── agent_definitions.py     # Specialist SubAgent 发现 + Router Prompt
├── retrieval.py             # 统一检索管道 (QueryRewriter → MultiRecall+RRF → Rerank → DynamicTopK)
├── async_tools.py           # Agent 工具 (get_time / web_search / knowledge_query / reimbursement)
│
├── index_manager.py         # 索引管理器 — 文档生命周期编排
├── document_processor.py    # 文档处理管线 (load → clean → parse → split → metadata)
├── node_parser_factory.py   # 按文件类型自动选择 Node Parser
├── milvus_manager.py        # Milvus RBAC 多租户管理
│
├── pg_database.py           # 双数据库管理器 (auth_db + conversations_db)
├── skills/
│   └── {finance,hr,engineering,business}/
│       ├── SKILL.md         # 技能定义
│       └── AGENT.md         # SubAgent 角色定义
├── workflow/
│   ├── engine.py            # 工作流引擎 (create/take_action/get/list)
│   ├── reimbursement.py     # 报销审批 LangGraph 状态机
│   └── models.py            # 工作流 Pydantic 模型
├── evaluation/
│   ├── online_eval.py       # 在线评估 (Langfuse traces → deepEval)
│   ├── offline_eval_rag.py  # RAG 离线评估
│   └── offline_eval_agent.py# Agent 离线评估
└── routes/
    ├── auth_routes.py       # 注册/登录
    ├── session_routes.py    # 会话管理
    ├── upload_routes.py     # 文件上传
    ├── document_routes.py   # 文档管理 (列表/搜索/删除/替换/SSE事件)
    └── workflow_routes.py   # 工单 API
```

## 三条核心数据流

### 1. 聊天
```
POST /chat → SSE 流式响应
  → Agent 中间件管线:
    SkillsMiddleware(注入技能元数据到 system prompt)
    → ModelCallLimit(thread=100次/会话, run=15次/请求)
    → ModelFallback(DeepSeek→Qwen 降级)
    → ModelRetry(3次指数退避)
    → ToolCallLimit(全局50次, web_search单独20次)
    → ToolRetry(3次指数退避)
  → LLM 自路由: 通过 Function Calling 自主选择工具/SubAgent
  → 工具调用 (web_search/knowledge_query/task/...)
  → Specialist SubAgent 委托 (通过 task 工具)
  → 流式输出 + SSE events + 引用溯源
```

### 2. 文档上传与索引
```
POST /upload → SHA-256 哈希 → 去重检测(pg_database)
  → MinIO 存储
  → 异步 process_and_index:
    → SimpleDirectoryReader 加载
    → DataCleaningComponent 清洗
    → NodeParser(按文件类型选择: PDF/Excel/Docx/代码/语义)
    → IngestionPipeline.arun()
    → node_id = 文件哈希_内容哈希(幂等去重)
    → Milvus 写入
    → SSE 状态推送(processing/done/error)
```

### 3. 检索
```
用户提问 → RetrievalPipeline:
  Step1: QueryRewriter — LLM 分类(FACTUAL/COMPLEX/SPECIFIC)并按策略扩写
  Step2: MultiRecall + RRF — 多查询变体并行检索 + Reciprocal Rank Fusion 融合
  Step3: Rerank — bge-reranker-v2-m3 精排
  Step4: DynamicTopK — 按分数分布自动裁剪(分数降幅超过30%截断)
  → 注入 LLM 上下文 + 引用溯源
```

## 关键设计决策

- **双数据库读写分离**: auth_db (asyncpg) 存用户/会话/文档元数据; conversations_db (psycopg AsyncConnectionPool) 存 LangGraph checkpointer + store
- **Milvus RBAC 多租户**: 每个注册用户自动创建独立 Milvus 用户 + 角色 + Collection，密码加密存储
- **节点去重**: `node_id = 文件哈希_内容哈希`，Milvus 写入幂等
- **LLM 自路由**: 参考 Claude Code / HermesAgent / OpenClaw，不设外部分类器，所有工具描述和 SubAgent 列表在 system prompt 中始终可见，LLM 通过 Function Calling 自行选择。Skills 元数据通过 SkillsMiddleware 按需加载完整内容
- **配置方式**: 敏感配置在 `key.env`（不提交），通用配置在 `config.py`
- **行为准则**: 见 `.claude/CLAUDE.md`（先思考再编码 / 简洁优先 / 精准修改 / 目标驱动执行）

## 开发指南

```bash
# 新增 Specialist Agent
# 1. 在 app/skills/ 下创建新目录
# 2. 编写 SKILL.md（技能定义描述 + YAML frontmatter: name, description）
# 3. 编写 AGENT.md（SubAgent 角色定义 + YAML frontmatter: name, description）
#    AGENT.md 会被 discover_specialist_agents() 自动发现并注入 system prompt

# 新增 Agent 工具
# 1. 在 app/async_tools.py 中定义 async 函数
# 2. 在 main.py 的 tools 列表中注册

# 新增工作流
# 1. 在 app/workflow/ 下定义 LangGraph 状态机
# 2. 在 main.py lifespan 中 register_workflow("name", build_graph)
# 3. 在 async_tools.py 中添加对应 tool

# 新增路由
# 在 app/routes/ 下创建 xxx_routes.py → main.py include_router
```
