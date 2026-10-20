"""Milvus 封装：建集合幂等、结构校验、批量入库、混合检索与租户过滤。"""

from __future__ import annotations

import hashlib
from typing import Any, cast

import pytest
from pymilvus import AsyncMilvusClient

from agent_platform.core.errors import ConfigurationError, KnowledgeError, RetrievalFailedError
from agent_platform.knowledge import milvus_schema as schema
from agent_platform.knowledge.milvus_store import MilvusStore
from agent_platform.knowledge.schemas import ChunkRecord, ImageRef, MediaType, build_image_id

_DIM = 4


class FakeMilvusClient:
    """最小 Milvus 客户端 fake：记录调用参数并返回可编排的结果。"""

    def __init__(
        self,
        *,
        exists: bool = False,
        dim: int = _DIM,
        search_rows: list[dict[str, Any]] | None = None,
        query_rows: list[dict[str, Any]] | None = None,
        missing_fields: set[str] | None = None,
    ) -> None:
        self.exists = exists
        self.dim = dim
        self.search_rows = search_rows if search_rows is not None else []
        self.query_rows = query_rows if query_rows is not None else []
        self.missing_fields = missing_fields or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def has_collection(self, collection_name: str, **kwargs: Any) -> bool:
        self.calls.append(("has_collection", {"collection_name": collection_name}))
        return self.exists

    async def create_collection(self, **kwargs: Any) -> None:
        self.calls.append(("create_collection", kwargs))
        self.exists = True

    async def load_collection(self, collection_name: str, **kwargs: Any) -> None:
        self.calls.append(("load_collection", {"collection_name": collection_name}))

    async def describe_collection(self, collection_name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("describe_collection", {"collection_name": collection_name}))
        return {
            "fields": [
                {
                    "name": name,
                    "params": {"dim": self.dim} if name == schema.FIELD_CONTENT_EMBEDDING else {},
                }
                for name in schema.REQUIRED_SCHEMA_FIELDS
                if name not in self.missing_fields
            ]
        }

    async def upsert(
        self, collection_name: str, data: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(("upsert", {"collection_name": collection_name, "data": data}))
        return {"upsert_count": len(data)}

    async def delete(self, collection_name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete", {"collection_name": collection_name, **kwargs}))
        return {"delete_count": 2}

    async def query(self, collection_name: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("query", {"collection_name": collection_name, **kwargs}))
        return self.query_rows

    async def hybrid_search(
        self, collection_name: str, **kwargs: Any
    ) -> list[list[dict[str, Any]]]:
        self.calls.append(("hybrid_search", {"collection_name": collection_name, **kwargs}))
        return [self.search_rows]

    async def close(self) -> None:
        self.closed = True

    def method_calls(self, name: str) -> list[dict[str, Any]]:
        """取出某方法的全部调用参数。"""
        return [payload for call, payload in self.calls if call == name]


def _store(client: FakeMilvusClient, **overrides: Any) -> MilvusStore:
    """用 fake 客户端构造 MilvusStore。"""
    kwargs: dict[str, Any] = {"collection": "chunks", "dim": _DIM, "timeout_s": 1.0}
    kwargs.update(overrides)
    # fake 与真实客户端同构，测试内显式转型（CLAUDE.md 9.3 允许注入 fake）
    return MilvusStore(cast(AsyncMilvusClient, client), **kwargs)


def _record(index: int, **overrides: Any) -> ChunkRecord:
    """构造一条切分单元。"""
    base: dict[str, Any] = {
        "chunk_id": f"c{index}",
        "content": f"第 {index} 段政策正文",
        "tenant_id": "demo",
        "dataset_id": "ds1",
        "document_id": "doc1",
        "document_name": "政策.pdf",
        "positions": [[1, 0, 10, 0, 10]],
        "source_uri": "gov_doc/政策.pdf",
    }
    base.update(overrides)
    return ChunkRecord(**base)


def _vector(value: float = 0.5) -> list[float]:
    """构造一条指定维度的向量。"""
    return [value] * _DIM


# ---------------------------------------------------------------------------
# 建集合
# ---------------------------------------------------------------------------
async def test_ensure_collection_creates_then_is_idempotent() -> None:
    """集合不存在时创建并加载；已存在时不重建。"""
    client = FakeMilvusClient(exists=False)
    store = _store(client)

    assert await store.ensure_collection() is True
    created = client.method_calls("create_collection")[0]
    assert created["collection_name"] == "chunks"
    assert created["index_params"] is not None
    assert client.method_calls("load_collection")

    assert await store.ensure_collection() is False
    assert len(client.method_calls("create_collection")) == 1


async def test_ensure_collection_fails_fast_on_dim_mismatch() -> None:
    """已存在集合的维度与配置不符时 fail-fast，避免写入错位向量。"""
    client = FakeMilvusClient(exists=True, dim=1024)
    with pytest.raises(ConfigurationError, match="向量维度"):
        await _store(client).ensure_collection()


async def test_ensure_collection_fails_fast_on_missing_media_fields() -> None:
    """旧集合缺 media_type/images 时 fail-fast，并给出重建方式（不自动删库）。"""
    client = FakeMilvusClient(
        exists=True, missing_fields={schema.FIELD_MEDIA_TYPE, schema.FIELD_IMAGES}
    )
    with pytest.raises(ConfigurationError, match="缺少必需字段") as excinfo:
        await _store(client).ensure_collection()

    assert schema.FIELD_MEDIA_TYPE in str(excinfo.value), "诊断信息必须点名缺了哪一列"
    assert "gov_policy_chunks_v2" in excinfo.value.detail, "错误信息必须给出可直接执行的下一步"
    assert client.method_calls("create_collection") == [], "不得自动删/建集合"


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
async def test_upsert_rejects_length_mismatch() -> None:
    """记录数与向量数不一致时中止写入。"""
    with pytest.raises(KnowledgeError, match="不匹配"):
        await _store(FakeMilvusClient()).upsert_chunks([_record(1)], [])


async def test_upsert_rejects_wrong_dimension() -> None:
    """向量维度与配置不符时中止写入。"""
    with pytest.raises(KnowledgeError, match="维度"):
        await _store(FakeMilvusClient()).upsert_chunks([_record(1)], [[0.1, 0.2]])


async def test_upsert_batches_and_excludes_sparse_field() -> None:
    """超过批大小时分次写入；行数据不含由 BM25 函数生成的稀疏字段。"""
    client = FakeMilvusClient()
    store = _store(client)
    records = [_record(i) for i in range(70)]

    written = await store.upsert_chunks(records, [_vector() for _ in records])

    assert written == 70
    batches = client.method_calls("upsert")
    assert [len(batch["data"]) for batch in batches] == [64, 6]
    assert schema.FIELD_CONTENT_SPARSE not in batches[0]["data"][0]
    assert batches[0]["data"][0][schema.FIELD_CHUNK_ID] == "c0"
    assert batches[0]["data"][0][schema.FIELD_TENANT_ID] == "demo"


async def test_upsert_empty_is_noop() -> None:
    """空输入不产生写入调用。"""
    client = FakeMilvusClient()
    assert await _store(client).upsert_chunks([], []) == 0
    assert client.method_calls("upsert") == []


async def test_upsert_writes_media_type_and_image_refs() -> None:
    """媒介类型与图片元数据随行进集合；图片序列化为 JSON 原生结构。"""
    sha = hashlib.sha256(b"fig").hexdigest()
    ref = ImageRef(
        image_id=build_image_id(sha), sha256=sha, mime="image/png", caption="图 1", page=2
    )
    client = FakeMilvusClient()
    record = _record(1, media_type=MediaType.IMAGE, images=[ref])

    await _store(client).upsert_chunks([record], [_vector()])

    row = client.method_calls("upsert")[0]["data"][0]
    assert row[schema.FIELD_MEDIA_TYPE] == "image"
    assert row[schema.FIELD_IMAGES] == [ref.model_dump(mode="json")]


async def test_delete_by_document_scopes_to_tenant() -> None:
    """按文档删除必须同时限定租户（NO-19 隔离不得绕过）。"""
    client = FakeMilvusClient()
    deleted = await _store(client).delete_by_document("doc1", "demo")

    assert deleted == 2
    expression = client.method_calls("delete")[0]["filter"]
    assert schema.FIELD_TENANT_ID in expression
    assert '"demo"' in expression
    assert schema.FIELD_DOCUMENT_ID in expression


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def _image_payload(**overrides: Any) -> dict[str, Any]:
    """构造一条存储态的图片元数据（JSON 原生 dict）。"""
    sha = hashlib.sha256(b"fig").hexdigest()
    payload: dict[str, Any] = {
        "image_id": build_image_id(sha),
        "sha256": sha,
        "mime": "image/png",
        "caption": "图 1",
        "page": 2,
    }
    payload.update(overrides)
    return payload


def _search_row(
    chunk_id: str = "c1",
    score: float = 0.8,
    *,
    nested: bool = True,
    media_type: str = "text",
    images: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造一条检索返回行（nested=True 用 entity 包裹形态）。"""
    entity = {
        schema.FIELD_CHUNK_ID: chunk_id,
        schema.FIELD_CONTENT: "证据正文",
        schema.FIELD_DATASET_ID: "ds1",
        schema.FIELD_DOCUMENT_ID: "doc1",
        schema.FIELD_DOCUMENT_NAME: "政策.pdf",
        schema.FIELD_SOURCE_URI: "gov_doc/政策.pdf",
        schema.FIELD_POSITIONS: [[2, 0, 10, 0, 10]],
        schema.FIELD_UPDATED_AT: 1700000000,
        schema.FIELD_MEDIA_TYPE: media_type,
        schema.FIELD_IMAGES: images if images is not None else [],
    }
    if nested:
        return {"id": chunk_id, "distance": score, "entity": entity}
    return {**entity, "id": chunk_id, "distance": score}


async def test_hybrid_search_builds_two_branches_with_tenant_filter() -> None:
    """混合检索构造稠密 + BM25 两个分支，并把租户过滤表达式原样下发。"""
    client = FakeMilvusClient(search_rows=[_search_row()])
    store = _store(client)

    hits = await store.hybrid_search(
        query_text="电价机制",
        query_vector=_vector(),
        expr='tenant_id == "demo"',
        candidates=32,
        vector_weight=0.7,
    )

    payload = client.method_calls("hybrid_search")[0]
    dense, sparse = payload["reqs"]
    assert dense.anns_field == schema.FIELD_CONTENT_EMBEDDING
    assert sparse.anns_field == schema.FIELD_CONTENT_SPARSE
    assert sparse.data == ["电价机制"]  # BM25 分支收原文，不收向量
    assert dense.expr == 'tenant_id == "demo"'
    assert payload["limit"] == 32
    assert len(hits) == 1
    assert hits[0].chunk_id == "c1"
    assert hits[0].score == pytest.approx(0.8)


async def test_hybrid_search_maps_positions_and_accepts_flat_rows() -> None:
    """两种返回形态都能映射；positions 供页号引用使用。"""
    client = FakeMilvusClient(
        search_rows=[_search_row("a", 0.9, nested=False), _search_row("b", 0.5)]
    )
    hits = await _store(client).hybrid_search(
        query_text="问题",
        query_vector=_vector(),
        expr='tenant_id == "demo"',
        candidates=8,
        vector_weight=0.5,
    )
    assert [hit.chunk_id for hit in hits] == ["a", "b"]
    assert hits[0].positions == [[2, 0, 10, 0, 10]]


async def test_hybrid_search_rejects_wrong_query_dimension() -> None:
    """查询向量维度不符时中止检索。"""
    with pytest.raises(RetrievalFailedError, match="查询向量维度"):
        await _store(FakeMilvusClient()).hybrid_search(
            query_text="问题", query_vector=[0.1, 0.2], expr="", candidates=8, vector_weight=0.5
        )


async def test_hybrid_search_skips_unrecognized_rows() -> None:
    """契约外的返回元素被跳过，而不是抛错（交由上层走无证据分支）。"""
    client = FakeMilvusClient(search_rows=[{"unexpected": True}, _search_row("ok")])
    hits = await _store(client).hybrid_search(
        query_text="问题", query_vector=_vector(), expr="", candidates=8, vector_weight=0.5
    )
    assert [hit.chunk_id for hit in hits] == ["ok"]


async def test_hybrid_search_maps_media_type_and_images() -> None:
    """检索命中带出媒介类型与图片元数据（ADR-0004）。"""
    payload = _image_payload()
    client = FakeMilvusClient(search_rows=[_search_row("c1", media_type="table", images=[payload])])

    hits = await _store(client).hybrid_search(
        query_text="问题", query_vector=_vector(), expr="", candidates=8, vector_weight=0.5
    )

    assert hits[0].media_type is MediaType.TABLE
    assert len(hits[0].images) == 1
    assert hits[0].images[0].image_id == payload["image_id"]


async def test_hybrid_search_degrades_unknown_media_and_skips_bad_images() -> None:
    """未知媒介类型降级为 text；非法图片条目跳过但不丢整条证据。"""
    client = FakeMilvusClient(
        search_rows=[
            _search_row(
                "c1", media_type="video", images=[{"image_id": "不是合法 ID"}, _image_payload()]
            )
        ]
    )

    hits = await _store(client).hybrid_search(
        query_text="问题", query_vector=_vector(), expr="", candidates=8, vector_weight=0.5
    )

    assert hits[0].media_type is MediaType.TEXT, "未知枚举不得让整条证据消失"
    assert len(hits[0].images) == 1, "非法图片条目只跳过自身"


async def test_hybrid_search_empty_returns_empty_list() -> None:
    """无命中返回空列表（不是异常，NO-12）。"""
    client = FakeMilvusClient(search_rows=[])
    hits = await _store(client).hybrid_search(
        query_text="问题", query_vector=_vector(), expr="", candidates=8, vector_weight=0.5
    )
    assert hits == []


async def test_count_parses_scalar_result() -> None:
    """count(*) 结果解析为整数。"""
    client = FakeMilvusClient(query_rows=[{"count(*)": 7}])
    assert await _store(client).count("demo") == 7


async def test_close_delegates_to_client() -> None:
    """close() 关闭底层连接。"""
    client = FakeMilvusClient()
    await _store(client).close()
    assert client.closed is True
