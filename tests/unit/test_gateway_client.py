"""LiteLLM 网关客户端：契约校验、错误映射与重试边界（CLAUDE.md 3.6 / 5 章）。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from agent_platform.core.errors import (
    ModelGatewayError,
    ModelRateLimitedError,
    ModelTimeoutError,
)
from agent_platform.knowledge.embedder import LiteLLMEmbedder
from agent_platform.knowledge.gateway_client import GatewayHttpClient
from agent_platform.knowledge.reranker import LiteLLMReranker
from agent_platform.settings import Settings


def _settings(*, max_retries: int = 0) -> Settings:
    """构造仅含网关字段的配置（跳过未用到的必填项，避免读取真实 .env）。"""
    return Settings.model_construct(
        litellm_base_url="http://gateway.test",
        litellm_api_key=SecretStr("test-key"),
        model_timeout_s=5.0,
        model_max_retries=max_retries,
    )


def _gateway(handler: Any, *, max_retries: int = 0) -> GatewayHttpClient:
    """用 MockTransport 构造网关客户端（不产生真实网络流量）。"""
    return GatewayHttpClient(
        _settings(max_retries=max_retries),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_embed_orders_results_by_index() -> None:
    """网关乱序返回时按 index 归位，保证与入参顺序对齐。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0, 0.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]},
                ]
            },
        )

    vectors = await _gateway(handler).embed(["甲", "乙"], logical_model="embedding")
    assert vectors == [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]


async def test_embed_rejects_incomplete_response() -> None:
    """返回条数不足时显式失败，不静默补齐（NO-06）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})

    with pytest.raises(ModelGatewayError, match="缺少下标"):
        await _gateway(handler).embed(["甲", "乙"], logical_model="embedding")


async def test_embed_rate_limited_maps_to_retryable_error() -> None:
    """429 映射为可重试的限流异常。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "too many requests"})

    with pytest.raises(ModelRateLimitedError):
        await _gateway(handler).embed(["甲"], logical_model="embedding")


async def test_embed_timeout_maps_to_timeout_error() -> None:
    """超时映射为可重试的超时异常。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(ModelTimeoutError):
        await _gateway(handler).embed(["甲"], logical_model="embedding")


async def test_embed_auth_failure_is_not_retryable() -> None:
    """鉴权失败映射为网关错误且不重试（CLAUDE.md 3.6 禁止重试 4xx）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid key"})

    with pytest.raises(ModelGatewayError, match="鉴权失败"):
        await _gateway(handler).embed(["甲"], logical_model="embedding")


async def test_retryable_status_is_retried_then_succeeds() -> None:
    """可重试状态码在有重试预算时重试并最终成功。"""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5]}]})

    vectors = await _gateway(handler, max_retries=2).embed(["甲"], logical_model="embedding")
    assert vectors == [[0.5]]
    assert attempts["n"] == 2


async def test_client_error_is_not_retried() -> None:
    """4xx 不重试：只发生一次调用。"""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    with pytest.raises(ModelGatewayError):
        await _gateway(handler, max_retries=3).embed(["甲"], logical_model="embedding")
    assert attempts["n"] == 1


async def test_rerank_returns_items() -> None:
    """重排响应解析为 (index, 分数) 候选。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "rerank"
        assert body["top_n"] == 2
        return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.9}]})

    items = await _gateway(handler).rerank("问题", ["甲", "乙"], logical_model="rerank", top_n=2)
    assert [(item.index, item.relevance_score) for item in items] == [(1, 0.9)]


async def test_rerank_rejects_malformed_response() -> None:
    """重排响应缺字段时抛网关错误（不信任外部响应，CLAUDE.md 3.3）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"index": "x"}]})

    with pytest.raises(ModelGatewayError, match="重排响应不符合契约"):
        await _gateway(handler).rerank("问题", ["甲"], logical_model="rerank")


async def test_list_models_parses_ids() -> None:
    """就绪探针可读取网关已注册的逻辑模型名。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "primary-chat"}, {"id": "embedding"}]})

    assert await _gateway(handler).list_models() == ["primary-chat", "embedding"]


async def test_list_models_requires_data_array() -> None:
    """响应结构不符时显式失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list"})

    with pytest.raises(ModelGatewayError, match="缺少 data"):
        await _gateway(handler).list_models()


async def test_embedder_fails_fast_on_dimension_mismatch() -> None:
    """嵌入维度与 MILVUS_DIM 不符时 fail-fast（避免写入脏向量）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    embedder = LiteLLMEmbedder(_gateway(handler), logical_model="embedding", expected_dim=4)
    with pytest.raises(Exception, match="嵌入维度不符"):
        await embedder.embed_documents(["甲"])


async def test_embedder_rejects_blank_query() -> None:
    """空问题不发起网关调用。"""
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})

    embedder = LiteLLMEmbedder(_gateway(handler), logical_model="embedding", expected_dim=1)
    with pytest.raises(Exception, match="查询文本为空"):
        await embedder.embed_query("   ")
    assert called["n"] == 0


async def test_reranker_sorts_desc_and_detects_out_of_range_index() -> None:
    """重排结果按分数降序；越界下标视为契约破坏并失败。"""
    payload = {
        "results": [{"index": 0, "relevance_score": 0.2}, {"index": 1, "relevance_score": 0.8}]
    }

    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    reranker = LiteLLMReranker(_gateway(ok_handler), logical_model="rerank")
    assert await reranker.rerank("问题", ["甲", "乙"]) == [(1, 0.8), (0, 0.2)]

    def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"index": 5, "relevance_score": 0.8}]})

    with pytest.raises(ModelGatewayError, match="越界下标"):
        await LiteLLMReranker(_gateway(bad_handler), logical_model="rerank").rerank(
            "问题", ["甲", "乙"]
        )
