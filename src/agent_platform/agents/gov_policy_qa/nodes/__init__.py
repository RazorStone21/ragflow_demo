"""节点实现：一节点一文件、单一职责（CLAUDE.md 2.2 / 4.2）。

节点统一形态：``async def node(state, /, *, writer, [config]) -> dict``

- ``state`` 位置参数只读：节点**不得**原地修改，只能返回部分状态字典（NO-04）；
- ``writer`` 由 LangGraph 注入（``StreamWriter``，CLAUDE.md 4.7）：节点用它推送
  ``AgentStreamEvent``；直接调用（单测）时默认空操作，无需构造运行时；
- ``config`` 仅在需要运行期上下文（如已认证租户）的节点上声明（CLAUDE.md 4.2 / NO-19）。

依赖（检索器、模型、护栏）经**工厂函数闭包**注入，节点不自行 new 客户端、
不拼 URL、不读环境变量（CLAUDE.md 2.3 / 4.8）。
"""

from __future__ import annotations

from agent_platform.agents.gov_policy_qa.nodes.answer_without_evidence import (
    build_answer_without_evidence_node,
)
from agent_platform.agents.gov_policy_qa.nodes.generate_answer import build_generate_answer_node
from agent_platform.agents.gov_policy_qa.nodes.result import answer_update
from agent_platform.agents.gov_policy_qa.nodes.retrieve_evidence import build_retrieve_evidence_node
from agent_platform.agents.gov_policy_qa.nodes.types import GovPolicyQaNode

__all__: list[str] = [
    "GovPolicyQaNode",
    "answer_update",
    "build_answer_without_evidence_node",
    "build_generate_answer_node",
    "build_retrieve_evidence_node",
]
