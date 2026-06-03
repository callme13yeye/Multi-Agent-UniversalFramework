# Agent设计思路
入口层（FastAPI）→ 编排层（DeepAgents中间件管线）→ 模型层（ModelGateway）→ 工具层（async_tools）→ 记忆层（LangGraph Store + Redis）→ 观测层（Langfuse）
---

## 一、多模型架构设计 — ✅ app/gateway/

### 1. 智能模型网关（ModelGateway）

**代码位置：** `app/gateway/model_gateway.py`

ModelGateway 是中心单例，管理所有模型的生命周期。所有 Agent 组件不直接依赖某个模型厂商，而是统一通过 Gateway 获取模型链（get_model_chain）。

- **Adapter 适配器** — `app/async_load_model.py` 中为每个 Provider 实现统一加载接口：
  - DeepSeek → `ChatDeepSeek`（LangChain 接口）+ `DeepSeek`（LlamaIndex 接口）
  - Bailian（阿里百炼/Qwen）→ `init_chat_model(model_provider="openai")`（LangChain 接口）+ `OpenAILike`（LlamaIndex 接口）
  - 本地模型（Embedding + Rerank）→ `HuggingFaceEmbedding` + `SentenceTransformerRerank`（线程池加载，非阻塞事件循环）
  - 同一模型可注册 LangChain + LlamaIndex 双接口，按角色自动选择（`ModelRole.interface` 属性）

- **Model Router（模型路由）** — `app/gateway/gateway_middleware.py` 中的 `GatewayMiddleware`：
  - 每次模型调用从 Gateway 获取健康排序的模型链（`get_model_chain`）
  - 按角色选择：`ModelRole.CHAT`（Agent对话）、`FALLBACK_CHAT`（备用）、`RETRIEVAL_LLM`（检索回答生成）、`RETRIEVAL_REWRITER`（Query改写）
  - 自动跳过已熔断（OPEN）的模型，逐个尝试直到成功
  - 兜底策略：即使活跃模型已熔断，也将其加入链尾（宁可失败也不错失）

- **熔断器（CircuitBreaker）** — `app/gateway/circuit_breaker.py`：
  - 标准三态：CLOSED → OPEN（连续失败 ≥5 次）→ HALF_OPEN（冷却 30s）→ CLOSED（探测成功）
  - 每模型独立熔断器，asyncio.Lock 保证状态转换安全

- **健康探活（HealthProbe）** — `app/gateway/health_probe.py`：
  - 后台定期（默认 30s）对所有注册模型发送轻量 ping
  - 自动恢复 HALF_OPEN 状态的熔断器

### 2. 模型热切换 — ✅ Admin API + Gateway

**代码位置：** `app/admin_routes.py` + `app/gateway/model_gateway.py:set_active_model()`

- 通过 `PUT /admin/models/{name}/activate?role=chat` 零停机热切换
- `set_active_model` 原子替换角色对应的活跃模型引用，进行中请求不受影响，下一个请求使用新模型
- 切换时自动重置目标模型的熔断器
- 缓存失效：`AsyncLoadModel.invalidate()` 清除类级缓存，下次请求重新加载

**当前局限（多租户配置化）：** ⬜ 每个用户独立模型配置（如不同模型名称/参数/超时）尚未实现。目前是多租户共享同一套模型配置（在 `config.py` 中定义），Gateway 按角色区分，不按用户区分。

### 3. 多租户隔离设计 — ✅ Milvus RBAC + 双数据库

**代码位置：** `app/milvus_manager.py`、`app/pg_database.py`

- **配置隔离：** ✅ 每个用户注册时自动创建独立的 Milvus 用户 + 角色 + Collection（`provision_user`），RBAC 最小权限集分配
- **资源隔离：** ✅ 双数据库架构 — auth_db（用户/会话/文档元数据）+ conversations_db（LangGraph checkpointer/store），各自独立连接池
- **限流与并发控制：** 🔄 目前仅 Agent 层有全局限制：
  - `ModelCallLimitMiddleware`（thread_limit=100/会话, run_limit=15/请求）
  - `ToolCallLimitMiddleware`（全局50次, web_search单独20次/5次）
  - 按用户级别或租户级别的精细化限流暂未实现

---

## 二、Agent记忆设计 — ✅ 三层记忆架构

**代码位置：** `app/async_create_agent.py` + `app/pg_database.py` + `app/redis_manager.py`

### 1. 短期记忆（对话上下文）
- **实现：** LangGraph Checkpointer（`AsyncPostgresSaver`），基于 conversations_db
- 每次 Agent 调用 `astream` 时传入 `thread_id=session_id`，LangGraph 自动管理消息历史
- 支持断点续传（`aget_state`、`update_state`），会话级别的上下文窗口

### 2. 长期记忆（跨会话）
- **实现：** LangGraph Store（`AsyncPostgresStore`），同样基于 conversations_db
- 记忆命名空间：`/memories/AGENTS.md`（用户级全局准则）
- 通过 `ensure_user_skills_init` 初始化每个用户的记忆文件
- `CompositeBackend` 统一路由：StoreBackend 存储用户记忆，FilesystemBackend 加载系统技能

### 3. 工作记忆（Run-scoped Context）
- **实现：** Pydantic `ChatContext`（`app/pydantic_models.py`）
- 包含 `user_id` + `session_id`，通过 `context_schema` 参数注入 Agent
- 跨中间件共享，用于工具调用中获取当前用户上下文

### 4. 缓存策略 — ✅ Redis
- **代码位置：** `app/redis_manager.py`
- 问答缓存：`qa:{question_hash}:{user_id}`，命中直接返回跳过检索+生成
- 热点缓存：命中次数 ≥5 自动延长 TTL（1h → 2h）
- 引用来源缓存：`sources:{user_id}:{session_id}`，跨进程共享供前端展示
- 待确认 Q&A 缓存：`pending_qa:{user_id}:{session_id}`，用户点赞后才写入长期缓存
- **非致命组件：** Redis 不可用时服务降级，不影响核心功能

---

## 三、Agent调用工具 — ✅ app/async_tools.py

**代码位置：** `app/async_tools.py`

当前已注册 4 个核心工具，通过 `@tool` 装饰器 + LangChain Tool 接口：

| 工具函数 | 功能 | 关联系统 |
|---------|------|---------|
| `async_get_current_time` | 获取当前北京时间 | 无外部依赖 |
| `async_web_search` | 联网搜索（Tavily API） | Tavily |
| `async_knowledge_query_ask` | 企业知识库 RAG 查询 | RetrievalPipeline + Milvus + Redis |
| `async_create_reimbursement_ticket` | 创建报销审批工单 | WorkflowEngine |

**关键设计：**
- `async_knowledge_query_ask` 是核心工具，内部执行完整 RetrievalPipeline（自适应重查→多路检索→RRF融合→精排→动态裁剪→LLM生成回答）
- 相关性兜底：bge-reranker 分数低于 0.70 视为"无相关内容"
- 工具权限隔离：Specialist Agent 通过 AGENT.md 的 `allowed_tools` 精确分配工具集
- 所有工具通过 `tools_map` 注册，供 `discover_specialist_agents` 按需分配

---

## 四、Agent状态机设计 — ✅ WorkflowEngine（LangGraph）

**代码位置：** `app/workflow/`（engine.py, reimbursement.py, models.py）

**工作流状态机（报销审批示例）：**

```
START → validate
  → (校验失败) END [status: rejected]
  → manager_approve [INTERRUPT]
    → (拒绝) END [status: rejected]
    → (≤1000元) process_payment → END [status: paid]
    → (>1000元) finance_review [INTERRUPT]
      → (拒绝) END
      → (>5000元) ceo_approve [INTERRUPT] → (拒绝) END
      → (≤5000元 / CEO通过) process_payment → END [status: paid]
```

**已实现的状态：**
- `pending` — 已创建，等待审批
- `approved` — 已通过
- `rejected` — 已拒绝
- `paid` — 已打款（终态）

**人机交互（Human-in-the-Loop）：**
- 使用 LangGraph 的 `interrupt()` 机制在每个审批节点中断
- 通过 `WorkflowEngine.take_action()` 恢复执行
- `pending_notification` 包含当前待办步骤和角色信息

**暂未实现的状态（设计中有，代码未实现）：** ⬜
- 执行中/等待模型/知识检索中/工具调用中/等待工具结果 — Agent 层由 DeepAgents 框架管理，未显式暴露给业务层
- 暂停中/已恢复/用户取消/执行超时/规划中/反思中/重试中 — 均未显式建模

---

## 五、Agent编排 — ✅ DeepAgents 中间件管线

**代码位置：** `app/async_create_agent.py`

基于 `deepagents.create_deep_agent` 组装完整的中间件管线，分三层：

**基础栈（create_deep_agent 自动处理）：**
1. TodoList
2. Skills（元数据注入 system prompt）
3. Filesystem（技能文件访问）
4. SubAgent（sub-agent 编排）
5. Summarization（上下文压缩）
6. PatchToolCalls

**领域定制栈（async_create_agent 注入）：**
1. **SkillsMiddleware** — 技能元数据动态注入 system prompt
2. **ModelCallLimitMiddleware** — 线程级 100 次/会话，运行级 15 次/请求
3. **GatewayMiddleware** — 健康感知模型路由（替代原 ModelFallbackMiddleware）
4. **ModelRetryMiddleware** — 模型调用快速重试（1次兜底，0.5s初始延迟）
5. **ToolCallLimitMiddleware** — 全局 50 次，web_search 单独限流 20次/会话、5次/请求
6. **ToolRetryMiddleware** — 工具调用重试（3次，指数退避 1s→2s→4s）

**尾部栈（create_deep_agent 自动追加）：**
1. Memory（记忆管理）
2. HumanInTheLoop（人工介入）

**CompositeBackend 统一后端路由：**
- `/memories/` → StoreBackend（用户记忆，按 user_id 命名空间隔离）
- `/skills/built-in/` → FilesystemBackend（文件系统技能加载）
- `/skills/` → StoreBackend（用户自定义技能）

---

## 六、Agent Skills设计 — ✅ 文件系统 + 动态加载

**代码位置：** `app/skills/` + `app/agent_definitions.py`

### 组织结构

```
app/skills/
├── business/
│   ├── SKILL.md      — 技能定义（name, description, allowed_tools）
│   └── AGENT.md      — SubAgent 角色定义（name, description, system_prompt, allowed_tools）
├── engineering/
├── finance/
└── hr/
```

### 四个已注册的 Specialist Agent

| 技能 | Agent 名称 | 职责 | 特有工具 |
|------|-----------|------|---------|
| `business` | `business_specialist` | 销售/市场/客户/合同 | knowledge + web_search + time |
| `engineering` | `engineering_specialist` | 代码/架构/技术选型/部署 | knowledge + web_search + time |
| `finance` | `finance_specialist` | 报销/会计/预算/税务 | **+ create_reimbursement_ticket** |
| `hr` | `hr_specialist` | 招聘/薪酬/福利/考勤 | knowledge + web_search + time |

### 自动发现机制
- `discover_specialist_agents()` 扫描 `app/skills/*/AGENT.md`
- 简易 YAML frontmatter 解析（name, description, allowed_tools）
- 按 `allowed_tools` 精确分配工具集，减少 token 占用
- 无 `allowed_tools` 声明或未匹配到工具的，继承父 Agent 全部工具

### 用户自定义技能 ⬜
- 架构上预留了 `/skills/` → StoreBackend，支持运行时用户自定义技能写入
- 但 `ensure_user_skills_init` 目前仅写入全局准则，未实现用户自定义技能的管理界面或 API

---

## 七、Agent完整链路规划执行 — ✅ 编排闭环 + 🔄 人工兜底

### 聊天完整链路

```
用户输入 → POST /chat
  → [FastAPI] 验证 JWT / 获取用户 + 会话
  → [PG] ensure_user_skills_init（初始化用户记忆）
  → [Langfuse] CallbackHandler（链路追踪）
  → [DeepAgents] agent.astream()
    ├── SkillsMiddleware → 注入技能元数据到 system prompt
    ├── ModelCallLimitMiddleware → 检查配额
    ├── GatewayMiddleware → 获取健康模型链
    ├── ModelRetryMiddleware → 失败重试
    ├── ToolCallLimitMiddleware → 限流
    ├── ToolRetryMiddleware → 工具失败重试
    ├── [LLM 自路由] → Function Calling 选择工具或 SubAgent
    │   ├── → async_knowledge_query_ask → RetrievalPipeline（重查→检索→RRF→精排→裁剪→生成）
    │   ├── → async_web_search → Tavily API
    │   ├── → async_create_reimbursement_ticket → WorkflowEngine
    │   └── → task 工具 → Specialist SubAgent
    └── Memory + Summarization → 上下文管理
  → SSE 流式输出 + 引用来源事件
  → 用户反馈（点赞/点踩）→ 缓存策略
```

### 规划与反思闭环 🔄
- 当前 Agent 完全依赖 LLM 自路由（Function Calling），没有显式的 "规划→执行→反思" 循环
- DeepAgents 框架内部可能有反思机制，但项目中未显式配置
- 工作流的 `interrupt()` 机制提供了人工审核节点，但仅限于审批流程

### 人工兜底方式
- **工作流中断：** `interrupt()` 等待人工审批，通过 `take_action(approved/rejected)` 恢复
- **系统降级：** 主模型不可用 → 自动 fallback 到备用模型（DeepSeek → Qwen）
- **熔断保护：** 连续失败自动熔断，后台探活自动恢复
- **知识库无结果：** Agent 必须告知用户"未找到相关内容"，禁止编造答案

---

## 八、Multi Agent 设计 — ✅ Router/Supervisor 架构

**代码位置：** `app/agent_definitions.py` + `app/async_create_agent.py:subagents参数`

### 架构模式

采用 **Router/Supervisor → Specialist Sub-Agent** 模式（参考 Claude Code / HermesAgent / OpenClaw）：

```
用户输入
  │
  ▼
Router Agent（主Agent）
  │  system prompt 始终可见所有 Specialist Agent 列表
  │  通过 Function Calling 自主选择
  │
  ├── 自己回答问题（使用通用工具）
  ├── task → business_specialist  （业务相关问题）
  ├── task → engineering_specialist（技术相关问题）
  ├── task → finance_specialist   （财务相关问题）
  └── task → hr_specialist        （人力资源问题）
```

### 边界能力

| 设计原则 | 当前实现 |
|---------|---------|
| 专业Agent只关注自身领域 | ✅ 每个 Agent 有独立的 system_prompt 和边界约束（AGENT.md） |
| 提示词权限隔离 | ✅ 各自 system prompt 互不干扰 |
| 工具权限精确分配 | ✅ allowed_tools 机制，细到每个工具 |
| 适用场景明确 | ✅ 4 个领域覆盖常见企业场景 |
| 不过度设计 | ✅ 都通过 Router 统一管理，每请求最多派发一个 SubAgent（非链式调用） |

### 关键决策
- **LLM 自路由**（不设外部分类器）：所有工具描述和 SubAgent 列表在 system prompt 中始终可见，LLM 通过 Function Calling 自行选择
- **subagents 参数**传给 `create_deep_agent` 后，框架自动创建 `SubAgentMiddleware` 并注入 `task` 工具
- **不传 subagents 时**行为与普通单 Agent 一致（向后兼容）

---

## 附录：六大层映射总览

| 层次 | 实现组件 | 代码位置 | 状态 |
|------|---------|---------|------|
| **入口层** | FastAPI + JWT Auth + SSE | `main.py`, `app/auth.py` | ✅ |
| **编排层** | DeepAgents 中间件管线 | `app/async_create_agent.py` | ✅ |
| **模型层** | ModelGateway + 熔断 + 探活 + 热切换 | `app/gateway/` | ✅ |
| **工具层** | 4 个核心 LangChain Tool | `app/async_tools.py` | ✅ |
| **记忆层** | Checkpointer + Store + Redis | `app/pg_database.py`, `app/redis_manager.py` | ✅ |
| **观测层** | Langfuse CallbackHandler + trace | `main.py`（ChatEndpoint 中注入） | ✅ |