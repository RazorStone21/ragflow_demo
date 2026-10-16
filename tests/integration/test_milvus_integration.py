"""真实 Milvus 的增删查与租户隔离（CLAUDE.md 7.2 / NO-19）。

本文件是**唯一**不需要任何外部密钥的集成测试：向量由测试自己造，BM25 分支由
Milvus 侧就地分词，因此只需 compose 起的 Milvus 就能跑。这让"向量库这条链到底通不通"
有一个不依赖云服务的判定点。

隔离断言是重点：多租户过滤是本平台的纵深防线之一，而它依赖过滤表达式被真正下发到
Milvus（而不是在应用层筛），所以必须在真库上验——fake 掉 Milvus 就只是验了我们自己的
Python 代码。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest
from pymilvus import AsyncMilvusClient

from agent_platform.core.errors import KnowledgeError
from agent_platform.knowledge.milvus_schema import tenant_filter
from agent_platform.knowledge.milvus_store import MilvusStore
from agent_platform.knowledge.schemas import ChunkRecord
from agent_platform.utils.text import content_fingerprint
from tests.integration._support import require_milvus

if TYPE_CHECKING:
    from agent_platform.settings import Settings

pytestmark = pytest.mark.integration

_RUN_SALT = uuid.uuid4().hex[:8]
"""本进程独占的租户后缀，让每次运行的租户名都不一样。

租户名原先写成固定的 ``test-tenant-a`` / ``test-tenant-b``，于是**断言绝对条数**
（如 ``count(TENANT_A) == 3``）就隐含了一个跨运行假设：这个租户名下除了本夹具刚写的
三行再无别行。这个假设是假的——夹具的清理写在 ``finally:`` 里，进程被杀就不执行，
残留会一直留着。实测发生过：某次中断留下的 ``doc-bb2b655b4444``（两个租户各 3 行）
把下一次运行的 ``count(TENANT_A)`` 从 3 顶成 6，测试报 ``assert 6 == 3``。

关键在于**这不是代码错**：那 6 行确实都是 ``test-tenant-a`` 的行，``count`` 按租户
统计的行为完全正确，错的是"这个租户名下只有我写的行"这个前提。所以修的是前提——
每次运行自带私有租户，夹具写入的即该租户的全部内容，绝对条数才重新有意义；顺带
让租户名互不相同的断言（前缀不同即不可能相等）也真的扎得下去。
"""

TENANT_A = f"test-tenant-a-{_RUN_SALT}"
TENANT_B = f"test-tenant-b-{_RUN_SALT}"
DATASET = "integration_test_dataset"

CORPUS = {
    "c1": "居民生活用电实行阶梯电价，第一档为每月零至二百四十千瓦时。",
    "c2": "新能源发电项目上网电价由省级价格主管部门核定并公示。",
    "c3": "电网企业应当按照国家有关规定支付可再生能源电价附加补助资金。",
}
"""三句互不相同的自造文本。

刻意**不**使用真实语料：NO-17 禁止把客户数据提交进仓库，测试夹具也不例外。
文本本身是公开政策常识，不含任何客户信息。
"""


def make_vector(seed: int, dim: int) -> list[float]:
    """造一个单位化的确定性向量（避免零向量被余弦距离判为未定义）。"""
    vector = [0.0] * dim
    vector[seed % dim] = 1.0
    vector[(seed * 7 + 3) % dim] = 0.5
    return vector


def make_record(chunk_id: str, content: str, *, tenant_id: str, document_id: str) -> ChunkRecord:
    """构造一条切分记录。"""
    return ChunkRecord(
        chunk_id=chunk_id,
        content=content,
        tenant_id=tenant_id,
        dataset_id=DATASET,
        document_id=document_id,
        document_name="集成测试样例.pdf",
        positions=[[1, 0, 0, 0, 0]],
        source_uri="integration/sample.pdf",
        content_fingerprint=content_fingerprint(content.encode("utf-8")),
        updated_at=1_700_000_000,
    )


@pytest.fixture
async def milvus_store(integration_settings: Settings) -> AsyncIterator[MilvusStore]:
    """连上真实 Milvus 并确保集合就绪。

    先探一次可达性（``require_milvus``）再拼 ``MilvusStore``：这样"服务没起"（skip）
    与"集合维度配错"（fail）能被区分开——把两者都变成 skip 会掩盖真实的配置事故。
    """
    await require_milvus(integration_settings)
    client = AsyncMilvusClient(
        uri=integration_settings.milvus_uri,
        token=integration_settings.milvus_token.get_secret_value(),
    )
    store = MilvusStore(
        client,
        collection=integration_settings.milvus_collection,
        dim=integration_settings.milvus_dim,
    )
    try:
        await store.ensure_collection()
        yield store
    finally:
        await store.close()


@pytest.fixture
async def seeded(
    milvus_store: MilvusStore, integration_settings: Settings
) -> AsyncIterator[MilvusStore]:
    """写入三个 chunk（两个租户各一份），用后清理。

    ``chunk_id`` 必须**逐租户唯一**：它是 Milvus 主键，而租户只是一列过滤字段。
    同一批里出现重复主键时 Milvus 直接拒收（code=1100），不会"后写覆盖前写"——
    这个坑只有真库会报，fake 掉 Milvus 的单元测试永远看不见。
    """
    dim = integration_settings.milvus_dim
    document_id = f"doc-{uuid.uuid4().hex[:12]}"
    records = [
        make_record(
            f"{document_id}-{tenant}-{key}", text, tenant_id=tenant, document_id=document_id
        )
        for key, text in CORPUS.items()
        for tenant in (TENANT_A, TENANT_B)
    ]
    vectors = [make_vector(index, dim) for index in range(len(records))]
    await milvus_store.upsert_chunks(records, vectors)
    # Milvus 写入是有界一致性：不 flush 的话紧随其后的 count()/检索可能看不到刚写的行
    # （实测立即查得 0、约 1 秒后才可见）。这里显式收口，让断言测的是"写入语义"
    # 而不是"我们跑得比 Milvus 的可见性窗口快"。
    await milvus_store.flush()
    try:
        yield milvus_store
    finally:
        await milvus_store.delete_by_document(document_id, TENANT_A)
        await milvus_store.delete_by_document(document_id, TENANT_B)


async def test_ensure_collection_is_idempotent(milvus_store: MilvusStore) -> None:
    """重复调用不得重建集合（重建会清空已有索引，代价极高）。"""
    assert await milvus_store.ensure_collection() is False


async def test_upsert_then_hybrid_search_finds_chunk(
    seeded: MilvusStore, integration_settings: Settings
) -> None:
    """写入的 chunk 能被混合检索命中，且元数据完整回传（引用可溯源，CLAUDE.md 7.4）。"""
    hits = await seeded.hybrid_search(
        query_text="新能源发电项目上网电价由谁核定？",
        query_vector=make_vector(1, integration_settings.milvus_dim),
        expr=tenant_filter(TENANT_A, DATASET),
        candidates=10,
        vector_weight=0.7,
    )
    assert hits, "混合检索不应零命中"
    matched = next((hit for hit in hits if "上网电价" in hit.content), None)
    assert matched is not None, f"未命中目标切分，实际: {[h.content[:20] for h in hits]}"
    assert matched.document_name == "集成测试样例.pdf"
    assert matched.source_uri == "integration/sample.pdf"
    assert matched.positions == [[1, 0, 0, 0, 0]]
    assert 0.0 <= matched.score <= 1.0, "WeightedRanker 融合分应归一化到 [0,1]"


async def test_tenant_filter_isolates_results(
    seeded: MilvusStore, integration_settings: Settings
) -> None:
    """同一份内容写入两个租户，各自检索只应看到自己的那一份（NO-19）。

    断言的是**条数**而非"没看到别人的 ID"：条数把"过滤没下发、只是恰好没排进 top-k"
    这种情况也一并挡住。
    """
    hits_a = await seeded.hybrid_search(
        query_text="阶梯电价",
        query_vector=make_vector(0, integration_settings.milvus_dim),
        expr=tenant_filter(TENANT_A, DATASET),
        candidates=20,
        vector_weight=0.7,
    )
    hits_b = await seeded.hybrid_search(
        query_text="阶梯电价",
        query_vector=make_vector(0, integration_settings.milvus_dim),
        expr=tenant_filter(TENANT_B, DATASET),
        candidates=20,
        vector_weight=0.7,
    )
    assert hits_a, "租户 A 应能检到自己刚写入的数据"
    assert hits_b, "租户 B 应能检到自己刚写入的数据"
    assert {hit.chunk_id.split("-")[-1] for hit in hits_a} == set(CORPUS)
    assert {hit.chunk_id.split("-")[-1] for hit in hits_b} == set(CORPUS)
    assert not ({h.chunk_id for h in hits_a} & {h.chunk_id for h in hits_b})


async def test_count_respects_tenant_scope(seeded: MilvusStore) -> None:
    """``count`` 按租户统计，不把其他租户的行算进来。

    断言绝对条数的前提是"这个租户名下只有本夹具写的行"——该前提由每次运行独占的
    租户名（见 ``_RUN_SALT``）保证，而不是由"历史上没人漏清理"保证。
    """
    assert await seeded.count(tenant_id=TENANT_A) == len(CORPUS)
    assert await seeded.count(tenant_id=TENANT_B) == len(CORPUS)


async def test_upsert_is_idempotent_by_primary_key(
    seeded: MilvusStore, integration_settings: Settings
) -> None:
    """同主键重复写入是覆盖而非新增——整个入库幂等契约就落在这一点上。

    脚本按内容 sha256 比对决定是否重入库；万一比对失效（改指纹算法、清单被删），
    最后一道防线就是这里的主键语义。它失效时表现为"检索结果里出现重复证据"，
    发生在新环境、很难当场归因，所以在真库上钉死。
    """
    before = await seeded.count(tenant_id=TENANT_A)
    target = (
        await seeded.hybrid_search(
            query_text="阶梯电价",
            query_vector=make_vector(0, integration_settings.milvus_dim),
            expr=tenant_filter(TENANT_A, DATASET),
            candidates=20,
            vector_weight=0.7,
        )
    )[0]

    rewritten = "改写后的正文：居民生活用电继续实行阶梯电价。"
    await seeded.upsert_chunks(
        [
            make_record(
                target.chunk_id,
                rewritten,
                tenant_id=TENANT_A,
                document_id=target.document_id,
            )
        ],
        [make_vector(0, integration_settings.milvus_dim)],
    )
    await seeded.flush()  # 同 seeded 夹具：覆盖结果要 flush 后才可见

    assert await seeded.count(tenant_id=TENANT_A) == before, "同主键写入不得增加行数"
    after = next(
        hit
        for hit in await seeded.hybrid_search(
            query_text="阶梯电价",
            query_vector=make_vector(0, integration_settings.milvus_dim),
            expr=tenant_filter(TENANT_A, DATASET),
            candidates=20,
            vector_weight=0.7,
        )
        if hit.chunk_id == target.chunk_id
    )
    assert after.content == rewritten, "同主键写入必须整体覆盖为新内容"


async def test_delete_by_document_removes_only_that_document(
    milvus_store: MilvusStore, integration_settings: Settings
) -> None:
    """删除按 document_id 精确命中，不误伤同租户的其他文档。"""
    dim = integration_settings.milvus_dim
    keep_doc, drop_doc = f"keep-{uuid.uuid4().hex[:8]}", f"drop-{uuid.uuid4().hex[:8]}"
    records = [
        make_record(f"{doc}-c{i}", text, tenant_id=TENANT_A, document_id=doc)
        for doc, text in ((keep_doc, CORPUS["c1"]), (drop_doc, CORPUS["c2"]))
        for i in range(1)
    ]
    await milvus_store.upsert_chunks(records, [make_vector(i, dim) for i in range(len(records))])
    try:
        assert await milvus_store.delete_by_document(drop_doc, TENANT_A) == 1
        assert await milvus_store.count(tenant_id=TENANT_A) >= 1
        remaining = await milvus_store.hybrid_search(
            query_text="阶梯电价",
            query_vector=make_vector(0, dim),
            expr=tenant_filter(TENANT_A, DATASET),
            candidates=20,
            vector_weight=0.7,
        )
        assert keep_doc in {hit.document_id for hit in remaining}
        assert drop_doc not in {hit.document_id for hit in remaining}
    finally:
        await milvus_store.delete_by_document(keep_doc, TENANT_A)
        await milvus_store.delete_by_document(drop_doc, TENANT_A)


async def test_dim_mismatch_fails_fast(milvus_store: MilvusStore) -> None:
    """向量维度不符时拒写，绝不让错位数据进索引（NO-06 不许"假装成功"）。"""
    record = make_record("bad-dim", CORPUS["c1"], tenant_id=TENANT_A, document_id="doc-bad-dim")
    with pytest.raises(KnowledgeError, match="维度"):
        await milvus_store.upsert_chunks([record], [[0.1, 0.2, 0.3]])
