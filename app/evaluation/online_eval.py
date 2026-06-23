"""=============== 生产环境在线评估 ==========
从 Langfuse 拉取真实用户 traces，按 session_id 聚合为多轮对话，
使用 deepEval 自动评估: GoalAccuracy / ToolUse / ConversationCompleteness。
评估分数写回 Langfuse，形成 "生产监控 → 评估 → 改进" 闭环。
max 5 并发控制防止触发 API 速率限制。
=============================================

在线评估脚本（多轮对话支持）：
- 从 Langfuse 拉取生产环境的 traces，按 session_id 聚合为多轮对话。
- 使用 deepEval 评估多轮指标：GoalAccuracy、ToolUse、ConversationCompleteness。
- 将评估分数写回 Langfuse（关联到 session 的第一个 trace）。
用法: python online_eval_agent.py --since 24h --limit 100
"""
import asyncio
import sys
import json
import logging
import argparse
import os
import ast
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Any, Tuple
from pathlib import Path

from dotenv import load_dotenv
from langfuse import get_client
from deepeval.test_case import ConversationalTestCase, Turn, ToolCall
from deepeval.metrics import (
    GoalAccuracyMetric,
    ToolUseMetric,
    ConversationCompletenessMetric,
)
from deepeval.models import GPTModel

# ---------- 项目配置 ----------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / "key.env")
os.environ["CONFIDENT_TRACE_VERBOSE"] = "0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("online_eval")

# 并发限制（防止同时评估过多 session 触发速率限制）
MAX_CONCURRENT_EVALUATIONS = 5

# ---------- 工具定义 ----------
from app.tools.common import async_get_current_time, async_web_search
from app.tools.knowledge import async_knowledge_query_ask

AVAILABLE_TOOLS = [async_get_current_time, async_web_search, async_knowledge_query_ask]

def build_tool_calls() -> List[ToolCall]:
    """构造符合 deepEval 要求的工具描述列表"""
    return [
        ToolCall(
            name=tool.name,
            description=getattr(tool, "description", tool.name),
        )
        for tool in AVAILABLE_TOOLS
    ]

# ---------- 评估模型组件 ----------
EVALUATION_MODEL = GPTModel(
    model="deepseek-chat",
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)

def build_metrics() -> List:
    """构建评估指标列表"""
    return [
        GoalAccuracyMetric(threshold=0.6, include_reason=True, model=EVALUATION_MODEL),
        ToolUseMetric(
            threshold=0.6,
            include_reason=True,
            available_tools=build_tool_calls(),
            model=EVALUATION_MODEL,
        ),
        ConversationCompletenessMetric(
            threshold=0.6, include_reason=True, model=EVALUATION_MODEL
        ),
    ]

langfuse = get_client()

# ========== 辅助函数 ==========
def safe_parse_json(value: Any) -> Any:
    """
    安全解析 JSON 字符串或直接返回字典/列表。
    若解析失败，尝试 ast.literal_eval，最后返回原值。
    """
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):
                pass
    return value

def ensure_dict(value: Any) -> dict:
    """确保返回一个字典"""
    if isinstance(value, dict):
        return value
    parsed = safe_parse_json(value)
    return parsed if isinstance(parsed, dict) else {}

def extract_user_content(input_data: Any) -> str:
    """从 trace 的 input 中提取用户消息"""
    if input_data is None:
        return ""
    parsed = safe_parse_json(input_data)

    if isinstance(parsed, dict):
        messages = parsed.get("messages", [])
        if messages:
            for msg in messages:
                if isinstance(msg, dict) and msg.get("role") == "user":
                    return msg.get("content", "")
        # 没有 messages 字段，直接检查 role
        if parsed.get("role") == "user":
            return parsed.get("content", "")
    elif isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict) and item.get("role") == "user":
                return item.get("content", "")
    return str(parsed) if parsed else ""

def extract_assistant_content(output_data: Any) -> str:
    """从 trace 的 output 中提取助手回复"""
    if output_data is None:
        return ""
    parsed = safe_parse_json(output_data)
    if isinstance(parsed, dict):
        return parsed.get("content", str(parsed))
    return str(parsed)

def parse_tool_calls(observations: Any) -> List[Dict[str, Any]]:
    """解析工具调用记录"""
    tool_calls = []
    if not observations:
        return tool_calls

    for obs in observations:
        if isinstance(obs, dict):
            obs_type = obs.get("type", "")
            tool_name = obs.get("name", "unknown")
            tool_input = ensure_dict(obs.get("input", {}))
            tool_output = obs.get("output", None)
        else:  # 对象属性
            obs_type = getattr(obs, "type", "")
            tool_name = getattr(obs, "name", "unknown")
            tool_input = ensure_dict(getattr(obs, "input", {}))
            tool_output = getattr(obs, "output", None)

        if obs_type == "TOOL":
            tool_calls.append({
                "name": tool_name,
                "args": tool_input,
                "output": tool_output,
            })
    return tool_calls

def extract_turn(trace) -> Optional[Dict[str, Any]]:
    """从单条 trace 中提取一次对话轮次（用户 + 助手 + 工具调用）"""
    trace_id = getattr(trace, "id", "")
    timestamp = getattr(trace, "timestamp", None) or getattr(trace, "start_time", None)

    user_content = extract_user_content(getattr(trace, "input", None))
    assistant_content = extract_assistant_content(getattr(trace, "output", None))

    if not user_content or not assistant_content:
        return None

    observations = getattr(trace, "observations", [])
    tool_calls = parse_tool_calls(observations)

    return {
        "user": user_content.strip(),
        "assistant": assistant_content.strip(),
        "tool_calls": tool_calls,
        "timestamp": timestamp,
        "trace_id": trace_id,
    }

def resolve_thread_id(trace) -> Optional[str]:
    """从 trace 的 metadata/config 中提取 thread_id"""
    # 尝试 metadata
    metadata = getattr(trace, "metadata", None) or {}
    if isinstance(metadata, dict):
        thread_id = metadata.get("thread_id")
        if thread_id:
            return thread_id
    # 尝试 config.configurable
    config = getattr(trace, "config", None) or {}
    if isinstance(config, dict):
        thread_id = config.get("configurable", {}).get("thread_id")
        if thread_id:
            return thread_id
    return None

# ========== 1. 拉取 traces ==========
async def fetch_traces_since(since: datetime, limit: int = 200) -> List[Any]:
    """分页拉取指定时间后的完整 traces"""
    logger.info(f"正在拉取 {since.strftime('%Y-%m-%d %H:%M:%S UTC')} 之后的 traces (上限 {limit} 条)")
    all_traces = []
    page = 1
    remaining = limit
    page_size = min(limit, 50)

    try:
        while remaining > 0:
            response = langfuse.api.trace.list(
                from_timestamp=since,
                limit=min(page_size, remaining),
                page=page,
            )
            # 兼容不同返回格式
            batch = response.data if hasattr(response, "data") else response
            if not batch:
                break

            for trace_summary in batch:
                trace_id = getattr(trace_summary, "id", "")
                if not trace_id:
                    all_traces.append(trace_summary)
                    continue
                # 获取完整 trace
                try:
                    full_trace = langfuse.api.trace.get(trace_id)
                    all_traces.append(full_trace)
                except Exception as e:
                    logger.warning(f"获取完整 trace {trace_id} 失败: {e}")
                    all_traces.append(trace_summary)

            remaining -= len(batch)
            page += 1

        logger.info(f"共拉取 {len(all_traces)} 条 traces")
        return all_traces
    except Exception as e:
        logger.error(f"拉取 traces 失败: {e}")
        return []

# ========== 2. 构建会话数据 ==========
def group_traces_by_session(traces: List[Any]) -> Dict[str, List[Dict]]:
    """
    按 thread_id 分组，每个会话包含按时间排序的轮次数据。
    """
    sessions: Dict[str, List[Dict]] = {}
    skipped = 0
    for trace in traces:
        thread_id = resolve_thread_id(trace)
        if not thread_id:
            skipped += 1
            continue

        turn = extract_turn(trace)
        if not turn:
            skipped += 1
            continue

        sessions.setdefault(thread_id, []).append(turn)

    # 按时间排序
    for sid in sessions:
        sessions[sid].sort(
            key=lambda x: x.get("timestamp") if x.get("timestamp") else datetime.min.replace(tzinfo=timezone.utc)
        )

    logger.info(f"按会话分组完成：共 {len(sessions)} 个会话（跳过了 {skipped} 条无法分组的 trace）")
    return sessions

# ========== 3. 构建 deepEval 测试用例 ==========
def turns_to_testcase(session_id: str, turns: List[Dict], expected_outcome: str = "") -> Optional[ConversationalTestCase]:
    """将多轮对话数据转为 deepEval 的 ConversationalTestCase"""
    conversation_turns = []
    for turn in turns:
        user_msg = turn.get("user", "").strip()
        if user_msg:
            conversation_turns.append(Turn(role="user", content=user_msg))

        assistant_msg = turn.get("assistant", "").strip()
        tools = turn.get("tool_calls", [])
        tool_call_objects = [
            ToolCall(
                name=tc["name"],
                description=f"Call {tc['name']}",
                input_parameters=tc.get("args", {}),
                output=tc.get("output"),
            )
            for tc in tools
        ]
        conversation_turns.append(
            Turn(
                role="assistant",
                content=assistant_msg,
                tools_called=tool_call_objects if tool_call_objects else None,
            )
        )

    if len(conversation_turns) < 2:
        return None

    return ConversationalTestCase(
        turns=conversation_turns,
        context=[],
        expected_outcome=expected_outcome,
        additional_metadata={
            "session_id": session_id,
            "num_turns": len(turns),
            "trace_ids": [t["trace_id"] for t in turns],
        },
    )

# ========== 4. 评估并写回 ==========
async def evaluate_one_session(
    session_id: str,
    test_case: ConversationalTestCase,
    metrics: List,
    trace_ids: List[str],
    semaphore: asyncio.Semaphore,
) -> Dict[str, Any]:
    """对单个会话执行所有指标评估并写回结果"""
    results = {}
    async with semaphore:
        for metric in metrics:
            name = metric.__class__.__name__
            try:
                await metric.a_measure(test_case)
                results[name] = {
                    "score": metric.score,
                    "reason": metric.reason,
                    "success": metric.is_successful(),
                }
                if trace_ids:
                    langfuse.create_score(
                        trace_id=trace_ids[0],
                        name=name,
                        value=metric.score,
                        comment=metric.reason,
                        metadata={
                            "session_id": session_id,
                            "num_turns": len(test_case.turns) // 2,
                        },
                    )
            except Exception as e:
                logger.error(f"会话 {session_id} 指标 {name} 评估失败: {e}", exc_info=True)
                results[name] = {"score": 0.0, "reason": str(e), "success": False}
    scores_str = ", ".join(
        f"{name}={data.get('score', 0):.3f}" for name, data in results.items()
    )
    logger.info(f"会话: {session_id}: {scores_str}")
    return results

async def evaluate_sessions_parallel(
    sessions: Dict[str, List[Dict]],
    expected_map: Dict[str, str],
    metrics: List,
) -> Dict[str, Dict[str, Any]]:
    """并发评估所有会话"""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_EVALUATIONS)
    tasks = {}
    for session_id, turns in sessions.items():
        expected = expected_map.get(session_id, "")
        test_case = turns_to_testcase(session_id, turns, expected)
        if not test_case:
            logger.warning(f"会话 {session_id} 无法构建测试用例，已跳过")
            continue

        trace_ids = [t["trace_id"] for t in turns]
        tasks[session_id] = evaluate_one_session(
            session_id, test_case, metrics, trace_ids, semaphore
        )

    if not tasks:
        logger.warning("没有可供评估的有效会话")
        return {}

    # 并发执行
    results = {}
    for session_id, task in tasks.items():
        results[session_id] = await task
    return results

# ========== 5. 汇总与主流程 ==========
def parse_time_offset(offset_str: str) -> datetime:
    """将 12h/2d 这样的字符串转为 UTC 时间"""
    import re
    pattern = re.compile(r"^(\d+)\s*(h|d|w|m)$")
    match = pattern.match(offset_str.lower())
    if not match:
        raise ValueError(f"不支持的时间格式: {offset_str}，请使用如 24h、2d、1w 等")
    value = int(match.group(1))
    unit = match.group(2)
    if unit == "h":
        delta = timedelta(hours=value)
    elif unit == "d":
        delta = timedelta(days=value)
    elif unit == "w":
        delta = timedelta(weeks=value)
    elif unit == "m":
        delta = timedelta(minutes=value)  # 方便测试
    else:
        delta = timedelta(hours=value)
    return datetime.now(timezone.utc) - delta

async def main():
    parser = argparse.ArgumentParser(description="多轮对话在线评估")
    parser.add_argument("--since", type=str, default="1w", help="时间范围，例如 12h, 2d, 3w")
    parser.add_argument("--limit", type=int, default=500, help="最多拉取的 trace 数量")
    parser.add_argument("--expected-outcomes", type=str, help="JSON文件，格式 {\"session_id\": \"预期目标\"}")
    args = parser.parse_args()

    # 解析时间范围
    try:
        since_dt = parse_time_offset(args.since)
    except ValueError as e:
        logger.error(f"时间格式错误: {e}")
        return

    # 加载预期目标（可选）
    expected_map = {}
    if args.expected_outcomes and os.path.exists(args.expected_outcomes):
        with open(args.expected_outcomes, "r", encoding="utf-8") as f:
            expected_map = json.load(f)
        logger.info(f"已加载 {len(expected_map)} 个会话的预期目标")

    metrics = build_metrics()
    logger.info(f"评估指标：{[m.__class__.__name__ for m in metrics]}")

    # 拉取 traces
    traces = await fetch_traces_since(since_dt, limit=args.limit)
    if not traces:
        logger.warning("未找到 traces，流程结束")
        return

    # 按会话分组
    sessions = group_traces_by_session(traces)
    if not sessions:
        logger.warning("分组后没有有效会话")
        return

    # 并发评估
    logger.info(f"开始评估 {len(sessions)} 个会话...")
    session_results = await evaluate_sessions_parallel(sessions, expected_map, metrics)

    # 汇总统计
    aggregated = {m.__class__.__name__: [] for m in metrics}
    for res in session_results.values():
        for name, data in res.items():
            if "score" in data:
                aggregated[name].append(data["score"])

    # 总结报告
    logger.info("\n" + "=" * 60 + "\n多轮对话在线评估汇总\n" + "=" * 60)
    logger.info(f"共评估 {len(session_results)} 个会话")
    for metric_name, score_list in aggregated.items():
        if score_list:
            avg = sum(score_list) / len(score_list)
            logger.info(f"{metric_name}: 平均分 {avg:.3f}（样本数 {len(score_list)}）")
        else:
            logger.info(f"{metric_name}: 无有效样本")

    langfuse.flush()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())