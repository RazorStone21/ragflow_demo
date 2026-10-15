"""检索证据节点（CLAUDE.md 4.2 / 7.4 / 7.5 / NO-12）。

一次检索 = 一次职责：取问题 → 调检索器 → 写证据与判定元数据 → 推流事件。
"检索不到"不是异常：``Retriever.search`` 返回空证据 + ``is_sufficient=False``，
由 ``edges.route_after_retrieve`` 决定走降级分支；只有基础设施故障
（向量库/嵌入网关不可用）才向上抛出，两者不得混为一谈。
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter

from agent_platform.agents.gov_policy_qa.nodes.types import GovPolicyQaNode
from agent_platform.agents.gov_policy_qa.state import GovPolicyQaState, latest_user_question
from agent_platform.core.errors import AgentPlatformError
from agent_platform.core.logging import get_logger
from agent_platform.core.streaming import AgentStreamEvent, SseEventType, noop_stream_writer
from agent_platform.knowledge.retriever import Retriever
from agent_platform.tenants import tenant_id_from_config

logger = get_logger(__name__)


def build_retrieve_evidence_node(retriever: Retriever) -> GovPolicyQaNode:
    """构造检索证据节点（依赖经闭包注入，CLAUDE.md 4.8）。

    Args:
        retriever: 混合检索 + 重排的证据获取器（已按租户过滤，NO-19）。

    Returns:
        满足统一节点形态的可调用对象。
    """

    async def retrieve_evidence(
        state: GovPolicyQaState,
        /,
        *,
        writer: StreamWriter = noop_stream_writer,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        """检索与问题相关的证据并写入状态。

        Args:
            state: 图状态（只读）。
            writer: LangGraph 流写入器；用于推送 ``retrieval.evidence`` 事件。
            config: 运行配置；租户标识取自其中已认证的上下文（NO-19）。

        Returns:
            ``{"evidence": [...], "retrieval_meta": ...}``。

        Raises:
            AgentPlatformError: 状态中缺少用户问题——调用链装配错误，显式失败。
            KnowledgeError: 检索基础设施故障（超时/不可用），由上层转为 run.error。
        """
        question = latest_user_question(state)
        if not question:
            raise AgentPlatformError(
                "状态中缺少用户问题（messages 中没有可用的 HumanMessage 文本）",
                detail="会话状态异常，请重新发起提问。",
            )
        tenant_id = tenant_id_from_config(config)
        outcome = await retriever.search(question, tenant_id=tenant_id)

        writer(
            AgentStreamEvent(
                event=SseEventType.RETRIEVAL_EVIDENCE,
                data={
                    "evidence": [item.model_dump(mode="json") for item in outcome.evidence],
                    "meta": outcome.meta.model_dump(mode="json"),
                },
            ).to_chunk()
        )
        logger.info(
            "agent.evidence_retrieved",
            extra={
                "event": "agent.evidence_retrieved",
                "tenant_id": tenant_id,
                "evidence_count": outcome.meta.evidence_count,
                "candidate_count": outcome.meta.candidate_count,
                "is_sufficient": outcome.meta.is_sufficient,
                "reason": outcome.meta.reason,
                "rerank_applied": outcome.meta.rerank_applied,
            },
        )
        return {"evidence": outcome.evidence, "retrieval_meta": outcome.meta}

    return retrieve_evidence
