"""政务能源政策问答智能体（gov_policy_qa）。

职责：基于 Milvus 混合检索（ADR-0002）得到的证据回答政务能源政策问题，
答案必须带引用；检索不到时明确拒答并给出澄清引导（CLAUDE.md 7.5 / NO-12）。

模块划分（CLAUDE.md 2.2）::

    state.py    State Schema（唯一来源：字段、reducer、节点名、状态常量）
    schemas.py  智能体内部契约（引用条目、引用标记解析）
    edges.py    条件路由纯函数（route_after_retrieve）
    nodes/      节点：一节点一文件（检索 / 生成 / 无证据降级）
    graph.py    装配与编译（build_graph(**deps)）
    prompts/    版本化提示词模板

本包 ``__init__`` 刻意**不导入** graph/nodes：保持轻量可导入，避免任何模块
在导入期拉起重依赖或产生副作用（CLAUDE.md 4.8）。
"""

from __future__ import annotations

__all__: list[str] = []
