# app/prompts/ — Prompt 模板集中管理
#
# 所有 System Prompt 模板按域分文件，集中在此目录。
# 新增 prompt 只需加新文件并从 __init__ 导出。
#
# 目录结构:
#   triage_prompt.py    — DeepAgent Triage 层 system prompt（分流判断）
#   executor_prompt.py  — Executor DeepAgent system prompt（后台任务执行）

from app.prompts.triage_prompt import build_triage_prompt
from app.prompts.executor_prompt import build_executor_prompt

__all__ = [
    # DeepAgent Triage 层
    "build_triage_prompt",
    # Executor DeepAgent 后台执行层
    "build_executor_prompt",
]
