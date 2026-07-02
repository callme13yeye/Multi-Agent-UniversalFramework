# knowledge_graph.py — 知识图谱服务层
# 实体抽取、关系构建、存储、GraphRAG 检索。
#
# 核心能力：
#   1. 从文本中抽取实体和关系（LLM 驱动）
#   2. 写入 Neo4j（MERGE 去重）
#   3. GraphRAG — 从向量检索结果出发，扩展图谱上下文
#   4. 图谱问答 — 直接查询图谱获取关联信息

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Optional

from langfuse import observe

from app.stores import neo4j_manager

logger = logging.getLogger(__name__)

__all__ = [
    "KnowledgeGraphService",
    "Entity",
    "Relation",
    "GraphContext",
]


# ═══════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════

class Entity:
    """知识图谱实体。"""
    name: str
    type: str           # PERSON, ORG, DOCUMENT, CONCEPT, POLICY, TERM, etc.
    description: str = ""
    source_doc_id: str = ""
    source_snippet: str = ""
    metadata: dict[str, Any] = {}

    def __init__(
        self,
        name: str,
        type: str,
        description: str = "",
        source_doc_id: str = "",
        source_snippet: str = "",
        **metadata,
    ):
        self.name = name
        self.type = type
        self.description = description
        self.source_doc_id = source_doc_id
        self.source_snippet = source_snippet
        self.metadata = metadata

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "source_doc_id": self.source_doc_id,
            "source_snippet": self.source_snippet,
            "metadata": self.metadata,
        }


class Relation:
    """知识图谱关系。"""
    source: str         # 源实体名称
    target: str         # 目标实体名称
    relation: str       # 关系类型: BELONGS_TO, RELATED_TO, PART_OF, REQUIRES, etc.
    description: str = ""
    source_doc_id: str = ""

    def __init__(
        self,
        source: str,
        target: str,
        relation: str,
        description: str = "",
        source_doc_id: str = "",
    ):
        self.source = source
        self.target = target
        self.relation = relation
        self.description = description
        self.source_doc_id = source_doc_id

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "description": self.description,
            "source_doc_id": self.source_doc_id,
        }


class GraphContext:
    """图谱检索结果 — 包含相关子图的结构化信息。"""
    entities: list[dict]
    relations: list[dict]
    summary: str = ""

    def __init__(
        self,
        entities: list[dict] = None,
        relations: list[dict] = None,
        summary: str = "",
    ):
        self.entities = entities or []
        self.relations = relations or []
        self.summary = summary

    def is_empty(self) -> bool:
        return len(self.entities) == 0

    def to_context_text(self) -> str:
        """将图谱上下文转换为 LLM 可读的文本。"""
        if self.is_empty():
            return ""

        parts = []

        if self.summary:
            parts.append(f"📊 知识图谱摘要:\n{self.summary}\n")

        if self.entities:
            parts.append("**相关实体:**")
            for e in self.entities[:15]:
                source = e.get("source_doc_id", "")
                source_tag = f" 📎来源: {source}" if source else ""
                parts.append(
                    f"- [{e.get('type', 'ENTITY')}] **{e.get('name', '?')}**"
                    f"{': ' + e.get('description', '') if e.get('description') else ''}"
                    f"{source_tag}"
                )

        if self.relations:
            parts.append("\n**实体关系:**")
            for r in self.relations[:30]:
                parts.append(
                    f"- {r.get('source', '?')} "
                    f"-[{r.get('relation', 'RELATED_TO')}]→ "
                    f"{r.get('target', '?')}"
                )

        return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# 知识图谱服务
# ═══════════════════════════════════════════════════════════════

class KnowledgeGraphService:
    """知识图谱服务 — 实体/关系抽取、存储、GraphRAG 检索。

    使用方式:
        kg_service = KnowledgeGraphService(gateway=gateway)
        await kg_service.extract_and_store(text, doc_id="doc_123")
        context = await kg_service.graph_rag(["实体1", "实体2"], question="...")
    """

    # 实体类型定义
    ENTITY_TYPES = [
        "PERSON",       # 人物
        "ORG",          # 组织/公司/部门
        "POLICY",       # 政策/制度/规定
        "TERM",         # 术语/定义
        "CONCEPT",      # 概念/方法论
        "ROLE",         # 职位/角色
        "PROCESS",      # 流程/步骤
        "DOCUMENT",     # 文档/表单
        "METRIC",       # 指标/KPI
        "TOOL",         # 工具/系统
    ]

    def __init__(self, gateway: Any = None):
        self._gateway = gateway

    # ── 公开 API ──────────────────────────────────────────────

    def get_llm(self):
        """获取图谱抽取/检索用的 LLM（公开接口）。

        优先从 ModelGateway 动态获取，gateway 未就绪时返回 None。
        """
        if self._gateway is not None:
            from app.gateway.types import ModelRole
            chain = self._gateway.get_model_chain(ModelRole.RETRIEVAL_LLM)
            if chain:
                return chain[0][1]
        return None

    # 向后兼容别名（旧代码可能还在用 _get_llm）
    _get_llm = get_llm

    @property
    def available(self) -> bool:
        """知识图谱服务是否可用。"""
        return neo4j_manager.available

    # ── 关键词提取（供工具和检索管道复用）────────────────────

    async def extract_keywords(
        self,
        query: str,
        context: str = "",
        max_keywords: int = 5,
    ) -> list[str]:
        """从查询文本中提取图谱搜索关键词。

        LLM 可用时用 LLM 提取实体名/概念词；不可用时回退到启发式规则。
        Args:
            query: 查询文本
            context: 可选的附加上下文（如文档片段）
            max_keywords: 最多返回关键词数

        Returns:
            关键词列表，最多 max_keywords 个
        """
        llm = self.get_llm()
        if llm is not None:
            context_section = f"\n相关上下文:\n{context}" if context else ""
            prompt = f"""从以下问题中提取用于知识图谱搜索的实体名称关键词。
只输出关键词，每行一个，最多 {max_keywords} 个。

规则：
- 提取实体名称（人名、部门名、政策名、概念、术语等）
- 去掉疑问词和停用词
- 如果没有明确实体，提取核心概念词

问题: {query}{context_section}

关键词（每行一个）:"""

            try:
                response = await llm.acomplete(prompt)
                keywords = [
                    line.strip().lstrip("- ").strip()
                    for line in response.text.strip().split("\n")
                    if line.strip() and len(line.strip()) >= 2
                ]
                if keywords:
                    logger.debug("[KG] LLM 提取关键词: %s", keywords[:max_keywords])
                    return keywords[:max_keywords]
            except Exception as e:
                logger.debug("[KG] LLM 关键词提取失败，回退启发式: %s", e)

        # 回退：正则分词
        import re
        return [
            w for w in re.split(r'[，。、；\s,.;：:？?！!]+', query)
            if len(w) >= 2
        ][:max_keywords]

    # ── 实体和关系抽取 ───────────────────────────────────────

    @observe(name="KG.extract_entities")
    async def extract_entities(
        self,
        text: str,
        doc_id: str = "",
        max_entities: int = 20,
    ) -> tuple[list[Entity], list[Relation]]:
        """从文本中抽取实体和关系（LLM 驱动）。

        Args:
            text: 要抽取的文本内容
            doc_id: 来源文档 ID
            max_entities: 最多抽取的实体数量

        Returns:
            (实体列表, 关系列表)
        """
        llm = self._get_llm()
        if llm is None:
            logger.warning("[KG] LLM 不可用，跳过实体抽取")
            return [], []

        if not text or len(text.strip()) < 10:
            return [], []

        # 截断过长文本（保护 token 消耗）
        text_truncated = text[:4000] if len(text) > 4000 else text

        entity_types_str = ", ".join(self.ENTITY_TYPES)
        prompt = f"""你是一个知识图谱构建助手。请从以下文本中抽取实体和关系。

**实体类型**（必须从以下类型中选择）: {entity_types_str}

**要求**:
1. 抽取重要实体（人名、组织、政策、术语、概念、流程等），最多 {max_entities} 个
2. 为每个实体写一句话的 description（基于原文内容）
3. 识别实体间的关系：BELONGS_TO（属于）、RELATED_TO（相关）、PART_OF（部分）、REQUIRES（需要）、MANAGES（管理）、REPORTS_TO（汇报给）、PRODUCES（产出）、FOLLOWS（遵循）

**输出 JSON 格式**（只输出 JSON，不要其他内容）:
{{
  "entities": [
    {{"name": "实体名", "type": "实体类型", "description": "一句话描述"}}
  ],
  "relations": [
    {{"source": "源实体名", "target": "目标实体名", "relation": "关系类型", "description": "关系说明"}}
  ]
}}

**文本**:
{text_truncated}

请抽取:"""

        try:
            response = await llm.acomplete(prompt)
            raw = response.text.strip()

            # 提取 JSON（可能被 markdown 代码块包裹）
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()

            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("[KG] LLM 输出 JSON 解析失败: %s | raw=%s", e, raw[:200])
            return [], []
        except Exception as e:
            logger.error("[KG] 实体抽取失败: %s", e)
            return [], []

        entities = []
        for e in data.get("entities", [])[:max_entities]:
            if not e.get("name") or not e.get("type"):
                continue
            # 类型标准化
            etype = e["type"].upper().strip()
            if etype not in self.ENTITY_TYPES:
                etype = "CONCEPT"
            entities.append(Entity(
                name=e["name"].strip(),
                type=etype,
                description=e.get("description", "").strip(),
                source_doc_id=doc_id,
                source_snippet=text_truncated[:200],
            ))

        relations = []
        entity_names = {e.name for e in entities}
        for r in data.get("relations", []):
            src = r.get("source", "").strip()
            tgt = r.get("target", "").strip()
            if not src or not tgt:
                continue
            # 只保留两端实体都被抽取到的关系
            if src not in entity_names and tgt not in entity_names:
                continue
            relations.append(Relation(
                source=src,
                target=tgt,
                relation=r.get("relation", "RELATED_TO").upper().strip(),
                description=r.get("description", "").strip(),
                source_doc_id=doc_id,
            ))

        logger.info(
            "[KG] 抽取完成: %d 个实体, %d 个关系 (doc=%s)",
            len(entities), len(relations), doc_id,
        )
        return entities, relations

    # ── 写入 Neo4j ───────────────────────────────────────────

    @observe(name="KG.store_entities")
    async def store_entities(
        self,
        entities: list[Entity],
        relations: list[Relation],
        doc_id: str = "",
    ) -> int:
        """将实体和关系批量写入 Neo4j（UNWIND 批量写入，减少网络往返）。

        使用 MERGE 实现幂等去重（同名同类型实体只创建一次，
        但会更新 description 和来源信息）。

        Args:
            entities: 实体列表
            relations: 关系列表
            doc_id: 关联文档 ID

        Returns:
            成功写入的实体数量
        """
        if not neo4j_manager.available:
            logger.warning("[KG] Neo4j 不可用，跳过存储")
            return 0

        stored_count = 0

        # ── 批量写入实体（UNWIND 单次网络往返） ──────────────
        if entities:
            try:
                records = await neo4j_manager.run_write(
                    """
                    UNWIND $entities AS entity
                    MERGE (e:Entity {name: entity.name, type: entity.type})
                    SET e.description = CASE
                        WHEN entity.description <> '' THEN entity.description
                        ELSE COALESCE(e.description, '')
                    END
                    SET e.source_doc_id = CASE
                        WHEN entity.source_doc_id <> '' THEN entity.source_doc_id
                        ELSE COALESCE(e.source_doc_id, '')
                    END
                    SET e.updated_at = datetime()
                    FOREACH (_ IN CASE WHEN $doc_id <> '' THEN [1] ELSE [] END |
                        MERGE (d:Document {doc_id: $doc_id})
                        MERGE (e)-[:MENTIONED_IN]->(d)
                    )
                    RETURN count(e) AS stored_count
                    """,
                    {
                        "entities": [e.to_dict() for e in entities],
                        "doc_id": doc_id,
                    },
                )
                if records:
                    stored_count = records[0].get("stored_count", len(entities))
            except Exception as e:
                logger.warning("[KG] 实体批量写入失败，回退逐条写入: %s", e)
                # 回退：逐个写入（兼容 Neo4j 旧版本不支持 UNWIND 的情况）
                for entity in entities:
                    try:
                        await neo4j_manager.run_write(
                            """
                            MERGE (e:Entity {name: $name, type: $type})
                            SET e.description = CASE
                                WHEN $description <> '' THEN $description
                                ELSE COALESCE(e.description, '')
                            END
                            SET e.source_doc_id = CASE
                                WHEN $source_doc_id <> '' THEN $source_doc_id
                                ELSE COALESCE(e.source_doc_id, '')
                            END
                            SET e.updated_at = datetime()
                            FOREACH (_ IN CASE WHEN $doc_id <> '' THEN [1] ELSE [] END |
                                MERGE (d:Document {doc_id: $doc_id})
                                MERGE (e)-[:MENTIONED_IN]->(d)
                            )
                            """,
                            {
                                "name": entity.name,
                                "type": entity.type,
                                "description": entity.description,
                                "source_doc_id": entity.source_doc_id,
                                "doc_id": doc_id,
                            },
                        )
                        stored_count += 1
                    except Exception as e2:
                        logger.warning("[KG] 实体写入失败 '%s': %s", entity.name, e2)

        # ── 批量写入关系（按类型分组，每组一次 UNWIND） ─────
        from collections import defaultdict

        rels_by_type: dict[str, list[Relation]] = defaultdict(list)
        for rel in relations:
            # 清理关系类型名称
            rel_type = rel.relation.replace("`", "").replace("'", "")
            if not rel_type or len(rel_type) > 50:
                rel_type = "RELATED_TO"
            rels_by_type[rel_type].append(rel)

        for rel_type, rels in rels_by_type.items():
            try:
                await neo4j_manager.run_write(
                    f"""
                    UNWIND $relations AS rel
                    MATCH (a:Entity {{name: rel.source}})
                    MATCH (b:Entity {{name: rel.target}})
                    MERGE (a)-[r:`{rel_type}`]->(b)
                    SET r.description = CASE
                        WHEN rel.description <> '' THEN rel.description
                        ELSE COALESCE(r.description, '')
                    END
                    SET r.source_doc_id = CASE
                        WHEN $doc_id <> '' THEN $doc_id
                        ELSE COALESCE(r.source_doc_id, '')
                    END
                    SET r.updated_at = datetime()
                    """,
                    {
                        "relations": [
                            {
                                "source": r.source,
                                "target": r.target,
                                "description": r.description,
                            }
                            for r in rels
                        ],
                        "doc_id": doc_id,
                    },
                )
            except Exception as e:
                logger.warning(
                    "[KG] 关系批量写入失败 (type=%s, count=%d): %s",
                    rel_type, len(rels), e,
                )

        logger.info("[KG] 存储完成: %d 个实体 (doc=%s)", stored_count, doc_id)
        return stored_count

    # ── 文档批量处理 ─────────────────────────────────────────

    @observe(name="KG.process_document")
    async def process_document(
        self,
        text: str,
        doc_id: str,
        chunk_size: int = 2000,
    ) -> int:
        """处理单个文档 — 分块抽取实体并写入图谱。

        Args:
            text: 文档全文
            doc_id: 文档唯一标识
            chunk_size: 每块文本大小（字符数）

        Returns:
            写入的总实体数
        """
        if not neo4j_manager.available:
            return 0

        # 简单分块（按句子边界粗略分割）
        chunks = []
        current = ""
        for paragraph in text.split("\n"):
            if len(current) + len(paragraph) > chunk_size and current:
                chunks.append(current.strip())
                current = paragraph
            else:
                current += "\n" + paragraph if current else paragraph
        if current.strip():
            chunks.append(current.strip())

        # 限制处理块数（保护 token 消耗）
        max_chunks = min(len(chunks), 10)
        chunks = chunks[:max_chunks]

        total_entities = 0

        # 并行抽取所有分块（LLM 调用是主要瓶颈，chunk 间无依赖）
        semaphore = asyncio.Semaphore(5)  # 限制并发数，避免触发 API 限流

        async def _extract_one(i: int, chunk: str):
            async with semaphore:
                entities, relations = await self.extract_entities(
                    chunk, doc_id=doc_id, max_entities=15,
                )
                logger.debug("[KG] 分块 %d/%d: +%d 实体", i + 1, len(chunks), len(entities))
                return entities, relations

        results = await asyncio.gather(*[
            _extract_one(i, chunk) for i, chunk in enumerate(chunks)
        ])

        all_entities: list[Entity] = []
        all_relations: list[Relation] = []
        for entities, relations in results:
            all_entities.extend(entities)
            all_relations.extend(relations)

        # 全局去重后批量写入
        seen = set()
        deduped_entities = []
        for e in all_entities:
            key = (e.name, e.type)
            if key not in seen:
                seen.add(key)
                deduped_entities.append(e)

        if deduped_entities:
            total_entities = await self.store_entities(deduped_entities, all_relations, doc_id=doc_id)

        logger.info(
            "[KG] 文档处理完成: doc=%s, chunks=%d, entities=%d, relations=%d",
            doc_id, len(chunks), total_entities, len(all_relations),
        )
        return total_entities

    # ── GraphRAG 检索 ────────────────────────────────────────

    @observe(name="KG.graph_rag")
    async def graph_rag(
        self,
        keywords: list[str],
        question: str = "",
        max_entities: int = 15,
        max_relations: int = 30,
        max_hops: int = 2,
    ) -> GraphContext:
        """GraphRAG — 从关键词出发，扩展图谱上下文。

        先根据关键词匹配起点实体，再通过多跳遍历扩展，
        返回一个包含相关实体和关系的子图。

        Args:
            keywords: 关键实体名称或关键词列表
            question: 原始问题（用于摘要生成）
            max_entities: 最多返回实体数
            max_relations: 最多返回关系数
            max_hops: 最大跳数

        Returns:
            GraphContext — 包含实体、关系和摘要
        """
        if not neo4j_manager.available or not keywords:
            return GraphContext()

        # Step 1: 通过关键词在全文索引中搜索匹配实体
        entity_matches: set[str] = set()
        for kw in keywords[:5]:
            # 先尝试精确名称匹配
            exact = await neo4j_manager.run_query(
                """
                MATCH (e:Entity)
                WHERE e.name CONTAINS $keyword
                RETURN e.name AS name, e.type AS type, e.description AS description,
                       e.source_doc_id AS source_doc_id
                LIMIT 10
                """,
                {"keyword": kw},
            )
            for r in exact:
                entity_matches.add(r["name"])

        if not entity_matches:
            logger.info("[KG] GraphRAG: 未找到匹配实体 (keywords=%s)", keywords)
            return GraphContext()

        # Step 2: 多跳遍历扩展子图
        # 从匹配实体出发，通过所有关系类型遍历 N 跳
        name_list = list(entity_matches)
        graph_entities: list[dict] = []
        graph_relations: list[dict] = []

        for hop in range(1, max_hops + 1):
            result = await neo4j_manager.run_query(
                """
                MATCH (a:Entity)-[r]-(b:Entity)
                WHERE a.name IN $names
                RETURN DISTINCT
                    a.name AS source_name, a.type AS source_type,
                    a.source_doc_id AS source_doc_id,
                    type(r) AS relation, r.description AS rel_desc,
                    b.name AS target_name, b.type AS target_type,
                    b.description AS target_desc,
                    b.source_doc_id AS target_source_doc_id
                LIMIT $max_relations
                """,
                {
                    "names": name_list,
                    "max_relations": max_relations,
                },
            )

            for rec in result:
                s_name = rec.get("source_name", "")
                t_name = rec.get("target_name", "")

                # 收集实体
                for n, nt, nd, ns in [
                    (s_name, rec.get("source_type"), None, rec.get("source_doc_id")),
                    (t_name, rec.get("target_type"), rec.get("target_desc"), rec.get("target_source_doc_id")),
                ]:
                    if n and not any(e["name"] == n for e in graph_entities):
                        graph_entities.append({
                            "name": n,
                            "type": nt or "UNKNOWN",
                            "description": nd or "",
                            "source_doc_id": ns or "",
                        })

                # 收集关系
                graph_relations.append({
                    "source": s_name,
                    "target": t_name,
                    "relation": rec.get("relation", "RELATED_TO"),
                    "description": rec.get("rel_desc", ""),
                })

            # 扩展搜索范围 — 将新发现的实体加入下一跳
            new_names = [e["name"] for e in graph_entities if e["name"] not in name_list]
            if not new_names:
                break
            name_list = new_names

        # 限制数量
        graph_entities = graph_entities[:max_entities]
        graph_relations = graph_relations[:max_relations]

        # Step 3: 生成摘要（如果有问题的话）
        summary = ""
        if question and graph_entities:
            summary = await self._summarize_graph(graph_entities, graph_relations, question)

        logger.info(
            "[KG] GraphRAG: %d 实体, %d 关系 (hops=%d, keywords=%s)",
            len(graph_entities), len(graph_relations), max_hops, keywords,
        )

        return GraphContext(
            entities=graph_entities,
            relations=graph_relations,
            summary=summary,
        )

    async def _summarize_graph(
        self,
        entities: list[dict],
        relations: list[dict],
        question: str,
    ) -> str:
        """用 LLM 对检索到的子图生成摘要。"""
        llm = self._get_llm()
        if llm is None:
            return ""

        entities_text = "\n".join(
            f"- [{e['type']}] {e['name']}: {e.get('description', '')}"
            for e in entities[:10]
        )
        relations_text = "\n".join(
            f"- {r['source']} -[{r['relation']}]→ {r['target']}"
            for r in relations[:10]
        )

        prompt = f"""基于以下知识图谱中的实体和关系，用 2-3 句话总结与问题相关的关键信息。

问题: {question}

实体:
{entities_text}

关系:
{relations_text}

摘要（中文，2-3 句，简洁有力）:"""

        try:
            response = await llm.acomplete(prompt)
            return response.text.strip()
        except Exception as e:
            logger.warning("[KG] 摘要生成失败: %s", e)
            return ""

    # ── 实体搜索 ─────────────────────────────────────────────

    @observe(name="KG.search_entities")
    async def search_entities(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict]:
        """全文搜索实体（优先使用 fulltext 索引，失败回退 CONTAINS）。

        Args:
            query: 搜索关键词
            limit: 返回数量上限

        Returns:
            匹配的实体列表
        """
        if not neo4j_manager.available:
            return []

        # 转义 Lucene 特殊字符，避免 fulltext query 解析报错
        import re as _re
        safe_query = _re.sub(r'([+\-&|!(){}\[\]^"~*?:\\])', r'\\\1', query)

        try:
            records = await neo4j_manager.run_query(
                """
                CALL db.index.fulltext.queryNodes("entity_name_ft", $query)
                YIELD node, score
                RETURN node.name AS name, node.type AS type,
                       node.description AS description,
                       node.source_doc_id AS source_doc_id, score
                ORDER BY score DESC
                LIMIT $limit
                """,
                {"query": safe_query, "limit": limit},
            )
            if records:
                return [dict(r) for r in records]
        except Exception as e:
            logger.debug("[KG] fulltext 索引查询失败，回退 CONTAINS: %s", e)

        # 回退：CONTAINS 子串匹配（fulltext 索引不存在时）
        records = await neo4j_manager.run_query(
            """
            MATCH (e:Entity)
            WHERE e.name CONTAINS $query OR e.description CONTAINS $query
            RETURN e.name AS name, e.type AS type, e.description AS description,
                   e.source_doc_id AS source_doc_id
            ORDER BY e.updated_at DESC
            LIMIT $limit
            """,
            {"query": query, "limit": limit},
        )
        return [dict(r) for r in records]

    # ── 删除文档关联的图谱数据 ───────────────────────────────

    async def remove_document_entities(self, doc_id: str) -> int:
        """删除与指定文档关联的所有实体和关系。

        仅删除仅由该文档引入的孤立实体；共享实体保留。

        Args:
            doc_id: 文档 ID

        Returns:
            删除的实体数量
        """
        if not neo4j_manager.available:
            return 0

        try:
            records = await neo4j_manager.run_write(
                """
                MATCH (d:Document {doc_id: $doc_id})
                OPTIONAL MATCH (d)-[r1:MENTIONED_IN]-(e:Entity)
                DETACH DELETE d, r1
                WITH e
                WHERE e IS NOT NULL
                // 删除不再关联任何文档的孤立实体
                MATCH (e)
                WHERE NOT (e)-[:MENTIONED_IN]->(:Document)
                DETACH DELETE e
                RETURN COUNT(e) AS deleted_count
                """,
                {"doc_id": doc_id},
            )
            count = records[0].get("deleted_count", 0) if records else 0
            logger.info("[KG] 文档 %s 图谱数据已清理 (删除 %d 个实体)", doc_id, count)
            return count
        except Exception as e:
            logger.error("[KG] 文档 %s 图谱数据清理失败: %s", doc_id, e)
            return 0


# ── 全局单例 ──────────────────────────────────────────────────

knowledge_graph_service = KnowledgeGraphService()
