"""Milvus 向量库封装（ADR-0002：检索主链路）。

职责：
- 集合生命周期（``ensure_collection``，幂等）；
- 幂等入库（``upsert_chunks``，主键为 RAGFlow chunk_id）；
- 混合检索（稠密 + BM25 稀疏，``WeightedRanker`` 融合，租户过滤由表达式强制）；
- 按文档删除（用作废文档的 chunk 清出索引）。

并发模型：使用 pymilvus 的 ``AsyncMilvusClient``（原生 async），
**不得**在 async 路径调用同步客户端（NO-05）。
连接在 ``open_milvus_store`` 中建立，禁止在模块导入期连库（CLAUDE.md 4.8）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pymilvus import AnnSearchRequest, AsyncMilvusClient, WeightedRanker
from pymilvus.exceptions import MilvusException

from agent_platform.core.errors import ConfigurationError, KnowledgeError, RetrievalFailedError
from agent_platform.core.logging import get_logger
from agent_platform.core.retry import build_retry, is_retryable
from agent_platform.knowledge import milvus_schema as schema
from agent_platform.knowledge.schemas import ChunkRecord, ImageRef, MediaType, Position
from agent_platform.settings import Settings

logger = get_logger(__name__)

_UPSERT_BATCH_SIZE = 64
"""单次 upsert 的批大小：控制 4096 维向量的单请求体量。"""


class MilvusHit(BaseModel):
    """混合检索的原始命中（未经重排）。"""

    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    score: float = Field(description="WeightedRanker 归一化融合分（0~1）")
    dataset_id: str = ""
    document_id: str = ""
    document_name: str = ""
    source_uri: str = ""
    positions: list[Position] = Field(default_factory=list)
    updated_at: int = 0
    media_type: MediaType = Field(
        default=MediaType.TEXT, description="切分媒介类型；未知值降级为 text"
    )
    images: list[ImageRef] = Field(default_factory=list, description="证据图片元数据（ADR-0004）")


def _milvus_retryable(exc: BaseException) -> bool:
    """Milvus 异常的可重试判定。

    ``MilvusException`` 覆盖连接中断、服务尚未就绪、协调器切主等瞬时故障；
    库中所有被重试的操作都是幂等的（检索只读、upsert 以主键整行覆盖），
    因此可安全重试。4xx 语义的调用错误由平台异常先行拦截，不会走到这里。
    """
    return is_retryable(exc) or isinstance(exc, MilvusException)


class MilvusStore:
    """Milvus 集合操作封装。

    Args:
        client: 已连接的 ``AsyncMilvusClient``（测试可注入 fake）。
        collection: 集合名。
        dim: 稠密向量维度；与嵌入输出不符时入库 fail-fast。
        timeout_s: 单次操作的超时秒数。
        max_attempts: 单次操作的尝试次数（含首次）。
    """

    def __init__(
        self,
        client: AsyncMilvusClient,
        *,
        collection: str,
        dim: int,
        timeout_s: float = 60.0,
        max_attempts: int = 3,
    ) -> None:
        self._client = client
        self._collection = collection
        self._dim = dim
        self._timeout_s = timeout_s
        self._retry = build_retry(max_attempts, retryable=_milvus_retryable)

    @property
    def collection(self) -> str:
        """集合名（日志与测试断言用）。"""
        return self._collection

    @property
    def dim(self) -> int:
        """稠密向量维度。"""
        return self._dim

    async def close(self) -> None:
        """关闭连接。"""
        await self._client.close()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def ensure_collection(self) -> bool:
        """确保集合存在且结构符合当前版本（幂等）。

        Returns:
            ``True`` 表示本次新建了集合，``False`` 表示已存在。

        Raises:
            ConfigurationError: 集合已存在但缺少必需字段或维度不符，需要重建。
            KnowledgeError: 建集合失败。
        """
        try:
            if await self._client.has_collection(self._collection):
                await self._verify_schema()
                return False
            await self._client.create_collection(
                collection_name=self._collection,
                schema=schema.build_collection_schema(self._dim),
                index_params=schema.build_index_params(),
            )
            await self._client.load_collection(self._collection)
        except MilvusException as exc:
            raise KnowledgeError(
                f"Milvus 建集合失败: {self._collection}（code={exc.code}）",
                detail="向量库不可用或集合配置冲突，请联系管理员。",
            ) from exc
        logger.info(
            "milvus.collection_created", extra={"collection": self._collection, "dim": self._dim}
        )
        return True

    async def _verify_schema(self) -> None:
        """校验已存在集合的维度与必需字段（不一致即 fail-fast）。

        Milvus 不能给已有集合加列，旧版本建的集合（缺 ``media_type`` /
        ``images``）只能新建集合并重入库。这里**不自动删集合**：删库不可逆，
        必须由人决定；错误信息给出重建方式，让人一步到位。
        """
        try:
            description = await self._client.describe_collection(self._collection)
        except MilvusException as exc:  # pragma: no cover - 需真实服务才触发
            raise KnowledgeError(f"读取集合描述失败: {self._collection}") from exc
        fields = description.get("fields", []) if isinstance(description, dict) else []
        present: set[str] = set()
        for field in fields:
            if not isinstance(field, dict):
                continue
            name = field.get("name")
            if isinstance(name, str):
                present.add(name)
            if name != schema.FIELD_CONTENT_EMBEDDING:
                continue
            params = field.get("params") or {}
            actual = params.get("dim")
            if actual is not None and int(actual) != self._dim:
                raise ConfigurationError(
                    f"集合 {self._collection} 的向量维度为 {actual}，配置 MILVUS_DIM={self._dim}",
                    detail="向量库维度与嵌入模型不一致，需重建集合或修正 MILVUS_DIM。",
                )
        missing = [name for name in schema.REQUIRED_SCHEMA_FIELDS if name not in present]
        if missing:
            raise ConfigurationError(
                f"集合 {self._collection} 缺少必需字段: {', '.join(missing)}"
                f"（当前集合结构版本 v{schema.COLLECTION_SCHEMA_VERSION}）",
                detail=(
                    "这是引入图片证据（ADR-0004）之前建的旧集合，Milvus 不支持原地加列。"
                    "请把 MILVUS_COLLECTION 指向一个新集合名后重新入库，例如："
                    "MILVUS_COLLECTION=gov_policy_chunks_v2 uv run python scripts/ingest.py "
                    "--source <语料目录>；旧集合保留用于回滚。"
                ),
            )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    async def upsert_chunks(self, records: list[ChunkRecord], embeddings: list[list[float]]) -> int:
        """幂等写入切分单元（主键覆盖）。

        Args:
            records: 切分单元（``chunk_id`` 唯一）。
            embeddings: 与 ``records`` 一一对应的稠密向量。

        Returns:
            写入的行数。

        Raises:
            KnowledgeError: 长度不匹配、维度不符或 Milvus 写入失败。
        """
        if not records:
            return 0
        if len(records) != len(embeddings):
            raise KnowledgeError(
                f"记录数与向量数不匹配: {len(records)} vs {len(embeddings)}",
                detail="入库参数不一致，已中止以避免写入错位数据。",
            )
        for index, vector in enumerate(embeddings):
            if len(vector) != self._dim:
                raise KnowledgeError(
                    f"第 {index} 条向量维度为 {len(vector)}，期望 {self._dim}",
                    detail="嵌入维度与向量库不一致，入库已中止。",
                )
        rows = [
            schema.to_row(record, vector)
            for record, vector in zip(records, embeddings, strict=True)
        ]
        written = 0
        for start in range(0, len(rows), _UPSERT_BATCH_SIZE):
            batch = rows[start : start + _UPSERT_BATCH_SIZE]
            await self._call("upsert", self._client.upsert, self._collection, data=batch)
            written += len(batch)
        logger.info("milvus.upserted", extra={"collection": self._collection, "rows": written})
        return written

    async def flush(self) -> None:
        """把已写入的数据落盘并使其对检索可见。

        Milvus 默认是**有界一致性**：``upsert_chunks`` 返回之后，新数据在随后的一小段
        时间里查不到。实测（standalone v2.6.24，本机）：写完立即 ``count()`` 得 0，
        约 1 秒后得 3。对"入库后紧接着提问"的流程，这会表现为明明刚入库却检索不到证据，
        进而触发 NO-12 的无证据降级分支——用户看到的是一句"没找到依据"。

        因此入库流程**收尾时**必须显式调用一次。刻意不放进 ``upsert_chunks`` 里：
        flush 会封存段，按批（每 32 条）调用会把批量写入的优势全部抵消。
        """
        await self._call("flush", self._client.flush, self._collection)
        logger.info("milvus.flushed", extra={"collection": self._collection})

    async def delete_by_document(self, document_id: str, tenant_id: str) -> int:
        """删除某文档在指定租户下的全部 chunk（撤稿/重建索引用）。

        Returns:
            删除条数。
        """
        expression = (
            schema.tenant_filter(tenant_id) + " && " + _eq(schema.FIELD_DOCUMENT_ID, document_id)
        )
        result = await self._call(
            "delete", self._client.delete, self._collection, filter=expression
        )
        deleted = int(result.get("delete_count", 0)) if isinstance(result, dict) else 0
        logger.info("milvus.deleted", extra={"document_id": document_id, "rows": deleted})
        return deleted

    async def count(self, tenant_id: str | None = None) -> int:
        """统计 chunk 数量（可限定租户；就绪探针与入库校验用）。"""
        expression = schema.tenant_filter(tenant_id) if tenant_id else ""
        result = await self._call(
            "query",
            self._client.query,
            self._collection,
            filter=expression,
            output_fields=["count(*)"],
        )
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return int(result[0].get("count(*)", 0))
        return 0

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    async def hybrid_search(
        self,
        *,
        query_text: str,
        query_vector: list[float],
        expr: str,
        candidates: int,
        vector_weight: float,
    ) -> list[MilvusHit]:
        """稠密 + BM25 混合检索，按归一化加权融合分返回候选。

        Args:
            query_text: 原始问题（BM25 分支的输入，由 Milvus 侧分词）。
            query_vector: 问题向量（稠密分支的输入）。
            expr: 过滤表达式，**必须**含租户条件（由 ``tenant_filter`` 构造，NO-19）。
            candidates: 每个分支的召回条数（重排前候选池大小）。
            vector_weight: 稠密分支权重（0~1），稀疏分支权重为 ``1 - weight``。

        Returns:
            融合后的命中列表（按融合分降序），无命中时返回空列表。

        Raises:
            RetrievalFailedError: 向量库不可用或检索失败。
        """
        if len(query_vector) != self._dim:
            raise RetrievalFailedError(
                f"查询向量维度为 {len(query_vector)}，期望 {self._dim}",
                detail="向量维度与索引不一致，检索已中止。",
            )
        dense_request = AnnSearchRequest(
            data=[query_vector],
            anns_field=schema.FIELD_CONTENT_EMBEDDING,
            param={"ef": max(64, candidates)},
            limit=candidates,
            expr=expr,
        )
        sparse_request = AnnSearchRequest(
            data=[query_text],  # BM25 分支接收原文，不接受预计算向量
            anns_field=schema.FIELD_CONTENT_SPARSE,
            param={},
            limit=candidates,
            expr=expr,
        )
        ranker = WeightedRanker(vector_weight, 1.0 - vector_weight)
        results = await self._call(
            "hybrid_search",
            self._client.hybrid_search,
            self._collection,
            reqs=[dense_request, sparse_request],
            ranker=ranker,
            limit=candidates,
            output_fields=list(schema.SEARCH_OUTPUT_FIELDS),
        )
        return _to_hits(results)

    async def _call(
        self, operation: str, func: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any
    ) -> Any:
        """带超时与有界重试地调用底层客户端方法。

        Raises:
            RetrievalFailedError: 重试耗尽后仍是 Milvus 侧错误。
        """

        @self._retry
        async def _invoke() -> Any:
            return await func(*args, timeout=self._timeout_s, **kwargs)

        try:
            return await _invoke()
        except MilvusException as exc:
            raise RetrievalFailedError(
                f"Milvus 操作失败: {operation}（code={exc.code}）",
                detail="向量库操作失败，请稍后重试或联系管理员。",
            ) from exc


def _eq(field: str, value: str) -> str:
    """构造字符串等值过滤子句（转义字面量）。"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{field} == "{escaped}"'


def _to_hits(results: Any) -> list[MilvusHit]:
    """把 ``hybrid_search`` 的原始返回映射为 ``MilvusHit``。

    兼容 Milvus 2.6 的两种嵌套形态（``[[{...}]]`` 与 ``[{"hits": [...]}]``），
    无法识别的元素被跳过而不是抛错——上层据命中数走无证据分支（NO-12）。
    """
    if not results:
        return []
    first = results[0]
    if isinstance(first, dict):
        raw_hits = first.get("hits", [])
    elif isinstance(first, list):
        raw_hits = first
    else:  # pragma: no cover - 契约外的返回形态
        return []
    hits: list[MilvusHit] = []
    for raw in raw_hits:
        if not isinstance(raw, dict):
            continue
        raw_entity = raw.get("entity")
        entity: dict[str, Any] = raw_entity if isinstance(raw_entity, dict) else raw
        chunk_id = str(entity.get(schema.FIELD_CHUNK_ID, raw.get("id", "")))
        content = str(entity.get(schema.FIELD_CONTENT, ""))
        if not chunk_id or not content:
            continue
        positions = entity.get(schema.FIELD_POSITIONS)
        hits.append(
            MilvusHit(
                chunk_id=chunk_id,
                content=content,
                score=float(raw.get("distance", 0.0)),
                dataset_id=str(entity.get(schema.FIELD_DATASET_ID, "")),
                document_id=str(entity.get(schema.FIELD_DOCUMENT_ID, "")),
                document_name=str(entity.get(schema.FIELD_DOCUMENT_NAME, "")),
                source_uri=str(entity.get(schema.FIELD_SOURCE_URI, "")),
                positions=positions if isinstance(positions, list) else [],
                updated_at=int(entity.get(schema.FIELD_UPDATED_AT, 0) or 0),
                media_type=_parse_media_type(entity.get(schema.FIELD_MEDIA_TYPE)),
                images=_parse_images(entity.get(schema.FIELD_IMAGES), chunk_id=chunk_id),
            )
        )
    return hits


_MAX_IMAGES_PER_CHUNK = 8
"""单条证据的图片上限，与 ``ChunkRecord.images`` 的约束保持一致。"""


def _parse_media_type(raw: Any) -> MediaType:
    """解析媒介类型；未知值降级为 ``text``。

    将来 RAGFlow 若新增 ``video`` 之类取值，旧代码按文本处理仍能给出内容；
    反过来，让未知枚举把整条证据弄丢才是真正的损失。
    """
    try:
        return MediaType(str(raw))
    except ValueError:
        return MediaType.TEXT


def _parse_images(raw: Any, *, chunk_id: str) -> list[ImageRef]:
    """解析图片元数据列表；非法条目只跳过该条并记 WARNING。

    一条损坏的图片记录不应带走整条文本证据——那会把"少一张图"放大成
    "少一条证据"。超过上限的条目同样丢弃（上限与契约一致）。
    """
    if not isinstance(raw, list):
        return []
    images: list[ImageRef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            images.append(ImageRef.model_validate(item))
        except ValidationError:
            logger.warning(
                "证据图片元数据非法，已跳过该条",
                extra={"event": "milvus.image_meta_invalid", "chunk_id": chunk_id},
            )
    if len(images) > _MAX_IMAGES_PER_CHUNK:
        logger.warning(
            "证据图片数超过上限，已截断",
            extra={"event": "milvus.image_meta_truncated", "chunk_id": chunk_id},
        )
        images = images[:_MAX_IMAGES_PER_CHUNK]
    return images


@asynccontextmanager
async def open_milvus_store(settings: Settings) -> AsyncIterator[MilvusStore]:
    """建立 Milvus 连接并交出 ``MilvusStore``（用完自动关闭）。

    连接在此处惰性建立：FastAPI lifespan / 脚本入口调用，
    不得在模块导入期调用（CLAUDE.md 4.8）。
    """
    client = AsyncMilvusClient(
        uri=settings.milvus_uri, token=settings.milvus_token.get_secret_value()
    )
    store = MilvusStore(client, collection=settings.milvus_collection, dim=settings.milvus_dim)
    try:
        yield store
    finally:
        await store.close()
