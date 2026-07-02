"""app/status_handler.py — Agent 运行时状态回调处理器

通过 LangChain 的 AsyncCallbackHandler 捕获 Agent 执行过程中的
关键生命周期事件，通过 asyncio.Queue 送入 SSE 流，实现前端
实时展示"思考中/知识检索中/生成回答中"等状态。

使用方式（在 generate_response_stream 中）:
    queue = asyncio.Queue()
    handler = StatusCallbackHandler(queue)
    configurable["callbacks"].append(handler)
    # 然后在多路复用循环中消费 queue

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
状态一览（对照 Agent设计思路.md 第四节）:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  executing    — 执行中（Agent 开始执行）
  thinking     — 思考中（LLM 首次推理）
  planning     — 规划中（TodoList 中间件触发）
  retrieving   — 知识检索中（knowledge_query 工具）
  searching    — 联网搜索中（web_search 工具）
  skill_calling— 技能调用中（task → SubAgent）
  tool_calling — 工具调用中（其他工具）
  summarizing  — 上下文压缩中（Summarization 中间件触发）
  generating   — 生成回答中（LLM 处理工具结果后输出文本）
  retrying     — 重试中（模型/工具重试）
  degraded     — 已降级备用模型
  restored     — 已恢复主模型
  cancelled    — 用户取消
  timeout      — 执行超时
  completed    — 完成（终态）
  error        — 异常终止

每个事件格式:
  {"_type": "status", "status": "<状态名>", "label": "<中文描述>",
   "elapsed_ms": <从 executing 起的毫秒数>}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import contextvars
import json
import logging
import time
from typing import Any, Optional

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult
from tenacity import RetryCallState

logger = logging.getLogger(__name__)

# 当前请求的 StatusCallbackHandler，供 GatewayMiddleware 跨层访问
current_handler: contextvars.ContextVar[Optional["StatusCallbackHandler"]] = (
    contextvars.ContextVar("status_handler", default=None)
)


class StatusCallbackHandler(AsyncCallbackHandler):
    """捕获 LangGraph 执行过程中的关键事件，推入 asyncio.Queue。

    每个事件被序列化为 JSON 字符串，格式:
      {"_type": "status", "status": "<状态名>", "label": "<中文描述>",
       "elapsed_ms": <从 executing 起的毫秒数>}

    队列消费者在 generate_response_stream 中，与 content 流多路复用。
    """

    # ── 用于检测 Summarization / TodoList 中间件的链名关键字 ──
    _SUMMARIZE_KEYWORDS = ("summar", "compress", "condense")
    _PLAN_KEYWORDS = ("todo", "plan")

    def __init__(self, queue: asyncio.Queue):
        """初始化状态回调处理器。

        Args:
            queue: 用于推送状态事件的异步队列
        """
        super().__init__()
        self.queue = queue
        self._start_time: Optional[float] = None  # 在 emit_executing() 时设置
        # 是否已调用过工具（用于区分直接回答 vs 工具辅助回答）
        self._tool_ever_called = False
        # 当前正在执行的工具数（支持并行工具调用）
        self._tool_count = 0
        # 模型降级标记（用于 degraded / restored 配对）
        self._degraded = False
        # 是否正在执行上下文压缩
        self._summarizing = False

    # ── 内部方法 ────────────────────────────────────────────

    async def _put(self, status: str, label: str):
        """推送一条状态事件到队列。"""
        event: dict[str, Any] = {
            "_type": "status",
            "status": status,
            "label": label,
        }
        if self._start_time is not None:
            event["elapsed_ms"] = int((time.monotonic() - self._start_time) * 1000)
        await self.queue.put(json.dumps(event))

    @staticmethod
    def _extract_chain_name(serialized: Any) -> str:
        """从 on_chain_start 的 serialized 参数中提取链名。"""
        if isinstance(serialized, dict):
            name = serialized.get("name", "")
            if name:
                return name
            id_val = serialized.get("id", "")
            if isinstance(id_val, list):
                return ".".join(str(x) for x in id_val)
            return str(id_val)
        if isinstance(serialized, str):
            return serialized
        return ""

    @staticmethod
    def _extract_subagent_name(input_str: str) -> str:
        """从 task 工具的 input_str 中提取 SubAgent 名称。"""
        try:
            data = json.loads(input_str) if isinstance(input_str, str) else input_str
            if isinstance(data, dict):
                return data.get("agent_name", data.get("name", "SubAgent"))
        except (json.JSONDecodeError, TypeError):
            pass
        if isinstance(input_str, str):
            for prefix in ("agent_name=", "name="):
                if prefix in input_str:
                    parts = input_str.split(prefix)
                    if len(parts) > 1:
                        return parts[1].split(",")[0].split(")")[0].strip()
        return "SubAgent"

    # ── 公开方法（供外部调用）────────────────────────────────

    async def emit_executing(self):
        """Agent 开始执行。应在 agent.astream() 调用前触发。"""
        self._start_time = time.monotonic()
        await self._put("executing", "执行中...")

    async def emit_degraded(self):
        """模型已降级到备用模型。"""
        if not self._degraded:
            self._degraded = True
            await self._put("degraded", "已降级备用模型")

    async def emit_restored(self):
        """模型已恢复主模型（降级后的首次成功调用主模型）。"""
        if self._degraded:
            self._degraded = False
            await self._put("restored", "已恢复主模型")

    async def emit_completed(self):
        """Agent 执行完成。"""
        await self._put("completed", "完成")

    async def emit_cancelled(self):
        """用户取消执行（连接断开 / 主动取消）。"""
        await self._put("cancelled", "已取消")

    async def emit_timeout(self):
        """执行超时。"""
        await self._put("timeout", "执行超时")

    async def emit_error(self, label: str = "执行异常"):
        """通用异常终止。"""
        await self._put("error", label)

    # ── LangChain 回调：Chain 层 ────────────────────────────

    async def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """链开始执行 — 检测 Summarization / TodoList 中间件。"""
        name = self._extract_chain_name(serialized)
        if not name:
            return

        name_lower = name.lower()

        # 检测上下文压缩（SummarizationMiddleware）
        if not self._summarizing and any(kw in name_lower for kw in self._SUMMARIZE_KEYWORDS):
            self._summarizing = True
            await self._put("summarizing", "上下文压缩中...")
            return

        # 检测规划（TodoListMiddleware）
        if any(kw in name_lower for kw in self._PLAN_KEYWORDS):
            await self._put("planning", "规划中...")
            return

    async def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **kwargs: Any,
    ) -> None:
        """链结束 — 标记压缩完成，下一个模型调用将恢复正常状态。"""
        if self._summarizing:
            self._summarizing = False

    # ── LangChain 回调：LLM 层 ─────────────────────────────

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """LLM 开始推理。

        状态逻辑:
          - 首次推理（未调用过工具）→ "思考中..."
          - 已调用过工具后再次推理 → "生成回答中..."
          - 上下文压缩中的 LLM 调用不覆盖 summarizing 状态
        """
        # 如果正在压缩中，不覆盖 summarizing 状态
        if self._summarizing:
            return

        if self._tool_count == 0:
            if self._tool_ever_called:
                await self._put("generating", "生成回答中...")
            else:
                await self._put("thinking", "思考中...")

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **kwargs: Any,
    ) -> None:
        """LLM 推理结束 → 如果这是最终回答（无工具调用），推"生成回答中"。"""
        if not self._tool_ever_called or self._summarizing:
            return

        try:
            generation = response.generations[0][0]
            msg = getattr(generation, "message", None)
            if msg is not None:
                has_tool_calls = bool(getattr(msg, "tool_calls", None))
                has_content = bool(getattr(msg, "content", None))
                if has_content and not has_tool_calls:
                    await self._put("generating", "生成回答中...")
        except (AttributeError, IndexError, KeyError) as e:
            logger.debug("on_llm_end 解析 response 失败: %s", e)

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **kwargs: Any,
    ) -> None:
        """LLM 调用出错 — 按错误类型推送不同状态。"""
        error_str = str(error).lower()
        if "timeout" in error_str or "timed out" in error_str:
            await self._put("timeout", "模型调用超时")
        elif "cancel" in error_str:
            await self._put("cancelled", "已取消")
        # 其他错误由 ModelRetryMiddleware / GatewayMiddleware 处理，
        # 此处不额外推送，避免干扰重试/降级流程

    # ── LangChain 回调：Tool 层 ────────────────────────────

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """工具开始执行 → 按工具类型推不同状态。"""
        self._tool_ever_called = True
        self._tool_count += 1

        name = serialized.get("name", "unknown")
        if name == "async_knowledge_query_ask":
            await self._put("retrieving", "知识检索中...")
        elif name == "async_web_search":
            await self._put("searching", "联网搜索中...")
        elif name == "task":
            agent_name = self._extract_subagent_name(input_str)
            await self._put("skill_calling", f"技能调用中（{agent_name}）")
        else:
            await self._put("tool_calling", "工具调用中...")

    async def on_tool_end(
        self,
        output: str,
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **kwargs: Any,
    ) -> None:
        """工具执行完成 → 减少计数器。"""
        self._tool_count -= 1

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **kwargs: Any,
    ) -> None:
        """工具执行出错 → 减少计数器（ToolRetryMiddleware 会处理重试）。"""
        self._tool_count -= 1
        error_str = str(error).lower()
        if "timeout" in error_str or "timed out" in error_str:
            await self._put("timeout", "工具调用超时")

    # ── LangChain 回调：Retry 层 ───────────────────────────

    async def on_retry(
        self,
        retry_state: RetryCallState,
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        **kwargs: Any,
    ) -> None:
        """模型/工具重试 → 推"重试中"。"""
        attempt = retry_state.attempt_number
        await self._put("retrying", f"重试中（第 {attempt} 次）")
