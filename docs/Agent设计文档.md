# TK-MultiAgent Agent 设计文档

---

## 目录

1. [架构总览](#1-架构总览)
2. [三层 Agent 模型](#2-三层-agent-模型)
   - [2.1 Triage 层](#21-triage-层)
   - [2.2 Executor 层](#22-executor-层)
   - [2.3 Specialist 层](#23-specialist-层)
3. [LLM 自路由机制](#3-llm-自路由机制)
4. [HITL 人审审批流](#4-hitl-人审审批流)
5. [后台任务执行引擎](#5-后台任务执行引擎)
6. [中间件管线](#6-中间件管线)
7. [工具系统](#7-工具系统)
8. [Schema 注册中心](#8-schema-注册中心)
9. [三层记忆模型](#9-三层记忆模型)
10. [全链路追踪](#10-全链路追踪)
11. [智能模型网关](#11-智能模型网关)
12. [运行时状态与可观测性](#12-运行时状态与可观测性)
13. [容错与韧性设计](#13-容错与韧性设计)
14. [自进化系统](#14-自进化系统)
   - [14.1 设计理念](#141-设计理念)
   - [14.2 进化周期](#142-进化周期)
   - [14.3 组件架构](#143-组件架构)
   - [14.4 热加载机制](#144-热加载机制)
   - [14.5 安全策略](#145-安全策略)
   - [14.6 Admin API](#146-admin-api)

---

## 1. 架构总览

```
                           ┌──────────────────────────┐
                           │     POST /chat (SSE)      │
                           │     唯一对话入口           │
                           └────────────┬─────────────┘
                                        │
                           ┌────────────▼─────────────┐
                           │     中间件管线             │
                           │  RateLimit → Gateway     │
                           │  → Retry → ToolLimit     │
                           │  → ToolRetry             │
                           └────────────┬─────────────┘
                                        │
                           ┌────────────▼─────────────┐
                           │   Triage DeepAgent        │
                           │   意图理解 · 任务分流      │
                           │   简单→直接处理           │
                           │   复杂→创建后台任务        │
                           └──────┬──────────┬────────┘
                                  │          │
                    直接回答      │          │  创建后台任务
                 (task→Specialist)│          │  (create_background_task)
                                  │          │
                                  │     ┌────▼─────────────────────┐
                                  │     │   Executor DeepAgent      │
                                  │     │   后台执行引擎             │
                                  │     │   规划→委托→审批→汇报      │
                                  │     └──────────┬───────────────┘
                                  │                │
                                  │          task→Specialist
                                  │                │
                           ┌──────▼────────────────▼──────┐
                           │     N 个 Specialist Agent      │
                           │     领域工具 · 结构化输出      │
                           │     ├── 8 个手动定义           │
                           │     └── 自进化动态生成 ←───────┼────────┐
                           └───────────────────────────────┘        │
                                                                    │
                  ┌─────────────────────────────────────────────────┘
                  │  EvolutionManager (自进化引擎)
                  │  缺口检测 → LLM 生成 → 验证 → 审批 → 热加载
                  └─────────────────────────────────────────────────
```

**核心设计决策**：不设外部路由分类器。所有 Specialist 的 name + description 通过 system prompt 注入，LLM 通过 Function Calling 自行选择合适的 Agent。此模式参考了 Claude Code、HermesAgent、OpenClaw 的实践。新增 **自进化能力**：系统从执行日志中检测能力缺口，LLM 自动生成 Specialist，无需重启即可热加载。

---

## 2. 三层 Agent 模型

项目采用 **Triage → Executor → Specialist** 三层 DeepAgent 架构。每一层有明确的职责边界，通过 LangGraph 的 `interrupt()` 机制实现层间控制流传递。

### 2.1 Triage 层

**定位**：用户对话的入口，唯一的职责是**判断任务复杂度并分流**。

**不负责的事情**：规划、编排、结果中转、进度跟踪——这些全部交给 Executor。

**运行方式**：在 HTTP 请求-响应周期内同步执行（SSE 流式返回）。

**分流规则**：

| 条件 | 判定 | 动作 |
|------|------|------|
| 单个 Specialist 能完成 + 无审批 + 单轮可答 | 简单任务 | `task` → Specialist → 提炼结果返回 |
| 需要 ≥2 个 Specialist 协作 | 复杂任务 | `create_background_task` → 返回 task_id |
| 涉及审批或人类决策 | 复杂任务 | `create_background_task` → 返回 task_id |
| 数据量大或跨系统长周期 | 复杂任务 | `create_background_task` → 返回 task_id |
| Specialist 返回 `[HUMAN_APPROVAL_REQUIRED]` | 升级为复杂任务 | 立即调用 `create_background_task` |

**关键安全规则**——`[HUMAN_APPROVAL_REQUIRED]` 升级：

Triage 在处理简单任务时，如果委托的 Specialist 返回了 `[HUMAN_APPROVAL_REQUIRED]` 标记，说明任务实际上涉及审批流程。Triage **绝不能自行处理审批标记**，必须立即调用 `create_background_task` 将任务升级为后台执行，由 Executor 接管审批流程。

**代码位置**：
- Prompt 定义：[app/prompts/triage_prompt.py](app/prompts/triage_prompt.py)
- System prompt 由 `build_triage_prompt(subagents)` 动态生成，Specialist 列表从实际扫描结果注入

### 2.2 Executor 层

**定位**：后台任务的执行引擎，LLM 自主驱动的长周期任务执行器。

**运行方式**：`asyncio.Task` 在后台驱动 LangGraph `astream()`，与 HTTP 请求-响应解耦。

**执行循环**：

```
分析任务目标
  → 拆解为子任务
  → 委托 Specialist（task 工具）
  → 评估结果
    → 满足 → 继续下一步
    → 不满足 → 重试/换策略/换 Specialist
  → 遇到 [HUMAN_APPROVAL_REQUIRED]
    → 调用 request_approval → interrupt() 挂起
    → 等待人审恢复
  → 全部完成 → 汇报结果
```

**与 Triage 的区别**：

| 维度 | Triage | Executor |
|------|--------|----------|
| 运行上下文 | HTTP SSE 流式 | asyncio.Task 后台 |
| 生命周期 | 秒级 | 分钟~小时级 |
| 工具集 | task, create_background_task, get_task_status | task, request_approval, read_task_journal |
| 职责 | 分流 | 执行 |
| 审批处理 | 检测到标记→升级 | 调用 request_approval→挂起→恢复 |

**代码位置**：
- Prompt 定义：[app/prompts/executor_prompt.py](app/prompts/executor_prompt.py)
- 执行引擎：[app/harness/task_executor.py](app/harness/task_executor.py)
- Agent 工厂：[app/async_create_agent.py](app/async_create_agent.py)

### 2.3 Specialist 层

**定位**：领域专家，每个 Specialist 拥有独立的能力边界和专属工具集。

**声明式定义**：每个 Specialist 通过 `AGENT.md` 文件定义，包含 YAML frontmatter + Markdown body，由 `discover_specialist_agents()` 在服务启动时自动扫描。

**Specialist 清单**（8 个）：

| Specialist | 文件 | 核心工具 |
|------------|------|----------|
| `general_assistant` | [subagents/general/](app/subagents/general/AGENT.md) | 通用问答、时间、搜索 |
| `talent_search_specialist` | [subagents/recruitment_talent/](app/subagents/recruitment_talent/AGENT.md) | Moka 候选人搜索/详情 |
| `job_management_specialist` | [subagents/recruitment_job/](app/subagents/recruitment_job/AGENT.md) | Moka 职位管理 |
| `resume_delivery_specialist` | [subagents/recruitment_resume/](app/subagents/recruitment_resume/AGENT.md) | Moka 简历推送 |
| `interview_coordinator` | [subagents/recruitment_interview/](app/subagents/recruitment_interview/AGENT.md) | Moka 面试查询/安排 |
| `offer_manager` | [subagents/recruitment_offer/](app/subagents/recruitment_offer/AGENT.md) | Moka Offer 状态管理 |
| `approval_initiator` | [subagents/recruitment_approval/](app/subagents/recruitment_approval/AGENT.md) | 审批请求发起 |
| `recruitment_analyst` | [subagents/recruitment_analytics/](app/subagents/recruitment_analytics/AGENT.md) | Moka 招聘漏斗分析 |

**AGENT.md 规范**：

```yaml
---
name: talent_search_specialist               # 唯一标识（LLM 路由目标）
description: 人才搜索专家 — 搜索、筛选候选人    # 注入 system prompt 的路由表
allowed_tools: [async_moka_search_candidates,  # 从 TOOL_REGISTRY 获取的工具
                async_moka_get_candidate_detail]
output_schema: |                              # 可选：结构化输出 JSON Schema
  {
    "type": "object",
    "required": ["candidates", "total_count"],
    "properties": { ... }
  }
---
# 角色身份（Markdown body → system_prompt）

## Identity
你是企业的**人才搜索专家 Agent**...

## 核心能力
...

## 约束与边界
...
```

**工具分配机制**：

`discover_specialist_agents()` 的处理流程：

1. 扫描 `app/subagents/*/AGENT.md`
2. 解析 YAML frontmatter → `name`, `description`, `allowed_tools`, `output_schema`
3. 从 `TOOL_REGISTRY` 按 `allowed_tools` 名称匹配工具对象
4. 如果声明了 `output_schema`，通过 `SchemaRegistry` 创建 `response_format` (Pydantic 模型)
5. 组装为 `SubAgent` TypedDict → 传给 `create_deep_agent(subagents=...)`

每个 Specialist **只拿到自己声明的工具**，减少 token 占用和工具选择错误。

**Specialsit 来源**：除了手动编写的 8 个 Specialist，系统还可通过 **自进化引擎 (EvolutionManager)** 从执行日志中自动发现能力缺口，由 LLM 生成新的 AGENT.md 并热加载——详见 [第 14 章](#14-自进化系统)。

**代码位置**：
- 发现与加载：[app/agent_definitions.py](app/agent_definitions.py)
- SubAgent 定义：[app/subagents/*/AGENT.md](app/subagents/)
- 暂存目录：[app/subagents/_staging/](app/subagents/_staging/)（进化生成的 Agent 审批前存放）

---

## 3. LLM 自路由机制

### 设计理念

传统的多 Agent 系统通常使用一个独立的"路由器"模块（分类器 ML 模型或规则引擎）来决定调用哪个下游 Agent。TK-MultiAgent 不采用这种方案，而是：

> **将所有 Specialist 的 name + description 注入 system prompt，让 LLM 通过 Function Calling 自行选择。**

### 为什么选择自路由

| 对比维度 | 外部分类器 | LLM 自路由 |
|----------|-----------|-----------|
| 维护成本 | 新增 Agent 需更新路由规则 | 新增 `AGENT.md` 即自动加入 |
| 灵活性 | 规则匹配，边界情况需人工补充 | 自然语言理解，模糊意图也能路由 |
| 级联错误 | 分类器错误 → 后续全错 | LLM 在上下文中可自我纠偏 |
| 延迟 | 多一次模型调用 | 无额外调用 |

### 实现方式

`create_deep_agent(subagents=...)` 会自动创建 `SubAgentMiddleware`，该中间件将 subagent 列表注入到 system prompt 和 `task` 工具的参数描述中：

```
可用 SubAgent:
- talent_search_specialist: 人才搜索专家 — 在企业人才库中按条件搜索...
- job_management_specialist: 职位管理专家 — 管理招聘职位...
- ...
```

当 LLM 决定需要委托 Specialist 时，调用 `task` 工具：

```json
{
  "subagent_type": "talent_search_specialist",
  "description": "搜索具有5年以上Python经验的后端工程师"
}
```

`SubAgentMiddleware` 拦截此调用 → 创建独立 SubAgent → 注入专属 system_prompt 和工具 → 执行 → 返回结果。

---

## 4. HITL 人审审批流

### 设计挑战

在 LangGraph 的多层 Agent 架构中，嵌套 interrupt 不可靠传播。具体来说：
- Specialist（SubAgent）在 Executor 的 subgraph 中运行
- 如果在 Specialist 内部触发 `interrupt()`，外层 Executor 可能无法正确检测
- 多个 Specialist 并发时的 interrupt 顺序不可预测

### 解决方案：双层分离

**核心思想**：将"发起审批请求"拆分为两个独立步骤，分别由不同层负责：

```
Specialist 侧（发起）:
  async_request_approval(title, approver_role, context)
    → 写入 Store("approval_requests", approval_id, data)
    → 返回标记字符串 [HUMAN_APPROVAL_REQUIRED]
    → Specialist 不暂停，正常返回

Executor 侧（暂停）:
  看到 Specialist 输出中的 [HUMAN_APPROVAL_REQUIRED]
    → 调用 request_approval(approval_id)
    → HumanInTheLoopMiddleware 拦截 → interrupt()
    → 任务挂起，等待人类决策
```

**完整时序**：

```
1. Executor → task(subagent_type="approval_initiator", desc="发起张明远的Offer审批")
2. Specialist(approval_initiator) → async_request_approval(...)
3.   async_request_approval → store.aput("approval_requests", "apr-abc123", data)
4.   async_request_approval → 返回 {"marker": "[HUMAN_APPROVAL_REQUIRED]", "approval_id": "apr-abc123"}
5. Executor 接收 Specialist 输出
6. Executor 检测 [HUMAN_APPROVAL_REQUIRED] → 调用 request_approval(approval_id="apr-abc123")
7. HumanInTheLoopMiddleware 拦截 → interrupt() → 任务进入 WAITING_HUMAN
8. 事件总线发布 task.interrupted → SSE 推送前端
9. 人类通过 POST /tasks/{id}/resume 做出决策 {action: "approved"}
10. TaskExecutor._resume_loop → 更新 Store 中审批状态
11. Command(resume=...) → 任务恢复执行
12. request_approval 工具返回审批结果 → Executor 继续下一步
```

### 审批状态持久化

审批数据存于 `Store("approval_requests", approval_id)`：
```json
{
  "approval_id": "apr-abc123",
  "title": "张明远 → AI大模型应用工程师 35K/月 Offer审批",
  "approver_role": "用人经理",
  "context": "候选人: 张明远\n职位: ...\n薪资: 35000元/月",
  "status": "pending",
  "decision": null,
  "comment": null
}
```

服务重启后审批数据不丢失。人审通过 `POST /tasks/{id}/resume` 恢复后，`TaskExecutor._update_approval_store()` 更新 `decision` 和 `status` 字段。

### 薪资分级审批规则

| 薪资范围 | 审批人 | 说明 |
|----------|--------|------|
| ≤ 30,000元/月 | `用人经理` | 单级审批 |
| 30,001-50,000元/月 | `部门负责人` | 需加签 |
| > 50,000元/月 | `CEO` | 需最终审批 |

### P0 兜底：审批标记强制检测

正常情况下，Executor LLM 看到 `[HUMAN_APPROVAL_REQUIRED]` 后会立即调用 `request_approval`。但如果 LLM 因幻觉、上下文压缩或推理偏差连续忽略该标记，系统有 P0 级别的兜底机制：

```
连续3轮检测到标记但未调用 request_approval
  → 抛出 ApprovalNotHandledError
  → _force_approval_interrupt()
  → 合成 interrupt_info（模拟 HITLRequest 格式）
  → 任务强制转入 WAITING_HUMAN
  → 前端收到 "系统自动挂起" 通知
```

计数器在每次检测到 `request_approval` 工具调用时归零。

**代码位置**：
- Specialist 侧工具：[app/tools/approval.py](app/tools/approval.py)
- Executor 侧工具：[app/tools/request_approval.py](app/tools/request_approval.py)
- 执行引擎 HITL 处理：[app/harness/task_executor.py](app/harness/task_executor.py) `_handle_interrupt` / `_resume_loop` / `_check_approval_marker_handled` / `_force_approval_interrupt`

---

## 5. 后台任务执行引擎

### 任务生命周期

```
CREATED → EXECUTING → WAITING_HUMAN → EXECUTING → ... → COMPLETED
                  ↘                ↗
                   FAILED      CANCELLED
```

### TaskHandle 数据结构

对外暴露的任务状态（`_handles` 内存 dict）：

```python
@dataclass
class TaskHandle:
    task_id: str           # 幂等 key（SHA-256 或 UUID）
    thread_id: str         # LangGraph thread ID（= task_id）
    goal: str              # 用户原始目标
    user_id: str           # 所属用户
    session_id: str        # 所属会话（完成后回写结果）
    status: TaskStatus     # 当前状态
    plan: list[dict]       # 执行计划（步骤列表）
    progress: str          # 当前进度描述（最近一条 AI 消息）
    result_summary: str    # 完成后的总结
    error_message: str     # 失败原因
    approval_id: str       # 当前挂起的审批请求 ID
```

### 幂等任务创建

用户在同一会话中重复提交相同目标时，通过 SHA-256 哈希去重：

```python
raw_key = f"{user_id}:{goal}"
task_id = f"task-{hashlib.sha256(raw_key.encode()).hexdigest()[:16]}"
```

已存在的任务直接返回已有 `TaskHandle`，不创建重复的执行线程。

### 执行循环 `_execute_loop`

```
1. 设置 trace_id（继承请求链路 + 追加 task_id）
2. 构造 LangGraph config (thread_id, user_id, task_id, store)
3. 发布 task.executing 事件
4. 检查快照：如有 → 注入恢复上下文 + journal 摘要
5. 构造初始消息（新任务：目标+上下文；恢复：快照信息+journal）
6. agent.astream(initial_state, config, stream_mode="updates")
   └── 逐事件处理：
       ├── __interrupt__ → _handle_interrupt() → return（挂起）
       └── 正常事件 → _process_event()（同步 progress + 写入 journal + 审批兜底检测）
7. 正常结束 → COMPLETED → 清理快照 → 回写结果 → 发布事件
```

### 服务重启恢复

`recover_tasks()` 在服务启动时扫描 `Store("task_snapshots",)`：

| 快照状态 | 恢复策略 |
|----------|----------|
| `waiting_human` | 只注册 TaskHandle，等待人审恢复（不启动 asyncio.Task） |
| `executing` / `created` | 重建 asyncio.Task，注入恢复上下文 + journal 摘要后继续执行 |
| `completed` / `failed` / `cancelled` | 跳过（终端状态），清理残留快照 |

### 优雅关闭

```
1. drain() — 拒绝新任务
2. 等待运行中任务完成（可配置超时，默认 30s）
3. 超时未完成 → 保存快照到 Store → cancel asyncio.Task
4. 清理 EventBus handler
5. 关闭 DeadLetterQueue scanner
```

### 任务结果回写

任务完成后，`_write_task_result()` 将结果写入 `Store("task_results", session_id)`。Triage 在下一次 `/chat` 时读取并标记为已读，实现跨请求的任务结果可见性。

**代码位置**：[app/harness/task_executor.py](app/harness/task_executor.py)

---

## 6. 中间件管线

Agent 的每次模型调用和工具调用都经过多层中间件处理。管线在 `async_create_agent()` 中组装。

### 管线架构

```
请求进入
  │
  ├── [deepagents 自动组装的基础栈]
  │   ├── TodoListMiddleware
  │   ├── FilesystemMiddleware
  │   ├── SubAgentMiddleware（如果传了 subagents）
  │   ├── SummarizationMiddleware
  │   └── PatchToolCallsMiddleware
  │
  ├── [用户定制中间件层]
  │   ├── ModelCallLimitMiddleware    ← 模型调用次数限制
  │   ├── GatewayMiddleware           ← 健康感知路由（替换静态 ModelFallback）
  │   ├── ModelRetryMiddleware        ← 模型调用失败重试
  │   ├── ToolCallLimitMiddleware ×2  ← 工具调用次数限制
  │   └── ToolRetryMiddleware         ← 工具调用失败重试
  │
  ├── [deepagents 自动组装的尾部栈]
  │   ├── MemoryMiddleware
  │   └── HumanInTheLoopMiddleware    ← HITL 审批中断
  │
  ▼
LLM / Tool 执行
```

### 各中间件参数

| 中间件 | 配置 | 说明 |
|--------|------|------|
| ModelCallLimit | thread=100, run=15 | 防止无限模型调用循环 |
| GatewayMiddleware | role=CHAT | 健康感知路由 + 自动降级 |
| ModelRetry | retries=1, delay=0.5s | 快速兜底重试 |
| ToolCallLimit (全局) | thread=50, run=50 | 工具调用预算 |
| ToolCallLimit (web_search) | thread=20, run=5 | 联网搜索单独限制 |
| ToolRetry | retries=3, backoff=2.0x | 指数退避重试 |

### 中间件位置策略

`extra_middleware` 参数支持 `prepend` / `append`，允许在管线前后注入自定义中间件。目前用于注入 `HumanInTheLoopMiddleware`（由 `create_deep_agent` 自动处理）。

**代码位置**：[app/async_create_agent.py](app/async_create_agent.py)

---

## 7. 工具系统

### 注册机制

全局 `TOOL_REGISTRY` dict 作为工具注册中心，通过 `@register_tool` 装饰器自动注册：

```python
from app.tools._registry import register_tool, TOOL_REGISTRY
from langchain.tools import tool

@register_tool
@tool
async def my_tool(param: str) -> str:
    """工具描述 — LLM 通过 Function Calling 看到此描述。"""
    ...
```

工具名冲突检测：同名工具注册时抛出 `ValueError`。

### 工具分发到 Specialist

`discover_specialist_agents()` 按 AGENT.md 的 `allowed_tools` 列表，从 `TOOL_REGISTRY` 精确匹配：

```python
subagent_tools = [
    TOOL_REGISTRY[name]
    for name in spec["allowed_tools"]
    if name in TOOL_REGISTRY
]
agent_def["tools"] = subagent_tools
```

每个 Specialist 只拿到自己需要的工具，无关工具不注入 — 减少 token 占用，降低工具选择错误的概率。

### 工具清单（16 个）

| 工具名 | 模块 | 用途 |
|--------|------|------|
| `async_get_current_time` | common.py | 时间查询 |
| `async_web_search` | common.py | 联网搜索 |
| `async_knowledge_query_ask` | knowledge.py | 知识库 RAG |
| `create_background_task` | task.py | 创建后台任务 |
| `get_task_status` | task_query.py | 查询任务状态 |
| `async_request_approval` | approval.py | Specialist 发起审批 |
| `request_approval` | request_approval.py | Executor 触发 interrupt |
| `read_task_journal` | read_journal.py | 读取执行日志 |
| `async_moka_search_candidates` | moka_candidate.py | 候选人搜索 |
| `async_moka_get_candidate_detail` | moka_candidate.py | 候选人详情 |
| `async_moka_get_job_detail` | moka_job.py | 职位详情 |
| `async_moka_push_resume` | moka_resume.py | 简历推送 |
| `async_moka_get_interviews` | moka_interview.py | 面试查询 |
| `async_moka_get_offer_status` | moka_offer.py | Offer 状态 |
| `async_moka_get_recruitment_funnel` | moka_analytics.py | 招聘漏斗分析 |
| `async_moka_list_jobs` | moka_job.py | 职位列表 |

### 非工具基础设施（resources.py）

`app/tools/resources.py` 管理工具运行时需要的共享资源注册：

- `knowledge_resources` — RAG 模型注册（embed / rerank / chat / pipeline / gateway）
- `_moka_client` — Moka API 客户端（Demo 模式下自动降级为仿真数据）
- `_task_executor` — 后台任务执行器（供 `create_background_task` 工具使用）
- `_dead_letter_queue` — 死信队列（供审批工具在 Store 写入失败时入队）

**代码位置**：
- 注册中心：[app/tools/_registry.py](app/tools/_registry.py)
- 基础设施：[app/tools/resources.py](app/tools/resources.py)

---

## 8. Schema 注册中心

### 设计目的

Specialist Agent 的输出可以是结构化的——比如候选人搜索结果、审批发起结果。结构化输出带来两个好处：

1. **下游可精确引用**：Executor 可以直接读取 `candidates[0].name` 而不需要在文本中正则提取
2. **验证与重试**：输出不符合 Schema 时，LLM 可以自动重试

### 架构

```
AGENT.md          SchemaRegistry             create_deep_agent
─────────         ──────────────             ─────────────────
output_schema:    ┌──────────────────┐
  {...JSON...}  ─→│ from_json_schema  │──→ Pydantic BaseModel
                  │ ("talent_search") │
                  └──────────────────┘
                          │
output_schema:           │
  talent_search_result ──┘  (引用已注册的 Schema)
                          │
                  ┌───────▼──────────┐
                  │ validate(name,   │──→ (validated_data, errors)
                  │        raw_data) │
                  └──────────────────┘
```

### 两种声明方式

**方式一：内联 JSON Schema**（在 AGENT.md frontmatter 中直接写）

```yaml
output_schema: |
  {
    "type": "object",
    "required": ["candidates", "total_count"],
    "properties": {
      "candidates": { ... },
      "total_count": { "type": "integer" }
    }
  }
```

**方式二：引用已注册的 Schema 名称**

```yaml
output_schema: talent_search_result
```

对应的 Pydantic 模型通过 `schema_registry.register("talent_search_result", MyModel)` 或 `from_json_schema()` 预先注册。

### 与 DeepAgents 的集成

`discover_specialist_agents()` 在构建 `SubAgent` 时，将 Pydantic 模型设置为 `response_format`：

```python
agent_def["response_format"] = output_schema_model
```

DeepAgents 的 `ToolStrategy` 将此转换为 `tool_choice="required"` 的 Function Calling 调用，LLM 必须产出符合 Schema 的 JSON。

### JSON Schema → Pydantic 类型映射

| JSON Schema type | Python type | 说明 |
|------------------|-------------|------|
| `string` | `str` | |
| `integer` | `int` | |
| `number` | `float` | |
| `boolean` | `bool` | |
| `array` | `list[str]` 或 `list[dict]` | 取决于 items.type |
| `object` | `dict` | |

必填字段（`required` 数组中）使用 `...`（Ellipsis），可选字段使用 `None` 作为默认值。

**代码位置**：[app/schemas/registry.py](app/schemas/registry.py)

---

## 9. 三层记忆模型

### Hot → Warm → Cold 分层

```
┌──────────────────────────────────────┐
│  Hot Memory（上下文窗口内）            │
│  - 当前步骤的 messages                │
│  - 最近的 tool_call / tool_result     │
│  - 受 SummarizationMiddleware 压缩    │
│  ⚡最快，但容量有限                    │
├──────────────────────────────────────┤
│  Warm Memory（Store 快速检索）         │
│  - 用户长期偏好                       │
│  - 任务执行计划（plan）                │
│  - 已完成的步骤摘要                    │
│  - 关键发现（key_findings）            │
│  - 人类决策历史                       │
│  🔍跨步骤可访问，不参与压缩             │
├──────────────────────────────────────┤
│  Cold Memory（归档存储）               │
│  - 历史任务完整记录（TaskMemory）       │
│  - 任务执行日志（Journal）             │
│  - 服务重启后可恢复                    │
│  ❄️按需搜索，永久保留                    │
└──────────────────────────────────────┘
```

### Journal（执行日志）

与 messages 不同，Journal 不受 `SummarizationMiddleware` 压缩影响，是任务执行的**永久结构化记录**。

**事件类型**：

| 事件 | 描述 |
|------|------|
| `specialist_result` | Specialist 委托完成（记录委托了谁、返回了什么） |
| `decision` | Executor 的关键决策（含超100字符的 AIMessage） |
| `approval_requested` | 任务挂起等待人审 |
| `error` | 异常/错误 |
| `completed` | 任务完成 |

**恢复时的作用**：Executor 从快照恢复后，先读取 Journal 生成结构化摘要（"已经做了什么、做到哪了"），作为恢复上下文注入。这比从压缩后的 messages 中猜测要精确得多。

### TaskContextManager

核心 API：

| 方法 | 用途 |
|------|------|
| `assemble_initial_context(goal, user_id)` | 新任务初始上下文（目标 + 用户偏好 + 历史参考） |
| `save_step_result(task_id, step_id, result, specialist)` | 步骤完成 → 保存摘要 + 结构化数据 |
| `save_human_decision(task_id, decision)` | 记录审批决策 → 触发偏好学习 |
| `build_resumption_context(task_id)` | 恢复上下文（目标 + 计划进度 + 关键发现 + 阻塞点） |
| `build_step_context(task_id, step_id)` | 单步执行上下文（依赖步骤的结构化输出 + 文本摘要） |
| `write_journal_entry(task_id, entry)` | 追加执行日志 |
| `build_journal_summary(task_id)` | 从 Journal 生成人类可读的执行摘要 |

**代码位置**：[app/task_context.py](app/task_context.py)

---

## 10. 全链路追踪

### trace_id 生成与传播

使用 Python `contextvars` 实现跨 asyncio 协程的 trace_id 自动传播，无需显式传参。

**格式**：

```
前台请求:  trace-{uuid16}
后台任务:  trace-{uuid16}/task-{task_id_short}
```

**生命周期**：

```
HTTP 请求到达 /chat
  → TraceContext.start_trace()          → trace-a1b2c3d4e5f6g7h8

Triage 执行（同一请求内）
  → trace_id 自动传播到所有工具调用

创建后台任务 _execute_loop()
  → TraceContext.set_task_context()     → trace-a1b2c3d4e5f6g7h8/task-x9y0z1w2

Executor 执行（后台 asyncio.Task）
  → trace_id 自动传播到所有工具调用和事件发布

事件总线 publish()
  → 自动注入 trace_id 到 data["trace_id"]
```

### TraceIdFilter

日志过滤器自动为每条日志记录注入当前 trace_id：

```
2026-06-18 10:30:15 [trace-a1b2c3d4e5f6g7h8/task-x9y0z1w2] INFO TaskExecutor 任务完成
```

### Langfuse 集成

`TraceContext.inject_to_metadata()` 将 trace_id 注入 Langfuse 的 metadata，实现 LLM 调用的全链路关联。

**代码位置**：[app/trace_context.py](app/trace_context.py)

---

## 11. 智能模型网关

### 职责

`ModelGateway` 作为中心单例，统一管理所有 LLM 实例的生命周期：

1. **模型注册**：启动时从 `config.py` 加载
2. **健康跟踪**：记录每次调用的延迟 & 成败
3. **熔断管理**：自动/手动触发与恢复
4. **智能路由**：返回健康排序的模型链
5. **热切换**：运行时无停机更换活跃模型

### 模型角色

```python
class ModelRole(Enum):
    CHAT = "chat"                     # Agent 对话（LangChain 接口）
    FALLBACK_CHAT = "fallback_chat"   # Agent 备用
    RETRIEVAL_LLM = "retrieval_llm"   # 检索答案生成（LlamaIndex 接口）
    RETRIEVAL_REWRITER = "retrieval_rewriter"  # Query 改写
```

### 路由链

`get_model_chain(role)` 返回健康排序的模型列表：

1. **当前活跃模型**（如果熔断器未 OPEN）
2. **降级链**（跳过已熔断的）
3. **兜底**：即使熔断也包含活跃模型（宁可失败也不错失）

Consumer（`GatewayMiddleware`）遍历此链，直到成功或全部失败。

### 熔断器

三态熔断器，每个模型独立实例：

```
CLOSED ──连续失败≥threshold──→ OPEN
OPEN   ──冷却时间到──────────→ HALF_OPEN（允许探测）
HALF_OPEN ──探测成功─────────→ CLOSED
HALF_OPEN ──探测失败─────────→ OPEN（重新计时）
```

默认参数：5 次连续失败触发熔断，30 秒冷却期，HALF_OPEN 最多 1 次探测。

### 后台探活

`HealthProbe` 每 30 秒对所有已注册模型发送轻量 ping（`ainvoke("ping")` 或 `acomplete("ping")`），成功 → `record_success`，失败 → `record_failure`。

### HealthRecord 并发保护

因为 `record_success()` / `record_failure()` 被两个并发的 asyncio 协程调用——`GatewayMiddleware`（请求路径）和 `HealthProbe`（后台探活）——每个模型的 `HealthRecord` 受独立的 `asyncio.Lock` 保护，防止并发写入造成的数据不一致。

**代码位置**：
- 网关核心：[app/gateway/model_gateway.py](app/gateway/model_gateway.py)
- 熔断器：[app/gateway/circuit_breaker.py](app/gateway/circuit_breaker.py)
- 探活：[app/gateway/health_probe.py](app/gateway/health_probe.py)
- 路由中间件：[app/gateway/gateway_middleware.py](app/gateway/gateway_middleware.py)
- 类型定义：[app/gateway/types.py](app/gateway/types.py)

---

## 12. 运行时状态与可观测性

### StatusCallbackHandler

`StatusCallbackHandler` 是 LangChain `AsyncCallbackHandler` 的实现，在 Agent 执行过程中产生结构化状态事件：

| 事件 | 触发时机 | 前端用途 |
|------|----------|----------|
| `agent_start` | Agent 开始执行 | 显示"思考中..." |
| `tool_start` | 工具被调用 | 显示工具名称和参数 |
| `tool_end` | 工具执行完毕 | 显示工具返回结果 |
| `model_degraded` | 主模型失败，降级到备用 | 显示降级警告 |
| `model_restored` | 备用恢复为主模型 | 清除降级警告 |
| `summarization` | 上下文压缩触发 | 显示"整理对话..." |

### 事件总线 (EventBus)

基于 Redis PubSub + 本地 handler 的双通道事件系统：

```
EventBus.publish("task.completed", data)
  ├── 本地处理器：同一 event loop 内立即执行（精确匹配 + 前缀通配符）
  └── Redis PubSub：跨进程/跨服务传播（不可用时降级为纯内存模式）
```

**任务事件约定**：

| 事件类型 | 数据 | 触发点 |
|----------|------|--------|
| `task.created` | TaskHandle.to_dict() | submit_task() |
| `task.executing` | TaskHandle.to_dict() | _execute_loop 开始 |
| `task.interrupted` | TaskHandle + interrupt_info | _handle_interrupt() |
| `task.completed` | TaskHandle.to_dict() | 正常完成 |
| `task.failed` | TaskHandle + error | 异常 |
| `task.cancelled` | TaskHandle.to_dict() | cancel_task() |
| `task.resumed` | TaskHandle.to_dict() | resume_task() |
| `task.recovered` | TaskHandle.to_dict() | recover_tasks() |

### SSE 实时推送

前端通过 `GET /tasks/{id}/events` 订阅 SSE 流，获取指定任务的实时状态变更。每次 `EventBus.publish()` 会通过订阅的 handler → `queue.put()` → SSE yield 推送到前端。

### Journal API

`GET /tasks/{task_id}/journal?limit=50` 返回任务的结构化执行日志（JSON 数组），每条记录包含 step、timestamp、event、description、detail 字段。

**代码位置**：
- 状态回调：[app/status_handler.py](app/status_handler.py)
- 事件总线：[app/harness/event_bus.py](app/harness/event_bus.py)

---

## 13. 容错与韧性设计

### 多层防御

```
Business Logic 层：  ApprovalGuard（审批兜底检测 + 3轮计数器）
                    DeadLetterQueue（审批 Store 写入失败 → 持久化重试）
                           │
Model Gateway 层：   CircuitBreaker（单模型熔断 → 降级链切换）
                    HealthProbe（30s 定时探活 → 自动恢复）
                           │
Middleware 层：      ModelRetry（1次快速兜底）
                    ToolRetry（3次指数退避）
                           │
Infrastructure 层：  TaskSnapshot（任务快照 → 重启恢复）
                    Journal（执行日志 → 恢复上下文重建）
                    EventBus（Redis 降级 → 纯内存模式）
```

### 死信队列 (DeadLetterQueue)

当 `async_request_approval` 的 Store 写入失败时（如 PG 暂时不可用），审批数据不会丢失：

```
Store 写入失败
  → dlq.enqueue("async_request_approval", {approval_id, approval_data})
    → 持久化到 Store("dead_letter",) — 服务重启不丢失
      → 后台扫描器 (每 2 分钟)
        → retry 处理器重新写入 Store
          → 成功 → 从死信队列删除
          → 失败 → 指数退避重试 (1m→2m→4m→...→max 1h) → 最终 abandoned
```

### 审批兜底检测 (ApprovalGuard)

P0 级别的安全机制，防止 LLM 因幻觉/上下文压缩连续忽略审批标记：

```
_execute_loop 每轮事件处理
  → _check_approval_marker_handled()
    → 检测本轮是否有 [HUMAN_APPROVAL_REQUIRED] 但无 request_approval 调用
      → 计数器 +1
      → 连续 3 轮 → ApprovalNotHandledError
        → _force_approval_interrupt()
          → 合成 HITLRequest → 任务强制转入 WAITING_HUMAN
```

### 幂等设计

| 场景 | 幂等键 | 去重方式 |
|------|--------|----------|
| 后台任务创建 | SHA-256(user_id + goal) | 返回已有 TaskHandle |
| 审批请求 | SHA-256(task_id + step_id + title) | 返回已有审批的 JSON |
| 死信入队 | SHA-256(operation_name + args) | 更新已有死信的重试计数 |
| 文档索引 | file_hash + content_hash | Milvus 写入跳过 |
| 进化提案生成 | SHA-256(gap_id + agent_name) | 覆盖已有提案 |

---

## 14. 自进化系统

### 14.1 设计理念

传统的 Multi-Agent 系统需要人工分析执行日志、手动编写新 Specialist。TK-MultiAgent 采用 **Hermes 风格的自进化架构**：系统从自身的执行轨迹中检测能力缺口，由 LLM 自动生成新的 Specialist 定义（AGENT.md），经过验证和审批后热加载到运行中服务——全程无需重启。

**核心原则**：

1. **数据驱动进化** — 缺口检测基于真实的执行日志（Journal），而非人工反馈
2. **人工审批门控** — LLM 生成的 Agent 必须经过人类审批才能上线
3. **零停机热加载** — 激活新 Agent 不影响运行中的请求
4. **Git 可追溯可回滚** — 每次激活自动 commit，出问题一键 Git revert

### 14.2 进化周期

```
定时扫描 (6h) / Admin API 手动触发
  │
  ▼
┌─────────────────────────────┐
│  1. GapDetector 缺口检测     │
│  - 扫描 Journal + task_results
│  - LLM 识别跨任务共性缺口     │
│  - 产出 GapReport            │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  2. SubAgentGenerator 生成   │
│  - 基于 GapReport + TOOL_REGISTRY
│  - LLM 生成 AGENT.md 全文     │
│  - 写入暂存目录 (_staging)    │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  3. Validator 回归验证       │
│  - 提取历史相关测试用例        │
│  - LLM Judge 评估生成质量     │
│  - 通过率 ≥ 70% → PENDING_REVIEW
│  - 否则 → DRAFT（不提交审批）  │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  4. 人工审批                 │
│  POST /admin/evolution/       │
│    proposals/{id}/review     │
│  approve / reject            │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  5. HotReloader 激活         │
│  - AGENT.md 迁移到正式目录    │
│  - Git commit                │
│  - 重新扫描 subagents         │
│  - 重建 Triage + Executor    │
│  - 原子替换 app.state 引用    │
└─────────────────────────────┘
```

### 14.3 组件架构

```
EvolutionManager (编排器)
├── GapDetector            ← 从 Journal + task_results 检测缺口
│   └── LLM: deepseek-v4-flash
├── SubAgentGenerator      ← LLM 生成 AGENT.md
│   └── LLM: deepseek-v4-flash
├── Validator              ← LLM Judge 回归验证
│   └── LLM: deepseek-v4-flash
└── HotReloader            ← 热加载 + Git 回滚
    └── 调用 discover_specialist_agents() + async_create_agent()
```

**数据源**：

| 数据源 | 用途 | 位置 |
|--------|------|------|
| Journal（执行日志） | 单任务缺口检测（事件类型、错误模式） | `Store("task_journal", task_id)` |
| task_results | 批量分析上下文（目标、结果摘要、错误信息） | `Store("task_results", session_id)` |
| TOOL_REGISTRY | 生成 Agent 时的可用工具列表 | 全局 dict |
| discover_specialist_agents() | 已有 Agent 列表（防冲突） | 文件系统扫描 |

**Store 持久化**：进化系统使用独立的 Store namespace，服务重启后状态不丢失：

| Namespace | 内容 |
|-----------|------|
| `("evolution", "proposals")` | 所有进化提案（含 AGENT.md 全文、验证结果、审批记录） |
| `("evolution", "gap_reports")` | 检测到的能力缺口报告 |
| `("evolution", "regression_tests")` | 回归测试数据集 |

**EventBus 事件**：

| 事件 | 触发时机 |
|------|----------|
| `evolution.gap_detected` | 缺口检测完成（含 gap_ids） |
| `evolution.proposals_created` | 一批提案生成完成 |
| `evolution.proposal_approved` | 提案审批通过 |
| `evolution.proposal_rejected` | 提案被驳回 |
| `evolution.activated` | Agent 热加载上线 |
| `evolution.rolled_back` | Agent 回滚下线 |
| `evolution.scan_completed` | 定时扫描周期完成 |

### 14.4 热加载机制

热加载是整个系统最关键的操作——它实现了"不重启服务即可让新 Agent 生效"：

```
HotReloader.activate_agent(proposal):
  1. 检查名称冲突 → 拒绝重复
  2. 记录当前 Git HEAD → proposal.git_prev_commit
  3. shutil.copy2(AGENT.md, _staging → subagents/{name}/)
  4. git add + git commit → proposal.git_commit_hash
  5. new_subagents = discover_specialist_agents()  ← 复用现有函数
  6. triage_prompt = build_triage_prompt(new_subagents)
  7. executor_prompt = build_executor_prompt(new_subagents)
  8. new_agent = await async_create_agent(...)       ← 复用工厂
  9. new_executor = await async_create_agent(...)
 10. app.state.agent = new_agent                     ← 原子替换
 11. app.state.executor_agent = new_executor
 12. task_executor.executor_agent = new_executor     ← 更新引用
```

**安全保证**：

- 进行中的 HTTP 请求持有旧 agent 引用，不受影响（Python 引用计数）
- 替换是单一的 Python 赋值操作，天然原子
- 重建耗时约 1-3 秒

**回滚机制**：

```
HotReloader.rollback_agent(proposal):
  → git checkout {prev_commit} -- subagents/{name}/
  → 重新扫描 + 重建 agent
  → 或：直接删除目录（新增 Agent 之前不存在的情况）
```

### 14.5 安全策略

| 层级 | 机制 | 说明 |
|------|------|------|
| AGENT.md 校验 | 工具白名单 + 名称冲突检测 + 必填字段检查 | 生成后立即校验 |
| 验证门控 | LLM Judge 回归验证 + 通过率 ≥ 70% | 未达标不提交审批（标记为 DRAFT） |
| 人工审批 | POST /admin/evolution/proposals/{id}/review | 所有提案默认需人工审批 |
| 运行时隔离 | 新 Agent 不影响进行中的请求 | Python 引用替换天然隔离 |
| Git 追溯 | 每次激活前自动 commit | 回滚时可精确恢复到激活前状态 |

### 14.6 Admin API

所有端点需要 `X-Admin-Token` 认证。

| 方法 | 路径 | 用途 |
|------|------|------|
| `GET` | `/admin/evolution/gaps` | 列出所有检测到的能力缺口 |
| `GET` | `/admin/evolution/gaps/{id}` | 查看单个缺口详情（含证据） |
| `POST` | `/admin/evolution/gaps/analyze` | 手动触发缺口分析（可指定 task_ids） |
| `POST` | `/admin/evolution/gaps/{id}/generate` | 基于指定缺口生成进化提案 |
| `GET` | `/admin/evolution/proposals` | 列出所有提案（支持 `?status=` 筛选） |
| `GET` | `/admin/evolution/proposals/{id}` | 查看提案详情（含 AGENT.md 全文） |
| `GET` | `/admin/evolution/proposals/{id}/preview` | 预览生成的 AGENT.md（解析后） |
| `POST` | `/admin/evolution/proposals/{id}/review` | 审批提案（`action: approve/reject`） |
| `POST` | `/admin/evolution/proposals/{id}/activate` | 激活已审批提案 → 热加载 |
| `POST` | `/admin/evolution/proposals/{id}/rollback` | 回滚已激活提案 → Git revert + 重建 |
| `GET` | `/admin/evolution/status` | 进化系统状态（缺口数/提案分布/活跃 agent） |
| `PUT` | `/admin/evolution/settings` | 运行时更新进化参数 |

**配置**（`config.py` 中 `evolution` 节）：

```python
"evolution": {
    "enabled": True,                   # 是否启用自进化系统
    "scan_interval_hours": 6.0,        # 自动扫描间隔
    "analysis_lookback_hours": 24,     # 分析回溯窗口
    "max_gaps_per_scan": 5,            # 每次扫描最大缺口数
    "min_tasks_for_analysis": 10,      # 最少任务数阈值
    "auto_approve_threshold": 0.0,     # 自动审批阈值（0=永远人工）
    "validation_min_pass_rate": 0.7,   # 验证通过率下限
    "llm_model": "deepseek-v4-flash",  # 进化系统使用的 LLM
}
```

**代码位置**：
- 编排器：[app/evolution/evolution_manager.py](app/evolution/evolution_manager.py)
- 缺口检测：[app/evolution/gap_detector.py](app/evolution/gap_detector.py)
- Agent 生成：[app/evolution/agent_generator.py](app/evolution/agent_generator.py)
- 回归验证：[app/evolution/validator.py](app/evolution/validator.py)
- 热加载器：[app/evolution/hot_reloader.py](app/evolution/hot_reloader.py)
- 运行时状态：[app/evolution/_state.py](app/evolution/_state.py)
- 数据模型：[app/evolution/types.py](app/evolution/types.py)
- Admin API：[app/evolution/admin_router.py](app/evolution/admin_router.py)
- Prompt 模板：[app/prompts/gap_detection.py](app/prompts/gap_detection.py) / [app/prompts/agent_generation.py](app/prompts/agent_generation.py)

---

## 附录

### A. 新增 Specialist 流程

**方式一：人工编写**（适用于已知需求的 Specialist）

1. 在 `app/subagents/` 下创建新目录（如 `recruitment_onboarding/`）
2. 编写 `AGENT.md`（YAML frontmatter: `name`, `description`, `allowed_tools`, 可选 `output_schema`）
3. 编写 Markdown body（角色身份、核心能力、约束与边界）
4. 如需新工具 → 在 `app/tools/{domain}.py` 中定义并加 `@register_tool` + `@tool`
5. 重启服务 → `discover_specialist_agents()` 自动发现

**方式二：自进化自动生成**（适用于从执行日志中发现的缺口）

1. 等待定时扫描或手动触发 `POST /admin/evolution/gaps/analyze`
2. 审查检测到的缺口 `GET /admin/evolution/gaps`
3. 基于缺口生成提案 `POST /admin/evolution/gaps/{id}/generate`
4. 预览 AGENT.md `GET /admin/evolution/proposals/{id}/preview`
5. 审批 `POST /admin/evolution/proposals/{id}/review` (approve/reject)
6. 激活 `POST /admin/evolution/proposals/{id}/activate` → 热加载，无需重启

### B. 新增审批流程

1. Specialist 的 AGENT.md 中声明 `allowed_tools: [..., async_request_approval]`
2. Specialist 在需要审批时调用 `async_request_approval(title, approver_role, context)`
3. Executor 看到 `[HUMAN_APPROVAL_REQUIRED]` 后调用 `request_approval(approval_id)`
4. `HumanInTheLoopMiddleware` 触发 `interrupt()`
5. 人类通过 `POST /tasks/{id}/resume` 决策

### C. 关键配置

| 配置项 | 位置 | 默认值 |
|--------|------|--------|
| 模型调用限制（每线程） | async_create_agent.py | 100 |
| 模型调用限制（每请求） | async_create_agent.py | 15 |
| 工具调用限制（全局） | async_create_agent.py | 50 |
| 模型重试次数 | async_create_agent.py | 1 |
| 工具重试次数 | async_create_agent.py | 3 |
| 熔断器失败阈值 | circuit_breaker.py | 5 |
| 熔断器冷却时间 | circuit_breaker.py | 30s |
| 健康探活间隔 | health_probe.py | 30s |
| 审批兜底检测轮数 | task_executor.py | 3 |
| 死信扫描间隔 | dead_letter.py | 120s |
| 死信最大重试 | dead_letter.py | 5 |
| 优雅关闭超时 | main.py | 30s |
| 进化扫描间隔 | config.py → evolution.scan_interval_hours | 6h |
| 进化分析回溯 | config.py → evolution.analysis_lookback_hours | 24h |
| 进化验证通过率 | config.py → evolution.validation_min_pass_rate | 70% |
| 进化最小任务数 | config.py → evolution.min_tasks_for_analysis | 10 |

### D. 文件索引

| 模块 | 文件 | 职责 |
|------|------|------|
| Agent 工厂 | [app/async_create_agent.py](app/async_create_agent.py) | 组装中间件管线 + create_deep_agent |
| Agent 定义 | [app/agent_definitions.py](app/agent_definitions.py) | SubAgent 发现 + AGENT.md 解析 |
| Triage Prompt | [app/prompts/triage_prompt.py](app/prompts/triage_prompt.py) | Triage system prompt 动态生成 |
| Executor Prompt | [app/prompts/executor_prompt.py](app/prompts/executor_prompt.py) | Executor system prompt 动态生成 |
| 缺口检测 Prompt | [app/prompts/gap_detection.py](app/prompts/gap_detection.py) | GapDetector LLM 分析 prompt |
| Agent 生成 Prompt | [app/prompts/agent_generation.py](app/prompts/agent_generation.py) | SubAgentGenerator LLM 生成 prompt |
| 任务执行器 | [app/harness/task_executor.py](app/harness/task_executor.py) | 后台任务生命周期 + HITL 处理 |
| 事件总线 | [app/harness/event_bus.py](app/harness/event_bus.py) | Agent 间异步通信 |
| 死信队列 | [app/harness/dead_letter.py](app/harness/dead_letter.py) | 失败操作持久化重试 |
| 工具注册 | [app/tools/_registry.py](app/tools/_registry.py) | TOOL_REGISTRY + register_tool |
| 工具资源 | [app/tools/resources.py](app/tools/resources.py) | Moka 客户端 / DLQ / 知识库资源引用 |
| Schema 注册 | [app/schemas/registry.py](app/schemas/registry.py) | JSON Schema → Pydantic 模型 |
| 任务上下文 | [app/task_context.py](app/task_context.py) | 三层记忆 + Journal + 快照 |
| 全链路追踪 | [app/trace_context.py](app/trace_context.py) | contextvars trace_id 传播 |
| 状态回调 | [app/status_handler.py](app/status_handler.py) | Agent 运行时状态 → SSE 事件 |
| 模型网关 | [app/gateway/model_gateway.py](app/gateway/model_gateway.py) | 注册/健康/熔断/路由/热切换 |
| 熔断器 | [app/gateway/circuit_breaker.py](app/gateway/circuit_breaker.py) | 三态熔断器 |
| 探活 | [app/gateway/health_probe.py](app/gateway/health_probe.py) | 后台定期模型 ping |
| 网关中间件 | [app/gateway/gateway_middleware.py](app/gateway/gateway_middleware.py) | 健康感知路由 |
| 审批工具 | [app/tools/approval.py](app/tools/approval.py) | Specialist 侧审批发起 |
| 审批挂起 | [app/tools/request_approval.py](app/tools/request_approval.py) | Executor 侧 interrupt 触发 |
| 进化编排器 | [app/evolution/evolution_manager.py](app/evolution/evolution_manager.py) | 进化周期编排 + 定时扫描 |
| 缺口检测 | [app/evolution/gap_detector.py](app/evolution/gap_detector.py) | Journal 分析 + LLM 缺口识别 |
| Agent 生成 | [app/evolution/agent_generator.py](app/evolution/agent_generator.py) | LLM 生成 AGENT.md |
| 回归验证 | [app/evolution/validator.py](app/evolution/validator.py) | LLM Judge 质量评估 |
| 热加载器 | [app/evolution/hot_reloader.py](app/evolution/hot_reloader.py) | 动态注册 + Git 回滚 |
| 进化状态 | [app/evolution/_state.py](app/evolution/_state.py) | 运行时状态 + Store 持久化 |
| 进化类型 | [app/evolution/types.py](app/evolution/types.py) | GapReport / Proposal / ValidationResult |
| 进化 Admin | [app/evolution/admin_router.py](app/evolution/admin_router.py) | 11 个管理 API 端点 |
