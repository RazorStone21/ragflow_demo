"""标识符生成与解析（CLAUDE.md 4.4 / 8.1）。

- ``thread_id`` 采用多租户命名空间：``"{tenant_id}:{user_id}:{session_id}"``，
  长度必须小于 255（langgraph-checkpoint-postgres 限制）。
- ``run_id`` / ``request_id`` 均为 UUID4 字符串。
"""

from __future__ import annotations

import re
import uuid

THREAD_ID_SEPARATOR = ":"
THREAD_ID_MAX_LEN = 255

_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class InvalidThreadIdError(ValueError):
    """thread_id 格式非法。"""


def new_run_id() -> str:
    """生成新的 run_id（UUID4）。"""
    return str(uuid.uuid4())


def new_request_id() -> str:
    """生成新的 request_id（UUID4）。"""
    return str(uuid.uuid4())


def new_session_id() -> str:
    """生成新的 session_id（UUID4，作为 thread_id 的第三段）。"""
    return str(uuid.uuid4())


def compose_thread_id(tenant_id: str, user_id: str, session_id: str) -> str:
    """拼装多租户 thread_id。

    Args:
        tenant_id: 租户标识（来自认证上下文，不得由前端直接决定，NO-19）。
        user_id: 用户标识。
        session_id: 会话标识。

    Returns:
        ``"{tenant_id}:{user_id}:{session_id}"``。

    Raises:
        InvalidThreadIdError: 任一段非法（空/含非法字符/超长）。
    """
    for name, value in (("tenant_id", tenant_id), ("user_id", user_id), ("session_id", session_id)):
        if not _SEGMENT_PATTERN.match(value):
            raise InvalidThreadIdError(
                f"非法的 {name}: {value!r}（仅允许字母/数字/._-，长度 1-64）"
            )
    # 段限 64 已保证总长 ≤ 194 < 255，此处无需再做总长校验
    return THREAD_ID_SEPARATOR.join((tenant_id, user_id, session_id))


def parse_thread_id(thread_id: str) -> tuple[str, str, str]:
    """解析 ``thread_id`` 为 ``(tenant_id, user_id, session_id)``。

    Raises:
        InvalidThreadIdError: 超长、段数不足、含空段或含非法字符。
    """
    # 外部传入的 thread_id 不做 compose 前置约束，需独立校验长度上限
    if len(thread_id) > THREAD_ID_MAX_LEN:
        raise InvalidThreadIdError(f"thread_id 超长（{len(thread_id)} > {THREAD_ID_MAX_LEN}）")
    parts = thread_id.split(THREAD_ID_SEPARATOR)
    if len(parts) != 3 or not all(parts):
        raise InvalidThreadIdError(f"非法 thread_id: {thread_id!r}（期望 tenant:user:session）")
    for name, value in zip(("tenant_id", "user_id", "session_id"), parts, strict=True):
        if not _SEGMENT_PATTERN.match(value):
            raise InvalidThreadIdError(f"非法的 {name}: {value!r}")
    return parts[0], parts[1], parts[2]
