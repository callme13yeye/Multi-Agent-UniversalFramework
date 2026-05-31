"""
agent_definitions.py — Specialist SubAgent 定义。

每个领域的 Specialist Agent 从 AGENT.md 文件加载角色身份定义，
自动构建 SubAgent TypedDict 供 ``create_deep_agent`` 消费。

使用方式::

    from app.agent_definitions import discover_specialist_agents

    subagents = discover_specialist_agents(all_tools)
    agent = await async_create_agent(..., subagents=subagents)
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
) -> list[SubAgent]:
    """扫描 skills 目录下的 AGENT.md，构建 Specialist SubAgent 列表。

    Args:
        skills_dir: skills 根目录（默认自动定位到 app/skills/）。

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

            # 不显式传 tools → 继承父 Agent 的全部工具
            # 不传 model → 继承父 Agent 的模型
            # 不传 middleware → create_deep_agent 构建默认中间件
            agent_def: SubAgent = {
                "name": spec["name"],
                "description": spec["description"],
                "system_prompt": spec["system_prompt"],
            }
            agents.append(agent_def)
            logger.info(
                "发现 Specialist Agent: %s — %s",
                spec["name"],
                spec["description"][:60],
            )
        except Exception as e:
            logger.warning("加载 %s 失败: %s", agent_file.name, e)

    return agents


# ── 构建 Router System Prompt ─────────────────────────────

def build_router_system_prompt(
    base_prompt: str,
    subagents: list[SubAgent],
) -> str:
    """在基础 system prompt 后追加可用 Specialist Agent 列表。"""
    if not subagents:
        return base_prompt

    agent_lines = "\n".join(
        f"- `{a['name']}`: {a['description']}"
        for a in subagents
    )
    router_instructions = f"""

## 可用 Specialist Agent

你可以使用 ``task`` 工具将任务委托给以下 Specialist Agent：

{agent_lines}

**使用规则：**
- 当用户问题明确属于某个领域时，委托给对应的 Specialist Agent 处理
- 当问题跨领域时，选择最相关的 Specialist Agent
- 简单问题（问时间、打招呼、通用知识）由你自己直接回答，无需委托
- 委托后，将 Specialist Agent 的返回结果转达给用户
"""
    return base_prompt + router_instructions
