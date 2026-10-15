"""向量化封装（CLAUDE.md 7.2 / NO-02）。

演示期经 LiteLLM 网关调用云端 Qwen3-Embedding；上线切私有化模型**只改网关映射**，
本模块与调用方（检索、入库）不变。维度由配置声明，与网关返回值不符时 **fail-fast**，
避免写入脏向量到 Milvus。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_platform.core.errors import KnowledgeError
from agent_platform.knowledge.gateway_client import GatewayHttpClient


@runtime_checkable
class Embedder(Protocol):
    """向量化器接口（便于测试替换为确定性 fake）。"""

    @property
    def dim(self) -> int:
        """向量维度（与 Milvus 集合的稠密字段一致）。"""
        ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量向量化（入库路径）。"""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """单条查询向量化（检索路径）。"""
        ...


class LiteLLMEmbedder:
    """经 LiteLLM 网关的向量化实现。

    Args:
        gateway: 网关客户端（可注入 MockTransport 供单测）。
        logical_model: 网关逻辑模型名（默认配置为 ``embedding``）。
        expected_dim: 期望维度（来自 ``MILVUS_DIM``）；实际返回不符即抛错。
    """

    def __init__(
        self, gateway: GatewayHttpClient, *, logical_model: str, expected_dim: int
    ) -> None:
        self._gateway = gateway
        self._logical_model = logical_model
        self._dim = expected_dim

    @property
    def dim(self) -> int:
        return self._dim

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量向量化并校验维度。"""
        return self._validate(await self._gateway.embed(texts, logical_model=self._logical_model))

    async def embed_query(self, text: str) -> list[float]:
        """单条查询向量化。

        Raises:
            KnowledgeError: 输入为空或维度不符。
        """
        if not text.strip():
            raise KnowledgeError("查询文本为空，无法向量化", detail="请提供非空的问题。")
        vectors = await self.embed_documents([text])
        return vectors[0]

    def _validate(self, vectors: list[list[float]]) -> list[list[float]]:
        """校验返回向量维度与配置一致（不符即 fail-fast）。"""
        for position, vector in enumerate(vectors):
            if len(vector) != self._dim:
                raise KnowledgeError(
                    f"嵌入维度不符: 第 {position} 条返回 {len(vector)} 维，配置 MILVUS_DIM={self._dim}",
                    detail="嵌入模型与向量库维度不一致，请核对 MILVUS_DIM 配置。",
                )
        return vectors
