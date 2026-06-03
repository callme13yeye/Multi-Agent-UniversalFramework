"""
agent_definitions.py — Specialist SubAgent 定义。

每个领域的 Specialist Agent 从 AGENT.md 文件加载角色身份定义，
自动构建 SubAgent TypedDict 供 ``create_deep_agent`` 消费。

使用方式::

    from app.agent_definitions import discover_specialist_agents

    # tools_map: 工具名 → 工具对象，用于按 AGENT.md 的 allowed_tools 分组
    subagents = discover_specialist_agents(tools_map={
        "async_knowledge_query_ask": knowledge_query_tool,
        "async_web_search": web_search_tool,
        ...
    })
    agent = await async_create_agent(..., subagents=subagents)

路由方式：
    SubAgent 列表通过 SubAgentMiddleware 注入到 system prompt 和
    ``task`` 工具的参数描述中。LLM 通过 Function Calling 自行选择
    合适的 Specialist Agent，无需额外的路由函数或分类器。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from deepagents import SubAgent

logger = logging.getLogger(__name__)


# ── AGENT.md 解析 ──────────────────────────────────────────

def _parse_agent_md(file_path: Path) -> dict[str, Any]:
    """简易解析 AGENT.md 的 YAML frontmatter + markdown body。

    支持 `key: value` 形式的 frontmatter 和 allowed_tools 列表。
    """
    content = file_path.read_text(encoding="utf-8")

    frontmatter: dict[str, str] = {}
    body_lines: list[str] = []
    in_frontmatter = False
    frontmatter_done = False

    for line in content.split("\n"):
        if line.strip() == "---":
            if not in_frontmatter:
                in_frontmatter = True
                continue
            frontmatter_done = True
            continue
        if not frontmatter_done:
            stripped = line.strip()
            if ":" in stripped and not stripped.startswith("#"):
                key, _, value = stripped.partition(":")
                frontmatter[key.strip()] = value.strip().strip('"').strip("'")
        else:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()

    # 解析 allowed_tools 列表
    allowed_str = frontmatter.get("allowed_tools", "[]")
    allowed_tools = [
        t.strip().strip('"').strip("'")
        for t in allowed_str.strip("[]").split(",")
        if t.strip()
    ]

    return {
        "name": frontmatter.get("name", file_path.parent.name),
        "description": frontmatter.get("description", ""),
        "allowed_tools": allowed_tools,
        "system_prompt": body,
    }


# ── 自动发现 ───────────────────────────────────────────────

def discover_specialist_agents(
    skills_dir: str | None = None,
    tools_map: dict[str, Any] | None = None,
) -> list[SubAgent]:
    """扫描 skills 目录下的 AGENT.md，构建 Specialist SubAgent 列表。

    根据 AGENT.md 中声明的 ``allowed_tools`` 精确分配工具——每个
    Specialist Agent 只拿到自己需要的工具，减少上下文中工具描述
    的 token 占用。

    Args:
        skills_dir: skills 根目录（默认自动定位到 app/skills/）。
        tools_map: ``{工具名: 工具对象}`` 映射。为 None 时所有
            SubAgent 继承父 Agent 的全部工具（向后兼容）。

    Returns:
        SubAgent 列表，可直接传给 ``create_deep_agent(subagents=...)``。
    """
    if skills_dir is None:
        skills_dir = os.path.join(os.path.dirname(__file__), "skills")

    skills_path = Path(skills_dir)
    agents: list[SubAgent] = []

    for child in sorted(skills_path.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue

        agent_file = child / "AGENT.md"
        if not agent_file.exists():
            continue

        try:
            spec = _parse_agent_md(agent_file)

            agent_def: SubAgent = {
                "name": spec["name"],
                "description": spec["description"],
                "system_prompt": spec["system_prompt"],
            }

            # 工具分组：按 AGENT.md 的 allowed_tools 精确分配
            allowed_names = spec["allowed_tools"]
            if allowed_names and tools_map:
                subagent_tools = [
                    tools_map[name]
                    for name in allowed_names
                    if name in tools_map
                ]
                if subagent_tools:
                    agent_def["tools"] = subagent_tools
                    logger.info(
                        "发现 Specialist Agent: %s — %s | tools=%s",
                        spec["name"],
                        spec["description"][:60],
                        allowed_names,
                    )
                else:
                    logger.warning(
                        "Specialist Agent %s 的 allowed_tools 未匹配到任何工具，将继承父 Agent 全部工具",
                        spec["name"],
                    )
            else:
                # 未声明 allowed_tools 或未提供 tools_map → 继承父 Agent 全部工具
                logger.info(
                    "发现 Specialist Agent: %s — %s（继承全部工具）",
                    spec["name"],
                    spec["description"][:60],
                )
            agents.append(agent_def)
        except Exception as e:
            logger.warning("加载 %s 失败: %s", agent_file.name, e)

    return agents
