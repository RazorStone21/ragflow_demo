"""会话资源路由（CLAUDE.md 8.1：语义对齐 LangGraph Server）。

**``POST /threads`` 不落任何数据**——它只按已认证的租户/用户签发一个可寻址的
``thread_id``。会话真正"存在"是在其首次 run 因 checkpoint 落库之时。

这是刻意的取舍：另建一张会话表会引入与图状态竞争的第二个状态源，
正是 CLAUDE.md 4.4 明令禁止的。代价是"刚创建就 GET"会 404，
因此把这条语义**写进响应模型的文档**（见 ``schemas/threads.py``），
而不是靠调用方猜。
"""

from __future__ import annotations

from fastapi import APIRouter

from agent_platform.api.deps import AuthDep, DepsDep, authorize_thread_id, require_existing_thread
from agent_platform.api.schemas.threads import ThreadResponse
from agent_platform.api.state_view import thread_response
from agent_platform.core.ids import compose_thread_id, new_session_id

router = APIRouter(prefix="/threads", tags=["threads"])


@router.post("", response_model=ThreadResponse, status_code=201, summary="创建会话")
async def create_thread(auth: AuthDep) -> ThreadResponse:
    """签发一个新会话 ID。

    租户段与用户段取自认证上下文，**绝不**接受调用方传入（NO-19）。
    """
    thread_id = compose_thread_id(auth.tenant_id, auth.user_id, new_session_id())
    return ThreadResponse(thread_id=thread_id)


@router.get("/{thread_id}", response_model=ThreadResponse, summary="查询会话")
async def get_thread(thread_id: str, deps: DepsDep, auth: AuthDep) -> ThreadResponse:
    """返回会话的检查点概况。

    Raises:
        NotFoundError: 会话不存在，或不属于当前认证上下文（两者同为 404，不作区分）。
    """
    authorize_thread_id(thread_id, auth)
    snapshot = await require_existing_thread(deps, thread_id)
    return thread_response(thread_id=thread_id, snapshot=snapshot)


__all__: list[str] = ["router"]
