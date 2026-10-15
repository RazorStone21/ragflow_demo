"""会话（thread）DTO（CLAUDE.md 8.1：语义对齐 LangGraph Server）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ThreadResponse(BaseModel):
    """会话资源。

    ``POST /threads`` 与 ``GET /threads/{thread_id}`` 共用本模型。注意二者语义不同：
    创建时**不会**产生任何 checkpoint（``latest_checkpoint_id``/``updated_at`` 为 ``None``），
    会话在其**首次 run** 时因 checkpoint 落库而真正存在。因此"刚创建就 GET"会得到 404。
    这是有意为之——不额外维护一张会话表（那会引入与图状态竞争的第二个状态源，
    CLAUDE.md 4.4 明令禁止）。
    """

    model_config = ConfigDict(frozen=True)

    thread_id: str = Field(min_length=1, description="会话 ID，形如 tenant:user:session")
    latest_checkpoint_id: str | None = Field(
        default=None, description="最新检查点 ID；尚无检查点为 None"
    )
    updated_at: str | None = Field(default=None, description="最新检查点时间（ISO 8601 UTC）")
    next: list[str] = Field(
        default_factory=list,
        description="待执行节点名（字段名对齐 LangGraph Server；空列表表示无挂起节点）",
    )
    has_interrupt: bool = Field(
        default=False, description="是否存在待人工介入的中断（HITL 尚未接入，当前恒为 False）"
    )


__all__: list[str] = ["ThreadResponse"]
