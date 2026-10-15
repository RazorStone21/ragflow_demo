"""有界重试与退避策略（CLAUDE.md 3.6）。

规则：
- 仅对**幂等且可重试**的错误重试：超时、429、5xx、连接中断。
- 对 4xx（含 401/403）、护栏拦截、参数错误**不得**重试。
- 指数退避 + 抖动 + 最大次数上限。

用法::

    retry = build_retry(max_attempts=3)
    result = await retry(call_gateway)(payload)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import httpx
import tenacity

from agent_platform.core.errors import AgentPlatformError

RETRYABLE_HTTP_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
"""可重试的 HTTP 状态码（客户端错误不重试，CLAUDE.md 3.6）。"""

_RETRYABLE_NAME_HINTS = ("timeout", "ratelimit", "rate_limit", "connection")

R = TypeVar("R")


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否可重试。

    判定顺序：
    1. 平台自身异常：以异常类上的 ``retryable`` 属性为准。
    2. ``httpx`` 超时/连接类异常：可重试。
    3. 带 ``status_code``/``response`` 的异常：状态码在 ``RETRYABLE_HTTP_STATUS`` 内才重试。
    4. 类名含 timeout/ratelimit/connection 的第三方异常（如 openai SDK 的
       ``APITimeoutError``/``APIConnectionError``）：可重试。
    其余一律不可重试。
    """
    if isinstance(exc, AgentPlatformError):
        return exc.retryable
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_HTTP_STATUS
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in RETRYABLE_HTTP_STATUS
    name = type(exc).__name__.lower()
    return any(hint in name for hint in _RETRYABLE_NAME_HINTS)


def build_retry(
    max_attempts: int,
    *,
    initial_wait_s: float = 0.5,
    max_wait_s: float = 20.0,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """构造 tenacity 重试装饰器（指数退避 + 抖动 + 有界）。

    Args:
        max_attempts: 总尝试次数（含首次；<1 时按 1 处理）。
        initial_wait_s: 首次退避基准秒数。
        max_wait_s: 单次退避上限秒数。

    Returns:
        可用于同步或异步函数的 ``tenacity`` 重试装饰器；重试耗尽后重抛原异常
        （``reraise=True``），由调用方转换为平台异常。
    """
    return tenacity.retry(
        retry=tenacity.retry_if_exception(is_retryable),
        wait=tenacity.wait_exponential_jitter(
            multiplier=initial_wait_s, max=max_wait_s, jitter=initial_wait_s
        ),
        stop=tenacity.stop_after_attempt(max(1, max_attempts)),
        reraise=True,
    )
