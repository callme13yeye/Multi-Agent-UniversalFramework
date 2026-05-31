"""
PlanningMiddleware — 任务理解与 CoT (Chain-of-Thought) 规划中间件。

工作方式：
  1. 拦截模型调用 (awrap_model_call)
  2. 分析用户问题的复杂度
  3. 对复杂任务：注入一个结构化规划消息，让模型先规划再执行
  4. 简单任务直接放行，不引入额外开销

设计思路：
  - 非侵入式：不修改原有消息，只在 messages 中插入规划节点
  - 渐进式：简单查询不触发规划，节省 token；复杂查询自动触发
  - 可扩展：预留了 ToT 多路径规划的接口 (generate_alternative_paths)

用法：
  middleware_list = [
      ...
      PlanningMiddleware(),
      ...
  ]
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.middleware import TurnAwareMiddleware
from langchain.agents.middleware.types import (
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import AIMessage, SystemMessage

logger = logging.getLogger(__name__)

# ── 复杂度关键词 ──────────────────────────────────────────
_COMPLEX_KEYWORDS = [
    "分析", "对比", "比较", "总结", "规划", "方案",
    "步骤", "流程", "为什么", "原因", "影响",
    "如何实现", "设计方案", "架构", "优化",
    "排查", "故障", "问题分析", "根因",
    "report", "analyze", "compare", "plan",
    "strategy", "roadmap", "design", "architecture",
    "troubleshoot", "root cause", "impact analysis",
]

_SIMPLE_KEYWORDS = [
    "你好", "嗨", "hello", "hi",
    "时间", "天气", "日期",
    "多少", "什么", "是谁",
    "ping", "ip", "地址",
]


def _classify_complexity(query: str) -> str:
    """判断任务复杂度: 'simple' | 'complex' | 'very_complex'"""
    q = query.lower().strip()

    # 空或超短查询 → simple
    if not q or len(q) < 5:
        return "simple"

    # 简单问候/单步查询 → simple
    for kw in _SIMPLE_KEYWORDS:
        if kw in q:
            return "simple"

    # 计算复杂关键词命中数
    complex_hits = sum(1 for kw in _COMPLEX_KEYWORDS if kw in q)

    # 长查询 (>25字) + 多关键词(>=2) → 多步复杂任务
    # 注：中文查询通常较短，25个中文字符已是很长的复合问题
    if len(q) > 25 and complex_hits >= 2:
        return "very_complex"
    if complex_hits >= 1:
        return "complex"
    if len(q) > 20:
        return "complex"

    return "simple"


def _build_plan(query: str, complexity: str, available_tools: list[str]) -> str:
    """根据查询和复杂度生成 CoT 规划文本。"""
    # 操作型任务不走知识库检索规划
    if _is_action_query(query):
        return _build_action_plan(query, complexity)

    tool_hints = _suggest_tools(query, available_tools)

    if complexity == "very_complex":
        return f"""我将按以下分步计划处理这个任务：

## 任务分析
目标：{query}

## 执行计划
1. **信息收集** — 先检索知识库获取基础信息 ({', '.join(tool_hints)})
2. **深度分析** — 基于收集的信息进行分析和推理
3. **补充验证** — 如有必要，联网获取最新信息对比验证
4. **答案合成** — 整合所有信息，给出结构化回答

## 约束
- 知识库没有的内容，我会明确说明"知识库中未找到"
- 涉及数据或事实的，我会标注信息来源"""
    elif complexity == "complex":
        return f"""我将按以下步骤处理：

1. **检索** — 查询知识库获取相关信息 ({', '.join(tool_hints)})
2. **整理** — 整合检索结果，梳理关键点
3. **回答** — 基于已有信息生成回答

如检索结果不足，我会补充联网搜索。"""
    else:
        return ""


# ── 操作型任务关键词 ────────────────────────────────────────────
_ACTION_KEYWORDS = [
    "报销", "申请", "提交", "创建", "审批", "请假",
    "下单", "订购", "购买",
]


def _is_action_query(query: str) -> bool:
    """判断是否是操作性任务（用户要执行操作，而非查询信息）。"""
    for kw in _ACTION_KEYWORDS:
        if kw in query:
            return True
    return False


def _build_action_plan(query: str, complexity: str) -> str:
    """操作性任务的规划（不检索知识库）。"""
    if complexity == "very_complex":
        return f"""我将按以下步骤处理这个任务：

## 执行计划
1. **执行操作** — 使用对应工具直接处理用户请求，无需检索知识库
2. **结果确认** — 将操作结果反馈给用户"""
    return """我将按以下步骤处理：
1. **执行** — 使用对应工具直接处理，无需检索知识库
2. **反馈** — 将结果告知用户"""


def _suggest_tools(query: str, available_tools: list[str]) -> list[str]:
    """根据查询内容建议可能需要的工具。"""
    suggestions = []
    tool_keywords = {
        "knowledge": ["知识", "文档", "政策", "制度", "规定", "手册", "指南", "knowledg", "rag"],
        "web": ["新闻", "最新", "天气", "今天", "实时", "热点", "202", "news", "weather", "current"],
        "time": ["时间", "日期", "现在", "time", "date", "星期"],
    }
    for tool_type, keywords in tool_keywords.items():
        for kw in keywords:
            if kw in query.lower():
                if tool_type == "knowledge" and "async_knowledge_query_ask" in available_tools:
                    suggestions.append("知识库查询")
                elif tool_type == "web" and "async_web_search" in available_tools:
                    suggestions.append("联网搜索")
                elif tool_type == "time" and "async_get_current_time" in available_tools:
                    suggestions.append("时间查询")
                break
    return suggestions or ["知识库查询"]


class PlanningMiddleware(TurnAwareMiddleware):
    """
    任务规划中间件。

    在模型调用前分析用户任务复杂度，对复杂任务注入 CoT 规划，
    引导模型先规划再执行（Plan-then-Execute）。
    """

    state_schema: type[AgentState[Any]] = AgentState

    # 可配置：最大消息长度才触发规划（节省简单查询的 token）
    min_query_length: int = 15

    def __init__(self, min_query_length: int = 15):
        self.min_query_length = min_query_length

    def _get_tool_names(self, request: ModelRequest) -> list[str]:
        """从 request 中提取可用工具名列表。"""
        names = []
        for tool in getattr(request, "tools", []) or []:
            if hasattr(tool, "name"):
                names.append(tool.name)
            elif isinstance(tool, dict):
                names.append(tool.get("name", ""))
        return [n for n in names if n]

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT] | AIMessage:
        """核心逻辑：分析任务 → 注入规划 → 执行模型。"""
        # 仅在收到用户输入后的首次模型调用时注入规划，
        # 避免工具调用后模型合成阶段重复注入。
        if not self._is_first_call_after_human_input(request.messages):
            return await handler(request)

        query = self._extract_query(request.messages)

        # 短查询不触发规划（节省 token 和延迟）
        if len(query) < self.min_query_length:
            return await handler(request)

        complexity = _classify_complexity(query)
        logger.debug(
            "PlanningMiddleware: query=%.50s | complexity=%s | len=%d",
            query, complexity, len(query),
        )

        # 简单任务直接放行
        if complexity == "simple":
            return await handler(request)

        # 复杂任务：生成规划并注入 messages
        tool_names = self._get_tool_names(request)
        plan_text = _build_plan(query, complexity, tool_names)

        if not plan_text:
            return await handler(request)

        # 注入规划消息（作为 assistant 的思考过程）
        plan_message = AIMessage(
            content=plan_text,
            additional_kwargs={"role": "planning", "complexity": complexity},
        )

        # 创建新的 messages 列表，在 user 消息后插入规划
        new_messages = list(request.messages)
        new_messages.append(plan_message)

        # 追加 system prompt 引导：告诉模型要按规划执行
        planning_instruction = (
            "\n\n【规划指令】\n"
            "以上是任务执行计划。请严格按照计划逐步执行，每步完成后标记完成。"
            "如果某一步依赖工具调用结果，等待工具返回后再继续下一步。"
        )
        new_system = (
            SystemMessage(
                content=(request.system_message.text if request.system_message else "")
                + planning_instruction
            )
            if request.system_message
            else SystemMessage(content=planning_instruction)
        )

        new_request = request.override(
            messages=new_messages,
            system_message=new_system,
        )

        logger.info(
            "CoT 规划已注入 | complexity=%s | 工具=%s",
            complexity, tool_names,
        )
        return await handler(new_request)

    # 同步版本保底
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        # 仅在收到用户输入后的首次模型调用时注入规划，
        # 避免工具调用后模型合成阶段重复注入。
        if not self._is_first_call_after_human_input(request.messages):
            return handler(request)

        query = self._extract_query(request.messages)
        if len(query) < self.min_query_length:
            return handler(request)
        complexity = _classify_complexity(query)
        if complexity == "simple":
            return handler(request)
        tool_names = self._get_tool_names(request)
        plan_text = _build_plan(query, complexity, tool_names)
        if not plan_text:
            return handler(request)
        plan_message = AIMessage(
            content=plan_text,
            additional_kwargs={"role": "planning", "complexity": complexity},
        )
        new_messages = list(request.messages)
        new_messages.append(plan_message)
        planning_instruction = (
            "\n\n【规划指令】\n"
            "以上是任务执行计划。请严格按照计划逐步执行，每步完成后标记完成。"
            "如果某一步依赖工具调用结果，等待工具返回后再继续下一步。"
        )
        new_system = (
            SystemMessage(
                content=(request.system_message.text if request.system_message else "")
                + planning_instruction
            )
            if request.system_message
            else SystemMessage(content=planning_instruction)
        )
        new_request = request.override(
            messages=new_messages,
            system_message=new_system,
        )
        return handler(new_request)
