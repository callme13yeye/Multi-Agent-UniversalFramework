# async_create_agent.py — Agent 工厂 + deepagents 管线
# 基于 deepagents.create_deep_agent, 支持原生 sub-agent 编排。
#
# 关键设计：
# - create_deep_agent 自动组装基础中间件栈（Filesystem/Skills/SubAgent/
#   Summarization/Memory/HumanInTheLoop），我们只注入领域定制中间件。
# - subagents 参数传入后，create_deep_agent 自动创建 SubAgentMiddleware
#   并注入 task 工具，让 Router Agent 可以 spawn Specialist Agent。
# - 不传 subagents 时行为与之前一致（向后兼容）。
from typing import Literal, Any, Type, Optional, Sequence
from pydantic import BaseModel

from app.async_load_model import AsyncLoadModel
from deepagents import create_deep_agent, SubAgent, CompiledSubAgent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    ModelFallbackMiddleware,
    ToolRetryMiddleware,
    ModelRetryMiddleware,
)
from deepagents.middleware import SkillsMiddleware
from app.skill_router import SkillRouterMiddleware
from app.planning_middleware import PlanningMiddleware
from deepagents.backends import StateBackend, StoreBackend, CompositeBackend, FilesystemBackend


async def async_create_agent(
        model_name: str,
        toolrouter_model_name: str,
        tools: list,
        system_prompt: str,
        checkpointer: Any,
        store: Any,
        context_schema: Optional[Type[BaseModel]] = None,
        extra_middleware: Optional[Sequence[Any]] = None,
        extra_middleware_position: Literal["prepend", "append"] = "append",
        subagents: Optional[Sequence[SubAgent | CompiledSubAgent]] = None,
):
    """Create a deep agent with the full middleware pipeline.

    Args:
        model_name: Primary chat model name (DeepSeek).
        toolrouter_model_name: Fallback model name (Qwen).
        tools: List of tools available to the agent.
        system_prompt: System instructions for the agent.
        checkpointer: LangGraph checkpointer for state persistence.
        store: LangGraph store for cross-session data.
        context_schema: Pydantic model for run-scoped context.
        extra_middleware: Additional middleware to inject.
        extra_middleware_position: Where to inject extra_middleware.
        subagents: Specialist sub-agent definitions (SubAgent | CompiledSubAgent).
                  When provided, the agent becomes a Router/Supervisor that can
                  spawn domain-specific sub-agents via the ``task`` tool.
    """
    langchain_api_llm = await AsyncLoadModel.async_langchain_api_model(model_name)
    fallback_model = await AsyncLoadModel.async_fallback_api_model(toolrouter_model_name)

    # ── CompositeBackend：统一文件/技能/记忆的后端路由 ──────────
    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            "/memories/": StoreBackend(
                store=store,
                namespace=lambda ctx: ("memories", ctx.context.user_id),
            ),
            "/skills/built-in/": FilesystemBackend(
                root_dir="app/skills",
                virtual_mode=True,
            ),
            "/skills/": StoreBackend(
                store=store,
                namespace=lambda ctx: ("skills", ctx.context.user_id),
            ),
        },
    )

    # ── 领域定制中间件（注入到 create_deep_agent 的 user 层） ──
    # create_deep_agent 自动处理的基础栈：
    #   [TodoList → Skills(如果传了skills参数) → Filesystem
    #    → SubAgent(如果传了subagents) → Summarization
    #    → PatchToolCalls]
    # 我们在之后注入以下定制中间件，然后 create_deep_agent 再追加
    # Memory/HumanInTheLoop 等尾部中间件。
    #
    # 注意: SkillRouter 要在 Skills 之前执行。
    # 我们不传 skills 参数给 create_deep_agent(避免它在基础栈中
    # 创建)，而是手动放入 user_middleware 以保持 SkillRouter 在前的顺序。
    user_middleware: list = [
        # 渐进式披露：根据 query 关键词仅激活相关技能
        SkillRouterMiddleware(),
        # 技能元数据注入 system prompt
        SkillsMiddleware(
            backend=backend,
            sources=["/skills/built-in/", "/skills/"],
        ),
        # CoT 任务规划：复杂任务先规划再执行
        PlanningMiddleware(min_query_length=15),
        # 模型调用限制 — run_limit 对应单个请求内的模型调用次数
        ModelCallLimitMiddleware(
            thread_limit=10,
            run_limit=30,
            exit_behavior="end",
        ),
        # 主模型不可用时自动降级到备用模型 (如 DeepSeek 不可用 → Qwen)
        ModelFallbackMiddleware(first_model=fallback_model),
        # 模型调用失败自动重试 (指数退避)
        ModelRetryMiddleware(
            max_retries=3,
            backoff_factor=2.0,
            initial_delay=1.0,
        ),
        # 全局限制工具调用
        ToolCallLimitMiddleware(thread_limit=50, run_limit=50),
        # 对 web_search 单独限制防止过量 API 调用
        ToolCallLimitMiddleware(
            tool_name="web_search",
            thread_limit=10,
            run_limit=10,
        ),
        # 工具调用失败自动重试 (指数退避)
        ToolRetryMiddleware(
            max_retries=3,
            backoff_factor=2.0,
            initial_delay=1.0,
        ),
    ]

    if extra_middleware:
        if extra_middleware_position == "prepend":
            user_middleware = list(extra_middleware) + user_middleware
        else:
            user_middleware = user_middleware + list(extra_middleware)

    agent = create_deep_agent(
        model=langchain_api_llm,
        tools=tools,
        system_prompt=system_prompt,
        middleware=user_middleware,
        subagents=subagents,
        backend=backend,
        context_schema=context_schema,
        checkpointer=checkpointer,
        store=store,
        memory=["/memories/AGENTS.md"],
    )
    return agent
