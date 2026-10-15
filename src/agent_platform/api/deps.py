"""HTTP 依赖注入：鉴权、租户上下文与检查点读取（CLAUDE.md 2.2 / 8.1 / 10.2）。

安全边界（CLAUDE.md 10.2 / NO-19）——本模块的全部职责可以概括成三句：

1. ``tenant_id`` / ``user_id`` **只**来自认证结果。请求体里没有这两个字段
   （``RunRequest`` 是 ``extra="forbid"``），也**不读取**任何前端传来的租户标识头。
2. ``thread_id`` 的租户段与用户段必须与认证上下文一致，否则按 **404** 处理而非 403——
   403 等于告诉探测者"这个会话存在但你没权限"，那是一个跨租户存在性预言机。
3. 图中节点通过 ``config["configurable"]["tenant_id"]`` 读租户
   （``tenants.context.tenant_id_from_config`` 读不到即 401），
   因此本模块是**唯一**写入该字段的地方。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from langchain_core.runnables import RunnableConfig
from langgraph.types import StateSnapshot

from agent_platform.api.container import AppDeps
from agent_platform.core.errors import ConfigurationError, NotFoundError, UnauthorizedError
from agent_platform.core.ids import InvalidThreadIdError, parse_thread_id
from agent_platform.tenants.context import TENANT_ID_CONFIG_KEY

_BEARER_SCHEME = "bearer"


@dataclass(frozen=True)
class AuthContext:
    """已认证的调用方身份。

    演示期为"静态 Token → 固定租户"（``demo_tenant_id``/``demo_user_id``），
    接入真实身份体系后只需替换 ``require_auth``，本模块其余部分与路由不受影响。
    """

    tenant_id: str
    user_id: str


def get_deps(request: Request) -> AppDeps:
    """取出 lifespan 注入的依赖容器。

    Raises:
        ConfigurationError: 应用未完成启动（lifespan 未运行或已关停）。
            归为 5xx 是正确的：这是服务端装配问题，不是调用方的错。
    """
    deps = getattr(request.app.state, "deps", None)
    if not isinstance(deps, AppDeps):
        raise ConfigurationError(
            "应用依赖不可用", detail="服务尚未完成启动或正在关停，请稍后重试。"
        )
    return deps


async def require_auth(request: Request) -> AuthContext:
    """校验 Bearer Token 并解析出已认证的租户上下文（CLAUDE.md 8.1 / 10.2）。

    Raises:
        UnauthorizedError: 凭据缺失、格式非法或与配置不符（401，不得重试）。
    """
    deps = get_deps(request)
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    token = credential.strip()
    if scheme.lower() != _BEARER_SCHEME or not token:
        # "缺失凭据"与"格式不对"合并为同一个错误：两者都只是没带对凭据，
        # 而分开报错只会帮攻击者确认服务端的解析规则。
        raise UnauthorizedError()

    expected = deps.settings.agent_api_bearer_token.get_secret_value()
    # 定长比较：普通的 == 会逐字节短路，把 Token 前缀逐位泄露出去。
    # 两侧都编码为 bytes——compare_digest 对 str 只接受 ASCII，bytes 一劳永逸。
    if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise UnauthorizedError()

    return AuthContext(tenant_id=deps.settings.demo_tenant_id, user_id=deps.settings.demo_user_id)


def thread_config(thread_id: str) -> RunnableConfig:
    """构造只读会话配置（读 state / history 用，不含租户）。"""
    return {"configurable": {"thread_id": thread_id}}


def checkpoint_config(thread_id: str, checkpoint_id: str) -> RunnableConfig:
    """构造指向特定检查点的配置（history 分页游标用）。

    与 ``thread_config`` 同样只读；多出的 ``checkpoint_id`` 让
    ``aget_state_history(before=...)`` 从该点往前翻页。
    """
    return {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}}


def run_config(*, thread_id: str, tenant_id: str) -> RunnableConfig:
    """构造运行配置。

    ``tenant_id`` 必须来自 ``AuthContext``（NO-19）；它只走 ``configurable``，
    不进 State，因此既不会被 checkpoint 持久化，也不会被后续 run 覆盖。
    """
    return {"configurable": {"thread_id": thread_id, TENANT_ID_CONFIG_KEY: tenant_id}}


def authorize_thread_id(thread_id: str, auth: AuthContext) -> None:
    """校验会话归属：格式非法或租户/用户段不匹配，一律 404。

    Raises:
        NotFoundError: 格式非法，或 tenant/user 段与认证上下文不一致。
            两种情况给同一个响应，跨租户探测便无法区分"不存在"与"无权限"。
    """
    try:
        tenant_id, user_id, _session_id = parse_thread_id(thread_id)
    except InvalidThreadIdError as exc:
        raise NotFoundError("会话不存在") from exc
    if tenant_id != auth.tenant_id or user_id != auth.user_id:
        raise NotFoundError("会话不存在")


async def load_snapshot(deps: AppDeps, thread_id: str) -> StateSnapshot:
    """读取最新检查点。

    未知会话返回 ``created_at is None`` 的空快照（LangGraph 的既定行为），
    不抛异常——调用方自行决定这是 404 还是"允许开跑"。
    """
    return await deps.graph.aget_state(thread_config(thread_id))


async def require_existing_thread(deps: AppDeps, thread_id: str) -> StateSnapshot:
    """读取最新检查点，会话不存在则 404。

    Raises:
        NotFoundError: 该会话尚无任何检查点（含"刚 POST /threads 创建、还没跑过"）。
    """
    snapshot = await load_snapshot(deps, thread_id)
    if snapshot.created_at is None:
        raise NotFoundError("会话不存在")
    return snapshot


DepsDep = Annotated[AppDeps, Depends(get_deps)]
AuthDep = Annotated[AuthContext, Depends(require_auth)]


__all__: list[str] = [
    "AuthContext",
    "AuthDep",
    "DepsDep",
    "authorize_thread_id",
    "checkpoint_config",
    "get_deps",
    "load_snapshot",
    "require_auth",
    "require_existing_thread",
    "run_config",
    "thread_config",
]
