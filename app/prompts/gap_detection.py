"""evolution/prompts/gap_detection.py — GapDetector 的 LLM prompt 模板。

GapDetector 将任务执行日志（journal、task_results、错误摘要）作为上下文，
让 LLM 分析其中反映的系统能力缺口。
"""

# ── 单任务分析 prompt ───────────────────────────────────

SINGLE_TASK_ANALYSIS_PROMPT = """你是一个 Multi-Agent 系统的能力分析师。分析以下后台任务的执行日志，判断是否存在系统能力缺口。

## 分析维度

1. **缺少合适的 Specialist**：是否存在某个子任务没有合适的 Specialist Agent 可以委托？
   - 判定标准：Executor LLM 尝试委托但失败 / 跳过某步骤 / 用 general_assistant 处理了专业领域问题
2. **现有工具能力不足**：哪些 Specialist 频繁返回"未找到"、"不支持"、"无法处理"？
   - 判定标准：同一类型错误在同一任务中出现 ≥2 次
3. **审批流程缺失**：是否需要人审但没有对应的审批机制？
4. **输出质量低下**：Specialist 返回的结果是否需要多次重试才能满足要求？

## 任务信息

{task_info}

## 执行日志（journal）

{journal_context}

## 输出格式

如果未发现明显缺口，返回 `null`。
如果发现缺口，返回以下 JSON（注意：花括号内的中文占位符请用实际内容替换）：

```json
{{
  "has_gap": true,
  "gaps": [
    {{
      "gap_type": "missing_specialist",
      "domain": "recruitment",
      "description": "缺少专门处理[[具体场景]]的 Specialist Agent",
      "severity": "medium",
      "suggested_action": "create_agent",
      "suggested_name": "[[snake_case_name]]",
      "suggested_spec": {{
        "description": "[[一句话中文描述]]",
        "allowed_tools": ["[[已有工具名]]", "..."],
        "reason": "[[为什么需要这个 Specialist]]"
      }}
    }}
  ]
}}
```

## 约束

- gap_type: "missing_specialist" | "missing_tool" | "insufficient_capability"
- severity: "low" | "medium" | "high" | "critical"
- suggested_action: "create_agent" | "create_tool" | "update_agent"
- suggested_name: 全小写下划线命名，体现领域和职责
- allowed_tools: 只能从 TOOL_REGISTRY 中已有的工具中选择，数量不超过5个
- 如果没有明显缺口，返回 `null`
- 只返回 JSON，不要额外解释
"""


# ── 批量任务分析 prompt ─────────────────────────────────

BATCH_ANALYSIS_PROMPT = """你是一个 Multi-Agent 系统的能力分析师。分析以下一批任务的执行摘要，识别跨任务的共性能力缺口。

## 可用工具列表
{available_tools}

## 现有 Specialist Agent
{existing_agents}

## 任务执行摘要（共 {task_count} 个任务）

{task_summaries}

## 输出格式

返回 JSON 数组，每个元素是一个独立的能力缺口。如果没发现共性缺口，返回空数组 `[]`。

```json
[
  {{
    "domain": "recruitment",
    "gap_type": "missing_specialist",
    "description": "缺少专门处理薪酬谈判的 Specialist Agent。多个任务中 Executor 尝试委托给 offer_management_specialist 但该 Agent 明确表示不负责薪酬谈判。",
    "severity": "high",
    "suggested_action": "create_agent",
    "suggested_name": "salary_negotiation_specialist",
    "suggested_spec": {{
      "description": "薪酬谈判专员 — 负责 Offer 薪资沟通、薪资结构调整建议、候选人薪资预期管理",
      "allowed_tools": ["async_moka_get_candidate_detail", "async_knowledge_query_ask", "async_get_current_time"],
      "reason": "offer_management_specialist 只查 Offer 状态，不负责谈判流程。多个招聘任务在 Offer 阶段卡住。"
    }}
  }}
]
```

## 约束

- 只返回**跨任务共性**的缺口（至少出现在 {min_occurrences} 个不同任务中）
- 缺口描述要具体，引用任务 ID 作为证据
- suggested_name 必须全小写下划线
- allowed_tools 只能从上面列出的可用工具中选择
- 每个缺口必须与其他缺口有明显区分度
- 只返回 JSON 数组，不要额外解释
"""


# ── 聚合分析 prompt ─────────────────────────────────────

AGGREGATION_PROMPT = """你是一个 Multi-Agent 系统的能力分析师。以下是从多个维度收集到的系统能力信号，请综合判断是否存在需要修复的能力缺口。

## 信号来源

{signals}

## 输出格式

```json
{{
  "summary": "此次分析覆盖 X 个任务、Y 条用户反馈，发现 N 个能力缺口",
  "gaps": [...]
}}
```

如果未发现值得关注的缺口，返回空的 gaps 数组。
"""
