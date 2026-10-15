"""请求上下文：request_id 分配/回传、日志上下文注入、访问日志（CLAUDE.md 8.1 / 3.7）。

**纯 ASGI 中间件，不是 ``BaseHTTPMiddleware``**。原因不是风格偏好：
``BaseHTTPMiddleware`` 会把下游调用扔进独立的 anyio task，``contextvars`` 因此**不会**
跨过去——``log_context`` 注入的 request_id 在端点里读不到，全链路日志关联直接断掉。
纯 ASGI 中间件就在同一个 task 里 ``await`` 下游，contextvar 原样可见。

本中间件同时是请求生命周期的**唯一**起点：``scope`` 上写入的 request_id 会被
错误处理器与 SSE 生成器读取，因此不要试图在别处再生成一个。
"""

from __future__ import annotations

import re
import time
from collections.abc import MutableMapping
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_platform.core.ids import new_request_id
from agent_platform.core.logging import get_logger, log_context

REQUEST_ID_HEADER = "x-request-id"
REQUEST_ID_SCOPE_KEY = "agent_platform.request_id"
"""``scope`` 中存放 request_id 的键（错误处理器与 SSE 生成器据此取用）。"""

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
"""调用方自带 request_id 的白名单。

只放行不含空白/控制字符的短 ASCII 串：这个值会进日志、进响应头、进 SSE 帧，
不设限就等于把日志注入与响应头注入的口子留给调用方。
"""

logger = get_logger(__name__)


def request_id_of(scope: MutableMapping[str, Any]) -> str | None:
    """从 ``scope`` 取出本次请求的 request_id（未经过中间件时为 ``None``）。"""
    value = scope.get(REQUEST_ID_SCOPE_KEY)
    return value if isinstance(value, str) else None


class RequestContextMiddleware:
    """为每个 HTTP 请求分配 request_id，注入日志上下文，并在结束时记录访问日志。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # lifespan / websocket 直接透传：前者没有请求语义，后者本平台未启用
            await self.app(scope, receive, send)
            return

        request_id = _resolve_request_id(scope)
        scope[REQUEST_ID_SCOPE_KEY] = request_id
        observed_status: list[int] = []
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                observed_status.append(message["status"])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        with log_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            finally:
                # 放在 finally：客户端中途断开时异常会穿透这里，
                # 但"这次请求发生过"的事实仍然要留痕，否则访问日志会莫名缺行。
                _log_access(scope, request_id=request_id, status=observed_status, started=started)


def _resolve_request_id(scope: MutableMapping[str, Any]) -> str:
    """取调用方自带且合法的 request_id，否则生成新的（CLAUDE.md 8.1）。"""
    incoming = _header_value(scope, REQUEST_ID_HEADER)
    if incoming is not None and _REQUEST_ID_PATTERN.match(incoming):
        return incoming
    return new_request_id()


def _header_value(scope: MutableMapping[str, Any], name: str) -> str | None:
    """按名取原始请求头（大小写不敏感）；不存在返回 ``None``。

    直接用 ``scope["headers"]`` 而非 ``Headers``：本中间件在 Starlette 的
    路由之前运行，此时还没有可用的 ``Request`` 对象。
    """
    target = name.lower().encode("latin-1")
    # 显式标注：scope 是 Any，不标注的话 key/value 会退化成 Any，
    # 返回值就被 mypy 判为 Any 而非本函数声明的 str | None。
    headers: list[tuple[bytes, bytes]] = scope.get("headers") or []
    for key, value in headers:
        if key.lower() == target:
            # latin-1 对任意字节序列都不会抛错；非 ASCII 会变成乱码，
            # 随后被 _REQUEST_ID_PATTERN 拒掉——这正是我们要的结果。
            return value.decode("latin-1")
    return None


def _log_access(
    scope: MutableMapping[str, Any], *, request_id: str, status: list[int], started: float
) -> None:
    """记录一条访问日志。

    ``status`` 为空表示响应头从未发出（应用在流式响应开始前就崩了），按 500 记。
    """
    logger.info(
        "http.request_completed",
        extra={
            "event": "http.request_completed",
            "request_id": request_id,
            "method": scope.get("method"),
            "path": scope.get("path"),
            "status": status[0] if status else 500,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )


__all__: list[str] = ["REQUEST_ID_HEADER", "REQUEST_ID_SCOPE_KEY", "RequestContextMiddleware"]
