# app/harness — AI 同事执行环境
#
# Harness 层负责让 Agent 脱离 HTTP 请求-响应循环，
# 成为一个能后台自主运行的持续进程。
#
# 组件：
#   event_bus.py      — 事件总线（Agent 间通信 + 外部通知）
#   task_executor.py  — 后台任务执行器（asyncio.Task 驱动 Agent 循环）

from app.harness.event_bus import EventBus
from app.harness.task_executor import TaskExecutor, TaskHandle, TaskStatus
from app.harness.dead_letter import DeadLetterQueue, DeadLetterEntry

__all__ = [
    "EventBus", "TaskExecutor", "TaskHandle", "TaskStatus",
    "DeadLetterQueue", "DeadLetterEntry",
]
