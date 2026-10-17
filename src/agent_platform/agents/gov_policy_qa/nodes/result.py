"""最终答案的状态更新片段构造（节点共用，避免各写一遍导致字段漂移）。

节点返回的必须是**部分状态字典**（CLAUDE.md 4.2 / NO-04）；把"给出答案"这件事
收敛到一处，保证 ``answer`` / ``answer_status`` / ``citations`` / ``messages``
四者始终同步写入——漏写任何一个都会让下游（API、评估）读到不一致的状态。

``retrieval_meta`` 通常由检索节点写入，只有**生成结果推翻检索判断**时才在这里
整体覆盖（见 ``downgrade_retrieval_meta``）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage
from pydantic import ValidationError

from agent_platform.agents.gov_policy_qa.schemas import Citation
from agent_platform.core.logging import get_logger
from agent_platform.knowledge.schemas import RetrievalMeta

logger = get_logger(__name__)

REASON_MODEL_DECLARED_INSUFFICIENT = "model_declared_insufficient_evidence"
"""``retrieval_meta.reason`` 的新取值：检索层认为证据可用，生成模型读后判定不够。

保留为稳定字符串而非内联字面量：评估、排障与测试都按它区分"检索没找到"和
"检索找到了但答不了"——两者面向用户的处置完全不同。
"""


def answer_update(
    *,
    answer: str,
    status: str,
    citations: Sequence[Citation] = (),
    retrieval_meta: RetrievalMeta | None = None,
) -> dict[str, Any]:
    """构造"给出最终答案"的状态更新。

    Args:
        answer: 面向用户的最终答案文本（已通过引用校验或为确定性兜底文案）。
        status: 答案状态（``state.STATUS_*`` 常量之一）。
        citations: 答案实际引用的证据条目。
        retrieval_meta: 需要覆盖写的检索元数据；``None`` 时不写该键，
            既有调用方（检索节点写入后保持不变）的行为不受影响。

    Returns:
        含 ``messages``（追加一条 AIMessage）、``answer``、``answer_status``、
        ``citations``（以及非 ``None`` 时的 ``retrieval_meta``）的部分状态字典。
    """
    update: dict[str, Any] = {
        "messages": [AIMessage(content=answer)],
        "answer": answer,
        "answer_status": status,
        "citations": list(citations),
    }
    if retrieval_meta is not None:
        update["retrieval_meta"] = retrieval_meta
    return update


def downgrade_retrieval_meta(
    meta: RetrievalMeta | dict[str, Any] | None,
) -> RetrievalMeta | None:
    """把检索判定改写为"证据不足"，其余字段原样保留。

    语义依据：``is_sufficient`` 的定义是"证据是否足以作答"。检索层只做初步判断
    （阈值 + 重排）；生成模型通读证据后声明无据，是更下游、信息更全的判断，
    应当覆盖它。不覆盖的后果是状态与元数据自相矛盾——``score_refusal`` 对这种
    情形直接判失败（"状态为 insufficient_evidence，检索元数据却判定
    is_sufficient=True"），修复了生成侧却仍然红。

    Args:
        meta: 当前检索元数据；接受 ``RetrievalMeta``、checkpoint 反序列化后的
            ``dict`` 或 ``None``。

    Returns:
        降级后的新模型（不可变，原对象不改动）；``None`` 入参或无法解析时返回
        ``None``——调用方据此让"证据不足"与"元数据缺失"保持一致。
    """
    if meta is None:
        return None
    try:
        parsed = meta if isinstance(meta, RetrievalMeta) else RetrievalMeta.model_validate(meta)
    except ValidationError:
        logger.warning(
            "agent.retrieval_meta_unparsable",
            extra={
                "event": "agent.retrieval_meta_unparsable",
                "meta_type": type(meta).__name__,
            },
        )
        return None
    return parsed.model_copy(
        update={"is_sufficient": False, "reason": REASON_MODEL_DECLARED_INSUFFICIENT}
    )


__all__: list[str] = [
    "REASON_MODEL_DECLARED_INSUFFICIENT",
    "answer_update",
    "downgrade_retrieval_meta",
]
