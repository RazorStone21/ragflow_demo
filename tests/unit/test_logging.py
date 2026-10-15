"""结构化日志与敏感信息脱敏（CLAUDE.md 3.7 / 10.1）。"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from agent_platform.core.logging import StructuredJsonFormatter, log_context


def _render(record: logging.LogRecord) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(StructuredJsonFormatter().format(record)))


def test_renders_single_line_json_with_standard_fields() -> None:
    """输出单行 JSON，含 timestamp/level/message 等标准字段。"""
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="thread created",
        args=(),
        exc_info=None,
    )
    payload = _render(record)

    assert payload["level"] == "INFO"
    assert payload["message"] == "thread created"
    assert str(payload["timestamp"]).endswith("Z")  # UTC、ISO 8601
    assert payload["logger"] == "test.logger"


def test_context_fields_injected_via_contextvar() -> None:
    """request_id/thread_id/tenant_id/run_id 经上下文注入到输出。"""
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="x",
        args=(),
        exc_info=None,
    )
    with log_context(request_id="req-9", thread_id="t:u:s", tenant_id="tenant_a"):
        payload = _render(record)

    assert payload["request_id"] == "req-9"
    assert payload["thread_id"] == "t:u:s"
    assert payload["tenant_id"] == "tenant_a"


def test_context_restored_after_block() -> None:
    """离开上下文后字段不残留（避免跨请求串号）。"""
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="x",
        args=(),
        exc_info=None,
    )
    with log_context(request_id="req-a"):
        pass
    assert "request_id" not in _render(record)


def test_sensitive_extra_keys_are_redacted() -> None:
    """键名含 key/token/secret/password 的额外字段不得落盘。"""
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="gateway call",
        args=(),
        exc_info=None,
    )
    record.api_key = "sk-real-secret-value"  # 模拟第三方库写入的敏感 extra 字段
    record.duration_ms = 12.5

    payload = _render(record)

    assert payload["api_key"] == "[REDACTED]"
    assert "sk-real-secret-value" not in json.dumps(payload)
    assert payload["duration_ms"] == 12.5
