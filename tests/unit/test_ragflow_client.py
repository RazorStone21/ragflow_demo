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
from agent_platform.core.retry import build_retry
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


async def test_list_datasets_filters_by_name_on_the_client() -> None:
    """名称过滤在客户端完成：**不把 ``name`` 发给服务端**。

    实测（v0.27.2）：服务端的 ``name`` 是**精确查找**而非过滤器，查不到时返回 4xx
    ``User '…' lacks permission for dataset '…'``（文案说的是权限，实为"不存在"）。
    若照字面当过滤器发出去，"数据集尚未创建"这一最常见情形会直接失败——首轮入库必然如此。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok([{"id": "ds1", "name": "gov_policy"}, {"id": "ds2", "name": "other"}])

    datasets = await _client(handler).list_datasets(name="gov_policy")

    assert [dataset.id for dataset in datasets] == ["ds1"]


async def test_list_datasets_name_filter_never_reaches_the_server() -> None:
    """把上一条的"不发给服务端"钉在请求串上——否则这次修复随时会被改回去。"""
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.query.decode())
        return _ok([{"id": "ds1", "name": "gov_policy"}])

    await _client(handler).list_datasets(name="gov_policy")

    assert queries == ["page=1&page_size=100"]


async def test_list_datasets_pages_until_name_is_found() -> None:
    """目标数据集不在首页时继续翻页，而不是当成不存在。"""
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        if page == 1:
            return _ok([{"id": f"ds{i}", "name": f"filler-{i}"} for i in range(100)])
        return _ok([{"id": "ds_target", "name": "gov_policy"}])

    datasets = await _client(handler).list_datasets(name="gov_policy")

    assert [dataset.id for dataset in datasets] == ["ds_target"]
    assert pages == [1, 2]


async def test_list_datasets_absent_name_is_empty_not_error() -> None:
    """翻到最后一页仍无同名数据集时返回空列表——"没有"不得表现为异常。

    这正是实测踩到的坑：服务端对"查不到"的响应是 4xx，客户端必须把它收敛成空列表，
    否则 ``ensure_dataset`` 永远走不到"创建"那一步。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok([{"id": "ds1", "name": "other"}])

    assert await _client(handler).list_datasets(name="gov_policy") == []


async def test_ensure_dataset_rejects_response_without_id() -> None:
    """创建响应缺少 id 时视为契约破坏。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"name": "gov_policy"}) if request.method == "POST" else _ok([])

    with pytest.raises(KnowledgeError, match="缺少 id"):
        await _client(handler).ensure_dataset("gov_policy")


async def test_ensure_dataset_passes_parser_config_on_create() -> None:
    """新建数据集时把解析配置带进 POST（上传接口不收这些键，见 ADR-0004）。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            captured.update(json.loads(request.content))
            return _ok({"id": "ds_new", "name": "gov_policy"})
        return _ok([])

    config = {"layout_recognize": "DeepDOC", "chunk_token_num": 512}
    dataset_id = await _client(handler).ensure_dataset("gov_policy", parser_config=config)

    assert dataset_id == "ds_new"
    assert captured["parser_config"] == config


async def test_ensure_dataset_updates_parser_config_when_exists() -> None:
    """已存在的数据集走 PUT 收敛解析配置，不重复创建。"""
    seen: list[str] = []
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.method == "PUT":
            captured.update(json.loads(request.content))
            return _ok(True)
        return _ok([{"id": "ds1", "name": "gov_policy"}])

    config = {"layout_recognize": "DeepDOC"}
    dataset_id = await _client(handler).ensure_dataset("gov_policy", parser_config=config)

    assert dataset_id == "ds1"
    assert seen == ["GET /api/v1/datasets", "PUT /api/v1/datasets/ds1"]
    assert captured == {"parser_config": config}


async def test_ensure_dataset_without_parser_config_sends_no_update() -> None:
    """未提供解析配置时不发 PUT：没有配置要收敛，就别去动数据集。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return _ok([{"id": "ds1", "name": "gov_policy"}])

    await _client(handler).ensure_dataset("gov_policy")

    assert seen == ["GET /api/v1/datasets"]


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


@pytest.mark.parametrize("key", ["layout_recognize", "image_context_size", "chunk_token_num"])
async def test_upload_document_rejects_unsupported_parser_config(tmp_path: Path, key: str) -> None:
    """上传接口只白名单 table_column_*，其余键显式拒绝而不是静默丢弃。

    v0.27.2 的 ``document_api.py`` 用 ``allowed_keys`` 过滤，传错位置不会有任何报错，
    解析配置看起来"设了"实际没生效——所以必须在这里失败。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("非法 parser_config 不应发出请求")

    source = tmp_path / "政策.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(ValidationFailedError, match="不支持 parser_config 键"):
        await _client(handler).upload_document("ds1", source, parser_config={key: "x"})


async def test_upload_document_sends_whitelisted_parser_config(tmp_path: Path) -> None:
    """白名单键照常发给上传接口。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return _ok([_doc("d1", run="UNSTART")])

    source = tmp_path / "表格.csv"
    source.write_bytes(b"a,b\n1,2\n")
    await _client(handler).upload_document(
        "ds1", source, parser_config={"table_column_mode": "manual"}
    )

    assert b"table_column_mode" in captured["body"]


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
# 切分图片下载（ADR-0004）
# ---------------------------------------------------------------------------
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
"""最小 PNG 魔数样本：只需通过魔数识别，不是可解码图片。"""


async def test_download_document_image_returns_raw_bytes() -> None:
    """成功时返回原始二进制；Content-Type 不作为判据（靠魔数）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/documents/images/ds1-k1"
        return httpx.Response(
            200, content=_PNG, headers={"content-type": "application/octet-stream"}
        )

    payload = await _client(handler).download_document_image("ds1-k1", max_bytes=1024)
    assert payload == _PNG


async def test_download_document_image_maps_json_error_body() -> None:
    """ "图片不存在"是 HTTP 200 + JSON 业务包，必须按正文识别而不是当成图片。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 102, "message": "Image not found."})

    with pytest.raises(ValidationFailedError, match="参数非法"):
        await _client(handler).download_document_image("ds1-k1", max_bytes=1024)


async def test_download_document_image_rejects_non_image() -> None:
    """200 且正文不是图片、也不是业务包时显式失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not an image</html>")

    with pytest.raises(ValidationFailedError, match="不是受支持的图片"):
        await _client(handler).download_document_image("ds1-k1", max_bytes=1024)


async def test_download_document_image_rejects_empty_body() -> None:
    """空正文是"图片损坏"而不是"空图片证据"。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    with pytest.raises(ValidationFailedError, match="空图片"):
        await _client(handler).download_document_image("ds1-k1", max_bytes=1024)


async def test_download_document_image_stops_at_size_cap() -> None:
    """超过字节上限即中止，不把整张图收进来再判断。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_PNG + b"\x00" * 2048)

    with pytest.raises(ValidationFailedError, match="超过上限"):
        await _client(handler).download_document_image("ds1-k1", max_bytes=1024)


@pytest.mark.parametrize("image_id", ["", "nohyphen", "-k1", "ds1-", "../ds1-k1", "ds1-k/1"])
async def test_download_document_image_rejects_bad_id(image_id: str) -> None:
    """非法图片 ID 在发请求前失败（ID 来自外部响应，默认不可信）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("非法图片 ID 不应发出请求")

    with pytest.raises(ValidationFailedError, match="非法图片 ID"):
        await _client(handler).download_document_image(image_id, max_bytes=1024)


async def test_download_document_image_404_is_not_retried() -> None:
    """4xx 不重试：图片不存在重试也只是再查一遍。"""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404, json={"code": 404})

    with pytest.raises(NotFoundError):
        await _client(handler).download_document_image("ds1-k1", max_bytes=1024)
    assert attempts["n"] == 1


async def test_download_document_image_retries_transient_status() -> None:
    """5xx 在有重试预算时重试并最终成功（与 ``_request`` 同一套策略）。"""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, json={"code": 100, "message": "busy"})
        return httpx.Response(200, content=_PNG)

    client = _client(handler)
    # 测试里把退避压到毫秒级：重试策略本身由 test_retry 覆盖，这里只验证接线。
    client._retry = build_retry(max_attempts=3, initial_wait_s=0.001, max_wait_s=0.01)

    assert await client.download_document_image("ds1-k1", max_bytes=1024) == _PNG
    assert attempts["n"] == 2


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
