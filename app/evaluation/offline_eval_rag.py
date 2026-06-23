# offline_eval_rag.py
"""
RAG 离线评估 - 评估检索与生成质量
"""
import asyncio, sys, uuid, json, logging, argparse, os
from typing import List, Dict, Any
from pathlib import Path

from dotenv import load_dotenv
from langfuse.langchain import CallbackHandler
from langfuse import get_client

from deepeval.dataset import EvaluationDataset, Golden
from deepeval.metrics import (
    FaithfulnessMetric,
    AnswerRelevancyMetric,
    ContextualRelevancyMetric, ContextualRecallMetric, ContextualPrecisionMetric,
)
from deepeval.test_case import LLMTestCase
from deepeval.tracing import trace
from deepeval.models import GPTModel

from app.async_create_agent import async_create_agent
from app.tools.common import async_get_current_time, async_web_search
from app.tools.knowledge import async_knowledge_query_ask
from app.tools import register_knowledge_resource
from app.async_load_model import AsyncLoadModel
from config import get_config
from app.pg_database import pg_db_manager
from app.milvus_manager import milvus_db_manager
from app.auth import register_user
from app.async_get_index import async_get_milvus_index
from llama_index.core import Settings
from llama_index.core.schema import QueryBundle

# ---------- 环境 ----------
PROJECT_ROOT = Path(__file__).parents[2]
load_dotenv(PROJECT_ROOT / "key.env")
os.environ["CONFIDENT_TRACE_VERBOSE"] = "0"
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("rag_eval")

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
    Settings.embed_model = embed_model
    register_knowledge_resource(
        embed_model=embed_model, rerank_model=rerank_model,
        llama_chat_model=llama_chat_llm,
    )
    await pg_db_manager.initialize()
    await milvus_db_manager.initialize()
    test_user_id = await ensure_test_user()
    return embed_model, rerank_model, test_user_id

# ---------- 数据集加载 ----------
def load_evaluation_dataset(file_path: str) -> EvaluationDataset:
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    goldens = []
    for item in data["goldens"]:
        goldens.append(
            Golden(
                input=item["input"],
                expected_output=item.get("expected_output"),
                context=item.get("context", []),
                additional_metadata=item.get("additional_metadata", {}),
                retrieval_context=item.get("retrieval_context", []),
            )
        )
    logger.info(f"加载了 {len(goldens)} 条 RAG 测试用例")
    return EvaluationDataset(goldens=goldens)

# ---------- 工具调用格式化 ----------
def format_tool_calls(tool_calls_log: List[Dict]) -> List[Any]:
    # RAG 指标不关心工具调用，但保留信息
    return tool_calls_log

# ---------- 单条用例执行 ----------
async def run_rag_case(
    agent,
    golden: Golden,
    langfuse_handler,
    metrics,
    test_user_id,
    embed_model,
    rerank_model,
    session_id=None
):
    session_id = session_id or str(uuid.uuid4())
    logger.info(f"RAG 评估会话 {session_id} 开始，问题: {golden.input[:80]}...")

    with trace(name=f"rag_eval_{session_id}", user_id="offline_eval"):
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
        try:
            async for chunk in agent.astream(
                {"messages": [{"role": "user", "content": golden.input}]},
                config=configurable,
                stream_mode="messages",
            ):
                if isinstance(chunk, (tuple, list)) and len(chunk) > 0:
                    msg = chunk[0]
                    if hasattr(msg, "content") and msg.content:
                        output_text += msg.content
        except Exception as e:
            logger.error(f"Agent 异常: {e}")
            output_text = f"Error: {e}"

        # ---------- 获取实际检索上下文 ----------
        actual_retrieval_context: List[str] = []
        is_knowledge_query = "async_knowledge_query_ask" in golden.additional_metadata.get("expected_tools", [])

        if is_knowledge_query:
            try:
                index = await async_get_milvus_index(
                    user_id=int(test_user_id),
                    embed_model=embed_model,
                )
                if index:
                    retriever = index.as_retriever(similarity_threshold=0.4, similarity_top_k=10, use_async=True)
                    nodes = await retriever.aretrieve(golden.input)
                    if rerank_model and nodes:
                        final_nodes = rerank_model.postprocess_nodes(
                            nodes,
                            query_bundle=QueryBundle(golden.input),
                        )
                    else:
                        final_nodes = nodes
                    actual_retrieval_context = [node.get_content() for node in final_nodes]
            except Exception as e:
                logger.warning(f"无法获取实际检索上下文: {e}")

            # 无论检索是否为空，都使用实际检索结果（不兜底）
            if not actual_retrieval_context:
                logger.warning(f"用例 [{golden.input[:50]}...] 实际检索上下文为空，评估将反映检索失败。")
                failed_results = {}
                for metric in metrics:
                    name = metric.__class__.__name__
                    if name in [
                        "FaithfulnessMetric",
                        "ContextualRelevancyMetric",
                        "ContextualRecallMetric",
                        "ContextualPrecisionMetric",
                    ]:
                        failed_results[name] = {"score": 0.0, "reason": "实际检索上下文为空", "success": False}
                    else:
                        failed_results[name] = {"score": 0.0, "reason": "检索失败", "success": False}
                return output_text.strip(), failed_results
            final_retrieval_context = actual_retrieval_context
        else:
            final_retrieval_context = golden.retrieval_context or []

        test_case = LLMTestCase(
            input=golden.input,
            actual_output=output_text.strip(),
            expected_output=golden.expected_output or "N/A",
            retrieval_context=final_retrieval_context,
            context=golden.context,
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
            }
        except Exception as e:
            logger.error(f"指标 {name} 失败: {e}")
            results[name] = {"score": 0.0, "reason": str(e), "success": False}

    return output_text.strip(), results

# ---------- 指标构建 ----------
def build_rag_metrics() -> List[Any]:
    evaluation_model = GPTModel(
        model="deepseek-chat",
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
        temperature=0
    )
    return [
        FaithfulnessMetric(threshold=0.7, include_reason=True, model=evaluation_model),
        AnswerRelevancyMetric(threshold=0.7, include_reason=True, model=evaluation_model),
        ContextualRelevancyMetric(threshold=0.7, include_reason=True, model=evaluation_model),
        ContextualRecallMetric(threshold=0.7, include_reason=True, model=evaluation_model),
        ContextualPrecisionMetric(threshold=0.7, include_reason=True, model=evaluation_model),
    ]

# ---------- 上传 Langfuse ----------
def flush_results_to_langfuse(results_by_session: Dict[str, Dict]):
    client = get_client()
    for session_id, data in results_by_session.items():
        for m_name, m_data in data["metrics"].items():
            client.create_score(trace_id=session_id, name=m_name, value=m_data["score"], comment=m_data.get("reason", ""))
    client.flush()
    logger.info("RAG 评估结果已上传至 Langfuse")

# ---------- 主流程 ----------
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, default=str(PROJECT_ROOT / "app" / "evaluation" / "datasets" / "rag_datasets.json"))
    args = parser.parse_args()

    logger.info("初始化模型、数据库与测试用户...")
    embed_model, rerank_model, test_user_id = await initialize_all()

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        logger.error(f"数据集不存在: {dataset_path}")
        return
    dataset = load_evaluation_dataset(str(dataset_path))

    metrics = build_rag_metrics()
    logger.info(f"RAG 指标: {[m.__class__.__name__ for m in metrics]}")

    agent = await async_create_agent(
        model_name=langchain_chat_model_name, tools=TOOLS, system_prompt=SYSTEM_PROMPT,
        checkpointer=None, store=None,
    )
    langfuse_handler = CallbackHandler()

    all_results = {}
    aggregation = {m.__class__.__name__: [] for m in metrics}

    for idx, golden in enumerate(dataset.goldens, start=1):
        output, results = await run_rag_case(agent, golden, langfuse_handler, metrics, test_user_id, embed_model, rerank_model)
        session_id = str(uuid.uuid4())
        all_results[session_id] = {
            "input": golden.input, "output": output,
            "expected": golden.expected_output, "metrics": results,
        }
        for m_name, m_data in results.items():
            aggregation[m_name].append(m_data["score"])
        logger.info(f"[{idx}/{len(dataset.goldens)}] Faithfulness={results.get('FaithfulnessMetric', {}).get('score', 0):.3f}")

    logger.info("\n" + "="*60 + "\nRAG 评估汇总\n" + "="*60)
    for m_name, scores in aggregation.items():
        if scores:
            logger.info(f"{m_name}: 平均 {sum(scores)/len(scores):.3f} (样本数 {len(scores)})")

    flush_results_to_langfuse(all_results)
    current_dir = Path(__file__).parent
    with open(current_dir / "rag_eval_results.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)

    await pg_db_manager.close()
    await milvus_db_manager.close()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())