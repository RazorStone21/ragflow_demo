"""LangGraph 事件流 → SSE 帧（CLAUDE.md 4.7 / 8.4）。

分三层职责，互不越界：

- **节点侧**只负责"有话说"——经 ``writer(AgentStreamEvent(...).to_chunk())`` 推
  ``{"event","data"}``，不关心帧格式；
- **本模块**负责把图的事件流补全成完整的 SSE 事件序列（``run.started`` … ``run.completed``）、
  分配 ``seq``、在空闲时插心跳、把异常转成 ``run.error``；
- **core/streaming.py** 独占帧的字节格式（``event:`` / ``data:`` / ``: ping``）。

三个必须踩准的实现细节（均已在已安装的 langgraph / sse-starlette 源码中核实）：

1. ``stream_mode`` 必须传 **list**。``langgraph/pregel/main.py`` 的 ``_output`` 用
   ``isinstance(stream_mode, list)`` 判断，传元组或裸串会**静默**退化成裸 payload，
   模式判别符随之丢失，随后所有事件都会被归错类。
2. 产出的是 ``(mode, payload)`` **二元组**（``astream`` 的 ``version`` 默认为 ``"v1"``）。
   若将来显式改用 ``version="v2"``，产出会变成 ``{"type","ns","data"}`` 字典——
   ``tests/unit/test_api_sse_framing.py`` 锁死了本假设，届时会直接失败而不是静默错乱。
3. 帧必须产出 **bytes**。``sse_starlette.event.ensure_bytes`` 会把 ``str`` 重新包装成
   ``ServerSentEvent(str(...))`` 再渲染，等于在已渲染好的每行前再加一层 ``data: ``。

为何不用 ``BaseHTTPMiddleware`` 记录 request_id、也不用 ``graph.astream_events``：
前者让 contextvar 跨不过 task 边界，后者产出的是给调试看的全量内部事件，
与本平台已冻结的 10 类 SSE 事件不是一回事。契约以 CLAUDE.md 8.4 为准。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Durability, StreamMode
from pydantic import BaseModel

from agent_platform.agents.gov_policy_qa.state import initial_state
from agent_platform.api.deps import run_config, thread_config
from agent_platform.api.schemas.events import (
    NodePayload,
    RunCompletedPayload,
    RunErrorPayload,
    RunStartedPayload,
)
from agent_platform.api.schemas.runs import RunRequest, RunResponse
from agent_platform.api.state_view import completed_payload
from agent_platform.core.errors import AgentPlatformError, ConfigurationError, ErrorCode
from agent_platform.core.logging import get_logger, log_context
from agent_platform.core.streaming import SseEmitter, SseEventType, heartbeat_frame

logger = get_logger(__name__)

_MODE_TASKS: StreamMode = "tasks"
_MODE_CUSTOM: StreamMode = "custom"

_STREAM_MODES: list[StreamMode] = [_MODE_TASKS, _MODE_CUSTOM]
"""订阅的流模式。

``tasks`` 而非 ``updates``：``tasks`` 在节点**开始执行前**发出（能给出真实的进入时序），
``updates`` 在节点**完成后**才发（只能事后追认）。客户端要的"正在检索"必须来自前者。

字面量 list 不可改成元组或常量元组，理由见模块 docstring 第 1 条。
"""

_DURABILITY: Durability = "sync"
"""检查点写入模式。

默认的 ``"async"`` 不保证在 ``astream`` 迭代结束前把检查点落库，
于是紧随其后的 ``aget_state`` 可能读到**上一个**检查点，``run.completed`` 里
就会是上一轮的答案。``"sync"`` 让收尾读取必然看到本轮终态。
"""


async def open_run_event_stream(
    *,
    graph: CompiledStateGraph[Any, Any, Any, Any],
    body: RunRequest,
    thread_id: str,
    tenant_id: str,
    request_id: str,
    run_id: str,
    heartbeat_s: int,
    on_completed: Callable[[RunCompletedPayload], Awaitable[None]] | None = None,
) -> AsyncIterator[bytes]:
    """执行一次运行并产出 SSE 帧（字节）。

    Args:
        graph: 已编译并挂好 checkpointer 的图。
        body: 运行请求（agent + message）。
        thread_id: 已鉴权的会话 ID。
        tenant_id: 已认证的租户（写入 config，供节点读检索隔离条件）。
        request_id: 本次 HTTP 请求 ID。
        run_id: 本次运行 ID。
        heartbeat_s: 空闲多久插一帧心跳。
        on_completed: 终态落地后的回调（如记录幂等结果）。异常会被当作本次运行失败处理——
            回调失败即意味着这次成功无法被幂等地重放，此时给客户端一个"完成"是在撒谎。

    Yields:
        UTF-8 编码的完整 SSE 帧（含结尾空行）。

    Notes:
        流内异常一律转为 ``run.error`` 帧后正常结束：响应头在第一个 ``yield`` 时
        就已经发出，此后再抛异常，Starlette 会报
        ``Caught handled exception, but response already started.``，
        客户端只会看到连接被掐断而拿不到任何错误说明（CLAUDE.md 8.4）。
    """
    emitter = SseEmitter(run_id=run_id, thread_id=thread_id, request_id=request_id)
    last_node: str | None = None

    with log_context(thread_id=thread_id, tenant_id=tenant_id, run_id=run_id):
        logger.info("run.started", extra={"event": "run.started", "agent": body.agent})
        yield _frame(emitter, SseEventType.RUN_STARTED, RunStartedPayload(agent=body.agent))

        iterator = _as_async_generator(
            graph.astream(
                initial_state(body.message),
                run_config(thread_id=thread_id, tenant_id=tenant_id),
                stream_mode=_STREAM_MODES,
                durability=_DURABILITY,
            )
        )
        pending: asyncio.Future[Any] | None = None

        try:
            while True:
                if pending is None:
                    # 同一个 future 跨多轮复用：asyncio.wait 超时**不会**取消它，
                    # 若每次超时都新建 __anext__()，图会被并发推进，直接抛
                    # "already running"。心跳分支必须 continue 回外层 this 变量。
                    pending = asyncio.ensure_future(iterator.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=heartbeat_s)
                if not done:
                    yield heartbeat_frame().encode("utf-8")
                    continue
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    break
                finally:
                    pending = None

                mode, payload = _split_chunk(chunk)
                if mode == _MODE_TASKS:
                    node = payload.get("name")
                    if not isinstance(node, str):
                        continue
                    if "triggers" in payload:
                        last_node = node
                        yield _frame(emitter, SseEventType.NODE_ENTERED, NodePayload(node=node))
                    else:
                        yield _frame(emitter, SseEventType.NODE_EXITED, NodePayload(node=node))
                elif mode == _MODE_CUSTOM:
                    for frame in _custom_frames(emitter, payload):
                        yield frame

            snapshot = await graph.aget_state(thread_config(thread_id))
            completed = completed_payload(snapshot.values or {})
            if on_completed is not None:
                await on_completed(completed)
            yield _frame(emitter, SseEventType.RUN_COMPLETED, completed)
            logger.info("run.completed", extra={"event": "run.completed"})
        except AgentPlatformError as exc:
            logger.warning(
                "run.failed",
                extra={
                    "event": "run.failed",
                    "error_code": exc.code.value,
                    "error": type(exc).__name__,
                    "node": last_node,
                },
            )
            yield _frame(
                emitter,
                SseEventType.RUN_ERROR,
                RunErrorPayload(
                    code=exc.code.value,
                    message=exc.detail,
                    retryable=exc.retryable,
                    node=last_node,
                ),
            )
            return
        except Exception as exc:  # 见下方说明：不是吞，是转成流内错误帧
            # 未预期异常同样必须让客户端收到 ``run.error`` 后正常收流：此刻响应头早已发出，
            # 再往外抛只会得到 ``Caught handled exception, but response already started.``，
            # 客户端看到的是连接被掐断、没有任何错误说明（CLAUDE.md 8.4）。
            # 这不是 NO-06 禁止的"吞异常"——堆栈完整进受控日志，客户端拿到明确的错误码；
            # 被吞掉的信息量为零。
            #
            # 收窄成 ``Exception`` 而非裸 ``except``：``CancelledError`` 继承 ``BaseException``，
            # 客户端断开触发的取消必须照常向上传播（那是正常控制流，不是运行失败）。
            logger.exception(
                "run.failed_unexpected",
                extra={"event": "run.failed_unexpected", "error": type(exc).__name__},
            )
            yield _frame(
                emitter,
                SseEventType.RUN_ERROR,
                RunErrorPayload(
                    code=ErrorCode.INTERNAL_ERROR.value,
                    # 内部诊断信息（str(exc)）不出境：可能含拓扑细节与输入原文（NO-07 / 8.2）
                    message="服务内部错误，请稍后重试。",
                    retryable=True,
                    node=last_node,
                ),
            )
            return
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    # 只吞取消本身：此处是清理路径，客户端的断开不应再衍生出新错误
                    await pending
            with contextlib.suppress(RuntimeError, StopAsyncIteration):
                # 生成器可能已被异常或取消收尾，aclose 的重复关闭无需上抛
                await iterator.aclose()


def _frame(emitter: SseEmitter, event_type: SseEventType, payload: BaseModel) -> bytes:
    """按 ``core/streaming.py`` 的帧格式渲染一帧，并编码为字节。"""
    return emitter.emit(event_type, payload.model_dump(mode="json")).encode("utf-8")


def _as_async_generator(stream: AsyncIterator[Any]) -> AsyncGenerator[Any, None]:
    """把 ``astream`` 的返回值收窄为异步生成器。

    LangGraph 把 ``astream`` 标注为 ``AsyncIterator``，运行时返回的却是异步生成器。
    我们需要 ``aclose()``：客户端中途断开时，必须立刻把关闭信号传进图里，
    否则那个生成器会一直挂到 GC，期间图侧的清理与回调都不执行。

    用运行时检查而非 ``cast``：一旦上游换了实现，这里当场报错，
    而不是留下一个到 GC 才暴露的资源泄漏。
    """
    if not isinstance(stream, AsyncGenerator):
        raise ConfigurationError(f"astream 未返回异步生成器: {type(stream).__name__}")
    return stream


def _split_chunk(chunk: Any) -> tuple[str, Any]:
    """把 ``astream`` 的产出拆成 ``(mode, payload)``。

    Raises:
        ConfigurationError: 形状不是预期的 ``(mode, payload)`` 二元组。
            宁可立刻报错也不做兼容猜测——猜错的后果是整条事件流静默错位。
    """
    if isinstance(chunk, tuple) and len(chunk) == 2 and isinstance(chunk[0], str):
        return chunk[0], chunk[1]
    raise ConfigurationError(
        f"astream 产出形状异常: {type(chunk).__name__}",
        detail="服务内部错误，请稍后重试。",
    )


def _custom_frames(emitter: SseEmitter, payload: Any) -> Iterator[bytes]:
    """把节点推来的 ``{"event","data"}`` 渲染成帧。

    事件名在此复核一次：节点侧 ``AgentStreamEvent`` 已用 ``SseEventType`` 校验过，
    但 ``writer()`` 也接受裸 dict。复核能让"绕过模型直接写"的缺陷在流内立刻暴露，
    而不是把客户端不认识的事件名悄悄放行。

    Raises:
        ConfigurationError: 负载不是对象、缺 ``event``、事件名不在 ``SseEventType`` 中，
            或 ``data`` 不是对象。
    """
    if not isinstance(payload, dict):
        raise ConfigurationError(f"节点写入的流事件不是对象: {type(payload).__name__}")
    raw_event = payload.get("event")
    if not isinstance(raw_event, str):
        raise ConfigurationError(f"流事件缺少合法的 event 字段: {raw_event!r}")
    try:
        event_type = SseEventType(raw_event)
    except ValueError as exc:
        raise ConfigurationError(f"节点写入了未知的流事件类型: {raw_event!r}") from exc
    data = payload.get("data")
    if data is not None and not isinstance(data, dict):
        raise ConfigurationError(f"流事件 {event_type.value} 的 data 不是对象")
    yield emitter.emit(event_type, data or {}).encode("utf-8")


def replay_run_frames(
    *, cached: RunResponse, agent: str, thread_id: str, request_id: str
) -> Iterator[bytes]:
    """幂等命中时的**确定性重放**（CLAUDE.md 8.3）。

    SSE 无法逐字节复制当初那串增量，因此只重放两帧：``run.started`` 与
    ``run.completed``（内容取自缓存的权威终态）。调用方拿到的事件**序列形状不同**，
    但终态完全一致——这正是幂等承诺的边界，已在 CHANGELOG 与接口说明中写明，
    不假装是"完全一样的一次流式响应"。

    ``run_id`` 沿用当初那次运行的 ID：客户端的对账键因此保持稳定。
    """
    emitter = SseEmitter(run_id=cached.run_id, thread_id=thread_id, request_id=request_id)
    yield _frame(emitter, SseEventType.RUN_STARTED, RunStartedPayload(agent=agent))
    yield _frame(
        emitter,
        SseEventType.RUN_COMPLETED,
        RunCompletedPayload(
            answer=cached.answer,
            answer_status=cached.answer_status,
            citations=cached.citations,
            retrieval_meta=cached.retrieval_meta,
        ),
    )


__all__: list[str] = ["open_run_event_stream", "replay_run_frames"]
