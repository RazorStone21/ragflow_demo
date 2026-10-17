"""图装配与编译（CLAUDE.md 4.8）：只声明节点/边/编译，不放业务实现。

拓扑::

    START → retrieve_evidence ─┬─(证据充足)→ generate_answer ─────────→ END
                               └─(其余情形)→ answer_without_evidence ─→ END

依赖（检索器、模型、checkpointer、store、护栏）全部经参数注入：模块导入期不连库、
不连网，开发与单测可注入 fake / 内存 checkpointer（CLAUDE.md 4.8 / NO-08）。

关于 ``langgraph.json``：其 ``graphs`` 指向本模块的 ``build_graph``（与 CLAUDE.md
附录示例一致）。**生产运行**由 api-server / worker 在进程启动时装配依赖后调用
``build_graph(**deps)``（见 ``api/deps.py``）；``langgraph dev``（LangGraph CLI）需要
零参数入口，而零参数无法在导入期安全地建立 Milvus/Postgres 连接（CLAUDE.md 4.8），
故本演示未提供零参数包装——如需接入 CLI，需先就 ``langgraph-cli`` 依赖立项评审。
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore

from agent_platform.agents.gov_policy_qa import edges, nodes
from agent_platform.agents.gov_policy_qa import state as qa_state
from agent_platform.core.models import ChatModel
from agent_platform.guardrails import Guardrail
from agent_platform.knowledge.retriever import Retriever

AGENT_VERSION = "0.2.0"
"""智能体版本（与 ``agents/registry.py`` 的声明保持一致）。"""


def build_graph(  # 依赖显式注入是本项目的硬性要求（CLAUDE.md 4.8）
    *,
    retriever: Retriever,
    chat: ChatModel,
    chat_logical_name: str,
    checkpointer: BaseCheckpointSaver[Any],
    store: BaseStore | None = None,
    guardrail: Guardrail | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """装配并编译 GovPolicyQa 图。

    Args:
        retriever: 证据检索器。
        chat: 聊天模型（经 LiteLLM 网关，CLAUDE.md 5.1）。
        chat_logical_name: 聊天模型逻辑名（埋点用，禁止在代码里写死型号，NO-03）。
        checkpointer: 检查点存储；**生产必须传 PostgresSaver**，单测可用内存实现
            （CLAUDE.md 4.4 / NO-08）。
        store: 长期记忆存储；本演示未启用（``None``）；接入长期记忆时传 PostgresStore。
        guardrail: 输出护栏；``None`` 时使用放行实现。

    Returns:
        已编译的图（可直接 ``ainvoke`` / ``astream``）。
    """
    builder: StateGraph[Any, Any, Any, Any] = StateGraph(qa_state.GovPolicyQaState)
    builder.add_node(qa_state.NODE_RETRIEVE_EVIDENCE, nodes.build_retrieve_evidence_node(retriever))
    builder.add_node(
        qa_state.NODE_GENERATE_ANSWER,
        nodes.build_generate_answer_node(chat, logical_name=chat_logical_name, guardrail=guardrail),
    )
    builder.add_node(
        qa_state.NODE_ANSWER_WITHOUT_EVIDENCE, nodes.build_answer_without_evidence_node()
    )

    builder.add_edge(START, qa_state.NODE_RETRIEVE_EVIDENCE)
    builder.add_conditional_edges(
        qa_state.NODE_RETRIEVE_EVIDENCE,
        edges.route_after_retrieve,
        [qa_state.NODE_GENERATE_ANSWER, qa_state.NODE_ANSWER_WITHOUT_EVIDENCE],
    )
    builder.add_edge(qa_state.NODE_GENERATE_ANSWER, END)
    builder.add_edge(qa_state.NODE_ANSWER_WITHOUT_EVIDENCE, END)

    return builder.compile(checkpointer=checkpointer, store=store)


__all__: list[str] = ["AGENT_VERSION", "build_graph"]
