"""无证据降级分支（CLAUDE.md 7.5 / NO-12）。

**本节点不调用模型**：没有证据时让模型"自由发挥"正是幻觉的来源。这里返回
确定性文案 + 澄清引导，并按检索判定理由给出可操作的下一步提示；判定理由属于
内部观测字段，映射为面向用户的话术，不把 ``below_rerank_threshold`` 这类
内部字符串直接抛给用户。

该分支是**正常业务路径**（不是错误）：检索不到应有明确、礼貌、可继续的回答，
而不是 500 或空白。
"""

from __future__ import annotations

from typing import Any

from langgraph.types import StreamWriter

from agent_platform.agents.gov_policy_qa.nodes.result import answer_update
from agent_platform.agents.gov_policy_qa.nodes.types import GovPolicyQaNode
from agent_platform.agents.gov_policy_qa.state import (
    STATUS_INSUFFICIENT_EVIDENCE,
    GovPolicyQaState,
)
from agent_platform.core.logging import get_logger
from agent_platform.core.streaming import noop_stream_writer
from agent_platform.knowledge.retriever import (
    REASON_BELOW_RERANK,
    REASON_BELOW_SIMILARITY,
    REASON_EMPTY_QUERY,
    REASON_NO_CANDIDATES,
)

logger = get_logger(__name__)

_BASE_ANSWER = (
    "抱歉，我在现有资料中没有找到足以支撑回答的内容，因此不给出结论（不做无依据的推测）。"
)

# 检索判定理由 → 面向用户的下一步建议（未列出的理由走默认话术）
_REASON_HINTS: dict[str, str] = {
    REASON_EMPTY_QUERY: "您的问题似乎为空，请补充完整的问题。",
    REASON_NO_CANDIDATES: "请补充政策名称、发布年份、地区或文号等线索，我再检索一次。",
    REASON_BELOW_SIMILARITY: "现有资料与该问题的相关性较低，请换用更贴近政策的表述提问。",
    REASON_BELOW_RERANK: "找到的片段不足以支撑结论，请把问题范围缩小到具体政策条款。",
}
_DEFAULT_HINT = "请换个说法或补充政策名称、年份、地区后再试。"


def build_answer_without_evidence_node() -> GovPolicyQaNode:
    """构造无证据降级节点（无依赖注入：不触达任何外部系统）。"""

    async def answer_without_evidence(
        state: GovPolicyQaState,
        /,
        *,
        writer: StreamWriter = noop_stream_writer,
    ) -> dict[str, Any]:
        """给出确定性拒答 + 澄清引导。

        Args:
            state: 图状态（只读）；读取 ``retrieval_meta.reason`` 生成提示。
            writer: 未使用（保留统一节点形态；本节点无增量内容可推）。

        Returns:
            含拒答文案与 ``insufficient_evidence`` 状态的部分状态字典。
        """
        del writer  # 无 token 增量：整段文案由 run.completed 一次性给出
        reason = _reason_of(state)
        hint = _REASON_HINTS.get(reason, _DEFAULT_HINT)
        logger.info(
            "agent.answer_insufficient_evidence",
            extra={"event": "agent.answer_insufficient_evidence", "reason": reason},
        )
        return answer_update(
            answer=f"{_BASE_ANSWER}\n\n{hint}", status=STATUS_INSUFFICIENT_EVIDENCE
        )

    return answer_without_evidence


def _reason_of(state: GovPolicyQaState) -> str:
    """读取检索判定理由；元数据缺失或不可解析时返回空串（走默认话术）。"""
    meta = state.get("retrieval_meta")
    reason = getattr(meta, "reason", None)
    if isinstance(reason, str):
        return reason
    if isinstance(meta, dict) and isinstance(meta.get("reason"), str):
        return str(meta["reason"])
    return ""


__all__: list[str] = ["build_answer_without_evidence_node"]
