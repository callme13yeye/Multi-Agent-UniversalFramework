# TK-MultiAgent — 企业级多智能体协作平台

基于 **FastAPI + LangGraph + DeepAgents + LlamaIndex** 构建的企业级 Multi-Agent 智能协作平台，提供 RAG 知识库检索、多智能体自主编排、人审审批（HITL）、智能模型网关、知识图谱增强等完整能力。

## 核心特性

- **两层 DeepAgent 编排** — Triage（分流）→ Executor（后台执行），LLM 自主规划、委托 SubAgent、审批、汇报；支持 Tool/SubAgent 热加载，无需重启
- **Agent Hook 横切层** — Journal 执行日志、ApprovalGuard 审批兜底、Delegation 委托追踪，通过 Middleware Hook 注入，SubAgent 无需感知
- **企业知识库 RAG** — 自适应查询重写 × 多路并行检索 × RRF 融合 × bge-reranker 精排 × 动态裁剪
- **GraphRAG 知识图谱** — Neo4j 驱动的实体/关系抽取与图谱增强检索，从向量检索结果出发扩展图上下文
- **文档全生命周期** — 上传 → SHA-256 去重 → MinIO 存储 → 处理管线（MinerU 智能 PDF / PyMuPDF 降级）→ Milvus 向量索引，支持 PDF/Excel/Docx/Markdown/代码等多种格式
- **人审审批（HITL）** — Executor DeepAgent + LangGraph interrupt 挂起/恢复 + ApprovalGuard 3 轮兜底检测
- **智能模型网关** — 多模型注册 × 健康探活 × 三态熔断 × 按角色降级链 × 模型热切换 × 滑动窗口限流
- **全链路追踪** — contextvars trace_id 传播 + 日志注入 + Langfuse 在线评估
- **三层记忆与 Journal** — Hot（当前轮）→ Warm（会话级）→ Cold（跨会话 Milvus）+ 结构化执行日志（不受 Summarization 压缩影响）
- **死信队列** — 失败操作的可靠存储与后台重试（审批写入等关键操作）
- **多租户隔离** — Milvus RBAC，每用户独立 Collection，物理级数据隔离
- **优雅关闭** — 有序关闭：任务排空 → 死信扫描停止 → 热加载停止 → 网关探活停止 → 连接池关闭

## 技术栈

| 层级 | 技术 |
|------|------|
| 框架 | FastAPI + Uvicorn + SSE (sse-starlette) |
| Agent 编排 | LangGraph + DeepAgents + LangChain |
| RAG 引擎 | LlamaIndex + Milvus 2.6 |
| 知识图谱 | Neo4j 5.x + GraphRAG |
| 模型 | DeepSeek V4 Flash / Qwen Turbo / Qwen3-Embedding-0.6B / bge-reranker-v2-m3 |
| PDF 解析 | MinerU (pipeline) → PyMuPDF 降级 |
| 存储 | PostgreSQL 16（双库） + Redis + MinIO + Neo4j |
| 追踪 | Langfuse + OpenTelemetry |
| 评估 | deepEval（在线/离线） |
| 热加载 | watchfiles |

## 快速开始

### 环境要求

- Python 3.13.11
- [uv](https://docs.astral.sh/uv/) 包管理器
- PostgreSQL 16（需要两个数据库：`auth_db` + `auth_conversations_db`）
- Milvus 2.6+
- Neo4j 5.x（可选，知识图谱功能）
- Redis（可选，限流降级为内存模式）
- MinIO（文档对象存储）
- MinerU（可选，PDF 智能解析，关闭后降级 PyMuPDF）

### 1. 克隆并安装依赖

```bash
git clone <repo-url>
cd agentrag

# 使用 uv 安装依赖
uv sync
```

### 2. 下载本地模型

```bash
python download_model/download_model.py
```

模型下载到 `models/` 目录：
- `Qwen3-Embedding-0.6B` — 文本嵌入
- `bge-reranker-v2-m3` — 精排重排序

### 3. 启动外部服务

| 服务 | 默认端口 | 用途 |
|------|---------|------|
| PostgreSQL | 5433 | auth_db（用户/会话/文档元数据）+ conversations_db（LangGraph 状态存储） |
| Milvus | 19530 | 向量数据库，多租户 RBAC |
| Neo4j | 7687 | 知识图谱（非致命依赖） |
| Redis | 6380 | 缓存 + 滑动窗口限流（非致命依赖） |
| MinIO | 9002 | 文档对象存储 |

### 4. 配置环境变量

复制 `key.env` 并填入你的 API Key 和服务地址：

```bash
# 必需：模型 API Key
DEEPSEEK_API_KEY="sk-xxxxxxxx"
DEEPSEEK_BASE_URL="https://api.deepseek.com/v1/"

# 必需：数据库连接
AUTH_DB_URL="postgresql://admin:password@localhost:5433/auth_db"
CONVERSATIONS_DB_URL="postgresql://admin:password@localhost:5433/auth_conversations_db"

# 必需：Milvus
MILVUS_URL="http://localhost:19530"
MILVUS_ADMIN_TOKEN="root:Milvus"

# 必需：MinIO
MINIO_ENDPOINT="localhost:9002"
MINIO_ACCESS_KEY="minioadmin"
MINIO_SECRET_KEY="minioadmin"

# 推荐：JWT
JWT_SECRET_KEY="your-secret-key"

# 推荐：Langfuse 追踪
LANGFUSE_SECRET_KEY="sk-lf-xxxxxxxx"
LANGFUSE_PUBLIC_KEY="pk-lf-xxxxxxxx"
LANGFUSE_BASE_URL="http://localhost:3000"

# 可选：Redis（不配置则自动降级为内存模式）
REDIS_URL="redis://localhost:6380/0"

# 可选：Neo4j 知识图谱
NEO4J_URI="bolt://localhost:7687"
NEO4J_USERNAME="neo4j"
NEO4J_PASSWORD="neo4jadmin"

# 可选：MinerU PDF 智能解析
MINERU_MODEL_SOURCE="local"
```

> 完整的配置项说明见 [key.env](key.env)。

### 5. 启动服务

```bash
python main.py
```

服务启动后访问：
- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

## 架构总览

```
用户请求 → RateLimitMiddleware → CORS → 路由层
                                          │
    ┌─────────────────────────────────────┤
    │  Agent 中间件管线                     │
    │  Hook: Journal → ApprovalGuard       │
    │   → Delegation                       │
    │  Gateway: ModelFallback → ModelRetry │
    └─────────────────────────────────────┘
                                          │
                    ┌──────────────────────┘
                    ▼
        ┌───────────────────────┐
        │   Triage DeepAgent    │  ← 分流判断（持有全部工具）
        │   理解意图 / 直接回答   │
        │   Hook: Delegation     │
        └───────┬───────────────┘
                │ 委托后台任务
                ▼
        ┌───────────────────────┐
        │  Executor DeepAgent   │  ← 后台长周期执行
        │  自主规划 / 委托 / 审批 │
        │  Hook: Journal +       │
        │  ApprovalGuard +       │
        │  Delegation            │
        └───────┬───────────────┘
                │ 调用领域专家
                ▼
        ┌───────────────────────┐
        │  Specialist SubAgents │  ← 动态发现的领域专家
        │  通用工具 / RAG 检索    │
        └───────────────────────┘
```

### 五条核心数据流

**1. 聊天**

```
POST /chat → SSE 流式响应
  → 限流 → Agent 管线 → Triage 分流
  → 直接回答 / 委托 Executor → Specialist 执行
  → 流式输出 + SSE 事件(content/status/error/sources) + 引用溯源
```

**2. 文档上传与索引**

```
POST /upload → SHA-256 哈希去重 → MinIO 存储
  → 异步处理: 加载 → 清洗 → MinerU/PyMuPDF 解析 → 按文件类型分块
  → node_id = 文件哈希_内容哈希 (幂等去重)
  → 知识图谱实体/关系抽取 → Neo4j 写入
  → Milvus 写入 → SSE 状态推送
```

**3. 检索（含 GraphRAG）**

```
用户提问 → RetrievalPipeline:
  Step1: QueryRewriter — LLM 分类(FACTUAL/COMPLEX/SPECIFIC)并策略扩写
  Step2: MultiRecall + RRF — 多路并行检索 + 倒数排名融合
  Step3: GraphRAG — 从向量检索结果出发，Neo4j 图谱扩展上下文
  Step4: Rerank — bge-reranker-v2-m3 精排
  Step5: DynamicTopK — 按分数分布自动裁剪
  → 注入 LLM 上下文 + 引用溯源
```

**4. 人审审批（HITL）**

```
SubAgent 调用 request_approval → Executor 检测 [HUMAN_APPROVAL_REQUIRED]
  → LangGraph interrupt 挂起 → 事件总线发布 task.interrupted
  → 前端展示审批卡片 → 人类 POST /tasks/{id}/resume
  → Agent 从中断处恢复继续执行
  → ApprovalGuard Hook: 3 轮未审批自动超时处理
```

**5. 后台任务生命周期**

```
Triage 调用 create_background_task → TaskExecutor 创建 asyncio.Task
  → Executor DeepAgent 自主执行（规划/委托/审批/汇报）
  → Journal Hook 记录每步执行日志
  → 事件总线实时推送进度 (task.executing → task.completed/failed)
  → 结果写入 Store → Triage 下次对话自动注入
  → 服务器重启后自动恢复未完成任务
```

## API 端点

### 认证

| 端点 | 方法 | 用途 |
|------|------|------|
| `/auth/register` | POST | 注册（自动创建 Milvus 租户） |
| `/auth/login` | POST | 登录（返回 JWT） |
| `/auth/login-form` | POST | 表单登录（OAuth2 兼容） |
| `/auth/me` | GET | 当前用户信息 |

### 对话

| 端点 | 方法 | 用途 |
|------|------|------|
| `/chat` | POST | SSE 流式聊天（唯一对话入口，需认证） |
| `/sessions` | GET | 会话列表 |
| `/sessions` | POST | 创建新会话 |
| `/sessions/{id}` | DELETE | 删除会话 |
| `/sessions/{id}/rename` | PATCH | 重命名会话 |
| `/sessions/{id}/messages` | GET | 历史消息 |
| `/sources/{session_id}` | GET | 检索来源文件列表 |
| `/feedback` | POST | 点赞/点踩反馈 |

### 任务与审批

| 端点 | 方法 | 用途 |
|------|------|------|
| `/tasks` | GET | 任务列表（支持状态筛选） |
| `/tasks/{id}` | GET | 任务详情（含审批状态/进度/plan） |
| `/tasks/{id}/resume` | POST | 人审决策（approved / rejected / provide_info） |
| `/tasks/{id}/events` | GET | 任务状态 SSE 实时推送 |
| `/tasks/{id}` | DELETE | 取消/删除任务 |
| `/tasks/{id}/journal` | GET | 任务执行日志（结构化记录，不受压缩影响） |

### 文档管理

| 端点 | 方法 | 用途 |
|------|------|------|
| `/upload` | POST | 文件上传（→ MinIO → 处理管线 → Milvus + Neo4j） |
| `/documents` | GET | 文档列表（分页 + 搜索） |
| `/documents/{id}` | DELETE | 删除文档 |
| `/documents/{id}/file` | GET | 查看文件内容 |
| `/documents/events` | GET | 文档处理状态 SSE 推送 |
| `/documents/{id}/replace` | PUT | 替换文档 |

### 管理

| 端点 | 方法 | 用途 |
|------|------|------|
| `/admin/models` | GET | 查询所有模型状态（需 X-Admin-Token） |
| `/admin/models/{name}/activate` | PUT | 手动激活模型 |
| `/admin/models/{name}/circuit` | PUT | 手动重置熔断器 |

## 项目结构

```
agentrag/
├── main.py                    # FastAPI 入口，lifespan 初始化全量资源 + 优雅关闭
├── config.py                  # 通用配置（模型注册/熔断/限流/超时/图谱/MinerU）
├── key.env                    # 敏感配置（API Key/数据库连接，不提交）
├── pyproject.toml             # 项目依赖（uv 管理）
│
├── app/
│   ├── agents/                # Agent 工厂层
│   │   ├── async_create_agent.py      # Agent 工厂 — 组装 deepagents 中间件管线
│   │   ├── agent_definitions.py       # Specialist SubAgent 发现 + Router Prompt
│   │   └── async_ensure_user_skills_init.py  # 用户技能初始化
│   │
│   ├── hooks/                 # Agent Hook 横切层（新）
│   │   ├── types.py           # HookDependencies / HookRole 类型定义
│   │   ├── factory.py         # assemble_hooks — 按角色组装 Hook 列表
│   │   ├── journal_hook.py    # Journal Hook — 记录执行日志到 Store
│   │   ├── approval_guard_hook.py  # ApprovalGuard — 3 轮未审批自动超时
│   │   └── delegation_hook.py # Delegation Hook — 委托事件追踪
│   │
│   ├── harness/               # 后台任务执行环境
│   │   ├── event_bus.py       # 事件总线（Agent 间通信 + SSE 推送）
│   │   ├── task_executor.py   # 后台任务执行器 + HITL 审批兜底 + 优雅关闭
│   │   ├── dead_letter.py     # 死信队列（失败操作重试/归档）
│   │   ├── task_context.py    # 三层记忆(Hot/Warm/Cold) + Journal + 快照
│   │   ├── trace_context.py   # 全链路 trace_id 传播 + 日志注入
│   │   ├── status_handler.py  # Agent 运行时状态 → SSE 事件
│   │   ├── tool_hot_reloader.py     # Tool 热加载器（watchfiles）
│   │   └── subagent_hot_reloader.py # SubAgent 热加载器（watchfiles）
│   │
│   ├── tools/                 # 工具注册中心（自动发现 + 热加载）
│   │   ├── _registry.py       # TOOL_REGISTRY + @register_tool 装饰器
│   │   ├── resources.py       # 非工具基础设施（知识库资源引用/缓存 Key）
│   │   ├── common.py          # 通用工具（时间/联网搜索）
│   │   ├── knowledge.py       # 知识库 RAG 检索
│   │   ├── graph_query.py     # 知识图谱查询工具
│   │   ├── task.py            # 后台任务创建
│   │   ├── task_query.py      # 任务状态查询
│   │   ├── approval.py        # 审批工具
│   │   ├── request_approval.py # 审批请求（触发 HITL interrupt）
│   │   └── read_journal.py    # 任务执行日志读取
│   │
│   ├── documents/             # 文档处理管线
│   │   ├── retrieval.py       # 统一检索管道（QueryRewriter + MultiRecall + RRF + Rerank + DynamicTopK）
│   │   ├── index_manager.py   # 索引管理器 — 文档生命周期编排
│   │   ├── document_processor.py  # 文档处理管线（加载/清洗/解析/分块）
│   │   ├── document_status.py # 文档处理状态管理
│   │   ├── document_event_bus.py  # 文档事件总线
│   │   ├── node_parser_factory.py # 按文件类型选择 Node Parser
│   │   └── datacleaning.py    # 数据清洗
│   │
│   ├── stores/                # 外部存储管理层（单例模式）
│   │   ├── pg_database.py     # PostgreSQL 双库管理器（auth + conversations）
│   │   ├── milvus_manager.py  # Milvus RBAC 多租户管理
│   │   ├── neo4j_manager.py   # Neo4j 连接管理
│   │   └── redis_manager.py   # Redis 缓存管理（含降级逻辑）
│   │
│   ├── gateway/               # 智能模型网关
│   │   ├── types.py           # ModelRole / ModelSpec 类型
│   │   ├── model_gateway.py   # 模型注册/路由/健康跟踪
│   │   ├── gateway_middleware.py  # 网关中间件（降级/重试）
│   │   ├── circuit_breaker.py # 三态熔断器
│   │   ├── health_probe.py    # 后台定期探活
│   │   └── rate_limit_middleware.py # Redis 滑动窗口限流
│   │
│   ├── readers/               # 文档解析器
│   │   └── mineru_reader.py   # MinerU PDF 智能解析引擎
│   │
│   ├── routes/                # API 路由层
│   │   ├── auth_routes.py     # 认证（注册/登录）
│   │   ├── chat_routes.py     # SSE 聊天 + 来源 + 反馈
│   │   ├── session_routes.py  # 会话管理
│   │   ├── upload_routes.py   # 文件上传
│   │   ├── document_routes.py # 文档管理
│   │   ├── task_routes.py     # 任务管理 + 审批 + Journal
│   │   └── admin_routes.py    # 管理端点
│   │
│   ├── prompts/               # Agent System Prompt
│   │   ├── triage_prompt.py   # Triage 分流 Prompt
│   │   └── executor_prompt.py # Executor 后台执行 Prompt
│   │
│   ├── schemas/               # SubAgent 输出 Schema 注册
│   │   └── registry.py        # Schema 注册中心
│   │
│   ├── utils/                 # 工具函数
│   │   ├── jwt.py             # JWT 令牌管理
│   │   ├── argon2id.py        # Argon2id 密码哈希
│   │   ├── file_hash.py       # SHA-256 文件哈希
│   │   └── credentials_encryption_decrypt.py  # Milvus 凭证加解密
│   │
│   ├── subagents/             # Specialist Sub-Agent 定义（AGENT.md 自动发现）
│   ├── evaluation/            # 在线/离线评估（deepEval + Langfuse）
│   │
│   ├── knowledge_graph.py     # 知识图谱服务（实体抽取/关系构建/GraphRAG）
│   ├── async_load_model.py    # 模型加载器（多 provider 统一接口）
│   ├── async_get_index.py     # Milvus 索引获取
│   ├── auth.py                # 认证逻辑
│   └── pydantic_models.py     # Pydantic 数据模型
│
├── models/                    # 本地模型文件
├── download_model/            # 模型下载脚本
└── docs/                      # 设计文档
    └── agent-design.md        # Agent 架构设计文档
```

## 开发指南

### 新增 Specialist SubAgent

1. 在 `app/subagents/` 下创建新目录
2. 编写 `AGENT.md`（YAML frontmatter: `name`, `description`, `allowed_tools`，可选 `output_schema`）
3. SubAgent 会被 `discover_specialist_agents()` 自动发现并注入 system prompt
4. 无需重启 — SubAgentHotReloader 自动检测变更并重建 Agent

### 新增 Agent 工具

```python
# 在 app/tools/{domain}.py 中定义
from app.tools._registry import register_tool
from langchain_core.tools import tool

@register_tool
@tool
async def my_tool(param: str) -> str:
    """工具描述，LLM 据此决定是否调用"""
    return f"结果: {param}"
```

工具通过 `TOOL_REGISTRY` 自动注册，无需修改 `main.py`。ToolHotReloader 自动检测文件变更并重建 Agent。

### 新增 Agent Hook

```python
# 在 app/hooks/ 下创建新 hook 模块
from app.hooks.types import HookDependencies

def create_my_hook(deps: HookDependencies):
    # 返回 AgentMiddleware 实例
    ...

# 在 app/hooks/factory.py 的 assemble_hooks() 中注册
```

Hook 在 Agent 的 middleware 管线中运行，SubAgent 无需任何改动。

### 新增审批流程

1. 在 SubAgent 的 `AGENT.md` 中定义审批规则
2. SubAgent 调用 `request_approval(title, approver_role, context)`
3. Executor 检测 `[HUMAN_APPROVAL_REQUIRED]` 标记 → LangGraph interrupt 挂起
4. ApprovalGuard Hook 提供 3 轮兜底检测
5. 人类通过 `POST /tasks/{id}/resume` 做出决策

详见 [app/tools/approval.py](app/tools/approval.py)、[app/tools/request_approval.py](app/tools/request_approval.py) 和 [app/harness/task_executor.py](app/harness/task_executor.py)。

### 运行评估

```bash
# 在线评估 — 从 Langfuse 拉取 traces
python -m app.evaluation.online_eval --since 24h --limit 100

# RAG 离线评估
python -m app.evaluation.offline_eval_rag

# Agent 离线评估
python -m app.evaluation.offline_eval_agent
```

## 关键设计决策

- **LLM 自路由** — 不设外部分类器，所有工具描述和 SubAgent 列表在 system prompt 中始终可见，LLM 通过 Function Calling 自行选择
- **双数据库读写分离** — auth_db（asyncpg）存用户/会话/文档元数据；conversations_db（psycopg AsyncConnectionPool）存 LangGraph 状态
- **智能模型网关** — 按角色分配模型（CHAT / FALLBACK_CHAT / RETRIEVAL_LLM / RETRIEVAL_REWRITER），健康探活（30s 间隔），三态熔断（5 次失败熔断 / 30s 冷却），按降级链自动切换
- **Hook 横切层** — 横切关注点（日志/审批兜底/委托追踪）通过 Middleware Hook 注入 Agent 管线，SubAgent 零感知
- **Tool/SubAgent 热加载** — watchfiles 监听目录变更，3s 防抖窗口，原子替换 Agent 实例，失败则回滚
- **节点去重** — `node_id = 文件哈希_内容哈希`，Milvus 写入幂等
- **配置分离** — 敏感配置在 `key.env`（不提交），通用配置在 `config.py`
- **非致命依赖降级** — Redis、Neo4j、MinerU 不可用时自动降级，不影响核心功能
- **死信队列** — 关键操作（审批写入）失败时写入死信队列，后台扫描器每 2 分钟重试

## License

Internal use.
