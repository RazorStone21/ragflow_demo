"""Milvus 集合 Schema 与行映射（ADR-0002 检索主链路）。

集合结构（稠密 + BM25 稀疏混合检索，CLAUDE.md 7.4）：

==================  ======================  ==========================================
字段                类型                    用途
==================  ======================  ==========================================
``chunk_id``        VARCHAR (PK)            RAGFlow chunk 主键，天然幂等
``content``         VARCHAR + analyzer     切分原文；``chinese`` 分词器供 BM25 使用
``content_sparse``  SPARSE_FLOAT_VECTOR     BM25 函数自动生成（不要写入）
``content_dense``   FLOAT_VECTOR(dim)       云/私有化嵌入向量
``tenant_id``       VARCHAR                 租户过滤，NO-19
``dataset_id``      VARCHAR                 数据集过滤
``document_id``     VARCHAR                 文档过滤/删除
``document_name``   VARCHAR                 引用展示
``source_uri``      VARCHAR                 引用来源
``positions``       JSON                    解析器位置信息（页号提取用）
``fingerprint``     VARCHAR                 内容指纹（幂等键组成）
``updated_at``      INT64                   UTC epoch 秒
==================  ======================  ==========================================

字段名集中在此模块导出，检索、入库、测试三处共用同一常量，杜绝拼写漂移。
"""

from __future__ import annotations

from typing import Any

from pymilvus import AsyncMilvusClient, CollectionSchema, DataType, Function, FunctionType
from pymilvus.milvus_client.index import IndexParams

from agent_platform.knowledge.schemas import ChunkRecord

FIELD_CHUNK_ID = "chunk_id"
FIELD_CONTENT = "content"
FIELD_CONTENT_EMBEDDING = "content_embedding"
FIELD_CONTENT_SPARSE = "content_sparse"
FIELD_TENANT_ID = "tenant_id"
FIELD_DATASET_ID = "dataset_id"
FIELD_DOCUMENT_ID = "document_id"
FIELD_DOCUMENT_NAME = "document_name"
FIELD_SOURCE_URI = "source_uri"
FIELD_POSITIONS = "positions"
FIELD_FINGERPRINT = "fingerprint"
FIELD_UPDATED_AT = "updated_at"

BM25_FUNCTION_NAME = "content_bm25"

CONTENT_MAX_LEN = 65535
"""VARCHAR 上限；RAGFlow 单个 chunk 远小于此，超长时由调用方截断。"""

# 检索需要回读的字段（稀疏向量与 BM25 原始分不可直接输出，故不在列）
SEARCH_OUTPUT_FIELDS: tuple[str, ...] = (
    FIELD_CHUNK_ID,
    FIELD_CONTENT,
    FIELD_DATASET_ID,
    FIELD_DOCUMENT_ID,
    FIELD_DOCUMENT_NAME,
    FIELD_SOURCE_URI,
    FIELD_POSITIONS,
    FIELD_UPDATED_AT,
)


def build_collection_schema(dim: int) -> CollectionSchema:
    """构造集合 Schema（含 BM25 内置函数）。

    Args:
        dim: 稠密向量维度，必须与嵌入模型输出一致（配置 ``MILVUS_DIM``）。

    Returns:
        可直接传给 ``create_collection`` 的 Schema；``chunk_id`` 为主键且
        ``auto_id=False``（主键由 RAGFlow 决定，保证重复入库幂等）。
    """
    schema = AsyncMilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(
        field_name=FIELD_CHUNK_ID, datatype=DataType.VARCHAR, max_length=128, is_primary=True
    )
    schema.add_field(
        field_name=FIELD_CONTENT,
        datatype=DataType.VARCHAR,
        max_length=CONTENT_MAX_LEN,
        enable_analyzer=True,
        # Milvus 2.6 内置中文分析器 = jieba 分词 + cnalphanumonly 过滤
        analyzer_params={"type": "chinese"},
    )
    schema.add_field(field_name=FIELD_CONTENT_SPARSE, datatype=DataType.SPARSE_FLOAT_VECTOR)
    schema.add_field(field_name=FIELD_CONTENT_EMBEDDING, datatype=DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field(field_name=FIELD_TENANT_ID, datatype=DataType.VARCHAR, max_length=64)
    schema.add_field(field_name=FIELD_DATASET_ID, datatype=DataType.VARCHAR, max_length=128)
    schema.add_field(field_name=FIELD_DOCUMENT_ID, datatype=DataType.VARCHAR, max_length=128)
    schema.add_field(field_name=FIELD_DOCUMENT_NAME, datatype=DataType.VARCHAR, max_length=512)
    schema.add_field(field_name=FIELD_SOURCE_URI, datatype=DataType.VARCHAR, max_length=1024)
    schema.add_field(field_name=FIELD_POSITIONS, datatype=DataType.JSON)
    schema.add_field(field_name=FIELD_FINGERPRINT, datatype=DataType.VARCHAR, max_length=64)
    schema.add_field(field_name=FIELD_UPDATED_AT, datatype=DataType.INT64)
    schema.add_function(
        Function(
            name=BM25_FUNCTION_NAME,
            function_type=FunctionType.BM25,
            input_field_names=[FIELD_CONTENT],
            output_field_names=[FIELD_CONTENT_SPARSE],
            description="中文 BM25 全文检索（jieba 分词）",
        )
    )
    return schema


def build_index_params() -> IndexParams:
    """构造索引参数（稀疏 BM25 + 稠密 HNSW + 标量 AUTOINDEX）。

    Returns:
        ``IndexParams``；标量字段不指定 ``index_type``，由 Milvus 按类型
        自动选择（VARCHAR → 倒排索引），避免硬编码索引算法名。
    """
    index_params = AsyncMilvusClient.prepare_index_params()
    index_params.add_index(
        field_name=FIELD_CONTENT_SPARSE,
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
        params={"inverted_index_algo": "DAAT_MAXSCORE", "bm25_k1": 1.2, "bm25_b": 0.75},
    )
    index_params.add_index(
        field_name=FIELD_CONTENT_EMBEDDING,
        index_type="HNSW",
        metric_type="COSINE",
        # M/efConstruction 取小值以适配 16GB 本地内存（CLAUDE.md 11.2 资源受限）
        params={"M": 16, "efConstruction": 100},
    )
    index_params.add_index(field_name=FIELD_TENANT_ID)  # AUTOINDEX：租户过滤加速，NO-19
    index_params.add_index(field_name=FIELD_DOCUMENT_ID)  # AUTOINDEX：按文档删除/过滤
    return index_params


def to_row(record: ChunkRecord, embedding: list[float]) -> dict[str, Any]:
    """把 ``ChunkRecord`` + 向量映射为 Milvus 行数据。

    稀疏字段 ``content_sparse`` **不得**出现：它由 BM25 函数在插入时自动生成。
    """
    return {
        FIELD_CHUNK_ID: record.chunk_id,
        FIELD_CONTENT: record.content[:CONTENT_MAX_LEN],
        FIELD_CONTENT_EMBEDDING: embedding,
        FIELD_TENANT_ID: record.tenant_id,
        FIELD_DATASET_ID: record.dataset_id,
        FIELD_DOCUMENT_ID: record.document_id,
        FIELD_DOCUMENT_NAME: record.document_name,
        FIELD_SOURCE_URI: record.source_uri,
        FIELD_POSITIONS: record.positions,
        FIELD_FINGERPRINT: record.content_fingerprint,
        FIELD_UPDATED_AT: record.updated_at,
    }


def tenant_filter(tenant_id: str, dataset_id: str | None = None) -> str:
    """构造 Milvus 过滤表达式（租户隔离必填，NO-19）。

    Args:
        tenant_id: 认证上下文解析出的租户标识（不得来自前端裸传）。
        dataset_id: 可选的数据集限定。

    Returns:
        形如 ``tenant_id == "t" && dataset_id == "d"`` 的表达式；
        字符串值经转义，防止表达式注入。
    """
    expression = f'{FIELD_TENANT_ID} == "{_escape(tenant_id)}"'
    if dataset_id:
        expression += f' && {FIELD_DATASET_ID} == "{_escape(dataset_id)}"'
    return expression


def _escape(value: str) -> str:
    """转义过滤表达式中的字符串字面量（双引号与反斜杠）。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')
