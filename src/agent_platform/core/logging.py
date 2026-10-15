"""结构化 JSON 日志（CLAUDE.md 3.7）。

- 统一字段：``timestamp/level/request_id/thread_id/tenant_id/run_id/agent/node/event/message``。
- 禁止 ``print``（ruff T20 强制）；禁止记录密钥、系统提示词全文与未脱敏 PII。
- 请求级上下文经 ``contextvars`` 注入，贯穿 request_id/thread_id/tenant_id/run_id。

用法::

    configure_logging(settings)            # 应用启动时调用一次
    with log_context(request_id=rid, tenant_id=tid):
        get_logger(__name__).info("thread created", extra={"event": "thread.created"})
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_CONTEXT_KEYS = ("request_id", "thread_id", "tenant_id", "run_id", "agent", "node")
_MISSING = "-"

_context: dict[str, ContextVar[str]] = {
    key: ContextVar(key, default=_MISSING) for key in _CONTEXT_KEYS
}

# 键名命中即视为敏感值，不落日志（CLAUDE.md 3.7 / 10.1）
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(key|token|secret|password|passwd|dsn|authorization|bearer)", re.I
)


class StructuredJsonFormatter(logging.Formatter):
    """将日志记录渲染为单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _CONTEXT_KEYS:
            value = getattr(record, key, None) or _context[key].get()
            if value != _MISSING:
                payload[key] = value
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event
        payload.update(self._safe_extras(record))
        if record.exc_info:
            # 堆栈仅进受控日志，不进对外响应（CLAUDE.md 3.6）
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def _safe_extras(record: logging.LogRecord) -> dict[str, Any]:
        standard = (
            set(logging.makeLogRecord({}).__dict__) | set(_CONTEXT_KEYS) | {"event", "message"}
        )
        extras: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key in standard or key.startswith("_"):
                continue
            extras[key] = "[REDACTED]" if _SENSITIVE_KEY_PATTERN.search(key) else value
        return extras


def configure_logging(*, level: str = "INFO", force: bool = False) -> None:
    """配置根 logger 输出单行 JSON 到 stdout。

    Args:
        level: 日志级别（DEBUG/INFO/WARNING/ERROR）。
        force: 是否移除既有 handler 后重新配置（测试与重复初始化用）。
    """
    root = logging.getLogger()
    if root.handlers and not force:
        return
    if force:
        root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    # 第三方库降噪（httpx 每请求一行 INFO，uvicorn.access 与我们的访问日志重复）
    for noisy in ("httpx", "httpcore", "uvicorn.access", "pymilvus"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """获取模块 logger（结构化字段经 extra 注入）。"""
    return logging.getLogger(name)


@contextmanager
def log_context(**fields: str) -> Iterator[None]:
    """在上下文内为所有日志注入字段（request_id/thread_id/tenant_id/run_id/agent/node）。

    未知键被忽略，避免打错键名造成静默丢失。
    """
    tokens = [(var, var.set(value)) for key, value in fields.items() if (var := _context.get(key))]
    try:
        yield
    finally:
        for var, token in tokens:
            var.reset(token)
