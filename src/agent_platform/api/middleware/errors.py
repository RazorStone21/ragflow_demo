"""统一异常 → RFC 9457 Problem Details（CLAUDE.md 8.2）。

用 ``add_exception_handler`` 而非"中间件里 try/except"：Starlette 按**异常类的 MRO**
挑选最具体的处理器，而中间件里的 ``except`` 是手写 ``isinstance`` 链的翻版，
新增异常类型时容易漏改，且很难覆盖路由层自己抛出的 ``HTTPException``。

**响应体一律不回显原始异常文本**。``AgentPlatformError.detail`` 已经过设计（面向调用方），
而 422 的 ``exc.errors()``、未捕获异常的 ``str(exc)`` 都可能带出输入原文（PII）、
内部路径与堆栈——CLAUDE.md 3.6 / 8.2 / NO-07 明令禁止。
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from agent_platform.api.middleware.request_context import request_id_of
from agent_platform.core.errors import (
    AgentPlatformError,
    ErrorCode,
    ProblemDetail,
    ValidationFailedError,
)
from agent_platform.core.logging import get_logger

PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"
"""RFC 9457 响应媒体类型（CLAUDE.md 8.2）。"""

_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.VALIDATION_FAILED,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_FAILED,
}
"""Starlette 自身抛出的 ``HTTPException``（路由未命中、方法不允许）到平台错误码的映射。

未列举的 4xx（如 405）落到 ``VALIDATION_FAILED``：本平台对 4xx 的统一语义就是
"客户端请求不合法、修正后重试，服务端不重试"，新增状态码不值得再造一个错误码。
"""

logger = get_logger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    """注册全部异常处理器。"""
    app.add_exception_handler(AgentPlatformError, _handle_platform_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected_error)


def _problem_response(problem: ProblemDetail, status: int) -> Response:
    """把 Problem Details 渲染为响应（``application/problem+json``）。"""
    return Response(
        content=problem.model_dump_json(),
        status_code=status,
        media_type=PROBLEM_JSON_MEDIA_TYPE,
    )


def _scope_of(request: Request) -> MutableMapping[str, Any]:
    """取得请求的 ``scope``（``Request.scope`` 即 ASGI scope）。"""
    return request.scope


# 三个处理器都收 (Request, Exception) 宽签名、再在函数体内显式收窄：
# Starlette 的 add_exception_handler 统一要求该签名。此处不用 assert 或 cast——
# 收窄失败意味着注册表的 MRO 匹配出了漏子，那种情况应当按未预期异常出声，
# 而不是把非平台异常硬套成平台错误码。


async def _handle_platform_error(request: Request, exc: Exception) -> Response:
    """平台异常：错误码与 HTTP 状态由异常类自身携带。"""
    if not isinstance(exc, AgentPlatformError):
        return await _handle_unexpected_error(request, exc)
    return _problem_response(
        exc.to_problem_details(
            request_id=request_id_of(_scope_of(request)), instance=request.url.path
        ),
        exc.http_status,
    )


async def _handle_request_validation_error(request: Request, exc: Exception) -> Response:
    """请求体/查询参数校验失败（422）。

    校验明细**不进响应体**：``exc.errors()`` 的 ``input`` 字段会原样带回用户输入，
    其中可能含 PII。明细只进受控日志（CLAUDE.md 3.7 / 10.1）。
    """
    if not isinstance(exc, RequestValidationError):
        return await _handle_unexpected_error(request, exc)
    error = ValidationFailedError(
        f"请求校验失败（{len(exc.errors())} 处）",
        detail="请求参数不合法，请检查字段类型、必填项与取值范围。",
    )
    logger.info(
        "http.validation_failed",
        extra={"event": "http.validation_failed", "error_count": len(exc.errors())},
    )
    return _problem_response(
        error.to_problem_details(
            request_id=request_id_of(_scope_of(request)), instance=request.url.path
        ),
        error.http_status,
    )


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    """Starlette 自身抛出的 ``HTTPException``（路由未命中、方法不允许等）。"""
    if not isinstance(exc, StarletteHTTPException):
        return await _handle_unexpected_error(request, exc)
    code = _STATUS_TO_CODE.get(
        exc.status_code,
        ErrorCode.INTERNAL_ERROR if exc.status_code >= 500 else ErrorCode.VALIDATION_FAILED,
    )
    detail = exc.detail if isinstance(exc.detail, str) else "请求无法处理。"
    problem = ProblemDetail(
        type=f"urn:agent-platform:error:{code.value.lower().replace('_', '-')}",
        title=code.value,
        status=exc.status_code,
        detail=detail,
        instance=request.url.path,
        code=code.value,
        request_id=request_id_of(_scope_of(request)),
    )
    return _problem_response(problem, exc.status_code)


async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
    """兜底：未预期异常一律 500，详情只进日志（CLAUDE.md 3.6 / 8.2）。

    注意 Starlette 的 ``ServerErrorMiddleware`` 在调用本处理器之后**仍会重新抛出**
    异常，以便 ASGI 服务器记录。因此单测必须用
    ``httpx.ASGITransport(app=..., raise_app_exceptions=False)``，
    否则断言到的是异常而不是这份响应。
    """
    logger.exception(
        "http.unhandled_error",
        extra={"event": "http.unhandled_error", "error": type(exc).__name__},
    )
    problem = ProblemDetail(
        type="urn:agent-platform:error:internal-error",
        title=ErrorCode.INTERNAL_ERROR.value,
        status=500,
        detail="服务内部错误，请稍后重试。",
        instance=request.url.path,
        code=ErrorCode.INTERNAL_ERROR.value,
        request_id=request_id_of(_scope_of(request)),
    )
    return _problem_response(problem, 500)


__all__: list[str] = ["PROBLEM_JSON_MEDIA_TYPE", "register_exception_handlers"]
