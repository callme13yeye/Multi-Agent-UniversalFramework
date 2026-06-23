# CLAUDE.md — TK-MultiAgent 企业级多智能体项目

## 项目概览

**TK-MultiAgent** — 企业级 Multi-Agent 智能协作系统，基于 FastAPI + LlamaIndex + LangGraph + DeepAgents 构建。

核心能力：
- 三层 DeepAgent 编排（Triage 分流 → Executor 后台执行 → Specialist Sub-Agent）
- 企业知识库 RAG（自适应重查 × 多路检索 × RRF 融合 × 精排 × 动态裁剪）
- 文档全生命周期管理（上传 → MinIO → 处理管线 → Milvus 索引）
- 人审/审批（Executor DeepAgent + HITL interrupt 挂起/恢复 + 3轮兜底检测）
- 智能模型网关（多模型注册 × 健康探活 × 熔断降级 × 热切换 × 限流）
- 在线/离线评估（Langfuse + deepEval）
- 全链路追踪（contextvars trace_id 传播 + 日志注入）

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
| Redis | 6380 | 缓存 + 滑动窗口限流（非致命依赖） |
| MinIO | 9002 | 文档对象存储 |

配置项在 `key.env`（不提交），模板参见 `key.env`。

## 模型

本地模型放在 `models/` 目录（通过 `download_model/download_model.py` 下载）：
- Qwen3-Embedding-0.6B — 文本嵌入
- bge-reranker-v2-m3 — 精排重排序

API 模型在 `config.py` 中配置并注册到 `ModelGateway`：
- `deepseek-v4-flash` — 主模型（chat + retrieval_llm）
- `qwen-turbo` — 备用降级模型（fallback_chat + retrieval_rewriter）

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
| `/auth/me` | GET | 当前用户信息 |
| `/chat` | POST | SSE 流式聊天（唯一对话入口） |
| `/sessions` | GET | 会话列表 |
| `/sessions` | POST | 创建新会话 |
| `/sessions/{id}` | DELETE | 删除会话 |
| `/sessions/{id}/rename` | PATCH | 重命名会话 |
| `/sessions/{id}/messages` | GET | 历史消息 |
| `/tasks/{id}` | GET | 任务详情（含审批状态/进度） |
| `/tasks/{id}/resume` | POST | 人审决策恢复（approved/rejected） |
| `/tasks/{id}/events` | GET | 任务状态 SSE 实时推送 |
| `/tasks/{id}` | DELETE | 取消/删除任务 |
| `/sources/{session_id}` | GET | 获取检索来源文件列表 |
| `/feedback` | POST | 点赞/点踩反馈 |
| `/upload` | POST | 文件上传（→ MinIO → 处理管线 → Milvus） |
| `/documents` | GET | 文档列表（分页+搜索） |
| `/documents/{id}` | DELETE | 删除文档 |
| `/documents/{id}/file` | GET | 查看文件内容 |
| `/documents/events` | GET | 文档处理状态 SSE 推送 |
| `/documents/{id}/replace` | PUT | 替换文档 |
| `/admin/models` | GET | 查询所有模型状态（需 X-Admin-Token） |
| `/admin/models/{name}/activate` | PUT | 手动激活模型 |
| `/admin/models/{name}/circuit` | PUT | 手动重置熔断器 |

## 架构总览

```
main.py                       # FastAPI 入口 — lifespan 初始化所有资源
└── 资源初始化顺序:
    1. pg_db_manager.initialize()       # 双数据库连接池
    2. milvus_db_manager.initialize()   # Milvus 管理员连接
    3. redis_manager.initialize()       # Redis 缓存（非致命）
    4. ModelGateway                     # 注册模型 → 健康探活 → 熔断器初始化
    5. initialize_model()               # 嵌入模型/reranker/LLM 加载
    6. RetrievalPipeline                # 统一检索管道
    7. discover_specialist_agents()     # 扫描 subagents/*/AGENT.md
    8. Executor DeepAgent + TaskExecutor  # 后台任务引擎（LLM 自主规划→委托→审批→汇报）
    9. MinIO 客户端初始化

app/
├── async_create_agent.py    # Agent 工厂 — 组装 deepagents 中间件管线
├── agent_definitions.py     # Specialist SubAgent 发现 + Router Prompt
├── retrieval.py             # 统一检索管道 (QueryRewriter → MultiRecall+RRF → Rerank → DynamicTopK)
│
├── index_manager.py         # 索引管理器 — 文档生命周期编排
├── document_processor.py    # 文档处理管线 (load → clean → parse → split → metadata)
├── node_parser_factory.py   # 按文件类型自动选择 Node Parser
├── milvus_manager.py        # Milvus RBAC 多租户管理
│
├── pg_database.py           # 双数据库管理器 (auth_db + conversations_db)
├── task_context.py          # 任务上下文管理器 (三层记忆: Hot/Warm/Cold + journal 写入/快照)
├── trace_context.py         # 全链路 trace_id 传播 (contextvars + TraceIdFilter)
├── status_handler.py        # Agent 运行时状态回调 (AsyncCallbackHandler → SSE 事件)
│
├── subagents/               # Specialist Sub-Agent 定义
│   ├── general/             #   通用助手
│   ├── recruitment_analytics/   # 招聘数据分析师
│   ├── recruitment_approval/    # 审批发起专员
│   ├── recruitment_interview/   # 面试协调专家
│   ├── recruitment_job/         # 职位管理专家
│   ├── recruitment_offer/       # Offer 管理专家
│   ├── recruitment_resume/      # 简历推送专员
│   └── recruitment_talent/      # 人才搜索专家
│
├── harness/
│   ├── event_bus.py         # 事件总线
│   ├── task_executor.py     # 后台任务执行器 (HITL 审批兜底/熔断)
│   └── dead_letter.py       # 死信队列
│
├── tools/                   # 工具注册中心（共 16 个工具）
│   ├── _registry.py         # TOOL_REGISTRY dict + @register_tool 装饰器
│   ├── common.py            # 通用工具 (时间/搜索)
│   ├── knowledge.py         # 知识库 RAG 检索
│   ├── task.py              # create_background_task
│   ├── task_query.py        # get_task_status
│   ├── approval.py          # Specialist 侧审批工具
│   ├── request_approval.py  # Executor 侧审批工具（触发 HITL interrupt）
│   ├── read_journal.py      # Executor 执行日志读取
│   ├── resources.py         # 非工具基础设施 (知识库资源/缓存/Moka客户端/任务执行器)
│   ├── moka_client.py       # Moka API 客户端封装
│   ├── moka_candidate.py    # 候选人搜索/详情
│   ├── moka_job.py          # 职位管理
│   ├── moka_resume.py       # 简历推送
│   ├── moka_interview.py    # 面试查询
│   ├── moka_offer.py        # Offer 状态查询
│   └── moka_analytics.py    # 招聘漏斗分析
│
├── prompts/
│   ├── triage_prompt.py     # Triage DeepAgent system prompt
│   └── executor_prompt.py   # Executor DeepAgent system prompt
│
├── schemas/
│   └── registry.py          # SchemaRegistry — SubAgent 输出 Schema 管理
│
├── gateway/                 # 智能模型网关
│   ├── types.py             # 共享类型 (CircuitState, ModelRole, ModelSpec, HealthRecord)
│   ├── model_gateway.py     # 模型注册/路由/健康跟踪/热切换
│   ├── circuit_breaker.py   # 三态熔断器 (CLOSED → OPEN → HALF_OPEN)
│   ├── health_probe.py      # 后台定期探活
│   ├── gateway_middleware.py # 健康感知路由中间件
│   └── rate_limit_middleware.py # Redis 滑动窗口限流（降级：内存模式）
│
├── evaluation/
│   ├── online_eval.py       # 在线评估 (Langfuse traces → deepEval)
│   ├── offline_eval_rag.py  # RAG 离线评估
│   ├── offline_eval_agent.py# Agent 离线评估
│   └── datasets/            # 评估数据集 (agent_datasets.json / rag_datasets.json)
│
└── routes/
    ├── auth_routes.py       # 注册/登录
    ├── session_routes.py    # 会话管理
    ├── upload_routes.py     # 文件上传
    ├── document_routes.py   # 文档管理 (列表/搜索/删除/替换/SSE事件)
    └── admin_routes.py      # 管理接口 (模型状态/熔断器)
```

## 三条核心数据流

### 1. 聊天
```
POST /chat → SSE 流式响应
  → RateLimitMiddleware（Redis 滑动窗口限流）
  → GatewayMiddleware（健康感知路由，动态获取降级链）
  → Agent 中间件管线:
    ModelCallLimit(thread=100次/会话, run=15次/请求)
    → ModelFallback(DeepSeek→Qwen 降级)
    → ModelRetry(3次指数退避)
    → ToolCallLimit(全局50次, web_search单独20次)
    → ToolRetry(3次指数退避)
  → Triage DeepAgent: 理解意图 → 直接回答 / 委托 Executor
  → Executor DeepAgent: 自主规划 → 委托 Specialist → HITL 审批 → 汇报
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
- **Milvus RBAC 多租户**: 每个注册用户自动创建独立 Milvus 用户 + 角色 + Collection，密码加密存储，用户间物理隔离
- **节点去重**: `node_id = 文件哈希_内容哈希`，Milvus 写入幂等
- **LLM 自路由**: 参考 Claude Code / HermesAgent / OpenClaw，不设外部分类器，所有工具描述和 SubAgent 列表在 system prompt 中始终可见，LLM 通过 Function Calling 自行选择。SubAgent 元数据由 `discover_specialist_agents()` 动态生成并注入 prompt
- **智能模型网关**: 多模型注册 → 健康探活（30s间隔）→ 三态熔断（5次失败熔断/30s冷却）→ 自动降级 → 手动热切换。按角色分配模型（CHAT / FALLBACK_CHAT / RETRIEVAL_LLM / RETRIEVAL_REWRITER）
- **三层记忆模型**: Hot（当前轮, memory）→ Warm（会话级, store）→ Cold（跨会话, Milvus），通过 `TaskContextManager` 管理 journal 写入和快照
- **全链路追踪**: `TraceContext` 单例通过 contextvars 传播 trace_id（格式 `trace-{uuid16}/task-{task_id_short}`），`TraceIdFilter` 自动注入日志
- **三层 Agent 架构**: Triage（分流、简单问答、委托判断）→ Executor（后台长周期任务、规划、审批协调）→ Specialist（8个领域子Agent，各有专属 Moka API 工具）
- **配置方式**: 敏感配置在 `key.env`（不提交），通用配置在 `config.py`
- **行为准则**: 见 `.claude/CLAUDE.md`（先思考再编码 / 简洁优先 / 精准修改 / 目标驱动执行）

## 开发指南

```bash
# 新增 Specialist Agent
# 1. 在 app/subagents/ 下创建新目录
# 2. 编写 AGENT.md（YAML frontmatter: name, description, allowed_tools, 可选 output_schema）
#    AGENT.md 会被 discover_specialist_agents() 自动发现并注入 system prompt

# 新增 Agent 工具
# 1. 在 app/tools/{domain}.py 中定义 async 函数并加 @register_tool + @tool
# 2. 无需修改 main.py — 工具通过 TOOL_REGISTRY 自动注册

# 新增审批流程
# 1. 在 SubAgent 的 AGENT.md 中定义审批规则（如薪资分级）
# 2. SubAgent 调用 async_request_approval(title, approver_role, context)
# 3. Executor DeepAgent 检测 [HUMAN_APPROVAL_REQUIRED] 标记 → 调用 request_approval → interrupt 挂起
# 4. 人类通过 POST /tasks/{id}/resume 做出决策
# 详见 app/tools/approval.py、app/tools/request_approval.py 和 app/harness/task_executor.py

# 新增路由
# 在 app/routes/ 下创建 xxx_routes.py → main.py include_router

# 新增模型
# 1. 在 config.py 的 models 注册表中添加模型配置
# 2. 分配 roles（chat / fallback_chat / retrieval_llm / retrieval_rewriter）
# 3. 如需新 provider，在 main.py 的 _load_model_for_role() 中增加分支
```
