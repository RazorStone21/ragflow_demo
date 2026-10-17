"""SSE 流式契约测试（CLAUDE.md 4.7 / 8.4 / 9.3）。

本文件是"线格式"与"事件序列"的唯一权威断言点。三处断言刻意做得很硬
（字节级、序号单调、事件全集），因为它们各自对应一个**静默**的失效方式：

1. 帧若被 ``sse_starlette`` 当作 ``str`` 重新包装，每行会多一层 ``data: `` 前缀。
   客户端不会报错，只会解析出乱码——所以直接断言字节。
2. ``stream_mode`` 传元组或裸串会退化成裸 payload，mode 判别符丢失，
   所有事件被归错类（节点进入被当成退出）。所以断言完整事件序列，而不是"出现了 run.completed"。
3. 流内异常若逃出生成器，Starlette 抛
   ``Caught handled exception, but response already started.``，客户端只看到连接被掐断。
   所以断言 ``run.error`` 帧确实到达。
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest

from agent_platform.agents.gov_policy_qa.nodes.result import REASON_MODEL_DECLARED_INSUFFICIENT
from agent_platform.agents.gov_policy_qa.prompts import ANSWER_NO_EVIDENCE_MARKER
from agent_platform.agents.gov_policy_qa.state import AnswerStatus
from agent_platform.core.errors import ErrorCode, ModelGatewayError, RetrievalFailedError
from agent_platform.core.streaming import SseEventType
from tests.conftest import (
    TEST_QUESTION,
    FakeChatModel,
    FakeRetriever,
    insufficient_outcome,
    sufficient_outcome,
)

if TYPE_CHECKING:
    from agent_platform.settings import Settings
    from tests.conftest import ApiHarness, OpenApi

PING = ": ping"
"""心跳帧（注释帧，无 event/data）。"""


def parse_sse(body: bytes) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 响应体解析为 ``[(event_type, payload), ...]``，心跳记为 ``(": ping", {})``。

    Raises:
        AssertionError: 帧结构非法（这会直接让"格式错乱"以失败暴露，而不是静默跳过）。
    """
    text = body.decode("utf-8")
    assert "\r" not in text, "帧必须用 \\n 分隔，不得出现 CR"
    parsed: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        if not block:
            continue
        if block.startswith(":"):
            assert block == PING, f"未知注释帧: {block!r}"
            parsed.append((PING, {}))
            continue
        event_type: str | None = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                assert event_type is None, f"单帧出现多个 event 行: {block!r}"
                event_type = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
            else:
                raise AssertionError(f"非法 SSE 行: {line!r}")
        assert event_type is not None, f"帧缺少 event 行: {block!r}"
        # 恰好一行 data：多行说明内容被当作 str 二次包装（每行又加了一层 data: 前缀）
        assert len(data_lines) == 1, f"帧 {event_type} 的 data 行数为 {len(data_lines)}，期望 1"
        parsed.append((event_type, json.loads(data_lines[0])))
    return parsed


def event_types(parsed: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """取出事件类型序列（含心跳占位）。"""
    return [event_type for event_type, _ in parsed]


def payloads_of(parsed: list[tuple[str, dict[str, Any]]], event_type: str) -> list[dict[str, Any]]:
    """取出某类事件的全部**负载**（即 ``payload`` 字段的内容）。"""
    return [event["payload"] for name, event in parsed if name == event_type]


def node_names(parsed: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, str]]:
    """取出 ``(事件类型, 节点名)`` 序列，便于断言节点进出时序。"""
    return [
        (name, event["payload"]["node"])
        for name, event in parsed
        if name in {SseEventType.NODE_ENTERED.value, SseEventType.NODE_EXITED.value}
    ]


async def stream_run(
    api: ApiHarness,
    thread_id: str,
    *,
    message: str = TEST_QUESTION,
    headers: dict[str, str] | None = None,
) -> tuple[Any, bytes]:
    """发起一次流式运行并返回 ``(响应, 完整响应体)``。"""
    async with api.client.stream(
        "POST",
        f"/api/v1/threads/{thread_id}/runs/stream",
        headers={**api.auth_headers, **(headers or {})},
        json={"agent": api.stack.agent_name, "message": message},
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
    return response, body


async def test_sse_headers_and_content_type(api: ApiHarness) -> None:
    """流式响应必须声明 text/event-stream 并关闭各级缓冲（CLAUDE.md 8.4）。"""
    thread_id = await api.create_thread()

    response, _ = await stream_run(api, thread_id)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    # 关掉反向代理缓冲，否则 SSE 会被攒成一次性响应
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


async def test_frames_are_bytes_not_double_wrapped(api: ApiHarness) -> None:
    """帧必须逐字节正确：不得出现二次包装造成的 ``data: data:``（CLAUDE.md 8.4）。"""
    thread_id = await api.create_thread()

    _, body = await stream_run(api, thread_id)

    assert b"data: data:" not in body
    assert b"event: event:" not in body
    # 每个事件块的 data 行都必须是**合法 JSON**（上面 parse_sse 已强制）
    assert parse_sse(body)


async def test_sufficient_evidence_event_sequence_is_complete(api: ApiHarness) -> None:
    """有证据路径的完整事件序列（CLAUDE.md 4.7 / 8.4 契约）。"""
    thread_id = await api.create_thread()

    _, body = await stream_run(api, thread_id)

    assert event_types(parse_sse(body)) == [
        SseEventType.RUN_STARTED.value,
        SseEventType.NODE_ENTERED.value,
        SseEventType.RETRIEVAL_EVIDENCE.value,
        SseEventType.NODE_EXITED.value,
        SseEventType.NODE_ENTERED.value,
        SseEventType.MESSAGE_DELTA.value,
        SseEventType.NODE_EXITED.value,
        SseEventType.RUN_COMPLETED.value,
    ]


async def test_nodes_enter_and_exit_in_the_right_order(api: ApiHarness) -> None:
    """节点事件必须成对且顺序正确：检索先于生成（CLAUDE.md 4.3）。

    这一条专门锁 ``tasks`` 流模式：LangGraph 里 ``updates`` 只在节点**完成后**发，
    用它做"进入"事件会让 node.entered 与 node.exited 双双滞后一步。
    """
    thread_id = await api.create_thread()

    _, body = await stream_run(api, thread_id)

    assert node_names(parse_sse(body)) == [
        (SseEventType.NODE_ENTERED.value, "retrieve_evidence"),
        (SseEventType.NODE_EXITED.value, "retrieve_evidence"),
        (SseEventType.NODE_ENTERED.value, "generate_answer"),
        (SseEventType.NODE_EXITED.value, "generate_answer"),
    ]


async def test_no_evidence_path_skips_model_events(api_factory: OpenApi) -> None:
    """无证据路径：不发 message.delta、直接走降级节点（NO-12）。"""
    chat = FakeChatModel()
    async with api_factory(retriever=FakeRetriever(insufficient_outcome()), chat=chat) as harness:
        thread_id = await harness.create_thread()
        _, body = await stream_run(harness, thread_id)

    assert event_types(parse_sse(body)) == [
        SseEventType.RUN_STARTED.value,
        SseEventType.NODE_ENTERED.value,
        SseEventType.RETRIEVAL_EVIDENCE.value,
        SseEventType.NODE_EXITED.value,
        SseEventType.NODE_ENTERED.value,
        SseEventType.NODE_EXITED.value,
        SseEventType.RUN_COMPLETED.value,
    ]
    entered = [
        node
        for event_type, node in node_names(parse_sse(body))
        if event_type == SseEventType.NODE_ENTERED.value
    ]
    assert entered == ["retrieve_evidence", "answer_without_evidence"]
    assert chat.stream_calls == 0


async def test_model_declared_no_evidence_sends_no_deltas(api_factory: OpenApi) -> None:
    """模型首字符声明无据：流里零 ``message.delta``，终态为拒答且元数据降级（NO-12）。

    客户端按"没收到增量 + run.completed 是拒答"渲染，不会先看到标记再被撤回。
    """
    async with api_factory(chat=FakeChatModel(reply=ANSWER_NO_EVIDENCE_MARKER)) as harness:
        thread_id = await harness.create_thread()
        _, body = await stream_run(harness, thread_id)

    parsed = parse_sse(body)
    assert not payloads_of(parsed, SseEventType.MESSAGE_DELTA.value)
    completed = payloads_of(parsed, SseEventType.RUN_COMPLETED.value)[0]
    assert completed["answer_status"] == AnswerStatus.INSUFFICIENT_EVIDENCE.value
    assert completed["citations"] == []
    assert completed["answer"]
    assert completed["retrieval_meta"]["is_sufficient"] is False
    assert completed["retrieval_meta"]["reason"] == REASON_MODEL_DECLARED_INSUFFICIENT


async def test_seq_is_monotonic_from_one_and_shared_across_events(api: ApiHarness) -> None:
    """``seq`` 在同一 run 内从 1 起严格递增（CLAUDE.md 8.4）。"""
    thread_id = await api.create_thread()

    _, body = await stream_run(api, thread_id)

    seqs = [payload["seq"] for event_type, payload in parse_sse(body) if event_type != PING]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_every_event_carries_correlation_ids(api: ApiHarness) -> None:
    """每个事件都带 run_id/thread_id/request_id，且三者全流一致（CLAUDE.md 8.4）。"""
    thread_id = await api.create_thread()

    response, body = await stream_run(api, thread_id)

    events = [payload for event_type, payload in parse_sse(body) if event_type != PING]
    assert events
    run_ids = {item["run_id"] for item in events}
    thread_ids = {item["thread_id"] for item in events}
    request_ids = {item["request_id"] for item in events}
    assert len(run_ids) == 1
    assert run_ids.pop()
    assert thread_ids == {thread_id}
    # request_id 与响应头一致：调用方可按它去服务端日志里对账
    assert request_ids == {response.headers["x-request-id"]}


async def test_run_completed_carries_authoritative_terminal_state(api: ApiHarness) -> None:
    """``run.completed`` 的答案取自 checkpoint 权威终态，与 ``GET /state`` 一致。

    不复用流式草稿：草稿可能已被 citation_missing 兜底文案整体替换（见 sse.py 说明）。
    """
    thread_id = await api.create_thread()

    _, body = await stream_run(api, thread_id)
    state = await api.client.get(f"/api/v1/threads/{thread_id}/state", headers=api.auth_headers)

    completed = payloads_of(parse_sse(body), SseEventType.RUN_COMPLETED.value)
    assert len(completed) == 1
    terminal = completed[0]
    assert terminal["answer_status"] == AnswerStatus.ANSWERED.value
    assert terminal["citations"][0]["chunk_id"] == "c1"
    assert terminal["answer"] == state.json()["answer"]
    assert terminal["citations"] == state.json()["citations"]


async def test_message_deltas_concatenate_to_the_final_answer(api_factory: OpenApi) -> None:
    """``message.delta`` 的拼接结果就是最终答案（客户端据此做逐字渲染）。

    用带引用的分块，确保拼接结果能过引用校验——否则会被 citation_missing 兜底整体替换，
    那正是"流式草稿不等于终态"的情形（本用例想验证的是相等的那一种）。
    """
    chunks = ["依据", "《电价办法》", "第3条", "[1]。"]
    async with api_factory(chat=FakeChatModel(chunks=chunks)) as harness:
        thread_id = await harness.create_thread()
        _, body = await stream_run(harness, thread_id)

    parsed = parse_sse(body)
    deltas = [payload["delta"] for payload in payloads_of(parsed, SseEventType.MESSAGE_DELTA.value)]
    assert deltas == chunks
    completed = payloads_of(parsed, SseEventType.RUN_COMPLETED.value)[0]
    assert completed["answer"] == "".join(chunks)


async def test_retrieval_evidence_event_carries_citable_evidence(api: ApiHarness) -> None:
    """``retrieval.evidence`` 必须带可引用的来源信息（CLAUDE.md 7.4）。"""
    thread_id = await api.create_thread()

    _, body = await stream_run(api, thread_id)

    payload = payloads_of(parse_sse(body), SseEventType.RETRIEVAL_EVIDENCE.value)[0]
    assert payload["evidence"][0]["chunk_id"] == "c1"
    assert payload["evidence"][0]["document_name"]
    assert payload["evidence"][0]["page_numbers"] == [3]
    assert payload["meta"]["is_sufficient"] is True


async def test_platform_error_becomes_run_error_frame(api_factory: OpenApi) -> None:
    """平台异常转成 ``run.error``：错误也走流，不得让客户端挂起（CLAUDE.md 8.4）。"""
    async with api_factory(chat=FakeChatModel(raises=ModelGatewayError("网关不可用"))) as harness:
        thread_id = await harness.create_thread()
        response, body = await stream_run(harness, thread_id)

    assert response.status_code == 200, "响应头早已发出，错误只能走流"
    parsed = parse_sse(body)
    errors = payloads_of(parsed, SseEventType.RUN_ERROR.value)
    assert len(errors) == 1
    payload = errors[0]
    assert payload["code"] == ErrorCode.MODEL_CALL_FAILED.value
    # 网关 502 属"非超时/限流类"，不自动重试（CLAUDE.md 3.6：仅幂等且可重试的错误才重试）
    assert payload["retryable"] is False
    assert payload["node"] == "generate_answer"
    assert "网关不可用" not in body.decode("utf-8"), "内部诊断信息不得进流"
    # 出错后必须收流：不得再补一帧 run.completed 冒充成功
    assert event_types(parsed)[-1] == SseEventType.RUN_ERROR.value
    assert not payloads_of(parsed, SseEventType.RUN_COMPLETED.value)


async def test_retrieval_failure_reports_the_failing_node(api_factory: OpenApi) -> None:
    """检索失败时 ``run.error`` 指明停在哪个节点，便于定位（CLAUDE.md 3.6）。"""
    async with api_factory(
        retriever=FakeRetriever(sufficient_outcome(), raises=RetrievalFailedError("向量库不可用"))
    ) as harness:
        thread_id = await harness.create_thread()
        _, body = await stream_run(harness, thread_id)

    payload = payloads_of(parse_sse(body), SseEventType.RUN_ERROR.value)[0]
    assert payload["code"] == ErrorCode.RETRIEVAL_FAILED.value
    assert payload["node"] == "retrieve_evidence"


async def test_unexpected_error_still_reaches_client_without_leaking_details(
    api_factory: OpenApi,
) -> None:
    """未预期异常也必须以 ``run.error`` 收流，且**不回显**异常文本（NO-07 / 8.2）。

    若此处不加兜底，异常会逃出生成器，Starlette 报
    ``Caught handled exception, but response already started.``——
    客户端看到的是连接被掐断，没有任何错误说明。
    """
    leak = "internal-dsn-postgresql://user:pw@10.0.0.1:5432/prod"
    async with api_factory(chat=FakeChatModel(raises=RuntimeError(leak))) as harness:
        thread_id = await harness.create_thread()
        _, body = await stream_run(harness, thread_id)

    parsed = parse_sse(body)
    errors = payloads_of(parsed, SseEventType.RUN_ERROR.value)
    assert len(errors) == 1
    payload = errors[0]
    assert payload["code"] == ErrorCode.INTERNAL_ERROR.value
    assert payload["node"] == "generate_answer"
    assert leak not in body.decode("utf-8")
    assert "RuntimeError" not in body.decode("utf-8")


async def test_heartbeat_frames_keep_the_stream_alive(
    api_factory: OpenApi, fake_settings: Settings
) -> None:
    """空闲超过 ``sse_heartbeat_s`` 时插入 ``: ping`` 注释帧（CLAUDE.md 8.4）。

    心跳是长检索期间唯一能让中间代理不掐断连接的东西，因此这条用例
    既断言"确实发了"，也断言"发心跳没打乱事件序列"。
    """
    slow = fake_settings.model_copy(update={"sse_heartbeat_s": 1})
    async with api_factory(
        settings=slow, retriever=FakeRetriever(sufficient_outcome(), delay_s=1.3)
    ) as harness:
        assert harness.stack.settings.sse_heartbeat_s == 1
        thread_id = await harness.create_thread()
        started = time.perf_counter()
        _, body = await stream_run(harness, thread_id)
        elapsed = time.perf_counter() - started

    parsed = parse_sse(body)
    assert [name for name, _ in parsed if name == PING], "慢依赖期间必须有心跳"
    assert elapsed >= 1.3, "用例本身没有制造足够的空闲时间，断言无意义"
    assert event_types(parsed)[-1] == SseEventType.RUN_COMPLETED.value
    # 心跳不占用 seq：去掉心跳后序号仍必须是连续的 1..N
    seqs = [event["seq"] for name, event in parsed if name != PING]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_fast_run_has_no_heartbeat(api: ApiHarness) -> None:
    """快路径不应出现心跳（否则等于给每个响应白加噪声）。"""
    thread_id = await api.create_thread()

    _, body = await stream_run(api, thread_id)

    assert PING not in event_types(parse_sse(body))


@pytest.mark.parametrize("value", ["", "not-a-thread", "other:someone:abc"])
async def test_stream_rejects_bad_thread_before_opening_the_stream(
    api: ApiHarness, value: str
) -> None:
    """鉴权/归属校验发生在发首帧**之前**，因此以 Problem Details 返回而非流内错误。"""
    response = await api.client.post(
        f"/api/v1/threads/{value}/runs/stream",
        headers=api.auth_headers,
        json={"agent": api.stack.agent_name, "message": TEST_QUESTION},
    )

    assert response.status_code in {404, 422}
    assert response.headers["content-type"] == "application/problem+json"


async def test_stream_unknown_agent_is_404(api: ApiHarness) -> None:
    """未知智能体在开流前拦截，不产生半截流。"""
    thread_id = await api.create_thread()

    response = await api.client.post(
        f"/api/v1/threads/{thread_id}/runs/stream",
        headers=api.auth_headers,
        json={"agent": "nope", "message": TEST_QUESTION},
    )

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


async def test_idempotent_stream_replays_started_and_completed(api_factory: OpenApi) -> None:
    """幂等命中的流式请求做确定性重放：``run.started`` + ``run.completed``（CLAUDE.md 8.3）。

    SSE 无法逐字节复现当初那串增量，因此承诺的边界是"终态一致、run_id 稳定"。
    同时断言图**没有**被再跑一次。
    """
    retriever = FakeRetriever(sufficient_outcome())
    async with api_factory(retriever=retriever) as harness:
        thread_id = await harness.create_thread()
        idem = {"Idempotency-Key": "stream-key-1"}
        _, first_body = await stream_run(harness, thread_id, headers=idem)
        _, replay_body = await stream_run(harness, thread_id, headers=idem)

    first = parse_sse(first_body)
    replay = parse_sse(replay_body)
    assert event_types(replay) == [
        SseEventType.RUN_STARTED.value,
        SseEventType.RUN_COMPLETED.value,
    ]
    assert len(retriever.calls) == 1, "命中幂等键时不得再跑一次图"

    def terminal(parsed: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        payload: dict[str, Any] = payloads_of(parsed, SseEventType.RUN_COMPLETED.value)[0]
        return payload

    assert terminal(replay) == terminal(first)
    # run_id 稳定：客户端的对账键不因重放而漂移
    assert replay[0][1]["run_id"] == first[0][1]["run_id"]
    # seq 从 1 重新开始（这是一条新的响应流）
    assert [item[1]["seq"] for item in replay] == [1, 2]


async def test_idempotent_stream_completion_matches_state(api_factory: OpenApi) -> None:
    """重放出来的终态必须与 ``GET /state`` 一致——缓存不是第二个事实来源。"""
    async with api_factory() as harness:
        thread_id = await harness.create_thread()
        idem = {"Idempotency-Key": "stream-key-2"}
        await stream_run(harness, thread_id, headers=idem)
        _, replay_body = await stream_run(harness, thread_id, headers=idem)
        state = await harness.client.get(
            f"/api/v1/threads/{thread_id}/state", headers=harness.auth_headers
        )

    replayed = payloads_of(parse_sse(replay_body), SseEventType.RUN_COMPLETED.value)[0]
    assert replayed["answer"] == state.json()["answer"]
    assert replayed["citations"] == state.json()["citations"]
