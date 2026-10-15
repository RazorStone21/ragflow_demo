"""智能体（CLAUDE.md 2.2 / 第 4 章：一个智能体一个目录）。

目录约定（以 ``gov_policy_qa/`` 为例）::

    agents/<name>/
    ├── graph.py      # 仅装配：声明节点/边并编译（build_graph(**deps)）
    ├── state.py      # State Schema 与节点名常量（唯一来源）
    ├── schemas.py    # 智能体内部结构化输出/契约模型
    ├── edges.py      # 条件路由纯函数（route_after_*）
    ├── nodes/        # 一节点一文件，单一职责
    └── prompts/      # 版本化 Prompt 模板（.jinja），不得内联在节点里

依赖方向：``api → agents → core/tools/knowledge → utils``；``core``、``tools``、
``knowledge`` **不得**反向依赖任何具体智能体（CLAUDE.md 2.3）。
"""

from __future__ import annotations

from agent_platform.agents.registry import AGENTS, AgentSpec, agent_names, get_agent

__all__: list[str] = ["AGENTS", "AgentSpec", "agent_names", "get_agent"]
