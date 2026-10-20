"""评估数据契约（CLAUDE.md 3.3 / 9.2）。

本模块定义三层评估共用的全部结构，是整个评估层的**唯一契约来源**：

- **A 层（确定性判分）**：对已录制的产物做纯函数打分，离线、可单测。判据是
  ``GoldenCase`` 与 ``CaseResult`` 的比较。
- **B 层（系统执行）**：跑真实检索/图产出 ``CaseResult`` 与 ``RunMeta``。
- **C 层（LLM 判分）**：对 ``CaseResult`` 里的答案与证据做裁判打分，结果同样是
  ``MetricResult``。

三层的产物统一装进 ``EvalArtifact``——这样"基线对比"只需比较同一种结构，
而 `--layer` 跑到哪一层只影响 ``cases[].metrics`` 里有哪些指标。

设计要点：

1. **期望文档按名称匹配，不按 ``chunk_id``**。``chunk_id`` 沿用 RAGFlow 主键，
   重新解析入库后会整体变化；拿它当黄金集判据，会在下次重入库时集体失效。
   代价是放弃了"必须命中同一片段"的强度——这是有意的取舍，见 ``scoring.py``。
2. **``draft`` 与 ``reviewed_by`` 互斥由校验器强制**。两者都表达"这题算不算数"，
   但含义不同（后者还要求已按原文核对过）；允许两者同时成立就等于留了一个
   "标了草稿却混进门禁"的口子。
3. **基线产物必须剥离语料正文**。原始 run 产物含证据全文（裁判要用），
   而基线要入仓（``tests/evals/baselines/``），二者不能是同一份文件——
   ``EvalArtifact.compact_for_baseline()`` 负责这次剥离（NO-17）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_platform.agents.gov_policy_qa.schemas import Citation
from agent_platform.agents.gov_policy_qa.state import AnswerStatus
from agent_platform.knowledge.schemas import EvidenceItem, ImageRef, RetrievalMeta

SCHEMA_VERSION = 1
"""产物结构版本。对比时版本不符即判"基线不兼容"（退出码 4），不做跨版本推断。"""

STRIPPED_EVIDENCE_CONTENT = "[基线副本已剥离正文]"
"""入仓基线里 ``evidence[].content`` 的占位标记（见 ``compact_for_baseline``）。

用一句显然不是语料的标记，而不是空串：``EvidenceItem.content`` 要求非空，
空串会让整份基线读不回来（实测踩过）；而标记还能让人一眼看出"这行的正文被剥了"，
不必去翻文档才知道。"""


class CaseCategory(StrEnum):
    """黄金集类别（覆盖 CLAUDE.md 9.2 要求的典型/边界/对抗）。"""

    TYPICAL = "typical"
    BOUNDARY = "boundary"
    REFUSAL = "refusal"
    SECURITY = "security"


class EvalLayer(StrEnum):
    """本次执行跑到哪一层（决定哪些指标可采）。"""

    RETRIEVAL = "retrieval"
    """只检索，不调生成模型——最便宜，用于回归检索策略。"""

    GRAPH = "graph"
    """跑完整图（检索 + 生成），产出答案与引用。"""

    JUDGE = "judge"
    """在 GRAPH 产物之上加裁判打分（需裁判模型）。"""


class MetricName(StrEnum):
    """指标名（稳定枚举；聚合、阈值、报告的键统一取自此，禁止散落字面量）。"""

    RETRIEVAL_HIT = "retrieval_hit"
    CITATION_INTEGRITY = "citation_integrity"
    STATUS_ACCURACY = "status_accuracy"
    REFUSAL_CORRECTNESS = "refusal_correctness"
    TENANT_ISOLATION = "tenant_isolation"
    PROMPT_HYGIENE = "prompt_hygiene"
    FAITHFULNESS = "faithfulness"
    ANSWER_RELEVANCY = "answer_relevancy"
    IMAGE_EVIDENCE_HIT = "image_evidence_hit"
    """期望的图片（按文档名 + 页码）是否随证据被检索到（ADR-0004）。"""

    IMAGE_CITATION_INTEGRITY = "image_citation_integrity"
    """被引用的证据若带图，引用条目是否原样带出同样的图片（ADR-0004）。"""


class MetricVerdict(StrEnum):
    """单个指标的判定。

    ``NOT_APPLICABLE`` 是**一等结果**而非"0 分"：题目不涉及这项能力时（如事实题
    没有租户隔离语义）必须显式区分"没考"与"考砸了"，否则聚合分会被大量
    无意义的通过/失败稀释，门禁也就失去了分辨力。

    ``ERROR``（**补记 2026-10-16**）是第三种"没有分数"，与 ``NOT_APPLICABLE``
    的差别在**谁的问题**：前者是"这道题不考这项"，后者是"考了、但没测出来"
    （典型：裁判输出两次都解析不出约定结构）。它必须与 ``FAIL`` 分开，因为
    ``FAIL × 0.0`` 会把一次**测量故障**记成一个**质量数据点**：首次真实基线
    对比时就出现过——一条样例的 faithfulness 因裁判输出不可解析被记成
    ``0.0000``，于是聚合分被压低 1/23，报告里表现为"faithfulness 跌破"
    且该样例出现在"新失败样例"里，而模型什么都没做错。**测不出来必须可见**
    （所以它不是 ``NOT_APPLICABLE``，本条样例照样判不通过、整轮照样 FAIL），
    **但不得伪装成测量结果**（所以它不计入聚合，也不出现在分数表里）。
    """

    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    ERROR = "error"
    """未采集到结果：测量本身失败（裁判输出不可解析、超时等），**不带分数**。"""


class RunVerdict(StrEnum):
    """整份产物的判定。"""

    PASS = "pass"
    FAIL = "fail"
    INCOMPLETE = "incomplete"
    """有指标未被采集（例如 ``--skip-judge``）；**不得**当作通过。"""


class ReferenceSource(BaseModel):
    """参考答案的原文出处（评审人据此逐字核对）。"""

    model_config = ConfigDict(frozen=True)

    file: str = Field(min_length=1, max_length=512, description="语料文件名（不含目录）")
    page: int | None = Field(default=None, ge=1, description="页码；非 PDF 语料可为 None")
    quote: str = Field(
        default="", max_length=1000, description="原文摘录（核对锚点，便于在长文档中定位）"
    )


class ExpectedDocument(BaseModel):
    """期望命中的文档（按**文件名**匹配，见模块 docstring 第 1 条）。"""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=512, description="语料文件名（不含目录）")
    must_hit: bool = Field(default=True, description="未命中是否判失败；False 为弱期望")
    pages: list[int] = Field(default_factory=list, description="期望涉及的页码（弱校验，仅供参考）")


class ExpectedImage(BaseModel):
    """期望随证据出现的图片（ADR-0004）。

    按**文档名 + 页码**匹配而非 ``image_id``：图片 ID 由图片内容哈希生成，
    重新解析/重新导出都可能变（换了 OCR 引擎、裁切框变了都会变），拿它当判据
    会在每次重入库后集体失效——与 ``ExpectedDocument`` 按文件名匹配同理。
    """

    model_config = ConfigDict(frozen=True)

    document_name: str = Field(min_length=1, max_length=512, description="语料文件名（不含目录）")
    page: int | None = Field(
        default=None, ge=1, description="页码；独立图片文件等无页码的语料可为 None"
    )
    must_hit: bool = Field(default=True, description="未命中是否判失败；False 为弱期望")


class GoldenCase(BaseModel):
    """一条黄金集样例。

    题目、期望状态与原文出处三者必须齐全——只写题目不写"期望什么"的样例无法判分，
    只写期望不写出处的样例无法核对（CLAUDE.md 9.2"不得自评自满"）。
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(
        pattern=r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$",
        description="稳定标识（kebab-case）；重命名等同新增/删除，会使历史基线不可比",
    )
    category: CaseCategory = Field(description="类别（决定它计入哪个阈值组）")
    capability: str = Field(
        min_length=1, max_length=200, description="本题要考的能力（人读，用于覆盖度评审）"
    )
    question: str = Field(min_length=1, max_length=2000, description="提问原文")
    expected_status: AnswerStatus = Field(description="期望的答案状态（五态之一）")
    expected_documents: list[ExpectedDocument] = Field(
        default_factory=list, description="期望命中的文档；拒答类通常为空"
    )
    expected_images: list[ExpectedImage] = Field(
        default_factory=list,
        description="期望随证据出现的图片；空表示本题不考图片（多模态指标判不适用）",
    )
    reference_answer: str = Field(default="", description="参考答案全文；拒答题可为空字符串")
    reference_answer_source: ReferenceSource | None = Field(
        default=None, description="参考答案出处；有参考答案时必须给出"
    )
    notes: str = Field(default="", description="备注：为何这样出题、边界在哪里")

    # --- 评审留痕（CLAUDE.md 9.2"不得自评自满"的可执行形态）---
    draft: bool = Field(default=True, description="草稿；True 时不计入门禁")
    added_by: str = Field(default="", max_length=64, description="起草人")
    added_at: str = Field(default="", description="起草日期（ISO 8601 UTC 日期）")
    reviewed_by: str = Field(default="", max_length=64, description="评审人；留空即未评审")
    reviewed_at: str = Field(default="", description="评审日期（ISO 8601 UTC 日期）")

    @model_validator(mode="after")
    def _check_review_consistency(self) -> GoldenCase:
        """强制 ``draft`` 与已签署状态一致，并阻断"有答案却没出处"的样例。"""
        if self.draft and self.reviewed_by:
            raise ValueError("draft=True 与已签署（reviewed_by 非空）不能并存：请择一")
        if self.reference_answer and self.reference_answer_source is None:
            raise ValueError("给出 reference_answer 时必须给出 reference_answer_source（出处）")
        return self

    @property
    def is_reviewed(self) -> bool:
        """是否已签署（非草稿且有评审人）。门禁只统计已签署样例。"""
        return not self.draft and bool(self.reviewed_by)


class MetricResult(BaseModel):
    """单项指标的判定结果。"""

    model_config = ConfigDict(frozen=True)

    name: MetricName = Field(description="指标名")
    verdict: MetricVerdict = Field(description="通过/失败/不适用")
    score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="分数 [0,1]；不适用时为 None（**不是** 0）",
    )
    detail: str = Field(default="", description="人读的判定依据（失败时说明差在哪）")

    @model_validator(mode="after")
    def _check_score_consistency(self) -> MetricResult:
        """没有分数 ↔ 没有分数的那两种判定，保持一致。

        避免报告里出现“N/A 却 0.0 分”的自相矛盾；``ERROR`` 同理——它一旦带上
        ``score=0.0``，就又变回"测量故障冒充质量数据点"，正是该判定存在的理由。
        """
        scoreless = (MetricVerdict.NOT_APPLICABLE, MetricVerdict.ERROR)
        if self.verdict in scoreless and self.score is not None:
            raise ValueError(f"{self.verdict.name} 的指标不得带分数")
        if self.verdict not in scoreless and self.score is None:
            raise ValueError("通过/失败的指标必须带分数")
        return self


class CaseResult(BaseModel):
    """一条黄金集样例的实际执行结果（含可复核的中间产物）。"""

    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1, description="对应 ``GoldenCase.id``")
    category: CaseCategory = Field(description="类别（自黄金集复制，便于单独查看产物）")
    question: str = Field(description="本次实际发出的问题")
    layer: EvalLayer = Field(description="产出该结果的层")
    answer: str = Field(default="", description="图产出的答案全文")
    answer_status: str = Field(default="", description="图产出的答案状态")
    citations: list[Citation] = Field(
        default_factory=list,
        description="图产出的引用条目（**不是**正文里出现的下标——二者不一致本身就是缺陷，"
        "所以必须分别保存才能判得出来）",
    )
    evidence: list[EvidenceItem] = Field(
        default_factory=list,
        description="本次实际检索到的证据（``--show`` 复核与裁判上下文的来源）",
    )
    retrieval_meta: RetrievalMeta | None = Field(default=None, description="检索判定元数据")
    node_trace: list[str] = Field(default_factory=list, description="节点进入序列（轨迹评估用）")
    metrics: list[MetricResult] = Field(default_factory=list, description="本层产出的指标")
    judge_raw: str = Field(default="", description="裁判模型的原始输出（供人判“样例错还是模型错”）")
    latency_ms: int = Field(default=0, ge=0, description="本样例执行耗时（毫秒）")
    error: str = Field(default="", description="执行失败原因；非空表示本样例未产出有效结果")
    thread_id: str = Field(
        default="",
        description="本样例写入检查点时使用的 thread_id；检索层不写检查点故为空。"
        "会话段里的样例名是截断形式，**只能**从这里反查样例与 thread 的对应关系"
        "（清理评估检查点、把某条样例的图状态捞出来复现时用得上）",
    )

    @property
    def passed(self) -> bool:
        """本样例是否全部适用指标均通过（不适用不计入）。

        ``ERROR`` **计入** ``considered`` 并被判为不通过：一条样例只要有一项该测
        而没测出来，它就不是"通过"。这与"测不出来必须可见"是同一条取舍——把
        ``ERROR`` 挡在 ``considered`` 之外，会让裁判故障表现成"这道题少考一项"，
        整轮反而更容易报达标。
        """
        considered = [m for m in self.metrics if m.verdict is not MetricVerdict.NOT_APPLICABLE]
        return bool(considered) and all(m.verdict is MetricVerdict.PASS for m in considered)


class RetrievalSnapshot(BaseModel):
    """本次运行生效的检索策略快照（配置漂移的探测依据）。"""

    model_config = ConfigDict(frozen=True)

    top_k: int = Field(ge=1)
    candidates: int = Field(ge=1)
    similarity_threshold: float = Field(ge=0.0)
    rerank_score_threshold: float = Field(ge=0.0)
    vector_weight: float = Field(ge=0.0, le=1.0)
    dim: int = Field(ge=1)


class ModelSnapshot(BaseModel):
    """本次运行使用的模型**逻辑名**（不含型号、不含密钥；NO-03）。"""

    model_config = ConfigDict(frozen=True)

    chat: str = Field(min_length=1)
    embedding: str = Field(min_length=1)
    rerank: str = Field(min_length=1)
    judge: str = Field(default="", description="裁判模型逻辑名；未启用裁判时为空")


class CorpusSnapshot(BaseModel):
    """语料快照（探测"重入库/重解析导致的不可比"）。"""

    model_config = ConfigDict(frozen=True)

    collection: str = Field(min_length=1)
    chunk_count: int = Field(
        ge=0,
        description=(
            "本轮租户**可检索到的**切分总数——按 `tenant_id` 收口，与检索路径同口径"
            "（NO-19 的过滤是下推到 Milvus 的，别的租户的行压根检不到）。"
            "整集合计数会把与之无关的行算进来（实测：集成测试夹具残留把 195 顶成 201），"
            "让这个字段因语料没变而漂移"
        ),
    )
    manifest_sha256: str = Field(default="", description="入库幂等清单的指纹；留空表示未取到")


class JudgeSnapshot(BaseModel):
    """裁判配置快照（CLAUDE.md 9.2"裁判模型与标准固定并记录版本"）。"""

    model_config = ConfigDict(frozen=True)

    backend: str = Field(default="", description="裁判实现，如 in-house 或 ragas")
    library_version: str = Field(default="", description="裁判库版本；自研时为空")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int | None = Field(
        default=None,
        ge=0,
        description=(
            "裁判单次输出上限。它是 D8 的诱因之一（1024 → 4096），故与分数一并留痕："
            "改它等于改「测量失败」的发生频率，不记就无法在对比里看见。"
            "None = 未记录（本字段引入前落盘的旧产物），**不得**当作 0 使用"
        ),
    )
    prompt_version: str = Field(default="", description="裁判提示词文件名（版本化资产，§5.5）")
    caveat: str = Field(
        default="",
        description="已知偏置的如实登记（如裁判与生成同源）；报告须显式展示，不得当中性分数",
    )


class GoldenSnapshot(BaseModel):
    """黄金集快照（探测"黄金集被改绿"）。"""

    model_config = ConfigDict(frozen=True)

    sha256: str = Field(min_length=1, description="全部黄金集文件内容的联合指纹")
    case_count: int = Field(ge=0)
    reviewed_count: int = Field(ge=0, description="已签署样例数；小于 case_count 即含草稿")


class RunMeta(BaseModel):
    """一次评估运行的元信息（产物的可比性全部由本模型承载）。"""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    started_at: str = Field(min_length=1, description="ISO 8601 UTC（``Z`` 结尾）")
    git_sha: str = Field(default="", description="当前提交；工作区脏时不代表产物可复现")
    git_dirty: bool = Field(default=False, description="工作区是否有未提交改动")
    prompt_versions: dict[str, str] = Field(
        default_factory=dict, description="各提示词资产名 → 内容指纹（Prompt 变更的探测依据）"
    )
    models: ModelSnapshot
    retrieval: RetrievalSnapshot
    corpus: CorpusSnapshot
    judge: JudgeSnapshot = Field(default_factory=JudgeSnapshot)
    golden: GoldenSnapshot


class EvalArtifact(BaseModel):
    """一次评估运行的完整产物（``local_data/evals/runs/<run_id>.json``）。"""

    model_config = ConfigDict(frozen=True)

    schema_version: int = Field(default=SCHEMA_VERSION, description="产物结构版本")
    run: RunMeta
    cases: list[CaseResult] = Field(default_factory=list)
    aggregate: dict[str, float] = Field(
        default_factory=dict, description="指标名 → 聚合分（仅统计已知晓的指标）"
    )
    verdict: RunVerdict = Field(default=RunVerdict.INCOMPLETE)
    notes: list[str] = Field(
        default_factory=list, description="本次运行的已知限制（如裁判同源），报告须原样展示"
    )

    def compact_for_baseline(self) -> EvalArtifact:
        """剥离语料正文，得到可入仓的基线副本（NO-17）。

        去掉的是 ``evidence[].content`` 与 ``judge_raw``——前者是受版权/合规约束的
        语料正文，后者可能整段复述语料。二者都是**裁判层**才需要的重字段：
        基线对比只看指标与配置快照，因此剥离后不损失任何可比性。
        证据的 ``chunk_id`` / ``document_name`` / ``page_numbers`` / ``score`` 全部保留，
        这样基线仍能回答"当时命中了哪几份文档的哪几页"。

        **正文位置换成标记而不是空串**（2026-10-16 修正）：``EvidenceItem.content``
        要求 ``min_length=1``（"证据必须有正文"是检索侧的硬约定，不该为了这里放宽），
        而 ``model_copy`` **不做校验**，于是早先写的 ``content=""`` 一路写进了文件——
        直到 ``read_artifact`` 才炸。后果是**已入库的那份基线自己读不回来**，
        ``--baseline`` 对它恒报"基线不可读"，而 ``--promote-baseline`` 当时照样打印
        "基线已提升"：写路径从不校验，读路径才校验，两者的缝正好把它漏了过去。
        """

        def _strip_captions(images: list[ImageRef]) -> list[ImageRef]:
            """去掉图注：图注由 VLM 生成，可能整段复述原文（与正文同类风险）。"""
            return [image.model_copy(update={"caption": ""}) for image in images]

        stripped = [
            case.model_copy(
                update={
                    "evidence": [
                        item.model_copy(
                            update={
                                "content": STRIPPED_EVIDENCE_CONTENT,
                                "images": _strip_captions(item.images),
                            }
                        )
                        for item in case.evidence
                    ],
                    "citations": [
                        citation.model_copy(update={"images": _strip_captions(citation.images)})
                        for citation in case.citations
                    ],
                    "judge_raw": "",
                }
            )
            for case in self.cases
        ]
        return self.model_copy(update={"cases": stripped})


__all__: list[str] = [
    "SCHEMA_VERSION",
    "STRIPPED_EVIDENCE_CONTENT",
    "CaseCategory",
    "CaseResult",
    "CorpusSnapshot",
    "EvalArtifact",
    "EvalLayer",
    "ExpectedDocument",
    "ExpectedImage",
    "GoldenCase",
    "GoldenSnapshot",
    "JudgeSnapshot",
    "MetricName",
    "MetricResult",
    "MetricVerdict",
    "ModelSnapshot",
    "ReferenceSource",
    "RetrievalSnapshot",
    "RunMeta",
    "RunVerdict",
]
