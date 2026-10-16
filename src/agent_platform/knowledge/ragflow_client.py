"""RAGFlow 客户端：**只做文档工程**（ADR-0002 双后端分工）。

职责边界（CLAUDE.md 第 7 章）：上传文档 → 触发解析 → 轮询状态 → 拉取切分。
检索**不在此处**：向量与检索统一在 Milvus 主链路（``milvus_store`` / ``retriever``），
RAGFlow 的可视化画布与内置检索不得承载核心流程（NO-01）。

契约依据：RAGFlow v0.27.2 源码（``api/apps/sdk/*``、``common/constants.py``、
``rag/nlp/__init__.py``），已逐项核实并记入 ADR-0002：

- 鉴权：``Authorization: Bearer <API_KEY>``（控制台「API」页生成）。
- 统一响应包：``{"code": 0, "data": ..., "message": ...}``；``code != 0`` 即业务失败。
  错误码：100 异常、101 参数非法、102 数据非法/缺失、106 仍在运行、
  108 权限、109 鉴权失败、404 不存在、409 冲突、500 服务错误。
- 上传：``POST /datasets/{id}/documents`` multipart，文件字段名 **``file``**（可重复）。
- 解析：``POST /datasets/{id}/chunks`` body ``{"document_ids": [...]}``，成功仅回 ``{"code": 0}``。
- 文档列表：``GET /datasets/{id}/documents`` → ``data.docs[]`` + ``data.total``；
  状态字段 ``run``，进度 ``progress``（0~1，取消为 -1）。
- 切分列表：``GET /datasets/{id}/documents/{doc_id}/chunks`` → ``data.chunks[]`` + ``data.total``。

相关限制（v0.27.2 FAQ）：单次上传总计 ≤1GB；本地文件名 ≤255 **字节**（中文名需按字节算）；
HTTP API 不受 32 文件批次限制。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Self

import httpx
from pydantic import BaseModel, ValidationError

from agent_platform.core.errors import (
    AgentPlatformError,
    KnowledgeError,
    KnowledgeParseTimeoutError,
    NotFoundError,
    UnauthorizedError,
    ValidationFailedError,
)
from agent_platform.core.logging import get_logger
from agent_platform.core.retry import build_retry, is_retryable
from agent_platform.knowledge.ragflow_schemas import (
    ChunkPage,
    DatasetInfo,
    DocumentInfo,
    DocumentPage,
    ParseStatus,
)
from agent_platform.settings import Settings

logger = get_logger(__name__)

FILE_NAME_MAX_BYTES = 255
"""RAGFlow 本地文件名上限（字节，非字符）。"""

DEFAULT_PAGE_SIZE = 100
"""分页大小：列表接口默认 30，调大以减少往返（切分多时明显）。"""

# 业务错误码 → 处理方式（v0.27.2 common/constants.py RetCode）
_CODE_AUTH_FAILED = 109  # AUTHENTICATION_ERROR
_CODE_PERMISSION = 108  # PERMISSION_ERROR
_CODE_ARGUMENT = 101  # ARGUMENT_ERROR
_CODE_DATA = 102  # DATA_ERROR
_CODE_RUNNING = 106  # RUNNING
_CODE_EXCEPTION = 100  # EXCEPTION_ERROR
_CODE_CONNECTION = 105  # CONNECTION_ERROR
_CODE_NOT_FOUND = 404

# 注意：RAGFlow 把 code 100（异常）与 105（连接错误）映射为 HTTP 500，
# 因此这两类瞬时故障由 HTTP 层的重试覆盖，无需在业务码层再判一次。
# 副作用：106（操作仍在进行）同样以 HTTP 500 返回，会被重试——语义上正确
# （等待上一次操作结束后重试），故不单独规避。


class RagFlowClient:
    """RAGFlow HTTP 客户端（Bearer 鉴权，带超时与有界重试）。

    生命周期：由调用方持有并 ``aclose()``，或用 async 上下文管理器；
    **不得**在模块导入期创建（CLAUDE.md 4.8）。
    """

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        """构造客户端。

        Args:
            settings: 平台配置（提供 RAGFlow 地址、API Key、解析超时）。
            client: 可注入的 ``httpx.AsyncClient``（测试用 MockTransport）。
        """
        self._base_url = settings.ragflow_base_url.rstrip("/")
        self._parse_timeout_s = settings.ragflow_parse_timeout_s
        # 鉴权头逐请求携带，而非只挂在自建客户端的默认头里：注入共享连接池是常见
        # 生产用法，若把凭据放在客户端默认头，注入时会静默丢失（已由单测锁定）。
        self._headers = {"Authorization": f"Bearer {settings.ragflow_api_key.get_secret_value()}"}
        if client is None:
            self._owns_client = True
            self._client = httpx.AsyncClient(
                headers=self._headers,
                timeout=httpx.Timeout(settings.model_timeout_s, read=300.0),
                # 与 GatewayHttpClient 同理：RAGFlow 是内网服务，不得继承宿主系统代理。
                # 实测不关时会被本机代理返回 502（见 gateway_client.py 的同类注释）。
                trust_env=False,
            )
        else:
            self._owns_client = False
            self._client = client
        # 上传大文件耗时较久，重试次数保守；仅幂等/可重入的调用才走重试
        self._retry = build_retry(max_attempts=3, retryable=is_retryable)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """关闭自建连接池（注入的客户端由调用方关闭）。"""
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # 数据集
    # ------------------------------------------------------------------
    async def ensure_dataset(self, name: str) -> str:
        """按名称查找数据集，不存在则创建（幂等）。

        Args:
            name: 数据集名称。

        Returns:
            数据集 ID。
        """
        existing = await self.list_datasets(name=name)
        for dataset in existing:
            if dataset.name == name:
                return dataset.id
        body = await self._request("POST", "/api/v1/datasets", json={"name": name})
        data = body.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise KnowledgeError(
                f"创建数据集 {name!r} 的响应缺少 id",
                detail="RAGFlow 返回了非预期结果，请检查服务版本。",
            )
        logger.info("ragflow.dataset_created", extra={"dataset": name, "dataset_id": data["id"]})
        return str(data["id"])

    async def list_datasets(
        self, *, name: str | None = None, page_size: int = DEFAULT_PAGE_SIZE
    ) -> list[DatasetInfo]:
        """列出数据集（可按名称过滤）。"""
        params: dict[str, Any] = {"page": 1, "page_size": page_size}
        if name:
            params["name"] = name
        body = await self._request("GET", "/api/v1/datasets", params=params)
        return _parse_list(body, DatasetInfo, "数据集")

    # ------------------------------------------------------------------
    # 文档
    # ------------------------------------------------------------------
    async def upload_document(
        self, dataset_id: str, path: Path, *, parser_config: dict[str, Any] | None = None
    ) -> DocumentInfo:
        """上传单个文档。

        Args:
            dataset_id: 数据集 ID。
            path: 本地文件路径（支持中文名，按 UTF-8 字节数校验长度）。
            parser_config: 可选的解析配置覆盖。

        Returns:
            上传后的文档信息（``run`` 初始为 ``UNSTART``）。

        Raises:
            ValidationFailedError: 文件不存在或文件名超过 255 字节。
            KnowledgeError: 上传失败。
        """
        if not path.is_file():
            raise ValidationFailedError(
                f"待上传文件不存在: {path}", detail="请检查入库源路径与文件名。"
            )
        name_bytes = len(path.name.encode("utf-8"))
        if name_bytes > FILE_NAME_MAX_BYTES:
            raise ValidationFailedError(
                f"文件名超过 {FILE_NAME_MAX_BYTES} 字节（{name_bytes}）: {path.name}",
                detail="请重命名文件后再入库。",
            )
        content = await asyncio.to_thread(path.read_bytes)
        files: dict[str, Any] = {"file": (path.name, content)}
        data: dict[str, str] = {}
        if parser_config:
            data["parser_config"] = json.dumps(parser_config, ensure_ascii=False)
        body = await self._request(
            "POST",
            f"/api/v1/datasets/{dataset_id}/documents",
            files=files,
            data=data or None,
            # 上传是新增语义，网络重试可能产生重复文档；RAGFlow 按内容去重不保证，
            # 因此入库脚本以"文件名 + 内容指纹"在本地 manifest 层做幂等前置判断。
            retryable=False,
        )
        items = body.get("data")
        if not isinstance(items, list) or not items:
            raise KnowledgeError(
                f"上传 {path.name!r} 未返回文档信息",
                detail="RAGFlow 未接受该文件，请检查文件格式与大小限制。",
            )
        try:
            return DocumentInfo.model_validate(items[0])
        except ValidationError as exc:
            raise KnowledgeError(f"上传响应不符合契约: {path.name!r}") from exc

    async def list_documents(
        self,
        dataset_id: str,
        *,
        document_ids: list[str] | None = None,
        name: str | None = None,
        run: list[ParseStatus] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> DocumentPage:
        """列出文档（含解析状态与进度）。"""
        params: dict[str, Any] = {"page": 1, "page_size": page_size}
        if document_ids:
            params["ids"] = ",".join(document_ids)
        if name:
            params["name"] = name
        if run:
            params["run"] = ",".join(status.value for status in run)
        body = await self._request("GET", f"/api/v1/datasets/{dataset_id}/documents", params=params)
        return _parse_object(body, DocumentPage, "文档列表")

    async def start_parsing(self, dataset_id: str, document_ids: list[str]) -> None:
        """触发解析（切分）。

        Args:
            dataset_id: 数据集 ID。
            document_ids: 待解析文档 ID。

        Raises:
            ValidationFailedError: ``document_ids`` 为空。
        """
        if not document_ids:
            raise ValidationFailedError("document_ids 为空，无文档可解析")
        await self._request(
            "POST", f"/api/v1/datasets/{dataset_id}/chunks", json={"document_ids": document_ids}
        )

    async def stop_parsing(self, dataset_id: str, document_ids: list[str]) -> None:
        """中止解析（撤销卡住的解析任务）。"""
        await self._request(
            "DELETE", f"/api/v1/datasets/{dataset_id}/chunks", json={"document_ids": document_ids}
        )

    async def delete_documents(self, dataset_id: str, document_ids: list[str]) -> int:
        """删除文档（同时删除其切分）。

        Returns:
            实际删除的文档数。
        """
        if not document_ids:
            return 0
        body = await self._request(
            "DELETE", f"/api/v1/datasets/{dataset_id}/documents", json={"ids": document_ids}
        )
        data = body.get("data")
        if isinstance(data, dict) and isinstance(data.get("deleted"), int):
            return int(data["deleted"])
        return len(document_ids)

    async def wait_parse_done(
        self,
        dataset_id: str,
        document_ids: list[str],
        *,
        timeout_s: int | None = None,
        poll_interval_s: float = 5.0,
    ) -> dict[str, DocumentInfo]:
        """轮询直到全部文档离开"进行中"状态或超时。

        Args:
            dataset_id: 数据集 ID。
            document_ids: 关注的文档 ID。
            timeout_s: 超时秒数；默认取配置 ``RAGFLOW_PARSE_TIMEOUT_S``
                （扫描件 OCR 很慢，默认 3600）。
            poll_interval_s: 轮询间隔。

        Returns:
            文档 ID → 最终文档信息（含 ``DONE``/``FAIL``/``CANCEL`` 状态）。

        Raises:
            KnowledgeParseTimeoutError: 超时仍有文档处于 ``UNSTART``/``RUNNING``/``SCHEDULE``。
        """
        budget_s = float(timeout_s if timeout_s is not None else self._parse_timeout_s)
        pending = set(document_ids)
        deadline = asyncio.get_running_loop().time() + budget_s
        latest: dict[str, DocumentInfo] = {}
        while pending:
            page = await self.list_documents(
                dataset_id, document_ids=sorted(pending), page_size=max(len(pending), 1)
            )
            for doc in page.docs:
                latest[doc.id] = doc
                if doc.run.is_terminal:
                    pending.discard(doc.id)
            if not pending:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise KnowledgeParseTimeoutError(
                    f"解析超时（{budget_s:.0f}s），仍有 {len(pending)} 个文档未完成",
                    detail="文档可能为扫描件或体量过大，请稍后在 RAGFlow 查看解析进度。",
                )
            await asyncio.sleep(poll_interval_s)
        return latest

    # ------------------------------------------------------------------
    # 切分
    # ------------------------------------------------------------------
    async def list_chunks(
        self,
        dataset_id: str,
        document_id: str,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> ChunkPage:
        """按页读取某文档的切分。"""
        body = await self._request(
            "GET",
            f"/api/v1/datasets/{dataset_id}/documents/{document_id}/chunks",
            params={"page": page, "page_size": page_size},
        )
        return _parse_object(body, ChunkPage, "切分列表")

    async def iter_chunks(
        self, dataset_id: str, document_id: str, *, page_size: int = DEFAULT_PAGE_SIZE
    ) -> AsyncIterator[tuple[int, ChunkPage]]:
        """按页迭代切分（避免一次性把全文档切分载入内存）。

        Yields:
            ``(页码, 该页切分)``；直到累计条数达到 ``total`` 为止。
        """
        page_no = 1
        seen = 0
        while True:
            page = await self.list_chunks(
                dataset_id, document_id, page=page_no, page_size=page_size
            )
            yield page_no, page
            seen += len(page.chunks)
            if not page.chunks or seen >= page.total:
                break
            page_no += 1

    # ------------------------------------------------------------------
    # 传输层
    # ------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        retryable: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """发起一次 RAGFlow 调用并解开统一响应包。

        Args:
            method: HTTP 方法。
            path: 以 ``/api/v1`` 开头的路径。
            retryable: 是否允许重试。**非幂等**操作（如上传）传 ``False``，
                避免重试造成重复文档。
            **kwargs: 透传给 ``httpx`` 的参数（json/params/files/data）。

        Returns:
            ``code == 0`` 时的完整响应体（含 ``data``）。

        Raises:
            UnauthorizedError / NotFoundError / ValidationFailedError / KnowledgeError:
                由 HTTP 状态与业务码映射而来。
        """

        async def _call() -> httpx.Response:
            response = await self._client.request(
                method, f"{self._base_url}{path}", headers=self._headers, **kwargs
            )
            if response.status_code in {408, 425, 429} or response.status_code >= 500:
                response.raise_for_status()
            return response

        runner = self._retry(_call) if retryable else _call
        try:
            response = await runner()
        except httpx.TimeoutException as exc:
            raise KnowledgeError(
                f"RAGFlow 调用超时: {path}", detail="文档服务响应超时，请稍后重试。"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise _map_http_status(exc.response, path) from exc
        except httpx.TransportError as exc:
            raise KnowledgeError(
                f"RAGFlow 连接失败: {path}（{type(exc).__name__}）",
                detail="文档服务当前不可达，请确认 RAGFlow 容器已启动。",
            ) from exc
        if response.status_code >= 400:
            raise _map_http_status(response, path)
        body = _decode(response, path)
        code = body.get("code")
        if code == 0:
            return body
        raise _map_business_code(code, str(body.get("message", "")), path)


def _decode(response: httpx.Response, path: str) -> dict[str, Any]:
    """解析响应 JSON；非 JSON 视为契约破坏。"""
    try:
        body = response.json()
    except ValueError as exc:
        raise KnowledgeError(
            f"RAGFlow 响应非 JSON: {path}", detail="文档服务返回了非预期内容。"
        ) from exc
    if not isinstance(body, dict):
        raise KnowledgeError(f"RAGFlow 响应不是 JSON 对象: {path}")
    return body


def _map_http_status(response: httpx.Response, path: str) -> AgentPlatformError:
    """HTTP 状态码 → 平台异常（4xx 不重试）。"""
    status = response.status_code
    if status in {401, 403}:
        return UnauthorizedError(f"RAGFlow 鉴权失败: {path} {status}")
    if status == 404:
        return NotFoundError(f"RAGFlow 资源不存在: {path}")
    if status == 429:
        return KnowledgeError(f"RAGFlow 限流: {path} 429", detail="请求过于频繁，请稍后重试。")
    if status >= 500:
        return KnowledgeError(
            f"RAGFlow 服务错误: {path} {status}", detail="文档服务内部错误，请稍后重试。"
        )
    return ValidationFailedError(f"RAGFlow 拒绝请求: {path} {status}", detail="请求参数不合法。")


def _map_business_code(code: Any, message: str, path: str) -> AgentPlatformError:
    """RAGFlow 业务码 → 平台异常。"""
    if code == _CODE_AUTH_FAILED:
        return UnauthorizedError(f"RAGFlow 鉴权失败: {path}（{message}）")
    if code == _CODE_PERMISSION:
        return UnauthorizedError(f"RAGFlow 权限不足: {path}（{message}）")
    if code in {_CODE_ARGUMENT, _CODE_DATA}:
        return ValidationFailedError(f"RAGFlow 参数非法: {path}（{message}）")
    if code == _CODE_RUNNING:
        return KnowledgeError(
            f"RAGFlow 操作仍在进行: {path}（{message}）", detail="上一次操作尚未结束，请稍后重试。"
        )
    if code == _CODE_NOT_FOUND:
        return NotFoundError(f"RAGFlow 资源不存在: {path}")
    return KnowledgeError(
        f"RAGFlow 返回业务错误 code={code}: {path}（{message}）",
        detail="文档服务未能完成本次操作。",
    )


def _parse_object[M: BaseModel](body: dict[str, Any], model: type[M], label: str) -> M:
    """把 ``data`` 对象解析为给定模型。"""
    data = body.get("data")
    if not isinstance(data, dict):
        raise KnowledgeError(f"RAGFlow {label}响应缺少 data 对象")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise KnowledgeError(f"RAGFlow {label}响应不符合契约: {exc.error_count()} 处错误") from exc


def _parse_list[M: BaseModel](body: dict[str, Any], model: type[M], label: str) -> list[M]:
    """把 ``data`` 数组解析为给定模型列表。"""
    data = body.get("data")
    if not isinstance(data, list):
        raise KnowledgeError(f"RAGFlow {label}响应缺少 data 数组")
    try:
        return [model.model_validate(item) for item in data]
    except ValidationError as exc:
        raise KnowledgeError(f"RAGFlow {label}响应不符合契约: {exc.error_count()} 处错误") from exc
