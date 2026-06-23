# TK-MultiAgent — 企业级多智能体协作系统

基于 **FastAPI + LlamaIndex + LangGraph + DeepAgents** 构建的企业级 Multi-Agent 智能协作平台，提供 RAG 知识库检索、多智能体自主编排、人审审批、智能模型网关等完整能力。

## 核心特性

- **三层 DeepAgent 编排** — Triage（分流）→ Executor（后台执行）→ Specialist Sub-Agent（8 个领域专家），LLM 自主规划、委托、审批、汇报
- **企业知识库 RAG** — 自适应重查 × 多路检索 × RRF 融合 × bge-reranker 精排 × 动态裁剪
- **文档全生命周期** — 上传 → MinIO 存储 → 处理管线（清洗/解析/分块）→ Milvus 向量索引，支持 PDF/Excel/Docx/代码等多种格式
- **人审审批（HITL）** — Executor DeepAgent + interrupt 挂起/恢复 + 3 轮兜底检测，支持薪资分级等业务规则
- **智能模型网关** — 多模型注册 × 健康探活 × 三态熔断 × 自动降级 × 模型热切换 × 滑动窗口限流
- **全链路追踪** — contextvars trace_id 传播 + 日志注入 + Langfuse 在线评估
- **三层记忆模型** — Hot（当前轮）→ Warm（会话级）→ Cold（跨会话 Milvus）
- **多租户隔离** — Milvus RBAC，每用户独立 Collection，物理级数据隔离

## 技术栈

| 层级 | 技术 |
|------|------|
| 框架 | FastAPI + Uvicorn |
| Agent 编排 | LangGraph + DeepAgents |
| RAG 引擎 | LlamaIndex + Milvus 2.6 |
| 模型 | DeepSeek V4 Flash / Qwen Turbo / Qwen3-Embedding / bge-reranker-v2-m3 |
| 存储 | PostgreSQL 16（双库） + Redis + MinIO |
| 追踪 | Langfuse + OpenTelemetry |
| 评估 | deepEval（在线/离线） |

## 快速开始

### 环境要求

- Python 3.13.11
- [uv](https://docs.astral.sh/uv/) 包管理器
- PostgreSQL 16（需要两个数据库）
- Milvus 2.6+
- Redis（可选，限流降级为内存模式）
- MinIO（文档存储）

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

模型会下载到 `models/` 目录：
- `Qwen3-Embedding-0.6B` — 文本嵌入
- `bge-reranker-v2-m3` — 精排重排序

### 3. 启动外部服务

确保以下服务已启动：

| 服务 | 默认端口 | 用途 |
|------|---------|------|
| PostgreSQL | 5433 | auth_db（用户/会话/文档元数据）+ conversations_db（LangGraph 状态存储） |
| Milvus | 19530 | 向量数据库，多租户 RBAC |
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
用户请求 → RateLimitMiddleware → GatewayMiddleware → Agent 中间件管线
                                                          │
    ┌─────────────────────────────────────────────────────┐
    │  Agent 中间件管线                                     │
    │  ModelCallLimit → ModelFallback → ModelRetry         │
    │  → ToolCallLimit → ToolRetry                         │
    └─────────────────────────────────────────────────────┘
                                                          │
                    ┌──────────────────────┘
                    ▼
        ┌───────────────────────┐
        │   Triage DeepAgent    │  ← 分流判断
        │   理解意图 / 直接回答   │
        └───────┬───────────────┘
                │ 委托后台任务
                ▼
        ┌───────────────────────┐
        │  Executor DeepAgent   │  ← 后台长周期执行
        │  自主规划 / 委托 / 审批 │
        └───────┬───────────────┘
                │ 调用领域专家
                ▼
        ┌───────────────────────┐
        │  Specialist SubAgents │  ← 8 个领域专家
        │  Moka API / RAG 工具   │
        └───────────────────────┘
```

### 三条核心数据流

**1. 聊天**

```
POST /chat → SSE 流式响应
  → 限流 → 网关路由 → Agent 管线 → Triage 分流
  → 直接回答 / 委托 Executor → Specialist 执行
  → 流式输出 + SSE 事件 + 引用溯源
```

**2. 文档上传与索引**

```
POST /upload → SHA-256 哈希去重 → MinIO 存储
  → 异步处理: 加载 → 清洗 → 解析(按文件类型) → 分块
  → node_id = 文件哈希_内容哈希 (幂等去重)
  → Milvus 写入 → SSE 状态推送
```

**3. 检索**

```
用户提问 → RetrievalPipeline:
  Step1: QueryRewriter — LLM 分类并扩写
  Step2: MultiRecall + RRF — 多路并行检索 + 倒数排名融合
  Step3: Rerank — bge-reranker-v2-m3 精排
  Step4: DynamicTopK — 按分数分布自动裁剪
  → 注入 LLM 上下文 + 引用溯源
```

## API 端点

### 认证

| 端点 | 方法 | 用途 |
|------|------|------|
| `/auth/register` | POST | 注册（自动创建 Milvus 租户） |
| `/auth/login` | POST | 登录（返回 JWT） |
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
| `/tasks/{id}` | GET | 任务详情（含审批状态/进度） |
| `/tasks/{id}/resume` | POST | 人审决策（approved / rejected） |
| `/tasks/{id}/events` | GET | 任务状态 SSE 实时推送 |
| `/tasks/{id}` | DELETE | 取消/删除任务 |

### 文档管理

| 端点 | 方法 | 用途 |
|------|------|------|
| `/upload` | POST | 文件上传（→ MinIO → 处理管线 → Milvus） |
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
├── main.py                    # FastAPI 入口，lifespan 初始化所有资源
├── config.py                  # 通用配置（模型注册表/熔断/限流/超时）
├── key.env                    # 敏感配置（API Key/数据库连接，不提交）
├── pyproject.toml             # 项目依赖
│
├── app/
│   ├── async_create_agent.py  # Agent 工厂 — 组装 deepagents 中间件管线
│   ├── agent_definitions.py   # Specialist SubAgent 发现 + Router Prompt
│   ├── retrieval.py           # 统一检索管道
│   ├── index_manager.py       # 索引管理器 — 文档生命周期编排
│   ├── document_processor.py  # 文档处理管线
│   ├── node_parser_factory.py # 按文件类型选择 Node Parser
│   ├── milvus_manager.py      # Milvus RBAC 多租户管理
│   ├── pg_database.py         # 双数据库管理器
│   ├── task_context.py        # 任务上下文管理器（三层记忆）
│   ├── trace_context.py       # 全链路 trace_id 传播
│   ├── status_handler.py      # Agent 运行时状态回调
│   │
│   ├── subagents/             # Specialist Sub-Agent 定义（8 个领域专家）
│   │   ├── general/           # 通用助手
│   │   ├── recruitment_*/     # 招聘领域专家（简历/面试/Offer/人才/职位/分析/审批）
│   │
│   ├── tools/                 # 工具注册中心（16 个工具）
│   │   ├── _registry.py       # TOOL_REGISTRY + @register_tool 装饰器
│   │   ├── common.py          # 通用工具（时间/搜索）
│   │   ├── knowledge.py       # 知识库 RAG 检索
│   │   ├── task.py            # 后台任务创建
│   │   ├── approval.py        # 审批工具
│   │   └── moka_*.py          # Moka API 封装
│   │
│   ├── prompts/               # Triage / Executor system prompt
│   ├── schemas/               # SubAgent 输出 Schema 管理
│   │
│   ├── gateway/               # 智能模型网关
│   │   ├── model_gateway.py   # 模型注册/路由/健康跟踪
│   │   ├── circuit_breaker.py # 三态熔断器
│   │   ├── health_probe.py    # 后台定期探活
│   │   └── rate_limit_middleware.py # Redis 滑动窗口限流
│   │
│   ├── harness/               # 后台任务执行引擎
│   │   ├── event_bus.py       # 事件总线
│   │   ├── task_executor.py   # HITL 审批兜底/熔断
│   │   └── dead_letter.py     # 死信队列
│   │
│   ├── evaluation/            # 在线/离线评估
│   ├── routes/                # API 路由
│   └── evolution/             # 自进化系统
│
├── models/                    # 本地模型文件
└── download_model/            # 模型下载脚本
```

## 开发指南

### 新增 Specialist Agent

1. 在 `app/subagents/` 下创建新目录
2. 编写 `AGENT.md`（YAML frontmatter: name, description, allowed_tools, 可选 output_schema），会被 `discover_specialist_agents()` 自动发现并注入 system prompt

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

无需修改 `main.py`，工具通过 `TOOL_REGISTRY` 自动注册。

### 新增审批流程

1. 在 SubAgent 的 `AGENT.md` 中定义审批规则（如薪资分级）
2. SubAgent 调用 `async_request_approval(title, approver_role, context)`
3. Executor DeepAgent 检测 `[HUMAN_APPROVAL_REQUIRED]` 标记 → interrupt 挂起
4. 人类通过 `POST /tasks/{id}/resume` 做出决策

详见 `app/tools/approval.py`、`app/tools/request_approval.py` 和 `app/harness/task_executor.py`。

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
- **双数据库读写分离** — auth_db（asyncpg）存用户/会话/文档；conversations_db（psycopg AsyncConnectionPool）存 LangGraph 状态
- **智能模型网关** — 按角色分配模型（CHAT / FALLBACK_CHAT / RETRIEVAL_LLM / RETRIEVAL_REWRITER），健康探活（30s 间隔），三态熔断（5 次失败熔断 / 30s 冷却）
- **节点去重** — `node_id = 文件哈希_内容哈希`，Milvus 写入幂等
- **配置分离** — 敏感配置在 `key.env`（不提交），通用配置在 `config.py`

## License

Internal use.
