"""「无据」前缀门与检索元数据降级（CLAUDE.md 4.7 / 7.5 / NO-12）。

两道契约各测一件事：

- ``NoEvidencePrefixGate``：只有**首个非空白前缀**能触发拒答判定。部分作答的开头
  不是标记，因此这条保护是结构性的，不靠文案匹配；标记之后的草稿一律不推送。
- ``downgrade_retrieval_meta``：生成侧推翻检索判断后，状态与元数据必须自洽
  （``score_refusal`` 对二者矛盾直接判失败）。不可变对象不得被原地改写。
"""

from __future__ import annotations

import pytest

from agent_platform.agents.gov_policy_qa.nodes.no_evidence_marker import NoEvidencePrefixGate
from agent_platform.agents.gov_policy_qa.nodes.result import (
    REASON_MODEL_DECLARED_INSUFFICIENT,
    downgrade_retrieval_meta,
)
from agent_platform.agents.gov_policy_qa.prompts import ANSWER_NO_EVIDENCE_MARKER
from agent_platform.knowledge.schemas import RetrievalMeta

_MARKER = ANSWER_NO_EVIDENCE_MARKER


def _meta(*, is_sufficient: bool = True) -> RetrievalMeta:
    """构造一份"检索层认为证据可用"的判定元数据。"""
    return RetrievalMeta(
        query="新能源电价机制",
        candidate_count=12,
        evidence_count=3,
        top_score=0.87,
        score_threshold=0.3,
        is_sufficient=is_sufficient,
        reason="sufficient",
        rerank_applied=True,
    )


# ---------------------------------------------------------------------------
# 前缀门
# ---------------------------------------------------------------------------
def test_marker_in_single_chunk_is_refused_and_nothing_is_released() -> None:
    """标记独占一个增量：判拒答、不推送任何文本，流结束时也是空串。"""
    gate = NoEvidencePrefixGate()

    assert gate.feed(_MARKER) is None
    assert gate.flush() == ""
    assert gate.is_refused is True


def test_marker_split_across_chunks_is_refused() -> None:
    """标记跨多个 token（模型逐字流式）仍能判定。"""
    gate = NoEvidencePrefixGate()

    released = [gate.feed(part) for part in ("【", "无", "据", "】")]

    assert released == [None, None, None, None]
    assert gate.is_refused is True
    assert gate.flush() == ""


def test_leading_whitespace_does_not_hide_the_marker() -> None:
    """标记之前只有空白时仍算"首个非空白前缀"。"""
    gate = NoEvidencePrefixGate()

    assert gate.feed("\n  ") is None
    assert gate.feed(_MARKER) is None
    assert gate.is_refused is True


def test_text_after_the_marker_is_discarded() -> None:
    """模型不守协议在标记后继续生成时，那段文字不得推送给用户。"""
    gate = NoEvidencePrefixGate()

    assert gate.feed(f"{_MARKER}抱歉，我无法回答。") is None
    assert gate.feed("还有更多解释") is None
    assert gate.flush() == ""
    assert gate.is_refused is True


def test_divergent_prefix_opens_the_gate_and_stays_open() -> None:
    """首个字符就与标记分歧：整段缓冲一次性放行，后续增量原样通过。"""
    gate = NoEvidencePrefixGate()

    assert gate.feed("根据") == "根据"
    assert gate.feed("证据") == "证据"
    assert gate.is_refused is False


def test_marker_not_at_prefix_is_released_as_normal_text() -> None:
    """标记出现在正文中间不算声明——部分作答的保护来自位置，不来自字串。"""
    text = f"已涵盖的部分见 [1]。以下内容未涵盖：{_MARKER}无关"

    gate = NoEvidencePrefixGate()

    assert gate.feed(text) == text
    assert gate.is_refused is False


def test_incomplete_marker_at_stream_end_is_released_by_flush() -> None:
    """流在标记的真前缀上结束（如只输出了「【无」）：按普通文本放行，不误判拒答。"""
    gate = NoEvidencePrefixGate()

    assert gate.feed("【无") is None
    assert gate.is_refused is False
    assert gate.flush() == "【无"


def test_marker_missing_closing_bracket_diverges() -> None:
    """「【无据x」不是标记：分歧点之前缓冲，分歧后整段放行，不得吞掉。"""
    gate = NoEvidencePrefixGate()

    assert gate.feed("【无据") is None
    assert gate.feed("x】正文") == "【无据x】正文"
    assert gate.is_refused is False


def test_empty_buffer_and_empty_delta_are_harmless() -> None:
    """空增量不推进判定，也不产生输出。"""
    gate = NoEvidencePrefixGate()

    assert gate.feed("") is None
    assert gate.flush() == ""
    assert gate.is_refused is False


@pytest.mark.parametrize("marker", ["", pytest.param(None, id="None")])
def test_empty_marker_is_rejected(marker: str | None) -> None:
    """空标记会让任何回答都"匹配成功"，构造期必须拒绝。"""
    with pytest.raises(ValueError, match="不得为空"):
        NoEvidencePrefixGate(marker)  # type: ignore[arg-type]  # 故意传非法值


# ---------------------------------------------------------------------------
# 检索元数据降级
# ---------------------------------------------------------------------------
def test_downgrade_from_model_keeps_other_fields_and_does_not_mutate() -> None:
    """模型入参：只改 is_sufficient 与 reason，排障字段原样保留，原对象不变。"""
    original = _meta()

    downgraded = downgrade_retrieval_meta(original)

    assert downgraded is not None
    assert downgraded.is_sufficient is False
    assert downgraded.reason == REASON_MODEL_DECLARED_INSUFFICIENT
    assert downgraded.query == original.query
    assert downgraded.candidate_count == original.candidate_count
    assert downgraded.evidence_count == original.evidence_count
    assert downgraded.top_score == original.top_score
    assert downgraded.rerank_applied is True
    assert original.is_sufficient is True, "原元数据不得被原地改写（frozen 模型的复用约定）"


def test_downgrade_from_checkpoint_dict() -> None:
    """checkpoint 反序列化后的 dict 形态同样可降级。"""
    downgraded = downgrade_retrieval_meta(_meta().model_dump())

    assert downgraded is not None
    assert downgraded.is_sufficient is False
    assert downgraded.reason == REASON_MODEL_DECLARED_INSUFFICIENT


def test_downgrade_of_none_stays_none() -> None:
    """元数据缺失时保持缺失：拒答与"没有判定"是一致的。"""
    assert downgrade_retrieval_meta(None) is None


def test_downgrade_of_unparsable_value_returns_none_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """不可解析的脏数据不得炸节点：记 WARNING 后按缺失处理。"""
    with caplog.at_level("WARNING"):
        assert downgrade_retrieval_meta({"is_sufficient": "not-a-bool"}) is None

    assert any(record.message == "agent.retrieval_meta_unparsable" for record in caplog.records)
