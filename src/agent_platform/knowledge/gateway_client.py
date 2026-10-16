"""LiteLLM 网关 HTTP 客户端（CLAUDE.md 5.1 / NO-02）。

嵌入与重排**必须**经网关调用，业务代码不得直连厂商端点：
演示期网关后接 SiliconFlow 的 Qwen3-Embedding/Qwen3-Reranker，上线切私有化
只改网关映射，本模块与上层代码不变（CLAUDE.md 5.2）。

职责边界：只做"HTTP 传输 + 错误映射 + 响应校验"，不含检索语义。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_platform.core.errors import (
    ModelGatewayError,
    ModelRateLimitedError,
    ModelTimeoutError,
)
from agent_platform.core.retry import build_retry
from agent_platform.settings import Settings

# 网关为 OpenAI 兼容接口：/v1/embeddings；/v1/rerank 为 LiteLLM 的 rerank 路由。
_EMBEDDINGS_PATH = "/v1/embeddings"
_RERANK_PATH = "/v1/rerank"
_MODELS_PATH = "/v1/models"


class EmbeddingResponse(BaseModel):
    """``/v1/embeddings`` 响应（只校验用得到的字段，其余忽略）。"""

    model_config = ConfigDict(extra="ignore")

    class _Item(BaseModel):
        model_config = ConfigDict(extra="ignore")

        index: int = Field(ge=0)
        embedding: list[float] = Field(min_length=1)

    data: list[_Item] = Field(min_length=1)


class RerankItem(BaseModel):
    """``/v1/rerank`` 单条结果。"""

    model_config = ConfigDict(extra="ignore")

    index: int = Field(ge=0, description="对应 documents 中的下标")
    relevance_score: float = Field(description="相关度分数")


class RerankResponse(BaseModel):
    """``/v1/rerank`` 响应。"""

    model_config = ConfigDict(extra="ignore")

    results: list[RerankItem] = Field(default_factory=list)


class GatewayHttpClient:
    """对 LiteLLM 网关的异步 HTTP 封装。

    生命周期：由调用方持有并显式 ``aclose()``（或用作 async 上下文管理器），
    不得在模块导入期创建连接（CLAUDE.md 4.8）。
    """

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        """构造客户端。

        Args:
            settings: 平台配置（提供网关地址、密钥、超时与重试次数）。
            client: 可注入的 ``httpx.AsyncClient``（测试用 MockTransport；
                传入时其生命周期由调用方负责）。
        """
        # 网关地址与鉴权头都由本类逐请求携带，不依赖注入客户端是否带 base_url/默认头：
        # 注入共享连接池是常见生产用法，若依赖客户端默认配置会静默丢失凭据（有单测锁定）。
        self._base_url = settings.litellm_base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {settings.litellm_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        if client is None:
            self._owns_client = True
            self._client = httpx.AsyncClient(
                headers=self._headers,
                timeout=httpx.Timeout(settings.model_timeout_s),
                # 本应用的全部 httpx 流量都指向内网/本机服务，没有一处应当走宿主 HTTP 代理。
                # 而 httpx 默认 trust_env=True 会在 Windows 上读取注册表里的系统代理
                # （实测本机被 Clash 的 127.0.0.1:7890 劫持，连 localhost 请求都返回 502），
                # 于是"网关在不在"取决于某台机器上有没有开代理。这与 10.1/NO-15 的
                # 数据不出域要求也相悖：离线/信创环境更不该由宿主配置决定内部链路走向。
                trust_env=False,
            )
        else:
            self._owns_client = False
            self._client = client
        self._retry = build_retry(max_attempts=settings.model_max_retries + 1)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """关闭自建的 HTTP 连接池（注入的客户端由调用方关闭）。"""
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # 具体接口
    # ------------------------------------------------------------------
    async def embed(self, texts: list[str], *, logical_model: str) -> list[list[float]]:
        """批量向量化。

        Args:
            texts: 待嵌入文本（非空；顺序即返回顺序）。
            logical_model: 网关逻辑模型名（如 ``embedding``），NO-02/5.2。

        Returns:
            与 ``texts`` 等长的向量列表。

        Raises:
            ModelTimeoutError: 超时（可重试）。
            ModelRateLimitedError: 网关限流 429（可重试）。
            ModelGatewayError: 其他网关错误或响应不符合契约。
        """
        if not texts:
            return []
        payload = {"model": logical_model, "input": texts}
        body = await self._post(_EMBEDDINGS_PATH, payload)
        try:
            parsed = EmbeddingResponse.model_validate(body)
        except ValidationError as exc:
            raise ModelGatewayError(
                f"嵌入响应不符合契约: {exc.error_count()} 处错误",
                detail="嵌入服务返回了无法解析的结果，请检查网关配置。",
            ) from exc
        # 网关可能乱序返回，按 index 归位（而不是信任数组顺序）
        ordered: list[list[float] | None] = [None] * len(texts)
        for item in parsed.data:
            if item.index < len(ordered):
                ordered[item.index] = item.embedding
        missing = [i for i, vec in enumerate(ordered) if vec is None]
        if missing:
            raise ModelGatewayError(
                f"嵌入响应缺少下标 {missing}（请求 {len(texts)} 条，返回 {len(parsed.data)} 条）",
                detail="嵌入服务返回结果不完整。",
            )
        return [vec for vec in ordered if vec is not None]

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        logical_model: str,
        top_n: int | None = None,
    ) -> list[RerankItem]:
        """对候选文档重排。

        Args:
            query: 用户问题。
            documents: 候选文本（顺序即 ``index`` 语义）。
            logical_model: 网关逻辑模型名（如 ``rerank``）。
            top_n: 期望返回条数；``None`` 表示全部。

        Returns:
            重排结果（**不保证按分数排序**，调用方需自行排序）。

        Raises:
            ModelTimeoutError / ModelRateLimitedError / ModelGatewayError: 同 ``embed``。
        """
        if not documents:
            return []
        payload: dict[str, Any] = {"model": logical_model, "query": query, "documents": documents}
        if top_n is not None:
            payload["top_n"] = top_n
        body = await self._post(_RERANK_PATH, payload)
        try:
            return RerankResponse.model_validate(body).results
        except ValidationError as exc:
            raise ModelGatewayError(
                f"重排响应不符合契约: {exc.error_count()} 处错误",
                detail="重排服务返回了无法解析的结果，请检查网关配置。",
            ) from exc

    async def list_models(self) -> list[str]:
        """列出网关已注册的模型逻辑名（就绪探针与集成测试用）。"""
        body = await self._get(_MODELS_PATH)
        data = body.get("data")
        if not isinstance(data, list):
            raise ModelGatewayError("网关 /v1/models 响应缺少 data 数组")
        return [str(item.get("id", "")) for item in data if isinstance(item, dict)]

    # ------------------------------------------------------------------
    # 传输层
    # ------------------------------------------------------------------
    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """带重试的 POST JSON，返回解析后的 JSON 对象。"""

        @self._retry
        async def _call() -> httpx.Response:
            response = await self._client.post(
                f"{self._base_url}{path}", json=payload, headers=self._headers
            )
            # 仅在可重试状态码上抛错，其余交给下面统一映射
            if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                response.raise_for_status()
            return response

        return await self._run(_call, path)

    async def _get(self, path: str) -> dict[str, Any]:
        """带重试的 GET JSON。"""

        @self._retry
        async def _call() -> httpx.Response:
            response = await self._client.get(f"{self._base_url}{path}", headers=self._headers)
            if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                response.raise_for_status()
            return response

        return await self._run(_call, path)

    async def _run(
        self, call: Callable[[], Awaitable[httpx.Response]], path: str
    ) -> dict[str, Any]:
        """执行一次带重试的调用并统一映射异常与响应体。"""
        try:
            response = await call()
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(
                f"网关调用超时: {path}", detail="模型服务响应超时，请稍后重试。"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise _map_status_error(exc, path) from exc
        except httpx.TransportError as exc:
            raise ModelGatewayError(
                f"网关连接失败: {path}（{type(exc).__name__}）",
                detail="模型网关当前不可达，请联系管理员。",
            ) from exc
        if response.status_code >= 400:
            raise _map_response_status(response, path)
        try:
            body = response.json()
        except ValueError as exc:
            raise ModelGatewayError(
                f"网关响应非 JSON: {path}", detail="模型服务返回了非预期内容。"
            ) from exc
        if not isinstance(body, dict):
            raise ModelGatewayError(
                f"网关响应不是 JSON 对象: {path}", detail="模型服务返回了非预期内容。"
            )
        return body


def _map_status_error(exc: httpx.HTTPStatusError, path: str) -> Exception:
    """把重试耗尽后仍失败的 ``HTTPStatusError`` 映射为平台异常。"""
    return _map_response_status(exc.response, path)


def _map_response_status(response: httpx.Response, path: str) -> Exception:
    """按状态码映射为平台异常（4xx 不重试，5xx 已在重试内耗尽预算）。"""
    status = response.status_code
    if status == 429:
        return ModelRateLimitedError(f"网关限流: {path} 429")
    if status in {401, 403}:
        return ModelGatewayError(
            f"网关鉴权失败: {path} {status}",
            detail="模型网关拒绝了本次调用，请检查网关密钥配置。",
        )
    if status >= 500:
        return ModelGatewayError(
            f"网关服务异常: {path} {status}", detail="模型服务当前不可用，请稍后重试。"
        )
    return ModelGatewayError(f"网关返回 {status}: {path}", detail="模型服务无法完成本次请求。")
