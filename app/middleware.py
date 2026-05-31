"""TurnAwareMiddleware — 只应在用户输入后首次模型调用时执行的中间件基类。

解决的问题：LangGraph Agent 循环中 model_node 会多次触发，
每次都会调用所有中间件的 wrap_model_call / awrap_model_call。
对于只需要在收到用户消息后执行一次的操作（如 CoT 规划注入、技能路由），
继承此基类即可自动获得"仅首次调用时执行"的行为。

子类只需：
1. 继承 TurnAwareMiddleware 而非 AgentMiddleware
2. 在 wrap_model_call / awrap_model_call 中使用
   self._is_first_call_after_human_input() 判断是否应执行，
   以及 self._extract_query() 提取用户查询
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ResponseT,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


class TurnAwareMiddleware(AgentMiddleware[AgentState[Any], ContextT, ResponseT]):
    """只应在用户输入后首次模型调用时执行的中间件基类。

    提供两个工具方法供子类使用：
    - _extract_query: 从消息列表中提取用户最新查询
    - _is_first_call_after_human_input: 检测是否为用户输入后的首次模型调用
    """

    def _extract_query(self, messages: list) -> str:
        """从消息列表中提取用户最新一条有效查询（跳过 AI 回复和工具返回）。"""
        for msg in reversed(messages):
            if isinstance(msg, (AIMessage, ToolMessage)):
                continue
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                texts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                full = "".join(texts).strip()
                if full:
                    return full
        return ""

    def _is_first_call_after_human_input(self, messages: list) -> bool:
        """检测当前是否为收到用户输入后的首次模型调用。

        从消息列表末尾向前扫描，跳过 SystemMessage，
        遇到的第一条非系统消息如果是 HumanMessage → 首次调用；
        如果是 AIMessage/ToolMessage → 循环中的后续调用。
        """
        for msg in reversed(messages):
            if isinstance(msg, SystemMessage):
                continue
            return isinstance(msg, HumanMessage)
        return False
