"""GovPolicyQa 的 State Schema 与状态访问器（CLAUDE.md 4.1，唯一来源）。

State 只放**编排必需**的信息：对话消息、检索证据、判定元数据、最终答案与引用。
不放连接、客户端、密钥，也不放文档全文（证据条数受 ``EVIDENCE_TOP_K`` 约束，
单条为已切分的片段，属于模型输入所必需）。

聚合字段必须显式声明 reducer（CLAUDE.md 4.1）：``messages`` 用 LangGraph 官方
``add_messages``，``evidence`` 用本模块的 ``merge_evidence``（按 chunk_id 去重取高分），
避免并发/多轮写入互相覆盖。

已知边界（诚实记录，不掩饰）：当前为**单轮**问答——检索只用最后一条用户消息，
不把历史回合拼进模型上下文（检索侧尚无查询改写，带上历史会产生无证据支撑的上下文）。
多轮改写（query rewriting / conversational retrieval）列入后续迭代。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from agent_platform.agents.gov_policy_qa.schemas import Citation
from agent_platform.core.errors import AgentPlatformError
from agent_platform.knowledge.schemas import EvidenceItem, RetrievalMeta

AGENT_NAME = "gov_policy_qa"
"""智能体名（= ``langgraph.json`` 中 ``graphs`` 的键 = 目录名）。"""

# 节点名常量：图的装配、路由与测试断言共用，禁止在别处写字符串字面量
NODE_RETRIEVE_EVIDENCE = "retrieve_evidence"
NODE_GENERATE_ANSWER = "generate_answer"
NODE_ANSWER_WITHOUT_EVIDENCE = "answer_without_evidence"

# 答案状态（稳定字符串，供 API 与断言使用）
STATUS_PENDING = "pending"
STATUS_ANSWERED = "answered"
STATUS_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
STATUS_CITATION_MISSING = "citation_missing"
STATUS_GUARDRAIL_BLOCKED = "guardrail_blocked"


def merge_evidence(current: list[EvidenceItem], update: list[EvidenceItem]) -> list[EvidenceItem]:
    """合并证据：按 ``chunk_id`` 去重，保留分数更高的一条（CLAUDE.md 4.1）。

    Args:
        current: 通道中已有证据（首次写入时为空列表）。
        update: 本次节点产出的证据。

    Returns:
        合并后的证据列表；顺序为"先已有、后新增"，便于稳定断言与展示。

    Notes:
        - 空 ``update`` 为无操作，**不会**清空已有证据（避免降级分支误删证据）；
        - 兼容 ``dict`` 输入：checkpoint 反序列化在极端情况下会退回原始字典，
          此处按 ``EvidenceItem`` 校验后再合并，防止脏数据进入下游。
    """
    merged: dict[str, EvidenceItem] = {}
    for raw in (*current, *update):
        item = _coerce_evidence(raw)
        existing = merged.get(item.chunk_id)
        if existing is None or item.score > existing.score:
            merged[item.chunk_id] = item
    return list(merged.values())


def _coerce_evidence(raw: EvidenceItem | dict[str, Any]) -> EvidenceItem:
    """把状态中的证据统一为 ``EvidenceItem``（已是模型则原样返回）。"""
    if isinstance(raw, EvidenceItem):
        return raw
    try:
        return EvidenceItem.model_validate(raw)
    except ValidationError as exc:
        raise AgentPlatformError(f"状态中的证据不符合契约: {exc.error_count()} 处错误") from exc


class GovPolicyQaState(TypedDict, total=False):
    """政务能源政策问答的图状态。

    Attributes:
        messages: 对话消息（reducer=``add_messages``）；节点只追加，不重写历史。
        evidence: 检索证据（reducer=``merge_evidence``）。
        retrieval_meta: 最近一次检索的判定元数据（路由依据，CLAUDE.md 4.3）。
        answer: 最终答案文本（含 ``[n]`` 引用标记）。
        answer_status: ``pending``/``answered``/``insufficient_evidence``/``citation_missing``。
        citations: 答案实际引用的证据条目（供 API 直接返回引用列表，不必回传全文）。
    """

    messages: Annotated[list[AnyMessage], add_messages]
    evidence: Annotated[list[EvidenceItem], merge_evidence]
    retrieval_meta: RetrievalMeta | None
    answer: str
    answer_status: str
    citations: list[Citation]


def initial_state(question: str) -> GovPolicyQaState:
    """构造一次新 run 的初始状态（API 层唯一入口，保证字段齐全）。

    Args:
        question: 用户问题（已由 HTTP 边界校验非空）。

    Returns:
        含初始用户消息、空证据与 ``pending`` 状态的完整状态字典。
    """
    return {
        "messages": [HumanMessage(content=question)],
        "evidence": [],
        "retrieval_meta": None,
        "answer": "",
        "answer_status": STATUS_PENDING,
        "citations": [],
    }


def latest_user_question(state: GovPolicyQaState) -> str:
    """取最后一条用户消息的纯文本（去掉首尾空白；无则返回空串）。

    空串由调用方判定为内部不变量破坏——不在此抛错，保持访问器为纯函数。
    """
    for message in reversed(state.get("messages") or []):
        if isinstance(message, HumanMessage):
            return message_text(message).strip()
    return ""


def message_text(message: AnyMessage) -> str:
    """提取消息的文本内容（兼容 ``str`` 与内容块列表两种形态）。

    返回值**不做首尾裁剪**：流式增量（``AIMessageChunk``）必须逐字拼接，
    裁剪会吞掉分块之间的空格并破坏答案排版；需要裁剪的调用方自行 ``strip()``。
    """
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return " ".join(parts)


__all__: list[str] = [
    "AGENT_NAME",
    "NODE_ANSWER_WITHOUT_EVIDENCE",
    "NODE_GENERATE_ANSWER",
    "NODE_RETRIEVE_EVIDENCE",
    "STATUS_ANSWERED",
    "STATUS_CITATION_MISSING",
    "STATUS_GUARDRAIL_BLOCKED",
    "STATUS_INSUFFICIENT_EVIDENCE",
    "STATUS_PENDING",
    "GovPolicyQaState",
    "initial_state",
    "latest_user_question",
    "merge_evidence",
    "message_text",
]
