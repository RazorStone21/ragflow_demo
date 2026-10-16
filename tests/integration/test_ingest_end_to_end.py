"""真实入库端到端：RAGFlow 解析 → 嵌入 → Milvus 索引，并验证幂等与旧产物清理。

这是实施计划 5.2 第 3 条「真实入库 → 重跑验证幂等」的自动化形态。之所以值得单独成
文件而不是靠手工跑一次脚本：入库链的幂等契约（ADR-0002）由**几个各自正确、合起来
才成立**的机制共同保证——清单指纹跳过、Milvus 主键沿用 chunk_id 的覆盖语义、内容
变更时先清旧文档再上传。任何一环悄悄失效，表现都是"多跑几次就有了重复数据"，
而这在人工抽查里几乎看不出来。

三个用例分别钉住这三件事：

1. 指纹未变 → 整文件跳过，向量条数一条不多；
2. 内容变更 → 换成新文档，且**旧 chunk 一条不剩**（否则是永不命中的孤儿行）；
3. 入库结果能被真实嵌入 + BM25 混合检索命中，且跨租户一条都看不到（NO-19）。

代价说明：一次用例要跑完整的解析 + 嵌入，占用真实的 RAGFlow 与云端嵌入额度，
因此用例数刻意压到最少、语料只用一份最小夹具（NO-17：不含任何客户数据）。
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_platform.knowledge.embedder import LiteLLMEmbedder
from agent_platform.knowledge.gateway_client import GatewayHttpClient
from agent_platform.knowledge.milvus_schema import tenant_filter
from agent_platform.knowledge.milvus_store import MilvusStore, open_milvus_store
from agent_platform.knowledge.ragflow_client import RagFlowClient
from scripts import ingest
from tests.integration._support import require_http, require_milvus

if TYPE_CHECKING:
    from agent_platform.settings import Settings

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample_energy_doc.txt"
SOURCE_NAME = "sample_energy_doc.txt"

TENANT = "integration-ingest-tenant"
"""专用租户：真实入库若写进 demo 租户，检索演示会被测试数据污染，且清不干净。"""

OTHER_TENANT = "integration-ingest-other"
"""仅用于验证"另一租户看不到本次入库的数据"。"""

DATASET = "integration_ingest_dataset"
"""专用数据集。``RagFlowClient`` 无 ``delete_dataset``，故数据集保留、只删其中的文档。"""

QUESTION = "居民生活用电的阶梯电价是怎么分档的？"

APPENDED = (
    "\n\n六、测试追加段落\n本节由集成测试追加，用于触发内容变更后的重新入库与旧产物清理验证。\n"
)


def ingest_args(source: Path, subset: Path, *, dry_run: bool = False) -> argparse.Namespace:
    """用**真实**解析器构造参数。

    手搓 ``Namespace`` 会在参数改名后静默失效（真实入库的取值悄悄退回默认值），
    走 ``build_parser()`` 才能让参数契约变化在测试里直接暴露。
    """
    argv = [
        "--source",
        str(source),
        "--subset",
        str(subset),
        "--tenant",
        TENANT,
        "--dataset",
        DATASET,
    ]
    if dry_run:
        argv.append("--dry-run")
    return ingest.build_parser().parse_args(argv)


def read_manifest(path: Path) -> dict[str, Any]:
    """读取清单中的文件条目（本文件只关心 ``files`` 段）。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    files = raw["files"]
    assert isinstance(files, dict)
    return files


@pytest.fixture(autouse=True)
async def dependencies(integration_settings: Settings) -> None:
    """入库链的三个外部依赖缺一不可，任一缺失都 skip 并给出启动提示。"""
    await require_http(integration_settings.ragflow_base_url, name="RAGFlow")
    await require_http(f"{integration_settings.litellm_base_url}/health/liveliness", name="LiteLLM")


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, Path]:
    """把夹具文档复制进临时目录并生成子集清单，返回 ``(语料目录, 子集文件)``。

    刻意**复制**而不是直接用 ``tests/fixtures/``：本文件有一个用例要改写语料内容，
    直接改仓库里的夹具会把一次测试运行变成一次源码修改。
    """
    source = tmp_path / "corpus"
    source.mkdir()
    shutil.copyfile(FIXTURE, source / SOURCE_NAME)
    subset = tmp_path / "subset.txt"
    subset.write_text(f"# 集成测试子集\n{SOURCE_NAME}\n", encoding="utf-8")
    return source, subset


@pytest.fixture
def ingest_settings(integration_settings: Settings, tmp_path: Path) -> Settings:
    """把清单改到临时目录：测试绝不能写到仓库的 ``local_data/``。"""
    return integration_settings.model_copy(
        update={"ingest_manifest_path": tmp_path / "ingest_manifest.json"}
    )


@pytest.fixture
async def milvus(integration_settings: Settings) -> AsyncIterator[MilvusStore]:
    """真实 Milvus 连接（断言与清理都靠它）。"""
    await require_milvus(integration_settings)
    async with open_milvus_store(integration_settings) as store:
        await store.ensure_collection()
        yield store


@pytest.fixture(autouse=True)
async def cleanup(
    ingest_settings: Settings, milvus: MilvusStore, tmp_path: Path
) -> AsyncIterator[None]:
    """用例结束后清掉本次写入的向量与 RAGFlow 文档。

    按**清单里记录的 document_id** 清理而不是"清空整个租户"：后者在测试互相干扰时
    会连别人的数据一起删，而清单是入库流程自己产出的、唯一权威的产物记录。

    刻意不做断言：清理失败不应顶掉上面真正的失败原因。
    """
    yield
    manifest_path = ingest_settings.ingest_manifest_path
    if not manifest_path.exists():
        return
    documents = [
        str(entry["document_id"])
        for entry in read_manifest(manifest_path).values()
        if isinstance(entry, dict) and entry.get("document_id")
    ]
    if not documents:
        return
    for document_id in documents:
        await milvus.delete_by_document(document_id, TENANT)
    async with RagFlowClient(ingest_settings) as ragflow:
        dataset_id = await ragflow.ensure_dataset(DATASET)
        await ragflow.delete_documents(dataset_id, documents)


async def test_rerun_with_unchanged_content_adds_nothing(
    corpus: tuple[Path, Path], ingest_settings: Settings, milvus: MilvusStore
) -> None:
    """指纹未变时重跑：应整文件跳过，向量条数一条不多（ADR-0002 幂等契约）。"""
    source, subset = corpus
    assert await ingest.run(ingest_args(source, subset), ingest_settings) == ingest.EXIT_OK

    recorded = read_manifest(ingest_settings.ingest_manifest_path)[SOURCE_NAME]
    assert recorded["chunk_count"] >= 1, f"入库未产生切分: {recorded}"
    assert recorded["tenant_id"] == TENANT
    count_after_first = await milvus.count(TENANT)
    assert count_after_first == recorded["chunk_count"], (
        f"索引里 {count_after_first} 条与清单记录的 {recorded['chunk_count']} 条不符"
    )

    assert await ingest.run(ingest_args(source, subset), ingest_settings) == ingest.EXIT_OK
    assert await milvus.count(TENANT) == count_after_first, "重跑产生了重复数据"


async def test_changed_content_replaces_document_without_orphans(
    corpus: tuple[Path, Path], ingest_settings: Settings, milvus: MilvusStore
) -> None:
    """内容变更后重新入库：换新文档，旧 chunk 一条不剩。

    这是本文件最重要的一条：Milvus 主键沿用 RAGFlow 的 chunk_id，内容一改 chunk_id
    全变，旧行不会被 upsert 覆盖——不主动清理就是永不命中的孤儿行，且会一直留在索引里
    被检索到（返回的内容对不上任何现存文档）。
    """
    source, subset = corpus
    assert await ingest.run(ingest_args(source, subset), ingest_settings) == ingest.EXIT_OK
    manifest_path = ingest_settings.ingest_manifest_path
    old_document_id = str(read_manifest(manifest_path)[SOURCE_NAME]["document_id"])

    target = source / SOURCE_NAME
    target.write_text(target.read_text(encoding="utf-8") + APPENDED, encoding="utf-8")
    assert await ingest.run(ingest_args(source, subset), ingest_settings) == ingest.EXIT_OK

    recorded = read_manifest(manifest_path)[SOURCE_NAME]
    new_document_id = str(recorded["document_id"])
    assert new_document_id != old_document_id, "内容已变，应重新上传得到新文档"
    assert await milvus.count(TENANT) == recorded["chunk_count"], (
        "索引条数与清单不符，新旧切分可能同时存在"
    )

    assert await milvus.delete_by_document(old_document_id, TENANT) == 0, (
        "旧文档的切分仍在索引中——内容变更后未清理产物（ADR-0002）"
    )

    async with RagFlowClient(ingest_settings) as ragflow:
        dataset_id = await ragflow.ensure_dataset(DATASET)
        page = await ragflow.list_documents(dataset_id)
    assert old_document_id not in {doc.id for doc in page.docs}, "旧文档未从 RAGFlow 删除"


async def test_ingested_chunks_are_retrievable_and_tenant_scoped(
    corpus: tuple[Path, Path], ingest_settings: Settings, milvus: MilvusStore
) -> None:
    """入库结果能被真实嵌入 + BM25 混合检索命中，且跨租户不可见（NO-19）。"""
    source, subset = corpus
    assert await ingest.run(ingest_args(source, subset), ingest_settings) == ingest.EXIT_OK
    recorded = read_manifest(ingest_settings.ingest_manifest_path)[SOURCE_NAME]

    async with GatewayHttpClient(ingest_settings) as gateway:
        embedder = LiteLLMEmbedder(
            gateway,
            logical_model=ingest_settings.embedding_model,
            expected_dim=ingest_settings.milvus_dim,
        )
        vector = await embedder.embed_query(QUESTION)

    hits = await milvus.hybrid_search(
        query_text=QUESTION,
        query_vector=vector,
        expr=tenant_filter(TENANT),
        candidates=ingest_settings.retrieval_candidates,
        vector_weight=ingest_settings.hybrid_vector_weight,
    )
    assert hits, "入库后按文档内容提问应能命中"
    assert any("阶梯电价" in hit.content for hit in hits), (
        f"未命中目标内容，实际: {[hit.content[:30] for hit in hits]}"
    )
    for hit in hits:
        assert hit.source_uri == SOURCE_NAME, "引用来源应为入库时的相对路径"
        assert hit.document_id == recorded["document_id"]
        assert 0.0 <= hit.score <= 1.0, f"融合分应在 0~1: {hit.score}"

    leaked = await milvus.hybrid_search(
        query_text=QUESTION,
        query_vector=vector,
        expr=tenant_filter(OTHER_TENANT),
        candidates=ingest_settings.retrieval_candidates,
        vector_weight=ingest_settings.hybrid_vector_weight,
    )
    assert leaked == [], f"另一租户检索到了本次入库的数据: {[hit.chunk_id for hit in leaked]}"
