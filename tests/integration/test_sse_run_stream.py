"""SSE 全序列 + 真实 Postgres 检查点（阶段四遗留项，CLAUDE.md 9.3）。

单测里的 SSE 用例走的是内存 checkpointer，所以它们证明不了两件事：

1. 检查点**真的落进了 Postgres**，而不是躺在进程内存里——换一套连接池、重建一次
   图之后还能读回同一会话的状态，才算数；
2. ``aget_state_history`` 在有真实表结构时按预期分页。

这两件事只有在真库上跑才有意义，因此本文件是"集成"而非"单元"。检索器与模型仍用
fake：本文件验的是**编排与持久化链路**，不是模型质量（后者归 ``tests/evals``）。
好处是它不依赖任何外部 API 密钥，只需要 compose 起的 Postgres。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from agent_platform.agents.gov_policy_qa.state import (
    STATUS_ANSWERED,
    STATUS_INSUFFICIENT_EVIDENCE,
)
from agent_platform.core.checkpoint import open_checkpoint_resources
from agent_platform.core.errors import RetrievalFailedError
from agent_platform.core.ids import compose_thread_id
from agent_platform.main import API_PREFIX
from tests.conftest import (
    CITED_REPLY,
    TEST_QUESTION,
    FakeChatModel,
    FakeRetriever,
    insufficient_outcome,
    sufficient_outcome,
)

if TYPE_CHECKING:
    from agent_platform.core.checkpoint import CheckpointResources
    from agent_platform.settings import Settings
    from tests.conftest import ApiHarness, OpenApi

pytestmark = pytest.mark.integration

AGENT = "gov_policy_qa"


# ---------------------------------------------------------------------------
# SSE 消费
# ---------------------------------------------------------------------------


async def collect_frames(response: httpx.Response) -> list[tuple[str, dict[str, Any]]]:
    """消费 SSE 响应，返回 ``[(event_type, data), ...]``。

    按行读取、遇空行成帧——与真实客户端（``scripts/demo_chat.py``）同一套语义，
    因此本函数也顺带验证了"服务端产出的帧，按标准方式读得出来"。
    """
    frames: list[tuple[str, dict[str, Any]]] = []
    block: list[str] = []
    async for line in response.aiter_lines():
        if line != "":
            block.append(line)
            continue
        if not block:
            continue  # 心跳（": ping"）之后的空行
        frames.append(_parse_block(block))
        block = []
    return frames


def _parse_block(block: list[str]) -> tuple[str, dict[str, Any]]:
    """解析一个 SSE 帧块（data 可能跨多行）。"""
    event_type = ""
    data_lines: list[str] = []
    for raw in block:
        if raw.startswith("event: "):
            event_type = raw.removeprefix("event: ")
        elif raw.startswith("data: "):
            data_lines.append(raw.removeprefix("data: "))
        elif raw.startswith(":"):
            continue
        else:  # pragma: no cover - 帧格式错乱时在此直接失败，不静默跳过
            raise AssertionError(f"非法 SSE 行: {raw!r}")
    assert event_type, f"帧缺少 event 行: {block!r}"
    if not data_lines:  # pragma: no cover - 平台事件一律带 data
        raise AssertionError(f"帧缺少 data 行: {block!r}")
    parsed = json.loads("\n".join(data_lines))
    assert isinstance(parsed, dict), f"帧 data 不是 JSON 对象: {block!r}"
    return event_type, parsed


async def run_stream(
    api: ApiHarness, thread_id: str, *, question: str = TEST_QUESTION
) -> list[tuple[str, dict[str, Any]]]:
    """发起一次流式运行并收集全部帧。"""
    url = f"{API_PREFIX}/threads/{thread_id}/runs/stream"
    async with api.client.stream(
        "POST", url, headers=api.auth_headers, json={"agent": AGENT, "message": question}
    ) as response:
        assert response.status_code == 200, await response.aread()
        assert response.headers["content-type"].startswith("text/event-stream")
        return await collect_frames(response)


def event_types(frames: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """取出事件类型序列。"""
    return [event_type for event_type, _ in frames]


# ---------------------------------------------------------------------------
# 全序列与持久化
# ---------------------------------------------------------------------------


async def test_sse_full_sequence_and_state_persisted(
    api_factory: OpenApi,
    integration_settings: Settings,
    checkpoint_resources: CheckpointResources,
) -> None:
    """一次完整运行：事件序列正确 + seq 单调 + 终态可从 Postgres 读回。"""
    retriever = FakeRetriever(sufficient_outcome("c1"))
    chat = FakeChatModel(CITED_REPLY, chunks=["依据", "《电价办法》", "第3条[1]。"])
    async with api_factory(
        settings=integration_settings,
        checkpointer=checkpoint_resources.saver,
        store=checkpoint_resources.store,
        retriever=retriever,
        chat=chat,
    ) as api:
        thread_id = await api.create_thread()
        frames = await run_stream(api, thread_id)

        kinds = event_types(frames)
        assert kinds[0] == "run.started"
        assert kinds[-1] == "run.completed", f"流必须以终态事件收尾，实际: {kinds}"
        assert "run.error" not in kinds
        for expected in ("node.entered", "retrieval.evidence", "message.delta", "node.exited"):
            assert expected in kinds, f"缺少事件 {expected}，实际: {kinds}"

        # seq 严格递增且全局唯一：客户端靠它去重与断点续读（CLAUDE.md 8.4）
        seqs = [data["seq"] for _, data in frames]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)

        # 每帧都要带齐贯穿字段，否则客户端无法把事件关联回 run/会话
        for _, data in frames:
            assert data["run_id"], "事件缺少 run_id"
            assert data["thread_id"], "事件缺少 thread_id"
            assert data["request_id"], "事件缺少 request_id"

        # 增量拼接必须等于终态答案，否则流式与落库是两套事实
        deltas = "".join(
            data["payload"].get("delta", "")
            for event_type, data in frames
            if event_type == "message.delta"
        )
        completed = frames[-1][1]["payload"]
        assert completed["answer"] == CITED_REPLY
        assert deltas == CITED_REPLY
        assert completed["answer_status"] == STATUS_ANSWERED
        assert completed["citations"], "接地回答必须带引用（CLAUDE.md 7.4）"

        # 状态经**真实 Postgres** 读回——这一步才是本文件与单测的分界线
        state = await api.client.get(
            f"{API_PREFIX}/threads/{thread_id}/state", headers=api.auth_headers
        )
        assert state.status_code == 200, state.text
        body = state.json()
        assert body["answer"] == CITED_REPLY
        assert body["answer_status"] == STATUS_ANSWERED
        assert body["next"] == [], "终态不应有待执行节点"
        assert body["has_interrupt"] is False
        assert body["latest_checkpoint_id"], "检查点必须已落库"


async def test_state_survives_new_connection_and_graph(
    api_factory: OpenApi,
    integration_settings: Settings,
    checkpoint_resources: CheckpointResources,
) -> None:
    """换一套连接池、重建一次图之后，同一 thread_id 的状态仍可读。

    这是"检查点真的落库了"的判据：内存 checkpointer 在这一步必然读不到东西，
    因此本用例对 NO-08 有真实区分度，不是走形式。
    """
    dsn = integration_settings.postgres_dsn.get_secret_value()
    async with api_factory(
        settings=integration_settings,
        checkpointer=checkpoint_resources.saver,
        store=checkpoint_resources.store,
    ) as api:
        thread_id = await api.create_thread()
        await run_stream(api, thread_id)

    async with (
        open_checkpoint_resources(dsn) as fresh,
        api_factory(
            settings=integration_settings, checkpointer=fresh.saver, store=fresh.store
        ) as api2,
    ):
        response = await api2.client.get(
            f"{API_PREFIX}/threads/{thread_id}/state", headers=api2.auth_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["answer"] == CITED_REPLY


# ---------------------------------------------------------------------------
# 历史与分页
# ---------------------------------------------------------------------------


async def test_history_grows_across_runs_and_paginates(
    api_factory: OpenApi,
    integration_settings: Settings,
    checkpoint_resources: CheckpointResources,
) -> None:
    """同一会话续跑会追加检查点，且历史可按游标分页。"""
    async with api_factory(
        settings=integration_settings,
        checkpointer=checkpoint_resources.saver,
        store=checkpoint_resources.store,
    ) as api:
        thread_id = await api.create_thread()
        url = f"{API_PREFIX}/threads/{thread_id}/history"

        await run_stream(api, thread_id)
        first = (await api.client.post(url, headers=api.auth_headers, json={"limit": 50})).json()
        assert first["items"], "跑过一次就该有检查点历史"
        assert first["next_cursor"] is None, "50 条以内不该还有下一页"

        await run_stream(api, thread_id, question="续问一次会追加检查点吗？")
        second = (await api.client.post(url, headers=api.auth_headers, json={"limit": 50})).json()
        assert len(second["items"]) > len(first["items"]), "续跑必须追加历史（会话连续性）"

        # 分页：只取一条时游标必须指向该条，且用它翻页能取到更早的检查点
        page = (await api.client.post(url, headers=api.auth_headers, json={"limit": 1})).json()
        assert len(page["items"]) == 1
        cursor = page["next_cursor"]
        assert cursor, "还有更早的检查点，游标不得为空"
        older = (
            await api.client.post(
                url, headers=api.auth_headers, json={"limit": 5, "before": cursor}
            )
        ).json()
        assert older["items"], "游标翻页应取得到更早的检查点"
        assert older["items"][0]["checkpoint_id"] != cursor


# ---------------------------------------------------------------------------
# 错误路径与租户隔离
# ---------------------------------------------------------------------------


async def test_retrieval_failure_is_streamed_as_run_error(
    api_factory: OpenApi,
    integration_settings: Settings,
    checkpoint_resources: CheckpointResources,
) -> None:
    """检索失败必须走流内 ``run.error`` 收尾，不能让客户端一直等（CLAUDE.md 4.7）。"""
    retriever = FakeRetriever(sufficient_outcome(), raises=RetrievalFailedError("向量库不可达"))
    async with api_factory(
        settings=integration_settings,
        checkpointer=checkpoint_resources.saver,
        store=checkpoint_resources.store,
        retriever=retriever,
    ) as api:
        thread_id = await api.create_thread()
        frames = await run_stream(api, thread_id)

        kinds = event_types(frames)
        assert kinds[-1] == "run.error", f"错误必须走流且终止流，实际: {kinds}"
        payload = frames[-1][1]["payload"]
        assert payload["code"] == "RETRIEVAL_FAILED"
        # 不重试：``RetrievalFailedError`` 继承 ``KnowledgeError`` 的默认 ``retryable=False``。
        # 这里刻意**不**按 http_status（503）推断可重试性——同一异常也被用于维度不符这类
        # 永久性配置错误（见 milvus_store.hybrid_search），叫客户端重试只会白耗配额；
        # 只有显式标记为瞬时的异常（如 KnowledgeParseTimeoutError）才置 True。
        assert payload["retryable"] is False
        assert "向量库不可达" not in payload["message"], "对外错误不得泄露内部细节"


async def test_thread_of_other_tenant_is_not_found(
    api_factory: OpenApi,
    integration_settings: Settings,
    checkpoint_resources: CheckpointResources,
) -> None:
    """他人租户的会话一律 404，且与"不存在"不可区分（NO-19 纵深防御）。"""
    async with api_factory(
        settings=integration_settings,
        checkpointer=checkpoint_resources.saver,
        store=checkpoint_resources.store,
    ) as api:
        foreign = compose_thread_id("other-tenant", "other-user", "abc123")
        state = await api.client.get(
            f"{API_PREFIX}/threads/{foreign}/state", headers=api.auth_headers
        )
        history = await api.client.post(
            f"{API_PREFIX}/threads/{foreign}/history", headers=api.auth_headers, json={"limit": 5}
        )
        for response in (state, history):
            assert response.status_code == 404, "越权访问未被拒绝"
            assert response.headers["content-type"].startswith("application/problem+json")


async def test_no_evidence_run_degrades_without_calling_model(
    api_factory: OpenApi,
    integration_settings: Settings,
    checkpoint_resources: CheckpointResources,
) -> None:
    """无证据时必须降级且不调模型（NO-12），在真实持久化路径上同样成立。"""
    retriever = FakeRetriever(insufficient_outcome())
    chat = FakeChatModel(CITED_REPLY)
    async with api_factory(
        settings=integration_settings,
        checkpointer=checkpoint_resources.saver,
        store=checkpoint_resources.store,
        retriever=retriever,
        chat=chat,
    ) as api:
        thread_id = await api.create_thread()
        frames = await run_stream(api, thread_id, question="今天天气怎么样？")

        assert frames[-1][0] == "run.completed"
        payload = frames[-1][1]["payload"]
        assert payload["answer_status"] == STATUS_INSUFFICIENT_EVIDENCE
        assert payload["citations"] == []
        # 节点走的是 astream（generate_answer），因此两个计数器都要查：
        # 只看 calls 的话，"其实调了模型"这件事会被漏掉，断言就成了摆设。
        assert chat.calls == []
        assert chat.stream_calls == 0
