"""app/hooks — Agent 级横切关注点 Hook 模块。

使用 LangChain Middleware Hook 机制（@wrap_tool_call、@after_model 等），
在 Agent 的 middleware 管线中注入日志、审计、审批检测等横切逻辑。

与 Harness 层（TaskExecutor）的分工：
   - Hook 层：Agent 内部的横切逻辑（日志、审计）
   - Harness 层：Agent 外部的编排逻辑（progress 同步、事件发布、P0 兜底中断）

新增 SubAgent 时无需任何改动 — Hook 在父 Agent（Triage/Executor）层面自动生效。
"""

from app.hooks.types import HookDependencies, HookRole
from app.hooks.factory import assemble_hooks

__all__ = [
    "HookDependencies",
    "HookRole",
    "assemble_hooks",
]
