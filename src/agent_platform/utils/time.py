"""时间规范化的纯函数（CLAUDE.md 2.2 / 3.1 / 8.1）。

对外序列化统一 ISO 8601 UTC，且统一以 ``Z`` 结尾——同一套 API 里不允许
``+00:00`` 与 ``Z`` 两种写法并存，否则客户端解析要写两份分支。

本模块名与标准库 ``time`` 同名，但 Python 3 的绝对导入语义下 ``import time``
取到的仍是标准库；仅 ``from agent_platform.utils import time`` 会取到本模块。
"""

from __future__ import annotations

from datetime import UTC, datetime


def iso_utc(raw: str | None) -> str | None:
    """把时间戳规范为 ISO 8601 UTC（``Z`` 结尾）。

    Args:
        raw: 原始时间戳字符串（LangGraph checkpoint 的时间戳形如
            ``2026-10-15T12:34:56.789012+00:00``）；``None`` 或空串原样返回 ``None``。

    Returns:
        形如 ``2026-10-15T12:34:56.789012Z`` 的字符串；无法解析时返回 ``None``。

    Notes:
        非法值返回 ``None`` 而**不**抛异常：时间戳是 checkpoint 的元数据而非业务字段，
        一个格式异常的时间戳不应把本来可用的查询变成 500。调用方按可空字段处理。
        朴素的（无时区）时间戳按 UTC 解释——checkpoint 全部由本平台以 UTC 写入。
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    """当前 UTC 时刻，规范为 ``iso_utc`` 的同一种表示（``Z`` 结尾）。

    与 ``iso_utc`` 成对使用：写入的产物时间戳与读回规范化后的时间戳必须字节一致，
    否则"重新读取产物再写回"会产生无意义的 diff。
    """
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__: list[str] = ["iso_utc", "utc_now_iso"]
