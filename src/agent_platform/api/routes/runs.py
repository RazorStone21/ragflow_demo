"""运行与状态路由（CLAUDE.md 8.1 / 8.3 / 8.4）。

四条语义对齐 LangGraph Server 的路径，外加一条流式路径：

- ``POST /threads/{id}/runs``         非流式，一次拿完整结果
- ``POST /threads/{id}/runs/stream``  流式（SSE，CLAUDE.md 8.4）
- ``GET  /threads/{id}/state``        最新状态
- ``POST /threads/{id}/history``      检查点分页历史

``thread_id`` 的归属在**每个**端点入口都校验一次（``authorize_thread_id``）：
授权不放在中间件里，是因为"会话属于谁"要结合路径参数判断，
放在此处可见、可测，也不给后续新增端点留下遗忘的空档。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from sse_starlette import EventSourceResponse

from agent_platform.agents.gov_policy_qa.state import initial_state
from agent_platform.agents.registry import get_agent
from agent_platform.api.container import AppDeps
from agent_platform.api.deps import (
    AuthContext,
    AuthDep,
    DepsDep,
    authorize_thread_id,
    checkpoint_config,
    require_existing_thread,
    run_config,
    thread_config,
)
from agent_platform.api.idempotency import (
    lookup_run,
    read_idempotency_key,
    remember_run,
)
from agent_platform.api.middleware.request_context import request_id_of
from agent_platform.api.schemas.events import RunCompletedPayload
from agent_platform.api.schemas.runs import (
    HistoryItem,
    HistoryRequest,
    HistoryResponse,
    RunRequest,
    RunResponse,
    StateResponse,
)
from agent_platform.api.sse import open_run_event_stream, replay_run_frames
from agent_platform.api.state_view import history_item, run_response, state_response
from agent_platform.core.errors import NotFoundError, ValidationFailedError
from agent_platform.core.ids import new_request_id, new_run_id

router = APIRouter(prefix="/threads/{thread_id}", tags=["runs"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # 反向代理（Nginx 等）默认会缓冲响应体，那会把 SSE 变成"攒够了一次性吐出"。
    # 该头是 Nginx 认的关缓冲开关；再往前还有 LB 时需同样配置。
    "X-Accel-Buffering": "no",
}
"""流式响应附加头。``Connection`` 属于逐跳头，交由 ASGI 服务器自行管理，此处不设。"""


def _authorize_run(body: RunRequest, thread_id: str, deps: AppDeps, auth: AuthContext) -> None:
    """运行请求的统一前置校验（三个端点共用，避免规则漂移）。

    Raises:
        NotFoundError: 会话不属于当前上下文，或智能体未注册/未在本进程装配。
        ValidationFailedError: 提问超长。
    """
    authorize_thread_id(thread_id, auth)
    get_agent(body.agent)  # 未注册即 404，清单以 agents/registry.py 为唯一来源
    if body.agent != deps.agent_name:
        # 本进程只装配了一个图。若不拦住，指名"另一个已注册智能体"会**静默**跑成
        # 当前这个图——注册表与装配不一致属于部署缺陷，必须显式暴露而不是将错就错。
        # detail 必须显式给出：基类默认值是"服务暂时不可用，请稍后重试"，
        # 对 404 而言那是**误导**——这个错误重试一千次也不会变，得改配置或改请求。
        raise NotFoundError(
            f"智能体未在本进程装配: {body.agent}",
            detail=f"当前部署未提供智能体 {body.agent}，请确认智能体名称。",
        )
    if len(body.message) > deps.settings.max_question_chars:
        raise ValidationFailedError(
            f"提问超长（{len(body.message)} 字符）",
            detail=f"单次提问不得超过 {deps.settings.max_question_chars} 个字符。",
        )


async def _terminal_values(
    deps: AppDeps, *, body: RunRequest, thread_id: str, tenant_id: str
) -> dict[str, Any]:
    """非流式执行一次运行，返回终态字段。"""
    values = await deps.graph.ainvoke(
        initial_state(body.message), run_config(thread_id=thread_id, tenant_id=tenant_id)
    )
    return values if isinstance(values, dict) else {}


@router.post("/runs", response_model=RunResponse, summary="发起一次运行（非流式）")
async def create_run(
    body: RunRequest,
    thread_id: str,
    request: Request,
    deps: DepsDep,
    auth: AuthDep,
) -> RunResponse:
    """同步执行一次运行并返回终态。

    提供了 ``Idempotency-Key`` 且命中缓存时直接返回首次结果，**不重复执行**
    （CLAUDE.md 8.3）——图节点带外部副作用，重放不是免费的。
    """
    _authorize_run(body, thread_id, deps, auth)
    idem_key = read_idempotency_key(request)
    if idem_key is not None:
        cached = await lookup_run(deps.store, tenant_id=auth.tenant_id, key=idem_key)
        if cached is not None:
            return cached

    run_id = new_run_id()
    values = await _terminal_values(deps, body=body, thread_id=thread_id, tenant_id=auth.tenant_id)
    result = run_response(run_id=run_id, thread_id=thread_id, values=values)
    if idem_key is not None:
        await remember_run(deps.store, tenant_id=auth.tenant_id, key=idem_key, response=result)
    return result


@router.post(
    "/runs/stream",
    response_class=EventSourceResponse,
    summary="发起一次运行（SSE 流式）",
)
async def stream_run(
    body: RunRequest,
    thread_id: str,
    request: Request,
    deps: DepsDep,
    auth: AuthDep,
) -> EventSourceResponse:
    """以 SSE 流式执行一次运行（事件类型见 CLAUDE.md 8.4）。

    ``ping=0`` 关掉 sse-starlette 自带心跳，改由 ``core/streaming.py`` 的
    ``heartbeat_frame()`` 按 ``sse_heartbeat_s`` 发送：平台的心跳格式只有一个来源。

    ``Idempotency-Key`` 命中时做**确定性重放**（``run.started`` + ``run.completed``）：
    SSE 无法逐字节复现当初那串增量，但终态一致——这是幂等承诺的边界，不粉饰。
    """
    _authorize_run(body, thread_id, deps, auth)
    request_id = request_id_of(request.scope) or new_request_id()
    idem_key = read_idempotency_key(request)
    if idem_key is not None:
        cached = await lookup_run(deps.store, tenant_id=auth.tenant_id, key=idem_key)
        if cached is not None:
            return EventSourceResponse(
                replay_run_frames(
                    cached=cached, agent=body.agent, thread_id=thread_id, request_id=request_id
                ),
                ping=0,
                headers=_SSE_HEADERS,
            )

    run_id = new_run_id()
    recorder: Callable[[RunCompletedPayload], Awaitable[None]] | None = None
    if idem_key is not None:
        # 取局部非空别名：闭包体内的类型窄化 mypy 保不住（外层变量可能被改），
        # 直接捕获 idem_key 会被判为 str | None。
        record_key = idem_key

        async def _record(payload: RunCompletedPayload) -> None:
            await remember_run(
                deps.store,
                tenant_id=auth.tenant_id,
                key=record_key,
                response=RunResponse(
                    run_id=run_id,
                    thread_id=thread_id,
                    answer=payload.answer,
                    answer_status=payload.answer_status,
                    citations=payload.citations,
                    retrieval_meta=payload.retrieval_meta,
                ),
            )

        recorder = _record

    return EventSourceResponse(
        open_run_event_stream(
            graph=deps.graph,
            body=body,
            thread_id=thread_id,
            tenant_id=auth.tenant_id,
            request_id=request_id,
            run_id=run_id,
            heartbeat_s=deps.settings.sse_heartbeat_s,
            on_completed=recorder,
        ),
        ping=0,
        headers=_SSE_HEADERS,
    )


@router.get("/state", response_model=StateResponse, summary="读取会话最新状态")
async def get_state(thread_id: str, deps: DepsDep, auth: AuthDep) -> StateResponse:
    """返回最新检查点：LangGraph 语义字段（next/has_interrupt 等）+ 业务终态字段。"""
    authorize_thread_id(thread_id, auth)
    snapshot = await require_existing_thread(deps, thread_id)
    return state_response(thread_id=thread_id, snapshot=snapshot)


@router.post("/history", response_model=HistoryResponse, summary="分页读取检查点历史")
async def get_history(
    body: HistoryRequest, thread_id: str, deps: DepsDep, auth: AuthDep
) -> HistoryResponse:
    """按 checkpoint 由新到旧分页返回。

    未知会话返回 **404 而非空列表**：空列表会让调用方以为"这个会话跑过但没有记录"，
    而真实情况是"你无权访问或它会话不存在"。
    """
    authorize_thread_id(thread_id, auth)
    await require_existing_thread(deps, thread_id)

    before = checkpoint_config(thread_id, body.before) if body.before else None
    items: list[HistoryItem] = []
    next_cursor: str | None = None
    # 多取一条来判断"是否还有下一页"：LangGraph 的迭代器不提供总数，
    # 只看本页是否取满会把"刚好取满"误判成"还有更多"。
    async for snapshot in deps.graph.aget_state_history(
        thread_config(thread_id), before=before, limit=body.limit + 1
    ):
        if len(items) >= body.limit:
            next_cursor = items[-1].checkpoint_id or None
            break
        items.append(history_item(snapshot))
    return HistoryResponse(items=items, next_cursor=next_cursor)


__all__: list[str] = ["router"]
