"""条件路由（CLAUDE.md 4.3）：确定性、可单测、有兜底。

路由只依赖检索判定元数据（``RetrievalMeta.is_sufficient``）与证据是否为空，
**不含模型判断**——涉及走向的决策不交给 LLM，避免不可复现的分支。

保守原则：任何"看不清"的情形（元数据缺失、反序列化后不可解析、证据为空）
一律走**无证据降级分支**。理由很直接——证据不足时最坏的后果是回答得保守，
而误判为充足则会产出无依据的结论（NO-12）。
"""

from __future__ import annotations

from pydantic import ValidationError

from agent_platform.agents.gov_policy_qa.state import (
    NODE_ANSWER_WITHOUT_EVIDENCE,
    NODE_GENERATE_ANSWER,
    GovPolicyQaState,
)
from agent_platform.core.logging import get_logger
from agent_platform.knowledge.schemas import RetrievalMeta

logger = get_logger(__name__)


def route_after_retrieve(state: GovPolicyQaState) -> str:
    """检索之后的分支选择。

    Args:
        state: 含 ``retrieval_meta`` 与 ``evidence`` 的图状态。

    Returns:
        ``generate_answer``（证据充足）或 ``answer_without_evidence``（其余全部情形）。
    """
    if not state.get("evidence"):
        return NODE_ANSWER_WITHOUT_EVIDENCE
    meta = _coerce_meta(state.get("retrieval_meta"))
    if meta is None or not meta.is_sufficient:
        return NODE_ANSWER_WITHOUT_EVIDENCE
    return NODE_GENERATE_ANSWER


def _coerce_meta(raw: object) -> RetrievalMeta | None:
    """把元数据统一为 ``RetrievalMeta``；不可解析时返回 ``None``（走降级）。

    checkpoint 反序列化在极端情况下会退回原始字典，故此处做一次显式校验；
    校验失败**显式记录**并降级，而不是静默当成充足（CLAUDE.md 3.6 / NO-06）。
    """
    if raw is None:
        return None
    if isinstance(raw, RetrievalMeta):
        return raw
    try:
        return RetrievalMeta.model_validate(raw)
    except ValidationError as exc:
        logger.warning(
            "agent.retrieval_meta_invalid",
            extra={"event": "agent.retrieval_meta_invalid", "errors": exc.error_count()},
        )
        return None


__all__: list[str] = ["route_after_retrieve"]
