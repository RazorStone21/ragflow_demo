"""知识域契约模型与页号提取（CLAUDE.md 7.4 引用要求）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_platform.knowledge.schemas import (
    ChunkRecord,
    EvidenceItem,
    ImageBBox,
    ImageRef,
    MediaType,
    RetrievalMeta,
    RetrievalOutcome,
    build_image_id,
    extract_page_numbers,
)


def _record(**overrides: object) -> ChunkRecord:
    """构造一条合法切分单元（便于逐字段覆盖）。"""
    base: dict[str, object] = {
        "chunk_id": "c1",
        "content": "电价机制",
        "tenant_id": "demo",
        "dataset_id": "ds1",
        "document_id": "doc1",
    }
    base.update(overrides)
    return ChunkRecord(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("positions", "expected"),
    [
        ([], []),
        ([[1, 0.0, 100.0, 0.0, 50.0]], [1]),  # PDF bbox：首元素为页号
        ([[3, 0, 1, 0, 1], [1, 0, 1, 0, 1]], [1, 3]),  # 多页且去重后升序
        ([[1], [1]], [1]),  # 重复页号去重
        ([{"page": 2, "x0": 0}], [2]),  # 字典形态
        ([2, "3"], [2, 3]),  # 标量/字符串形态
        ([[0, 10, 20, 30, 40]], []),  # 0 不是合法页号
        ([["a", "b"]], []),  # 无法识别
        ([[1, 100.0]], [1]),  # 坐标不被误当页号（只有首元素参与判定）
    ],
)
def test_extract_page_numbers(positions: list[object], expected: list[int]) -> None:
    """从多种 positions 形态中稳健提取页号，且不把 bbox 坐标误判为页号。"""
    assert extract_page_numbers(positions) == expected  # type: ignore[arg-type]


def test_page_numbers_ignores_large_float_coordinates() -> None:
    """浮点型坐标（如 1200.0）不作为页号采信。"""
    assert extract_page_numbers([[1, 1200.0, 800.0, 0.0, 0.0]]) == [1]


def test_chunk_record_requires_tenant_and_content() -> None:
    """租户与正文为必填（NO-19 隔离键不得缺失）。"""
    with pytest.raises(ValidationError):
        ChunkRecord(chunk_id="c1", content="", tenant_id="demo", dataset_id="d", document_id="x")
    with pytest.raises(ValidationError):
        ChunkRecord(  # type: ignore[call-arg]  # 故意漏传 tenant_id 以断言必填校验生效
            chunk_id="c1", content="正文", dataset_id="d", document_id="x"
        )


def test_models_are_frozen() -> None:
    """契约模型不可变（CLAUDE.md 3.3）。"""
    record = _record()
    with pytest.raises(ValidationError):
        setattr(record, "content", "篡改")  # noqa: B010  # 动态赋值以触发 frozen 校验


def test_outcome_carries_insufficient_evidence() -> None:
    """证据不足以空证据 + is_sufficient=False 表达，而非异常（CLAUDE.md 7.5）。"""
    outcome = RetrievalOutcome(
        evidence=[],
        meta=RetrievalMeta(
            query="无关问题",
            candidate_count=0,
            evidence_count=0,
            score_threshold=0.3,
            is_sufficient=False,
            reason="no_candidates",
        ),
    )
    assert outcome.evidence == []
    assert outcome.meta.is_sufficient is False
    assert outcome.meta.top_score is None


def test_evidence_item_keeps_citation_fields() -> None:
    """证据必须能追溯文档、页码与来源（CLAUDE.md 7.4）。"""
    item = EvidenceItem(
        chunk_id="c1",
        content="正文",
        score=0.9,
        document_id="doc1",
        document_name="某政策.pdf",
        page_numbers=[2, 3],
        source_uri="gov_doc/某政策.pdf",
    )
    assert item.document_name
    assert item.page_numbers
    assert item.source_uri


# ---------------------------------------------------------------------------
# 图片证据契约（ADR-0004）
# ---------------------------------------------------------------------------
_SHA = "0123456789abcdef" * 4
"""64 位十六进制摘要样本。"""


def _image_ref(**overrides: object) -> ImageRef:
    """构造一条合法图片引用。"""
    base: dict[str, object] = {
        "image_id": build_image_id(_SHA),
        "sha256": _SHA,
        "mime": "image/png",
    }
    base.update(overrides)
    return ImageRef(**base)  # type: ignore[arg-type]


def test_image_id_is_derived_from_sha256() -> None:
    """图片 ID 由内容摘要生成：同一张图在任何租户、任何次入库下 ID 都相同。"""
    assert build_image_id(_SHA) == "img_" + _SHA[:32]
    assert _image_ref().image_id == "img_" + _SHA[:32]


def test_image_ref_rejects_id_not_derived_from_sha() -> None:
    """ID 与摘要对不上说明数据被拼过，必须拒绝而不是照单全收。"""
    with pytest.raises(ValidationError, match="必须由 sha256 生成"):
        _image_ref(image_id="img_" + "f" * 32)


def test_image_ref_forbids_path_like_fields() -> None:
    """对外契约不含路径/URL/base64/存储 key（NO-19）。"""
    with pytest.raises(ValidationError):
        _image_ref(url="http://ragflow.internal/x.png")
    with pytest.raises(ValidationError):
        _image_ref(path="/tmp/x.png")


def test_image_ref_rejects_unknown_mime_and_bad_sha() -> None:
    """MIME 走白名单，摘要必须是 64 位十六进制。"""
    with pytest.raises(ValidationError):
        _image_ref(mime="image/svg+xml")
    with pytest.raises(ValidationError):
        _image_ref(sha256="abc")


def test_image_ref_carries_page_and_bbox() -> None:
    """页码与坐标可选，但给了就必须合法。"""
    ref = _image_ref(page=7, bbox=ImageBBox(left=1, right=20, top=3, bottom=30))
    assert ref.page == 7
    assert ref.bbox is not None
    assert ref.bbox.right == 20
    with pytest.raises(ValidationError):
        _image_ref(page=0)
    with pytest.raises(ValidationError):
        ImageBBox(left=-1, right=1, top=0, bottom=0)


def test_chunk_record_defaults_to_text_without_images() -> None:
    """旧数据没有新字段时按纯文本处理（加法式扩展，ADR-0004）。"""
    record = _record()
    assert record.media_type is MediaType.TEXT
    assert record.images == []


def test_chunk_record_limits_images_to_eight() -> None:
    """单条证据的图片数量有上限，防止一条切分把上下文撑爆。"""
    refs = [_image_ref() for _ in range(9)]
    with pytest.raises(ValidationError):
        _record(images=refs)


def test_evidence_item_carries_media_type_and_images() -> None:
    """检索证据原样带出媒介类型与图片引用，供引用与取图使用。"""
    item = EvidenceItem(
        chunk_id="c1",
        content="图 1 为本年度电价曲线",
        score=0.8,
        media_type=MediaType.IMAGE,
        images=[_image_ref(page=3)],
    )
    assert item.media_type is MediaType.IMAGE
    assert item.images[0].page == 3
