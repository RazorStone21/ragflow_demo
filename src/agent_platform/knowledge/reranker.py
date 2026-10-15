"""重排封装（CLAUDE.md 7.4：检索默认混合检索 + Rerank）。

演示期经 LiteLLM 网关调用云端 Qwen3-Reranker。重排失败**不得**中断检索：
调用方（``retriever``）捕获后回退到融合分排序，保证"降级但仍可用"（CLAUDE.md 3.6）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_platform.core.errors import ModelGatewayError
from agent_platform.knowledge.gateway_client import GatewayHttpClient


@runtime_checkable
class Reranker(Protocol):
    """重排器接口（便于测试替换为确定性 fake）。"""

    async def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """重排候选。

        Returns:
            ``(原始下标, 分数)`` 列表，**按分数降序**；调用方据此重排证据。
        """
        ...


class LiteLLMReranker:
    """经 LiteLLM 网关的重排实现。

    Args:
        gateway: 网关客户端。
        logical_model: 网关逻辑模型名（默认配置为 ``rerank``）。
    """

    def __init__(self, gateway: GatewayHttpClient, *, logical_model: str) -> None:
        self._gateway = gateway
        self._logical_model = logical_model

    async def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """重排并按下标归位后降序返回。

        Raises:
            ModelGatewayError 及其子类: 网关调用失败（由调用方决定是否降级）。
        """
        if not documents:
            return []
        items = await self._gateway.rerank(
            query, documents, logical_model=self._logical_model, top_n=top_n
        )
        # 越界下标说明网关返回与请求不匹配：属于契约破坏，显式失败（不静默丢弃候选），
        # 由调用方回退到融合分排序（CLAUDE.md 3.6 降级策略）。
        for item in items:
            if not 0 <= item.index < len(documents):
                raise ModelGatewayError(
                    f"重排返回越界下标 {item.index}（候选数 {len(documents)}）",
                    detail="重排服务返回结果异常，已回退到向量排序。",
                )
        return sorted(
            ((item.index, item.relevance_score) for item in items),
            key=lambda pair: pair[1],
            reverse=True,
        )
