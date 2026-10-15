"""RAGFlow 客户端：响应包解包、错误码映射、解析轮询与分页。

契约依据 RAGFlow v0.27.2 源码（见 ``ragflow_client`` 模块文档与 ADR-0002），
测试用 ``httpx.MockTransport`` 复刻真实响应形状，不发真实请求。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from agent_platform.core.errors import (
    KnowledgeError,
    KnowledgeParseTimeoutError,
    NotFoundError,
    UnauthorizedError,
    ValidationFailedError,
)
from agent_platform.knowledge.ragflow_client import FILE_NAME_MAX_BYTES, RagFlowClient
from agent_platform.knowledge.ragflow_schemas import ParseStatus
from agent_platform.settings import Settings

_BASE = "http://ragflow.test"


def _client(handler: Any) -> RagFlowClient:
    """用 MockTransport 构造 RAGFlow 客户端。"""
    settings = Settings.model_construct(
        ragflow_base_url=_BASE,
        ragflow_api_key=SecretStr("test-key"),
        ragflow_parse_timeout_s=3600,
        model_timeout_s=5.0,
    )
    return RagFlowClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _ok(data: Any) -> httpx.Response:
    """构造 RAGFlow 成功响应包。"""
    return httpx.Response(200, json={"code": 0, "data": data, "message": "success"})


def _err(code: int, message: str = "boom", status: int = 200) -> httpx.Response:
    """构造 RAGFlow 业务错误响应包。"""
    return httpx.Response(status, json={"code": code, "message": message})


def _doc(document_id: str, run: str = "DONE", **overrides: Any) -> dict[str, Any]:
    """构造一个文档对象（字段名对齐 v0.27.2 的 map_doc_keys 输出）。"""
    base: dict[str, Any] = {
        "id": document_id,
        "name": f"{document_id}.pdf",
        "location": f"{document_id}.pdf",
        "size": 1024,
        "run": run,
        "progress": 1.0 if run == "DONE" else 0.5,
        "progress_msg": "",
        "chunk_count": 3,
    }
    base.update(overrides)
    return base


def _chunk(chunk_id: str, page: int = 1) -> dict[str, Any]:
    """构造一个切分对象（positions 为 [页号, left, right, top, bottom]）。"""
    return {
        "id": chunk_id,
        "content": f"{chunk_id} 的正文",
        "document_id": "d1",
        "docnm_kwd": "政策.pdf",
        "positions": [[page, 0, 10, 0, 10]],
        "available": True,
    }


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------
async def test_ensure_dataset_reuses_existing_by_name() -> None:
    """同名数据集已存在时直接复用，不重复创建。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return _ok([{"id": "ds1", "name": "gov_policy"}])

    assert await _client(handler).ensure_dataset("gov_policy") == "ds1"
    assert seen == ["GET /api/v1/datasets"]  # 未触发 POST


async def test_ensure_dataset_creates_when_absent() -> None:
    """不存在时创建并返回新数据集 ID。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["name"] == "gov_policy"
            return _ok({"id": "ds_new", "name": "gov_policy"})
        return _ok([])

    assert await _client(handler).ensure_dataset("gov_policy") == "ds_new"


async def test_ensure_dataset_rejects_response_without_id() -> None:
    """创建响应缺少 id 时视为契约破坏。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"name": "gov_policy"}) if request.method == "POST" else _ok([])

    with pytest.raises(KnowledgeError, match="缺少 id"):
        await _client(handler).ensure_dataset("gov_policy")


# ---------------------------------------------------------------------------
# 上传与解析
# ---------------------------------------------------------------------------
async def test_upload_document_sends_repeated_file_field(tmp_path: Path) -> None:
    """上传使用 multipart 且文件字段名为 ``file``（v0.27.2 源码契约）。"""
    source = tmp_path / "政策.pdf"
    source.write_bytes(b"%PDF-1.4 fake")

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type", "")
        captured["auth"] = request.headers.get("authorization", "")
        captured["body"] = request.content
        return _ok([_doc("d1", run="UNSTART")])

    doc = await _client(handler).upload_document("ds1", source)

    assert doc.id == "d1"
    assert doc.run is ParseStatus.UNSTART
    assert "multipart/form-data" in captured["content_type"]
    assert captured["auth"] == "Bearer test-key"
    assert b'name="file"' in captured["body"]
    assert "政策.pdf".encode() in captured["body"]


async def test_upload_document_rejects_missing_file(tmp_path: Path) -> None:
    """源文件不存在时前置失败，不发请求。"""
    with pytest.raises(ValidationFailedError, match="不存在"):
        await _client(lambda request: _ok([])).upload_document("ds1", tmp_path / "nope.pdf")


async def test_upload_document_rejects_long_filename_by_bytes(tmp_path: Path) -> None:
    """文件名按 UTF-8 字节数校验（中文名 1 字 3 字节）。"""
    too_long = "文" * (FILE_NAME_MAX_BYTES // 3)  # 255 字节上限下必然超限
    source = tmp_path / f"{too_long}.pdf"
    source.write_bytes(b"x")

    with pytest.raises(ValidationFailedError, match="文件名超过"):
        await _client(lambda request: _ok([])).upload_document("ds1", source)


async def test_start_parsing_posts_document_ids() -> None:
    """解析接口按 body 传 document_ids，成功仅回 {"code": 0}。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/datasets/ds1/chunks"
        assert json.loads(request.content) == {"document_ids": ["d1", "d2"]}
        return httpx.Response(200, json={"code": 0})

    await _client(handler).start_parsing("ds1", ["d1", "d2"])


async def test_start_parsing_requires_document_ids() -> None:
    """空 document_ids 前置失败。"""
    with pytest.raises(ValidationFailedError):
        await _client(lambda request: _ok(None)).start_parsing("ds1", [])


async def test_wait_parse_done_returns_terminal_statuses() -> None:
    """轮询到终态即返回，含失败状态（由调用方决定如何处理）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        run = "RUNNING" if calls["n"] == 1 else "FAIL"
        return _ok({"docs": [_doc("d1", run=run)], "total": 1})

    result = await _client(handler).wait_parse_done("ds1", ["d1"], poll_interval_s=0.01)

    assert result["d1"].run is ParseStatus.FAIL
    assert result["d1"].run.is_terminal is True
    assert result["d1"].run.is_success is False


async def test_wait_parse_done_times_out() -> None:
    """超时仍有未完成文档时抛解析超时（可重试异常）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"docs": [_doc("d1", run="RUNNING", progress=0.3)], "total": 1})

    with pytest.raises(KnowledgeParseTimeoutError, match="超时"):
        await _client(handler).wait_parse_done("ds1", ["d1"], timeout_s=0)


# ---------------------------------------------------------------------------
# 切分读取
# ---------------------------------------------------------------------------
async def test_list_chunks_parses_positions() -> None:
    """切分解析出正文、文档名与位置信息。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"chunks": [_chunk("k1", page=4)], "total": 1})

    page = await _client(handler).list_chunks("ds1", "d1")
    assert page.total == 1
    assert page.chunks[0].id == "k1"
    assert page.chunks[0].docnm_kwd == "政策.pdf"
    assert page.chunks[0].positions == [[4, 0, 10, 0, 10]]


async def test_iter_chunks_pages_until_total() -> None:
    """迭代器按页拉取，累计到 total 即停止。"""
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page_no = int(request.url.params["page"])
        requested_pages.append(page_no)
        return _ok({"chunks": [_chunk(f"k{page_no}")], "total": 2})

    pages = [page async for _no, page in _client(handler).iter_chunks("ds1", "d1", page_size=1)]

    assert requested_pages == [1, 2]
    assert sum(len(page.chunks) for page in pages) == 2


async def test_iter_chunks_stops_on_empty_page() -> None:
    """空页立即停止，避免无限翻页。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"chunks": [], "total": 99})

    pages = [page async for _no, page in _client(handler).iter_chunks("ds1", "d1")]
    assert len(pages) == 1


async def test_delete_documents_reports_deleted_count() -> None:
    """删除返回实际删除数（v0.27.2 代码返回 data.deleted）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert json.loads(request.content) == {"ids": ["d1"]}
        return _ok({"deleted": 1})

    assert await _client(handler).delete_documents("ds1", ["d1"]) == 1


# ---------------------------------------------------------------------------
# 错误映射
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_err(109, "authentication error"), UnauthorizedError),
        (_err(108, "permission denied"), UnauthorizedError),
        (_err(102, "invalid data"), ValidationFailedError),
        (_err(101, "argument error"), ValidationFailedError),
        (_err(404, "not found"), NotFoundError),
        (_err(103, "operating error"), KnowledgeError),
        (httpx.Response(401, json={"code": 401}), UnauthorizedError),
        (httpx.Response(404, json={}), NotFoundError),
        (httpx.Response(500, json={"code": 100, "message": "exception"}), KnowledgeError),
    ],
)
async def test_error_mapping(response: httpx.Response, expected: type[Exception]) -> None:
    """HTTP 状态与业务码均映射到平台异常（4xx 不重试）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return response

    with pytest.raises(expected):
        await _client(handler).list_datasets()


async def test_running_business_code_is_reported() -> None:
    """业务码 106（操作进行中）给出可操作的提示。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _err(106, "another task is running")

    with pytest.raises(KnowledgeError, match="仍在进行"):
        await _client(handler).list_datasets()


async def test_non_json_response_is_rejected() -> None:
    """非 JSON 响应被视为契约破坏，而不是静默当成成功。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    with pytest.raises(KnowledgeError, match="非 JSON"):
        await _client(handler).list_datasets()


async def test_list_documents_parses_status_and_progress() -> None:
    """文档列表解析出状态、进度与总数（分页契约 data.total）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"docs": [_doc("d1", run="RUNNING", progress=0.42)], "total": 1})

    page = await _client(handler).list_documents("ds1")
    assert page.total == 1
    assert page.docs[0].run is ParseStatus.RUNNING
    assert page.docs[0].progress == pytest.approx(0.42)


async def test_cancelled_document_progress_is_accepted() -> None:
    """取消状态的进度为 -1（v0.27.2 set_progress 语义），模型需接受该取值。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"docs": [_doc("d1", run="CANCEL", progress=-1.0)], "total": 1})

    page = await _client(handler).list_documents("ds1")
    assert page.docs[0].run is ParseStatus.CANCEL
    assert page.docs[0].progress == -1.0
