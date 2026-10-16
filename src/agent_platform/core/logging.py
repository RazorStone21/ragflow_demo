"""结构化 JSON 日志（CLAUDE.md 3.7）。

- 统一字段：``timestamp/level/request_id/thread_id/tenant_id/run_id/agent/node/event/message``。
- 禁止 ``print``（ruff T20 强制）；禁止记录密钥、系统提示词全文与未脱敏 PII。
- 请求级上下文经 ``contextvars`` 注入，贯穿 request_id/thread_id/tenant_id/run_id。

用法::

    configure_logging(level="INFO")        # 应用启动时调用一次
    with log_context(request_id=rid, tenant_id=tid):
        get_logger(__name__).info("thread created", extra={"event": "thread.created"})
"""

from __future__ import annotations

import json
import logging
import re
import sys
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
    """配置根 logger 输出单行 JSON 到 **stderr**。

    写 stderr 而非 stdout 是有意的：脚本（``scripts/``）的标准输出要留给结果本身，
    例如 ``demo_chat.py > answer.txt`` 应当只拿到干净的回答，不被日志行污染。
    ``logging.StreamHandler()`` 不传参数时的默认流本就是 stderr，此处依赖该默认行为，
    不显式传 ``sys.stderr`` 以免在被重定向后捕获到替换过的对象。

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


def enable_utf8_console() -> None:
    """把 stdout/stderr 切到 UTF-8（Windows 控制台默认 GBK，中文会乱码或抛错）。

    为什么放在 ``core/logging.py``：本模块是进程级输出配置的归属地（它已经安装根
    handler、设置级别，都是进程全局副作用），编码同属这一层，放这里好过在两个 CLI
    脚本里各抄一份。

    为什么用 ``reconfigure()`` 而不是替换 ``sys.stdout``：``logging.StreamHandler()``
    在创建时就抓住了当时的流对象；``reconfigure()`` 是**就地**修改编码，handler 持有的
    同一对象随之生效。换成 ``sys.stdout = ...`` 会让 handler 继续写旧流。

    Linux/容器下本就是 UTF-8，此函数为幂等空转；仅在 Windows 本地脚本场景有意义。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


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
