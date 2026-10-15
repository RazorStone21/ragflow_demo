"""API 层构造的 SSE 事件负载（CLAUDE.md 8.4）。

分工（与 ``core/streaming.py`` 的注释互为呼应）：

- 节点推来的负载（``retrieval.evidence`` / ``message.delta``）**逐块透传**：它们在节点内
  已经 ``model_dump(mode="json")`` 序列化过，此处再校验一遍只会新增一个失败面，
  换不来任何契约收益。
- 本模块描述的是 **API 层自己合成**的那几类事件（``run.started`` / ``node.*`` /
  ``run.completed`` / ``run.error``）——它们是客户端可以直接依赖的结构，因此必须成模型。

关于 ``run.completed``：它的答案一律取自 checkpoint 读回的**权威终态**，而不是流式草稿。
降级分支根本不发 token 增量；且草稿可能已被 ``citation_missing`` / ``guardrail_blocked``
的兜底文案整体替换。客户端必须以本事件为准（``generate_answer`` 模块 docstring 同此约定）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agent_platform.agents.gov_policy_qa.schemas import Citation
from agent_platform.agents.gov_policy_qa.state import AnswerStatus
from agent_platform.knowledge.schemas import RetrievalMeta


class RunStartedPayload(BaseModel):
    """``run.started`` 负载。"""

    model_config = ConfigDict(frozen=True)

    agent: str = Field(min_length=1, description="智能体名（registry 中的注册名）")


class NodePayload(BaseModel):
    """``node.entered`` / ``node.exited`` 负载。"""

    model_config = ConfigDict(frozen=True)

    node: str = Field(min_length=1, description="节点名（图内常量，不是模型自由文本）")


class RunCompletedPayload(BaseModel):
    """``run.completed`` 负载：本次 run 的权威终态。"""

    model_config = ConfigDict(frozen=True)

    answer: str = Field(description="最终答案全文（含 [n] 引用标记）")
    answer_status: AnswerStatus = Field(description="答案状态；不是 answered 时答案通常是拒答文案")
    citations: list[Citation] = Field(default_factory=list, description="答案实际引用的证据条目")
    retrieval_meta: RetrievalMeta | None = Field(
        default=None, description="最近一次检索的判定元数据（无检索则为 None）"
    )


class RunErrorPayload(BaseModel):
    """``run.error`` 负载：流内错误（CLAUDE.md 8.4，错误也走流，不得让客户端挂起）。

    ``message`` 取自 ``AgentPlatformError.detail``（面向调用方的说明），
    **不得**使用 ``.message``（内部诊断信息，可能含拓扑细节，CLAUDE.md 3.6 / NO-07）。
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, description="稳定错误码（core/errors.py 的 ErrorCode 值）")
    message: str = Field(min_length=1, description="面向调用方的可理解说明")
    retryable: bool = Field(description="是否可重试（客户端据此决定是否重发）")
    node: str | None = Field(
        default=None, description="出错时最后进入的节点名；未进入任何节点为 None"
    )
