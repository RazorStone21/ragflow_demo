"""检索编排：阈值判定、降级路径与"无证据不抛错"（CLAUDE.md 7.5 / NO-12）。"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from agent_platform.core.errors import ModelGatewayError
from agent_platform.knowledge.milvus_store import MilvusHit
from agent_platform.knowledge.retriever import (
    REASON_BELOW_RERANK,
    REASON_BELOW_SIMILARITY,
    REASON_EMPTY_QUERY,
    REASON_NO_CANDIDATES,
    REASON_SUFFICIENT,
    Retriever,
)
from agent_platform.knowledge.schemas import ImageRef, MediaType, build_image_id

_DIM = 4


class FakeEmbedder:
    """确定性向量化器：记录调用次数，返回固定向量。"""

    def __init__(self, dim: int = _DIM, *, raises: BaseException | None = None) -> None:
        self._dim = dim
        self.raises = raises
        self.queries: list[str] = []

    @property
    def dim(self) -> int:
        return self._dim

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.5] * self._dim for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        if self.raises is not None:
            raise self.raises
        return [0.5] * self._dim


class FakeStore:
    """确定性检索后端：记录下发的过滤表达式与参数。"""

    def __init__(self, hits: list[MilvusHit] | None = None) -> None:
        self.hits = hits or []
        self.calls: list[dict[str, Any]] = []

    async def hybrid_search(self, **kwargs: Any) -> list[MilvusHit]:
        self.calls.append(kwargs)
        return self.hits


class FakeReranker:
    """确定性重排器：按入参顺序给出递减分数，或抛出预设异常。"""

    def __init__(
        self, scores: list[float] | None = None, *, raises: BaseException | None = None
    ) -> None:
        self.scores = scores
        self.raises = raises
        self.calls: list[list[str]] = []

    async def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[tuple[int, float]]:
        self.calls.append(documents)
        if self.raises is not None:
            raise self.raises
        scores = self.scores if self.scores is not None else [0.1] * len(documents)
        return sorted(enumerate(scores), key=lambda pair: pair[1], reverse=True)


def _hit(index: int, score: float, **overrides: Any) -> MilvusHit:
    """构造一条检索命中。"""
    base: dict[str, Any] = {
        "chunk_id": f"c{index}",
        "content": f"第 {index} 段正文",
        "score": score,
        "dataset_id": "ds1",
        "document_id": "doc1",
        "document_name": "政策.pdf",
        "source_uri": "gov_doc/政策.pdf",
        "positions": [[3, 0, 10, 0, 10]],
    }
    base.update(overrides)
    return MilvusHit(**base)


def _retriever(
    store: FakeStore,
    embedder: FakeEmbedder | None = None,
    reranker: Any = None,
    **overrides: Any,
) -> Retriever:
    """构造默认阈值的检索器（阈值 0.2/0.3，top_k=3，候选 16）。"""
    kwargs: dict[str, Any] = {
        "top_k": 3,
        "candidates": 16,
        "similarity_threshold": 0.2,
        "rerank_score_threshold": 0.3,
        "vector_weight": 0.7,
    }
    kwargs.update(overrides)
    return Retriever(
        store,  # type: ignore[arg-type]  # 注入 fake（CLAUDE.md 9.3）
        embedder or FakeEmbedder(),  # 结构上满足 Embedder 协议
        reranker,
        **kwargs,
    )


async def test_search_returns_evidence_with_citations() -> None:
    """命中证据携带文档名、页码与来源（CLAUDE.md 7.4）。"""
    store = FakeStore([_hit(1, 0.9), _hit(2, 0.5)])
    outcome = await _retriever(store, reranker=FakeReranker([0.8, 0.4])).search(
        "新能源电价机制", tenant_id="demo"
    )

    assert outcome.meta.is_sufficient is True
    assert outcome.meta.reason == REASON_SUFFICIENT
    assert outcome.meta.rerank_applied is True
    assert [item.chunk_id for item in outcome.evidence] == ["c1", "c2"]
    assert outcome.evidence[0].page_numbers == [3]
    assert outcome.evidence[0].document_name == "政策.pdf"
    assert outcome.evidence[0].source_uri == "gov_doc/政策.pdf"


async def test_search_evidence_carries_media_and_images() -> None:
    """证据原样带出媒介类型与图片引用，供引用与取图使用（ADR-0004）。"""
    sha = hashlib.sha256(b"fig").hexdigest()
    ref = ImageRef(image_id=build_image_id(sha), sha256=sha, mime="image/png", page=3)
    store = FakeStore([_hit(1, 0.9, media_type=MediaType.IMAGE, images=[ref])])

    outcome = await _retriever(store).search("电价曲线图", tenant_id="demo")

    assert outcome.evidence[0].media_type is MediaType.IMAGE
    assert outcome.evidence[0].images == [ref]


async def test_search_passes_tenant_filter_to_store() -> None:
    """租户隔离必须下发到向量库过滤表达式（NO-19）。"""
    store = FakeStore([_hit(1, 0.9), _hit(2, 0.9)])
    await _retriever(store, reranker=FakeReranker([0.9, 0.9])).search(
        "电价", tenant_id="tenant_a", dataset_id="ds9"
    )

    expression = store.calls[0]["expr"]
    assert '"tenant_a"' in expression
    assert '"ds9"' in expression


async def test_search_normalizes_query() -> None:
    """问题先做空白归一化，避免把多空格问题原样送检。"""
    store = FakeStore([_hit(1, 0.9), _hit(2, 0.9)])
    outcome = await _retriever(store, reranker=FakeReranker([0.9, 0.9])).search(
        "  新能源   电价  ", tenant_id="demo"
    )
    assert outcome.meta.query == "新能源 电价"
    assert store.calls[0]["query_text"] == "新能源 电价"


async def test_empty_query_short_circuits() -> None:
    """空问题不发起嵌入与检索。"""
    store = FakeStore([_hit(1, 0.9)])
    embedder = FakeEmbedder()
    outcome = await _retriever(store, embedder).search("   ", tenant_id="demo")

    assert outcome.meta.reason == REASON_EMPTY_QUERY
    assert outcome.meta.is_sufficient is False
    assert embedder.queries == []
    assert store.calls == []


async def test_no_candidates_is_not_an_error() -> None:
    """向量库无命中时返回空证据而非异常（NO-12）。"""
    outcome = await _retriever(FakeStore([])).search("无关问题", tenant_id="demo")

    assert outcome.evidence == []
    assert outcome.meta.reason == REASON_NO_CANDIDATES
    assert outcome.meta.is_sufficient is False


async def test_candidates_below_similarity_threshold_are_dropped() -> None:
    """融合分低于阈值时不进入证据。"""
    store = FakeStore([_hit(1, 0.05), _hit(2, 0.1)])
    outcome = await _retriever(store).search("问题", tenant_id="demo")

    assert outcome.meta.reason == REASON_BELOW_SIMILARITY
    assert outcome.meta.candidate_count == 0


async def test_rerank_threshold_rejects_weak_evidence() -> None:
    """重排分全部低于阈值时判定证据不足，并保留候选数供观测。"""
    store = FakeStore([_hit(1, 0.9), _hit(2, 0.8)])
    outcome = await _retriever(store, reranker=FakeReranker([0.1, 0.2])).search(
        "问题", tenant_id="demo"
    )

    assert outcome.evidence == []
    assert outcome.meta.reason == REASON_BELOW_RERANK
    assert outcome.meta.candidate_count == 2
    assert outcome.meta.rerank_applied is True


async def test_single_candidate_skips_rerank() -> None:
    """仅一条候选时不做无意义的网关调用。"""
    reranker = FakeReranker([0.9])
    outcome = await _retriever(FakeStore([_hit(1, 0.9)]), reranker=reranker).search(
        "问题", tenant_id="demo"
    )

    assert reranker.calls == []
    assert outcome.meta.rerank_applied is False
    assert outcome.meta.is_sufficient is True


async def test_rerank_failure_degrades_to_fused_order() -> None:
    """重排网关失败时降级为融合分排序，检索仍可用且显式标记降级（CLAUDE.md 3.6）。"""
    store = FakeStore([_hit(1, 0.5), _hit(2, 0.9)])
    reranker = FakeReranker(raises=ModelGatewayError("网关挂了"))
    outcome = await _retriever(store, reranker=reranker).search("问题", tenant_id="demo")

    assert outcome.meta.rerank_applied is False
    assert outcome.meta.is_sufficient is True
    assert [item.chunk_id for item in outcome.evidence] == ["c2", "c1"]  # 按融合分降序


async def test_evidence_is_capped_by_top_k() -> None:
    """证据条数受 top_k 限制（控制上下文体量）。"""
    store = FakeStore([_hit(i, 0.9) for i in range(6)])
    outcome = await _retriever(store, reranker=FakeReranker([0.9] * 6), top_k=2).search(
        "问题", tenant_id="demo"
    )

    assert len(outcome.evidence) == 2
    assert outcome.meta.evidence_count == 2
    assert outcome.meta.candidate_count == 6


async def test_embedding_failure_propagates_as_infrastructure_error() -> None:
    """嵌入网关故障是基础设施异常，与"检索不到"区分开（分层错误处理）。"""
    store = FakeStore([_hit(1, 0.9)])
    embedder = FakeEmbedder(raises=ModelGatewayError("网关不可用"))

    with pytest.raises(ModelGatewayError):
        await _retriever(store, embedder).search("问题", tenant_id="demo")
    assert store.calls == []
