"""知识域数据契约（CLAUDE.md 3.3 / 7.4）。

本模块是 `knowledge/` 与 `agents/` 之间的唯一契约来源：
- ``ChunkRecord``：入库单元（RAGFlow 切分产物 + 检索所需元数据）。
- ``EvidenceItem``：检索证据，必须携带引用信息（来源/章节/页码/链接），
  无引用不得作为事实结论（CLAUDE.md 7.4 / NO-12）。
- ``RetrievalOutcome``：检索结果 + 判定元数据，供路由做确定性决策。

所有模型不可变（``frozen=True``）；大对象（全文/图像）不入 State，只保留引用。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# RAGFlow 的 positions 结构随解析器而变（PDF 为 bbox 列表，文本为页号列表），
# 因此只做"列表"级别的约束，页号经 page_numbers() 防御式提取。
Position = list[Any]

_MAX_PAGE_HINT = 10000
"""浮点页号的采信上限：超过该值视为坐标而非页号，避免把 bbox 误当页号。"""

ImageMime = Literal["image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff"]
"""允许进入证据链的图片类型。

白名单而非通配：取图端点会把 MIME 原样写进 ``Content-Type``，放开等于让
上游内容决定响应类型；这几类是 RAGFlow 解析与浏览器展示的公共交集。
"""

_IMAGE_ID_PREFIX = "img_"
_IMAGE_ID_SHA_CHARS = 32


def build_image_id(sha256_hex: str) -> str:
    """由图片内容的 sha256 生成平台图片 ID。

    Args:
        sha256_hex: 64 位小写十六进制摘要。

    Returns:
        ``img_`` 加摘要前 32 位；同一张图片在任何租户、任何次入库下 ID 都相同。
    """
    return f"{_IMAGE_ID_PREFIX}{sha256_hex[:_IMAGE_ID_SHA_CHARS]}"


class MediaType(StrEnum):
    """切分的媒介类型（供检索、引用与前端渲染区分）。"""

    TEXT = "text"
    IMAGE = "image"
    TABLE = "table"


class ImageBBox(BaseModel):
    """图片在页面中的坐标（RAGFlow ``positions`` 的 left/right/top/bottom）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    left: int = Field(ge=0)
    right: int = Field(ge=0)
    top: int = Field(ge=0)
    bottom: int = Field(ge=0)


class ImageRef(BaseModel):
    """证据图片的对外元数据（ADR-0004）。

    **刻意不含**路径、URL、base64、存储 key 或 RAGFlow 内部 ID：客户端只拿得到
    平台生成的标识，取图必须走会话内的鉴权端点。``image_id`` 与 ``sha256``
    由校验器强制对应，二者对不上说明数据被拼过。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    image_id: str = Field(pattern=r"^img_[0-9a-f]{32}$", description="平台图片 ID")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="图片内容 sha256")
    mime: ImageMime = Field(description="图片 MIME（白名单）")
    caption: str = Field(default="", max_length=240, description="图注或描述")
    page: int | None = Field(default=None, ge=1, description="所在页码（1 起）")
    bbox: ImageBBox | None = Field(default=None, description="页面内坐标")

    @model_validator(mode="after")
    def _image_id_derives_from_sha256(self) -> ImageRef:
        expected = build_image_id(self.sha256)
        if self.image_id != expected:
            raise ValueError(f"image_id 必须由 sha256 生成（期望 {expected}）")
        return self


class ChunkRecord(BaseModel):
    """待写入 Milvus 的切分单元。

    ``chunk_id`` 直接沿用 RAGFlow 的 chunk 主键，保证同步管线幂等
    （同一 chunk 重复入库为 upsert 覆盖，不产生重复）。
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(min_length=1, max_length=128, description="RAGFlow chunk 主键")
    content: str = Field(min_length=1, description="切分文本（BM25 与嵌入的输入）")
    tenant_id: str = Field(min_length=1, max_length=64, description="租户隔离键，NO-19")
    dataset_id: str = Field(min_length=1, max_length=128, description="RAGFlow 数据集 ID")
    document_id: str = Field(min_length=1, max_length=128, description="RAGFlow 文档 ID")
    document_name: str = Field(default="", max_length=512, description="文档名（引用展示用）")
    positions: list[Position] = Field(default_factory=list, description="解析器给出的位置信息")
    source_uri: str = Field(default="", max_length=1024, description="原始来源路径/URI（引用用）")
    content_fingerprint: str = Field(
        default="", max_length=64, description="内容指纹（sha256 前 64 位十六进制），幂等键组成"
    )
    updated_at: int = Field(default=0, ge=0, description="UTC epoch 秒")
    media_type: MediaType = Field(default=MediaType.TEXT, description="切分媒介类型")
    images: list[ImageRef] = Field(
        default_factory=list, max_length=8, description="切分关联的图片（只存元数据）"
    )

    def page_numbers(self) -> list[int]:
        """从 ``positions`` 中防御式提取页号（去重、升序）。

        RAGFlow 不同解析器输出的结构不同，此处对任意嵌套形状容错：
        只收集能安全转为正整数的首个元素。
        """
        return extract_page_numbers(self.positions)


class EvidenceItem(BaseModel):
    """检索证据（含引用，CLAUDE.md 7.4）。"""

    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(min_length=1, description="切分主键（用于去重与引用）")
    content: str = Field(min_length=1, description="证据原文")
    score: float = Field(description="最终排序分（重排分为准，无重排时为融合分）")
    vector_score: float | None = Field(default=None, description="稠密检索原始分（余弦相似度）")
    bm25_score: float | None = Field(default=None, description="BM25 原始分")
    dataset_id: str = Field(default="", description="数据集 ID")
    document_id: str = Field(default="", description="文档 ID")
    document_name: str = Field(default="", description="文档名")
    page_numbers: list[int] = Field(default_factory=list, description="页码（引用展示用）")
    source_uri: str = Field(default="", description="来源路径/URI")
    media_type: MediaType = Field(default=MediaType.TEXT, description="证据媒介类型")
    images: list[ImageRef] = Field(
        default_factory=list, max_length=8, description="证据关联的图片（只存元数据）"
    )


class RetrievalMeta(BaseModel):
    """检索判定元数据（供 edges.py 做确定性路由，CLAUDE.md 4.3）。"""

    model_config = ConfigDict(frozen=True)

    query: str = Field(description="归一化后的检索问题")
    candidate_count: int = Field(ge=0, description="混合检索召回的候选数（重排前）")
    evidence_count: int = Field(ge=0, description="最终保留的证据数")
    top_score: float | None = Field(default=None, description="最高分；空结果为 None")
    score_threshold: float = Field(ge=0.0, description="本次生效的分数阈值")
    is_sufficient: bool = Field(description="证据是否足以作答（False 时走降级分支）")
    reason: str = Field(description="判定理由（稳定字符串，便于观测与测试断言）")
    rerank_applied: bool = Field(
        default=False, description="本次是否成功重排；False 表示已降级为融合分排序"
    )


class RetrievalOutcome(BaseModel):
    """一次检索的完整产出：证据 + 元数据。

    无结果属于**正常业务分支**而非异常：``evidence`` 为空、``is_sufficient=False``，
    由上层走"无证据不编造"降级（CLAUDE.md 7.5 / NO-12）。
    """

    model_config = ConfigDict(frozen=True)

    evidence: list[EvidenceItem] = Field(default_factory=list)
    meta: RetrievalMeta


def extract_page_numbers(positions: list[Position]) -> list[int]:
    """从 RAGFlow 的 positions 结构中提取页号。

    RAGFlow 的 ``positions`` 随解析器而变，已观测到的形态：
    ``[[page, x0, x1, y0, y1], ...]``（PDF/OCR）、``[page, ...]``、
    ``[{"page": n, ...}]``、``[]``。约定**只有首元素**可能是页号——
    坐标值是浮点数，若一并参与转换会被误判为页号，因此这里不做扁平化遍历。

    Args:
        positions: RAGFlow 输出的位置列表。

    Returns:
        去重且升序的页号列表；无法识别的元素被忽略（不抛异常）。
    """
    found: set[int] = set()
    for item in positions:
        # 列表形态取首元素；标量形态直接判定；字典形态取 page 字段
        candidate = item[0] if isinstance(item, (list, tuple)) and item else item
        page = _coerce_page(candidate)
        if page is not None:
            found.add(page)
    return sorted(found)


def _coerce_page(value: Any) -> int | None:
    """尝试把单个元素解释为页号（正整数）；否则返回 None。"""
    if isinstance(value, dict):
        return _coerce_page(value.get("page") or value.get("page_no") or value.get("page_number"))
    if isinstance(value, bool):  # bool 是 int 子类，必须先排除
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    # 浮点数只接受"看起来像页号"的整数值，且坐标值（通常远大于合理页数）不予采信
    if isinstance(value, float):
        return int(value) if value.is_integer() and 1.0 <= value <= _MAX_PAGE_HINT else None
    return None
