# ADR-0006：评估方案更换与 RAGAS 进程内判分

| 项 | 值 |
|---|---|
| 状态 | Accepted（生效中） |
| 决策日期 | 2026-10-21 |
| 决策者 | 需求方指定新指标表并要求 RAGAS 进入主流程；平台架构组执行并登记偏离 |
| 关联 | [CLAUDE.md](../../CLAUDE.md) 第 1 章评估行、第 9 章 9.2 / 9.3、第 10 章 10.1、NO-14、NO-16、NO-20；[ADR-0003](0003-evaluation-framework-and-judge.md)（本篇取代其"四、裁判模型"与"隔离环境离线判分"两节）；[ADR-0004](0004-multimodal-document-evidence.md)（图片指标保留）；[ADR-0005](0005-cloud-qwen-judge.md)（云端裁判选型不变） |
| 后续状态 | 不可就地修改；推翻时新增 supersede ADR |

## 背景

原评估方案以自研裁判（逐条断言判 faithfulness、模型自报 relevancy）加确定性判分器为主，RAGAS 只作**隔离环境**离线判分器：ADR-0003 授权它存在，但要求 `uv run --no-project --with ragas` 现场解析，项目锁文件零污染。

需求方于 2026-10-21 给出新的指标表，并要求两件事同时成立：

1. **检索侧换成 Recall@K / Precision@K / MRR**，工具为自研脚本 + LlamaIndex Evaluation；
2. **生成侧用 RAGAS 的 Faithfulness / Answer Relevancy / Answer Correctness**，另加引用准确率与拒答准确率；
3. "原来的测评不需要了"——`retrieval_hit`、`status_accuracy` 等旧指标退役。

RAGAS 进入主流程意味着它必须出现在 `uv.lock` 里，而 ragas 0.4.3 的 `requires_dist` **无条件**依赖已归档的 `langchain-community`，直接撞上 NO-14。这是本篇存在的原因：不是绕过规则，而是把偏离的范围、边界与回滚方式写清楚。

同时，自研裁判的两个指标被 RAGAS 取代后，原来"裁判与被评者同源"的偏置讨论（ADR-0003）与"Qwen 裁判"的选型（ADR-0005）都需要重新定位：Qwen 仍然判分，但判的是**引用准确率**，RAGAS 走它自己的提示词。

## 决策

### 一、依赖形态：`eval` 依赖组

```toml
[dependency-groups]
eval = [
    "ragas==0.4.3",
    "langchain-community>=0.3.31,<0.4",
]
```

- **精确锁定 `ragas==0.4.3`**：判分实现依赖它的私有方法 `_generate_statements`（见下文"空断言陷阱"），补丁升级也必须重验。
- **钉住 `langchain-community<0.4`**：ragas 对 langchain 系依赖不设上界，而 0.4.x 删除了它 import 的 `langchain_community.chat_models.vertexai.ChatVertexAI`，不钉住就会在下次解析时炸在 import 阶段。
- `uv sync` 默认不安装 `eval` 组；评估命令一律用 `uv run --group eval`。运行时、默认安装与生产镜像**不得**包含或导入 `langchain-community`、`ragas`。当前仓库没有 Dockerfile，生产部署命令在补 Dockerfile 时须按此核对，不得凭假设宣称已隔离。
- 实测锁定组合：ragas 0.4.3 / langchain-community 0.3.31 / langchain-core 1.6.10 / langchain-openai 1.7.2 / openai 3.3.0 / instructor 1.17.0 / datasets 5.1.0。`openai` 由 3.31.0 降到 3.3.0 是 instructor 的约束所致，属锁文件级变更，必须重跑全量单测与网关集成测试。

### 二、指标与口径（CLAUDE.md 9.2 的展开）

检索侧（确定性，零第三方依赖）：

| 指标 | 定义 | 阈值 |
|---|---|---:|
| `recall_at_k` | `#(相关文档 ∩ top-K 证据涉及文档) / #(相关文档)`，文档按 basename + casefold 去重 | 0.90 |
| `precision_at_k` | 相关证据块数 ÷ **实际返回证据块数**（≤K） | 0.60 |
| `mrr` | 首个来自相关文档的证据位次的倒数，无命中为 0 | 0.75 |
| `document_precision_at_k` | `#(相关文档 ∩ 证据文档集) / #(证据文档集)`，**仅监控** | — |

`K = EVIDENCE_TOP_K = 8`。Precision 用块级分母的理由：21 条可答题多数只有 1–2 份相关文档，固定分母 8 的理论上限只有 0.125–0.25，0.60 永远达不到。块级分母衡量"返回的证据里混入无关文档的比例"，文档覆盖率由 Recall 负责；`document_precision_at_k` 作为文档级诊断并行记录，用于区分"切分粒度问题"与"检索跑偏"。

生成侧（RAGAS 0.4.3）：

| 指标 | 采集条件 | 阈值 |
|---|---|---:|
| `faithfulness` | 有答案且至少 1 条证据 | 0.90 |
| `answer_relevancy` | 有答案 | 0.85 |
| `answer_correctness` | 有答案且有参考答案 | 0.85 |
| `hallucination_rate` | `1 − aggregate(faithfulness)`，**仅监控** | — |

`answer_correctness` 取 RAGAS 默认权重（0.75 事实性 + 0.25 语义相似度，`beta=1.0`）。期望 `answered` 而实际拒答或答案为空时，直接记 `answer_correctness = FAIL 0.0`，不调用 RAGAS——这是测得出的坏结果。拒答题的三项生成指标全部 `NOT_APPLICABLE`，由 `refusal_correctness` 兜底。

引用准确率（自研裁判）：

| 指标 | 定义 | 阈值 |
|---|---|---:|
| `citation_accuracy` | 被引用证据逐条判定"是否支持对应断言"，支持数 ÷ 引用总数 | 0.95 |

裁判输入只含被引用的证据块、答案与问题；输出严格为 `{"citations": [{"index", "supported", "reason"}]}`，索引集合必须与 `result.citations` 完全一致。结构上对不上（缺项、多项、重复、引用块无法映射证据）记 `FAIL 0.0`——那是测得出的坏结果；解析失败记 `ERROR`。无引用时记 `NOT_APPLICABLE`。

人工抽检是引用准确率的**独立校验**，不替代脚本判分：每轮从原始产物确定性随机抽最多 20 条引用做人工核对，一致率低于 0.90 时该轮 `citation_accuracy` 不得作为门禁依据。工作表与结论只写 `local_data/evals/audits/`，不入库（避免把证据原文带进入库产物）。

保留的红线（确定性）：`citation_integrity = 1.00`、`tenant_isolation = 1.00`、`prompt_hygiene = 1.00`、`image_citation_integrity = 1.00`、`image_evidence_hit = 0.90`。退役：`retrieval_hit`、`status_accuracy`。

### 三、黄金集标签

检索三项需要"相关文档全集"，这不是从检索结果里挑出来的集合——那样 Recall 会恒为满分。21 条 `answered` 样例由**人工**补齐 `relevant_documents`（文件名、页码、核对依据），并用独立的 `relevance_reviewed_by` / `relevance_reviewed_at` 签署；题目本身的审阅字段不动。11 条拒答/安全样例标签为空且不得有签署字段。标签未齐时 `--require-relevance-labels` 直接失败，不进入"样本不足"的软路径。

### 四、四态语义不变

`PASS` / `FAIL` / `NOT_APPLICABLE` / `ERROR` 沿用现有定义：`ERROR` 不带分数、从聚合中排除、但该样例判不通过。缺样本与测量失败一律不判通过。`answer_correctness` 的空断言陷阱（见下）映射为 `ERROR`，不得映射为 `1.0` 或 `0.0`。

### 五、结构性技术决定

**空断言陷阱**：RAGAS 0.4.3 的 `AnswerCorrectness` 在响应或参考答案抽取出的断言为空时，把事实性分直接置为 `1.0`。这不是"答案正确"，是"抽取失败被当成满分"。本仓实现 `StrictAnswerCorrectness` 覆写 `_generate_statements`，空列表抛错并映射为 `ERROR`。该实现依赖 ragas 私有方法，任何升级都要重验，这是精确锁版本的原因。

**遥测**：ragas 默认向 `t.explodinggradients.com` 发送遥测（稳定 UUID 与模型名）。`ragas_backend.py` 在**导入 ragas 之前**强制 `RAGAS_DO_NOT_TRACK=true`，不提供关闭开关（NO-15 的立法意图）。`.env.example` 保留该变量并注明代码会覆盖。

**代理**：RAGAS 的 LLM 与嵌入客户端复用 `core.models.build_http_async_client`（`trust_env=False`），避免 Windows 上 Clash 经注册表代理劫持 httpx。

**模型**：RAGAS 与引用裁判都经网关 `judge` 逻辑名，指向云端 Qwen（ADR-0005）。**独立性只在判分方法**——RAGAS 用其自带提示词，引用裁判用本仓提示词；两者共用同一模型，产物 `run.judge.caveat` 必须写明这一点。

## 备选方案与权衡

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 继续隔离环境跑 RAGAS（ADR-0003 原形态） | 锁文件零污染；NO-14 无冲突 | 每次评估现场解析依赖，同一次判分重跑未必同版本；无法进主流程与门禁 | 否决（需求方要求入主流程） |
| 保留自研 faithfulness / relevancy | 与证据绑定可逐条复核；无归档依赖 | 与需求方指定的 RAGAS 指标口径不一致，跨项目不可比 | 否决（旧指标退役） |
| DeepEval 替代 RAGAS | 维护更活跃 | 同样引入重型依赖，指标口径与需求方指定不符 | 否决 |
| **RAGAS 0.4.3 + eval 组（本篇）** | 指标口径对齐需求方；版本锁定；默认环境零污染 | 锁文件含归档依赖；库约 9 个月无发布；依赖私有方法 | **采用（限公开演示语料）** |

## 后果

**正面**：

- 指标口径与需求方给定表逐项对齐，检索侧有可解释的公式与标签，生成侧有跨项目可比的 RAGAS 分数；
- 偏离被关进 `eval` 依赖组，默认安装与运行时不受影响；
- 引用准确率有独立的人工抽检校验，脚本判分不再是唯一证据。

**负面与风险**：

- `uv.lock` 含已归档的 `langchain-community`，NO-14 的偏离范围必须靠"默认不安装"来守住，任何把 eval 组装进生产镜像的操作都破坏了这个边界；
- ragas 0.4.3 约 9 个月无发布，且 `StrictAnswerCorrectness` 依赖私有方法，升级成本高；
- **数据出域扩大**：RAGAS 会把问题、证据、答案、**参考答案**发往云端 Qwen，Answer Relevancy / Correctness 还会调用云端嵌入。仅限公开演示语料；客户数据与生产环境不得使用；
- RAGAS 内部提示词不接受本仓的 `neutralize_untrusted` 中和，证据与答案中的注入文本对 RAGAS 裁判是可见的；引用裁判已中和。公开演示语料可接受，生产需换本地裁判或评估 RAGAS 的提示词覆盖；
- 旧基线（schema v1）新代码读入即报不可读，只能作历史；新基线必须在标签齐备、语料就绪、工作区干净后重新生成；
- 单条约 8 次模型调用，21 条串行约 10–20 分钟；判分缓存是必要的成本控制，`--no-judge-cache` 用于核对缓存是否污染。

## 合规与许可证

- 这是对 NO-14 的**授权偏离**，范围仅限 `eval` 依赖组；除 ragas 的传递依赖外，不得新增任何对 `langchain-community` 的引用，也不得援引本篇为其它库开同类口子；
- 对 CLAUDE.md §9.2（裁判模型与标准固定）与 §10.1（数据不出域）的演示期偏离同 ADR-0005 的边界：仅限公开演示语料；
- ragas 为 Apache-2.0；`langchain-community` 为 MIT（已归档）；依赖链中的 `datasets`、`instructor`、`scipy` 等许可证以各自发布页为准，正式交付前由法务复核 SBOM；
- Qwen 权重许可证与服务条款的处理同 ADR-0005，本篇不重复。

## 迁移与回滚

**迁移**：`uv sync --group eval` → 按新 schema 重跑评估 → 人工补齐标签并签署 → 语料就绪后生成新基线。旧产物与旧基线保留原样，不迁移、不兼容。

**回滚**：从 `pyproject.toml` 删除 `eval` 组、`uv lock` 重新生成、删除 `ragas_backend.py` / `ragas_judge.py` 与引用裁判，恢复 ADR-0003 的隔离脚本形态。旧产物的分数与 schema 一并回滚；跨形态的分数不可比。

## 未决与后续事项

- `scripts/eval_label_worksheet.py` 只生成工作表，不写黄金集；21 条标签的签署是用户的人工步骤；
- 引用抽检的样例量（20）与一致率阈值（0.90）为首轮取值，按实际耗时与分歧率校准；
- 语料就绪（v2 集合补全 19 份文本文档或切换集合）是本轮产出正式分数的前置条件，未就绪时交付停在代码、单测与工作表；
- 生产环境的私有化 RAGAS 裁判与嵌入选型需另立 ADR，并完成算力与精度压测。
