"""``scripts/demo_chat.py`` 的单元测试（CLAUDE.md 9.3：离线确定）。

这是端到端冒烟脚本，仓库里没有比它更靠外的一层，所以它自身的消费逻辑必须有测试兜住：
写它的时候真的漏过一版"帧累积"的错误（循环里没有把行攒进 block，导致每帧都是空的），
而那种错误在只跑单测派生的假数据时看不出来——必须喂**真实格式**的 SSE 字节流。

因此 ``test_run_stream_*`` 用 httpx.MockTransport 回放与 ``SseEvent.to_sse()`` 同构的帧，
覆盖整条消费路径：分帧 → 解析 → 渲染 → 退出码。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from agent_platform.core.errors import ConfigurationError
from agent_platform.settings import Settings
from scripts import demo_chat

# ---------------------------------------------------------------------------
# 帧构造与传输替身
# ---------------------------------------------------------------------------


def sse(event: str, payload: dict[str, Any], **envelope: Any) -> bytes:
    """按 ``SseEvent.to_sse()`` 的真实格式拼一帧：``event: X\\ndata: {...}\\n\\n``。"""
    body = {"payload": payload, "seq": 1, "run_id": "r1", "thread_id": "t1", "request_id": "q1"}
    body.update(envelope)
    return f"event: {event}\ndata: {json.dumps(body, separators=(',', ':'), ensure_ascii=False)}\n\n".encode()


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """构造走 MockTransport 的客户端（不发真实请求）。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://testserver")


def make_frame(event: str, payload: dict[str, Any] | None = None) -> demo_chat.Frame:
    """构造一个已解析的帧。"""
    return demo_chat.Frame(event=event, data={"payload": payload or {}})


def stream_handler(chunks: list[bytes]) -> Callable[[httpx.Request], httpx.Response]:
    """返回一个总是回放同一段 SSE 字节的处理器。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=b"".join(chunks)
        )

    return _handler


# ---------------------------------------------------------------------------
# 帧解析
# ---------------------------------------------------------------------------


def test_parse_frame_reads_event_and_data() -> None:
    """标准帧解析出事件名与 JSON 负载。"""
    block = ["event: run.started", 'data: {"payload":{"agent":"gov_policy_qa"}}']
    frame = demo_chat.parse_frame(block)
    assert frame is not None
    assert frame.event == "run.started"
    assert frame.data["payload"]["agent"] == "gov_policy_qa"


def test_parse_frame_returns_none_for_heartbeat() -> None:
    """心跳是 ``:`` 开头的注释行，按 SSE 规范忽略，不是错误。"""
    assert demo_chat.parse_frame([": ping"]) is None
    assert demo_chat.parse_frame([": keep-alive", "", ""]) is None


def test_parse_frame_joins_multiline_data() -> None:
    """SSE 允许 data 跨多行，拼接时用换行连接（规范如此）。"""
    frame = demo_chat.parse_frame(["event: x", 'data: {"a":', "data: 1}"])
    assert frame is not None
    assert frame.data == {"a": 1}


def test_parse_frame_handles_data_without_space_after_colon() -> None:
    """``data:{...}`` 与 ``data: {...}`` 都要能读，不能假设一定有空格。"""
    frame = demo_chat.parse_frame(["event:x", 'data:{"k":"v"}'])
    assert frame is not None
    assert (frame.event, frame.data) == ("x", {"k": "v"})


def test_parse_frame_rejects_missing_event() -> None:
    """只有 data 没有 event 属协议破损：必须显式失败，不能猜事件类型。"""
    with pytest.raises(ValueError, match="缺少 event"):
        demo_chat.parse_frame(['data: {"a":1}'])


def test_parse_frame_rejects_non_json_data() -> None:
    """data 不是合法 JSON 时抛错，而不是当成空负载继续。"""
    with pytest.raises(ValueError, match="不是合法 JSON"):
        demo_chat.parse_frame(["event: x", "data: not-json"])


def test_parse_frame_rejects_non_object_data() -> None:
    """data 是 JSON 数组等非对象时同样拒绝。"""
    with pytest.raises(ValueError, match="不是 JSON 对象"):
        demo_chat.parse_frame(["event: x", "data: [1,2]"])


# ---------------------------------------------------------------------------
# 单帧渲染
# ---------------------------------------------------------------------------


def test_run_error_frame_maps_to_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    """run.error 必须以专用退出码结束，让调用方能区分"流内失败"与"连不上"。"""
    frame = make_frame(
        "run.error", {"code": "KNOWLEDGE_UNAVAILABLE", "message": "检索不可用", "retryable": True}
    )
    assert demo_chat._handle_frame(frame) == demo_chat.EXIT_RUN_ERROR
    assert "KNOWLEDGE_UNAVAILABLE" in capsys.readouterr().err


def test_message_delta_frame_writes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """增量答案写 stdout（``demo_chat.py > answer.txt`` 要拿到干净答案，故不进 stderr）。"""
    assert demo_chat._handle_frame(make_frame("message.delta", {"delta": "电价"})) is None
    captured = capsys.readouterr()
    assert captured.out == "电价"
    assert captured.err == ""


def test_progress_frames_write_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """进度类事件只进 stderr，不得污染 stdout 里的答案。"""
    for event in ("run.started", "node.entered", "retrieval.evidence"):
        assert (
            demo_chat._handle_frame(make_frame(event, {"node": "retrieve", "evidence": [1]}))
            is None
        )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "retrieve" in captured.err


def test_unknown_event_is_ignored(capsys: pytest.CaptureFixture[str]) -> None:
    """服务端新增事件类型时客户端不得崩溃（向前兼容：忽略而非报错）。"""
    assert demo_chat._handle_frame(make_frame("something.new", {"x": 1})) is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# ---------------------------------------------------------------------------
# 引用渲染
# ---------------------------------------------------------------------------


def test_render_citations_includes_name_pages_and_source() -> None:
    """引用必须给出文档名、页码与来源（CLAUDE.md 7.4 可溯源）。"""
    text = demo_chat.render_citations(
        [
            {
                "document_name": "新能源电价政策.pdf",
                "page_numbers": [3, 5],
                "source_uri": "0012.新能源电价政策.pdf",
            }
        ]
    )
    assert "[1] 新能源电价政策.pdf" in text
    assert "第 3, 5 页" in text
    assert "0012.新能源电价政策.pdf" in text


def test_render_citations_handles_empty_and_missing_fields() -> None:
    """无引用与字段缺失都要能渲染，不能因缺字段抛异常（降级分支必走）。"""
    assert "未引用任何证据" in demo_chat.render_citations([])
    text = demo_chat.render_citations([{}])
    assert "(未知文档)" in text


# ---------------------------------------------------------------------------
# 流消费（真实字节回放）
# ---------------------------------------------------------------------------


async def test_run_stream_consumes_full_sequence(capsys: pytest.CaptureFixture[str]) -> None:
    """完整回放一段真实格式的 SSE：必须拿到全部增量、引用与成功退出码。

    这段用例专门盯住帧累积：心跳后的空行若让 block 提前清空，就不会有任何输出，
    而以"答案为空"的形式静默失败。
    """
    chunks = [
        sse("run.started", {"agent": "gov_policy_qa"}),
        sse("node.entered", {"node": "retrieve_evidence"}),
        sse("retrieval.evidence", {"evidence": [{"chunk_id": "c1"}]}),
        b": ping\n\n",
        sse("message.delta", {"delta": "居民电价"}),
        sse("message.delta", {"delta": "按年度核定[1]。"}),
        sse("node.exited", {"node": "generate_answer"}),
        sse(
            "run.completed",
            {
                "answer": "居民电价按年度核定[1]。",
                "answer_status": "grounded",
                "citations": [{"document_name": "电价办法.pdf", "page_numbers": [3]}],
                "retrieval_meta": {},
            },
        ),
    ]
    async with make_client(stream_handler(chunks)) as client:
        code = await demo_chat.run_stream(
            client,
            base_url="http://testserver",
            headers={"Authorization": "Bearer t"},
            thread_id="t1",
            agent="gov_policy_qa",
            question="2025年新能源电价机制？",
        )

    captured = capsys.readouterr()
    assert code == demo_chat.EXIT_OK
    assert "居民电价" in captured.out
    assert "按年度核定[1]。" in captured.out
    assert "电价办法.pdf" in captured.out
    assert "grounded" in captured.out, "答案状态要显示，未接地时必须看得出来"
    assert "命中证据 1 条" in captured.err


async def test_run_stream_stops_at_run_error(capsys: pytest.CaptureFixture[str]) -> None:
    """run.error 必须立刻结束消费——服务端已终止流，客户端不该继续等。"""
    chunks = [
        sse("run.started", {"agent": "gov_policy_qa"}),
        sse("run.error", {"code": "KNOWLEDGE_UNAVAILABLE", "message": "检索不可用"}),
    ]
    async with make_client(stream_handler(chunks)) as client:
        code = await demo_chat.run_stream(
            client,
            base_url="http://testserver",
            headers={},
            thread_id="t1",
            agent="gov_policy_qa",
            question="q",
        )

    captured = capsys.readouterr()
    assert code == demo_chat.EXIT_RUN_ERROR
    assert "KNOWLEDGE_UNAVAILABLE" in captured.err


async def test_run_stream_reports_protocol_error_on_truncated_stream(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """流结束了却没有 run.completed，说明连接被掐断——必须报协议错误而非假装成功。"""
    chunks = [sse("run.started", {"agent": "gov_policy_qa"})]
    async with make_client(stream_handler(chunks)) as client:
        code = await demo_chat.run_stream(
            client,
            base_url="http://testserver",
            headers={},
            thread_id="t1",
            agent="gov_policy_qa",
            question="q",
        )

    assert code == demo_chat.EXIT_PROTOCOL
    assert "run.completed" in capsys.readouterr().err


async def test_run_stream_reports_http_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """4xx/5xx 走 HTTP 退出码，且把响应体打出来便于定位。"""

    def _unauthorized(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, text="unauthorized")

    async with make_client(_unauthorized) as client:
        code = await demo_chat.run_stream(
            client,
            base_url="http://testserver",
            headers={},
            thread_id="t1",
            agent="gov_policy_qa",
            question="q",
        )

    captured = capsys.readouterr()
    assert code == demo_chat.EXIT_HTTP
    assert "401" in captured.err
    assert "unauthorized" in captured.err


async def test_create_thread_returns_id() -> None:
    """建会话取回 thread_id。"""

    def _created(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(201, json={"thread_id": "abc"})

    async with make_client(_created) as client:
        assert (
            await demo_chat.create_thread(client, base_url="http://testserver", headers={}) == "abc"
        )


async def test_create_thread_rejects_missing_id() -> None:
    """响应缺少 thread_id 属服务端契约破损：必须报错，不能带着空 ID 继续跑。"""

    def _empty(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(201, json={})

    async with make_client(_empty) as client:
        with pytest.raises(ConfigurationError, match="thread_id"):
            await demo_chat.create_thread(client, base_url="http://testserver", headers={})


# ---------------------------------------------------------------------------
# 命令行契约
# ---------------------------------------------------------------------------


def test_parser_requires_question() -> None:
    """--question 必填：没有提问就没有可跑的东西。"""
    with pytest.raises(SystemExit):
        demo_chat.build_parser().parse_args([])


def test_base_url_override_wins(fake_settings: Settings) -> None:
    """显式 --base-url 优先，且去掉尾部斜杠避免拼出双斜杠。"""
    assert (
        demo_chat._resolve_base_url(fake_settings, "http://10.0.0.1:9000/")
        == "http://10.0.0.1:9000"
    )


def test_base_url_rewrites_wildcard_bind_address(fake_settings: Settings) -> None:
    """未指定时按 host/port 拼装，且 ``0.0.0.0`` 这类通配绑定地址要改写成可连接的回环。

    ``AGENT_API_HOST`` 是**服务端监听**地址，直接拿去连会失败（0.0.0.0 不是可路由目标）。
    """
    assert fake_settings.agent_api_host == "0.0.0.0", "前提：默认配置就是通配绑定"
    assert demo_chat._resolve_base_url(fake_settings, None) == "http://127.0.0.1:8000"


def test_base_url_keeps_concrete_host(fake_settings: Settings) -> None:
    """显式配了具体地址时原样使用，不得被改写成回环。"""
    settings = fake_settings.model_copy(update={"agent_api_host": "10.0.0.7"})
    assert demo_chat._resolve_base_url(settings, None) == "http://10.0.0.7:8000"


def test_exit_codes_are_distinct_and_documented() -> None:
    """退出码是对外契约：既不得重复，也不得与 --help 的声明脱节。"""
    codes = (
        demo_chat.EXIT_OK,
        demo_chat.EXIT_USAGE,
        demo_chat.EXIT_HTTP,
        demo_chat.EXIT_RUN_ERROR,
        demo_chat.EXIT_PROTOCOL,
    )
    assert len(set(codes)) == len(codes)
    epilog = demo_chat.build_parser().epilog
    assert epilog is not None
    for code in codes:
        assert str(code) in epilog
