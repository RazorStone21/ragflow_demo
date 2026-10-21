"""门禁判定与基线对比的单元测试（CLAUDE.md 9.2 / 9.3）。

**这个文件守的是"过没过"这句话本身**。判分器算得再准，若判定逻辑把"没采到"读成
"通过"、把"黄金集换了"读成"分数涨了"，整套评估就只是在生产一份好看的表格。
所以这里不测"分数算得对不对"（那在 ``test_evaluation_scoring.py``），只测**判定
与对比的每一种出口**，包括全部拒绝分支。

离线是硬要求：整份文件不连任何服务、不读真实产物。评估最该被反复验证的部分，
不能反过来依赖"先跑二十分钟真实评估才知道改没改坏"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_platform.core.errors import ArtifactIncompatibleError, ConfigurationError
from agent_platform.evaluation.reporting import (
    aggregate,
    compare,
    decide_verdict,
    detect_drift,
    detect_judge_traceability_gap,
    evaluate_gate,
    load_thresholds,
    metric_samples,
    promote_baseline,
    read_artifact,
    render_case_detail,
    render_comparison,
    render_gate,
    write_artifact,
)
from agent_platform.evaluation.schemas import (
    SCHEMA_VERSION,
    STRIPPED_EVIDENCE_CONTENT,
    CaseCategory,
    CaseResult,
    CorpusSnapshot,
    EvalArtifact,
    EvalLayer,
    GoldenSnapshot,
    MetricName,
    MetricResult,
    MetricVerdict,
    ModelSnapshot,
    RetrievalSnapshot,
    RunMeta,
    RunVerdict,
)

# ---------------------------------------------------------------------------
# 构造器：产物与阈值全部在内存里搭出来，不碰磁盘也不连服务
# ---------------------------------------------------------------------------

_THRESHOLDS_TOML = """\
schema_version = 2

[thresholds]
citation_integrity = 1.00
faithfulness = 0.80

[min_samples]
citation_integrity = 2
faithfulness = 1
"""


def _write_thresholds(tmp_path: Path, body: str) -> Path:
    """写一份带当前结构版本的阈值文件；``body`` 只放用例真正关心的段落。

    版本号由这里统一加：``load_thresholds`` 先校验版本、后校验内容，漏掉它时
    任何"未知指标名""值不合法"的用例都会先撞上版本错误，测的就不是它想测的。
    """
    path = tmp_path / "thresholds.toml"
    path.write_text(f"schema_version = {SCHEMA_VERSION}\n\n{body}", encoding="utf-8")
    return path


def _metric(name: MetricName, verdict: MetricVerdict, score: float | None) -> MetricResult:
    """造一条指标结果；``NOT_APPLICABLE`` 必须不带分数（契约会拒绝别的组合）。"""
    return MetricResult(name=name, verdict=verdict, score=score)


def _case(
    case_id: str,
    *,
    metrics: list[MetricResult] | None = None,
    error: str = "",
) -> CaseResult:
    """造一条样例结果，默认"两项指标全过"。"""
    return CaseResult(
        case_id=case_id,
        category=CaseCategory.TYPICAL,
        question="问题",
        layer=EvalLayer.GRAPH,
        error=error,
        metrics=metrics
        if metrics is not None
        else [
            _metric(MetricName.CITATION_INTEGRITY, MetricVerdict.PASS, 1.0),
            _metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 1.0),
        ],
    )


def _run_meta(*, run_id: str = "run-1", golden_sha: str = "g1") -> RunMeta:
    return RunMeta(
        run_id=run_id,
        started_at="2026-10-16T00:00:00Z",
        models=ModelSnapshot(chat="primary-chat", embedding="embedding", rerank="rerank"),
        retrieval=RetrievalSnapshot(
            top_k=8,
            candidates=64,
            similarity_threshold=0.2,
            rerank_score_threshold=0.3,
            vector_weight=0.7,
            dim=4096,
        ),
        corpus=CorpusSnapshot(collection="gov_policy_chunks", chunk_count=195),
        golden=GoldenSnapshot(sha256=golden_sha, case_count=2, reviewed_count=0),
    )


def _artifact(
    cases: list[CaseResult], *, run_id: str = "run-1", golden_sha: str = "g1"
) -> EvalArtifact:
    return EvalArtifact(
        run=_run_meta(run_id=run_id, golden_sha=golden_sha),
        cases=cases,
        aggregate=aggregate(cases),
        verdict=decide_verdict(cases),
    )


@pytest.fixture
def thresholds(tmp_path: Path) -> Any:
    """从一份最小 TOML 装载阈值（顺带覆盖真实装载路径，而不是手搓 ``Thresholds``）。"""
    path = tmp_path / "thresholds.toml"
    path.write_text(_THRESHOLDS_TOML, encoding="utf-8")
    return load_thresholds(path)


# ---------------------------------------------------------------------------
# 聚合：不适用不进分母
# ---------------------------------------------------------------------------
def test_aggregate_excludes_not_applicable_from_the_denominator() -> None:
    """N/A 若被当成 0 分，一个"只对一半样例适用"的指标会显得很差，掩盖真问题。"""
    cases = [
        _case("a", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 1.0)]),
        _case(
            "b",
            metrics=[
                _metric(MetricName.FAITHFULNESS, MetricVerdict.NOT_APPLICABLE, None),
            ],
        ),
    ]
    assert aggregate(cases) == {"faithfulness": 1.0, "hallucination_rate": 0.0}
    assert metric_samples(cases) == {"faithfulness": 1}


def test_aggregate_rounds_to_four_places() -> None:
    """留痕要能逐位比对；多余的浮点精度只是噪声。"""
    cases = [
        _case("a", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 1.0)]),
        _case("b", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.FAIL, 0.0)]),
        _case("c", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.FAIL, 0.0)]),
    ]
    assert aggregate(cases) == {"faithfulness": 0.3333, "hallucination_rate": 0.6667}


def test_aggregate_derives_hallucination_rate_from_faithfulness() -> None:
    """幻觉率不是第七个判分器：它是 ``1 − faithfulness`` 的派生量（ADR-0006）。

    只监控、不设门禁，因此它出现在聚合分里，但**不进** ``metric_samples``——
    样本数属于 faithfulness，给派生量再记一个"适用条数"会让分母出现两份口径。
    """
    cases = [
        _case("a", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 0.8)]),
        _case("b", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 1.0)]),
    ]

    scores = aggregate(cases)
    assert scores["hallucination_rate"] == 0.1
    assert "hallucination_rate" not in metric_samples(cases)


def test_aggregate_omits_hallucination_rate_without_faithfulness_samples() -> None:
    """没有 faithfulness 样本时省略而不是记 0：0 幻觉率读起来是"完美"。

    事实是"这一轮没有可核的证据"，与 NO-12 的"没证据不得给结论"同一条道理。
    """
    cases = [
        _case(
            "a",
            metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.NOT_APPLICABLE, None)],
        ),
        _case("b", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.ERROR, None)]),
    ]

    assert "hallucination_rate" not in aggregate(cases)


def test_aggregate_ignores_cases_that_failed_to_execute() -> None:
    """执行失败的样例没有指标，天然不进分母——不能按 0 分计入。"""
    cases = [_case("a"), _case("b", metrics=[], error="Milvus 不可达")]
    assert aggregate(cases)["citation_integrity"] == 1.0
    assert metric_samples(cases)["citation_integrity"] == 1


# ---------------------------------------------------------------------------
# 门禁：三种"不达标"各有各的来路
# ---------------------------------------------------------------------------
def test_gate_passes_when_all_metrics_clear(thresholds: Any) -> None:
    gate = evaluate_gate([_case("a"), _case("b")], thresholds)
    assert gate.ok
    assert gate.breaches == []
    assert gate.insufficient_samples == []


def test_gate_flags_a_metric_below_its_floor(thresholds: Any) -> None:
    """跌破阈值：分数够低就报。"""
    cases = [
        _case("a"),
        _case("b", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.FAIL, 0.5)]),
    ]
    gate = evaluate_gate(cases, thresholds)
    assert [breach.metric for breach in gate.breaches] == [MetricName.FAITHFULNESS]
    assert not gate.ok


def test_gate_treats_an_absent_metric_as_a_breach(thresholds: Any) -> None:
    """**缺席即跌破**：阈值里列了而本次没有，说明这类能力这一轮没有任何证据。

    这是最容易骗过自己的情形——只跑了检索层时，报告里压根不会出现
    ``citation_integrity`` 那一行，若不判跌破，一份只测了检索的产物会"全绿"。
    """
    gate = evaluate_gate(
        [_case("a", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 1.0)])],
        thresholds,
    )
    assert [breach.metric for breach in gate.breaches] == [MetricName.CITATION_INTEGRITY]
    assert not gate.ok


def test_insufficient_samples_is_not_a_pass(thresholds: Any) -> None:
    """**没采够就是没证据**：分数满分但样本量低于下限，仍不得算达标。

    这条曾经写错：``ok`` 只看 ``breaches``，于是窄跑恰好框进一条拒答样例时，
    拒答正确性会以"1.0 满分／1 条样本"的姿态报"达标"。``min_samples`` 的存在意义
    就是拦住它，因此必须同时体现在判定与渲染上。
    """
    gate = evaluate_gate([_case("a")], thresholds)  # citation_integrity 下限 2 条
    assert [item.metric for item in gate.insufficient_samples] == [MetricName.CITATION_INTEGRITY]
    assert not gate.ok, "样本量不足却判达标＝假绿"
    assert "样本量不足" in render_gate(gate)


def test_render_gate_marks_unsampled_metric_with_a_dash_not_zero(tmp_path: Path) -> None:
    """没测出来的指标要显示 ``—``：渲染成 0.0000 会被读成"考了零分"。"""
    path = _write_thresholds(
        tmp_path, "[thresholds]\nfaithfulness = 0.80\ncitation_integrity = 1.00\n"
    )
    gate = evaluate_gate(
        [_case("a", metrics=[_metric(MetricName.CITATION_INTEGRITY, MetricVerdict.PASS, 1.0)])],
        load_thresholds(path),
    )
    rendered = render_gate(gate)
    assert "—" in rendered
    assert "0.0000" not in rendered, "没采到的指标不得渲染成 0 分"


def test_measurement_failure_is_not_scored_as_a_zero(thresholds: Any) -> None:
    """**测不出来 ≠ 考了零分**：``ERROR`` 不得进入聚合。

    这条守的是一个真实发生过的失真。裁判输出两次都解析不出结构时，代码曾记
    ``FAIL`` + ``score=0.0``；首次真实基线对比里，一条样例因此把 faithfulness
    从 1.0 拉到聚合 0.9392 → 0.8770，报告读作"跌破"，该样例还进了"新失败样例"——
    **一次测量故障伪装成了一次质量回归**。现在它不带分数、不进分母，
    样例仍判不通过（可见性不减），但聚合分不再被凭空压低。
    """
    clean = [
        _case("a", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 1.0)]),
        _case("b", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 1.0)]),
    ]
    broken = [
        _case("a", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 1.0)]),
        _case("b", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.ERROR, None)]),
    ]
    assert aggregate(clean)["faithfulness"] == 1.0
    assert aggregate(broken)["faithfulness"] == 1.0, "未采集的样例不得被算成 0 分"
    assert metric_samples(clean)["faithfulness"] == 2
    assert metric_samples(broken)["faithfulness"] == 1, "分母要跟着变，否则分数与样本数对不上"


def test_measurement_failure_still_fails_the_case_and_the_run(thresholds: Any) -> None:
    """可见性一分不减：没测出来照样判不通过，整轮 FAIL。"""
    case = _case("b", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.ERROR, None)])
    assert not case.passed, "有一项该测没测出来，这条样例就不是通过"
    assert decide_verdict([_case("a"), case]) is RunVerdict.FAIL


def test_gate_is_not_ok_when_a_metric_was_not_measured(thresholds: Any) -> None:
    """没测出来同样不达标——``ok`` 少算这一项，报告就会在缺一块输入时报"达标"。"""
    gate = evaluate_gate(
        [
            _case("a"),
            _case(
                "b",
                metrics=[
                    _metric(MetricName.CITATION_INTEGRITY, MetricVerdict.PASS, 1.0),
                    _metric(MetricName.FAITHFULNESS, MetricVerdict.ERROR, None),
                ],
            ),
        ],
        thresholds,
    )
    assert [item.metric for item in gate.measurement_failures] == [MetricName.FAITHFULNESS]
    assert gate.measurement_failures[0].errored == 1
    assert not gate.ok


def test_a_metric_that_errored_everywhere_is_not_reported_as_a_zero(tmp_path: Path) -> None:
    """全部样例都测量失败：报"未采集"，**不是**报"跌破 0.0000"。

    两者都让 ``ok`` 为假，但读者去向完全不同：写 0.0000 会让人去查模型为什么不
    忠实，实际该查的是裁判为什么解析不出结构。而"压根没跑这项"（例如只跑了检索层）
    仍走原口径判跌破——本条**不得**把那一条也放走。
    """
    path = _write_thresholds(tmp_path, "[thresholds]\nfaithfulness = 0.80\n")
    only_errors = evaluate_gate(
        [_case("a", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.ERROR, None)])],
        load_thresholds(path),
    )
    assert only_errors.breaches == [], "未采集不是跌破"
    assert [item.metric for item in only_errors.measurement_failures] == [MetricName.FAITHFULNESS]
    assert "未采集到结果" in render_gate(only_errors)

    never_ran = evaluate_gate([_case("a", metrics=[])], load_thresholds(path))
    assert [breach.metric for breach in never_ran.breaches] == [MetricName.FAITHFULNESS]
    assert never_ran.measurement_failures == []


def test_render_gate_keeps_monitor_only_metrics_out_of_the_verdict_table(
    thresholds: Any,
) -> None:
    """幻觉率要可见，但**不得**出现在带"达标/跌破"的判定表里。

    它没有阈值，混进门禁表会让读者以为它被考核了；单独一段"监控指标（不设门禁）"
    既保住了可见性，又把语义说清楚。
    """
    metrics = [
        _metric(MetricName.CITATION_INTEGRITY, MetricVerdict.PASS, 1.0),
        _metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 0.9),
    ]
    rendered = render_gate(
        evaluate_gate([_case("a", metrics=metrics), _case("b", metrics=metrics)], thresholds)
    )

    assert "监控指标（不设门禁）：" in rendered
    assert "hallucination_rate: 0.1000" in rendered
    verdict_rows = rendered.split("监控指标（不设门禁）：")[0]
    assert "hallucination_rate" not in verdict_rows


def test_comparison_reports_an_unmeasured_metric_without_a_phantom_drop(thresholds: Any) -> None:
    """对比里的"未测全"：分数照常展示，但判定栏不得写成"达标"。"""
    baseline = _artifact(
        [
            _case("a", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 1.0)]),
            _case("b", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 1.0)]),
        ]
    )
    current = _artifact(
        [
            _case("a", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 1.0)]),
            _case("b", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.ERROR, None)]),
        ]
    )
    comparison = compare(baseline, current, thresholds)
    assert comparison.metrics["faithfulness"] == (1.0, 1.0), "不得出现 0.0 造成的假跌落"
    assert [item.metric for item in comparison.measurement_failures] == [MetricName.FAITHFULNESS]
    rendered = render_comparison(comparison)
    assert "未测全" in rendered
    assert "未采集到结果" in rendered


def test_render_comparison_marks_monitor_metrics_without_a_floor_or_samples(
    thresholds: Any,
) -> None:
    """监控指标在对比表里：判定栏写"监控"，阈值列与样本列都是占位符。

    ``hallucination_rate`` 没有"适用样例数"这回事（由 faithfulness 派生）；
    显示 0 会让人以为一条都没测，显示阈值则会让人以为它被考核。
    """
    metrics = [
        _metric(MetricName.CITATION_INTEGRITY, MetricVerdict.PASS, 1.0),
        _metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 0.9),
        _metric(MetricName.DOCUMENT_PRECISION_AT_K, MetricVerdict.PASS, 0.5),
    ]
    artifact = _artifact([_case("a", metrics=metrics), _case("b", metrics=metrics)])
    rendered = render_comparison(compare(artifact, artifact, thresholds))

    def row(name: str) -> str:
        return next(line for line in rendered.splitlines() if line.startswith(name))

    assert "监控" in row("hallucination_rate")
    assert "—" in row("hallucination_rate"), "派生的幻觉率没有样本数，不得渲染成 0"
    assert "监控" in row("document_precision_at_k")
    assert "—" in row("document_precision_at_k"), "监控指标不设阈值，阈值列必须是占位符"


# ---------------------------------------------------------------------------
# 整轮判定
# ---------------------------------------------------------------------------
def test_verdict_fails_on_any_execution_error() -> None:
    """有样例没跑成，整轮就不能叫 pass——它连结论都缺一块。"""
    assert decide_verdict([_case("a"), _case("b", metrics=[], error="超时")]) is RunVerdict.FAIL


def test_verdict_fails_on_a_single_failing_case() -> None:
    """一条样例崩了就是 fail：聚合分可能仍然很好看，但那正是它危险的地方。"""
    cases = [
        _case("a"),
        _case("b", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.FAIL, 0.1)]),
    ]
    assert decide_verdict(cases) is RunVerdict.FAIL


# ---------------------------------------------------------------------------
# 对比：本阶段最该被验证的能力——"人为做差的产物能否被检出"
# ---------------------------------------------------------------------------
def test_compare_detects_a_case_that_regressed(thresholds: Any) -> None:
    """**验收判据**：喂一份人为做差的产物，必须逐例检出回归。

    只比聚合分是不够的：本例里总分仍然过得去（两条样例的 faithfulness 均值从
    1.0 掉到 0.6，而 citation_integrity 依旧是满分），真正说明问题的是"**哪一条**
    样例从通过变成了失败"。
    """
    baseline = _artifact([_case("a"), _case("b")])
    worsened = _artifact(
        [
            _case("a"),
            _case("b", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.FAIL, 0.2)]),
        ],
        run_id="run-2",
    )
    comparison = compare(baseline, worsened, thresholds)

    assert comparison.newly_failing == ["b"]
    assert comparison.newly_passed == []
    assert comparison.metrics["faithfulness"] == (1.0, 0.6)
    # 聚合分看不出问题的那一半，恰恰是这条断言的价值：只看 0.6 会以为"还行"。
    assert comparison.metrics["citation_integrity"] == (1.0, 1.0)
    assert "b" in render_comparison(comparison)


def test_compare_detects_a_case_that_was_fixed(thresholds: Any) -> None:
    """反向也要认：修好了就该出现在新通过清单里，否则改进没有反馈。"""
    baseline = _artifact(
        [
            _case("a"),
            _case("b", metrics=[_metric(MetricName.FAITHFULNESS, MetricVerdict.FAIL, 0.2)]),
        ]
    )
    improved = _artifact([_case("a"), _case("b")], run_id="run-2")
    assert compare(baseline, improved, thresholds).newly_passed == ["b"]
    # 修好的样例也要出现在报告里：改进若不可见，就不会有人为了看见它去改进。
    assert "新通过样例 1 条" in render_comparison(compare(baseline, improved, thresholds))


def test_compare_reports_cases_missing_from_the_current_run(thresholds: Any) -> None:
    """黄金集缩水（基线里有、本次没跑）必须显式喊出来，否则"变好"可能只是少考了几题。"""
    comparison = compare(_artifact([_case("a"), _case("b")]), _artifact([_case("a")]), thresholds)
    assert comparison.missing_in_current == ["b"]
    assert "未执行" in render_comparison(comparison)


def test_compare_flags_a_changed_golden_set(thresholds: Any) -> None:
    """换了题的两组分数不可直接比较——这是对比功能最该主动说破的一件事。"""
    comparison = compare(
        _artifact([_case("a")], golden_sha="before"),
        _artifact([_case("a")], golden_sha="after", run_id="run-2"),
        thresholds,
    )
    assert comparison.golden_changed
    assert "黄金集已变化" in render_comparison(comparison)


def test_compare_surfaces_config_drift(thresholds: Any) -> None:
    """配置漂移要逐字段列出，否则读者会把两组不可比的数字直接相减。"""
    baseline = _artifact([_case("a")])
    changed = _artifact([_case("a")], run_id="run-2").model_copy(
        update={
            "run": _run_meta(run_id="run-2").model_copy(
                update={
                    "retrieval": _run_meta().retrieval.model_copy(update={"top_k": 20}),
                    "prompt_versions": {"answer.system": "v3"},
                }
            )
        }
    )
    drifts = {
        drift.field: (drift.baseline, drift.current)
        for drift in detect_drift(baseline.run, changed.run)
    }

    assert drifts["retrieval.top_k"] == ("8", "20")
    assert drifts["prompt.answer.system"] == ("", "v3")
    assert "配置漂移" in render_comparison(compare(baseline, changed, thresholds))


def test_compare_uses_the_same_threshold_implementation_as_the_gate(thresholds: Any) -> None:
    """对比里的免责/达标口径必须与无基线路径**同源**，否则两条路会悄悄分叉。"""
    artifact = _artifact([_case("a")])
    gate = evaluate_gate(artifact.cases, thresholds)
    comparison = compare(_artifact([_case("a")], run_id="run-0"), artifact, thresholds)
    assert comparison.breaches == gate.breaches
    assert comparison.insufficient_samples == gate.insufficient_samples


# ---------------------------------------------------------------------------
# 产物读写
# ---------------------------------------------------------------------------
def test_artifact_round_trips_through_disk(tmp_path: Path) -> None:
    """写出去再读回来必须等价——基线对比全靠它，序列化掉字段会静默改变判定。"""
    path = tmp_path / "runs" / "run-1.json"
    original = _artifact([_case("a"), _case("b")])
    write_artifact(original, path)
    assert read_artifact(path) == original


def test_read_artifact_rejects_a_missing_file(tmp_path: Path) -> None:
    """路径写错要给「不存在」，而不是抛裸 ``FileNotFoundError``（NO-06）。"""
    with pytest.raises(ArtifactIncompatibleError, match="不存在"):
        read_artifact(tmp_path / "nope.json")


def test_read_artifact_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{不是 JSON", encoding="utf-8")
    with pytest.raises(ArtifactIncompatibleError, match="JSON"):
        read_artifact(path)


def test_read_artifact_rejects_a_future_schema_version(tmp_path: Path) -> None:
    """跨版本不做推断：宁可让人重跑，也不拿结构对不上的数字做对比。"""
    path = tmp_path / "future.json"
    path.write_text(json.dumps({"schema_version": SCHEMA_VERSION + 1}), encoding="utf-8")
    with pytest.raises(ArtifactIncompatibleError, match="结构版本"):
        read_artifact(path)


def test_read_artifact_can_tolerate_a_version_mismatch_for_manual_viewing(
    tmp_path: Path,
) -> None:
    """``allow_schema_mismatch`` 只服务于人工查看，故此处仅要求它不抛版本错误。"""
    payload = _artifact([_case("a")]).model_dump(mode="json")
    payload["schema_version"] = SCHEMA_VERSION + 1
    path = tmp_path / "future.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArtifactIncompatibleError):
        read_artifact(path)
    assert read_artifact(path, allow_schema_mismatch=True).cases[0].case_id == "a"


def test_read_artifact_reports_the_offending_fields_on_a_shape_mismatch(
    tmp_path: Path,
) -> None:
    """结构不符时要指出**哪个字段**——只报"结构错误"等于让人去猜。"""
    payload = _artifact([_case("a")]).model_dump(mode="json")
    payload["cases"][0]["category"] = "not-a-category"
    path = tmp_path / "wrong.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArtifactIncompatibleError) as excinfo:
        read_artifact(path)
    assert "cases.0.category" in str(excinfo.value.detail)


# ---------------------------------------------------------------------------
# 基线提升
# ---------------------------------------------------------------------------
def test_promote_baseline_strips_corpus_text_but_keeps_the_audit_trail() -> None:
    """提升后的基线不含语料正文，但必须留着"命中了哪份文档的哪一页"。

    前者是 NO-17（入仓产物不含语料），后者是基线可复核的前提；两个方向都要守住，
    所以这里两边都断言。
    """
    from agent_platform.knowledge.schemas import EvidenceItem

    case = _case("a").model_copy(
        update={
            "evidence": [
                EvidenceItem(
                    chunk_id="c1",
                    content="受版权约束的语料正文",
                    score=0.9,
                    document_name="0002-x.pdf",
                    page_numbers=[3],
                )
            ],
            "judge_raw": "裁判原始输出，可能整段复述语料",
        }
    )
    promoted = promote_baseline(_artifact([case])).cases[0]

    assert "受版权约束的语料正文" not in promoted.evidence[0].content
    assert promoted.evidence[0].content == STRIPPED_EVIDENCE_CONTENT
    assert promoted.judge_raw == ""
    assert promoted.evidence[0].chunk_id == "c1"
    assert promoted.evidence[0].document_name == "0002-x.pdf"
    assert promoted.evidence[0].page_numbers == [3]


def test_promoted_baseline_records_where_the_full_trail_lives() -> None:
    """基线比 runs 目录里那份瘦，读到的人应当能立刻知道完整留痕在哪。"""
    promoted = promote_baseline(_artifact([_case("a")]))
    assert any("local_data/evals/runs/" in note for note in promoted.notes)


def test_promoted_baseline_is_still_a_valid_comparable_artifact(tmp_path: Path) -> None:
    """提升后的文件必须还能被 ``read_artifact`` 读回并直接用于对比。

    **样例必须带证据**——这正是本条测试早先失效的原因：它用的是没有证据的样例，
    剥离逻辑根本没被执行到，于是它一路绿灯，而真实基线（每条都带证据）写出去
    就再也读不回来。防线必须压在被保护的那条路径上。
    """
    from agent_platform.knowledge.schemas import EvidenceItem

    case = _case("a").model_copy(
        update={
            "evidence": [
                EvidenceItem(chunk_id="c1", content="语料正文", score=0.9, document_name="x.pdf")
            ]
        }
    )
    path = tmp_path / "baseline.json"
    write_artifact(promote_baseline(_artifact([case])), path)
    read_back = read_artifact(path)
    assert read_back.cases[0].metrics == _case("a").metrics
    assert read_back.cases[0].evidence[0].chunk_id == "c1"


def test_write_artifact_refuses_an_artifact_it_cannot_read_back(tmp_path: Path) -> None:
    """写路径必须拒绝产出"读不回来"的文件——它看起来与合格产物毫无区别。

    早先 ``write_artifact`` 只做 ``model_dump_json``（不校验），而
    ``compact_for_baseline`` 用不校验的 ``model_copy`` 把 ``content`` 置成空串，
    于是 ``--promote-baseline`` 打印"基线已提升"，写出的却是自己读不回的文件。
    """
    from agent_platform.knowledge.schemas import EvidenceItem

    broken = _case("a").model_copy(
        update={
            "evidence": [
                EvidenceItem(chunk_id="c1", content="正文", score=1.0).model_copy(
                    update={"content": ""}
                )
            ]
        }
    )
    with pytest.raises(ArtifactIncompatibleError, match="读回来会失败") as excinfo:
        write_artifact(_artifact([broken]), tmp_path / "broken.json")
    assert "cases.0.evidence.0.content" in str(excinfo.value.detail)
    assert not (tmp_path / "broken.json").exists(), "拒绝之后不得留下半份文件"


# ---------------------------------------------------------------------------
# 裁判可追溯性：有分数、却说不清谁判的
# ---------------------------------------------------------------------------
_JUDGE_SCORE = [
    _metric(MetricName.FAITHFULNESS, MetricVerdict.PASS, 0.9),
    _metric(MetricName.ANSWER_RELEVANCY, MetricVerdict.PASS, 0.9),
]


def _with_judge_model(
    artifact: EvalArtifact, name: str, *, ragas_version: str = ""
) -> EvalArtifact:
    return artifact.model_copy(
        update={
            "run": artifact.run.model_copy(
                update={
                    "models": _run_meta().models.model_copy(update={"judge": name}),
                    "judge": artifact.run.judge.model_copy(update={"ragas_version": ragas_version}),
                }
            )
        }
    )


def test_judge_traceability_gap_flags_scores_with_no_judge_model() -> None:
    """**这条守的是一个真实发生过的缺陷**：裁判分数落了盘，裁定者却是空的。

    2026-10-16 的首轮全量运行就是这样：``run.judge`` 有 backend/提示词版本，
    样例里也有三列裁判分数，而 ``run.models.judge`` 是空串——因为
    ``_apply_judge`` 当时只更新了前者，``Judge`` 存下的 ``logical_name`` 无人读取。
    产物看起来完全正常，只有想回答"这批分是哪个模型给的"时才发现无处可查。
    """
    gap = detect_judge_traceability_gap(_artifact([_case("a", metrics=list(_JUDGE_SCORE))]))
    assert gap
    assert "models.judge" in gap


def test_judge_traceability_gap_is_quiet_when_the_judge_is_recorded() -> None:
    artifact = _with_judge_model(
        _artifact([_case("a", metrics=list(_JUDGE_SCORE))]), "judge", ragas_version="0.4.3"
    )
    assert detect_judge_traceability_gap(artifact) == ""


def test_judge_traceability_gap_flags_ragas_scores_without_a_version() -> None:
    """RAGAS 的分数会随版本漂移：记了裁判模型、没记 RAGAS 版本，同样无法复现。

    ``models.judge`` 只能回答"哪个模型判的"，回答不了"用哪套判分库判的"——
    0.4.3 的 Answer Correctness 与后续版本的行为差异不在模型身上。
    """
    artifact = _with_judge_model(_artifact([_case("a", metrics=list(_JUDGE_SCORE))]), "judge")

    gap = detect_judge_traceability_gap(artifact)
    assert "ragas_version" in gap


def test_judge_traceability_gap_does_not_require_ragas_version_for_citation_scores() -> None:
    """引用准确率是自研裁判，只要求 ``models.judge``，不得被 RAGAS 的检查误伤。"""
    artifact = _with_judge_model(
        _artifact(
            [
                _case(
                    "a",
                    metrics=[_metric(MetricName.CITATION_ACCURACY, MetricVerdict.PASS, 1.0)],
                )
            ]
        ),
        "judge",
    )

    assert detect_judge_traceability_gap(artifact) == ""


def test_judge_traceability_gap_ignores_runs_without_judge_metrics() -> None:
    """没跑裁判的产物（只跑检索层/图层的）本来就不该有裁判模型，不能误报。

    指标刻意只取非裁判项：``_case()`` 的默认指标里就有 ``faithfulness``，
    直接拿默认值来测会得到"有裁判分数"的相反前提。
    """
    artifact = _artifact(
        [
            _case(
                "a",
                metrics=[
                    _metric(MetricName.CITATION_INTEGRITY, MetricVerdict.PASS, 1.0),
                    _metric(MetricName.RECALL_AT_K, MetricVerdict.PASS, 1.0),
                ],
            )
        ]
    )
    assert detect_judge_traceability_gap(artifact) == ""


def test_judge_traceability_gap_does_not_count_not_applicable_as_a_score() -> None:
    """``NOT_APPLICABLE`` 是"没考"而非"考了"，不构成"有分数却说不清谁判的"。"""
    artifact = _artifact(
        [
            _case(
                "a",
                metrics=[
                    _metric(MetricName.FAITHFULNESS, MetricVerdict.NOT_APPLICABLE, None),
                    _metric(MetricName.ANSWER_RELEVANCY, MetricVerdict.NOT_APPLICABLE, None),
                ],
            )
        ]
    )
    assert detect_judge_traceability_gap(artifact) == ""


def _baseline_paths() -> list[Path]:
    return sorted(Path("tests/evals/baselines").glob("*.json"))


def _baseline_schema_version(path: Path) -> object:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("schema_version") if isinstance(payload, dict) else None


def _baselines() -> list[Path]:
    """当前结构版本的基线。

    schema v1 的基线（2026-10-16 phase7）保留为历史：新代码读了会当场报
    "结构版本不兼容"，把它们混入对比测试只会让每条用例都变成版本错误。
    """
    return [path for path in _baseline_paths() if _baseline_schema_version(path) == SCHEMA_VERSION]


def test_committed_baselines_are_readable_and_traceable() -> None:
    """**入仓的每一份基线都必须读得回来，且说得清是谁判的分。**

    这是本次真实事故的回归防线：首份基线曾因 ``content=""`` 违背 ``min_length=1``
    而**自己读不回来**——写路径不校验、读路径才校验，于是 ``--promote-baseline``
    打印"已提升"，而 ``--baseline`` 对它恒报"基线不可读"。整条对比链在真实基线
    上从未跑通过，单测却全绿（它们用的是没有证据的样例）。

    所以这条测试不看构造出来的样本，只看**仓库里真正躺着的那几份文件**。
    """
    found = _baselines()
    if not found:
        pytest.skip("尚未生成 schema v2 基线；旧 v1 基线只作历史")
    for path in found:
        artifact = read_artifact(path)
        assert not detect_judge_traceability_gap(artifact), path
        assert artifact.cases, f"{path} 里没有样例"


def test_the_committed_baseline_keeps_the_audit_trail_without_the_corpus() -> None:
    """基线的两个方向都要在真实文件上成立：剥离了正文，但留住了命中文档与页码。"""
    found = _baselines()
    if not found:
        pytest.skip("尚未生成 schema v2 基线")
    for path in found:
        evidence = [item for case in read_artifact(path).cases for item in case.evidence]
        assert evidence, f"{path} 里没有任何证据条目"
        assert all(item.content == STRIPPED_EVIDENCE_CONTENT for item in evidence), path
        assert all(item.chunk_id for item in evidence), f"{path} 丢了 chunk_id"
        assert all("受版权" not in item.content for item in evidence)


def test_legacy_baselines_are_rejected_instead_of_silently_compared() -> None:
    """旧 v1 基线不得被新代码读来对比：指标集、黄金集与裁判都变了。

    它们留在仓库里只为人翻阅历史；``--baseline`` 指向它们时必须立刻报
    "结构版本不兼容"，而不是让两套不同语义的数字并排出现。
    """
    legacy = [
        path for path in _baseline_paths() if _baseline_schema_version(path) != SCHEMA_VERSION
    ]
    if not legacy:
        pytest.skip("仓库中没有旧版基线")
    for path in legacy:
        with pytest.raises(ArtifactIncompatibleError, match="结构版本"):
            read_artifact(path)


def test_judge_traceability_gap_recognizes_a_real_artifact() -> None:
    """守卫必须在**真实产物**上有效，而不只在自己造的样本上。

    做法：拿已入库的基线，把 ``models.judge`` 抹掉（这正是当初那个缺陷的形态），
    断言守卫认得出来。这样既证明了守卫认得真实数据结构，又不必让入仓的基线
    真的带着缺口躺着——那种"靠留一份坏数据来证明守卫有效"的做法，代价是每次
    对比都要先撞一次墙。
    """
    baselines = _baselines()
    if not baselines:
        pytest.skip("基线尚未入库")
    blanked = _with_judge_model(read_artifact(baselines[0]), "")
    assert detect_judge_traceability_gap(blanked)


# ---------------------------------------------------------------------------
# 样例明细渲染：评审人据以翻原文的那份输出
# ---------------------------------------------------------------------------
def test_render_case_detail_gives_the_reviewer_a_path_back_to_the_source() -> None:
    """明细必须带**文档名与页码**，否则评审协议在工具层面就执行不了。

    ``--show <id>`` 的用途是"拿实际检索到的证据去翻原文核对参考答案"。只给分数或
    chunk_id 核不动——chunk_id 是内部主键，人在 PDF 上找不到它。故这里逐个断言
    文档名、页码与引用下标都在输出里。
    """
    from agent_platform.agents.gov_policy_qa.schemas import Citation
    from agent_platform.knowledge.schemas import EvidenceItem

    result = _case("typical-01").model_copy(
        update={
            "answer": "现货市场建设的主要任务包括……[1]",
            "answer_status": "answered",
            "latency_ms": 1234,
            "node_trace": ["retrieve_evidence", "generate_answer"],
            "citations": [
                Citation(
                    index=1,
                    chunk_id="c1",
                    document_name="0002-关于加快建设全国统一电力市场体系的指导意见.pdf",
                    page_numbers=[4],
                )
            ],
            "evidence": [
                EvidenceItem(
                    chunk_id="c1",
                    content="正文",
                    score=0.7654,
                    document_name="0002-关于加快建设全国统一电力市场体系的指导意见.pdf",
                    page_numbers=[4],
                )
            ],
        }
    )
    rendered = render_case_detail(result, question="现货市场建设的主要任务是什么？")

    assert "现货市场建设的主要任务是什么？" in rendered
    assert "answered" in rendered
    assert "1234 ms" in rendered
    assert "retrieve_evidence → generate_answer" in rendered
    # 复核的抓手：文档名 + 页码 + 下标，二者都要有。
    assert rendered.count("0002-关于加快建设全国统一电力市场体系的指导意见.pdf") == 2
    assert "页码 [4]" in rendered
    assert "[1]" in rendered
    assert "分数 0.7654" in rendered


def test_render_case_detail_shows_media_type_and_image_ids() -> None:
    """多模态证据的明细要能让评审人认出"是哪张图"（ADR-0004）。

    给 image_id、页码与哈希前 12 位：完整哈希会挤爆一行，12 位足够回答
    "界面上这张图和证据里那张是不是同一张"。
    """
    import hashlib

    from agent_platform.agents.gov_policy_qa.schemas import Citation
    from agent_platform.knowledge.schemas import (
        EvidenceItem,
        ImageRef,
        MediaType,
        build_image_id,
    )

    sha = hashlib.sha256(b"figure").hexdigest()
    ref = ImageRef(image_id=build_image_id(sha), sha256=sha, mime="image/png", page=3)
    result = _case("typical-01").model_copy(
        update={
            "citations": [
                Citation(index=1, chunk_id="c1", document_name="扫描件.pdf", images=[ref])
            ],
            "evidence": [
                EvidenceItem(
                    chunk_id="c1",
                    content="正文",
                    score=0.8,
                    document_name="扫描件.pdf",
                    page_numbers=[3],
                    media_type=MediaType.IMAGE,
                    images=[ref],
                )
            ],
        }
    )

    rendered = render_case_detail(result)

    assert "媒介 image" in rendered
    assert ref.image_id in rendered
    assert f"sha256 {sha[:12]}…" in rendered


def test_render_case_detail_marks_absent_page_numbers_with_a_dash() -> None:
    """页码缺失要显示 ``—``：渲染成空列表会让人以为"第 0 页"。"""
    from agent_platform.knowledge.schemas import EvidenceItem

    result = _case("typical-01").model_copy(
        update={
            "evidence": [
                EvidenceItem(chunk_id="c1", content="正文", score=0.5, document_name="x.pdf")
            ]
        }
    )
    rendered = render_case_detail(result)
    assert "页码 —" in rendered
    assert "[]" not in rendered


def test_render_case_detail_falls_back_to_the_recorded_question() -> None:
    """不给 ``question`` 时用产物里记录的那句——评审人要看到**实际发出**的是什么。

    黄金集里的问题和真正发出去的未必逐字相同（模板拼装、改写等），对不上时要看得见。
    """
    result = _case("typical-01").model_copy(update={"question": "实际发出的问题"})
    assert "实际发出的问题" in render_case_detail(result)


def test_render_case_detail_uses_placeholders_instead_of_blank_lines() -> None:
    """空状态/空答案要有占位符：一行空着读者分不清"没产出"和"渲染坏了"。"""
    rendered = render_case_detail(_case("typical-01"))
    assert "（未产出）" in rendered  # 没有 answer_status
    assert "（空）" in rendered  # 没有 answer


def test_render_case_detail_surfaces_the_execution_error() -> None:
    """执行失败的原因必须出现在明细里，而不是只在产物 JSON 深处。"""
    rendered = render_case_detail(_case("typical-01", metrics=[], error="Milvus 不可达"))
    assert "执行错误：Milvus 不可达" in rendered


def test_render_case_detail_shows_no_score_for_a_not_applicable_metric() -> None:
    """不适用的指标不得渲染出分数——``0.0000`` 会被读成"考了零分"。"""
    result = _case(
        "refusal-01",
        metrics=[
            _metric(MetricName.REFUSAL_CORRECTNESS, MetricVerdict.PASS, 1.0),
            _metric(
                MetricName.TENANT_ISOLATION,
                MetricVerdict.NOT_APPLICABLE,
                None,
            ).model_copy(update={"detail": "本题不涉及租户隔离"}),
        ],
    )
    rendered = render_case_detail(result)
    assert "refusal_correctness: pass（1.0000）" in rendered
    assert "tenant_isolation: not_applicable — 本题不涉及租户隔离" in rendered
    assert "tenant_isolation: not_applicable（" not in rendered


# ---------------------------------------------------------------------------
# 阈值装载的拒绝分支
# ---------------------------------------------------------------------------
def test_load_thresholds_rejects_an_unknown_metric_name(tmp_path: Path) -> None:
    """指标改名而阈值没跟着改时，必须显式失败——静默忽略会让它无声退出所有门禁。"""
    path = _write_thresholds(tmp_path, "[thresholds]\nfaithfulnes = 0.8\n")
    with pytest.raises(ConfigurationError, match="未知指标名"):
        load_thresholds(path)


def test_load_thresholds_rejects_a_missing_thresholds_section(tmp_path: Path) -> None:
    """没有阈值等于没有门禁，不能默认放行。"""
    path = _write_thresholds(tmp_path, "[min_samples]\nfaithfulness = 3\n")
    with pytest.raises(ConfigurationError, match="thresholds"):
        load_thresholds(path)


@pytest.mark.parametrize("version", ["", "schema_version = 1\n"])
def test_load_thresholds_rejects_a_schema_version_mismatch(tmp_path: Path, version: str) -> None:
    """旧阈值文件（含压根没有版本号的）不得被新代码静默沿用。

    新指标表与旧门禁不是同一套语义：旧文件里没列出的新指标会"没列出＝不设门禁"，
    悄悄放掉一整类能力。缺版本号与写错版本号必须是同一条错误。
    """
    path = tmp_path / "thresholds.toml"
    path.write_text(f"{version}[thresholds]\nfaithfulness = 0.9\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="结构版本不匹配"):
        load_thresholds(path)


@pytest.mark.parametrize("metric", ["hallucination_rate", "document_precision_at_k"])
def test_load_thresholds_rejects_a_monitor_only_metric_as_a_threshold(
    tmp_path: Path, metric: str
) -> None:
    """仅监控指标不得设下限：一个观察量有了门禁就会被读成质量要求。"""
    path = _write_thresholds(tmp_path, f"[thresholds]\n{metric} = 0.5\n")

    with pytest.raises(ConfigurationError, match="仅监控指标"):
        load_thresholds(path)


@pytest.mark.parametrize("metric", ["hallucination_rate", "document_precision_at_k"])
def test_load_thresholds_rejects_a_monitor_only_metric_as_a_sample_floor(
    tmp_path: Path, metric: str
) -> None:
    """``[min_samples]`` 同样拒绝：样本量下限也是门禁的一半。"""
    path = _write_thresholds(
        tmp_path, f"[thresholds]\nfaithfulness = 0.9\n[min_samples]\n{metric} = 3\n"
    )

    with pytest.raises(ConfigurationError, match="仅监控指标"):
        load_thresholds(path)


def test_load_thresholds_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="不存在"):
        load_thresholds(tmp_path / "nope.toml")


def test_load_thresholds_rejects_malformed_toml(tmp_path: Path) -> None:
    """阈值文件是人手改的，写坏语法要给"TOML 非法"而不是裸 ``TOMLDecodeError``。"""
    path = tmp_path / "thresholds.toml"
    path.write_text("[thresholds\nfaithfulness = 0.8\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="TOML 非法"):
        load_thresholds(path)


def test_load_thresholds_rejects_a_non_numeric_value(tmp_path: Path) -> None:
    """值必须是数字：写成 ``"80%"`` 时要当场说清是哪个键，而不是等比较时才崩。"""
    path = _write_thresholds(tmp_path, '[thresholds]\nfaithfulness = "80%"\n')
    with pytest.raises(ConfigurationError, match="不是合法数值"):
        load_thresholds(path)


@pytest.mark.parametrize("value", ["3.5", "0", "-1"])
def test_load_thresholds_rejects_an_invalid_sample_floor(tmp_path: Path, value: str) -> None:
    """``min_samples`` 必须是正整数：**小于 1 的下限等于没有下限**。

    ``3.5`` 走的是另一条错路——``int(3.5)`` 会静默截断成 3，于是"我写了 3.5 条"与
    "至少 3 条"无人察觉地变成了同一件事。这类"配置敲错一点、门禁悄悄松一档"与
    ``test_insufficient_samples_is_not_a_pass`` 守的正是同一条底线，只是入口不同。
    """
    path = _write_thresholds(
        tmp_path, f"[thresholds]\nfaithfulness = 0.8\n[min_samples]\nfaithfulness = {value}\n"
    )
    with pytest.raises(ConfigurationError, match="不小于 1 的整数"):
        load_thresholds(path)


@pytest.mark.parametrize("value", ["-0.1", "1.5"])
def test_load_thresholds_rejects_a_threshold_outside_the_unit_interval(
    tmp_path: Path, value: str
) -> None:
    """阈值的定义域是 [0,1]：负阈值等于**取消**这条门禁，且不会留下任何痕迹。"""
    path = _write_thresholds(tmp_path, f"[thresholds]\nfaithfulness = {value}\n")
    with pytest.raises(ConfigurationError, match=r"超出 \[0,1\]"):
        load_thresholds(path)
