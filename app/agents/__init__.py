# app/agents — Agent 工厂与定义
#
# 负责 Agent 的创建、发现和初始化：
#   - SubAgent 发现（AGENT.md 扫描解析）
#   - DeepAgent 工厂（中间件管线组装）
#   - 用户技能/记忆初始化
#
# 组件：
#   agent_definitions.py            — Specialist SubAgent 发现 + AGENT.md 解析
#   async_create_agent.py           — Agent 工厂（deepagents 中间件管线）
#   async_ensure_user_skills_init.py — 用户技能/记忆初始化

from app.agents.agent_definitions import discover_specialist_agents
from app.agents.async_create_agent import async_create_agent
from app.agents.async_ensure_user_skills_init import ensure_user_skills_init

__all__ = [
    "discover_specialist_agents",
    "async_create_agent",
    "ensure_user_skills_init",
]
