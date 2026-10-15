"""图状态 → API DTO 的翻译层（CLAUDE.md 8.1）。

单独成模块的理由：同一份"终态字段"要供**四条**路径复用——``POST /runs`` 的响应体、
``POST /runs/stream`` 的 ``run.completed`` 事件、``GET /state``、``POST /history``。
把它们散在各路由里各写一份，等状态字段一改就会出现"流式返回值与查询返回值不一致"，
而客户端恰恰靠两份互相印证。此处是唯一翻译点。

**权威终态一律取自 checkpoint**，不复用流式草稿；理由见 ``sse.py`` 的模块说明。
"""

from __future__ import annotations

from typing import Any

from langgraph.types import StateSnapshot

from agent_platform.agents.gov_policy_qa.schemas import Citation
from agent_platform.agents.gov_policy_qa.state import AnswerStatus
from agent_platform.api.schemas.events import RunCompletedPayload
from agent_platform.api.schemas.runs import HistoryItem, RunResponse, StateResponse
from agent_platform.api.schemas.threads import ThreadResponse
from agent_platform.knowledge.schemas import RetrievalMeta
from agent_platform.utils.time import iso_utc


def _status_of(values: dict[str, Any]) -> AnswerStatus:
    """取答案状态；缺失或非法值一律归为 ``pending``。

    ``AnswerStatus()`` 对未知值会抛 ``ValueError``——旧 checkpoint 里的历史取值
    不该让一次查询接口 500，故降级为 ``pending`` 并在语义上表示"没有确定的答案状态"。
    """
    raw = values.get("answer_status")
    if not raw:
        return AnswerStatus.PENDING
    try:
        return AnswerStatus(raw)
    except ValueError:
        return AnswerStatus.PENDING


def _citations_of(values: dict[str, Any]) -> list[Citation]:
    """取引用列表并逐条校验（checkpoint 反序列化可能给出原始 dict）。"""
    return [Citation.model_validate(item) for item in values.get("citations") or []]


def _meta_of(values: dict[str, Any]) -> RetrievalMeta | None:
    """取检索元数据；无检索记录时为 ``None``。"""
    raw = values.get("retrieval_meta")
    return RetrievalMeta.model_validate(raw) if raw else None


def completed_payload(values: dict[str, Any]) -> RunCompletedPayload:
    """构造 ``run.completed`` 负载。"""
    return RunCompletedPayload(
        answer=values.get("answer") or "",
        answer_status=_status_of(values),
        citations=_citations_of(values),
        retrieval_meta=_meta_of(values),
    )


def run_response(*, run_id: str, thread_id: str, values: dict[str, Any]) -> RunResponse:
    """构造 ``POST /runs`` 的响应。"""
    return RunResponse(
        run_id=run_id,
        thread_id=thread_id,
        answer=values.get("answer") or "",
        answer_status=_status_of(values),
        citations=_citations_of(values),
        retrieval_meta=_meta_of(values),
    )


def _checkpoint_id_of(snapshot: StateSnapshot) -> str | None:
    """取快照对应的 checkpoint_id（LangGraph 把它放在 ``config.configurable`` 里）。"""
    configurable = snapshot.config.get("configurable") or {}
    value = configurable.get("checkpoint_id")
    return value if isinstance(value, str) else None


def thread_response(*, thread_id: str, snapshot: StateSnapshot) -> ThreadResponse:
    """构造会话资源响应（``POST``/``GET /threads`` 共用）。"""
    return ThreadResponse(
        thread_id=thread_id,
        latest_checkpoint_id=_checkpoint_id_of(snapshot),
        updated_at=iso_utc(snapshot.created_at),
        next=list(snapshot.next),
        # 读真实值而非硬编码 False：HITL 接入后（interrupt/Command(resume)）此处自动生效
        has_interrupt=bool(snapshot.interrupts),
    )


def state_response(*, thread_id: str, snapshot: StateSnapshot) -> StateResponse:
    """构造 ``GET /state`` 的响应（LangGraph 语义字段 + 本智能体业务字段）。"""
    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    return StateResponse(
        thread_id=thread_id,
        latest_checkpoint_id=_checkpoint_id_of(snapshot),
        updated_at=iso_utc(snapshot.created_at),
        next=list(snapshot.next),
        has_interrupt=bool(snapshot.interrupts),
        answer=values.get("answer") or "",
        answer_status=_status_of(values),
        citations=_citations_of(values),
        retrieval_meta=_meta_of(values),
    )


def history_item(snapshot: StateSnapshot) -> HistoryItem:
    """把单个历史检查点映射为一条记录。

    ``step`` 取自 checkpoint 元数据（LangGraph 用它标识图执行到第几步）。
    """
    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    metadata = snapshot.metadata or {}
    step = metadata.get("step")
    checkpoint_id = _checkpoint_id_of(snapshot)
    return HistoryItem(
        checkpoint_id=checkpoint_id or "",
        created_at=iso_utc(snapshot.created_at),
        step=step if isinstance(step, int) else None,
        next=list(snapshot.next),
        answer=values.get("answer") or "",
        answer_status=_status_of(values),
        citations=_citations_of(values),
    )


__all__: list[str] = [
    "completed_payload",
    "history_item",
    "run_response",
    "state_response",
    "thread_response",
]
