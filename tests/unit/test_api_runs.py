"""运行、状态、历史与幂等测试（CLAUDE.md 8.1 / 8.3 / NO-12 / NO-19）。

非流式路径（``POST /runs``）与流式路径共用同一份终态翻译层（``api/state_view.py``），
本文件锁定两者的一致性：同一 thread 上，``/runs`` 的响应字段必须与 ``/state`` 完全对齐。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from agent_platform.agents.gov_policy_qa.state import AnswerStatus
from agent_platform.api.idempotency import IDEMPOTENCY_HEADER, IDEMPOTENCY_NAMESPACE
from agent_platform.knowledge.retriever import REASON_NO_CANDIDATES
from tests.conftest import (
    TEST_QUESTION,
    FakeChatModel,
    FakeRetriever,
    insufficient_outcome,
    sufficient_outcome,
)

if TYPE_CHECKING:
    from tests.conftest import ApiHarness, OpenApi


def _run_body(api: ApiHarness, message: str = TEST_QUESTION) -> dict[str, str]:
    """构造运行请求体（租户不由前端指定，NO-19）。"""
    return {"agent": api.stack.agent_name, "message": message}


async def test_run_returns_answer_with_citations(api: ApiHarness) -> None:
    """有证据时返回带引用的答案（CLAUDE.md 7.4：答案与证据绑定）。"""
    thread_id = await api.create_thread()

    response = await api.client.post(
        f"/api/v1/threads/{thread_id}/runs", headers=api.auth_headers, json=_run_body(api)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["thread_id"] == thread_id
    assert body["run_id"]
    assert body["answer_status"] == AnswerStatus.ANSWERED.value
    assert body["citations"], "有证据的答案必须带引用"
    assert body["citations"][0]["chunk_id"] == "c1"
    assert body["citations"][0]["document_name"]
    assert body["retrieval_meta"]["is_sufficient"] is True


async def test_run_without_evidence_degrades_and_does_not_call_model(
    api_factory: OpenApi,
) -> None:
    """无证据时不编造：走降级分支且模型一次都不调（NO-12）。"""
    chat = FakeChatModel()
    async with api_factory(retriever=FakeRetriever(insufficient_outcome()), chat=chat) as harness:
        thread_id = await harness.create_thread()
        response = await harness.client.post(
            f"/api/v1/threads/{thread_id}/runs",
            headers=harness.auth_headers,
            json=_run_body(harness),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer_status"] == AnswerStatus.INSUFFICIENT_EVIDENCE.value
    assert body["citations"] == []
    assert body["retrieval_meta"]["reason"] == REASON_NO_CANDIDATES
    assert chat.stream_calls == 0, "无证据分支不得调用模型"


async def test_run_and_state_report_identical_terminal_fields(api: ApiHarness) -> None:
    """``/runs`` 与 ``/state`` 必须给出同一份终态（单一翻译层的存在理由）。"""
    thread_id = await api.create_thread()
    run = await api.client.post(
        f"/api/v1/threads/{thread_id}/runs", headers=api.auth_headers, json=_run_body(api)
    )
    state = await api.client.get(f"/api/v1/threads/{thread_id}/state", headers=api.auth_headers)

    assert run.status_code == 200
    assert state.status_code == 200
    run_body, state_body = run.json(), state.json()
    for field in ("answer", "answer_status", "citations", "retrieval_meta"):
        assert run_body[field] == state_body[field], f"字段 {field} 在两个接口间不一致"
    assert state_body["thread_id"] == thread_id
    assert state_body["latest_checkpoint_id"]
    assert state_body["next"] == []
    assert state_body["has_interrupt"] is False


async def test_state_of_unrun_thread_is_404(api: ApiHarness) -> None:
    """未跑过的会话读状态是 404，而不是返回 pending 空壳。"""
    thread_id = await api.create_thread()

    response = await api.client.get(f"/api/v1/threads/{thread_id}/state", headers=api.auth_headers)

    assert response.status_code == 404


async def test_unknown_agent_is_404(api: ApiHarness) -> None:
    """未注册的智能体返回 404（清单以 agents/registry.py 为唯一来源）。"""
    thread_id = await api.create_thread()

    response = await api.client.post(
        f"/api/v1/threads/{thread_id}/runs",
        headers=api.auth_headers,
        json={"agent": "no_such_agent", "message": TEST_QUESTION},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


async def test_registered_but_unwired_agent_is_404_not_silently_rewritten(
    api: ApiHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注册表里有、但本进程没装配的智能体必须 404。

    若放行，请求会**静默**跑成当前这个图——调用方拿到的是另一个智能体的答案，
    却以为命中了自己点名的那个。这是部署缺陷，必须显式暴露。
    """
    from agent_platform.agents import registry
    from agent_platform.agents.registry import AGENTS, AgentSpec

    extra = AgentSpec(
        name="other_agent",
        owner="测试",
        version="0.1.0",
        entrypoint="./src/agent_platform/agents/other/graph.py:build_graph",
    )
    monkeypatch.setattr(registry, "AGENTS", (*AGENTS, extra))
    monkeypatch.setattr(registry, "_BY_NAME", {spec.name: spec for spec in (*AGENTS, extra)})

    thread_id = await api.create_thread()
    response = await api.client.post(
        f"/api/v1/threads/{thread_id}/runs",
        headers=api.auth_headers,
        json={"agent": "other_agent", "message": TEST_QUESTION},
    )

    assert response.status_code == 404
    assert "other_agent" in response.json()["detail"]


async def test_oversized_question_is_rejected_before_running(api_factory: OpenApi) -> None:
    """超长提问在边界拒绝，且**不触发检索**（省下真实成本；上限可配，NO-03）。"""
    retriever = FakeRetriever(sufficient_outcome())
    async with api_factory(retriever=retriever) as harness:
        limit = harness.stack.settings.max_question_chars
        thread_id = await harness.create_thread()
        response = await harness.client.post(
            f"/api/v1/threads/{thread_id}/runs",
            headers=harness.auth_headers,
            json=_run_body(harness, message="问" * (limit + 1)),
        )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"
    assert str(limit) in response.json()["detail"]
    assert retriever.calls == []


async def test_extra_request_fields_are_forbidden(api: ApiHarness) -> None:
    """请求体多出字段直接 422：静默忽略会让人误以为 tenant_id 生效了（NO-19）。"""
    thread_id = await api.create_thread()

    response = await api.client.post(
        f"/api/v1/threads/{thread_id}/runs",
        headers=api.auth_headers,
        json={**_run_body(api), "tenant_id": "attacker_tenant"},
    )

    assert response.status_code == 422


async def test_missing_message_is_422(api: ApiHarness) -> None:
    """缺必填字段是 422，且响应体不回显用户输入（CLAUDE.md 8.2 / 10.1）。"""
    thread_id = await api.create_thread()

    response = await api.client.post(
        f"/api/v1/threads/{thread_id}/runs",
        headers=api.auth_headers,
        json={"agent": api.stack.agent_name},
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"


async def test_run_on_cross_tenant_thread_is_404(api: ApiHarness) -> None:
    """跨租户会话不可运行（NO-19）。"""
    response = await api.client.post(
        "/api/v1/threads/other_tenant:someone:abc/runs",
        headers=api.auth_headers,
        json=_run_body(api),
    )

    assert response.status_code == 404


@pytest.mark.parametrize("key", ["", "has space", "x" * 200, "slash/and?query"])
async def test_illegal_idempotency_key_is_422(api: ApiHarness, key: str) -> None:
    """幂等键格式受限：它会成为 store 主键并进日志（CLAUDE.md 8.3）。"""
    thread_id = await api.create_thread()

    response = await api.client.post(
        f"/api/v1/threads/{thread_id}/runs",
        headers={**api.auth_headers, IDEMPOTENCY_HEADER: key},
        json=_run_body(api),
    )

    assert response.status_code == 422


async def test_idempotent_run_returns_cached_result_without_reexecuting(
    api_factory: OpenApi,
) -> None:
    """同一幂等键第二次请求直接返回首次结果，**不重复执行**（CLAUDE.md 8.3）。

    节点带外部副作用，重放不是免费的——因此断言点落在"检索器只被调过一次"，
    而不只是"两次响应相同"。
    """
    retriever = FakeRetriever(sufficient_outcome())
    async with api_factory(retriever=retriever) as harness:
        thread_id = await harness.create_thread()
        headers = {**harness.auth_headers, IDEMPOTENCY_HEADER: "order-2026-0001"}
        first = await harness.client.post(
            f"/api/v1/threads/{thread_id}/runs", headers=headers, json=_run_body(harness)
        )
        second = await harness.client.post(
            f"/api/v1/threads/{thread_id}/runs", headers=headers, json=_run_body(harness)
        )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(retriever.calls) == 1, "命中幂等键时不得再执行一次图"


async def test_idempotency_cache_is_scoped_per_tenant(api_factory: OpenApi) -> None:
    """幂等命名空间以 tenant_id 为第一段，跨租户不可见（NO-19）。

    此处直接经 ``BaseStore`` 断言**存储布局**，而不是靠"换个 token 再请求"
    ——后者同时受鉴权与租户解析影响，失败时分不清是哪一环坏了。
    """
    async with api_factory() as harness:
        thread_id = await harness.create_thread()
        key = "shared-key"
        await harness.client.post(
            f"/api/v1/threads/{thread_id}/runs",
            headers={**harness.auth_headers, IDEMPOTENCY_HEADER: key},
            json=_run_body(harness),
        )
        tenant_id = harness.stack.settings.demo_tenant_id
        stored = await harness.stack.store.aget((tenant_id, IDEMPOTENCY_NAMESPACE), key)
        absent = await harness.stack.store.aget(("other_tenant", IDEMPOTENCY_NAMESPACE), key)

    assert stored is not None
    assert absent is None


async def test_run_without_idempotency_key_is_not_cached(api_factory: OpenApi) -> None:
    """不带幂等键时不留任何缓存条目（不偷存用户数据）。"""
    async with api_factory() as harness:
        thread_id = await harness.create_thread()
        await harness.client.post(
            f"/api/v1/threads/{thread_id}/runs",
            headers=harness.auth_headers,
            json=_run_body(harness),
        )
        tenant_id = harness.stack.settings.demo_tenant_id
        stored = await harness.stack.store.aget((tenant_id, IDEMPOTENCY_NAMESPACE), "anything")

    assert stored is None


async def test_history_paginates_newest_first_with_cursor(api: ApiHarness) -> None:
    """历史按 checkpoint 由新到旧分页，游标指向下一页（CLAUDE.md 8.1 游标分页）。"""
    thread_id = await api.create_thread()
    await api.client.post(
        f"/api/v1/threads/{thread_id}/runs", headers=api.auth_headers, json=_run_body(api)
    )

    first = await api.client.post(
        f"/api/v1/threads/{thread_id}/history",
        headers=api.auth_headers,
        json={"limit": 2},
    )
    assert first.status_code == 200, first.text
    page = first.json()
    assert len(page["items"]) == 2
    assert page["next_cursor"] == page["items"][-1]["checkpoint_id"]
    # 最新一条是终态：已跑到 END，答案就位
    assert page["items"][0]["next"] == []
    assert page["items"][0]["answer_status"] == AnswerStatus.ANSWERED.value
    # 次新一条是中途快照：仍在等待 generate_answer
    assert page["items"][1]["next"] == ["generate_answer"]
    assert page["items"][1]["answer"] == ""

    second = await api.client.post(
        f"/api/v1/threads/{thread_id}/history",
        headers=api.auth_headers,
        json={"limit": 2, "before": page["next_cursor"]},
    )
    assert second.status_code == 200
    later = second.json()
    assert later["items"], "翻页必须拿到更早的记录"
    first_page_ids = {item["checkpoint_id"] for item in page["items"]}
    assert not first_page_ids & {item["checkpoint_id"] for item in later["items"]}


async def test_history_last_page_has_null_cursor(api: ApiHarness) -> None:
    """取满但已到末页时 next_cursor 为 null——不能把"刚好取满"当成"还有更多"。"""
    thread_id = await api.create_thread()
    await api.client.post(
        f"/api/v1/threads/{thread_id}/runs", headers=api.auth_headers, json=_run_body(api)
    )
    all_items = await api.client.post(
        f"/api/v1/threads/{thread_id}/history",
        headers=api.auth_headers,
        json={"limit": 100},
    )
    total = len(all_items.json()["items"])
    assert total >= 2

    last_page = await api.client.post(
        f"/api/v1/threads/{thread_id}/history",
        headers=api.auth_headers,
        json={"limit": total},
    )

    assert len(last_page.json()["items"]) == total
    assert last_page.json()["next_cursor"] is None


async def test_history_of_unrun_thread_is_404(api: ApiHarness) -> None:
    """未知会话返回 404 而非空列表：空列表会被读成"跑过但没有记录"。"""
    thread_id = await api.create_thread()

    response = await api.client.post(
        f"/api/v1/threads/{thread_id}/history", headers=api.auth_headers, json={}
    )

    assert response.status_code == 404


@pytest.mark.parametrize("limit", [0, 101, -1])
async def test_history_limit_bounds_are_enforced(api: ApiHarness, limit: int) -> None:
    """分页上限由 DTO 约束（CLAUDE.md 8.1：字段必须有约束）。"""
    thread_id = await api.create_thread()

    response = await api.client.post(
        f"/api/v1/threads/{thread_id}/history",
        headers=api.auth_headers,
        json={"limit": limit},
    )

    assert response.status_code == 422


async def test_history_rejects_unknown_cursor_field(api: ApiHarness) -> None:
    """多出字段直接 422（``extra="forbid"``）。"""
    thread_id = await api.create_thread()

    response = await api.client.post(
        f"/api/v1/threads/{thread_id}/history",
        headers=api.auth_headers,
        json={"limit": 5, "after": "x"},
    )

    assert response.status_code == 422


async def test_second_run_appends_to_same_thread(api: ApiHarness) -> None:
    """同一会话第二次运行累积消息（``add_messages`` reducer），不覆盖历史（CLAUDE.md 4.1）。"""
    thread_id = await api.create_thread()
    await api.client.post(
        f"/api/v1/threads/{thread_id}/runs", headers=api.auth_headers, json=_run_body(api, "第一问")
    )
    second = await api.client.post(
        f"/api/v1/threads/{thread_id}/runs", headers=api.auth_headers, json=_run_body(api, "第二问")
    )

    assert second.status_code == 200
    history = await api.client.post(
        f"/api/v1/threads/{thread_id}/history",
        headers=api.auth_headers,
        json={"limit": 100},
    )
    steps = sorted({item["step"] for item in history.json()["items"] if item["step"]})
    assert steps[-1] >= 4, "两轮运行应产生更多检查点步数"


async def test_run_response_never_leaks_internal_details(api: ApiHarness) -> None:
    """答案与元数据中不得出现连接串/网关地址/模型型号（CLAUDE.md 10.1 / NO-03）。"""
    thread_id = await api.create_thread()
    response = await api.client.post(
        f"/api/v1/threads/{thread_id}/runs", headers=api.auth_headers, json=_run_body(api)
    )
    payload = json.dumps(response.json(), ensure_ascii=False)

    assert "postgresql://" not in payload
    assert "localhost" not in payload
    assert "litellm" not in payload.lower()
