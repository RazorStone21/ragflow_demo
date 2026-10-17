"""GovPolicyQa 端到端装配测试：拓扑、双分支、自定义流、checkpoint 与异常传播（CLAUDE.md 4.3 / 4.4 / 4.7 / 4.8 / NO-06 / NO-12）。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from agent_platform.agents.gov_policy_qa.edges import route_after_retrieve
from agent_platform.agents.gov_policy_qa.graph import build_graph
from agent_platform.agents.gov_policy_qa.nodes.answer_without_evidence import (
    _BASE_ANSWER,
    _REASON_HINTS,
)
from agent_platform.agents.gov_policy_qa.nodes.generate_answer import (
    _BLOCKED_ANSWER,
    _MODEL_NO_EVIDENCE_ANSWER,
)
from agent_platform.agents.gov_policy_qa.nodes.result import REASON_MODEL_DECLARED_INSUFFICIENT
from agent_platform.agents.gov_policy_qa.prompts import ANSWER_NO_EVIDENCE_MARKER
from agent_platform.agents.gov_policy_qa.state import (
    NODE_ANSWER_WITHOUT_EVIDENCE,
    NODE_GENERATE_ANSWER,
    NODE_RETRIEVE_EVIDENCE,
    STATUS_ANSWERED,
    STATUS_GUARDRAIL_BLOCKED,
    STATUS_INSUFFICIENT_EVIDENCE,
    STATUS_PENDING,
    GovPolicyQaState,
    initial_state,
)
from agent_platform.core.errors import (
    ModelGatewayError,
    RetrievalFailedError,
    UnauthorizedError,
)
from agent_platform.core.streaming import SseEventType
from agent_platform.guardrails import Guardrail, GuardrailDecision, PassThroughGuardrail
from agent_platform.knowledge.retriever import REASON_NO_CANDIDATES, REASON_SUFFICIENT
from agent_platform.knowledge.schemas import EvidenceItem, RetrievalMeta, RetrievalOutcome

_QUESTION = "新能源电价机制如何确定？"


class FakeRetriever:
    """确定性检索器：按调用顺序返回预设结果或抛异常，记录 ``(问题, 租户)`` 入参。"""

    def __init__(
        self,
        outcome: RetrievalOutcome,
        *more: RetrievalOutcome,
        raises: BaseException | None = None,
    ) -> None:
        self.outcomes = [outcome, *more]
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    async def search(
        self, query: str, *, tenant_id: str, dataset_id: str | None = None
    ) -> RetrievalOutcome:
        """消费预设结果（仅最后一个可被重复使用）；预设异常时抛出。"""
        del dataset_id
        self.calls.append((query, tenant_id))
        if self.raises is not None:
            raise self.raises
        if len(self.outcomes) > 1:
            return self.outcomes.pop(0)
        return self.outcomes[0]


class FakeChatModel:
    """确定性聊天模型：流式逐块产出 ``AIMessageChunk``，可预设异常；记录入参。"""

    def __init__(
        self,
        chunks: Sequence[str] = ("依据", "《电价办法》第3条", "[1]。"),
        *,
        raises: BaseException | None = None,
    ) -> None:
        self.chunks = list(chunks)
        self.raises = raises
        self.stream_calls: list[list[BaseMessage]] = []

    async def ainvoke(
        self,
        input: Any,  # noqa: A002  # 与 core.models.ChatModel 协议签名保持一致
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        """非流式调用（图只走 ``astream``；保持协议完整并同样支持预设异常）。"""
        del config, kwargs
        if self.raises is not None:
            raise self.raises
        return AIMessage(content="".join(self.chunks))

    def astream(
        self,
        input: Any,  # noqa: A002  # 同上：与 core.models.ChatModel 协议签名保持一致
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AIMessageChunk]:
        """流式调用：记录入参后按 ``chunks`` 逐块产出（或抛出预设异常）。"""
        del config, kwargs
        self.stream_calls.append(list(input))
        return self._stream()

    async def _stream(self) -> AsyncIterator[AIMessageChunk]:
        """产出增量；预设异常从流中冒泡（不得被吞，NO-06）。"""
        if self.raises is not None:
            raise self.raises
        for text in self.chunks:
            yield AIMessageChunk(content=text)


class BlockingGuardrail(PassThroughGuardrail):
    """记录型拦截护栏：``check_output`` 恒返回拦截判定，并记录 ``(被检文本, 租户)``。

    用于锁定端到端装配契约（CLAUDE.md 4.8 / 10.3）：图必须把注入的护栏真正接到
    ``generate_answer`` 节点上，且拦截判定必须改写最终答案——两者任一失效即断言失败。
    """

    def __init__(self, reason: str = "演示用拦截规则命中") -> None:
        self.reason = reason
        self.output_checks: list[tuple[str, str]] = []

    async def check_output(self, *, text: str, tenant_id: str) -> GuardrailDecision:
        """记录本次输出检查入参，返回拦截判定（code 为 ``GUARDRAIL_BLOCKED``）。"""
        self.output_checks.append((text, tenant_id))
        return GuardrailDecision.block(self.reason)


def _evidence(chunk_id: str, score: float = 0.9) -> EvidenceItem:
    """构造一条带完整引用信息的证据（CLAUDE.md 7.4）。"""
    return EvidenceItem(
        chunk_id=chunk_id,
        content=f"第 {chunk_id} 段：居民电价按年度核定并公示。",
        score=score,
        document_name="新能源电价政策.pdf",
        page_numbers=[3],
        source_uri="gov_doc/新能源电价政策.pdf",
    )


def _sufficient_outcome(*chunk_ids: str) -> RetrievalOutcome:
    """构造"证据充足"的检索结果（锚定路由的 ``generate_answer`` 分支）。"""
    evidence = [_evidence(chunk_id) for chunk_id in (chunk_ids or ("c1",))]
    return RetrievalOutcome(
        evidence=evidence,
        meta=RetrievalMeta(
            query=_QUESTION,
            candidate_count=len(evidence),
            evidence_count=len(evidence),
            top_score=evidence[0].score,
            score_threshold=0.3,
            is_sufficient=True,
            reason=REASON_SUFFICIENT,
            rerank_applied=False,
        ),
    )


def _insufficient_outcome() -> RetrievalOutcome:
    """构造"无证据"的检索结果（锚定路由的降级分支，非异常，NO-12）。"""
    return RetrievalOutcome(
        evidence=[],
        meta=RetrievalMeta(
            query=_QUESTION,
            candidate_count=0,
            evidence_count=0,
            top_score=None,
            score_threshold=0.3,
            is_sufficient=False,
            reason=REASON_NO_CANDIDATES,
            rerank_applied=False,
        ),
    )


def _config(thread_id: str, *, tenant_id: str | None = "tenant_a") -> RunnableConfig:
    """构造运行配置：thread_id 寻址会话，tenant_id 模拟认证上下文（CLAUDE.md 4.4 / NO-19）。"""
    configurable: dict[str, Any] = {"thread_id": thread_id}
    if tenant_id is not None:
        configurable["tenant_id"] = tenant_id
    return {"configurable": configurable}


def _build(
    retriever: FakeRetriever | None = None,
    chat: FakeChatModel | None = None,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    guardrail: Guardrail | None = None,
) -> tuple[CompiledStateGraph[Any, Any, Any, Any], FakeRetriever, FakeChatModel]:
    """装配图：注入 fake 依赖；checkpointer 默认为内存实现（仅限单测，NO-08）。"""
    fake_retriever = retriever or FakeRetriever(_sufficient_outcome())
    fake_chat = chat or FakeChatModel()
    graph = build_graph(
        retriever=fake_retriever,  # type: ignore[arg-type]  # 注入 fake（CLAUDE.md 9.3）
        chat=fake_chat,
        chat_logical_name="primary-chat",
        checkpointer=checkpointer or InMemorySaver(),
        guardrail=guardrail,
    )
    return graph, fake_retriever, fake_chat


async def _collect_custom_chunks(
    graph: CompiledStateGraph[Any, Any, Any, Any], question: str, config: RunnableConfig
) -> list[dict[str, Any]]:
    """收集一次 run 的自定义流事件（形态 = ``AgentStreamEvent.to_chunk()``）。"""
    return [
        chunk
        async for chunk in graph.astream(initial_state(question), config, stream_mode="custom")
    ]


def test_graph_topology_has_explicit_branches() -> None:
    """拓扑显式声明两条分支且终点均连 END，无悬挂路径（CLAUDE.md 4.3）。"""
    graph, _, _ = _build()
    drawable = graph.get_graph()

    assert {NODE_RETRIEVE_EVIDENCE, NODE_GENERATE_ANSWER, NODE_ANSWER_WITHOUT_EVIDENCE} <= set(
        drawable.nodes
    )
    edges = {(edge.source, edge.target) for edge in drawable.edges}
    assert (START, NODE_RETRIEVE_EVIDENCE) in edges
    branch_targets = {
        edge.target
        for edge in drawable.edges
        if edge.source == NODE_RETRIEVE_EVIDENCE and edge.conditional
    }
    assert branch_targets == {NODE_GENERATE_ANSWER, NODE_ANSWER_WITHOUT_EVIDENCE}
    assert {target for source, target in edges if source == NODE_GENERATE_ANSWER} == {END}
    assert {target for source, target in edges if source == NODE_ANSWER_WITHOUT_EVIDENCE} == {END}
    assert not any(source == END for source, _ in edges)  # END 为终点，不允许有出边


async def test_evidence_path_answers_with_citations() -> None:
    """证据充足时走生成分支：answered、引用非空且末条消息与 ``answer`` 一致（NO-12）。"""
    graph, retriever, chat = _build(
        FakeRetriever(_sufficient_outcome("c1", "c2")),
        FakeChatModel(("依据", "第3条", "[1]。")),
    )
    state = await graph.ainvoke(initial_state(_QUESTION), _config("t-answered"))

    assert retriever.calls == [(_QUESTION, "tenant_a")]
    assert [item.chunk_id for item in state["evidence"]] == ["c1", "c2"]
    assert state["answer_status"] == STATUS_ANSWERED
    assert state["answer"] == "依据第3条[1]。"
    assert [citation.chunk_id for citation in state["citations"]] == ["c1"]
    assert state["citations"][0].index == 1
    assert len(chat.stream_calls) == 1
    last = state["messages"][-1]
    assert isinstance(last, AIMessage)
    assert last.content == state["answer"]


async def test_output_guardrail_block_replaces_answer_verbatim() -> None:
    """输出护栏拦截必须落到 guardrail_blocked 状态与固定拦截文案，模型原稿不得泄漏给用户（CLAUDE.md 4.8 / 10.3）。"""
    chunks = ("依据", "《电价办法》第3条", "[1]。")
    draft = "".join(chunks)
    guardrail = BlockingGuardrail()
    graph, _, _ = _build(
        FakeRetriever(_sufficient_outcome("c1")),
        FakeChatModel(chunks),
        guardrail=guardrail,
    )
    state = await graph.ainvoke(initial_state(_QUESTION), _config("t-guardrail-blocked"))

    assert state["answer_status"] == STATUS_GUARDRAIL_BLOCKED
    assert state["answer"] == _BLOCKED_ANSWER  # 逐字绑定：拦截后用户只能拿到固定拦截文案
    # 护栏必须被真实调用且入参为认证租户与被检原稿：build_graph 漏传 guardrail 的静默绕过即红
    assert guardrail.output_checks == [(draft, "tenant_a")]


async def test_degraded_path_never_calls_model() -> None:
    """证据不足时走降级分支：insufficient_evidence 且全链路不调用模型（NO-12）。"""
    graph, _, chat = _build(
        FakeRetriever(_insufficient_outcome()), FakeChatModel(("不该出现的答案",))
    )
    state = await graph.ainvoke(initial_state(_QUESTION), _config("t-degraded"))

    assert state["answer_status"] == STATUS_INSUFFICIENT_EVIDENCE
    assert state["evidence"] == []
    assert state["citations"] == []
    # 逐字绑定确定性拒答文案：基座文案 + REASON_NO_CANDIDATES 对应的下一步提示（NO-12）
    assert state["answer"] == f"{_BASE_ANSWER}\n\n{_REASON_HINTS[REASON_NO_CANDIDATES]}"
    assert chat.stream_calls == []


async def test_custom_stream_emits_evidence_then_deltas() -> None:
    """充足路径的流：先 retrieval.evidence 后若干 message.delta，拼接等于最终答案（CLAUDE.md 4.7）。"""
    chunks = ("依据", "《电价办法》第3条", "[1]。")
    graph, _, _ = _build(FakeRetriever(_sufficient_outcome("c1")), FakeChatModel(chunks))
    config = _config("t-stream")

    streamed = await _collect_custom_chunks(graph, _QUESTION, config)
    events = [chunk["event"] for chunk in streamed]

    assert events[0] == SseEventType.RETRIEVAL_EVIDENCE.value
    assert events[1:] == [SseEventType.MESSAGE_DELTA.value] * len(chunks)
    assert [item["chunk_id"] for item in streamed[0]["data"]["evidence"]] == ["c1"]
    assert streamed[0]["data"]["meta"]["is_sufficient"] is True
    deltas = [chunk["data"]["delta"] for chunk in streamed[1:]]
    assert deltas == list(chunks)

    snapshot = await graph.aget_state(config)
    assert "".join(deltas) == snapshot.values["answer"]
    invoked = await graph.ainvoke(initial_state(_QUESTION), _config("t-invoke"))
    assert invoked["answer"] == snapshot.values["answer"]
    assert invoked["answer_status"] == snapshot.values["answer_status"] == STATUS_ANSWERED


async def test_model_declared_no_evidence_streams_no_delta() -> None:
    """证据充足但模型读完声明无据：走生成节点却零 message.delta，状态落拒答（NO-12）。

    这不是"检索没找到"的降级分支——检索确实给了证据；是生成侧推翻检索判断。
    事件序列里只有 retrieval.evidence，用户不会先看到草稿再被撤回。
    """
    graph, _, chat = _build(
        FakeRetriever(_sufficient_outcome("c1")),
        FakeChatModel((ANSWER_NO_EVIDENCE_MARKER,)),
    )
    config = _config("t-model-refused")

    streamed = await _collect_custom_chunks(graph, _QUESTION, config)

    assert [chunk["event"] for chunk in streamed] == [SseEventType.RETRIEVAL_EVIDENCE.value]
    assert len(chat.stream_calls) == 1
    snapshot = await graph.aget_state(config)
    assert snapshot.values["answer_status"] == STATUS_INSUFFICIENT_EVIDENCE
    assert snapshot.values["answer"] == _MODEL_NO_EVIDENCE_ANSWER
    assert snapshot.values["citations"] == []
    meta = RetrievalMeta.model_validate(snapshot.values["retrieval_meta"])
    assert meta.is_sufficient is False
    assert meta.reason == REASON_MODEL_DECLARED_INSUFFICIENT
    assert meta.evidence_count == 1, "排障字段必须保留，只改判定与理由"


async def test_custom_stream_has_no_delta_on_degraded_path() -> None:
    """降级路径的流中不得出现 message.delta（该分支不调用模型，CLAUDE.md 4.7 / NO-12）。"""
    graph, _, _ = _build(FakeRetriever(_insufficient_outcome()), FakeChatModel(("不该出现",)))
    streamed = await _collect_custom_chunks(graph, _QUESTION, _config("t-stream-degraded"))

    assert [chunk["event"] for chunk in streamed] == [SseEventType.RETRIEVAL_EVIDENCE.value]


async def test_missing_tenant_fails_closed_before_retrieval() -> None:
    """config 缺 tenant_id 时以 401 失败且不发起检索，不得回退默认租户（NO-19）。"""
    graph, retriever, _ = _build()
    with pytest.raises(UnauthorizedError) as excinfo:
        await graph.ainvoke(initial_state(_QUESTION), _config("t-no-tenant", tenant_id=None))

    assert excinfo.value.http_status == 401
    assert retriever.calls == []


async def test_checkpoint_roundtrip_preserves_evidence_and_route() -> None:
    """同一 thread 重复 invoke 后，checkpoint 中的证据仍可还原且路由判定可用（CLAUDE.md 4.1 / 4.4）。"""
    graph, _, _ = _build(FakeRetriever(_sufficient_outcome("c1", "c2")))
    config = _config("t-roundtrip")
    await graph.ainvoke(initial_state(_QUESTION), config)
    await graph.ainvoke(initial_state(_QUESTION), config)

    snapshot = await graph.aget_state(config)
    restored = [EvidenceItem.model_validate(raw) for raw in snapshot.values["evidence"]]
    # 证据 reducer 按 chunk_id 去重：两轮检索同一批 chunk 不重复累积
    assert [item.chunk_id for item in restored] == ["c1", "c2"]
    meta = RetrievalMeta.model_validate(snapshot.values["retrieval_meta"])
    assert meta.is_sufficient is True
    # aget_state 的 values 静态类型为 dict[str, Any]，cast 到 State 类型以复用真实路由函数
    assert route_after_retrieve(cast(GovPolicyQaState, snapshot.values)) == NODE_GENERATE_ANSWER
    # messages 用 add_messages reducer 跨轮追加：两轮各 1 问 1 答
    assert len(snapshot.values["messages"]) == 4


async def test_model_gateway_error_propagates_and_state_stays_unanswered() -> None:
    """模型网关异常必须冒泡，且最终状态不得被伪装成 answered（CLAUDE.md 3.6 / NO-06）。"""
    graph, _, chat = _build(
        FakeRetriever(_sufficient_outcome("c1")),
        FakeChatModel(raises=ModelGatewayError("网关不可用")),
    )
    config = _config("t-gateway-error")

    with pytest.raises(ModelGatewayError):
        await graph.ainvoke(initial_state(_QUESTION), config)

    assert len(chat.stream_calls) == 1
    snapshot = await graph.aget_state(config)
    assert snapshot.values["answer_status"] == STATUS_PENDING  # 无节点改写该字段
    assert snapshot.values["answer"] == ""
    assert NODE_GENERATE_ANSWER in snapshot.next  # run 停在中途，未到达 END


async def test_retriever_infrastructure_failure_propagates() -> None:
    """向量库故障属基础设施异常，不得被吞成"无证据"降级作答（CLAUDE.md 3.6 / NO-06）。"""
    graph, _, chat = _build(
        FakeRetriever(_sufficient_outcome("c1"), raises=RetrievalFailedError("向量库不可用")),
        FakeChatModel(("不该出现",)),
    )
    with pytest.raises(RetrievalFailedError):
        await graph.ainvoke(initial_state(_QUESTION), _config("t-retriever-down"))

    assert chat.stream_calls == []


async def test_threads_do_not_share_state() -> None:
    """不同 thread_id 的状态互不串扰，各自独立持久化判定与对话（CLAUDE.md 4.4 / 10.2）。"""
    retriever = FakeRetriever(_sufficient_outcome("c1"), _insufficient_outcome())
    graph, _, _ = _build(retriever)
    config_a = _config("t-iso-a")
    config_b = _config("t-iso-b")

    state_a = await graph.ainvoke(initial_state("问题A"), config_a)
    state_b = await graph.ainvoke(initial_state("问题B"), config_b)

    assert retriever.calls == [("问题A", "tenant_a"), ("问题B", "tenant_a")]
    assert state_a["answer_status"] == STATUS_ANSWERED
    assert state_b["answer_status"] == STATUS_INSUFFICIENT_EVIDENCE
    snapshot_a = await graph.aget_state(config_a)
    snapshot_b = await graph.aget_state(config_b)
    assert [message.content for message in snapshot_a.values["messages"]] == [
        "问题A",
        state_a["answer"],
    ]
    assert [message.content for message in snapshot_b.values["messages"]] == [
        "问题B",
        state_b["answer"],
    ]
    assert [item.chunk_id for item in snapshot_a.values["evidence"]] == ["c1"]
    assert snapshot_b.values["evidence"] == []
