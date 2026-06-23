"""evolution/validator.py — 回归验证器。

对生成的 SubAgent 定义进行验证：
1. 从历史任务 journal 中提取相关测试用例
2. 用 LLM Judge 评估新 Agent 在这些用例上的表现
3. 产出 ValidationResult

验证策略（MVP 版）：
    - 不实际调用外部 API（避免副作用）
    - 用 LLM Judge 模拟评估：给定用户输入 + Agent 定义 + 预期行为，LLM 判断 Agent 是否能正确处理
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any, TYPE_CHECKING

from app.evolution.types import EvolutionProposal, GapReport, ValidationResult
from app.agent_definitions import _parse_agent_md

if TYPE_CHECKING:
    from langgraph.store.postgres.aio import AsyncPostgresStore
    from app.harness.event_bus import EventBus

logger = logging.getLogger(__name__)

# LLM Judge 评估 prompt
JUDGE_PROMPT = """你是一个 Multi-Agent 系统的质量评估专家。评估一个新生成的 Specialist Agent 定义是否能够正确处理给定的用户输入。

## Agent 定义（AGENT.md）
```
{agent_md}
```

## 测试用例
**用户输入**: {user_input}
**预期行为**: {expected_behavior}

## 评估维度
1. **职责匹配** (40%): 这个 Agent 的职责范围是否覆盖了这个用户输入？
2. **工具可用性** (30%): Agent 拥有的工具是否能完成这个任务？（不需要实际调用，只检查工具功能是否匹配）
3. **行为指引** (30%): Agent 的 system prompt 是否能引导它正确处理这个输入？

## 输出格式
```json
{{
  "passed": true/false,
  "score": 0.0-1.0,
  "reason": "评估理由（中文，50-150字）",
  "dimensions": {{
    "responsibility_match": 0.0-1.0,
    "tool_availability": 0.0-1.0,
    "behavior_guidance": 0.0-1.0
  }}
}}
```
"""


class Validator:
    """回归验证器 — 检测新 SubAgent 定义的质量。

    MVP 版本使用 LLM Judge 进行模拟评估（不实际调用外部 API）。
    进阶版可接入 offline_eval_agent 的评价管线进行端到端测试。
    """

    def __init__(
        self,
        store: "AsyncPostgresStore",
        event_bus: "EventBus",
    ):
        self.store = store
        self.event_bus = event_bus

    # ── 主入口 ───────────────────────────────────────────

    async def validate_agent(
        self,
        proposal: EvolutionProposal,
        test_cases: list[dict] | None = None,
    ) -> ValidationResult:
        """验证一个新的 SubAgent 定义。

        Args:
            proposal: 进化提案（含 agent_md_content）
            test_cases: 测试用例列表 [{user_input, expected_behavior}]，不传则从历史数据提取

        Returns:
            ValidationResult
        """
        # 1. 获取测试用例
        if test_cases is None:
            test_cases = await self.extract_test_cases(proposal)

        if not test_cases:
            logger.info("[Validator] 无可用测试用例，跳过验证")
            return ValidationResult(
                passed=True,
                total_tests=0,
                passed_tests=0,
                summary="无可用测试用例，跳过验证",
            )

        logger.info("[Validator] 开始验证 %s — %d 个测试用例", proposal.id, len(test_cases))

        # 2. 对每个测试用例运行 LLM Judge
        details = []
        passed_count = 0

        for i, tc in enumerate(test_cases):
            try:
                result = await self._llm_judge(
                    agent_md=proposal.agent_md_content,
                    user_input=tc.get("user_input", tc.get("input", "")),
                    expected_behavior=tc.get("expected_behavior", tc.get("expected_outcome", "")),
                )
                details.append({
                    "test_id": f"test-{i:03d}",
                    "user_input": tc.get("user_input", tc.get("input", "")),
                    "expected_behavior": tc.get("expected_behavior", tc.get("expected_outcome", "")),
                    "passed": result.get("passed", False),
                    "score": result.get("score", 0.0),
                    "reason": result.get("reason", ""),
                })
                if result.get("passed"):
                    passed_count += 1
            except Exception as e:
                logger.warning("[Validator] 测试用例 %d 评估失败: %s", i, e)
                details.append({
                    "test_id": f"test-{i:03d}",
                    "passed": False,
                    "score": 0.0,
                    "reason": f"评估异常: {e}",
                })

        # 3. 汇总结果
        total = len(test_cases)
        pass_rate = passed_count / total if total > 0 else 0.0
        passed = pass_rate >= 0.6  # MVP 阶段：60% 通过率即视为通过

        summary = (
            f"验证完成: {passed_count}/{total} 通过 ({pass_rate:.0%})"
        )
        if not passed:
            summary += f" — 未达到通过阈值 (60%)"

        validation = ValidationResult(
            passed=passed,
            total_tests=total,
            passed_tests=passed_count,
            failed_tests=total - passed_count,
            pass_rate=pass_rate,
            summary=summary,
            details=details,
        )

        logger.info("[Validator] %s %s", proposal.id, summary)
        return validation

    async def extract_test_cases(
        self,
        proposal: EvolutionProposal,
        limit: int = 10,
    ) -> list[dict]:
        """从历史任务 journal 中提取与新 Agent 相关的测试用例。

        策略：
        1. 扫描 task_results，找 goal 与 gap 描述语义相近的任务
        2. 从这些任务的 journal 中提取用户输入和预期行为
        3. 如果没有历史数据，用 LLM 基于 GapReport 合成几个代表性用例
        """
        test_cases = []

        # ── 尝试从历史任务中提取 ──
        try:
            items = await self.store.asearch(("task_results",), limit=100)
        except Exception:
            items = []

        for item in items:
            if not item.value or len(test_cases) >= limit:
                break

            data = item.value
            goal = data.get("goal", "")
            task_id = data.get("task_id", "")

            # 简单相关性判断：关键词重叠
            if not self._is_related(goal, proposal):
                continue

            # 从 task_results 提取用户提问
            result_summary = data.get("result_summary", "")[:200]

            test_cases.append({
                "user_input": goal,
                "expected_behavior": result_summary or f"成功完成: {goal[:100]}",
                "source": f"task:{task_id}",
            })

        # ── 如果历史数据不够，用 LLM 合成测试用例 ──
        if len(test_cases) < 3:
            synthetic = await self._synthesize_test_cases(proposal, min(3, limit))
            test_cases.extend(synthetic)

        return test_cases[:limit]

    # ── 内部方法 ─────────────────────────────────────────

    async def _llm_judge(
        self,
        agent_md: str,
        user_input: str,
        expected_behavior: str,
    ) -> dict:
        """LLM Judge 评估单个测试用例。"""
        prompt = JUDGE_PROMPT.format(
            agent_md=agent_md,
            user_input=user_input,
            expected_behavior=expected_behavior,
        )

        result_text = await self._call_llm(prompt)
        if not result_text:
            return {"passed": False, "score": 0.0, "reason": "LLM 评估失败"}

        try:
            return _json.loads(result_text)
        except _json.JSONDecodeError:
            # 尝试提取 JSON
            start = result_text.find("{")
            end = result_text.rfind("}")
            if start != -1 and end != -1:
                try:
                    return _json.loads(result_text[start:end + 1])
                except _json.JSONDecodeError:
                    pass
            return {
                "passed": "true" in result_text.lower() and "passed" in result_text.lower(),
                "score": 0.5,
                "reason": result_text[:200],
            }

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
            logger.error("[Validator] LLM 调用失败: %s", e)
            return ""

    @staticmethod
    def _is_related(goal: str, proposal: EvolutionProposal) -> bool:
        """简单判断一个任务目标是否与新 Agent 相关。

        通过关键词重叠进行粗筛。
        """
        if not goal:
            return False

        goal_lower = goal.lower()
        # 从 agent_name 中提取关键词
        name_parts = proposal.agent_name.replace("_", " ").split()
        overlap = sum(1 for part in name_parts if part.lower() in goal_lower)
        return overlap >= 1 or proposal.agent_name[:6].lower() in goal_lower

    async def _synthesize_test_cases(
        self,
        proposal: EvolutionProposal,
        count: int = 3,
    ) -> list[dict]:
        """用 LLM 合成代表性测试用例。"""
        prompt = f"""根据以下新 Agent 的定义，生成 {count} 个典型用户输入作为测试用例。

## Agent 定义
{proposal.agent_md_content[:2000]}

## 输出格式
```json
[
  {{"user_input": "用户可能问的问题", "expected_behavior": "Agent 应该如何响应"}},
  ...
]
```

只返回 JSON 数组。"""

        result = await self._call_llm(prompt)
        if not result:
            return []

        try:
            return _json.loads(result)
        except _json.JSONDecodeError:
            # 容错提取
            start = result.find("[")
            end = result.rfind("]")
            if start != -1 and end != -1:
                try:
                    return _json.loads(result[start:end + 1])
                except _json.JSONDecodeError:
                    pass
            return []
