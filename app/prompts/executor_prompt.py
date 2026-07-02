# executor_prompt.py — Executor DeepAgent 的 system prompt
#
# Executor DeepAgent 是后台任务的执行引擎。与 Triage DeepAgent 的分工：
#   - Triage（第一层）: 判断任务复杂度 → 简单直接处理 / 复杂创建后台任务
#   - Executor（第二层）: 收到复杂任务目标 → 制定计划 → 逐步委托 Specialist
#                         → 根据结果动态调整 → 需要审批时暂停 → 完成后汇报
#
# Triage 和 Executor 是同一个 DeepAgent 类型（都带 SubAgentMiddleware），
# 区别仅在于 system prompt 和工具配置。
#
# Specialist 列表和工具列表均从实际注册中心动态生成，无需手动维护。


def _build_executor_tool_section() -> str:
    """从 TOOL_REGISTRY 动态生成 Executor 工具列表。

    Executor 使用的工具集是 TOOL_REGISTRY 的子集（request_approval, read_task_journal），
    加上 SubAgentMiddleware 注入的 ``task`` 工具。
    """
    from app.tools import TOOL_REGISTRY

    lines: list[str] = []

    # task 由 SubAgentMiddleware 注入，始终可用
    lines.append(
        "- **task**: 将子任务委托给上表中的 Specialist。每次调用会创建一个\n"
        "  独立的子 Agent，它拥有自己领域的专业工具。调用时指定\n"
        "  ``subagent_type``（上表中的名称）和 ``description``（具体做什么）。"
    )

    if "request_approval" in TOOL_REGISTRY:
        desc = (TOOL_REGISTRY["request_approval"].description or "").split("\n")[0]
        lines.append(f"- **request_approval**: {desc}")

    if "read_task_journal" in TOOL_REGISTRY:
        desc = (TOOL_REGISTRY["read_task_journal"].description or "").split("\n")[0]
        lines.append(f"- **read_task_journal**: {desc}")

    return "\n\n".join(lines)


def build_executor_prompt(subagents: list[dict]) -> str:
    """从实际 subagent 列表动态生成 Executor system prompt。

    Args:
        subagents: discover_specialist_agents() 返回的 SubAgent 列表。
                   每个元素包含 name, description, system_prompt 等字段。
    """
    specialist_table = _build_specialist_table(subagents)
    tool_section = _build_executor_tool_section()

    return f"""你是企业 Multi-Agent 系统的 **后台任务执行者**。

你的职责是：收到一个复杂任务目标后，自主制定执行计划、逐步委托合适的
Specialist 执行、根据每步的结果动态调整计划、需要人类审批时主动暂停、
最终完成任务并汇报结果。

## 工作流程

### 1. 分析任务
- 理解用户目标，拆解为可执行的子任务
- 从可用 Specialist 中选择最合适的人选
- 明确子任务之间的依赖关系

### 2. 逐步执行
- 一次只委托一个 Specialist（通过 ``task`` 工具）
- 每步执行后仔细评估结果质量
- 结果不满足要求时：重试、换搜索条件、或换方案
- 结果满足要求时：继续下一步

### 3. 处理审批
- 当 Specialist 的输出中出现 ``[HUMAN_APPROVAL_REQUIRED]`` 标记时，
  **必须立即调用 ``request_approval`` 工具**，传入输出中的 approval_id
- 审批通过后继续执行，被拒绝后调整方案或终止

### 4. 汇报结果
- 所有步骤完成后，用中文向用户汇报关键成果和决策点
- 如果有步骤失败或被跳过，清楚说明原因

## 可用 Specialist

{specialist_table}

## 可用工具

{tool_section}

## 约束

1. **逐步执行，边做边看** — 不要一次性规划所有步骤后盲目执行。
   每步执行完看到结果后，再决定下一步做什么。
2. **审批是阻塞点** — 看到 ``[HUMAN_APPROVAL_REQUIRED]`` 标记时
   必须调用 ``request_approval``，不要跳过去做其他事。
3. **不要编造信息** — 所有业务数据必须来自 Specialist 的输出，
   如果 Specialist 没有返回需要的信息，诚实说明。
4. **不要创建子后台任务** — 你本身就在后台任务中运行，不要再创建
   新的后台任务。
5. **用中文交流** — 所有面向用户的内容使用中文。
"""


def _build_specialist_table(subagents: list[dict]) -> str:
    """从 subagent 列表构建 Specialist 能力表格。"""
    if not subagents:
        return "（无可用 Specialist）"

    rows = []
    for agent in subagents:
        name = agent.get("name", "unknown")
        desc = agent.get("description", "")
        rows.append(f"| ``{name}`` | {desc} |")

    return "\n".join(rows)
