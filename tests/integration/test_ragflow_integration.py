"""真实 RAGFlow：数据集、上传、解析、切分读取（CLAUDE.md 第 7 章 / ADR-0002）。

RAGFlow 在双后端分工里只做**文档工程**（解析/切分/文档管理），检索不在这里。
因此本文件只验到"切分拿得出来"为止——切分之后的嵌入与索引归
``test_ingest_end_to_end.py``。

这里验的是接口契约而非算法质量：RAGFlow 的响应信封（``{"code":0,"data":...}``）、
分页形状、``run`` 状态名、``chunk_id`` 的稳定性。这些字段名一旦与上游版本不符，
整条入库链会在很靠后的位置以奇怪的方式失败，所以在真容器上先钉一遍。

**遗留清理说明**：``RagFlowClient`` 刻意没有 ``delete_dataset``（入库链从不删数据集），
所以本文件创建的 ``integration_test_dataset`` 会留在 RAGFlow 里，只删其中的文档。
数据集名带 ``integration_test`` 前缀以便识别；需要彻底清掉时在 RAGFlow 控制台删除。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_platform.knowledge.ragflow_client import RagFlowClient
from agent_platform.knowledge.ragflow_schemas import ParseStatus
from tests.integration._support import require_http

if TYPE_CHECKING:
    from agent_platform.settings import Settings

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample_energy_doc.txt"
DATASET_NAME = "integration_test_dataset"
"""专用数据集：刻意不复用演示库，免得测试数据混进检索结果里。"""


@pytest.fixture
async def ragflow(integration_settings: Settings) -> AsyncIterator[RagFlowClient]:
    """连上真实 RAGFlow；进程没起则 skip。

    401 不算 skip：能连上却认证失败说明 ``RAGFLOW_API_KEY`` 配错了，那是要修的，
    跳过只会让它拖到第一次真实入库时才暴露。
    """
    await require_http(integration_settings.ragflow_base_url, name="RAGFlow")
    async with RagFlowClient(integration_settings) as client:
        yield client


@pytest.fixture
async def dataset_id(ragflow: RagFlowClient) -> str:
    """确保测试数据集存在，返回其 ID（``ensure_dataset`` 幂等）。"""
    return await ragflow.ensure_dataset(DATASET_NAME)


async def test_ensure_dataset_is_idempotent(ragflow: RagFlowClient) -> None:
    """重复调用返回同一 ID：入库脚本每次运行都会调它，不能换来换去。"""
    first = await ragflow.ensure_dataset(DATASET_NAME)
    second = await ragflow.ensure_dataset(DATASET_NAME)
    assert first == second
    assert first, "数据集 ID 不得为空"


async def test_created_dataset_is_listed(ragflow: RagFlowClient, dataset_id: str) -> None:
    """列表接口的响应形状必须解析得出来（上游改过这些字段名）。"""
    datasets = await ragflow.list_datasets()
    assert dataset_id in {dataset.id for dataset in datasets}
    assert all(dataset.name for dataset in datasets), "数据集名不得为空"


async def test_name_filter_narrows_to_the_target(ragflow: RagFlowClient, dataset_id: str) -> None:
    """``ensure_dataset`` 靠名称过滤找库，过滤不生效会退化成"每跑一次建一个新的"。"""
    matched = await ragflow.list_datasets(name=DATASET_NAME)
    assert {dataset.id for dataset in matched} == {dataset_id}


async def test_upload_parse_and_read_chunks(ragflow: RagFlowClient, dataset_id: str) -> None:
    """上传 → 解析 → 等完成 → 读出切分，并校验切分元数据可用于引用。"""
    document = await ragflow.upload_document(dataset_id, FIXTURE)
    try:
        assert document.name == FIXTURE.name
        assert document.run is not ParseStatus.DONE, "刚上传的文档不该是已完成态"

        await ragflow.start_parsing(dataset_id, [document.id])
        statuses = await ragflow.wait_parse_done(dataset_id, [document.id])
        final = statuses[document.id]
        assert final.run.is_success, f"解析未成功: {final.run} / {final.progress_msg}"
        assert final.chunk_count >= 1, "解析成功却没切分，入库链在第二步就断了"

        pages = [page async for _index, page in ragflow.iter_chunks(dataset_id, document.id)]
        chunks = [chunk for page in pages for chunk in page.chunks]
        assert chunks, "解析完成后必须能读出切分"

        joined = "".join(chunk.content for chunk in chunks)
        assert "阶梯电价" in joined, "切分正文应包含原文内容"
        for chunk in chunks:
            assert chunk.id, "切分主键为空——Milvus 的主键就取它，不能丢"
            assert chunk.document_id == document.id
    finally:
        # 清理放在 finally 且**不做断言**：断言失败会顶掉上面真正的失败原因。
        # 删除语义由 test_deleted_document_disappears_from_listing 单独验证。
        await ragflow.delete_documents(dataset_id, [document.id])


async def test_chunk_pagination_reports_stable_total(
    ragflow: RagFlowClient, dataset_id: str
) -> None:
    """``iter_chunks`` 依赖 ``total`` 判断翻页终点；total 对不上会多拉一页空数据。"""
    document = await ragflow.upload_document(dataset_id, FIXTURE)
    try:
        await ragflow.start_parsing(dataset_id, [document.id])
        await ragflow.wait_parse_done(dataset_id, [document.id])

        seen = [
            chunk
            async for _index, page in ragflow.iter_chunks(dataset_id, document.id, page_size=1)
            for chunk in page.chunks
        ]
        assert len(seen) >= 1, "page_size=1 至少要能读出一页"
        assert len({chunk.id for chunk in seen}) == len(seen), "翻页重复返回了同一批切分"
    finally:
        await ragflow.delete_documents(dataset_id, [document.id])


async def test_deleted_document_disappears_from_listing(
    ragflow: RagFlowClient, dataset_id: str
) -> None:
    """删除后不得再出现在文档列表里（撤稿/重建索引都依赖这一语义）。"""
    document = await ragflow.upload_document(dataset_id, FIXTURE)
    deleted = await ragflow.delete_documents(dataset_id, [document.id])
    assert deleted >= 1, "删除接口应回报至少删除 1 个文档"

    page = await ragflow.list_documents(dataset_id)
    assert document.id not in {item.id for item in page.docs}
