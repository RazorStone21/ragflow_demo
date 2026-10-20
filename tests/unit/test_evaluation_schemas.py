"""评估契约自身的单元测试（CLAUDE.md 3.3 / 9.2）。

契约里的校验器不是装饰：``MetricResult`` 的两条不变量与 ``GoldenCase`` 的两条
评审不变量的存在，就是为了让"假绿"在构造阶段就失败，而不是在报告里变成一个
看起来正常的数字。因此每条不变量的**拒绝分支**都必须被测到。
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from agent_platform.agents.gov_policy_qa.schemas import Citation
from agent_platform.evaluation.schemas import (
    SCHEMA_VERSION,
    STRIPPED_EVIDENCE_CONTENT,
    CaseCategory,
    CaseResult,
    CorpusSnapshot,
    EvalArtifact,
    EvalLayer,
    ExpectedImage,
    GoldenCase,
    GoldenSnapshot,
    MetricName,
    MetricResult,
    MetricVerdict,
    ModelSnapshot,
    RetrievalSnapshot,
    RunMeta,
    RunVerdict,
)
from agent_platform.knowledge.schemas import EvidenceItem, ImageRef, build_image_id


def test_not_applicable_metric_cannot_carry_a_score() -> None:
    """``N/A`` 却带分数会让"没考"混进聚合分——必须拒绝。"""
    with pytest.raises(ValidationError, match="不得带分数"):
        MetricResult(
            name=MetricName.FAITHFULNESS,
            verdict=MetricVerdict.NOT_APPLICABLE,
            score=0.0,
        )


def test_applicable_metric_must_carry_a_score() -> None:
    """通过/失败却无分数，聚合时会被静默当成 0 或 None——必须拒绝。"""
    with pytest.raises(ValidationError, match="必须带分数"):
        MetricResult(name=MetricName.FAITHFULNESS, verdict=MetricVerdict.PASS)


def test_metric_score_is_bounded() -> None:
    """分数必须在 [0,1]，越界是判分器写错了而不是数据问题。"""
    with pytest.raises(ValidationError):
        MetricResult(name=MetricName.RETRIEVAL_HIT, verdict=MetricVerdict.PASS, score=1.5)


def _make_artifact() -> EvalArtifact:
    evidence = EvidenceItem(chunk_id="c1", content="受版权约束的语料正文", score=0.9)
    return EvalArtifact(
        run=RunMeta(
            run_id="run-1",
            started_at="2026-10-16T00:00:00Z",
            models=ModelSnapshot(chat="primary-chat", embedding="embedding", rerank="rerank"),
            retrieval=RetrievalSnapshot(
                top_k=8,
                candidates=64,
                similarity_threshold=0.2,
                rerank_score_threshold=0.3,
                vector_weight=0.7,
                dim=4096,
            ),
            corpus=CorpusSnapshot(collection="gov_policy_chunks", chunk_count=195),
            golden=GoldenSnapshot(sha256="abc", case_count=28, reviewed_count=28),
        ),
        cases=[
            CaseResult(
                case_id="typical-01",
                category=CaseCategory.TYPICAL,
                question="问题",
                layer=EvalLayer.GRAPH,
                evidence=[evidence],
                judge_raw="裁判原始输出",
            )
        ],
        verdict=RunVerdict.PASS,
    )


def test_schema_version_is_pinned() -> None:
    """版本号是基线兼容性判定的依据，改动必须是显式动作。"""
    assert SCHEMA_VERSION == 1


def test_compact_for_baseline_strips_corpus_text() -> None:
    """基线副本必须剥离语料正文与裁判原文（NO-17：入仓的产物不含语料）。

    正文位置留的是**标记**而不是空串：``EvidenceItem.content`` 要求非空，空串会让
    整份基线读不回来——这一点实测踩过（见 ``compact_for_baseline`` 的 docstring）。
    故这里断言的是"原文不在了"加上"位置上是标记"，而不是"等于空串"。
    """
    compact = _make_artifact().compact_for_baseline()

    case = compact.cases[0]
    original_content = _make_artifact().cases[0].evidence[0].content
    assert original_content
    assert case.evidence[0].content == STRIPPED_EVIDENCE_CONTENT
    assert original_content not in case.evidence[0].content
    assert case.judge_raw == ""


def test_compact_for_baseline_keeps_the_comparable_parts() -> None:
    """剥离后仍要保留"当时命中了哪份文档的哪一页"——否则基线没法复核。"""
    original = _make_artifact()
    compact = original.compact_for_baseline()

    assert compact.run == original.run
    assert compact.cases[0].case_id == original.cases[0].case_id
    assert compact.cases[0].evidence[0].chunk_id == original.cases[0].evidence[0].chunk_id
    assert compact.cases[0].evidence[0].score == original.cases[0].evidence[0].score


def test_compact_for_baseline_does_not_mutate_the_original() -> None:
    """``frozen=True`` 的模型不得被就地改动（否则跑完对比就把原产物毁了）。"""
    original = _make_artifact()

    original.compact_for_baseline()

    assert original.cases[0].evidence[0].content == "受版权约束的语料正文"


def _image_ref(*, caption: str) -> ImageRef:
    """构造一张带图注的图片引用。"""
    sha = hashlib.sha256(b"figure").hexdigest()
    return ImageRef(
        image_id=build_image_id(sha), sha256=sha, mime="image/png", caption=caption, page=2
    )


def test_expected_images_are_optional_and_page_is_nullable() -> None:
    """旧黄金集没有 expected_images 仍可装载；独立图片文件没有页码。"""
    base = {
        "id": "typical-01",
        "category": CaseCategory.TYPICAL,
        "capability": "能力",
        "question": "问题",
        "expected_status": "answered",
    }
    legacy = GoldenCase.model_validate(base)
    assert legacy.expected_images == []

    with_image = GoldenCase.model_validate(
        {**base, "expected_images": [{"document_name": "扫描件.pdf"}]}
    )
    assert with_image.expected_images[0].page is None
    assert with_image.expected_images[0].must_hit is True


def test_expected_image_page_must_be_positive() -> None:
    """页码从 1 起，0 是出题时的笔误而不是合法值。"""
    with pytest.raises(ValidationError):
        ExpectedImage(document_name="扫描件.pdf", page=0)


def test_compact_for_baseline_strips_image_captions_but_keeps_ids() -> None:
    """基线保留图片 ID/哈希/页码，但图注是 VLM 生成、可能复述原文，必须剥掉。"""
    caption = "图中整段复述的政策原文"
    ref = _image_ref(caption=caption)
    original = _make_artifact()
    case = original.cases[0].model_copy(
        update={
            "evidence": [original.cases[0].evidence[0].model_copy(update={"images": [ref]})],
            "citations": [Citation(index=1, chunk_id="c1", images=[ref])],
        }
    )

    compact = original.model_copy(update={"cases": [case]}).compact_for_baseline()

    for images in (compact.cases[0].evidence[0].images, compact.cases[0].citations[0].images):
        assert images[0].caption == ""
        assert images[0].image_id == ref.image_id
        assert images[0].sha256 == ref.sha256
        assert images[0].page == ref.page
    assert caption not in compact.model_dump_json()


def test_run_verdict_has_an_incomplete_member() -> None:
    """``incomplete`` 是一等判定：有指标没采集时必须能与"通过"区分开。"""
    assert RunVerdict.INCOMPLETE.value == "incomplete"
