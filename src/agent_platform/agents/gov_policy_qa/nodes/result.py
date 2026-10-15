"""最终答案的状态更新片段构造（节点共用，避免各写一遍导致字段漂移）。

节点返回的必须是**部分状态字典**（CLAUDE.md 4.2 / NO-04）；把"给出答案"这件事
收敛到一处，保证 ``answer`` / ``answer_status`` / ``citations`` / ``messages``
四者始终同步写入——漏写任何一个都会让下游（API、评估）读到不一致的状态。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage

from agent_platform.agents.gov_policy_qa.schemas import Citation


def answer_update(
    *, answer: str, status: str, citations: Sequence[Citation] = ()
) -> dict[str, Any]:
    """构造"给出最终答案"的状态更新。

    Args:
        answer: 面向用户的最终答案文本（已通过引用校验或为确定性兜底文案）。
        status: 答案状态（``state.STATUS_*`` 常量之一）。
        citations: 答案实际引用的证据条目。

    Returns:
        含 ``messages``（追加一条 AIMessage）、``answer``、``answer_status``、``citations``
        的部分状态字典。
    """
    return {
        "messages": [AIMessage(content=answer)],
        "answer": answer,
        "answer_status": status,
        "citations": list(citations),
    }


__all__: list[str] = ["answer_update"]
