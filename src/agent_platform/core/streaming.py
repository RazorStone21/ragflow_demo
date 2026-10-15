"""SSE 流事件封装（CLAUDE.md 4.7 / 8.4）。

标准事件类型为全平台统一枚举；每个事件携带 ``run_id/thread_id/request_id`` 与
自增序号 ``seq``；错误也走流（``run.error``），不得让客户端挂起。

线格式::

    event: retrieval.evidence
    data: {"type":"retrieval.evidence","seq":3,...}

    : ping            <- 心跳注释帧
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SseEventType(StrEnum):
    """SSE 标准事件类型（CLAUDE.md 8.4，全平台统一，不得自定义同义事件）。"""

    RUN_STARTED = "run.started"
    NODE_ENTERED = "node.entered"
    MESSAGE_DELTA = "message.delta"
    TOOL_CALLED = "tool.called"
    TOOL_RESULT = "tool.result"
    RETRIEVAL_EVIDENCE = "retrieval.evidence"
    HUMAN_INTERRUPT = "human.interrupt"
    NODE_EXITED = "node.exited"
    RUN_COMPLETED = "run.completed"
    RUN_ERROR = "run.error"


class SseEvent(BaseModel):
    """单个 SSE 事件的线上结构（不可变）。"""

    model_config = ConfigDict(frozen=True)

    type: SseEventType = Field(description="事件类型")
    seq: int = Field(ge=1, description="事件序号（同一 run 内自增，从 1 起）")
    run_id: str = Field(min_length=1, description="本次运行 ID")
    thread_id: str = Field(min_length=1, description="会话线程 ID")
    request_id: str = Field(min_length=1, description="请求 ID（贯穿日志与追踪）")
    payload: dict[str, Any] = Field(default_factory=dict, description="事件负载")

    def to_sse(self) -> str:
        """渲染为 SSE 线格式（``event: ...\\ndata: ...\\n\\n``）。"""
        data = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        return f"event: {self.type.value}\ndata: {data}\n\n"


def heartbeat_frame() -> str:
    """SSE 保活注释帧（CLAUDE.md 8.4 心跳要求）。"""
    return ": ping\n\n"


class SseEmitter:
    """按 run 递增序号并渲染事件帧的发送器（不直接持有连接，便于单测）。"""

    def __init__(self, *, run_id: str, thread_id: str, request_id: str) -> None:
        self._run_id = run_id
        self._thread_id = thread_id
        self._request_id = request_id
        self._seq = 0

    @property
    def seq(self) -> int:
        """当前已发出的最大序号。"""
        return self._seq

    def emit(self, event_type: SseEventType, payload: dict[str, Any] | None = None) -> str:
        """构造下一个事件并返回其 SSE 帧字符串。"""
        self._seq += 1
        event = SseEvent(
            type=event_type,
            seq=self._seq,
            run_id=self._run_id,
            thread_id=self._thread_id,
            request_id=self._request_id,
            payload=payload or {},
        )
        return event.to_sse()
