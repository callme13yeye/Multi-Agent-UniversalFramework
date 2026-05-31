"""
RetrievalPipeline — 统一检索管道：自适应重查 × 多路并行检索 × RRF 融合 × 动态裁剪。

Pipeline 流程:
  1. AdaptiveQueryRewriter — LLM 分类查询类型，按策略扩写
  2. MultiRecall + RRF    — 多路并行检索 + Reciprocal Rank Fusion 融合
  3. Rerank               — bge-reranker 精排
  4. DynamicTopK          — 根据分数分布自动裁剪结果
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from langfuse import observe
from llama_index.core import VectorStoreIndex
from llama_index.core.schema import NodeWithScore, QueryBundle
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.core.postprocessor import SentenceTransformerRerank

from app.async_get_index import async_get_milvus_index

logger = logging.getLogger(__name__)

__all__ = ["RetrievalPipeline", "QueryRewriter"]


# ═══════════════════════════════════════════════════════════════
# Step 1: QueryRewriter — LLM 驱动的查询重写/扩展
# ═══════════════════════════════════════════════════════════════

class QueryRewriter:
    """
    自适应查询重写器：用 LLM 一次调用同时完成查询分类和策略扩写。

    三种查询类型及对应策略:
      - FACTUAL（事实型）: 扩写 2-3 个变体，提高召回
      - COMPLEX（复杂型）: 扩写 0-1 个变体，保持原意精度
      - SPECIFIC（精确型）: 不扩写，原文直接检索
    """

    def __init__(self, llm: Any, enabled: bool = True):
        self.llm = llm
        self.enabled = enabled

    @observe(name="QueryRewriter.rewrite")
    async def rewrite(self, question: str) -> list[str]:
        """返回 [original, variant1, ...] 的查询列表。"""
        if not self.enabled or not question:
            return [question]

        prompt = f"""你是一个智能搜索查询扩展助手。请先判断问题类型，再按类型执行对应的扩展策略。

判断规则:
- FACTUAL: 事实型，涉及具体数值、定义、是否判断、流程等（如"年假几天"、"报销流程是什么"）
- COMPLEX: 复杂型，涉及分析、总结、对比、介绍等（如"分析Q3营收趋势"、"对比两种方案"）
- SPECIFIC: 精确型，涉及特定文档、具体章节、精确引用等（如"2024财报第18页的现金流"）

问题: {question}

输出格式（只输出以下内容，不要额外说明）:
类型: <FACTUAL|COMPLEX|SPECIFIC>
变体:
<FACTUAL 类型: 生成 2-3 个同义改写或术语展开的变体，每行一个>
<COMPLEX 类型: 生成 0-1 个变体，保持原文精度>
<SPECIFIC 类型: 不生成变体，此行留空>"""

        try:
            response = await self.llm.acomplete(prompt)
            lines = response.text.strip().split("\n")

            # 解析查询类型
            query_type = "GENERAL"
            for line in lines:
                if line.startswith("类型:"):
                    query_type = line.replace("类型:", "").strip().upper()

            # 解析变体
            variant_start = False
            variants = []
            for line in lines:
                if line.startswith("变体:"):
                    variant_start = True
                    continue
                if variant_start and line.strip() and len(line.strip()) > 3:
                    variants.append(line.strip())

            # 去重 + 限制数量
            all_queries = [question] + [
                v for v in variants if v.lower() != question.lower()
            ]
            result = all_queries[:3]

            logger.info(
                "[QueryRewriter] type=%s | %s → %s",
                query_type,
                question[:40],
                [q[:40] for q in result],
            )
            return result
        except Exception as e:
            logger.warning(f"[QueryRewriter] LLM 改写失败，回退原文: {e}")
            return [question]


# ═══════════════════════════════════════════════════════════════
# Pipeline — 统一检索管道
# ═══════════════════════════════════════════════════════════════

class RetrievalPipeline:
    """
    统一检索管道，串联 自适应改写 → 多路并行检索 + RRF 融合 → 精排 → 动态裁剪。

    使用方式:
        pipeline = RetrievalPipeline(embed_model, rerank_model, llama_model)
        nodes = await pipeline.retrieve(
            question="公司年假政策",
            user_id=1,
            session_id="...",
        )
    """

    def __init__(
        self,
        embed_model: HuggingFaceEmbedding,
        rerank_model: SentenceTransformerRerank,
        llama_llm: Any,
        enable_rewriter: bool = True,
        enable_fusion: bool = True,
        top_k: int = 10,
        drop_ratio: float = 0.3,
        min_score: float = 0.70,
    ):
        self.embed_model = embed_model
        self.rerank_model = rerank_model
        self.llama_llm = llama_llm

        # 子组件
        self.query_rewriter = QueryRewriter(llm=self.llama_llm, enabled=enable_rewriter)

        # 配置
        self.enable_fusion = enable_fusion
        self.top_k = top_k
        self.drop_ratio = drop_ratio
        self.min_score = min_score

    # ── 对外接口 ──────────────────────────────────────────────

    @observe(name="RetrievalPipeline.retrieve")
    async def retrieve(
        self,
        question: str,
        user_id: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> list[NodeWithScore]:
        """
        执行完整检索管道。

        Returns:
            Top-K NodeWithScore 列表（已重排序）
        """
        if user_id is None:
            logger.error("[RetrievalPipeline] user_id 为空，无法检索")
            return []

        # Step 0: 获取用户索引
        index = await async_get_milvus_index(
            user_id=int(user_id),
            embed_model=self.embed_model,
        )
        if index is None:
            logger.warning(f"[RetrievalPipeline] 用户 {user_id} 知识库为空")
            return []

        # Step 1: 重查 (Query Rewriting)
        expanded_queries = await self.query_rewriter.rewrite(question)
        logger.info(
            "[RetrievalPipeline] Step1 重查完成: %d 个查询变体",
            len(expanded_queries),
        )

        # Step 2: 多路并行检索 + RRF 融合
        base_retriever = index.as_retriever(
            similarity_top_k=self.top_k * 2,  # 多取一些供融合后裁剪
            use_async=True,
        )

        if self.enable_fusion and len(expanded_queries) > 1:
            logger.info(
                "[RetrievalPipeline] Step2 多路并行检索 + RRF 融合 (%d 个变体)",
                len(expanded_queries),
            )
            nodes = await self._multirecall_fusion(base_retriever, expanded_queries)
        else:
            nodes = await base_retriever.aretrieve(question)

        if not nodes:
            logger.info("[RetrievalPipeline] 检索结果为空")
            return []

        logger.info(
            "[RetrievalPipeline] Step2 多路召回完成: %d 个节点",
            len(nodes),
        )

        # Step 3: 精排 (Rerank)
        if self.rerank_model and nodes:
            try:
                # 用 pipeline top_k 覆盖 reranker top_n，确保返回数量与管道一致
                self.rerank_model.top_n = min(self.top_k, len(nodes))
                nodes = self.rerank_model.postprocess_nodes(
                    nodes,
                    query_bundle=QueryBundle(query_str=question),
                )
                # 转换 np.float32 → float，避免 Pydantic 序列化警告
                for n in nodes:
                    if n.score is not None:
                        n.score = float(n.score)
                logger.info(
                    "[RetrievalPipeline] Step3 精排完成: %d 个节点",
                    len(nodes),
                )
            except Exception as e:
                logger.warning(f"[RetrievalPipeline] 精排失败: {e}")

        # Step 4: 动态 Top-K 裁剪
        nodes = self._dynamic_top_k(nodes)
        logger.info(
            "[RetrievalPipeline] Step4 动态裁剪: 返回 %d 个结果",
            len(nodes),
        )

        return nodes

    # ── 内部方法 ──────────────────────────────────────────────

    async def _multirecall_fusion(
        self, retriever, queries: list[str], rrf_k: int = 60
    ) -> list[NodeWithScore]:
        """
        多路并行检索 + RRF 融合。

        对每个查询变体独立检索，然后用 Reciprocal Rank Fusion
        合并排名，奖励在多个查询变体中稳定出现的文档。
        """
        import asyncio

        tasks = [retriever.aretrieve(q) for q in queries]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        # RRF: score = Σ 1/(k + rank)
        rrf_scores: dict[str, dict] = {}
        for i, results in enumerate(all_results):
            if isinstance(results, Exception):
                logger.warning(
                    "[RetrievalPipeline] 第 %d 路检索异常: %s", i, results
                )
                continue
            for rank, node in enumerate(results, 1):
                nid = node.node.node_id
                if nid not in rrf_scores:
                    rrf_scores[nid] = {"node": node, "score": 0.0}
                rrf_scores[nid]["score"] += 1.0 / (rrf_k + rank)

        # 按 RRF 分数降序排列
        sorted_nodes = sorted(
            rrf_scores.values(), key=lambda x: x["score"], reverse=True
        )
        return [item["node"] for item in sorted_nodes]

    def _dynamic_top_k(self, nodes: list[NodeWithScore]) -> list[NodeWithScore]:
        """
        根据 Rerank 后的分数分布动态裁剪结果。

        从最高分开始遍历，当后续分数相对于最高分的下降幅度
        超过 drop_ratio 时截断，至少保留 min_k 条保底。
        """
        if not nodes:
            return []
        max_k = self.top_k
        drop_ratio = self.drop_ratio
        min_k = 1

        top_score = nodes[0].score
        if top_score is None or top_score <= 0:
            return nodes[:max_k]

        for i in range(1, len(nodes)):
            current = nodes[i].score
            if current is not None and top_score > 0:
                if (top_score - current) / top_score > drop_ratio:
                    result = nodes[:max(i, min_k)]
                    return result[:max_k]
        return nodes[:max_k]

