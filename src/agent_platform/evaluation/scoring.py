"""A 层确定性判分（CLAUDE.md 9.2 / NO-12 / NO-16）。

**全部是纯函数**：入参只有已录制的产物与黄金集，零 I/O、零网络、零时钟，
因此可以在 ``tests/unit/`` 里穷举分支，跑得比一次模型调用还快。

三条贯穿全局的判分原则：

1. **不做语义相似，只做确定性比对**。文档命中按文件名比对、引用按下标与
   ``chunk_id`` 比对、状态按枚举比对。任何"看起来像"的模糊判定都交给 C 层裁判，
   而不是混进这一层——否则"哪些分数是可信的"就没有答案了。
2. **"没考"与"考砸了"必须分开**。题目不涉及的能力返回 ``NOT_APPLICABLE`` 且
   ``score=None``；把它记成 0 分会让聚合分被大量假失败淹没，记成 1 分则更糟——
   那是凭空刷高通过率。``MetricResult`` 用校验器强制该不变量。
3. **判据只用黄金集里写死的东西**。绝不拿"系统的输出"去推断"系统应该输出什么"，
   那是最隐蔽的自评自满（CLAUDE.md 9.2）。
"""

from __future__ import annotations

from collections.abc import Iterable

from agent_platform.agents.gov_policy_qa.schemas import extract_citation_indices
from agent_platform.agents.gov_policy_qa.state import AnswerStatus
from agent_platform.evaluation.schemas import (
    CaseCategory,
    CaseResult,
    EvalLayer,
    GoldenCase,
    MetricName,
    MetricResult,
    MetricVerdict,
)
from agent_platform.guardrails.sanitize import EVIDENCE_CLOSE_MARKER, EVIDENCE_OPEN_MARKER
from agent_platform.knowledge.schemas import EvidenceItem

REFUSAL_STATUSES: frozenset[str] = frozenset(
    {
        AnswerStatus.INSUFFICIENT_EVIDENCE.value,
        AnswerStatus.CITATION_MISSING.value,
        AnswerStatus.GUARDRAIL_BLOCKED.value,
    }
)
"""视为"拒答"的状态：三者都不构成事实性结论，只是拒因不同。

``pending`` **不在其中**——它表示流程没跑完，属于执行失败而非拒答，
按拒答给它打分等于把"跑挂了"记成"拒得对"。
"""

_NO_INDEX_LIMIT = 10**9
"""收集正文引用下标时的"无上限"取值。

用来**复用同一个解析器**：``extract_citation_indices`` 本就会丢弃越界下标，
而越界引用恰恰是要抓的缺陷，所以先取一个不可能越界的上界把全部下标收集齐，
再自行判定合法与否。刻意不另写一份正则——同一份语法解析两遍是缺陷的温床
（CLAUDE.md 3.1）。
"""


def score_retrieval_hit(case: GoldenCase, result: CaseResult) -> MetricResult:
    """证据是否命中了全部 ``must_hit`` 期望文档。

    按**文件名**比对（去目录、大小写归一），不做语义相似、不看片段内容。
    这样判据与"是否命中同一片段"解耦：重解析入库会改变 ``chunk_id``，
    但不该改变"这份文档被检索到了"这一事实。

    Args:
        case: 黄金集样例。
        result: 该样例的执行结果。

    Returns:
        ``NOT_APPLICABLE``（没有 must_hit 期望，如拒答题）；否则为命中比例，
        全部命中记 ``PASS``。
    """
    required = [doc.name for doc in case.expected_documents if doc.must_hit]
    if not required:
        return _na(MetricName.RETRIEVAL_HIT, "本题未声明必须命中的文档")

    found = {_normalize_name(item.document_name) for item in result.evidence}
    found |= {_normalize_name(item.source_uri) for item in result.evidence}
    missing = [name for name in required if _normalize_name(name) not in found]
    hit = len(required) - len(missing)
    score = hit / len(required)
    if not missing:
        return _metric(MetricName.RETRIEVAL_HIT, MetricVerdict.PASS, score, f"全部命中（{hit} 份）")
    return _metric(
        MetricName.RETRIEVAL_HIT,
        MetricVerdict.FAIL,
        score,
        f"未命中 {len(missing)}/{len(required)} 份：{'、'.join(missing)}",
    )


def score_status(case: GoldenCase, result: CaseResult) -> MetricResult:
    """实际答案状态是否等于期望状态。"""
    actual = result.answer_status
    expected = case.expected_status.value
    if actual == expected:
        return _metric(MetricName.STATUS_ACCURACY, MetricVerdict.PASS, 1.0, f"状态为 {actual}")
    return _metric(
        MetricName.STATUS_ACCURACY,
        MetricVerdict.FAIL,
        0.0,
        f"期望 {expected}，实际 {actual or '<空>'}",
    )


def score_citations(case: GoldenCase, result: CaseResult) -> MetricResult:
    """引用完整性（NO-12 红线，阈值 1.00，不留余地）。

    四项判据，任一不满足即失败：

    1. 正文出现的**每一个** ``[n]`` 都指向真实存在的证据（越界即失败）；
    2. 正文下标集合与引用列表的下标集合**完全一致**（防"正文写了 [3]、引用列表却是 [1]"）；
    3. 每条引用条目与它指向的证据 ``chunk_id`` 相同、文档名一致（防**编号错位**——
       这正是阶段五实测缺陷之一）；
    4. ``answered`` 状态必须至少有一条引用（无引用不得作为事实结论）。

    Args:
        case: 黄金集样例（未使用，保持统一签名以便批量分派）。
        result: 该样例的执行结果。

    Returns:
        全部满足记 ``PASS``；检索层结果（无答案）记 ``NOT_APPLICABLE``。
    """
    del case  # 统一签名，便于 pending_metrics 批量分派
    if result.layer is EvalLayer.RETRIEVAL or not result.answer_status:
        return _na(MetricName.CITATION_INTEGRITY, "检索层无答案，不判引用")

    body_indices = extract_citation_indices(result.answer, max_index=_NO_INDEX_LIMIT)
    listed_indices = [citation.index for citation in result.citations]
    problems: list[str] = []

    out_of_range = sorted(i for i in body_indices if i > len(result.evidence))
    if out_of_range:
        problems.append(
            f"正文引用了不存在的证据 {out_of_range}（本次仅 {len(result.evidence)} 条）"
        )
    if set(body_indices) != set(listed_indices):
        problems.append(
            f"正文下标 {sorted(body_indices)} 与引用列表 {sorted(listed_indices)} 不一致"
        )
    misplaced = _find_misplaced_citations(result)
    if misplaced:
        problems.append("引用条目与证据不符：" + "；".join(misplaced))
    if result.answer_status == AnswerStatus.ANSWERED.value and not result.citations:
        problems.append("状态为 answered 却没有任何引用")

    if problems:
        return _metric(MetricName.CITATION_INTEGRITY, MetricVerdict.FAIL, 0.0, " | ".join(problems))
    return _metric(
        MetricName.CITATION_INTEGRITY,
        MetricVerdict.PASS,
        1.0,
        f"引用 {len(listed_indices)} 条，编号与证据一一对应",
    )


def score_refusal(case: GoldenCase, result: CaseResult) -> MetricResult:
    """该拒答时是否真的拒答，且拒得干净。

    只对**期望拒答**的样例适用（期望状态属于 :data:`REFUSAL_STATUSES`）。
    判据是结构性的、不依赖任何文案字面量：

    1. 实际状态属于拒答状态；
    2. 答案非空（空白拒答无法向用户交代，兄弟节点 docstring 已明写不得如此）；
    3. 答案正文**不含任何引用标记**——"拒答"却带着 ``[1]``，说明未验证的草稿漏了出来；
    4. 若实际状态是 ``insufficient_evidence``，``retrieval_meta`` 必须为空或
       ``is_sufficient=False``（拒答与检索判定必须自洽）。
    """
    if case.expected_status.value not in REFUSAL_STATUSES:
        return _na(MetricName.REFUSAL_CORRECTNESS, "本题不期望拒答")

    actual = result.answer_status
    problems: list[str] = []
    if actual not in REFUSAL_STATUSES:
        problems.append(f"期望拒答，实际状态 {actual or '<空>'}")
    if not result.answer.strip():
        problems.append("拒答文案为空")
    leaked = extract_citation_indices(result.answer, max_index=_NO_INDEX_LIMIT)
    if leaked:
        problems.append(f"拒答答案里仍带引用标记 {sorted(leaked)}")
    if actual == AnswerStatus.INSUFFICIENT_EVIDENCE.value:
        meta = result.retrieval_meta
        if meta is not None and meta.is_sufficient:
            problems.append("状态为 insufficient_evidence，检索元数据却判定 is_sufficient=True")

    if problems:
        return _metric(
            MetricName.REFUSAL_CORRECTNESS, MetricVerdict.FAIL, 0.0, " | ".join(problems)
        )
    return _metric(MetricName.REFUSAL_CORRECTNESS, MetricVerdict.PASS, 1.0, f"拒答状态 {actual}")


def score_tenant_isolation(
    case: GoldenCase, foreign_evidence: Iterable[EvidenceItem]
) -> MetricResult:
    """跨租户负向隔离：换一个"什么都不拥有"的租户检索同一问题，必须一无所获（NO-19）。

    用**负向**判据而非"两个租户结果不同"，是因为负向判据不依赖语料的租户分布：
    它断言的是"过滤真的下推到了数据源"，而不是"两份结果恰好不一样"。

    Args:
        case: 黄金集样例；仅 ``security`` 类适用（该探针由 runner 只对这些样例执行）。
        foreign_evidence: 陌生租户检索到的证据。

    Returns:
        ``NOT_APPLICABLE``（非安全类）；否则空证据记 ``PASS``。
    """
    if case.category is not CaseCategory.SECURITY:
        return _na(MetricName.TENANT_ISOLATION, "非安全类样例，未做跨租户探针")
    leaked = list(foreign_evidence)
    if leaked:
        names = sorted({item.document_name or item.chunk_id for item in leaked})
        return _metric(
            MetricName.TENANT_ISOLATION,
            MetricVerdict.FAIL,
            0.0,
            f"陌生租户检索到 {len(leaked)} 条证据（{'、'.join(names[:5])}）——租户过滤未生效",
        )
    return _metric(MetricName.TENANT_ISOLATION, MetricVerdict.PASS, 1.0, "陌生租户零命中")


def score_prompt_hygiene(rendered_prompt: str, *, had_evidence: bool) -> MetricResult:
    """提示词卫生：渲染后的证据块里不得残留原始栅栏标记（CLAUDE.md 10.3）。

    这是**接线断言**，不是 ``sanitize`` 的功能测试（那由 ``tests/unit/test_sanitize.py``
    覆盖）。它回答的是"生产渲染路径是否真的调用了中和函数"：把
    ``neutralize_untrusted`` 从 ``build_answer_messages`` 里删掉，本指标立刻变红，
    而 ``sanitize`` 自己的单测依然全绿。

    判据是**出现次数**而非"是否存在"：合法证据块本来就用这两个标记包裹，
    各出现一次是正常的。代码块里出现第二次，只可能是证据正文没被中和。

    Args:
        rendered_prompt: 经**生产渲染路径**（``build_answer_messages``）产出的文本。
        had_evidence: 本次是否有证据；无证据时无渲染内容，判不适用。
    """
    if not had_evidence:
        return _na(MetricName.PROMPT_HYGIENE, "本题无证据，无渲染内容可查")

    residues = [marker for marker in _fence_tokens() if rendered_prompt.count(marker) > 1]
    if residues:
        return _metric(
            MetricName.PROMPT_HYGIENE,
            MetricVerdict.FAIL,
            0.0,
            f"渲染后的证据块残留栅栏标记 {residues}——中和未接线或被绕过",
        )
    return _metric(MetricName.PROMPT_HYGIENE, MetricVerdict.PASS, 1.0, "无残留栅栏标记")


def score_case(
    case: GoldenCase,
    result: CaseResult,
    *,
    foreign_evidence: Iterable[EvidenceItem] = (),
    rendered_prompt: str | None = None,
) -> CaseResult:
    """对一条样例跑完全部适用指标，返回**附加了 metrics** 的新结果。

    以"骨架是否具备"决定哪些指标进入：检索层只判命中，图/裁判层才判状态、引用与拒答。
    这样同一条样例在不同 ``--layer`` 下的指标集合是可预期的，聚合口径才稳定。

    Args:
        case: 黄金集样例。
        result: 该样例的执行结果。
        foreign_evidence: 跨租户探针证据（仅安全类样例由 runner 提供）。
        rendered_prompt: 生产渲染路径的提示词文本；``None`` 表示未渲染（跳过该指标）。

    Returns:
        新的 ``CaseResult``（原对象不被修改——`frozen=True` 的模型本就不能原地改）。
    """
    metrics: list[MetricResult] = [score_retrieval_hit(case, result)]
    if result.layer is not EvalLayer.RETRIEVAL:
        metrics.append(score_status(case, result))
        metrics.append(score_citations(case, result))
        metrics.append(score_refusal(case, result))
    if case.category is CaseCategory.SECURITY:
        metrics.append(score_tenant_isolation(case, foreign_evidence))
    if rendered_prompt is not None:
        metrics.append(score_prompt_hygiene(rendered_prompt, had_evidence=bool(result.evidence)))
    metrics.extend(
        metric for metric in result.metrics if metric.name not in {m.name for m in metrics}
    )
    return result.model_copy(update={"metrics": metrics})


def _find_misplaced_citations(result: CaseResult) -> list[str]:
    """找出与所指向证据不符的引用条目（编号错位 / chunk_id 不符 / 文档名不符）。"""
    problems: list[str] = []
    for citation in result.citations:
        position = citation.index - 1
        if not 0 <= position < len(result.evidence):
            problems.append(f"引用 [{citation.index}] 越界")
            continue
        target = result.evidence[position]
        if citation.chunk_id != target.chunk_id:
            problems.append(
                f"引用 [{citation.index}] 指向 {citation.chunk_id}，证据实为 {target.chunk_id}"
            )
        elif _normalize_name(citation.document_name) != _normalize_name(target.document_name):
            problems.append(
                f"引用 [{citation.index}] 文档名 {citation.document_name!r}"
                f" 与证据 {target.document_name!r} 不符"
            )
    return problems


def _fence_tokens() -> tuple[str, str]:
    """生产模板里实际使用的两种栅栏标记。"""
    return (EVIDENCE_OPEN_MARKER, EVIDENCE_CLOSE_MARKER)


def _normalize_name(value: str) -> str:
    """文档名归一：取 basename、去首尾空白、大小写折叠。

    只做这三样——再做模糊匹配（去标点、拆词）就会把不同文档判成同一份，
    那比漏判危险得多。
    """
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    return name.strip().casefold()


def _metric(name: MetricName, verdict: MetricVerdict, score: float, detail: str) -> MetricResult:
    """构造带分数的指标结果。"""
    return MetricResult(name=name, verdict=verdict, score=score, detail=detail)


def _na(name: MetricName, detail: str) -> MetricResult:
    """构造"不适用"的指标结果（不带分数，由 ``MetricResult`` 校验器强制）。"""
    return MetricResult(name=name, verdict=MetricVerdict.NOT_APPLICABLE, score=None, detail=detail)


__all__: list[str] = [
    "REFUSAL_STATUSES",
    "score_case",
    "score_citations",
    "score_prompt_hygiene",
    "score_refusal",
    "score_retrieval_hit",
    "score_status",
    "score_tenant_isolation",
]
