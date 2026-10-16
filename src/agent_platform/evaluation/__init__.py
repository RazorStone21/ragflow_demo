"""评估集与评测器（CLAUDE.md 9.2 / NO-16）。

**分层是"必不必要跑模型/基础设施"的分层，不是"要不要联网"的分层**——这两件事
常被混为一谈，混了就会把"纯函数判分"也塞进需要 Postgres 的测试里：

===========  ==================  ================================================
层           依赖                内容
===========  ==================  ================================================
A 确定性判分  无（纯函数）        ``scoring.py``：对已录制的产物按黄金集判分
B 系统执行    真基建 + 网关       ``runner.py``：跑检索与图，产出待判分的产物
C 裁判判分    真基建 + 裁判模型   LLM-as-judge（faithfulness / answer relevancy）
===========  ==================  ================================================

A 层放 ``tests/unit/``（快、确定、必跑），B/C 层放 ``tests/evals/``（默认 skip）。

模块职责：

- ``schemas``：三层共用的数据契约，唯一来源；
- ``loader``：黄金集装载与覆盖度报告；
- ``scoring``：A 层确定性判分（纯函数，零 I/O）；
- ``runner``：B 层执行与轨迹采集（复用 API 服务的装配路径）；
- ``reporting``：产物读写、基线对比、**配置漂移告警**。

**依赖方向**（CLAUDE.md 2.3 未列本包，此处明确）：
``evaluation → agents/core/knowledge/settings``，即评估层可以读运行时模块，
**但任何运行时模块不得 import 本包**——评估是旁路，不能进请求处理链路。
``tests/unit/test_evaluation_no_back_import.py`` 用 AST 扫描断言这一点。

已知缺口（诚实登记，不掩饰）：

- **HITL 无评估样例**：图中尚无 ``interrupt()``/``Command(resume=)``，因此
  ``GoldenCase`` 的 HITL 维度在首版黄金集中为 **0 条（N/A，非"已覆盖"）**。
  待 HITL 落地后补。
- **裁判与生成同源**：裁判模型复用 ``primary-chat`` 的上游，存在自偏好偏置，
   faithfulness 一类分数会偏乐观。产物与报告均须显式标注（见 ``JudgeSnapshot.caveat``）。
- **检索分路原始分未采集**：``EvidenceItem.vector_score``/``bm25_score`` 恒为 ``None``
  （原因见 ``knowledge/retriever.py`` 的 ``_to_evidence``）。补齐需改检索形态，
  属检索策略变更，按 NO-16 须先有基线，故本阶段只登记不改动。

规范偏离：本层使用 RAGAS，而 RAGAS 依赖已归档的 ``langchain-community``
（CLAUDE.md §1 / NO-14）。该偏离经用户授权并已在 [ADR-0003] 逐条登记，
集成形态为**隔离环境**（不写入 ``pyproject.toml``/``uv.lock``）。

[ADR-0003]: ../../../docs/adr/0003-evaluation-framework-and-judge.md
"""

from __future__ import annotations

from agent_platform.evaluation.schemas import SCHEMA_VERSION

__all__: list[str] = ["SCHEMA_VERSION"]
