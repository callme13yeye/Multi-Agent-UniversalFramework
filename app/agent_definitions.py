"""
agent_definitions.py — Specialist SubAgent 定义。

每个领域的 Specialist Agent 从 AGENT.md 文件加载角色身份定义，
自动构建 SubAgent TypedDict 供 ``create_deep_agent`` 消费。

使用方式::

    from app.agent_definitions import discover_specialist_agents

    # 工具从 TOOL_REGISTRY 自动获取，按 AGENT.md 的 allowed_tools 分组
    subagents = discover_specialist_agents()
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
from app.tools import TOOL_REGISTRY
from app.schemas import schema_registry

logger = logging.getLogger(__name__)


# ── AGENT.md 解析 ──────────────────────────────────────────

def _parse_agent_md(file_path: Path) -> dict[str, Any]:
    """简易解析 AGENT.md 的 YAML frontmatter + markdown body。

    支持 ``key: value`` 形式的 frontmatter、allowed_tools 列表，
    以及 YAML 多行块标量（``key: |`` / ``key: >``）。
    """
    content = file_path.read_text(encoding="utf-8")

    frontmatter: dict[str, str] = {}
    body_lines: list[str] = []
    in_frontmatter = False
    frontmatter_done = False
    # 多行块标量状态
    _multiline_key: str | None = None
    _multiline_lines: list[str] = []

    for line in content.split("\n"):
        if line.strip() == "---":
            if not in_frontmatter:
                in_frontmatter = True
                continue
            # 结束 frontmatter — 收尾多行值
            if _multiline_key is not None:
                frontmatter[_multiline_key] = "\n".join(_multiline_lines).strip()
                _multiline_key = None
                _multiline_lines = []
            frontmatter_done = True
            continue

        if not frontmatter_done:
            stripped = line.strip()

            # 正在收集多行值
            if _multiline_key is not None:
                # 多行值的行通常有缩进。非缩进且含冒号的行才是新 key
                has_indent = len(line) > 0 and line[0] in (" ", "\t")
                if not has_indent and stripped and ":" in stripped and not stripped.startswith("#") and not stripped.startswith("{"):
                    # 下一个 key — 收尾上一个多行值
                    frontmatter[_multiline_key] = "\n".join(_multiline_lines).strip()
                    _multiline_key = None
                    _multiline_lines = []
                    # fall through to process this line as a new key
                else:
                    _multiline_lines.append(line)
                    continue

            if not stripped or stripped.startswith("#"):
                continue

            if ":" in stripped:
                key, _, value = stripped.partition(":")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                # 检查多行块标量
                if value in ("|", ">", "|-", ">-", "|+"):
                    _multiline_key = key
                    _multiline_lines = []
                else:
                    frontmatter[key] = value
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

    # 解析 output_schema — 支持两种声明方式：
    #   1. 内联 JSON Schema 字符串
    #   2. 引用已注册的 Schema 名称（如 output_schema: talent_search_result）
    output_schema_raw = frontmatter.get("output_schema", "")
    output_schema_model = None
    output_schema_name = ""

    if output_schema_raw:
        stripped = output_schema_raw.strip()
        if stripped.startswith("{"):
            # 内联 JSON Schema
            try:
                import json as _json
                schema_dict = _json.loads(stripped)
                # 使用 specialist name 作为模型名
                schema_name = frontmatter.get("name", file_path.parent.name)
                output_schema_model = schema_registry.from_json_schema(
                    schema_name, schema_dict,
                )
                output_schema_name = schema_name
            except (ValueError, TypeError) as e:
                logger.warning(
                    "[Schema] %s 的 output_schema JSON 解析失败: %s",
                    file_path.parent.name, e,
                )
        else:
            # 引用已注册的 Schema 名称
            output_schema_name = stripped
            output_schema_model = schema_registry.get(output_schema_name)
            if output_schema_model is None:
                logger.warning(
                    "[Schema] %s 引用的 output_schema '%s' 未注册",
                    file_path.parent.name, output_schema_name,
                )

    return {
        "name": frontmatter.get("name", file_path.parent.name),
        "description": frontmatter.get("description", ""),
        "allowed_tools": allowed_tools,
        "system_prompt": body,
        "output_schema_model": output_schema_model,
        "output_schema_name": output_schema_name,
    }


# ── 自动发现 ───────────────────────────────────────────────

def discover_specialist_agents(
    subagents_dir: str | None = None,
) -> list[SubAgent]:
    """扫描 subagents 目录下的 AGENT.md，构建 Specialist SubAgent 列表。

    根据 AGENT.md 中声明的 ``allowed_tools`` 从 TOOL_REGISTRY 获取工具对象，
    每个 Specialist Agent 只拿到自己需要的工具，减少 token 占用。

    Args:
        subagents_dir: SubAgent 根目录（默认自动定位到 app/subagents/）。

    Returns:
        SubAgent 列表，可直接传给 ``create_deep_agent(subagents=...)``。
    """
    if subagents_dir is None:
        subagents_dir = os.path.join(os.path.dirname(__file__), "subagents")

    subagents_path = Path(subagents_dir)
    agents: list[SubAgent] = []

    for child in sorted(subagents_path.iterdir()):
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

            # ── 结构化输出：如果声明了 output_schema，设置 response_format ──
            output_schema_model = spec.get("output_schema_model")
            if output_schema_model is not None:
                agent_def["response_format"] = output_schema_model
                logger.info(
                    "[Schema] Specialist %s 启用结构化输出 — schema=%s",
                    spec["name"],
                    spec.get("output_schema_name", spec["name"]),
                )

            # 从工具注册中心按 allowed_tools 获取工具对象
            allowed_names = spec["allowed_tools"]
            if allowed_names:
                subagent_tools = [
                    TOOL_REGISTRY[name]
                    for name in allowed_names
                    if name in TOOL_REGISTRY
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
                        "Specialist Agent %s 的 allowed_tools 未匹配到任何已注册工具: %s",
                        spec["name"],
                        allowed_names,
                    )
            else:
                logger.info(
                    "发现 Specialist Agent: %s — %s（未声明 allowed_tools，无工具）",
                    spec["name"],
                    spec["description"][:60],
                )
            agents.append(agent_def)
        except Exception as e:
            logger.warning("加载 %s 失败: %s", agent_file.name, e)

    return agents
