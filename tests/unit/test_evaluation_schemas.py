"""评估契约自身的单元测试（CLAUDE.md 3.3 / 9.2）。

契约里的校验器不是装饰：``MetricResult`` 的两条不变量与 ``GoldenCase`` 的评审
不变量，是为了让"假绿"在构造阶段就失败，而不是在报告里变成一个看起来正常的
数字。因此每条不变量的**拒绝分支**都必须被测到。

2026-10-21（ADR-0006）新增的两组不变量同样如此：``ERROR`` 不得带分数（测量
故障不是 0 分），``relevant_documents`` 未签署不得构造（未核对的标签不得进
门禁）。
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
    CitationAudit,
    CorpusSnapshot,
    EvalArtifact,
    EvalLayer,
    ExpectedImage,
    GoldenCase,
    GoldenSnapshot,
    JudgeSnapshot,
    MetricName,
    MetricResult,
    MetricVerdict,
    ModelSnapshot,
    RelevantDocument,
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


def test_error_metric_cannot_carry_a_score() -> None:
    """``ERROR`` 带分数就又变回"测量故障冒充质量数据点"（ADR-0003 的教训）。

    这是 ``MetricVerdict.ERROR`` 存在的全部意义：它必须与 ``FAIL 0.0`` 在
    结构上不可混淆，而不是靠调用方自觉。
    """
    with pytest.raises(ValidationError, match="不得带分数"):
        MetricResult(name=MetricName.FAITHFULNESS, verdict=MetricVerdict.ERROR, score=0.0)


def test_applicable_metric_must_carry_a_score() -> None:
    """通过/失败却无分数，聚合时会被静默当成 0 或 None——必须拒绝。"""
    with pytest.raises(ValidationError, match="必须带分数"):
        MetricResult(name=MetricName.FAITHFULNESS, verdict=MetricVerdict.PASS)


def test_metric_score_is_bounded() -> None:
    """分数必须在 [0,1]，越界是判分器写错了而不是数据问题。"""
    with pytest.raises(ValidationError):
        MetricResult(name=MetricName.RECALL_AT_K, verdict=MetricVerdict.PASS, score=1.5)


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
                citation_audits=[CitationAudit(index=1, supported=True, reason="人工核对")],
                judge_raw="裁判原始输出",
            )
        ],
        verdict=RunVerdict.PASS,
    )


def test_schema_version_is_pinned() -> None:
    """版本号是基线兼容性判定的依据，改动必须是显式动作。

    v2（ADR-0006）改了指标集、黄金集标签与裁判库；旧产物不做迁移，读入即报
    不可读。这条断言是那次"不兼容"的显式声明。
    """
    assert SCHEMA_VERSION == 2


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


def test_compact_for_baseline_strips_citation_audits() -> None:
    """人工抽检结论不进基线：理由同 ``judge_raw``，避免把证据原文带进入库文件。"""
    original = _make_artifact()
    assert original.cases[0].citation_audits, "构造的样例必须真的带审计记录"
    assert original.compact_for_baseline().cases[0].citation_audits == []


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
    assert original.cases[0].citation_audits


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


# ---------------------------------------------------------------------------
# 相关性标签：未签署不得进构造（ADR-0006）
# ---------------------------------------------------------------------------
def _case_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "typical-01",
        "category": CaseCategory.TYPICAL,
        "capability": "电价",
        "question": "贵州燃煤发电基准价是多少？",
        "expected_status": "answered",
    }
    payload.update(overrides)
    return payload


def _labels(*names: str) -> list[dict[str, object]]:
    return [{"file_name": name, "pages": [2], "basis": "原文写明了价格"} for name in names]


def test_signed_labels_are_accepted_and_must_cover_must_hit_documents() -> None:
    """Happy path：已签署的标签可构造；``must_hit`` 必须是标签的子集。"""
    case = GoldenCase.model_validate(
        _case_payload(
            expected_documents=[{"name": "0030.贵州电网销售电价表.xlsx", "must_hit": True}],
            relevant_documents=_labels("0030.贵州电网销售电价表.xlsx"),
            relevance_reviewed_by="kevin",
            relevance_reviewed_at="2026-10-21",
        )
    )

    assert case.has_relevance_labels
    assert case.is_answerable


def test_labels_without_signature_are_rejected() -> None:
    """未签署的标签不得构造——否则草稿就能混进检索门禁。"""
    with pytest.raises(ValidationError, match="必须同时给出 relevance_reviewed_by"):
        GoldenCase.model_validate(_case_payload(relevant_documents=_labels("a.pdf")))


def test_half_signed_labels_are_rejected() -> None:
    """有核对人没有日期（或反之）同样算未签署。"""
    with pytest.raises(ValidationError, match="同时给出或同时留空"):
        GoldenCase.model_validate(
            _case_payload(relevant_documents=_labels("a.pdf"), relevance_reviewed_by="kevin")
        )


def test_non_answerable_cases_cannot_carry_labels_or_signatures() -> None:
    """拒答/安全样例由 ``refusal_correctness`` 判分，给它们贴检索标签没有意义。"""
    with pytest.raises(ValidationError, match=r"非可答题.*不得有 relevant_documents"):
        GoldenCase.model_validate(
            _case_payload(
                expected_status="insufficient_evidence",
                relevant_documents=_labels("a.pdf"),
                relevance_reviewed_by="kevin",
                relevance_reviewed_at="2026-10-21",
            )
        )
    with pytest.raises(ValidationError, match="非可答题不得有相关性标签的签署字段"):
        GoldenCase.model_validate(
            _case_payload(
                expected_status="insufficient_evidence",
                relevance_reviewed_by="kevin",
                relevance_reviewed_at="2026-10-21",
            )
        )


def test_must_hit_documents_must_be_listed_in_the_labels() -> None:
    """``must_hit`` 文档不在标签全集里，会让 Recall 的分母自相矛盾。"""
    with pytest.raises(ValidationError, match="必须同时出现在 relevant_documents"):
        GoldenCase.model_validate(
            _case_payload(
                expected_documents=[{"name": "关键文件.pdf", "must_hit": True}],
                relevant_documents=_labels("背景材料.pdf"),
                relevance_reviewed_by="kevin",
                relevance_reviewed_at="2026-10-21",
            )
        )


def test_unlabeled_cases_remain_loadable_for_drafting() -> None:
    """标签整题缺席是允许的：草稿阶段尚未核对，检索三项判不适用（ADR-0006）。

    若构造阶段就要求 ``must_hit`` 被覆盖，未标注的黄金集连装载都做不到——
    而"先装进来、再逐条补标签"正是既定流程。
    """
    case = GoldenCase.model_validate(
        _case_payload(expected_documents=[{"name": "关键文件.pdf", "must_hit": True}])
    )

    assert case.is_answerable
    assert not case.has_relevance_labels


def test_weak_expectations_do_not_need_to_be_in_the_labels() -> None:
    """``must_hit=False`` 是弱期望，可以不在相关全集里。"""
    case = GoldenCase.model_validate(
        _case_payload(
            expected_documents=[{"name": "仅供参考.pdf", "must_hit": False}],
            relevant_documents=_labels("关键文件.pdf"),
            relevance_reviewed_by="kevin",
            relevance_reviewed_at="2026-10-21",
        )
    )
    assert case.has_relevance_labels


def test_duplicate_label_file_names_are_rejected() -> None:
    """重复文件名会把 Recall 的分母算大（同一份文档被数两次）。"""
    with pytest.raises(ValidationError, match="文件名不得重复"):
        GoldenCase.model_validate(
            _case_payload(
                relevant_documents=_labels("A.PDF", "a.pdf"),
                relevance_reviewed_by="kevin",
                relevance_reviewed_at="2026-10-21",
            )
        )


def test_label_file_name_must_be_a_basename() -> None:
    """带目录的名字永远匹配不上证据里的 ``document_name``。"""
    with pytest.raises(ValidationError, match="必须是文件名"):
        RelevantDocument(file_name="gov_doc/0001.pdf")


# ---------------------------------------------------------------------------
# 快照：新字段默认值必须是"未记录"而不是编一个数
# ---------------------------------------------------------------------------
def test_golden_snapshot_counts_default_to_zero() -> None:
    """旧产物读回来的计数是 0；报告据此显示"未标注"，而不是假装标齐了。"""
    snapshot = GoldenSnapshot(sha256="abc", case_count=28, reviewed_count=28)

    assert snapshot.answerable_count == 0
    assert snapshot.relevance_labeled_count == 0


def test_judge_snapshot_records_ragas_provenance() -> None:
    """RAGAS 版本、指标名单、嵌入型号与遥测状态都要能写进快照。"""
    snapshot = JudgeSnapshot(
        backend="ragas+in-house",
        ragas_version="0.4.3",
        ragas_metrics=["faithfulness", "answer_relevancy", "answer_correctness"],
        embedding_model="embedding",
        telemetry_disabled=True,
    )

    assert snapshot.ragas_version == "0.4.3"
    assert snapshot.ragas_metrics == ["faithfulness", "answer_relevancy", "answer_correctness"]
    assert snapshot.embedding_model == "embedding"
    assert snapshot.telemetry_disabled is True
    assert snapshot.max_tokens is None
    assert snapshot.upstream_model == ""


def test_citation_audit_index_starts_at_one() -> None:
    """引用编号从 1 起；0 是导出脚本的笔误。"""
    with pytest.raises(ValidationError):
        CitationAudit(index=0, supported=True)
