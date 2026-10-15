"""条件路由：确定性纯函数，任何"看不清"的情形一律保守降级（CLAUDE.md 4.3 / NO-12）。"""

from __future__ import annotations

import copy
from typing import cast

import pytest

from agent_platform.agents.gov_policy_qa.edges import route_after_retrieve
from agent_platform.agents.gov_policy_qa.state import (
    NODE_ANSWER_WITHOUT_EVIDENCE,
    NODE_GENERATE_ANSWER,
    GovPolicyQaState,
)
from agent_platform.knowledge.schemas import EvidenceItem, RetrievalMeta

# 图（graph.py）通过 add_conditional_edges 实际注册的两个分支；断言只引用这两个常量
_REGISTERED_BRANCHES: tuple[str, str] = (NODE_GENERATE_ANSWER, NODE_ANSWER_WITHOUT_EVIDENCE)


def _evidence(**overrides: object) -> EvidenceItem:
    """构造一条默认合格的检索证据（字段可覆盖）。"""
    base: dict[str, object] = {
        "chunk_id": "chunk-001",
        "content": "《中华人民共和国能源法》第四十二条节选。",
        "score": 0.92,
    }
    base.update(overrides)
    return EvidenceItem.model_validate(base)


def _meta(
    *,
    is_sufficient: bool = True,
    reason: str = "证据充足",
    query: str = "分布式光伏补贴政策",
    candidate_count: int = 8,
    evidence_count: int = 2,
    top_score: float | None = 0.92,
    score_threshold: float = 0.5,
    rerank_applied: bool = True,
) -> RetrievalMeta:
    """构造一份默认判定为"充足"的检索元数据（字段可覆盖）。"""
    return RetrievalMeta(
        query=query,
        candidate_count=candidate_count,
        evidence_count=evidence_count,
        top_score=top_score,
        score_threshold=score_threshold,
        is_sufficient=is_sufficient,
        reason=reason,
        rerank_applied=rerank_applied,
    )


def _route(state: dict[str, object]) -> str:
    """以任意形态的状态字典调用路由，并断言返回值是图已注册的分支之一。"""
    result = route_after_retrieve(cast(GovPolicyQaState, state))
    assert result in _REGISTERED_BRANCHES  # 任何输入都不得落到未注册节点（CLAUDE.md 4.3）
    return result


def test_sufficient_evidence_routes_to_generate_answer() -> None:
    """证据非空且 is_sufficient=True 时路由到 generate_answer 分支。"""
    state: dict[str, object] = {
        "evidence": [_evidence(), _evidence(chunk_id="chunk-002", score=0.71)],
        "retrieval_meta": _meta(is_sufficient=True, reason="证据充足"),
    }

    assert _route(state) == NODE_GENERATE_ANSWER


def test_insufficient_meta_routes_to_degraded_branch() -> None:
    """is_sufficient=False 时即使证据非空也必须走 answer_without_evidence（NO-12）。"""
    state: dict[str, object] = {
        "evidence": [_evidence()],
        "retrieval_meta": _meta(is_sufficient=False, reason="低于重排阈值"),
    }

    assert _route(state) == NODE_ANSWER_WITHOUT_EVIDENCE


def test_empty_evidence_overrides_optimistic_meta() -> None:
    """证据为空而 meta 声称充足时，矛盾以"无证据"为准并保守降级（NO-12）。"""
    state: dict[str, object] = {"evidence": [], "retrieval_meta": _meta(is_sufficient=True)}

    assert _route(state) == NODE_ANSWER_WITHOUT_EVIDENCE


@pytest.mark.parametrize(
    "state",
    [
        pytest.param({"evidence": [_evidence()]}, id="缺 retrieval_meta 键"),
        pytest.param(
            {"evidence": [_evidence()], "retrieval_meta": None}, id="retrieval_meta 为 None"
        ),
    ],
)
def test_missing_or_none_meta_degrades(state: dict[str, object]) -> None:
    """元数据缺失（缺键或 None）时必须降级，不得默认按充足处理（NO-12）。"""
    assert _route(state) == NODE_ANSWER_WITHOUT_EVIDENCE


def test_dict_form_meta_after_checkpoint_deserialization() -> None:
    """checkpoint 反序列化退回 dict 形态的元数据仍能正确判定走向（断点恢复后可路由）。"""
    raw_meta = _meta().model_dump(mode="json")
    assert isinstance(raw_meta, dict)

    assert _route({"evidence": [_evidence()], "retrieval_meta": raw_meta}) == NODE_GENERATE_ANSWER

    insufficient = _meta(is_sufficient=False, reason="证据不足").model_dump(mode="json")
    assert (
        _route({"evidence": [_evidence()], "retrieval_meta": insufficient})
        == NODE_ANSWER_WITHOUT_EVIDENCE
    )


@pytest.mark.parametrize(
    "malformed_meta",
    [
        pytest.param("不是元数据", id="字符串"),
        pytest.param(1, id="数字"),
        pytest.param(True, id="布尔"),
        pytest.param([], id="空列表"),
        pytest.param(["is_sufficient"], id="字符串列表"),
        pytest.param({"query": "缺字段"}, id="缺必填字段的字典"),
        pytest.param(
            {
                "query": "q",
                "candidate_count": 1,
                "evidence_count": 1,
                "score_threshold": 0.5,
                "is_sufficient": "也许",
                "reason": "r",
            },
            id="布尔字段为非合法值",
        ),
        pytest.param(
            {
                "query": "q",
                "candidate_count": -1,
                "evidence_count": 0,
                "score_threshold": 0.5,
                "is_sufficient": True,
                "reason": "r",
            },
            id="数值违反下界约束",
        ),
    ],
)
def test_malformed_meta_degrades_without_raising(malformed_meta: object) -> None:
    """残缺 dict 与垃圾类型的元数据一律降级且不得抛异常（保守优先，NO-12）。"""
    state: dict[str, object] = {"evidence": [_evidence()], "retrieval_meta": malformed_meta}

    assert _route(state) == NODE_ANSWER_WITHOUT_EVIDENCE


def test_empty_state_degrades() -> None:
    """空状态 {} 不含任何证据线索，必须降级（NO-12）。"""
    assert _route({}) == NODE_ANSWER_WITHOUT_EVIDENCE


def test_route_is_deterministic_and_does_not_mutate_state() -> None:
    """同一状态连续调用结果一致，且路由不得原地修改入参 State（CLAUDE.md 4.2/4.3）。"""
    state: dict[str, object] = {
        "evidence": [_evidence()],
        "retrieval_meta": _meta(),
    }
    snapshot = copy.deepcopy(state)

    results = [_route(state) for _ in range(3)]

    assert results == [NODE_GENERATE_ANSWER] * 3
    assert _route(copy.deepcopy(state)) == results[0]  # 等值的新对象也得到相同走向
    assert state == snapshot  # 入参未被原地修改


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param({}, NODE_ANSWER_WITHOUT_EVIDENCE, id="空状态"),
        pytest.param(
            {"evidence": [], "retrieval_meta": None},
            NODE_ANSWER_WITHOUT_EVIDENCE,
            id="证据为空且元数据为 None",
        ),
        pytest.param(
            {"evidence": [_evidence()], "retrieval_meta": None},
            NODE_ANSWER_WITHOUT_EVIDENCE,
            id="证据非空但元数据为 None",
        ),
        pytest.param(
            {"evidence": "不是列表", "retrieval_meta": "垃圾"},
            NODE_ANSWER_WITHOUT_EVIDENCE,
            id="两个字段都是垃圾类型",
        ),
        pytest.param(
            {"evidence": [{"chunk_id": "c1"}], "retrieval_meta": {"query": "q"}},
            NODE_ANSWER_WITHOUT_EVIDENCE,
            id="两个字段均为残缺字典",
        ),
        pytest.param(
            {"retrieval_meta": _meta()},
            NODE_ANSWER_WITHOUT_EVIDENCE,
            id="缺 evidence 键",
        ),
        pytest.param(
            {"evidence": [_evidence()], "retrieval_meta": _meta(is_sufficient=False)},
            NODE_ANSWER_WITHOUT_EVIDENCE,
            id="显式判定证据不足",
        ),
        pytest.param(
            {"evidence": [], "retrieval_meta": _meta(is_sufficient=True)},
            NODE_ANSWER_WITHOUT_EVIDENCE,
            id="证据为空而元数据乐观",
        ),
        pytest.param(
            {
                "evidence": [{"chunk_id": "c1", "content": "证据原文", "score": 0.9}],
                "retrieval_meta": _meta().model_dump(mode="json"),
            },
            NODE_GENERATE_ANSWER,
            id="两字段均为反序列化字典",
        ),
    ],
)
def test_any_input_returns_registered_branch(state: dict[str, object], expected: str) -> None:
    """无论输入多畸形，返回值都落在图真实注册的两个分支之一，且走向符合保守预期（4.3）。"""
    assert NODE_GENERATE_ANSWER != NODE_ANSWER_WITHOUT_EVIDENCE  # 两个分支必须可区分

    assert _route(state) == expected
