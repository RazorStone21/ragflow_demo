"""引用标记解析与引用条目构造（NO-12 在输出边界的确定性校验）。"""

from __future__ import annotations

from typing import Any

import pytest

from agent_platform.agents.gov_policy_qa.schemas import build_citations, extract_citation_indices
from agent_platform.knowledge.schemas import EvidenceItem


def _evidence(chunk_id: str, **overrides: Any) -> EvidenceItem:
    """构造一条证据。"""
    base: dict[str, Any] = {
        "chunk_id": chunk_id,
        "content": "正文",
        "score": 0.9,
        "document_name": "政策.pdf",
        "page_numbers": [2, 3],
        "source_uri": "gov_doc/政策.pdf",
    }
    base.update(overrides)
    return EvidenceItem(**base)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("结论 [1]", [1]),
        ("结论 [1][3]", [1, 3]),
        ("结论【2】", [2]),
        ("多引用 [1,2]", [1, 2]),
        ("全角分隔 [1，2]", [1, 2]),
        ("重复引用 [1] 与 [1] 只算一次", [1]),
        ("无标记的回答", []),
        ("越界 [9]", []),
        ("零号 [0] 非法", []),
        ("正文里出现的方括号 [详见附件] 不算引用", []),
    ],
)
def test_extract_citation_indices(answer: str, expected: list[int]) -> None:
    """解析规则：只承认指向已有证据的合法下标，去重保序。"""
    assert extract_citation_indices(answer, max_index=3) == expected


def test_extract_citation_indices_returns_order_of_appearance() -> None:
    """保序：先出现的引用在前（引用列表按答案叙述顺序展示）。"""
    assert extract_citation_indices("先 [3] 后 [1]", max_index=3) == [3, 1]


def test_build_citations_maps_index_to_evidence() -> None:
    """下标 1 对应提示词里的第 1 条证据（1 基）。"""
    evidence = [_evidence("c1"), _evidence("c2", document_name="另一个政策.pdf")]
    citations = build_citations(evidence, [2])

    assert len(citations) == 1
    assert citations[0].index == 2
    assert citations[0].chunk_id == "c2"
    assert citations[0].document_name == "另一个政策.pdf"
    assert citations[0].page_numbers == [2, 3]
    assert citations[0].source_uri == "gov_doc/政策.pdf"


def test_build_citations_skips_out_of_range() -> None:
    """越界下标被跳过，不抛异常（调用方已保证合法，双保险）。"""
    assert build_citations([_evidence("c1")], [1, 5]) == build_citations([_evidence("c1")], [1])


def test_citation_is_immutable() -> None:
    """引用条目不可变（DTO 约定，CLAUDE.md 3.3）。"""
    citation = build_citations([_evidence("c1")], [1])[0]
    with pytest.raises(ValueError, match="frozen"):
        citation.index = 2  # 故意赋值以验证 frozen 约束生效
