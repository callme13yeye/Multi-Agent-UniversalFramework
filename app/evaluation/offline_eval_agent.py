# offline_eval_agent.py — Agent 离线评估
# ============== deepEval + 自定义多轮评估 ==========
# 加载 JSON 数据集，模拟多轮对话，逐轮调用 Agent 采集响应。
# 使用 deepEval 的 GoalAccuracy / ToolUse / ConversationCompleteness 等指标，
# 用 GPTModel (DeepSeek) 作为评测模型 (LLM-as-a-Judge)。
# 评估结果上传 Langfuse 统一可观测。
# ====================================================
"""
Agent 离线评估
评估指标：
评估维度：目标达成、工具名称使用、工具参数正确性
"""
import asyncio, sys, uuid, json, logging, argparse, os
from typing import List, Dict
from pathlib import Path

from dotenv import load_dotenv
from langfuse.langchain import CallbackHandler
from langfuse import get_client

from deepeval.dataset import EvaluationDataset, ConversationalGolden
from deepeval.test_case import ConversationalTestCase, Turn, ToolCall
from deepeval.metrics import (
    ToolCorrectnessMetric,
    ArgumentCorrectnessMetric,
    TaskCompletionMetric,
    GoalAccuracyMetric,
    ToolUseMetric,
    ConversationCompletenessMetric
)
from deepeval.tracing import trace
from deepeval.models import GPTModel

from app.async_create_agent import async_create_agent
from app.async_tools import (
    async_get_current_time, async_web_search, async_knowledge_query_ask,
    register_knowledge_resource,
)
from app.async_load_model import AsyncLoadModel
from config import get_config
from app.pg_database import pg_db_manager
from app.milvus_manager import milvus_db_manager
from app.auth import register_user

# ---------- 环境 ----------
PROJECT_ROOT = Path(__file__).parents[2]
load_dotenv(PROJECT_ROOT / "key.env")
os.environ["CONFIDENT_TRACE_VERBOSE"] = "0"
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("agent_eval")

config = get_config()
langchain_chat_model_name = config["langchain_chat_model_name"]
embed_model_path_dir  = config["embed_model_path"]
rerank_model_path_dir = config["rerank_model_path"]
llama_chat_model_name = config["llama_chat_model_name"]

SYSTEM_PROMPT = """你是一个健康有用的智能助手，能够根据用户的问题选择最合适的工具。
                请遵循以下规则：
                1.当问题需要联网查询时，使用async_web_search工具。
                2.当问题涉及到时间、日期时，使用async_get_current_time工具。
                3.当问题涉及文档内容、专业知识、公司信息、项目详细、技术细节等需要查询知识库时，使用async_knowledge_query_ask工具。
                4.请始终使用中文进行回答。"""

TOOLS = [async_get_current_time, async_web_search, async_knowledge_query_ask]

# ---------- 确保测试用户 ----------
async def ensure_test_user() -> int:
    test_user = os.getenv("EVAL_TEST_USER", "eval_test_user")
    test_password = os.getenv("EVAL_TEST_PASSWORD", "EvalTest123!")
    user = await pg_db_manager.get_user_by_username(test_user)
    if user:
        logger.info(f"使用已存在的测试用户：{test_user} (id={user['id']})")
        return user["id"]
    else:
        user_id = await register_user(test_user, test_password)
        logger.info(f"自动创建测试用户 {test_user} (id={user_id})")
        return user_id

# ---------- 初始化 ----------
async def initialize_all():
    embed_model = await AsyncLoadModel.async_local_load_embed_model(embed_model_path_dir)
    rerank_model = await AsyncLoadModel.async_local_load_rerank_model(rerank_model_path_dir)
    llama_chat_llm = await AsyncLoadModel.async_llama_index_api_model(llama_chat_model_name)
    register_knowledge_resource(
        embed_model=embed_model, rerank_model=rerank_model,
        llama_chat_model=llama_chat_llm,
    )
    await pg_db_manager.initialize()
    await milvus_db_manager.initialize()
    test_user_id = await ensure_test_user()
    return test_user_id

# ---------- 数据集加载 ----------
def load_evaluation_dataset(file_path: str) -> EvaluationDataset:
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    goldens = []
    for item in data["goldens"]:
        turns = []
        for t in item["turns"]:
            retrieval_ctx = t.get("retrieval_context")
            if retrieval_ctx is None:
                retrieval_ctx = []
            turns.append(
                Turn(
                    role=t["role"],
                    content=t["content"],
                    retrieval_context=retrieval_ctx,
                )
            )
        additional_meta = item.get("additional_metadata", {})
        if "expected_tools" in additional_meta:
            new_expected = []
            for et in additional_meta["expected_tools"]:
                if isinstance(et, dict):
                    new_expected.append(
                        ToolCall(
                            name=et["name"],
                            description=et.get("description", ""),
                            input_parameters=et.get("input_parameters"),
                            output=et.get("output"),
                        )
                    )
                else:
                    new_expected.append(et)
            additional_meta["expected_tools"] = new_expected
        goldens.append(
            ConversationalGolden(
                scenario=item.get("scenario", ""),
                expected_outcome=item.get("expected_outcome"),
                turns=turns,
                context=item.get("context", []),
                additional_metadata=additional_meta,
            )
        )
    logger.info(f"加载了 {len(goldens)} 条 Agent 测试用例")
    return EvaluationDataset(goldens=goldens)

# ---------- 工具调用格式化 ----------
def format_tool_calls(tool_calls_log: List[Dict]) -> List[ToolCall]:
    formatted = []
    for tc in tool_calls_log:
        if not tc.get("name"):
            continue
        formatted.append(ToolCall(
            name=tc["name"],
            description=f"Call {tc['name']}",
            input_parameters=tc.get("args", {}),
            output=None,
        ))
    return formatted

# ---------- 多轮用例执行 ----------
async def run_agent_case(
    agent,
    golden: ConversationalGolden,
    langfuse_handler,
    metrics,
    test_user_id,
    session_id=None,
):
    session_id = session_id or str(uuid.uuid4())
    logger.info(f"Agent 评估会话 {session_id} 开始，场景: {golden.scenario}")

    messages_history: List[Dict] = []
    test_turns: List[Turn] = []

    for idx, turn in enumerate(golden.turns):
        if turn.role == "user":
            current_messages = messages_history + [{"role": "user", "content": turn.content}]
            configurable = {
                "configurable": {
                    "thread_id": session_id,
                    "user_id": str(test_user_id),
                    "session_id": session_id,
                },
                "callbacks": [langfuse_handler],
                "metadata": {"langfuse_user_id": "offline_eval", "langfuse_session_id": session_id},
            }
            output_text = ""
            tool_calls_by_index: Dict[int, Dict] = {}
            try:
                async for chunk in agent.astream(
                    {"messages": current_messages},
                    config=configurable, stream_mode="messages",
                ):
                    if isinstance(chunk, (tuple, list)) and len(chunk) > 0:
                        msg = chunk[0]
                        if hasattr(msg, "content") and msg.content:
                            output_text += msg.content
                        # 核心修复：处理 tool_call_chunks
                        if hasattr(msg, "tool_call_chunks") and msg.tool_call_chunks:
                            for tcc in msg.tool_call_chunks:
                                if isinstance(tcc, dict):
                                    idx = tcc.get("index", 0)
                                    name_delta = tcc.get("name")
                                    args_delta = tcc.get("args", "")
                                else:
                                    idx = getattr(tcc, "index", 0)
                                    name_delta = getattr(tcc, "name", None)
                                    args_delta = getattr(tcc, "args", "")
                                if idx not in tool_calls_by_index:
                                    tool_calls_by_index[idx] = {"name": "", "args_str": ""}
                                if name_delta:
                                    tool_calls_by_index[idx]["name"] += name_delta
                                if args_delta:
                                    tool_calls_by_index[idx]["args_str"] += args_delta
            except Exception as e:
                logger.error(f"Agent 异常: {e}")
                output_text = f"Error: {e}"

            # 将合并的字符串参数解析为字典
            tool_calls_log = []
            for idx in sorted(tool_calls_by_index.keys()):
                info = tool_calls_by_index[idx]
                name = info["name"].strip()
                args_str = info["args_str"].strip()
                if not name:
                    continue
                try:
                    args = json.loads(args_str) if args_str else {}
                except json.JSONDecodeError:
                    logger.warning(f"无法解析工具调用参数 JSON: {args_str}")
                    args = {}
                tool_calls_log.append({"name": name, "args": args})

            assistant_turn = Turn(
                role="assistant",
                content=output_text.strip(),
                tools_called=format_tool_calls(tool_calls_log) if tool_calls_log else None,
                retrieval_context=[],
            )

            test_turns.append(turn)               # 原始用户 Turn
            test_turns.append(assistant_turn)     # 实际助手 Turn
            messages_history.append({"role": "user", "content": turn.content})
            messages_history.append({"role": "assistant", "content": output_text.strip()})

        elif turn.role == "assistant":
            # golden 中预设的助手消息直接作为历史，不调用 Agent
            messages_history.append({"role": "assistant", "content": turn.content})
            test_turns.append(turn)               # 直接使用 golden 中的助手 Turn

    with trace(name=f"agent_eval_{session_id}", user_id="offline_eval"):
        test_case = ConversationalTestCase(
            turns=test_turns,
            context=golden.context,
            expected_outcome=golden.expected_outcome,
            additional_metadata=golden.additional_metadata,
        )

    results = {}
    for metric in metrics:
        name = metric.__class__.__name__
        try:
            if hasattr(metric, "a_measure"):
                await metric.a_measure(test_case)
            else:
                metric.measure(test_case)
            results[name] = {
                "score": metric.score,
                "reason": metric.reason,
                "success": metric.is_successful() if hasattr(metric, "is_successful") else None,
                "skipped": getattr(metric, "skipped", False),
            }
        except Exception as e:
            logger.error(f"指标 {name} 失败: {e}")
            results[name] = {"score": 0.0, "reason": str(e), "success": False, "skipped": False}

    final_output = test_turns[-1].content if test_turns else ""
    return final_output.strip(), results

# ---------- 指标构建 ----------
def build_agent_metrics(available_tools, relevant_topics):
    evaluation_model = GPTModel(
        model="deepseek-chat",
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
    )
    available_tool_calls = [
        ToolCall(name=tool.name, description=getattr(tool, "description", tool.name))
        for tool in available_tools
    ]
    # 以下为单轮会话评估指标
    # return [
    #     TaskCompletionMetric(threshold=0.6, include_reason=True, model=evaluation_model),
    #     ToolCorrectnessMetric(threshold=0.6, include_reason=True, available_tools=available_tool_calls, model=evaluation_model),
    #     ArgumentCorrectnessMetric(threshold=0.6, include_reason=True, model=evaluation_model),
    # ]
    # 以下为多轮会话指标评估
    return [
        GoalAccuracyMetric(threshold=0.6, include_reason=True, model=evaluation_model),
        ToolUseMetric(threshold=0.6, include_reason=True, available_tools=available_tool_calls, model=evaluation_model),
        ConversationCompletenessMetric(threshold=0.6, include_reason=True, model=evaluation_model),
    ]
# ---------- 上传 Langfuse ----------
def flush_results_to_langfuse(results_by_session):
    client = get_client()
    for session_id, data in results_by_session.items():
        for m_name, m_data in data["metrics"].items():
            if m_data.get("skipped"):
                continue
            client.create_score(trace_id=session_id, name=m_name, value=m_data["score"], comment=m_data.get("reason", ""))
    client.flush()
    logger.info("Agent 评估结果已上传至 Langfuse")

# ---------- 主流程 ----------
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, default=str(PROJECT_ROOT / "app" / "evaluation" / "datasets" / "agent_datasets.json"))
    args = parser.parse_args()

    logger.info("正在初始化...")
    test_user_id = await initialize_all()

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        logger.error(f"数据集不存在: {dataset_path}")
        return
    dataset = load_evaluation_dataset(str(dataset_path))

    relevant_topics = ["知识库查询", "时间查询", "天气查询", "安全策略"]
    metrics = build_agent_metrics(TOOLS, relevant_topics)
    logger.info(f"Agent 指标: {[m.__class__.__name__ for m in metrics]}")

    agent = await async_create_agent(
        model_name=langchain_chat_model_name, tools=TOOLS, system_prompt=SYSTEM_PROMPT,
        checkpointer=None, store=None,
    )
    langfuse_handler = CallbackHandler()

    all_results = {}
    aggregation = {m.__class__.__name__: [] for m in metrics}

    for idx, golden in enumerate(dataset.goldens, start=1):
        output, results = await run_agent_case(agent, golden, langfuse_handler, metrics, test_user_id)
        session_id = str(uuid.uuid4())
        all_results[session_id] = {
            "scenario": golden.scenario, "output": output,
            "expected_outcome": golden.expected_outcome, "metrics": results,
        }
        for m_name, m_data in results.items():
            if not m_data.get("skipped"):
                aggregation[m_name].append(m_data["score"])
            else:
                logger.info(f"[跳过的指标] {m_name}: {m_data.get('reason')}")
        scores_detail = ", ".join(
            f"{name}={results.get(name, {}).get('score', 0):.3f}"
            for name in aggregation.keys()
        )
        logger.info(f"[{idx}/{len(dataset.goldens)}] {golden.scenario}: {scores_detail}")

    logger.info("\n" + "="*60 + "\nAgent 评估汇总\n" + "="*60)
    for m_name, scores in aggregation.items():
        if scores:
            logger.info(f"{m_name}: 平均 {sum(scores)/len(scores):.3f} (样本数 {len(scores)})")

    flush_results_to_langfuse(all_results)
    current_dir = Path(__file__).parent
    with open(current_dir / "agent_eval_results.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)

    await pg_db_manager.close()
    await milvus_db_manager.close()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())