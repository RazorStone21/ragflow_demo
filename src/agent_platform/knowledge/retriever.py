"""检索编排：混合检索 → 阈值过滤 → 重排 → 证据（CLAUDE.md 7.4 / 7.5）。

设计要点：
- **无结果不是异常**：证据不足返回空证据 + ``is_sufficient=False``，
  由上层走"不编造"降级分支（CLAUDE.md 7.5 / NO-12）；
- **重排可降级**：重排失败不阻断检索，退回融合分排序并标记
  ``rerank_applied=False``（CLAUDE.md 3.6）；
- **租户过滤强制**：过滤表达式由 ``milvus_schema.tenant_filter`` 构造，
  调用方无法绕过（NO-19）。
"""

from __future__ import annotations

from agent_platform.core.errors import AgentPlatformError
from agent_platform.core.logging import get_logger
from agent_platform.core.tracing import NoOpTracingHook, TracingHook
from agent_platform.knowledge.embedder import Embedder
from agent_platform.knowledge.milvus_schema import tenant_filter
from agent_platform.knowledge.milvus_store import MilvusHit, MilvusStore
from agent_platform.knowledge.reranker import Reranker
from agent_platform.knowledge.schemas import (
    EvidenceItem,
    RetrievalMeta,
    RetrievalOutcome,
    extract_page_numbers,
)
from agent_platform.utils.text import normalize_whitespace

logger = get_logger(__name__)

# 判定理由的稳定取值（供路由与测试断言，禁止随意改写语义）
REASON_SUFFICIENT = "sufficient"
REASON_EMPTY_QUERY = "empty_query"
REASON_NO_CANDIDATES = "no_candidates"
REASON_BELOW_SIMILARITY = "below_similarity_threshold"
REASON_BELOW_RERANK = "below_rerank_threshold"


class Retriever:
    """混合检索 + 重排的证据获取器。

    Args:
        store: Milvus 封装。
        embedder: 向量化器（经网关）。
        reranker: 重排器；``None`` 表示不重排（按融合分排序）。
        top_k: 最终保留的证据条数上限。
        candidates: 每个检索分支的召回条数（重排候选池）。
        similarity_threshold: 融合分预过滤阈值（重排前）。
        rerank_score_threshold: 重排分阈值（重排后），决定证据是否可用。
        vector_weight: 混合检索中稠密分支权重。
        tracing: 埋点钩子（默认空操作，阶段二接 Langfuse/OTel）。
    """

    def __init__(
        self,
        store: MilvusStore,
        embedder: Embedder,
        reranker: Reranker | None,
        *,
        top_k: int,
        candidates: int,
        similarity_threshold: float,
        rerank_score_threshold: float,
        vector_weight: float,
        tracing: TracingHook | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._reranker = reranker
        self._top_k = top_k
        self._candidates = candidates
        self._similarity_threshold = similarity_threshold
        self._rerank_score_threshold = rerank_score_threshold
        self._vector_weight = vector_weight
        self._tracing = tracing or NoOpTracingHook()

    async def search(
        self, query: str, *, tenant_id: str, dataset_id: str | None = None
    ) -> RetrievalOutcome:
        """检索证据。

        Args:
            query: 用户问题原文。
            tenant_id: 认证上下文解析出的租户标识（不得由前端决定，NO-19）。
            dataset_id: 可选的数据集限定。

        Returns:
            ``RetrievalOutcome``；证据不足时为**空证据**而非异常。

        Raises:
            KnowledgeError 及其子类: 向量化或向量库不可用（基础设施故障，
                与"检索不到"区分开）。
        """
        normalized = normalize_whitespace(query)
        if not normalized:
            return _empty_outcome("", REASON_EMPTY_QUERY, self._rerank_score_threshold)

        query_vector = await self._embedder.embed_query(normalized)
        hits = await self._store.hybrid_search(
            query_text=normalized,
            query_vector=query_vector,
            expr=tenant_filter(tenant_id, dataset_id),
            candidates=self._candidates,
            vector_weight=self._vector_weight,
        )
        if not hits:
            return _empty_outcome(normalized, REASON_NO_CANDIDATES, self._rerank_score_threshold)

        candidates = [hit for hit in hits if hit.score >= self._similarity_threshold]
        if not candidates:
            return _empty_outcome(normalized, REASON_BELOW_SIMILARITY, self._rerank_score_threshold)

        scored, rerank_applied = await self._apply_rerank(normalized, candidates)
        kept = [item for item in scored if item.score >= self._rerank_score_threshold]
        threshold = self._rerank_score_threshold
        if not kept:
            return _empty_outcome(
                normalized,
                REASON_BELOW_RERANK,
                threshold,
                candidate_count=len(candidates),
                rerank_applied=rerank_applied,
            )

        evidence = kept[: self._top_k]
        self._tracing.record_retrieval(
            query=normalized,
            hit_count=len(evidence),
            top_score=evidence[0].score,
            tenant_id=tenant_id,
        )
        logger.info(
            "retrieval.completed",
            extra={
                "event": "retrieval.completed",
                "candidate_count": len(candidates),
                "evidence_count": len(evidence),
                "rerank_applied": rerank_applied,
                "top_score": round(evidence[0].score, 4),
            },
        )
        return RetrievalOutcome(
            evidence=evidence,
            meta=RetrievalMeta(
                query=normalized,
                candidate_count=len(candidates),
                evidence_count=len(evidence),
                top_score=evidence[0].score,
                score_threshold=threshold,
                is_sufficient=True,
                reason=REASON_SUFFICIENT,
                rerank_applied=rerank_applied,
            ),
        )

    async def _apply_rerank(
        self, query: str, candidates: list[MilvusHit]
    ) -> tuple[list[EvidenceItem], bool]:
        """重排候选（失败则降级为融合分排序）。

        Returns:
            ``(证据列表, 是否成功重排)``；返回列表按分数降序。
        """
        fused = [_to_evidence(hit, hit.score) for hit in candidates]
        if self._reranker is None or len(candidates) < 2:
            # 单条候选无排序意义，且省一次网关调用
            return sorted(fused, key=lambda item: item.score, reverse=True), False
        try:
            ranked = await self._reranker.rerank(
                query, [hit.content for hit in candidates], top_n=self._top_k
            )
        except AgentPlatformError as exc:
            # 重排是增强而非必需：降级但显式记录，不静默（CLAUDE.md 3.6 / NO-06）
            logger.warning(
                "retrieval.rerank_degraded",
                extra={"event": "retrieval.rerank_degraded", "error": type(exc).__name__},
            )
            return sorted(fused, key=lambda item: item.score, reverse=True), False
        reranked = [_to_evidence(candidates[index], score) for index, score in ranked]
        return reranked, True


def _to_evidence(hit: MilvusHit, score: float) -> EvidenceItem:
    """把检索命中转换为证据条目（保留引用信息，CLAUDE.md 7.4）。

    Notes:
        ``vector_score`` / ``bm25_score`` **恒为 ``None``，且不是本函数丢的**：
        ``MilvusStore.hybrid_search`` 走的是 Milvus ``hybrid_search`` +
        ``WeightedRanker``，该调用路径**只返回融合后的单一分数**，分路原始分
        在数据源处就没有被取回。要拿到分路分数，得把一次混合检索拆成两次独立
        ``search``（稠密一次、稀疏一次）再自行融合——那是**检索形态的变更**，
        按 CLAUDE.md 9.2 / NO-16 必须先有评估基线才谈得上评估其影响，
        因此当前只在评估层登记该缺口，不动检索行为。
    """
    return EvidenceItem(
        chunk_id=hit.chunk_id,
        content=hit.content,
        score=score,
        vector_score=None,
        bm25_score=None,
        dataset_id=hit.dataset_id,
        document_id=hit.document_id,
        document_name=hit.document_name,
        page_numbers=extract_page_numbers(hit.positions),
        source_uri=hit.source_uri,
        media_type=hit.media_type,
        images=list(hit.images),
    )


def _empty_outcome(
    query: str,
    reason: str,
    threshold: float,
    *,
    candidate_count: int = 0,
    rerank_applied: bool = False,
) -> RetrievalOutcome:
    """构造"证据不足"结果（空证据 + 判定理由，非异常）。"""
    return RetrievalOutcome(
        evidence=[],
        meta=RetrievalMeta(
            query=query,
            candidate_count=candidate_count,
            evidence_count=0,
            top_score=None,
            score_threshold=threshold,
            is_sufficient=False,
            reason=reason,
            rerank_applied=rerank_applied,
        ),
    )
