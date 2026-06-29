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

    def __init__(self, llm: Any = None, enabled: bool = True, gateway: Any = None):
        self.llm = llm
        self.enabled = enabled
        self.gateway = gateway

    def _get_llm(self):
        """获取当前应使用的 LLM（优先从 gateway 动态获取）。"""
        if self.gateway is not None:
            from app.gateway.types import ModelRole
            chain = self.gateway.get_model_chain(ModelRole.RETRIEVAL_REWRITER)
            if chain:
                return chain[0][1]
        return self.llm

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
            response = await self._get_llm().acomplete(prompt)
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
        fallback_llm: Optional[Any] = None,
        enable_rewriter: bool = True,
        enable_fusion: bool = True,
        enable_graph_rag: bool = True,
        enable_relevance_filter: bool = True,
        top_k: int = 10,
        drop_ratio: float = 0.25,
        min_score: float = 0.75,
        gateway: Any = None,
        knowledge_graph_service: Any = None,
    ):
        self.embed_model = embed_model
        self.rerank_model = rerank_model
        self.fallback_llm = fallback_llm
        self.gateway = gateway
        self.kg_service = knowledge_graph_service

        # 子组件：查询重写用 fallback 模型（或 gateway 动态获取）
        self.query_rewriter = QueryRewriter(
            llm=self.fallback_llm,
            enabled=enable_rewriter,
            gateway=gateway,
        )

        # 配置
        self.enable_fusion = enable_fusion
        self.enable_graph_rag = enable_graph_rag
        self.enable_relevance_filter = enable_relevance_filter
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
        skip_relevance_filter: bool = False,
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

        # Step 3.5: LLM 相关性验证（过滤"高分但不回答问题的"片段）
        if self.enable_relevance_filter and nodes and not skip_relevance_filter:
            before = len(nodes)
            nodes = await self._llm_relevance_filter(nodes, question)
            logger.info(
                "[RetrievalPipeline] Step3.5 相关性过滤: %d → %d 个节点",
                before, len(nodes),
            )

        # Step 4: 动态 Top-K 裁剪
        nodes = self._dynamic_top_k(nodes)
        logger.info(
            "[RetrievalPipeline] Step4 动态裁剪: 返回 %d 个结果",
            len(nodes),
        )

        return nodes

    # ── 图谱增强接口 ──────────────────────────────────────────

    @observe(name="RetrievalPipeline.retrieve_with_graph")
    async def retrieve_with_graph(
        self,
        question: str,
        user_id: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> dict:
        """检索 + 图谱增强 — 同时返回向量检索结果和图谱上下文。

        🚀 性能优化：LLM 相关性过滤与图谱检索**并行执行**，
        延迟 = max(过滤耗时, 图谱耗时) 而非两者之和。

        Returns:
            {
                "nodes": list[NodeWithScore],       # 向量检索结果（已过滤）
                "graph_context": GraphContext,      # 图谱上下文（可能为空）
            }
        """
        # Step A: 向量检索（跳过 LLM 过滤，先拿原始结果）
        nodes = await self.retrieve(
            question=question,
            user_id=user_id,
            session_id=session_id,
            skip_relevance_filter=True,
        )

        if not nodes:
            return {"nodes": [], "graph_context": None}

        # Step B: LLM 相关性过滤 与 图谱检索 **并行执行**
        import asyncio

        async def _do_filter():
            if self.enable_relevance_filter:
                try:
                    return await self._llm_relevance_filter(nodes, question)
                except Exception as e:
                    logger.warning("[RetrievalPipeline] 相关性过滤异常: %s", e)
            return nodes

        async def _do_graph():
            if self.enable_graph_rag and self.kg_service is not None and self.kg_service.available:
                try:
                    keywords = await self._extract_keywords_from_nodes(nodes, question)
                    return await self.kg_service.graph_rag(
                        keywords=keywords,
                        question=question,
                        max_entities=15,
                        max_relations=30,
                        max_hops=2,
                    )
                except Exception as e:
                    logger.warning("[RetrievalPipeline] 图谱增强失败: %s", e)
            return None

        filtered_nodes, graph_context = await asyncio.gather(
            _do_filter(), _do_graph(),
        )

        return {
            "nodes": filtered_nodes,
            "graph_context": graph_context if graph_context and not graph_context.is_empty() else None,
        }

    async def _extract_keywords_from_nodes(
        self,
        nodes: list[NodeWithScore],
        question: str,
    ) -> list[str]:
        """从问题和检索结果中提取图谱搜索关键词。

        委托给 KnowledgeGraphService.extract_keywords()，
        该方法内置 LLM 提取 + 启发式回退。
        """
        # 拼接检索到的内容摘要作为上下文
        snippets = []
        for n in nodes[:3]:
            content = n.node.get_content()[:200]
            snippets.append(content)
        context_text = "\n".join(snippets)

        return await self.kg_service.extract_keywords(
            query=question,
            context=context_text,
            max_keywords=5,
        )

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

    async def _llm_relevance_filter(
        self,
        nodes: list[NodeWithScore],
        question: str,
        max_check: int = 5,
    ) -> list[NodeWithScore]:
        """用 LLM 验证每个检索片段是否真正与问题相关。

        Reranker 分数高 ≠ 内容回答得了问题。例如查"唐凯的学历"，
        "唐凯精通 Docker" 分数可能很高但完全不相关。
        这个方法用 LLM 逐片段判断相关性，过滤掉"高分噪音"。

        性能：一次 LLM 调用检查 max_check 个片段（每段截断 200 字），
        在 retrieve_with_graph 中与图谱检索并行执行以隐藏延迟。

        Args:
            nodes: 精排后的节点列表
            question: 用户原始问题
            max_check: 最多检查的节点数（控制 token 消耗和延迟）

        Returns:
            过滤后的节点列表
        """
        if not nodes:
            return []

        # 取前 N 个节点做检查
        check_nodes = nodes[:max_check]
        if len(check_nodes) <= 1:
            return nodes

        # 获取 LLM（优先用 RETRIEVAL_LLM，不可用时跳过）
        llm = None
        if self.gateway is not None:
            try:
                from app.gateway.types import ModelRole
                chain = self.gateway.get_model_chain(ModelRole.RETRIEVAL_LLM)
                if chain:
                    llm = chain[0][1]
            except Exception:
                pass

        if llm is None:
            logger.debug("[RelevanceFilter] LLM 不可用，跳过相关性过滤")
            return nodes

        # 构建精简 prompt — 一次判断所有片段
        chunks_text = []
        for i, n in enumerate(check_nodes):
            content = n.node.get_content()[:200]
            chunks_text.append(f"[{i + 1}] {content}")

        prompt = f"""判断以下文档片段是否与问题直接相关。只输出不相关的编号（如"3,5"），全相关输出"无"。

问题: {question}

{chr(10).join(chunks_text)}

不相关的编号:"""

        try:
            response = await llm.acomplete(prompt)
            raw = response.text.strip()
            logger.debug("[RelevanceFilter] LLM 响应: %s", raw)

            # 解析不相关编号
            if raw == "无" or raw.lower() == "none" or raw == "":
                return nodes

            # 提取数字
            import re
            irrelevant_indices = set()
            for m in re.finditer(r'\d+', raw):
                idx = int(m.group()) - 1  # 转为 0-based
                if 0 <= idx < len(check_nodes):
                    irrelevant_indices.add(idx)

            if not irrelevant_indices:
                return nodes

            # 过滤
            filtered = [n for i, n in enumerate(nodes) if i not in irrelevant_indices]
            removed = [f"#{i+1}" for i in sorted(irrelevant_indices)]
            logger.info(
                "[RelevanceFilter] 过滤掉 %d 个不相关片段: %s",
                len(irrelevant_indices), ", ".join(removed),
            )
            return filtered

        except Exception as e:
            logger.warning("[RelevanceFilter] LLM 调用失败，保留全部结果: %s", e)
            return nodes

    def _dynamic_top_k(self, nodes: list[NodeWithScore]) -> list[NodeWithScore]:
        """
        根据 Rerank 后的分数分布动态裁剪结果。

        两级过滤：
        1. 绝对阈值：单个节点分数低于 min_score 直接丢弃
        2. 相对截断：后续分数相对最高分降幅超过 drop_ratio 时截断

        至少保留 min_k 条保底。
        """
        if not nodes:
            return []
        max_k = self.top_k
        drop_ratio = self.drop_ratio
        min_score = self.min_score
        min_k = 1

        # ── 第 0 级：绝对分数过滤 ──────────────────────────
        nodes = [n for n in nodes if n.score is not None and n.score >= min_score]
        if not nodes:
            return []

        top_score = nodes[0].score
        if top_score is None or top_score <= 0:
            return nodes[:max_k]

        # ── 第 1 级：相对降幅截断 ──────────────────────────
        for i in range(1, len(nodes)):
            current = nodes[i].score
            if current is not None and top_score > 0:
                if (top_score - current) / top_score > drop_ratio:
                    result = nodes[:max(i, min_k)]
                    return result[:max_k]
        return nodes[:max_k]

