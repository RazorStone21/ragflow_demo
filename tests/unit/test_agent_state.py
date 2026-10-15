"""State 契约：reducer 语义、状态访问器与初始状态完整性（CLAUDE.md 4.1）。"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_platform.agents.gov_policy_qa.state import (
    STATUS_PENDING,
    GovPolicyQaState,
    initial_state,
    latest_user_question,
    merge_evidence,
    message_text,
)
from agent_platform.knowledge.schemas import EvidenceItem


def _evidence(chunk_id: str, score: float, **overrides: Any) -> EvidenceItem:
    """构造一条证据。"""
    base: dict[str, Any] = {
        "chunk_id": chunk_id,
        "content": f"{chunk_id} 的正文",
        "score": score,
        "document_name": "政策.pdf",
        "page_numbers": [1],
    }
    base.update(overrides)
    return EvidenceItem(**base)


# ---------------------------------------------------------------------------
# merge_evidence（reducer）
# ---------------------------------------------------------------------------
def test_merge_evidence_dedupes_by_chunk_id_keeping_higher_score() -> None:
    """同一 chunk 重复出现时保留分数更高的一条（CLAUDE.md 4.1）。"""
    merged = merge_evidence([_evidence("c1", 0.4)], [_evidence("c1", 0.9)])
    assert len(merged) == 1
    assert merged[0].score == 0.9


def test_merge_evidence_keeps_existing_when_update_is_lower() -> None:
    """新分更低时不得覆盖旧证据（避免降级分支把好证据冲掉）。"""
    merged = merge_evidence([_evidence("c1", 0.9)], [_evidence("c1", 0.2)])
    assert merged[0].score == 0.9


def test_merge_evidence_preserves_order_and_appends_new() -> None:
    """顺序为"先已有、后新增"，便于稳定断言与展示。"""
    merged = merge_evidence([_evidence("c1", 0.5), _evidence("c2", 0.4)], [_evidence("c3", 0.3)])
    assert [item.chunk_id for item in merged] == ["c1", "c2", "c3"]


def test_merge_evidence_empty_update_is_noop() -> None:
    """空更新不清空已有证据（无证据分支不得误删证据）。"""
    current = [_evidence("c1", 0.7)]
    assert merge_evidence(current, []) == current


def test_merge_evidence_accepts_dict_payloads_from_checkpoint() -> None:
    """兼容 checkpoint 反序列化退回的原始字典（按契约校验后合并）。"""
    raw: dict[str, Any] = _evidence("c1", 0.8).model_dump()
    merged = merge_evidence([], [raw])  # type: ignore[list-item]  # 故意注入非模型形态
    assert isinstance(merged[0], EvidenceItem)
    assert merged[0].chunk_id == "c1"


def test_merge_evidence_rejects_invalid_dict_payload() -> None:
    """脏数据显式失败而不是带着残缺字段继续（NO-06）。"""
    from agent_platform.core.errors import AgentPlatformError

    with pytest.raises(AgentPlatformError, match="不符合契约"):
        merge_evidence([], [{"chunk_id": "c1"}])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# 访问器
# ---------------------------------------------------------------------------
def test_initial_state_is_complete_and_pending() -> None:
    """初始状态字段齐全（缺字段会在流式/路由处炸），状态为 pending。"""
    state = initial_state("新能源电价机制是什么？")
    assert set(state) == {
        "messages",
        "evidence",
        "retrieval_meta",
        "answer",
        "answer_status",
        "citations",
    }
    assert state["answer_status"] == STATUS_PENDING
    assert state["evidence"] == []
    assert state["retrieval_meta"] is None


def test_latest_user_question_returns_last_human_message() -> None:
    """取最后一条用户消息（多轮时以最新问题为准）。"""
    state: GovPolicyQaState = {
        "messages": [
            HumanMessage(content="第一个问题"),
            AIMessage(content="第一个回答"),
            HumanMessage(content="  第二个问题  "),
        ]
    }
    assert latest_user_question(state) == "第二个问题"


def test_latest_user_question_ignores_ai_messages_only() -> None:
    """只有模型消息时返回空串（由调用方判定为装配错误）。"""
    assert latest_user_question({"messages": [AIMessage(content="只有回答")]}) == ""


def test_latest_user_question_handles_empty_state() -> None:
    """状态缺 messages 键时返回空串而不抛 KeyError。"""
    assert latest_user_question({}) == ""


def test_message_text_keeps_whitespace_for_streaming() -> None:
    """文本提取不裁剪首尾空白：流式增量必须逐字拼接，否则会吞掉分块空格。"""
    from langchain_core.messages import AIMessageChunk

    assert message_text(AIMessageChunk(content=" 结论 [1] ")) == " 结论 [1] "


def test_message_text_joins_content_blocks() -> None:
    """内容块列表形态（多模态消息）拼接文本块。"""
    message = HumanMessage(content=[{"type": "text", "text": "第一段"}, "第二段"])
    assert message_text(message) == "第一段 第二段"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param("纯文本内容", "纯文本内容", id="str-content-as-is"),
        pytest.param(
            [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}],
            "第一段 第二段",
            id="dict-blocks-with-str-text",
        ),
        pytest.param(
            [{"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}}],
            "",
            id="dict-block-without-text-key-skipped",
        ),
        pytest.param([{"text": None}], "", id="dict-block-with-none-text-skipped"),
        pytest.param(
            [
                {"type": "text", "text": "第一段"},
                {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}},
                "第二段",
            ],
            "第一段 第二段",
            id="mixed-blocks-skip-non-text",
        ),
    ],
)
def test_message_text_extracts_only_str_text_blocks(
    content: str | list[str | dict[str, Any]], expected: str
) -> None:
    """内容块列表只收集文本：无 str ``text`` 的 dict 块（图片等）被跳过而非报错，文本块以空格拼接。"""
    assert message_text(HumanMessage(content=content)) == expected
