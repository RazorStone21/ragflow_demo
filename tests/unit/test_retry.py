"""重试策略边界（CLAUDE.md 3.6）：仅超时/429/5xx 重试，4xx/鉴权/护栏不重试。"""

from __future__ import annotations

import httpx
import pytest

from agent_platform.core.errors import (
    GuardrailBlockedError,
    ModelRateLimitedError,
    ModelTimeoutError,
    NotFoundError,
    UnauthorizedError,
)
from agent_platform.core.retry import RETRYABLE_HTTP_STATUS, build_retry, is_retryable


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://example.invalid/api")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"status {status_code}", request=request, response=response)


class TimeErrorWithStatus(Exception):  # noqa: N818  # 模拟第三方 SDK 的异常类名，不入本仓异常层级
    """模拟第三方 SDK 异常（带 status_code 属性）。"""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ModelTimeoutError("t"), True),
        (ModelRateLimitedError("r"), True),
        (httpx.ConnectTimeout("timeout"), True),
        (httpx.ConnectError("conn reset"), True),
        (httpx.ReadTimeout("read timeout"), True),
        (_http_status_error(503), True),
        (TimeErrorWithStatus(500), True),  # 第三方 SDK 风格（带 status_code）
        (_http_status_error(400), False),
        (_http_status_error(401), False),
        (_http_status_error(403), False),
        (_http_status_error(404), False),
        (UnauthorizedError(), False),
        (NotFoundError("n"), False),
        (GuardrailBlockedError("g"), False),
        (ValueError("bad input"), False),
    ],
)
def test_is_retryable_matrix(exc: BaseException, expected: bool) -> None:
    """可重试判定矩阵。"""
    assert is_retryable(exc) is expected


def test_retryable_status_set_excludes_client_errors() -> None:
    """4xx 客户端错误不得出现在可重试集合中（429 除外）。"""
    for status in (400, 401, 403, 404, 409, 422):
        assert status not in RETRYABLE_HTTP_STATUS
    assert 429 in RETRYABLE_HTTP_STATUS


async def test_retry_is_bounded_and_reraises() -> None:
    """重试次数有上限；耗尽后重抛原异常（不得静默吞掉）。"""
    attempts = 0

    @build_retry(max_attempts=3, initial_wait_s=0.001, max_wait_s=0.01)
    async def flaky() -> None:
        nonlocal attempts
        attempts += 1
        raise ModelTimeoutError("still timing out")

    with pytest.raises(ModelTimeoutError):
        await flaky()
    assert attempts == 3


async def test_no_retry_for_client_error() -> None:
    """不可重试异常只尝试一次。"""
    attempts = 0

    @build_retry(max_attempts=5, initial_wait_s=0.001, max_wait_s=0.01)
    async def client_error() -> None:
        nonlocal attempts
        attempts += 1
        raise UnauthorizedError()

    with pytest.raises(UnauthorizedError):
        await client_error()
    assert attempts == 1


async def test_success_after_transient_failure() -> None:
    """瞬时失败后成功返回。"""
    attempts = 0

    @build_retry(max_attempts=3, initial_wait_s=0.001, max_wait_s=0.01)
    async def flaky_then_ok() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise ModelRateLimitedError("429")
        return "ok"

    assert await flaky_then_ok() == "ok"
    assert attempts == 2
