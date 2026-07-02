# app/harness — AI 同事执行环境
#
# Harness 层负责让 Agent 脱离 HTTP 请求-响应循环，
# 成为一个能后台自主运行的持续进程。
#
# 组件：
#   event_bus.py              — 事件总线（Agent 间通信 + 外部通知）
#   task_executor.py          — 后台任务执行器（asyncio.Task 驱动 Agent 循环）
#   dead_letter.py            — 死信队列（失败消息重试/归档）
#   task_context.py           — 任务上下文管理器（三层记忆: Hot/Warm/Cold + Journal + 快照）
#   trace_context.py          — 全链路追踪（contextvars trace_id 传播 + 日志注入）
#   status_handler.py         — Agent 运行时状态回调（AsyncCallbackHandler → SSE 事件）
#   tool_hot_reloader.py      — Tool 目录热加载器（watchfiles 监听 app/tools/）
#   subagent_hot_reloader.py  — SubAgent 目录热加载器（watchfiles 监听 app/subagents/）

from app.harness.event_bus import EventBus
from app.harness.task_executor import TaskExecutor, TaskHandle, TaskStatus
from app.harness.dead_letter import DeadLetterQueue, DeadLetterEntry
from app.harness.task_context import TaskContextManager
from app.harness.tool_hot_reloader import ToolHotReloader
from app.harness.subagent_hot_reloader import SubAgentHotReloader

__all__ = [
    "EventBus", "TaskExecutor", "TaskHandle", "TaskStatus",
    "DeadLetterQueue", "DeadLetterEntry",
    "TaskContextManager",
    "ToolHotReloader", "SubAgentHotReloader",
]
