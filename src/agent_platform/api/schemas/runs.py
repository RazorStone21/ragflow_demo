"""运行（run）与会话状态 DTO（CLAUDE.md 8.1 / 8.3）。

请求模型一律 ``extra="forbid"``：多出的字段直接 422。这不是洁癖——请求体里出现
``tenant_id``/``thread_id`` 之类的字段时，静默忽略会让人误以为它生效了，
而显式拒绝能把"前端试图自己指定租户"这件事在联调期就暴露出来（NO-19）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agent_platform.agents.gov_policy_qa.schemas import Citation
from agent_platform.agents.gov_policy_qa.state import AnswerStatus
from agent_platform.knowledge.schemas import RetrievalMeta


class RunRequest(BaseModel):
    """发起一次运行的请求体。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str = Field(
        min_length=1,
        max_length=64,
        description="智能体名；运行时经 agents/registry 校验，未注册返回 404",
    )
    message: str = Field(
        min_length=1,
        description="用户提问；长度上限由 settings.max_question_chars 在边界校验（可配置，NO-03）",
    )


class RunResponse(BaseModel):
    """``POST /runs`` 的响应：本次运行的权威终态。"""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1, description="本次运行 ID（日志与追踪的关联键）")
    thread_id: str = Field(min_length=1, description="会话 ID")
    answer: str = Field(description="最终答案全文")
    answer_status: AnswerStatus = Field(description="答案状态")
    citations: list[Citation] = Field(default_factory=list, description="答案引用的证据条目")
    retrieval_meta: RetrievalMeta | None = Field(default=None, description="检索判定元数据")


class StateResponse(BaseModel):
    """``GET /threads/{thread_id}/state`` 的响应。

    前五个字段对齐 LangGraph Server 的 ``StateSnapshot`` 语义，
    后四个是本智能体的业务字段。
    """

    model_config = ConfigDict(frozen=True)

    thread_id: str = Field(min_length=1)
    latest_checkpoint_id: str | None = Field(default=None)
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC")
    next: list[str] = Field(default_factory=list, description="待执行节点名")
    has_interrupt: bool = Field(default=False)
    answer: str = Field(default="", description="最近一次运行的最终答案")
    answer_status: AnswerStatus = Field(
        default=AnswerStatus.PENDING, description="最近一次运行的答案状态"
    )
    citations: list[Citation] = Field(default_factory=list)
    retrieval_meta: RetrievalMeta | None = Field(default=None)


class HistoryRequest(BaseModel):
    """``POST /history`` 的请求体（游标分页，CLAUDE.md 8.1）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    limit: int = Field(default=20, ge=1, le=100, description="本页条数上限")
    before: str | None = Field(
        default=None, description="游标：上一页最后一条的 checkpoint_id；首页传 null"
    )


class HistoryItem(BaseModel):
    """历史记录中的单个检查点。"""

    model_config = ConfigDict(frozen=True)

    checkpoint_id: str = Field(min_length=1)
    created_at: str | None = Field(default=None, description="ISO 8601 UTC")
    step: int | None = Field(default=None, description="图执行的步序号")
    next: list[str] = Field(default_factory=list, description="该检查点上的待执行节点名")
    answer: str = Field(default="")
    answer_status: AnswerStatus = Field(default=AnswerStatus.PENDING)
    citations: list[Citation] = Field(default_factory=list)


class HistoryResponse(BaseModel):
    """``POST /history`` 的响应。"""

    model_config = ConfigDict(frozen=True)

    items: list[HistoryItem] = Field(default_factory=list, description="本页记录（由新到旧）")
    next_cursor: str | None = Field(default=None, description="下一页游标；为 null 表示已到末页")


__all__: list[str] = [
    "HistoryItem",
    "HistoryRequest",
    "HistoryResponse",
    "RunRequest",
    "RunResponse",
    "StateResponse",
]
