# ADR-0003：评估框架（RAGAS 经授权的偏离形态）与裁判模型选型

| 项 | 值 |
|---|---|
| 状态 | Accepted（生效中） |
| 决策日期 | 2026-10-16 |
| 决策者 | 平台架构组；**使用 RAGAS 与选用 DeepSeek 裁判均由需求方明确授权** |
| 关联 | [CLAUDE.md](../../CLAUDE.md) 第 9 章（尤其 9.2）、第 5 章第 8 条（裁判须用私有化本地模型）、**NO-14**、NO-15、NO-16；[ADR-0001](0001-langgraph-as-orchestration-backbone.md)、[ADR-0002](0002-ragflow-milvus-division-of-labor.md) |
| 后续状态 | 不可就地修改；如需推翻（例如 ragas 恢复维护、或改用私有化裁判），新增 supersede ADR 并指向本篇 |

## 背景

CLAUDE.md §9.2 要求每个智能体与每条 RAG 管线维护**黄金集**与轨迹评估，并规定 Prompt / 模型 / 检索策略 / 图结构 / 护栏的变更**必须**跑评估回归且达标方可上线（NO-16）。在本篇之前，`evaluation/` 是空壳，门禁链里没有任何一步在度量质量——**NO-16 客观上无法被遵守**，任何调参都是裸奔。

因此需要选一套评估框架。候选与最终结论见"备选方案"，本篇记录的是**经授权的偏离如何落地、边界划在哪里**，以及**裁判模型的偏置如何如实登记**。

## 决策

### 一、RAGAS 采用"隔离环境判分器"形态（形态 2），交付物零污染

RAGAS **不进入** `pyproject.toml` / `uv.lock`。它以**独立工具环境**运行，只吃本平台录制的产物：

```bash
uv run --no-project -p 3.12 \
  --with "ragas>=0.4.3" --with "langchain-community<0.4" --with "langchain-openai" \
  python scripts/eval_ragas.py <产物路径>
```

成立的**前提是判分器不 import 本仓模块**——RAGAS 的全部输入（`user_input` / `response` / `retrieved_contexts`）都能从录制的产物里取到，无需运行时对象。这与评估层"确定性层只对已录制产物做纯函数打分"的分层设计天然契合。

**实测证据（2026-10-16，本机 Win11，Python 3.12）**：

| 项 | 实测值 |
|---|---|
| 项目 `uv.lock` 影响 | **无**。跑完后 `uv.lock` sha256 前后逐字节一致（`4227704d1c5b65753edfc204b820446133579fb0106bad86599eec87f3078735`） |
| 可跑性 | 跑通。合成中文样例：答案有据 → `Faithfulness = 1.0`；注入幻觉对照 → `0.5`。**判分有分辨力**，不是恒 1 的空转 |
| 调用路径 | 经**网关**（`base_url=http://localhost:4000`），未新增任何厂商密钥 |
| 落地版本 | ragas 0.4.3 / langchain-community **0.3.31**（`<0.4` 这条 pin 是让它能 import 的关键）/ langchain-core 1.6.11 / langchain-openai 1.7.2 / openai 3.3.0 / datasets 5.1.0 / instructor 1.17.0 |
| 现代 API | `ragas.metrics.collections.Faithfulness` + `llm_factory(..., provider="openai", client=openai.AsyncOpenAI(...))` → `await metric.ascore(...)`。**`LangchainLLMWrapper` 不需要**，且现代 collections 指标显式拒收 legacy wrapper |
| 总耗时 | 单条样例约 3.7s |

### 二、RAGAS 默认外发遥测，必须显式关闭（**本次实测新增的发现**）

这不是"可能"，是**实测发生了**：默认配置下 ragas 向 `t.explodinggradients.com` 发出 HTTPS POST，服务端返回 **200（已送达）**。设置 `RAGAS_DO_NOT_TRACK=true` 后，外部 DNS 解析与 HTTP 请求**归零**（同时用 DNS 与 requests 双重探针验证）。

| 事实 | 内容 |
|---|---|
| 端点 | `ragas/_analytics.py:37` `USAGE_TRACKING_URL = "https://t.explodinggradients.com"` |
| 触发 | 模块 import 即启动 `AnalyticsBatcher` 守护线程（batch=10 / flush 10s）+ `atexit` flush；`requests.post(..., timeout=1)`，异常被 `silent()` 吞掉 |
| 载体 | `llms/base.py`（provider、model name）、`metrics/base.py`（metric name、num_rows、language） |
| 用户标识 | `_analytics.py:80-105` `get_userid()`：在 appdirs 用户数据目录持久化一个 uuid，**每个事件都带上** |
| 关闭方式 | `RAGAS_DO_NOT_TRACK=true`（`do_not_track()` 为 `lru_cache`，**只认字面量 `"true"`**，且必须在首次 track 前设置） |
| 载荷内容 | 元数据（指标名、行数、语言、ragas 版本、随机 uuid、模型名），**不含问题/答案正文** |

**结论**：载荷虽不含语料正文，但"元数据 + 稳定用户标识默认外发"已足以构成数据出域（CLAUDE.md 10.1 / NO-15 的立法意图）。`scripts/eval_ragas.py` **必须**在导入 ragas 之前设置 `RAGAS_DO_NOT_TRACK=true`，并在产物里记录该开关已生效。

### 三、裁判分层：确定性层为主干，裁判层为补充

§9.2 要求的覆盖面里大部分**不需要模型**。故评估分三层，成本与可信度都不同：

| 层 | 内容 | 是否需模型 | 是否需基础设施 |
|---|---|---|---|
| A 确定性判分 | 引用完整性、检索命中、状态一致、拒答正确性、租户隔离、提示词卫生 | 否 | **否**（纯函数，离线秒级） |
| B 系统执行 | 跑真实检索/图，录制产物 | 是（生成） | 是 |
| C 裁判判分 | faithfulness、answer relevancy | 是（裁判） | 是 |

同一指标（faithfulness / answer_relevancy）**允许两套后端并存**，但**不得写进同一份产物的同一个指标位**：自研裁判写主产物，RAGAS 写旁挂文件（`local_data/evals/ragas/<run_id>.json`），靠 `JudgeSnapshot.backend` / `library_version` 区分。二者可交叉对照——**若两边分数长期系统性背离，那本身就是"某一方不可信"的信号**，比单看一个数字有价值。

**C 层独有的第三种结果**：裁判是**会失败**的一层（输出不可解析、超时），而 A 层是纯函数、不会"测不出来"。因此契约里必须把"测不出来"（`MetricVerdict.ERROR`）与"考砸了"（`FAIL`）分开——早先把前者记成 `score=0.0`，等于让裁判的一次故障伪装成模型的一次质量回归（实测发生过：聚合分被压低 1/23、报告显示"跌破"、样例进"新失败样例"）。取舍是**两条都要**：不带分数、不进聚合（不污染测量），但该样例判不通过、整轮 FAIL（可见性不减）。

### 四、裁判模型：DeepSeek（网关逻辑名 `judge`），同源偏置如实登记

需求方选定 **DeepSeek**，复用现有 `primary-chat` 上游。**这是经权衡的取舍，代价必须写明**：

> **裁判与被评者同源**（`judge` 与 `primary-chat` 在网关指向同一型号），存在**自偏好（self-preference）偏置**：模型倾向给自己家族写的答案打高分，faithfulness / answer_relevancy 会**偏乐观**，**不得当中性分数使用**。

该偏置的处理方式是**登记而非掩盖**：

1. 常量 `SAME_SOURCE_CAVEAT`（`evaluation/judge.py`）写入每份产物的 `run.judge.caveat`；
2. 报告渲染时与分数**并排展示**；
3. 登记在 `CHANGELOG.md` 与 `.env.example` 的告警注释里；
4. 生产须换**独立家族**的私有化裁判模型（§5.8），届时按 §9.2 跑一次回归确认分数变化并留痕。

**注意**：`judge` 与 `primary-chat` 必须是**两个逻辑名**，哪怕当前指向同一型号。评测要固定裁判版本以保跨版本可比，生成侧却要随产品迭代调型；合并成一个逻辑名会让"只换生成模型、不动裁判"做不到。

### 五、不采用 Promptfoo

Promptfoo 及其依赖链需要 **Node.js 工具链**，而本仓是 uv workspace monorepo、交付目标为离线 K8s（§11.8 要求断网环境自包含）。引入它等于在 Python 交付物里塞进第二套包管理与运行时，收益不抵成本。DeepEval 同样不采用——其能力与自研 A/C 层重叠，而重叠部分我们已经用纯函数实现且可 100% 单测覆盖。

## 备选方案与权衡

| 方案 | 权衡 | 结论 |
|---|---|---|
| **RAGAS 进项目依赖** | 最省事，但 ragas 0.4.3 对 `langchain-community` 是**无条件**依赖 → 硬性违反 **NO-14**；且其 import 路径 `from langchain_community.chat_models.vertexai import ChatVertexAI` 在 0.4.x 已被移除，**装上也 import 不了** | **否决** |
| **RAGAS 隔离环境判分器**（采纳） | 交付物零污染、NO-14 影响面压到最小；代价是**多一个手动步骤**（判分需先跑 B 层录产物），且需维护一条 `--with` 命令行 | **采纳** |
| **完全自研，不引入 RAGAS** | 无任何依赖违规、无疑似外发；代价是放弃成熟实现与社区口径，faithfulness 这类指标要自己重造 | **部分采纳**：A 层与 C 层自研并作为主干，RAGAS 仅作旁证 |
| **Promptfoo / DeepEval** | 见"决策·五" | 否决 |
| **LangSmith 评估** | SaaS，数据出域 | 否决（NO-15） |

## 后果

**正面**：

- NO-16 从"纸面规则"变成**可执行门禁**：有黄金集、有产物、有基线对比、有配置漂移告警。
- 交付物**不含** `langchain-community`，NO-14 被隔离在工具环境里。
- 语料出域的边界收窄：遥测已定位并有关闭开关，产物留在本地 `local_data/`。
- 判分结果**可人工复核**：faithfulness 保存逐条断言清单而非仅一个总分，`judge_raw` 保留原始裁判输出，人能判"是样例错了还是模型错了"。

**负面 / 风险**：

- **裁判同源偏置**（见"决策·四"）——这是本篇最需要在每次汇报时重复说明的一条。
- **首份基线的若干数字反映的是缺陷，不是能力**：`refusal_correctness = 0.4545`、`status_accuracy = 0.7857` 由生成侧缺陷 D1 主导（见"未决"第 8 条），`answer_relevancy = 0.7714` 部分由裁判资产缺陷 D2 造成（第 7 条）。**这些数字在 D1 / D2 修复前不得单独引用为质量结论**，只能引用为"缺陷已可度量"的证据。
- RAGAS **0.4.3 发布于 2026-01-13，距今约 9 个月无更新**，且依赖链会把已归档库带进**工具环境**（不进交付物）。**触发条件**：若长期仍不更新，或现代 API 出现不可绕过的缺陷，则重新评估是否整体迁回自研判分。
- 隔离形态意味着 RAGAS 的分数**不参与**主产物门禁判定，只作旁证——门禁判定完全由自研 A 层 + C 层承担。

## 合规与许可证

### 本次经授权的规范偏离（逐条登记，不静默）

| 条例 | 原文 | 违反事实（已核实） | 影响面 |
|---|---|---|---|
| **NO-14** | "不得新增对已归档的 `langchain-community` 的依赖" | ragas 0.4.3 的 `requires_dist` 含**无条件** `langchain-community`（PyPI 元数据核对，无版本修饰、无 extra 限定） | **已隔离**：仅存在于工具环境，**不进** `pyproject.toml` / `uv.lock` / 交付镜像 |
| **§1 技术栈表 SDK 行** | "`langchain-community` 已归档，**禁止新增引用**" | 同上（NO-14 在技术栈表内的重申） | 同上 |
| **§3.8 依赖管理** | 新增依赖前必须确认 ①必要性 ②许可证 ③私有化可获取 ④维护活跃度 | ③④ 存疑：9 个月无更新；依赖链引入已归档库 | 同上；已设"重新评估"触发条件 |

**授权留痕**：需求方于 2026-10-16 明确授权使用 RAGAS，**并要求把被违反的条例标注出来**。本篇即该标注的落点。

**关键缓解**：经 spike 实测（见"决策·一"表的锁文件指纹证据），**项目的 `pyproject.toml` 与 `uv.lock` 零改动**，因此上述违规**不进入交付物**，只影响开发期的评估工具环境。这也是选择形态 2 而非形态 1 的决定性理由。

### 许可证

- ragas：Apache-2.0；langchain-community：MIT；二者在**工具环境**中使用，不随产品分发。
- 裁判模型（DeepSeek）：经**网关**调用，业务代码不含厂商 SDK、不含厂商密钥（NO-02 / NO-03）。
- **生产须换本地私有化裁判模型**（§5.8：评估/裁判模型在私有化场景应使用本地模型，数据不得出域）。本篇生效期内该项为**演示期偏离**，与 ADR-0002 登记的"演示期云端嵌入/重排"同性质，按同一先例**封边界、入册**。

## 迁移与回滚

- **回滚 RAGAS**：删除 `scripts/eval_ragas.py` 与旁挂产物即可。因 `pyproject.toml` / `uv.lock` **从未被修改**，无依赖需要回退——这是形态 2 的主要好处。
- **回滚裁判模型**：改网关 `judge` 路由的映射，业务代码零改动（NO-02）。换型后**必须**按 §9.2 重跑一次评估并留痕，因为分数会变。
- **裁判偏置的消除路径**：接入独立家族的私有化裁判模型 → 更新 `judge` 路由 → 跑回归 → 对比新旧分数并记录差异幅度（差异幅度本身就是"偏置有多大"的量化答案）。

## 未决与后续事项（诚实记录）

1. **裁判同源偏置尚未量化**。当前只知道"偏乐观"，不知道乐观多少。待私有化裁判接入后做一次对照即可给出数字。
2. **HITL 无评估样例**。图里尚无 `interrupt()`，`human.interrupt` 事件从未发出，故黄金集该类别为 **0 条（登记 N/A）**，不得编造。schema 已预留取值，待 HITL 落地补齐。
3. **`_to_evidence` 的分路分数（`vector_score` / `bm25_score`）恒为 `None`**。根因已查明：Milvus `hybrid_search` + `WeightedRanker` **只返回融合分**，分路原始分在数据源就没拿到。取分需改成两次独立 `search`，属**检索策略变更**，按 NO-16 须**先有基线**，故本期只登记不动手。
4. **RAGAS 遥测的关闭依赖运行者**。`RAGAS_DO_NOT_TRACK=true` 是环境变量，任何绕过 `scripts/eval_ragas.py` 直接调用 ragas 的做法都会重新打开外发。已在脚本内强制设置，但无法从代码层阻止他人另起调用。
5. **`judge` 路由需容器重建才生效**。实测运行中的网关只加载了 `primary-chat` / `embedding` / `rerank`（容器创建早于配置变更）。需 `docker compose up -d litellm` 重建后 `judge` 才可用——**属环境前置条件，不是代码缺陷**。
6. **RAGAS 隔离环境未钉版本，留痕的可复现性有缺口**。隔离环境由 `uv run --no-project --with …` 现场解析，**没有锁定文件**，解析结果可随上游发布漂移：本轮解析出 `ragas 0.4.3` / `langchain-community 0.3.31` / `langchain-core 1.6.11` / `langchain-openai 1.7.2`，而 ragas 的约束并未钉死其中任何一项。后果是同一份产物换台机器重判，未必得到同一分数。**缓解**：判分库与版本已随产物的 `backend.dependencies` 落盘，比对分数前先比这一栏；**不得**用"重跑一次分数不同"直接判定模型或检索发生了变化。彻底修法是给隔离环境一份自己的锁文件（或把 RAGAS 分数整体降级为"仅供参考"），待 RAGAS 迁回自研与否的决策一并处理。
7. **裁判提示词 `relevancy.judge.v1` 与 NO-12 冲突（缺陷 D2）**。其规则 2 只区分"本可回答却拒答／确实超范围而拒答"，**缺"即使答得出也必须拒答"这一档**，于是把"拒绝逐字输出系统提示词"这一**正确**行为判成不当拒答。后果直接落在本篇授权的那两个裁判指标上：`answer_relevancy` 当前**不可作为质量结论使用**。修法是补一档（属 §9.2 管辖的 Prompt 资产变更，必须跑回归并留痕）。评分器的"不正确"会伪装成"模型的缺陷"，这是引入裁判层必然要付的代价，也是本 ADR 把原始裁判输出落盘的原因。
8. **生成侧缺"模型自称无法回答"分支（缺陷 D1）**。`agents/gov_policy_qa/nodes/generate_answer.py` 在模型输出"现有资料未涵盖……"时仍记 `answered`，并保留 `[1]` 之类引用标记——而引用指向并不支持该结论的切片。这**不是评估问题而是图缺陷**，且是 **NO-12 的实质违反**（无证据却给出了带引用的体面回答）。已将 6 条受影响样例与其证据登记进首份基线，按 NO-16 跑回归后修复；`citation_integrity = 1.0` 是**结构性**检查，结构上无法发现此类错误——这也说明确定性判分器覆盖不到的地方，正是裁判层存在的理由。
9. **裁判指标的逐轮噪声尚未消除（实测）**。生成侧 `temperature` 非 0，同一份代码重跑会产出**不同文本的答案**（实测：同题首轮"结论：两份文件分别就…"、次轮"现有资料显示，两份文件分别对…"），于是裁判缓存必然未命中、重新判分，`faithfulness` 一轮内抖动 −0.0622、`answer_relevancy` +0.0304；同期**确定性指标逐位相同**。结论：`--baseline` 的裁判指标 Δ **不构成回归证据**，须结合答案原文与 `judge_raw` 人工判断（四条读数约束见 `tests/evals/README.md` 第 5.4 节）。彻底消除须让评估时的生成侧固定随机性（`temperature=0` 或固定种子），属图/模型配置变更，按 NO-16 排在基线之后。**另记**：首份基线已逐条核查无测量失败，故其 0.9392 是干净值——"哪一份产物可以当基线"因此多了一条检查项。
