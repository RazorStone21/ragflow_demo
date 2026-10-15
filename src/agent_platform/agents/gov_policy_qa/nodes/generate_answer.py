"""生成带引用的回答（CLAUDE.md 4.2 / 4.7 / 5.5 / 7.5 / 10.3）。

流程：渲染提示词（证据以不可信数据栅栏隔离）→ 流式调用模型（逐 token 推
``message.delta``）→ 校验答案确实带合法引用 → 输出护栏检查 → 写入状态。

三处刻意的设计取舍：

1. **不重试带引用的生成**：答案已边生成边推送给用户，重试会让用户先看到被否决的
   文本；因此引用缺失时直接走确定性兜底（状态 ``citation_missing``），最终以
   ``run.completed`` 的答案为准。
2. **引用在输出边界做确定性校验**：``[n]`` 必须指向真实证据下标，否则判为无引用
   ——"无引用不得作为事实结论"（NO-12）不能只靠提示词自觉。
3. **不用结构化输出（JSON/tool）**：本节点的产出需要逐 token 流式呈现，JSON 流对
   用户不可读；引用完整性改由上述边界校验保证（CLAUDE.md 5.6 允许"解析失败走
   兜底分支"）。
"""

from __future__ import annotations

import time
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamWriter

from agent_platform.agents.gov_policy_qa.nodes.result import answer_update
from agent_platform.agents.gov_policy_qa.nodes.types import GovPolicyQaNode
from agent_platform.agents.gov_policy_qa.prompts import (
    ANSWER_HUMAN_PROMPT,
    ANSWER_SYSTEM_PROMPT,
    render_prompt,
)
from agent_platform.agents.gov_policy_qa.schemas import build_citations, extract_citation_indices
from agent_platform.agents.gov_policy_qa.state import (
    STATUS_ANSWERED,
    STATUS_CITATION_MISSING,
    STATUS_GUARDRAIL_BLOCKED,
    STATUS_INSUFFICIENT_EVIDENCE,
    GovPolicyQaState,
    latest_user_question,
    message_text,
)
from agent_platform.core.logging import get_logger
from agent_platform.core.models import ChatModel
from agent_platform.core.streaming import AgentStreamEvent, SseEventType, noop_stream_writer
from agent_platform.core.tracing import NoOpTracingHook, TracingHook
from agent_platform.guardrails import Guardrail, PassThroughGuardrail
from agent_platform.guardrails.sanitize import neutralize_untrusted
from agent_platform.knowledge.schemas import EvidenceItem
from agent_platform.tenants import tenant_id_from_config

logger = get_logger(__name__)

_INSUFFICIENT_ANSWER = (
    "抱歉，本次没有检索到可用资料，我无法给出有依据的回答。请换一种问法或补充政策名称、年份后再试。"
)
# 必须是**非空拒答文案**，不能是 ""：
#   - 本模块 docstring 称该分支为"确定性兜底"，且"最终以 run.completed 的答案为准"，
#     空串无法兜底、也无内容可作准；
#   - 与另两条兜底文案（_INSUFFICIENT_ANSWER / _BLOCKED_ANSWER）同一 register；
#   - 兄弟分支 answer_without_evidence 的 docstring 明写不得以"空白"作答；
#   - 草稿早已经 message.delta 流式推给用户，此处若不给出替代文案，用户最终看到的是
#     空白结论——引用缺失（NO-12：无引用不得作为事实结论）必须以显式拒答收尾。
_CITATION_MISSING_ANSWER = (
    "抱歉，本次生成的回答未能给出有效引用，无法确认其有据可依，因此不予采纳。"
    "请换一种问法或补充政策名称、年份、地区后再试。"
)
_BLOCKED_ANSWER = "抱歉，本次回答未通过内容安全校验，已被拦截。请调整提问方式后重试。"


def build_generate_answer_node(
    model: ChatModel,
    *,
    logical_name: str,
    guardrail: Guardrail | None = None,
    tracing: TracingHook | None = None,
) -> GovPolicyQaNode:
    """构造生成回答的节点（依赖经闭包注入，CLAUDE.md 4.8）。

    Args:
        model: 经网关的聊天模型（CLAUDE.md 5.1，含超时/重试/fallback 配置）。
        logical_name: 网关**逻辑模型名**（如 ``PRIMARY_CHAT_MODEL`` 的取值）；
            用于埋点记录"用了哪个模型"，不得在代码里写死型号（NO-03 / 5.2）。
        guardrail: 输出护栏；``None`` 时使用放行实现（阶段二接入 NeMo Guardrails）。
        tracing: 埋点钩子；``None`` 时空操作实现。

    Returns:
        满足统一节点形态的可调用对象。
    """
    hook = tracing or NoOpTracingHook()
    guard = guardrail or PassThroughGuardrail()

    async def generate_answer(
        state: GovPolicyQaState,
        /,
        *,
        writer: StreamWriter = noop_stream_writer,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        """基于证据生成带引用的回答。

        Args:
            state: 图状态（只读）。
            writer: 流写入器；逐增量推送 ``message.delta``。
            config: 运行配置（取已认证租户，用于输出护栏）。

        Returns:
            含答案、状态与引用的部分状态字典。
        """
        evidence = list(state.get("evidence") or [])
        question = latest_user_question(state)
        if not question or not evidence:
            # 防御：正常由 route_after_retrieve 拦住；状态被外部直接构造时不得编造
            logger.warning(
                "agent.generate_without_evidence",
                extra={
                    "event": "agent.generate_without_evidence",
                    "has_question": bool(question),
                    "evidence_count": len(evidence),
                },
            )
            return answer_update(answer=_INSUFFICIENT_ANSWER, status=STATUS_INSUFFICIENT_EVIDENCE)

        tenant_id = tenant_id_from_config(config)
        messages = _build_messages(question, evidence)
        started = time.perf_counter()
        draft, token_usage = await _stream_answer(model, messages, writer)
        duration_ms = (time.perf_counter() - started) * 1000
        hook.record_model_call(
            logical_name=logical_name,
            prompt_version=ANSWER_SYSTEM_PROMPT,
            duration_ms=duration_ms,
            token_usage=token_usage,
            agent="gov_policy_qa",
        )

        indices = extract_citation_indices(draft, max_index=len(evidence))
        if not draft or not indices:
            logger.warning(
                "agent.answer_missing_citations",
                extra={
                    "event": "agent.answer_missing_citations",
                    "answer_chars": len(draft),
                    "evidence_count": len(evidence),
                },
            )
            return answer_update(answer=_CITATION_MISSING_ANSWER, status=STATUS_CITATION_MISSING)

        decision = await guard.check_output(text=draft, tenant_id=tenant_id)
        if not decision.allowed:
            logger.warning(
                "agent.answer_blocked",
                extra={
                    "event": "agent.answer_blocked",
                    "reason": decision.reason,
                    "code": decision.code.value if decision.code else None,
                },
            )
            return answer_update(answer=_BLOCKED_ANSWER, status=STATUS_GUARDRAIL_BLOCKED)

        logger.info(
            "agent.answer_generated",
            extra={
                "event": "agent.answer_generated",
                "tenant_id": tenant_id,
                "answer_chars": len(draft),
                "citation_count": len(indices),
                "duration_ms": round(duration_ms, 1),
                "token_usage": token_usage,
            },
        )
        return answer_update(
            answer=draft,
            status=STATUS_ANSWERED,
            citations=build_citations(evidence, indices),
        )

    return generate_answer


def _build_messages(question: str, evidence: list[EvidenceItem]) -> list[BaseMessage]:
    """渲染提示词：系统规则 + 用户消息（证据栅栏 + 问题）。"""
    blocks = [
        {
            "index": position,
            "document_name": neutralize_untrusted(item.document_name),
            "page_numbers": list(item.page_numbers),
            "content": neutralize_untrusted(item.content),
        }
        for position, item in enumerate(evidence, start=1)
    ]
    return [
        SystemMessage(content=render_prompt(ANSWER_SYSTEM_PROMPT)),
        HumanMessage(
            content=render_prompt(ANSWER_HUMAN_PROMPT, evidence=blocks, question=question)
        ),
    ]


async def _stream_answer(
    model: ChatModel, messages: list[BaseMessage], writer: StreamWriter
) -> tuple[str, dict[str, int] | None]:
    """流式生成答案：逐增量推流并拼接完整文本。

    Returns:
        ``(完整答案文本, token 用量)``；用量在网关未返回时为空。
    """
    parts: list[str] = []
    token_usage: dict[str, int] | None = None
    async for chunk in model.astream(messages):
        delta = message_text(chunk)
        if delta:
            parts.append(delta)
            writer(
                AgentStreamEvent(event=SseEventType.MESSAGE_DELTA, data={"delta": delta}).to_chunk()
            )
        usage = chunk.usage_metadata
        if usage:
            # 网关在流末给出累计用量；只取三个标准整型字段进埋点（CLAUDE.md 5.7）
            token_usage = {
                "input_tokens": int(usage.get("input_tokens", 0)),
                "output_tokens": int(usage.get("output_tokens", 0)),
                "total_tokens": int(usage.get("total_tokens", 0)),
            }
    return "".join(parts).strip(), token_usage


__all__: list[str] = ["build_generate_answer_node"]
