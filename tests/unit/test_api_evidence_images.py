"""会话取图端点（ADR-0004 / CLAUDE.md 8.6）。

端点是**按会话取图**，不是按 ID 取图：URL 里的 chunk_id 与 image_id 只在
本次会话的证据里定位，命不中一律 404。本文件同时锁住两件事：取到的字节与
安全响应头，以及"任何取不到的理由都不得区分"（否则端点就成了存在性探针）。
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from agent_platform.core.errors import KnowledgeError
from agent_platform.core.ids import compose_thread_id, new_session_id
from agent_platform.knowledge.retriever import REASON_SUFFICIENT
from agent_platform.knowledge.schemas import (
    EvidenceItem,
    ImageRef,
    MediaType,
    RetrievalMeta,
    RetrievalOutcome,
    build_image_id,
)
from agent_platform.main import API_PREFIX
from tests.conftest import TEST_QUESTION, FakeRetriever

if TYPE_CHECKING:
    from tests.conftest import ApiHarness, OpenApi

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_DOCUMENT_ID = "doc-figure"
_CHUNK_ID = "chunk-figure"


def _figure() -> tuple[RetrievalOutcome, ImageRef]:
    """构造一条带图片的证据与对应的检索结果。"""
    sha = hashlib.sha256(_PNG).hexdigest()
    ref = ImageRef(
        image_id=build_image_id(sha),
        sha256=sha,
        mime="image/png",
        caption="电价曲线图",
        page=3,
    )
    item = EvidenceItem(
        chunk_id=_CHUNK_ID,
        content="第 3 页的图 1 展示了分时电价曲线。",
        score=0.9,
        dataset_id="ds-demo",
        document_id=_DOCUMENT_ID,
        document_name="电价政策.pdf",
        page_numbers=[3],
        source_uri="gov_doc/电价政策.pdf",
        media_type=MediaType.IMAGE,
        images=[ref],
    )
    outcome = RetrievalOutcome(
        evidence=[item],
        meta=RetrievalMeta(
            query=TEST_QUESTION,
            candidate_count=1,
            evidence_count=1,
            top_score=item.score,
            score_threshold=0.3,
            is_sufficient=True,
            reason=REASON_SUFFICIENT,
            rerank_applied=False,
        ),
    )
    return outcome, ref


def _url(thread_id: str, chunk_id: str, image_id: str) -> str:
    """拼取图端点 URL。"""
    return f"{API_PREFIX}/threads/{thread_id}/evidence/{chunk_id}/images/{image_id}"


async def _seed(api: ApiHarness, ref: ImageRef, *, data: bytes = _PNG) -> None:
    """把图片放进 fake 存储（键含服务端解析出的租户，NO-19）。"""
    api.stack.image_store.add(
        tenant_id=api.stack.settings.demo_tenant_id,
        document_id=_DOCUMENT_ID,
        chunk_id=_CHUNK_ID,
        image_id=ref.image_id,
        mime=ref.mime,
        data=data,
    )


async def _run(api: ApiHarness, thread_id: str) -> None:
    """跑一次图（证据会随 checkpoint 落库，取图端点从这里匹配）。"""
    response = await api.client.post(
        f"{API_PREFIX}/threads/{thread_id}/runs",
        headers=api.auth_headers,
        json={"agent": api.stack.agent_name, "message": TEST_QUESTION},
    )
    assert response.status_code == 200, response.text


async def test_serves_image_with_safety_headers(api_factory: OpenApi) -> None:
    """取图返回原字节，并带 no-store / nosniff / inline 三项响应头。"""
    outcome, ref = _figure()
    async with api_factory(retriever=FakeRetriever(outcome)) as api:
        thread_id = await api.create_thread()
        await _run(api, thread_id)
        await _seed(api, ref)

        response = await api.client.get(
            _url(thread_id, _CHUNK_ID, ref.image_id), headers=api.auth_headers
        )

    assert response.status_code == 200, response.text
    assert response.content == _PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == f'inline; filename="{ref.image_id}.png"'


async def test_run_citations_carry_images(api_factory: OpenApi) -> None:
    """引用条目原样带出图片元数据；客户端据此才找得到取图 URL（ADR-0004）。"""
    outcome, ref = _figure()
    async with api_factory(retriever=FakeRetriever(outcome)) as api:
        thread_id = await api.create_thread()
        response = await api.client.post(
            f"{API_PREFIX}/threads/{thread_id}/runs",
            headers=api.auth_headers,
            json={"agent": api.stack.agent_name, "message": TEST_QUESTION},
        )

    citation = response.json()["citations"][0]
    assert citation["chunk_id"] == _CHUNK_ID
    assert [image["image_id"] for image in citation["images"]] == [ref.image_id]


async def test_unauthenticated_is_401(api_factory: OpenApi) -> None:
    """未带凭据是 401（唯一需要与 404 区分的情形）。"""
    outcome, ref = _figure()
    async with api_factory(retriever=FakeRetriever(outcome)) as api:
        thread_id = await api.create_thread()
        await _run(api, thread_id)
        await _seed(api, ref)

        response = await api.client.get(_url(thread_id, _CHUNK_ID, ref.image_id))

    assert response.status_code == 401
    assert api.stack.image_store.get_calls == [], "未认证不得触碰存储"


async def test_thread_without_run_is_404(api_factory: OpenApi) -> None:
    """还没跑过的会话没有证据，自然取不到图。"""
    outcome, ref = _figure()
    async with api_factory(retriever=FakeRetriever(outcome)) as api:
        thread_id = await api.create_thread()
        await _seed(api, ref)

        response = await api.client.get(
            _url(thread_id, _CHUNK_ID, ref.image_id), headers=api.auth_headers
        )

    assert response.status_code == 404


async def test_foreign_tenant_thread_is_404(api_factory: OpenApi) -> None:
    """跨租户的会话 ID 与"不存在"同形，不提供存在性预言机（NO-19）。"""
    outcome, ref = _figure()
    async with api_factory(retriever=FakeRetriever(outcome)) as api:
        settings = api.stack.settings
        foreign = compose_thread_id("another", settings.demo_user_id, new_session_id())

        response = await api.client.get(
            _url(foreign, _CHUNK_ID, ref.image_id), headers=api.auth_headers
        )

    assert response.status_code == 404
    assert api.stack.image_store.get_calls == []


async def test_chunk_not_in_session_evidence_is_404(api_factory: OpenApi) -> None:
    """chunk_id 不在会话证据里时不查存储（URL 不是取图授权）。"""
    outcome, ref = _figure()
    async with api_factory(retriever=FakeRetriever(outcome)) as api:
        thread_id = await api.create_thread()
        await _run(api, thread_id)
        await _seed(api, ref)

        response = await api.client.get(
            _url(thread_id, "chunk-other", ref.image_id), headers=api.auth_headers
        )

    assert response.status_code == 404
    assert api.stack.image_store.get_calls == []


async def test_image_not_belonging_to_chunk_is_404(api_factory: OpenApi) -> None:
    """chunk 命中但 image_id 不是它的图片，同样是 404。"""
    outcome, ref = _figure()
    other = build_image_id(hashlib.sha256(b"other").hexdigest())
    async with api_factory(retriever=FakeRetriever(outcome)) as api:
        thread_id = await api.create_thread()
        await _run(api, thread_id)
        await _seed(api, ref)

        response = await api.client.get(_url(thread_id, _CHUNK_ID, other), headers=api.auth_headers)

    assert response.status_code == 404
    assert api.stack.image_store.get_calls == []


async def test_missing_file_is_404(api_factory: OpenApi) -> None:
    """证据里有这张图但存储里没有文件：404，不泄露原因。"""
    outcome, ref = _figure()
    async with api_factory(retriever=FakeRetriever(outcome)) as api:
        thread_id = await api.create_thread()
        await _run(api, thread_id)

        response = await api.client.get(
            _url(thread_id, _CHUNK_ID, ref.image_id), headers=api.auth_headers
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "图片文件已不存在，请重新入库。"


async def test_store_failure_is_404(api_factory: OpenApi) -> None:
    """存储故障对调用方同样是 404（服务端日志留痕，响应不暴露内部原因）。"""
    outcome, ref = _figure()
    async with api_factory(retriever=FakeRetriever(outcome)) as api:
        thread_id = await api.create_thread()
        await _run(api, thread_id)
        api.stack.image_store.raises = KnowledgeError("磁盘挂了")

        response = await api.client.get(
            _url(thread_id, _CHUNK_ID, ref.image_id), headers=api.auth_headers
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "图片暂时无法读取。"


@pytest.mark.parametrize("bad_image_id", ["not-an-image-id", "img_zzzz", "..", "img_%2e%2e"])
async def test_malformed_image_id_is_404_not_422(api_factory: OpenApi, bad_image_id: str) -> None:
    """非法 ID 走业务 404 而不是 FastAPI 的 422——422 会泄露"格式对不对"。"""
    outcome, ref = _figure()
    async with api_factory(retriever=FakeRetriever(outcome)) as api:
        thread_id = await api.create_thread()
        await _run(api, thread_id)
        await _seed(api, ref)

        response = await api.client.get(
            _url(thread_id, _CHUNK_ID, bad_image_id), headers=api.auth_headers
        )

    assert response.status_code == 404


async def test_store_is_queried_with_tenant_from_auth(api_factory: OpenApi) -> None:
    """取图按认证租户 + 证据里的 document/chunk 定位（不信任 URL 之外的任何来源）。"""
    outcome, ref = _figure()
    async with api_factory(retriever=FakeRetriever(outcome)) as api:
        thread_id = await api.create_thread()
        await _run(api, thread_id)
        await _seed(api, ref)

        await api.client.get(_url(thread_id, _CHUNK_ID, ref.image_id), headers=api.auth_headers)

    assert api.stack.image_store.get_calls == [
        (
            api.stack.settings.demo_tenant_id,
            _DOCUMENT_ID,
            _CHUNK_ID,
            ref.image_id,
            ref.mime,
        )
    ]
