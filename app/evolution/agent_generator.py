"""evolution/agent_generator.py — SubAgent 生成器。

使用 LLM 基于 GapReport 生成符合项目规范的 AGENT.md 文件。

核心职责:
    1. 构建生成上下文（gap + existing agents + TOOL_REGISTRY + reference template）
    2. 调用 LLM 生成 AGENT.md 全文
    3. 验证生成内容的格式正确性
    4. 将 AGENT.md 写入暂存目录（_staging），等待审批后迁移
"""

from __future__ import annotations

import json as _json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.evolution.types import EvolutionProposal, GapReport, ProposalStatus, ProposalType
from app.prompts.agent_generation import AGENT_GENERATION_PROMPT
from app.tools import TOOL_REGISTRY
from app.agent_definitions import discover_specialist_agents, _parse_agent_md

logger = logging.getLogger(__name__)

# 暂存目录（审批通过后才迁移到正式 subagents 目录）
STAGING_DIR = Path(__file__).parent.parent / "subagents" / "_staging"


class SubAgentGenerator:
    """使用 LLM 基于能力缺口生成新的 Specialist SubAgent 定义（AGENT.md）。

    生成流程:
        1. 构建 prompt 上下文（缺口描述 + 现有 Agent + 可用工具 + 参考模板）
        2. LLM 生成 AGENT.md
        3. 格式校验（解析 frontmatter、检查工具白名单）
        4. 写入暂存目录
        5. 创建 EvolutionProposal
    """

    def __init__(self):
        pass

    # ── 主入口 ───────────────────────────────────────────

    async def generate(self, gap: GapReport) -> str:
        """基于缺口报告生成 AGENT.md 内容字符串。

        Args:
            gap: 能力缺口报告

        Returns:
            完整的 AGENT.md 内容（YAML frontmatter + Markdown body）
        """
        # 1. 构建生成上下文
        context = self._build_generation_context(gap)

        # 2. 调用 LLM 生成
        logger.info("[AgentGenerator] 开始生成 Agent: %s", gap.suggested_name or gap.id)
        agent_md = await self._llm_generate(context)

        # 3. 基础校验
        if not agent_md or "---" not in agent_md:
            raise ValueError(f"LLM 生成的 AGENT.md 格式无效")

        # 4. 解析校验
        validation_ok, errors = self._validate_agent_md(agent_md, gap)
        if not validation_ok:
            logger.warning("[AgentGenerator] 生成的 AGENT.md 校验不通过: %s", errors)
            # 不抛异常，让 Validator 做最终判定——因为 LLM 校验可能过于严格

        logger.info("[AgentGenerator] AGENT.md 生成完成 (%d 字符)", len(agent_md))
        return agent_md

    async def create_proposal(self, gap: GapReport, agent_md_content: str) -> EvolutionProposal:
        """将生成的 AGENT.md 包装为 EvolutionProposal。

        写入暂存目录供人工审查，提案状态为 draft。
        """
        # 解析 agent name
        agent_name = gap.suggested_name
        if not agent_name:
            agent_name = self._extract_agent_name_from_md(agent_md_content)

        proposal_id = f"evo-{uuid.uuid4().hex[:8]}"

        proposal = EvolutionProposal(
            id=proposal_id,
            gap_id=gap.id,
            type=ProposalType.NEW_AGENT,
            status=ProposalStatus.DRAFT,
            agent_name=agent_name,
            agent_md_content=agent_md_content,
        )

        # 写入暂存目录
        staging_path = self._write_to_staging(agent_name, agent_md_content)
        logger.info(
            "[AgentGenerator] 提案 %s → 暂存目录: %s",
            proposal_id, staging_path,
        )

        return proposal

    async def refine(self, proposal: EvolutionProposal, feedback: str) -> str:
        """根据反馈迭代改进 AGENT.md。

        用于审批人给出修改意见后重新生成。
        """
        from app.prompts.agent_generation import AGENT_REFINEMENT_PROMPT

        available_tools = self._build_available_tools_text()
        prompt = AGENT_REFINEMENT_PROMPT.format(
            current_agent_md=proposal.agent_md_content,
            feedback=feedback,
            available_tools=available_tools,
        )

        refined = await self._call_llm(prompt)

        if not refined or "---" not in refined:
            raise ValueError("迭代生成的 AGENT.md 格式无效")

        return refined

    # ── 内部方法 ─────────────────────────────────────────

    def _build_generation_context(self, gap: GapReport) -> dict[str, str]:
        """构建 LLM 生成所需的完整上下文。"""
        # 缺口描述
        gap_desc_lines = [
            f"领域: {gap.domain}",
            f"缺口类型: {gap.gap_type}",
            f"问题描述: {gap.description}",
            f"严重程度: {gap.severity}",
            f"建议操作: {gap.suggested_action}",
        ]
        if gap.suggested_spec:
            spec = gap.suggested_spec
            if isinstance(spec, dict):
                gap_desc_lines.append(f"建议规格: {_json.dumps(spec, ensure_ascii=False, indent=2)}")

        return {
            "gap_description": "\n".join(gap_desc_lines),
            "existing_agents": self._build_existing_agents_text(),
            "available_tools": self._build_available_tools_text(),
            "reference_agent_md": self._get_reference_agent_md(),
        }

    async def _llm_generate(self, context: dict[str, str]) -> str:
        """调用 LLM 生成 AGENT.md。"""
        prompt = AGENT_GENERATION_PROMPT.format(
            gap_description=context["gap_description"],
            existing_agents=context["existing_agents"],
            available_tools=context["available_tools"],
            reference_agent_md=context["reference_agent_md"],
            name="{placeholder}",  # LLM 自己决定
        )

        return await self._call_llm(prompt)

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM。"""
        from app.async_load_model import AsyncLoadModel
        from langchain_core.messages import HumanMessage

        try:
            llm = await AsyncLoadModel.async_langchain_api_model("deepseek-v4-flash")
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            content = response.content
            if isinstance(content, list):
                content = "".join(
                    c.get("text", "") if isinstance(c, dict) else str(c)
                    for c in content
                )
            return str(content).strip()
        except Exception as e:
            logger.error("[AgentGenerator] LLM 调用失败: %s", e)
            raise

    def _validate_agent_md(self, content: str, gap: GapReport) -> tuple[bool, list[str]]:
        """校验生成的 AGENT.md 格式是否合法。

        检查项：
        1. YAML frontmatter 可解析
        2. name 字段存在且不冲突
        3. allowed_tools 中的工具都在 TOOL_REGISTRY 中
        4. description 字段存在
        """
        errors = []

        # 1. 尝试用 _parse_agent_md 解析
        try:
            # 写入唯一临时文件再解析（避免并发验证竞争）
            import uuid as _uuid
            temp_dir = STAGING_DIR / f"_validate_{_uuid.uuid4().hex[:8]}"
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp_file = temp_dir / "AGENT.md"
            temp_file.write_text(content, encoding="utf-8")
            spec = _parse_agent_md(temp_file)
            # 清理临时目录
            import shutil as _shutil
            _shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as e:
            errors.append(f"AGENT.md 解析失败: {e}")
            return False, errors

        # 2. 检查必填字段
        if not spec.get("name"):
            errors.append("缺少 name 字段")
        if not spec.get("description"):
            errors.append("缺少 description 字段")

        # 3. 检查名称冲突
        existing = discover_specialist_agents()
        existing_names = {a.get("name", "") for a in existing}
        if spec.get("name") in existing_names:
            errors.append(f"名称 '{spec['name']}' 与已有 Agent 冲突")

        # 4. 检查 allowed_tools 有效性
        allowed = spec.get("allowed_tools", [])
        for tool_name in allowed:
            if tool_name not in TOOL_REGISTRY:
                errors.append(f"工具 '{tool_name}' 不在 TOOL_REGISTRY 中（会在加载时静默忽略）")

        # 5. 检查 system_prompt 非空
        if not spec.get("system_prompt", "").strip():
            errors.append("system prompt body 为空")

        return len(errors) == 0, errors

    @staticmethod
    def _extract_agent_name_from_md(content: str) -> str:
        """从 AGENT.md 内容中提取 agent name。"""
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("name:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
        return f"generated_{uuid.uuid4().hex[:6]}"

    @staticmethod
    def _write_to_staging(agent_name: str, content: str) -> Path:
        """将 AGENT.md 写入暂存目录。"""
        agent_dir = STAGING_DIR / agent_name
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_file = agent_dir / "AGENT.md"
        agent_file.write_text(content, encoding="utf-8")

        # 同时写一个 metadata.json 记录生成时间
        meta_file = agent_dir / "_meta.json"
        meta_file.write_text(
            _json.dumps({
                "agent_name": agent_name,
                "generated_at": datetime.now().isoformat(),
                "staged": True,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return agent_file

    # ── 上下文构建辅助 ──────────────────────────────────

    @staticmethod
    def _build_existing_agents_text() -> str:
        """构建现有 Specialist 列表文本。"""
        try:
            agents = discover_specialist_agents()
        except Exception:
            agents = []

        if not agents:
            return "（无现有 Specialist）"

        lines = []
        for a in agents:
            name = a.get("name", "unknown")
            desc = a.get("description", "")
            tools = a.get("tools", [])
            tool_names = [getattr(t, "name", str(t)) for t in tools] if tools else []
            lines.append(f"- `{name}`: {desc}")
            if tool_names:
                lines.append(f"  工具: {', '.join(tool_names[:5])}")
        return "\n".join(lines)

    @staticmethod
    def _build_available_tools_text() -> str:
        """构建可用工具详细列表文本。"""
        lines = []
        for name, func in sorted(TOOL_REGISTRY.items()):
            doc = getattr(func, "description", "") or ""
            # 取第一段作为摘要
            doc_summary = doc.strip().split("\n")[0][:120] if doc else "（无描述）"
            lines.append(f"- `{name}`: {doc_summary}")
        return "\n".join(lines) if lines else "（无可用工具）"

    @staticmethod
    def _get_reference_agent_md() -> str:
        """获取一个现有 AGENT.md 作为格式参考。"""
        # 优先选一个有完整 output_schema 的 agent 作为参考
        candidates = [
            "recruitment_talent",
            "recruitment_resume",
            "recruitment_interview",
            "recruitment_job",
            "recruitment_offer",
            "recruitment_analytics",
            "recruitment_approval",
            "general",
        ]
        subagents_root = Path(__file__).parent.parent / "subagents"
        for name in candidates:
            ref_file = subagents_root / name / "AGENT.md"
            if ref_file.exists():
                content = ref_file.read_text(encoding="utf-8")
                if len(content) > 200:
                    return content

        return """---
name: example_specialist
description: 示例专员 — 负责某领域的具体操作
allowed_tools: [async_get_current_time, async_web_search]
---

# Example Specialist Agent

## Identity
你是企业的 **示例专员 Agent**。

## 核心能力
### 1. 示例能力
- 使用 `async_get_current_time` 查询时间

## 工作流程
```
用户提问 → 分析需求 → 调用工具 → 返回结果
```

## 约束与边界
1. **只做本分**：你只负责示例操作
"""
