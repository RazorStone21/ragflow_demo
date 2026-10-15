"""多租户 thread_id 组装与解析（CLAUDE.md 4.4）。"""

from __future__ import annotations

import uuid

import pytest

from agent_platform.core.ids import (
    THREAD_ID_MAX_LEN,
    InvalidThreadIdError,
    compose_thread_id,
    new_request_id,
    new_run_id,
    new_session_id,
    parse_thread_id,
)


def test_compose_and_parse_roundtrip() -> None:
    """组装与解析互逆。"""
    thread_id = compose_thread_id("tenant_a", "user_1", "sess-42")
    assert thread_id == "tenant_a:user_1:sess-42"
    assert parse_thread_id(thread_id) == ("tenant_a", "user_1", "sess-42")


@pytest.mark.parametrize(
    ("tenant", "user", "session"),
    [
        ("", "u", "s"),  # 空段
        ("tenant", "user", "sess with space"),  # 非法字符
        ("tenant", "user", "sess:colon"),  # 冒号会破坏分隔
        ("t" * 65, "u", "s"),  # 单段超长
    ],
)
def test_invalid_segments_rejected(tenant: str, user: str, session: str) -> None:
    """非法段（空/含特殊字符/超长）一律拒绝。"""
    with pytest.raises(InvalidThreadIdError):
        compose_thread_id(tenant, user, session)


def test_composed_thread_id_stays_within_limit() -> None:
    """段长上限（64）保证总长不超过 postgres checkpointer 的 255 限制。"""
    longest = compose_thread_id("t" * 64, "u" * 64, "s" * 64)
    assert len(longest) <= THREAD_ID_MAX_LEN


def test_parse_rejects_malformed() -> None:
    """解析非法 thread_id 抛错（段数/空段/非法字符/超长）。"""
    for bad in ("onlyone", "a:b", "a::c", ":b:c", "a:b:c:d", "t" * 300 + ":b:c", "a:b:c d"):
        with pytest.raises(InvalidThreadIdError):
            parse_thread_id(bad)


def test_generated_ids_are_uuid4_strings() -> None:
    """run_id/request_id/session_id 均为合法 UUID4 字符串。"""
    for generator in (new_run_id, new_request_id, new_session_id):
        value = generator()
        assert uuid.UUID(value).version == 4
