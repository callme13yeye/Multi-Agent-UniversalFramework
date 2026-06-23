"""trace_id 全链路追踪 — contextvars 驱动的链路ID传播。

trace_id 在请求入口（/chat）生成，通过 contextvars 自动传播到
所有工具调用、Agent 执行、事件发布、日志输出中，无需显式传参。

设计原则：
    - trace_id 在 HTTP 层生成，绑定到请求生命周期
    - 后台任务从原始请求继承 trace_id，并追加 task_id 作为子链路
    - 所有事件自动携带 trace_id，方便跨服务日志关联

链路上下文格式：
    前台请求:    trace-{uuid16}
    后台任务:    trace-{uuid16}/task-{task_id_short}

使用方式::

    from app.trace_context import TraceContext

    ctx = TraceContext()
    trace_id = ctx.start_trace()        # 请求入口
    ctx.set_task_context(task_id)       # 后台任务继承
    current = ctx.current               # 任意位置读取
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# contextvar — 每个 asyncio task 自动隔离
_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default=""
)


class TraceContext:
    """trace_id 生命周期管理器。

    单例模式 — 所有模块共享同一个 contextvar。
    """

    def start_trace(self) -> str:
        """为新请求生成 trace_id。在 /chat 端点入口调用。

        Returns:
            格式为 trace-{16位hex} 的追踪ID
        """
        trace_id = f"trace-{uuid.uuid4().hex[:16]}"
        _trace_id.set(trace_id)
        return trace_id

    def set_task_context(self, task_id: str) -> str:
        """后台任务继承请求 trace_id。

        在 _execute_loop 中调用，将当前 trace_id 追加 task_id。
        格式: trace-{uuid16}/task-{task_id_short}

        Args:
            task_id: 后台任务ID
        """
        parent = _trace_id.get()
        if parent:
            new_trace = f"{parent}/task-{task_id[-8:]}"
        else:
            new_trace = f"trace-{uuid.uuid4().hex[:16]}/task-{task_id[-8:]}"
        _trace_id.set(new_trace)
        return new_trace

    @property
    def current(self) -> str:
        """获取当前 trace_id（可在任何地方读取）。"""
        return _trace_id.get()

    @staticmethod
    def inject_to_config(config: dict) -> dict:
        """将 trace_id 注入 LangGraph config，供 checkpointer 和工具使用。

        Args:
            config: LangGraph 的 config dict
        Returns:
            注入了 trace_id 的 config dict
        """
        trace_id = _trace_id.get()
        if trace_id and "configurable" in config:
            config["configurable"]["trace_id"] = trace_id
        return config

    @staticmethod
    def inject_to_metadata(metadata: dict) -> dict:
        """将 trace_id 注入 metadata dict（Langfuse 等外部系统使用）。

        Args:
            metadata: 元数据 dict
        Returns:
            注入了 trace_id 的 dict
        """
        trace_id = _trace_id.get()
        if trace_id:
            metadata["trace_id"] = trace_id
        return metadata


# 全局单例
trace_context = TraceContext()


# ── 日志过滤器 — 自动为每条日志添加 trace_id ─────────

class TraceIdFilter(logging.Filter):
    """将当前 trace_id 注入到日志记录的 trace_id 字段中。"""

    def filter(self, record):
        tid = _trace_id.get()
        record.trace_id = tid if tid else "-"
        return True
