"""产物聚合、阈值门禁与基线对比（CLAUDE.md 9.2 / 9.3）。

本模块**全部是纯函数**（除两个薄薄的 JSON 读写外），因此它可以被离线单测完整覆盖：
门禁本身的可信度不能依赖"跑一次真实评估才知道对不对"。``runner.py`` 需要真实
基础设施所以被排除在覆盖率之外，而**判断"过没过"的这部分逻辑留在这里**，
正是为了让"喂一份人为做差的产物能否检出回归"这件事能用单测回答。

三条判据互相独立，缺一不可：

1. **阈值**：本次聚合分是否低于 ``thresholds.toml``。只看这个会把"整体还行、
   个别样例崩了"漏掉。
2. **逐例回归**：基线通过、本次失败的样例清单。只看这个会在"基线本来就很难看"
   时集体沉默——所以两者都要。
3. **配置漂移**：本次与基线的检索策略/Prompt 指纹/模型逻辑名是否不同。这一条
   不是判失败，而是要把话说清楚："这两个数字本来就不可比，因为它们跑的不是
   同一套配置"——静默并排两个不可比的数字，是评估留痕最隐蔽的失真。

阈值**不进 ``settings.py``**（CLAUDE.md 3.5 管的是服务配置）：评估阈值是门禁配置，
必须随黄金集版本化、可逐条 review 理由，且**绝不能被环境变量悄悄调低**——
那等于给"改门禁"开后门。
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_platform.core.errors import ArtifactIncompatibleError, ConfigurationError
from agent_platform.evaluation.schemas import (
    SCHEMA_VERSION,
    CaseResult,
    EvalArtifact,
    MetricName,
    MetricVerdict,
    RunMeta,
    RunVerdict,
)
from agent_platform.knowledge.schemas import ImageRef

_MONITOR_ONLY: frozenset[MetricName] = frozenset(
    {MetricName.HALLUCINATION_RATE, MetricName.DOCUMENT_PRECISION_AT_K}
)
"""只监控、不设门禁的指标（ADR-0006）。

它们在聚合分与对比报告里可见，但**不得**出现在 ``thresholds.toml`` 的
``[thresholds]`` / ``[min_samples]`` 里——一个只用于观察的指标一旦有了下限，
就会被读成"质量要求"，而它的定义（例如幻觉率 = 1 − Faithfulness）与门禁指标的
语义并不等价。加载器会显式拒绝，不做静默忽略。
"""

MONITOR_ONLY_METRICS: frozenset[str] = frozenset(metric.value for metric in _MONITOR_ONLY)
"""``_MONITOR_ONLY`` 的字符串形式；报告与脚本按指标名判断时用这个。"""


class Thresholds(BaseModel):
    """门禁阈值与最小样本量（``tests/evals/config/thresholds.toml`` 的契约）。"""

    model_config = ConfigDict(frozen=True)

    schema_version: int = Field(
        default=SCHEMA_VERSION,
        description="阈值文件结构版本；必须与产物 schema 一致，防止旧阈值被新代码静默沿用",
    )
    thresholds: dict[MetricName, float] = Field(
        description="指标名 → 下限；未列出的指标不参与门禁（但仍在报告中展示）"
    )
    min_samples: dict[MetricName, int] = Field(
        default_factory=dict,
        description="指标名 → 最小适用样例数；低于此值的聚合分不具代表性，报告须标注",
    )


class ThresholdBreach(BaseModel):
    """一项跌破阈值的指标。"""

    model_config = ConfigDict(frozen=True)

    metric: MetricName
    threshold: float
    actual: float
    samples: int = Field(ge=0)


class MeasurementFailure(BaseModel):
    """一项**没测出来**的指标（与"考砸了"是两回事，见 ``MetricVerdict.ERROR``）。

    ``ThresholdBreach`` 描述的是"测出来了，但太低"；本模型描述的是"压根没测出来"。
    两者都让 ``Gate.ok`` 为假，但**不得混为一谈**：把未采集写成 ``actual=0.0``
    的跌破，读报告的人会去查模型为什么不忠实，而真正该查的是裁判为什么解析不出结构。
    """

    model_config = ConfigDict(frozen=True)

    metric: MetricName
    errored: int = Field(ge=1, description="采集失败的样例数")


def measurement_errors(results: Sequence[CaseResult]) -> dict[str, int]:
    """各指标采集失败的样例数（``MetricVerdict.ERROR`` 的计数）。"""
    counts: dict[str, int] = {}
    for result in results:
        for metric in result.metrics:
            if metric.verdict is MetricVerdict.ERROR:
                counts[metric.name.value] = counts.get(metric.name.value, 0) + 1
    return dict(sorted(counts.items()))


class ConfigDrift(BaseModel):
    """本次与基线的一处配置差异。"""

    model_config = ConfigDict(frozen=True)

    field: str
    baseline: str
    current: str


class Gate(BaseModel):
    """门禁判定中**与基线无关**的那一半：本次分数、样本量、跌破与样本不足。

    单独成型的理由：阈值是"这一轮自己够不够格"，基线只是"和上一轮比变了没"。
    早先只有 ``compare()`` 会比对阈值，于是**不传 ``--baseline`` 时整份
    ``thresholds.toml`` 根本不参与判定**——一个"记得加参数才生效的门禁"，
    与没有门禁相差无几。把判定抽出来，两条路径共用同一份实现，顺带杜绝
    "两处各算一遍阈值、口径悄悄分叉"。
    """

    model_config = ConfigDict(frozen=True)

    scores: dict[str, float] = Field(description="指标名 → 本次聚合分")
    samples: dict[str, int] = Field(description="指标名 → 本次适用样例数（与聚合分母一致）")
    thresholds: dict[str, float] = Field(description="指标名 → 阈值")
    breaches: list[ThresholdBreach] = Field(default_factory=list)
    insufficient_samples: list[ThresholdBreach] = Field(
        default_factory=list, description="样本量不足的指标：同样不达标（见 ok）"
    )
    measurement_failures: list[MeasurementFailure] = Field(
        default_factory=list, description="未采集到结果的指标：无证据即不达标（见 ok）"
    )

    @property
    def ok(self) -> bool:
        """本次是否达标：**既无跌破，也无样本不足，更不能有没测出来的指标**。

        ``insufficient_samples`` 必须计入判定，不能只当告警——这正是早先的写法，
        它与 ``thresholds.toml`` 里 ``[min_samples]`` 的立意相矛盾：那条下限存在的
        唯一目的，就是拦住"某指标只采到一两条、分数再高也说不了什么"的假绿。
        一旦窄跑（``--limit``）恰好框进一条拒答样例，``refusal_correctness`` 就会
        以 1.0 满分、1 条样本的姿态出现在聚合里；只看 ``breaches`` 的话这一轮会
        堂堂正正地报"达标"。**没采够就是没证据，没证据不得判通过**（NO-12 同一条
        道理，也是 tests/evals/README.md 第四节写明的行为）。

        观察期不影响这里：观察期说的是"退出码暂不接入 pre-commit"，而不是"判定可以
        放宽"。判定放宽了，产物里留下的就是一份与规则不符的记录。
        """
        return not self.breaches and not self.insufficient_samples and not self.measurement_failures


class Comparison(BaseModel):
    """基线与本次的完整对比结论（供渲染与退出码判定）。"""

    model_config = ConfigDict(frozen=True)

    baseline_run_id: str
    current_run_id: str
    metrics: dict[str, tuple[float | None, float | None]] = Field(
        description="指标名 → (基线分, 本次分)；任一侧无样本时为 ``None``"
    )
    samples: dict[str, int] = Field(description="指标名 → 本次适用样例数")
    thresholds: dict[str, float] = Field(
        default_factory=dict, description="指标名 → 阈值；报告表格与分数并排展示"
    )
    breaches: list[ThresholdBreach] = Field(default_factory=list)
    insufficient_samples: list[ThresholdBreach] = Field(
        default_factory=list, description="样本量不足的指标；与 Gate 同口径，同样不达标"
    )
    measurement_failures: list[MeasurementFailure] = Field(
        default_factory=list, description="未采集到结果的指标；与 Gate 同口径，同样不达标"
    )
    drifts: list[ConfigDrift] = Field(default_factory=list)
    newly_failing: list[str] = Field(default_factory=list, description="基线通过、本次失败的样例")
    newly_passed: list[str] = Field(default_factory=list, description="基线失败、本次通过的样例")
    missing_in_current: list[str] = Field(
        default_factory=list, description="基线里有、本次未执行的样例（黄金集缩水的信号）"
    )
    golden_changed: bool = Field(
        default=False, description="黄金集指纹是否变化；变化时两组分数不同题，须显式喊出来"
    )


def aggregate(results: Sequence[CaseResult]) -> dict[str, float]:
    """按指标名聚合均分（只统计**适用**的指标，不适用不计入分母）。

    分母用"适用条数"而非"样例总数"：把 N/A 当 0 分会让一个只对一半样例适用的指标
    看起来很差，进而掩盖真正该看的数字。每个指标实际算了多少条由 ``metric_samples``
    给出，并在报告中与分数并列展示。

    返回的浮点保留 4 位小数——留痕要能逐位比对，多余的精度只是噪声。
    """
    totals: dict[str, list[float]] = {}
    for result in results:
        for metric in result.metrics:
            if metric.verdict is MetricVerdict.NOT_APPLICABLE or metric.score is None:
                continue
            totals.setdefault(metric.name.value, []).append(metric.score)
    scores = {name: round(sum(values) / len(values), 4) for name, values in sorted(totals.items())}
    # 幻觉率是派生量，不是第七个判分器。没有 faithfulness 样本时**省略**而不是记 0：
    # 0 幻觉率读起来是"完美"，而事实是"这一轮没有可核的证据"。
    faithfulness = scores.get(MetricName.FAITHFULNESS.value)
    if faithfulness is not None:
        scores[MetricName.HALLUCINATION_RATE.value] = round(1.0 - faithfulness, 4)
    return dict(sorted(scores.items()))


def metric_samples(results: Sequence[CaseResult]) -> dict[str, int]:
    """各指标实际参与聚合的样例数（与 ``aggregate`` 的分母严格一致）。"""
    counts: dict[str, int] = {}
    for result in results:
        for metric in result.metrics:
            if metric.verdict is not MetricVerdict.NOT_APPLICABLE and metric.score is not None:
                counts[metric.name.value] = counts.get(metric.name.value, 0) + 1
    return dict(sorted(counts.items()))


def evaluate_gate(results: Sequence[CaseResult], thresholds: Thresholds) -> Gate:
    """把一轮结果判成门禁结论（**不需要基线**）。

    这是"够不够格"的唯一实现；``compare()`` 与无基线路径都走这里，因此两条路径
    不可能出现口径分叉。
    """
    scores = aggregate(results)
    samples = metric_samples(results)
    breaches, insufficient, unmeasured = check_thresholds(
        scores, samples, thresholds, measurement_errors=measurement_errors(results)
    )
    return Gate(
        scores=scores,
        samples=samples,
        thresholds={name.value: floor for name, floor in thresholds.thresholds.items()},
        breaches=breaches,
        insufficient_samples=insufficient,
        measurement_failures=unmeasured,
    )


_JUDGE_METRICS = frozenset(
    {
        MetricName.FAITHFULNESS,
        MetricName.ANSWER_RELEVANCY,
        MetricName.ANSWER_CORRECTNESS,
        MetricName.CITATION_ACCURACY,
    }
)
_RAGAS_METRICS = frozenset(
    {MetricName.FAITHFULNESS, MetricName.ANSWER_RELEVANCY, MetricName.ANSWER_CORRECTNESS}
)


def detect_judge_traceability_gap(artifact: EvalArtifact) -> str:
    """产物是否"有裁判分数、却说不清谁判的"。返回空串表示没问题。

    §9.2 要求"裁判模型与标准固定并记录版本"。若某轮的样例带着裁判分数，而
    ``run.models.judge`` 为空，这份产物就永久失去"谁判的"这一栏——日后要回答
    "这批分数是本地裁判还是云端裁判给的""换了裁判之后分数为什么动了"，
    唯一能查的地方就是它。把这样的产物提成基线，等于让之后每一次对比都建立在一个
    说不清来源的参照上，而且它长得与合格产物一模一样。

    **为什么不是 schema 校验**：更早就存在的产物（含已入库的首份基线）本来就缺这一栏，
    在读取时硬拦会让它们直接不可读，连"翻旧账"都做不到。所以只在这里提供一个可被
    显式调用的检查，由 ``_promote`` 决定是否据此拒绝入库。

    Args:
        artifact: 待检查的产物。

    Returns:
        空串（合格）或一句说明缺口的话。
    """
    has_judge_score = any(
        metric.name in _JUDGE_METRICS
        and metric.verdict not in (MetricVerdict.NOT_APPLICABLE, MetricVerdict.ERROR)
        for case in artifact.cases
        for metric in case.metrics
    )
    if has_judge_score and not artifact.run.models.judge:
        return (
            "样例里有裁判指标（faithfulness / answer_relevancy / answer_correctness / "
            "citation_accuracy），但 run.models.judge 为空："
            "这份产物说不清是谁判的分，不能作为基线"
        )
    has_ragas_score = any(
        metric.name in _RAGAS_METRICS
        and metric.verdict not in (MetricVerdict.NOT_APPLICABLE, MetricVerdict.ERROR)
        for case in artifact.cases
        for metric in case.metrics
    )
    if has_ragas_score and not artifact.run.judge.ragas_version:
        return (
            "样例里有 RAGAS 指标，但 run.judge.ragas_version 为空："
            "RAGAS 的分数会随版本漂移，说不清版本就无法复现，不能作为基线"
        )
    return ""


def render_gate(gate: Gate) -> str:
    """渲染无基线时的门禁表（有基线时由 ``render_comparison`` 承担同一职责）。"""
    unmeasured = {item.metric.value for item in gate.measurement_failures}
    lines = [f"{'指标':<22}{'本次':>9}{'阈值':>9}{'样本':>7}   判定"]
    for name in sorted(gate.thresholds):
        floor = gate.thresholds[name]
        after = gate.scores.get(name)
        samples = gate.samples.get(name, 0)
        verdict = "—"
        if after is not None:
            verdict = "达标" if after >= floor else "跌破"
        if name in unmeasured:
            verdict = "未测全"
        lines.append(f"{name:<22}{_fmt(after):>9}{_fmt(floor):>9}{samples:>7}   {verdict}")

    monitor = {name: gate.scores[name] for name in MONITOR_ONLY_METRICS if name in gate.scores}
    if monitor:
        lines.append("")
        lines.append("监控指标（不设门禁）：")
        lines.extend(f"  · {name}: {value:.4f}" for name, value in sorted(monitor.items()))

    if gate.breaches:
        lines.append("")
        lines.append("跌破阈值：")
        lines.extend(
            f"  ✗ {breach.metric.value}: {_breach_actual(breach)}" for breach in gate.breaches
        )
    if gate.insufficient_samples:
        lines.append("")
        lines.append("样本量不足（同样不达标：没采够即无证据）：")
        lines.extend(
            f"  ! {item.metric.value}: 仅 {item.samples} 条 < 要求 {item.threshold:.0f} 条"
            for item in gate.insufficient_samples
        )
    if gate.measurement_failures:
        lines.append("")
        lines.append("未采集到结果（同样不达标：测不出来不等于考得好）：")
        lines.extend(
            f"  ? {item.metric.value}: {item.errored} 条样例测量失败，已从聚合中排除"
            for item in gate.measurement_failures
        )
    return "\n".join(lines)


def decide_verdict(results: Sequence[CaseResult]) -> RunVerdict:
    """整轮判定：有执行错误或有样例未通过即 ``FAIL``，否则 ``PASS``。

    ``INCOMPLETE`` 由调用方在"指标被刻意跳过"时覆盖（如 ``--skip-judge``）——
    执行器只回答"这一轮我看到的样例过没过"。
    """
    if any(result.error for result in results):
        return RunVerdict.FAIL
    if any(not result.passed for result in results):
        return RunVerdict.FAIL
    return RunVerdict.PASS


def load_thresholds(path: Path) -> Thresholds:
    """从 TOML 装载阈值。

    Args:
        path: ``thresholds.toml`` 路径。

    Returns:
        ``Thresholds``；``thresholds`` 段必填，``min_samples`` 段可选。

    Raises:
        ConfigurationError: 文件不存在/不可读/TOML 非法，或出现未知指标名。
            未知指标名必须显式失败——那通常意味着指标被改名而阈值没跟着改，
            静默忽略会让那条指标**无声地**退出所有门禁。
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"阈值文件不存在: {path}", detail=str(exc)) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(
            f"阈值文件不可读或 TOML 非法: {path}（{type(exc).__name__}）", detail=str(exc)
        ) from exc

    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        # 旧文件缺 schema_version 时这里也会拦下：新指标表与旧门禁不是同一套语义，
        # 静默沿用会让"没列出的指标就不设门禁"这条规则悄悄放掉新指标。
        raise ConfigurationError(
            f"阈值文件结构版本不匹配: {path}（文件 {version!r}，本代码要求 {SCHEMA_VERSION}）",
            detail="按新指标表重写阈值文件后再跑评估；旧阈值不得被新代码静默沿用。",
        )

    thresholds_raw = raw.get("thresholds")
    if not isinstance(thresholds_raw, dict) or not thresholds_raw:
        raise ConfigurationError(
            f"阈值文件缺少 [thresholds] 段或为空: {path}",
            detail="没有阈值等于没有门禁，必须显式给出。",
        )
    min_samples_raw = raw.get("min_samples") or {}
    _reject_monitor_only(thresholds_raw, path, section="thresholds")
    _reject_monitor_only(min_samples_raw, path, section="min_samples")
    return Thresholds(
        schema_version=version,
        thresholds=_parse_metric_map(thresholds_raw, path, cast=float),
        min_samples=_parse_min_samples(min_samples_raw, path),
    )


def _reject_monitor_only(raw: Any, path: Path, *, section: str) -> None:
    """拒绝把仅监控指标写进门禁配置。

    静默忽略是不够的：写文件的人显然认为它该被考核，而门禁不会执行——
    要么他以为有门禁（实际没有），要么下次有人"修好"它并把它变成真门禁。
    两种结果都比一条报错差。

    Raises:
        ConfigurationError: ``section`` 里出现 ``_MONITOR_ONLY`` 中的指标。
    """
    if not isinstance(raw, dict):
        return
    monitor_names = {metric.value for metric in _MONITOR_ONLY}
    rejected = sorted(key for key in raw if key in monitor_names)
    if rejected:
        raise ConfigurationError(
            f"阈值文件 [{section}] 段包含仅监控指标: {'、'.join(rejected)}（{path}）",
            detail=(
                "幻觉率与文档级精确率只用于观察，不设门禁。"
                "给它们设下限会让监控指标被读成质量要求；请从 [thresholds] / [min_samples] 中删除。"
            ),
        )


def _parse_metric_map(
    raw: dict[str, Any], path: Path, *, cast: Callable[[Any], Any]
) -> dict[MetricName, Any]:
    """把 TOML 的字符串键转成 ``MetricName``，未知键显式失败。

    Raises:
        ConfigurationError: 键不是已知指标名，或值无法转成目标类型。
    """
    parsed: dict[MetricName, Any] = {}
    for key, value in raw.items():
        name = _metric_name(key, path)
        try:
            score = cast(value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"阈值文件的 {key!r} 不是合法数值: {value!r}", detail=str(exc)
            ) from exc
        if not 0.0 <= score <= 1.0:
            # 阈值超出 [0,1] 时门禁要么恒不触发、要么恒触发。恒触发是吵的（一眼可见），
            # 恒不触发才是危险的——一个敲错的负号就能让某条指标无声退出所有门禁。
            # 指标分本身被 ``MetricResult`` 约束在 [0,1]，故这里不是风格偏好而是定义域。
            raise ConfigurationError(
                f"阈值文件的 {key!r} 超出 [0,1]: {score!r}",
                detail="所有指标分都在 [0,1]；负阈值等于取消这条门禁，不想要门禁就不要列出该指标。",
            )
        parsed[name] = score
    return parsed


def _parse_min_samples(raw: dict[str, Any], path: Path) -> dict[MetricName, int]:
    """解析 ``[min_samples]``：值必须是**正整数**。

    为什么不能直接 ``int(value)``：``3.5`` 会被静默截断成 3，而 ``0`` / ``-1`` 会让
    "样本量下限"彻底失效——``count < -1`` 恒为假，于是任何窄跑（``--limit``）都
    "达标"。这与 ``Gate.ok`` 计入 ``insufficient_samples`` 是同一件事的两半：那边
    保证没采够不判通过，这边保证这条下限本身不会被写成一句空话。门槛被配置悄悄
    调没，和判定放宽是一样的事，只是更隐蔽。

    ``bool`` 要单独挡下：TOML 的 ``true`` 在 Python 里就是 ``int``，会被当成 1。

    Raises:
        ConfigurationError: 键不是已知指标名，或值不是正整数。
    """
    parsed: dict[MetricName, int] = {}
    for key, value in raw.items():
        name = _metric_name(key, path)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigurationError(
                f"阈值文件的 min_samples.{key!r} 必须为不小于 1 的整数: {value!r}",
                detail="小于 1 的下限等于没有下限；小数会被静默截断，故一并拒绝。",
            )
        parsed[name] = value
    return parsed


def _metric_name(key: str, path: Path) -> MetricName:
    """把 TOML 的字符串键转成 ``MetricName``。

    Raises:
        ConfigurationError: 键不是已知指标名。两处解析（阈值与最小样本量）共用，
            避免"未知指标名"这件事出现两份判断而其中一份漏掉。
    """
    try:
        return MetricName(key)
    except ValueError as exc:
        known = "、".join(sorted(item.value for item in MetricName))
        raise ConfigurationError(
            f"阈值文件中的未知指标名: {key!r}（{path}）", detail=f"已知指标：{known}"
        ) from exc


def check_thresholds(
    scores: dict[str, float],
    samples: dict[str, int],
    thresholds: Thresholds,
    *,
    measurement_errors: Mapping[str, int] | None = None,
) -> tuple[list[ThresholdBreach], list[ThresholdBreach], list[MeasurementFailure]]:
    """比对阈值，返回 ``(跌破清单, 样本不足清单, 未采集清单)``。

    **指标缺席即跌破**：阈值里列了某指标而本次聚合里没有它（例如只跑了检索层），
    这不能当成"没考所以不算"——它意味着这类能力这一轮**没有任何证据**，
    按"无证据不得判定通过"处理（NO-12 的同一条道理）。

    但"缺席"有**两种成因**，报告里必须分开：压根没跑（还在上面的口径里），
    与跑了而测量失败（``measurement_errors`` 给出计数）。后者若也写成
    ``actual=0.0`` 的跌破，读报告的人会去查模型，而该查的是裁判。
    """
    breaches: list[ThresholdBreach] = []
    insufficient: list[ThresholdBreach] = []
    unmeasured: list[MeasurementFailure] = []
    errors = measurement_errors or {}
    for metric, floor in sorted(thresholds.thresholds.items(), key=lambda item: item[0].value):
        key = metric.value
        actual = scores.get(key)
        if actual is None:
            if errored := errors.get(key):
                # 全部样例的该项都测量失败：这是"没测出来"，不是"测得 0 分"。
                unmeasured.append(MeasurementFailure(metric=metric, errored=errored))
            else:
                breaches.append(
                    ThresholdBreach(metric=metric, threshold=floor, actual=0.0, samples=0)
                )
            continue
        count = samples.get(key, 0)
        if actual < floor:
            breaches.append(
                ThresholdBreach(metric=metric, threshold=floor, actual=actual, samples=count)
            )
        minimum = thresholds.min_samples.get(metric)
        if minimum is not None and count < minimum:
            insufficient.append(
                ThresholdBreach(
                    metric=metric, threshold=float(minimum), actual=actual, samples=count
                )
            )
        # 部分样例测量失败：分数本身仍有效（分母已排除失败样例），但这一轮
        # **没把该测的都测完**，按"没采够即无证据"并入未采集清单。
        if errored := errors.get(key):
            unmeasured.append(MeasurementFailure(metric=metric, errored=errored))
    return breaches, insufficient, unmeasured


def detect_drift(baseline: RunMeta, current: RunMeta) -> list[ConfigDrift]:
    """比对两次运行的配置快照，列出全部差异。

    覆盖四类：检索策略、模型逻辑名、裁判配置、Prompt 指纹（含语料清单指纹）。
    """
    drifts: list[ConfigDrift] = []
    for group, left, right in (
        ("retrieval", baseline.retrieval.model_dump(), current.retrieval.model_dump()),
        ("models", baseline.models.model_dump(), current.models.model_dump()),
        ("judge", baseline.judge.model_dump(), current.judge.model_dump()),
    ):
        drifts.extend(_diff_flat(group, left, right))
    drifts.extend(_diff_flat("prompt", baseline.prompt_versions, current.prompt_versions))
    drifts.extend(
        _diff_flat(
            "corpus",
            {
                "collection": baseline.corpus.collection,
                "chunk_count": baseline.corpus.chunk_count,
                "manifest_sha256": baseline.corpus.manifest_sha256,
            },
            {
                "collection": current.corpus.collection,
                "chunk_count": current.corpus.chunk_count,
                "manifest_sha256": current.corpus.manifest_sha256,
            },
        )
    )
    return drifts


def _diff_flat(group: str, left: dict[str, Any], right: dict[str, Any]) -> list[ConfigDrift]:
    """逐键比较两个扁平字典，值不同即记一条漂移（缺键按空串计）。"""
    drifts: list[ConfigDrift] = []
    for key in sorted(set(left) | set(right)):
        before, after = left.get(key, ""), right.get(key, "")
        if before != after:
            drifts.append(
                ConfigDrift(field=f"{group}.{key}", baseline=str(before), current=str(after))
            )
    return drifts


def compare(
    baseline: EvalArtifact,
    current: EvalArtifact,
    thresholds: Thresholds,
) -> Comparison:
    """对比基线与本次产物，产出完整结论。

    Args:
        baseline: 基线产物（已通过 ``read_artifact`` 校验版本）。
        current: 本次产物。
        thresholds: 门禁阈值。

    Returns:
        ``Comparison``；调用方据 ``breaches`` / ``newly_failing`` 决定退出码。
    """
    gate = evaluate_gate(current.cases, thresholds)
    breaches, insufficient = gate.breaches, gate.insufficient_samples
    samples, scores = gate.samples, gate.scores

    baseline_by_id = {case.case_id: case for case in baseline.cases}
    current_by_id = {case.case_id: case for case in current.cases}
    newly_failing = sorted(
        case_id
        for case_id, case in current_by_id.items()
        if case_id in baseline_by_id and baseline_by_id[case_id].passed and not case.passed
    )
    newly_passed = sorted(
        case_id
        for case_id, case in current_by_id.items()
        if case_id in baseline_by_id and not baseline_by_id[case_id].passed and case.passed
    )
    missing = sorted(set(baseline_by_id) - set(current_by_id))

    baseline_scores = aggregate(baseline.cases)
    metrics = {
        name: (baseline_scores.get(name), scores.get(name))
        for name in sorted(set(baseline_scores) | set(scores))
    }
    return Comparison(
        baseline_run_id=baseline.run.run_id,
        current_run_id=current.run.run_id,
        metrics=metrics,
        samples=samples,
        thresholds={name.value: floor for name, floor in thresholds.thresholds.items()},
        breaches=breaches,
        insufficient_samples=insufficient,
        measurement_failures=gate.measurement_failures,
        drifts=detect_drift(baseline.run, current.run),
        newly_failing=newly_failing,
        newly_passed=newly_passed,
        missing_in_current=missing,
        golden_changed=baseline.run.golden.sha256 != current.run.golden.sha256,
    )


def render_case_detail(result: CaseResult, *, question: str = "") -> str:
    """渲染单条样例的明细（``--show <id>`` 的复核辅助）。

    附上每条证据的**文档名与页码**：评审人要拿它去翻原文核对参考答案，
    只给分数是核对不动的。
    """
    lines = [
        f"样例 {result.case_id}（{result.category.value}／{result.layer.value}）",
        f"问题：{question or result.question}",
        f"状态：{result.answer_status or '（未产出）'}　耗时：{result.latency_ms} ms",
    ]
    if result.error:
        lines.append(f"执行错误：{result.error}")
    if result.node_trace:
        lines.append(f"节点序列：{' → '.join(result.node_trace)}")
    lines.append(f"答案：{result.answer or '（空）'}")
    if result.citations:
        lines.append("引用：")
        lines.extend(
            f"  [{item.index}] {item.document_name}"
            f"（页码 {item.page_numbers or '—'}）chunk {item.chunk_id}"
            f"{_describe_images(item.images)}"
            for item in result.citations
        )
    if result.evidence:
        lines.append("检索到的证据：")
        lines.extend(
            f"  [{position}] {item.document_name or item.chunk_id}"
            f"（页码 {item.page_numbers or '—'}，媒介 {item.media_type.value}）"
            f"分数 {item.score:.4f}{_describe_images(item.images)}"
            for position, item in enumerate(result.evidence, start=1)
        )
    if result.metrics:
        lines.append("指标：")
        lines.extend(
            f"  {metric.name.value}: {metric.verdict.value}"
            f"{'' if metric.score is None else f'（{metric.score:.4f}）'} — {metric.detail}"
            for metric in result.metrics
        )
    return "\n".join(lines)


def _describe_images(images: Sequence[ImageRef]) -> str:
    """渲染图片引用的简短说明（image_id、页码、哈希前缀），无图返回空串。

    哈希只给前 12 位：完整哈希 64 位会把一行挤得没法读，而评审要做的
    "两张图是不是同一张"的判断，12 位已经足够区分。
    """
    if not images:
        return ""
    parts = []
    for ref in images:
        page = f"第 {ref.page} 页" if ref.page else "页码—"
        parts.append(f"{ref.image_id}（{page}，sha256 {ref.sha256[:12]}…）")
    return "；图片 " + "、".join(parts)


def render_comparison(comparison: Comparison) -> str:
    """把对比结论渲染成人类可读的多段文本（控制台输出用）。

    漂移段与黄金集变化段**放在最前面**：它们是"这两组数字能不能比"的前提，
    排在分数表后面会让人先读结论再读前提。
    """
    lines: list[str] = []
    if comparison.golden_changed:
        lines.append("⚠ 黄金集已变化：基线与本次不是同一套题，逐例对比仅供参考。")
    if comparison.drifts:
        lines.append("⚠ 配置漂移（这正是本次要评估的变更，两组数字不可直接比较）：")
        lines.extend(
            f"    {drift.field}: {drift.baseline} → {drift.current}" for drift in comparison.drifts
        )
    if comparison.missing_in_current:
        lines.append(
            f"⚠ 基线有而本次未执行的样例 {len(comparison.missing_in_current)} 条："
            f"{'、'.join(comparison.missing_in_current[:5])}"
            f"{'…' if len(comparison.missing_in_current) > 5 else ''}"
        )

    lines.append("")
    lines.append(f"{'指标':<22}{'基线':>9}{'本次':>9}{'Δ':>10}{'阈值':>9}{'样本':>7}   判定")
    for name, (before, after) in comparison.metrics.items():
        delta = "—" if before is None or after is None else f"{after - before:+.4f}"
        monitor_only = name in MONITOR_ONLY_METRICS
        floor = None if monitor_only else comparison.thresholds.get(name)
        count = comparison.samples.get(name)
        # 监控指标没有"适用样例数"这回事（幻觉率由 faithfulness 派生），
        # 显示 0 会让人以为一条都没测。
        samples = "—" if monitor_only and count is None else str(count or 0)
        verdict = "监控" if monitor_only else "—"
        if not monitor_only and floor is not None and after is not None:
            verdict = "达标" if after >= floor else "跌破"
        if any(item.metric.value == name for item in comparison.measurement_failures):
            # 分数列是**部分样例**的均分（失败样例已排除），单看它读不出"这轮没测全"。
            verdict = "未测全"
        lines.append(
            f"{name:<22}{_fmt(before):>9}{_fmt(after):>9}{delta:>10}"
            f"{_fmt(floor):>9}{samples:>7}   {verdict}"
        )

    if comparison.breaches:
        lines.append("")
        lines.append("跌破阈值：")
        for breach in comparison.breaches:
            lines.append(f"  ✗ {breach.metric.value}: {_breach_actual(breach)}")
    if comparison.insufficient_samples:
        lines.append("")
        lines.append("样本量不足（同样不达标：没采够即无证据）：")
        for item in comparison.insufficient_samples:
            lines.append(
                f"  ! {item.metric.value}: 仅 {item.samples} 条 < 要求 {item.threshold:.0f} 条"
            )
    if comparison.measurement_failures:
        lines.append("")
        lines.append("未采集到结果（同样不达标：测不出来不等于考得好）：")
        for failure in comparison.measurement_failures:
            lines.append(
                f"  ? {failure.metric.value}: {failure.errored} 条样例测量失败，已从聚合中排除"
            )
    if comparison.newly_failing:
        lines.append("")
        lines.append(f"新失败样例 {len(comparison.newly_failing)} 条：")
        lines.extend(f"  ✗ {case_id}" for case_id in comparison.newly_failing)
    if comparison.newly_passed:
        lines.append("")
        lines.append(f"新通过样例 {len(comparison.newly_passed)} 条：")
        lines.extend(f"  ✓ {case_id}" for case_id in comparison.newly_passed)
    return "\n".join(lines)


def _breach_actual(breach: ThresholdBreach) -> str:
    """跌破描述。

    ``samples == 0`` 时**不能**渲染成 "0.0000 < 1.0000"——那读起来像"考了 0 分"，
    而事实是"这一轮根本没采集到该指标"，两者的处置完全不同（后者要补跑，
    前者要改实现）。所以这里必须分叉。
    """
    if breach.samples == 0:
        return f"未采集（阈值 {breach.threshold:.4f}，本轮无适用样例）"
    return f"{breach.actual:.4f} < {breach.threshold:.4f}（适用 {breach.samples} 条）"


def _fmt(value: float | None) -> str:
    """分数格式化：无样本显示 ``—``，避免把"没测"渲染成 0.0000。"""
    return "—" if value is None else f"{value:.4f}"


def write_artifact(artifact: EvalArtifact, path: Path) -> None:
    """把产物写入 JSON（目录自动创建；UTF-8、缩进 2、保留中文）。

    写出前做一次**读回自检**：``model_dump_json`` 同样不做校验，而本仓大量使用
    ``model_copy(update=...)``——那也不校验。两条路都能让一个不合契约的对象
    一路写进文件，直到 ``read_artifact`` 才炸。

    **这不是假想的**（2026-10-16 实测）：``compact_for_baseline`` 曾把
    ``evidence[].content`` 置为空串，违背 ``min_length=1``；``--promote-baseline``
    照样打印"基线已提升"，而那份**已入库的基线自己读不回来**，``--baseline``
    对它恒报"基线不可读"。写路径不校验、读路径才校验，缝正好在这里。

    Raises:
        ArtifactIncompatibleError: 写出的内容无法按契约读回。宁可当场失败——
            一份读不回来的产物比没有产物更糟：它看起来是一样的，只在被引用时
            才暴露，而那时引用它的人已经不知道它写自哪一轮了。
    """
    body = artifact.model_dump_json(indent=2, exclude_none=False)
    try:
        EvalArtifact.model_validate_json(body)
    except ValidationError as exc:
        fields = "；".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:5]
        )
        raise ArtifactIncompatibleError(
            f"产物写不出去——它不符合契约，读回来会失败: {path}", detail=fields
        ) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n"：产物要能逐字节比对——入库的基线会被重新读回、再写出，而默认的
    # 行尾翻译会让同一份 JSON 在 Windows 上落成 CRLF、在 Linux 上落成 LF，于是每次
    # 提升基线都产生一次整文件 diff。仓库的行尾策略本就是 LF（.gitattributes）。
    path.write_text(body, encoding="utf-8", newline="\n")


def promote_baseline(artifact: EvalArtifact) -> EvalArtifact:
    """把产物提升为可入仓的基线副本，并记一条来源说明。

    剥离动作**全部**委托给 ``EvalArtifact.compact_for_baseline()``（NO-17 那条
    "入仓产物不含语料"的唯一实现，已由 ``tests/unit/test_evaluation_schemas.py``
    守着）。此处只补一件事：在 ``notes`` 里写清这份文件是提升来的、以及完整留痕
    在哪——读到基线的人不必去猜它为什么比 runs 目录里那份瘦。

    **早先这里曾有一份自写的剥离逻辑**（清空 ``answer``/``judge_raw``/``evidence``），
    它有两个问题：一是同一件事有了两份实现，改其中一份另一份不会跟着变；二是它把
    ``evidence`` 整段丢掉，连带丢了 ``chunk_id``/``page_numbers``——而基线的用处之一
    正是"当时命中了哪几份文档的哪几页"（见 ``compact_for_baseline`` 的 docstring）。
    """
    return artifact.compact_for_baseline().model_copy(
        update={
            "notes": [
                *artifact.notes,
                "本文件为提升后的基线副本：已按 compact_for_baseline 剥离语料正文与"
                "裁判原文（完整留痕见 local_data/evals/runs/）。",
            ],
        }
    )


def read_artifact(path: Path, *, allow_schema_mismatch: bool = False) -> EvalArtifact:
    """读入产物并校验结构版本。

    Args:
        path: 产物 JSON 路径。
        allow_schema_mismatch: ``True`` 时容忍版本不符（仅用于人工查看，
            **不得**用于对比）。

    Returns:
        ``EvalArtifact``。

    Raises:
        ArtifactIncompatibleError: 文件不存在、JSON 非法、结构不符契约，
            或 ``schema_version`` 与当前不符且未放行。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ArtifactIncompatibleError(f"产物不存在: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactIncompatibleError(
            f"产物不可读或 JSON 非法: {path}（{type(exc).__name__}）"
        ) from exc

    version = payload.get("schema_version") if isinstance(payload, dict) else None
    if version != SCHEMA_VERSION and not allow_schema_mismatch:
        raise ArtifactIncompatibleError(
            f"产物结构版本不兼容: {path}",
            detail=f"产物为 {version!r}，当前支持 {SCHEMA_VERSION}。跨版本不做推断，请重跑。",
        )
    try:
        return EvalArtifact.model_validate(payload)
    except ValidationError as exc:
        # 只捕 pydantic 的校验错误（NO-06：不写裸 except）。原异常进 ``__cause__``，
        # 字段级错误摘要进 detail，便于定位是哪张快照变了形。
        fields = "；".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:5]
        )
        raise ArtifactIncompatibleError(f"产物结构不符合契约: {path}", detail=fields) from exc


__all__: list[str] = [
    "MONITOR_ONLY_METRICS",
    "Comparison",
    "ConfigDrift",
    "Gate",
    "MeasurementFailure",
    "ThresholdBreach",
    "Thresholds",
    "aggregate",
    "check_thresholds",
    "compare",
    "decide_verdict",
    "detect_drift",
    "detect_judge_traceability_gap",
    "evaluate_gate",
    "load_thresholds",
    "measurement_errors",
    "metric_samples",
    "promote_baseline",
    "read_artifact",
    "render_case_detail",
    "render_comparison",
    "render_gate",
    "write_artifact",
]
