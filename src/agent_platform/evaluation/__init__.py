"""评估集与评测器（CLAUDE.md 9.2 / NO-16，ADR-0006）。

**分层是"必不必要跑模型/基础设施"的分层，不是"要不要联网"的分层**——这两件事
常被混为一谈，混了就会把"纯函数判分"也塞进需要 Postgres 的测试里：

===========  ==================  ================================================
层           依赖                内容
===========  ==================  ================================================
A 确定性判分  无（纯函数）        ``scoring.py``：检索三项、引用完整性、拒答、
                                 租户隔离、提示词卫生、图片指标
B 系统执行    真基建 + 网关       ``runner.py``：跑检索与图，产出待判分的产物
C 裁判判分    真基建 + 裁判模型   ``ragas_judge.py``（RAGAS 三项）与
                                 ``judge.py``（引用准确率）
===========  ==================  ================================================

A 层放 ``tests/unit/``（快、确定、必跑），B/C 层放 ``tests/evals/``（默认 skip）。

模块职责：

- ``schemas``：三层共用的数据契约，唯一来源；
- ``loader``：黄金集装载、相关性标签校验与覆盖度报告；
- ``scoring``：A 层确定性判分（纯函数，零 I/O）；
- ``runner``：B 层执行与轨迹采集（复用 API 服务的装配路径）；
- ``ragas_backend``：RAGAS 0.4.3 的懒导入与客户端构造（``eval`` 依赖组）；
- ``ragas_judge``：RAGAS 三项的适用性、缓存与四态映射；
- ``judge``：引用裁判（``citation.judge.v1``），只判被引用的断言；
- ``reporting``：产物读写、基线对比、**配置漂移告警**。

**依赖方向**（CLAUDE.md 2.3 未列本包，此处明确）：
``evaluation → agents/core/knowledge/settings``，即评估层可以读运行时模块，
**但任何运行时模块不得 import 本包**——评估是旁路，不能进请求处理链路。
``tests/unit/test_evaluation_no_back_import.py`` 用 AST 扫描断言这一点。

已知缺口（诚实登记，不掩饰）：

- **HITL 无评估样例**：图中尚无 ``interrupt()``/``Command(resume=)``，因此
  ``GoldenCase`` 的 HITL 维度在黄金集中为 **0 条（N/A，非"已覆盖"）**。
  待 HITL 落地后补。
- **相关性标签未签署**：21 条可答题的 ``relevant_documents`` 待人工补齐；
  在此之前检索三项记 ``N/A``，检索层等于没考。
- **裁判与生成共用云端模型**：RAGAS 与引用裁判都走网关 ``judge``（云端 Qwen），
  独立性只在判分方法，不在模型；问题、证据、答案与参考答案会出域，产物里带
  ``JudgeSnapshot.caveat``。仅限公开演示语料（ADR-0005 / ADR-0006）。
- **检索分路原始分未采集**：``EvidenceItem.vector_score``/``bm25_score`` 恒为 ``None``
  （原因见 ``knowledge/retriever.py`` 的 ``_to_evidence``）。补齐需改检索形态，
  属检索策略变更，按 NO-16 须先有基线，故本阶段只登记不改动。

规范偏离：RAGAS 0.4.3 无条件依赖已归档的 ``langchain-community``（CLAUDE.md
§1 / NO-14）。该偏离经用户授权、逐条登记在 [ADR-0006]，形态是 **``eval`` 依赖组**：
默认 ``uv sync`` 不安装、运行时模块不得 import、生产镜像不得包含。

[ADR-0006]: ../../../docs/adr/0006-ragas-in-process-evaluation.md
"""

from __future__ import annotations

from agent_platform.evaluation.schemas import SCHEMA_VERSION

__all__: list[str] = ["SCHEMA_VERSION"]
