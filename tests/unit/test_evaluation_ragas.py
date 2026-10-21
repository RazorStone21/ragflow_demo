"""RAGAS 判分层（适用性 / 四态 / 缓存 / 数值校验）的离线单元测试（ADR-0006）。

这里**不装 RAGAS**也能跑：``RagasJudge`` 只依赖 ``RagasBackend`` 协议，测试用
fake 后端把每条适用性规则与失败路径跑满。真正需要 RAGAS 源码的只有
``_strict_correctness_class``（用一个 fake 基类验证空断言防护）与遥测强制关闭
（子进程里用假模块验证"导入前覆盖环境变量"这个顺序）。

失败语义是重点：测不出来记 ``ERROR``、**不带分数**，本样例判不通过；记成
``0.0`` 会让测量事故伪装成质量回归（ADR-0003 的教训，ADR-0006 沿用）。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_platform.agents.gov_policy_qa.schemas import Citation
from agent_platform.agents.gov_policy_qa.state import AnswerStatus
from agent_platform.evaluation.judge import JudgeCache
from agent_platform.evaluation.ragas_backend import (
    TELEMETRY_ENV,
    RagasMeasurementError,
    RagasRequest,
    RagasScore,
    _checked_value,
    _strict_correctness_class,
    canonical_request,
)
from agent_platform.evaluation.ragas_judge import (
    RagasJudge,
    _exception_diagnostics,
    _faithfulness_contexts,
)
from agent_platform.evaluation.schemas import (
    CaseCategory,
    CaseResult,
    EvalLayer,
    GoldenCase,
    MetricName,
    MetricResult,
    MetricVerdict,
    ReferenceSource,
)
from agent_platform.knowledge.schemas import EvidenceItem

_DOC = "0030.贵州电网销售电价表.xlsx"
_QUESTION = "贵州燃煤发电基准价是多少？"
_ANSWER = "贵州燃煤发电基准价为 0.3515 元/千瓦时 [1]。"
_REFERENCE = "贵州燃煤发电基准价为 0.3515 元/千瓦时。"

_RAGAS: tuple[MetricName, ...] = (
    MetricName.FAITHFULNESS,
    MetricName.ANSWER_RELEVANCY,
    MetricName.ANSWER_CORRECTNESS,
)


# ---------------------------------------------------------------------------
# fake 后端：记录调用、可注入异常、身份可切换
# ---------------------------------------------------------------------------


@dataclass
class _FakeBackend:
    """满足 ``RagasBackend`` 协议的离线替身。"""

    results: dict[MetricName, float | Exception] = field(
        default_factory=lambda: {
            MetricName.FAITHFULNESS: 0.9,
            MetricName.ANSWER_RELEVANCY: 0.8,
            MetricName.ANSWER_CORRECTNESS: 0.7,
        }
    )
    identity: str = "fake|judge@fake-model|embedding|default"
    calls: list[tuple[MetricName, RagasRequest]] = field(default_factory=list)
    closed: bool = False

    @property
    def library_version(self) -> str:
        return "0.4.3-fake"

    @property
    def embedding_model(self) -> str:
        return "embedding"

    @property
    def telemetry_disabled(self) -> bool:
        return True

    @property
    def metric_names(self) -> tuple[str, ...]:
        return tuple(metric.value for metric in self.results)

    def cache_identity(self, metric: MetricName) -> str:
        return f"{self.identity}|{metric.value}"

    async def score(self, metric: MetricName, request: RagasRequest) -> RagasScore:
        self.calls.append((metric, request))
        result = self.results[metric]
        if isinstance(result, Exception):
            raise result
        return RagasScore(value=result)

    async def aclose(self) -> None:
        self.closed = True


def _case(*, answerable: bool = True, with_reference: bool = True) -> GoldenCase:
    if not answerable:
        return GoldenCase(
            id="c1",
            category=CaseCategory.REFUSAL,
            capability="拒答",
            question="火星上的电价是多少？",
            expected_status=AnswerStatus.INSUFFICIENT_EVIDENCE,
        )
    return GoldenCase(
        id="c1",
        category=CaseCategory.TYPICAL,
        capability="电价",
        question=_QUESTION,
        expected_status=AnswerStatus.ANSWERED,
        reference_answer=_REFERENCE if with_reference else "",
        reference_answer_source=(
            ReferenceSource(file=_DOC, page=2, quote=_REFERENCE) if with_reference else None
        ),
    )


def _citation(index: int = 1, chunk_id: str = "chunk-1") -> Citation:
    return Citation(
        index=index, chunk_id=chunk_id, document_name=_DOC, source_uri="", page_numbers=[2]
    )


def _result(
    *,
    answer: str = _ANSWER,
    status: str = AnswerStatus.ANSWERED.value,
    evidence: bool = True,
) -> CaseResult:
    return CaseResult(
        case_id="c1",
        category=CaseCategory.TYPICAL,
        question=_QUESTION,
        layer=EvalLayer.GRAPH,
        answer=answer,
        answer_status=status,
        citations=[_citation()] if answer else [],
        evidence=(
            [
                EvidenceItem(
                    chunk_id="chunk-1",
                    content="贵州燃煤发电基准价为 0.3515 元/千瓦时。",
                    score=0.9,
                    document_name=_DOC,
                    page_numbers=[2],
                )
            ]
            if evidence
            else []
        ),
    )


def _judge(tmp_path: Path, backend: _FakeBackend, *, cache_enabled: bool = True) -> RagasJudge:
    return RagasJudge(backend, cache_dir=tmp_path / "cache", cache_enabled=cache_enabled)


def _by_name(result: CaseResult) -> dict[MetricName, Any]:
    return {metric.name: metric for metric in result.metrics}


# ---------------------------------------------------------------------------
# 适用性矩阵
# ---------------------------------------------------------------------------
async def test_non_answerable_cases_skip_ragas_entirely(tmp_path: Path) -> None:
    """拒答/安全样例三项全 N/A：拿 Answer Relevancy 给合法拒答打分是错的尺子。"""
    backend = _FakeBackend()
    scored = await _judge(tmp_path, backend).score(
        _case(answerable=False), _result(answer="无法回答")
    )

    metrics = _by_name(scored)
    assert {metrics[name].verdict for name in _RAGAS} == {MetricVerdict.NOT_APPLICABLE}
    assert all(metrics[name].score is None for name in _RAGAS)
    assert backend.calls == [], "不适用就不该产生模型/嵌入调用"


async def test_expected_answered_but_refused_falls_back_into_correctness(tmp_path: Path) -> None:
    """期望 answered 而实际拒答：正确性直接 ``FAIL 0.0``，不调 RAGAS。

    状态兜底并入 Answer Correctness（``status_accuracy`` 已退役），拒答题本身
    则由 ``refusal_correctness`` 兜底——两处各判各的，不重复也不遗漏。
    """
    backend = _FakeBackend()
    result = _result(answer="证据不足，无法回答。", status=AnswerStatus.INSUFFICIENT_EVIDENCE.value)

    scored = await _judge(tmp_path, backend).score(_case(), result)

    metrics = _by_name(scored)
    correctness = metrics[MetricName.ANSWER_CORRECTNESS]
    assert correctness.verdict is MetricVerdict.FAIL
    assert correctness.score == 0.0
    assert AnswerStatus.INSUFFICIENT_EVIDENCE.value in correctness.detail
    assert metrics[MetricName.FAITHFULNESS].verdict is MetricVerdict.NOT_APPLICABLE
    assert metrics[MetricName.ANSWER_RELEVANCY].verdict is MetricVerdict.NOT_APPLICABLE
    assert backend.calls == []


async def test_expected_answered_but_blank_answer_is_named_as_blank(tmp_path: Path) -> None:
    """状态写着 answered、答案却是空串：同样兜底，但理由要说"答案为空"。

    两个分支的分数一样（都是 0），可读的 detail 决定人能不能一眼分清"拒答"
    与"产出了个空对象"——后者往往是图里的缺陷，不是模型判断。
    """
    scored = await _judge(tmp_path, _FakeBackend()).score(
        _case(), _result(answer="   ", status=AnswerStatus.ANSWERED.value)
    )

    correctness = _by_name(scored)[MetricName.ANSWER_CORRECTNESS]
    assert correctness.verdict is MetricVerdict.FAIL
    assert correctness.score == 0.0
    assert "答案为空" in correctness.detail


async def test_answer_without_evidence_skips_faithfulness(tmp_path: Path) -> None:
    """无证据可核时 faithfulness 记 N/A；relevancy / correctness 照常判。"""
    backend = _FakeBackend()
    scored = await _judge(tmp_path, backend).score(_case(), _result(evidence=False))

    metrics = _by_name(scored)
    assert metrics[MetricName.FAITHFULNESS].verdict is MetricVerdict.NOT_APPLICABLE
    assert metrics[MetricName.ANSWER_RELEVANCY].score == 0.8
    assert metrics[MetricName.ANSWER_CORRECTNESS].score == 0.7
    assert [metric for metric, _ in backend.calls] == [
        MetricName.ANSWER_RELEVANCY,
        MetricName.ANSWER_CORRECTNESS,
    ]


async def test_answer_without_reference_skips_correctness(tmp_path: Path) -> None:
    """没有参考答案就无法判正确性——不是"不通过"，是"本题不考这一项"。"""
    backend = _FakeBackend()
    scored = await _judge(tmp_path, backend).score(_case(with_reference=False), _result())

    metrics = _by_name(scored)
    assert metrics[MetricName.ANSWER_CORRECTNESS].verdict is MetricVerdict.NOT_APPLICABLE
    assert metrics[MetricName.ANSWER_CORRECTNESS].score is None
    assert [metric for metric, _ in backend.calls] == [
        MetricName.FAITHFULNESS,
        MetricName.ANSWER_RELEVANCY,
    ]


def test_faithfulness_contexts_mirror_the_generator_evidence_block() -> None:
    """上下文必须带编号、文档名与页码——只喂正文会让有据的指称被判无据（D14）。

    生成器看到的是 ``[n] 文档：…（第 … 页）``（``answer.human.v1.jinja``），
    答案里也照此指称来源；RAGAS 的 NLI 裁判缺了这些元数据，就会把
    "某文件规定了…"判成上下文未写。编号必须从 1 起且与引用编号一致。
    """
    evidence = [
        EvidenceItem(
            chunk_id="chunk-1",
            content="正文一",
            score=0.9,
            document_name=_DOC,
            page_numbers=[2, 3],
        ),
        EvidenceItem(chunk_id="chunk-2", content="正文二", score=0.8),
    ]

    assert _faithfulness_contexts(evidence) == (
        f"[1] 文档：{_DOC}（第 2、3 页）\n\n正文一",
        "[2] 文档：\n\n正文二",
    )


async def test_full_answer_sends_the_right_request_shape(tmp_path: Path) -> None:
    """三项各用各的输入：faithfulness 带证据、relevancy 不带、correctness 带参考答案。

    请求接错（例如把参考答案喂给 relevancy）不会报错，只会让分数悄悄失去意义。
    """
    backend = _FakeBackend()
    await _judge(tmp_path, backend).score(_case(), _result())

    requests = dict(backend.calls)
    assert requests[MetricName.FAITHFULNESS] == RagasRequest(
        question=_QUESTION,
        answer=_ANSWER,
        contexts=(f"[1] 文档：{_DOC}（第 2 页）\n\n贵州燃煤发电基准价为 0.3515 元/千瓦时。",),
    )
    assert requests[MetricName.ANSWER_RELEVANCY] == RagasRequest(question=_QUESTION, answer=_ANSWER)
    assert requests[MetricName.ANSWER_CORRECTNESS] == RagasRequest(
        question=_QUESTION, answer=_ANSWER, reference=_REFERENCE
    )


async def test_zero_score_is_a_failure_with_a_score(tmp_path: Path) -> None:
    """真实测出的 0 分是 ``FAIL`` **且带分数**——与测量失败的 ``ERROR`` 严格区分。"""
    backend = _FakeBackend(
        results={
            MetricName.FAITHFULNESS: 0.0,
            MetricName.ANSWER_RELEVANCY: 0.4,
            MetricName.ANSWER_CORRECTNESS: 0.3,
        }
    )
    scored = await _judge(tmp_path, backend).score(_case(), _result())

    faithfulness = _by_name(scored)[MetricName.FAITHFULNESS]
    assert faithfulness.verdict is MetricVerdict.FAIL
    assert faithfulness.score == 0.0


# ---------------------------------------------------------------------------
# 测量失败：ERROR 且不带分数；一项失败不拖累其他项
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "failure",
    [
        RagasMeasurementError("AnswerCorrectness 未从文本中抽出任何断言"),
        RuntimeError("网关超时"),
    ],
)
async def test_backend_failure_is_error_without_a_score(tmp_path: Path, failure: Exception) -> None:
    """RAGAS 抛错（含第三方异常）一律 ``ERROR``、无分数、不通过，且留痕原因。"""
    backend = _FakeBackend(
        results={
            MetricName.FAITHFULNESS: failure,
            MetricName.ANSWER_RELEVANCY: 0.8,
            MetricName.ANSWER_CORRECTNESS: 0.7,
        }
    )

    scored = await _judge(tmp_path, backend).score(_case(), _result())

    faithfulness = _by_name(scored)[MetricName.FAITHFULNESS]
    assert faithfulness.verdict is MetricVerdict.ERROR
    assert faithfulness.score is None
    assert "未测出结果" in faithfulness.detail
    assert not scored.passed, "测不出来照样算这条样例没通过"
    assert "[ragas:faithfulness]" in scored.judge_raw
    assert str(failure) in scored.judge_raw


async def test_one_failing_metric_does_not_block_the_others(tmp_path: Path) -> None:
    """每项独立执行：faithfulness 测量失败时，relevancy 与 correctness 照常出分。"""
    backend = _FakeBackend(
        results={
            MetricName.FAITHFULNESS: RagasMeasurementError("NaN"),
            MetricName.ANSWER_RELEVANCY: 0.8,
            MetricName.ANSWER_CORRECTNESS: 0.7,
        }
    )

    metrics = _by_name(await _judge(tmp_path, backend).score(_case(), _result()))

    assert metrics[MetricName.FAITHFULNESS].verdict is MetricVerdict.ERROR
    assert metrics[MetricName.ANSWER_RELEVANCY].score == 0.8
    assert metrics[MetricName.ANSWER_CORRECTNESS].score == 0.7


async def test_other_metrics_are_preserved_and_replaced_by_name(tmp_path: Path) -> None:
    """已有同名指标被替换，其他指标（如检索三项）原样保留。"""
    result = _result()
    existing = [
        *result.metrics,
        _metric(MetricName.RECALL_AT_K, 1.0),
        _metric(MetricName.FAITHFULNESS, 0.1),
    ]
    result = result.model_copy(update={"metrics": existing})

    scored = await _judge(tmp_path, _FakeBackend()).score(_case(), result)

    metrics = _by_name(scored)
    assert metrics[MetricName.RECALL_AT_K].score == 1.0
    assert metrics[MetricName.FAITHFULNESS].score == 0.9, "同名旧值必须被本次结果替换"
    assert len([m for m in scored.metrics if m.name is MetricName.FAITHFULNESS]) == 1


def _metric(name: MetricName, value: float) -> MetricResult:
    return MetricResult(name=name, verdict=MetricVerdict.PASS, score=value)


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
async def test_a_cached_score_costs_no_backend_call(tmp_path: Path) -> None:
    backend = _FakeBackend()
    first = await _judge(tmp_path, backend).score(_case(), _result())
    calls_after_first = len(backend.calls)

    second = await _judge(tmp_path, backend).score(_case(), _result())

    assert len(backend.calls) == calls_after_first
    assert {m.name: m.score for m in first.metrics} == {m.name: m.score for m in second.metrics}
    faithfulness = _by_name(second)[MetricName.FAITHFULNESS]
    assert "缓存命中" in faithfulness.detail
    assert '"cache_hit": true' in second.judge_raw


async def test_cache_identity_change_invalidates_old_scores(tmp_path: Path) -> None:
    """换裁判/嵌入/库版本后必须重新判：旧分数属于另一件测量工具。"""
    backend = _FakeBackend()
    await _judge(tmp_path, backend).score(_case(), _result())
    backend.calls.clear()

    backend.identity = "fake|judge@other-model|embedding|default"
    await _judge(tmp_path, backend).score(_case(), _result())

    assert len(backend.calls) == 3, "身份变了就不得复用旧缓存"


async def test_corrupt_cache_falls_back_to_a_real_call(tmp_path: Path) -> None:
    """缓存条目损坏时真调一次，而不是把样例记成失败。"""
    backend = _FakeBackend()
    await _judge(tmp_path, backend).score(_case(), _result())
    for path in (tmp_path / "cache").rglob("*.json"):
        path.write_text("这不是 JSON", encoding="utf-8")
    backend.calls.clear()

    scored = await _judge(tmp_path, backend).score(_case(), _result())

    assert len(backend.calls) == 3
    assert _by_name(scored)[MetricName.FAITHFULNESS].score == 0.9


async def test_cache_disabled_bypasses_and_does_not_write(tmp_path: Path) -> None:
    backend = _FakeBackend()
    judge = _judge(tmp_path, backend, cache_enabled=False)

    await judge.score(_case(), _result())
    await judge.score(_case(), _result())

    assert len(backend.calls) == 6, "缓存关闭后第二次必须重新判"
    assert not (tmp_path / "cache").exists()


async def test_cache_key_is_scoped_per_metric(tmp_path: Path) -> None:
    """三项指标的缓存键互不相同：同名请求不得让 relevancy 拿到 faithfulness 的分。"""
    request = RagasRequest(question=_QUESTION, answer=_ANSWER)
    keys = {
        metric: JudgeCache.key(
            prompt_name=f"ragas:{metric.value}",
            model_key="same-identity",
            prompt=canonical_request(request),
        )
        for metric in _RAGAS
    }

    assert len(set(keys.values())) == len(_RAGAS)


# ---------------------------------------------------------------------------
# 纯函数：数值校验与请求规范化
# ---------------------------------------------------------------------------
class _Payload:
    def __init__(self, value: Any) -> None:
        self.value = value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.5, 0.5),
        (1.0 + 1e-9, 1.0),
        (-1e-9, 0.0),
        (0.0, 0.0),
        (1.0, 1.0),
    ],
)
def test_in_range_values_are_accepted_and_clamped(raw: float, expected: float) -> None:
    assert _checked_value(MetricName.FAITHFULNESS, _Payload(raw)) == expected


@pytest.mark.parametrize(
    "raw",
    [float("nan"), float("inf"), float("-inf"), 1.5, -0.01, None, "不是数字", _Payload("x")],
)
def test_non_finite_or_out_of_range_values_raise(raw: Any) -> None:
    with pytest.raises(RagasMeasurementError):
        _checked_value(MetricName.FAITHFULNESS, _Payload(raw))


def test_canonical_request_is_stable_and_sensitive_to_input() -> None:
    request = RagasRequest(
        question="问", answer="答", contexts=("证据一", "证据二"), reference="参"
    )
    same = RagasRequest(question="问", answer="答", contexts=("证据一", "证据二"), reference="参")
    other = RagasRequest(question="问", answer="答", contexts=("证据一",), reference="参")

    assert canonical_request(request) == canonical_request(same)
    assert canonical_request(request) != canonical_request(other)
    assert json.loads(canonical_request(request))["contexts"] == ["证据一", "证据二"]


def test_strict_correctness_rejects_empty_statements() -> None:
    """空断言必须抛错：RAGAS 0.4.3 会把它当"事实性满分"，这是缺陷不是宽容。"""

    class _Base:
        def __init__(self, statements: list[str]) -> None:
            self._statements = statements

        async def _generate_statements(self, question: str, text: str) -> list[str]:
            return list(self._statements)

    strict_cls = _strict_correctness_class(_Base)
    assert strict_cls(["有断言"])._statements == ["有断言"]

    with pytest.raises(RagasMeasurementError):
        asyncio.run(strict_cls([])._generate_statements("问", "答"))


# ---------------------------------------------------------------------------
# 遥测：导入 RAGAS 之前必须强制关死（CLAUDE.md 10.1 / NO-15）
# ---------------------------------------------------------------------------
_TELEMETRY_SCRIPT = """
import os, sys, types
import agent_platform.evaluation.ragas_backend as rb

ragas = types.ModuleType("ragas")
metrics = types.ModuleType("ragas.metrics")
collections = types.ModuleType("ragas.metrics.collections")
collections.Faithfulness = object
collections.AnswerRelevancy = object
collections.AnswerCorrectness = object
sys.modules.update(
    {"ragas": ragas, "ragas.metrics": metrics, "ragas.metrics.collections": collections}
)

rb._import_ragas()
print(os.environ[rb.TELEMETRY_ENV])
"""


@pytest.mark.parametrize("initial", [None, "false", "0"])
def test_telemetry_is_forced_off_before_import(initial: str | None) -> None:
    """环境里写 ``false`` 也会被覆盖成 ``true``，且覆盖发生在 import 之前。

    用子进程而不是直接调 ``_force_telemetry_off``：要验证的是**顺序**——
    一旦有人把环境赋值挪到 ``import ragas`` 之后，同进程测试仍会通过，而
    真实运行会先启动外发线程。子进程里用假模块避免装 RAGAS。
    """
    root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.pop(TELEMETRY_ENV, None)
    if initial is not None:
        env[TELEMETRY_ENV] = initial
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root / "src"), env.get("PYTHONPATH")]))

    completed = subprocess.run(
        [sys.executable, "-c", _TELEMETRY_SCRIPT],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "true"


def _incomplete_exception(*, reasoning_tokens: int | None) -> Exception:
    """构造一个带 ``last_completion`` 的截断异常（形状对齐 instructor）。"""
    details = (
        SimpleNamespace(reasoning_tokens=reasoning_tokens) if reasoning_tokens is not None else None
    )
    usage = SimpleNamespace(
        prompt_tokens=2277,
        completion_tokens=4096,
        completion_tokens_details=details,
    )
    message = SimpleNamespace(content="x" * 120)
    choice = SimpleNamespace(finish_reason="length", message=message)
    completion = SimpleNamespace(usage=usage, choices=[choice])

    class _IncompleteError(Exception):
        def __init__(self) -> None:
            super().__init__("truncated")
            self.last_completion = completion

    return _IncompleteError()


def test_exception_diagnostics_records_counts_but_not_content() -> None:
    """截断诊断只带计数与结束原因，正文不得进日志（CLAUDE.md 3.7）。"""
    diagnostics = _exception_diagnostics(_incomplete_exception(reasoning_tokens=0))
    assert diagnostics == {
        "prompt_tokens": 2277,
        "completion_tokens": 4096,
        "reasoning_tokens": 0,
        "finish_reason": "length",
        "content_chars": 120,
    }


def test_exception_diagnostics_empty_for_unrelated_errors() -> None:
    """无 ``last_completion`` 的异常不产出诊断字段，避免伪造计数。"""
    assert _exception_diagnostics(ValueError("boom")) == {}
