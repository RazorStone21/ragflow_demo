"""RAGAS 指标的适用性、缓存与四态映射（ADR-0006）。

``ragas_backend.py`` 只管"怎么调 RAGAS"；本模块决定**什么时候不调**、失败怎么记、
分数从哪来。两层分开的理由：适用性规则是本仓的评估契约（可以离线单测），
而 RAGAS 的调用细节会随版本变化。

适用性（ADR-0006 §三）：

- 期望非 ``answered``（拒答/安全类）→ 三项全部 ``NOT_APPLICABLE``。Answer Relevancy
  对合法拒答有系统性惩罚，拿它给拒答打分等于用错的尺子量。
- 期望 ``answered`` 但实际未答或答案为空 → ``answer_correctness`` 直接
  ``FAIL 0.0``（状态兜底并入正确性），不调用 RAGAS；faithfulness / relevancy 因无
  事实性内容记 ``NOT_APPLICABLE``。
- 正常作答：faithfulness 需有证据；correctness 需有参考答案；否则各自 N/A。

失败语义：RAGAS 抛错、空断言、NaN、越界一律记 ``MetricVerdict.ERROR``——**不带分数**，
本样例因此判不通过。把测量故障记成 0 分会让一次裁判事故伪装成一次质量回归
（ADR-0003 已付过这次学费）。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_platform.agents.gov_policy_qa.state import AnswerStatus
from agent_platform.core.logging import get_logger
from agent_platform.evaluation.judge import JudgeCache
from agent_platform.evaluation.ragas_backend import (
    RagasBackend,
    RagasMeasurementError,
    RagasRequest,
    canonical_request,
)
from agent_platform.evaluation.schemas import (
    CaseResult,
    GoldenCase,
    MetricName,
    MetricResult,
    MetricVerdict,
)

logger = get_logger(__name__)

_RAGAS_METRICS: tuple[MetricName, ...] = (
    MetricName.FAITHFULNESS,
    MetricName.ANSWER_RELEVANCY,
    MetricName.ANSWER_CORRECTNESS,
)


class RagasJudge:
    """把 RAGAS 后端接到评测产物上的判分器。"""

    def __init__(
        self,
        backend: RagasBackend,
        *,
        cache_dir: Path,
        cache_enabled: bool = True,
    ) -> None:
        """
        Args:
            backend: 判分后端（真实 ``RagasScorer`` 或离线 fake）。
            cache_dir: 判分缓存目录（只有成功分数会写入）。
            cache_enabled: ``False`` 时完全绕过缓存（``--no-judge-cache``，
                用于核对缓存是否污染了本轮分数）。
        """
        self._backend = backend
        self._cache = JudgeCache(cache_dir)
        self._cache_enabled = cache_enabled

    @property
    def library_version(self) -> str:
        """RAGAS 版本。"""
        return self._backend.library_version

    @property
    def embedding_model(self) -> str:
        """嵌入模型逻辑名。"""
        return self._backend.embedding_model

    @property
    def telemetry_disabled(self) -> bool:
        """遥测是否已强制关闭。"""
        return self._backend.telemetry_disabled

    @property
    def metric_names(self) -> tuple[str, ...]:
        """启用的指标名。"""
        return self._backend.metric_names

    async def aclose(self) -> None:
        """释放后端客户端。"""
        await self._backend.aclose()

    async def score(self, case: GoldenCase, result: CaseResult) -> CaseResult:
        """对一条样例补上 RAGAS 三项指标。

        Args:
            case: 黄金集样例（问题、期望状态、参考答案）。
            result: 图层的执行结果。

        Returns:
            新的 ``CaseResult``：``metrics`` 追加三项（已存在同名指标被替换），
            ``judge_raw`` 追加每项的 value 留痕。
        """
        answer = result.answer.strip()
        metrics: list[MetricResult] = []
        raws: list[str] = []

        if not case.is_answerable:
            metrics.extend(
                _na_all(
                    "本题期望非 answered，RAGAS 生成指标不适用（拒答由 refusal_correctness 兜底）"
                )
            )
        elif result.answer_status != AnswerStatus.ANSWERED.value or not answer:
            actual = result.answer_status or "<空>"
            metrics.append(
                MetricResult(
                    name=MetricName.ANSWER_CORRECTNESS,
                    verdict=MetricVerdict.FAIL,
                    score=0.0,
                    detail=f"期望 answered，实际状态 {actual}"
                    + ("；答案为空" if not answer else "（状态兜底并入正确性）"),
                )
            )
            metrics.extend(
                [
                    _na(
                        MetricName.FAITHFULNESS,
                        "实际未作答，无事实性内容可核（原因见 answer_correctness）",
                    ),
                    _na(
                        MetricName.ANSWER_RELEVANCY,
                        "实际未作答，Answer Relevancy 不适用于拒答/空答案",
                    ),
                ]
            )
        else:
            metrics.extend(await self._score_answerable(case, result, answer=answer, raws=raws))

        handled = {metric.name for metric in metrics}
        preserved = [metric for metric in result.metrics if metric.name not in handled]
        raw_text = "\n\n".join(section for section in raws if section)
        if raw_text and result.judge_raw:
            raw_text = f"{result.judge_raw}\n\n{raw_text}"
        elif not raw_text:
            raw_text = result.judge_raw
        return result.model_copy(update={"metrics": [*preserved, *metrics], "judge_raw": raw_text})

    async def _score_answerable(
        self,
        case: GoldenCase,
        result: CaseResult,
        *,
        answer: str,
        raws: list[str],
    ) -> list[MetricResult]:
        """正常作答路径：逐项判断适用性并独立执行（一项失败不拖累其他项）。"""
        metrics: list[MetricResult] = []
        contexts = tuple(item.content for item in result.evidence)

        if contexts:
            request = RagasRequest(question=case.question, answer=answer, contexts=contexts)
            metrics.append(await self._run(MetricName.FAITHFULNESS, request, raws=raws))
        else:
            metrics.append(_na(MetricName.FAITHFULNESS, "本题无检索证据，无据可核"))

        relevancy_request = RagasRequest(question=case.question, answer=answer)
        metrics.append(await self._run(MetricName.ANSWER_RELEVANCY, relevancy_request, raws=raws))

        if case.reference_answer.strip():
            correctness_request = RagasRequest(
                question=case.question,
                answer=answer,
                reference=case.reference_answer,
            )
            metrics.append(
                await self._run(MetricName.ANSWER_CORRECTNESS, correctness_request, raws=raws)
            )
        else:
            metrics.append(_na(MetricName.ANSWER_CORRECTNESS, "本题没有参考答案，无从判正确性"))
        return metrics

    async def _run(
        self, metric: MetricName, request: RagasRequest, *, raws: list[str]
    ) -> MetricResult:
        """执行一项 RAGAS 指标（带缓存与错误隔离）。"""
        prompt = canonical_request(request)
        key = JudgeCache.key(
            prompt_name=f"ragas:{metric.value}",
            model_key=self._backend.cache_identity(metric),
            prompt=prompt,
        )
        if self._cache_enabled:
            cached = await self._cache.load(key)
            if cached is not None:
                value = _cached_value(cached)
                if value is not None:
                    raws.append(_raw_section(metric, {"value": value, "cache_hit": True}))
                    return _graded(metric, value, cache_hit=True)

        try:
            score = await self._backend.score(metric, request)
        except RagasMeasurementError as exc:
            logger.warning(
                "eval.ragas_measurement_failed",
                extra={
                    "event": "eval.ragas_measurement_failed",
                    "metric": metric.value,
                    "error": type(exc).__name__,
                },
            )
            raws.append(_raw_section(metric, {"error": str(exc)}))
            return _measurement_error(metric, str(exc))
        except Exception as exc:  # 第三方库异常的边界：记录后转 ERROR，不吞（NO-06）
            logger.warning(
                "eval.ragas_backend_failed",
                extra={
                    "event": "eval.ragas_backend_failed",
                    "metric": metric.value,
                    "error": type(exc).__name__,
                },
            )
            raws.append(_raw_section(metric, {"error": f"{type(exc).__name__}: {exc}"}))
            return _measurement_error(metric, f"{type(exc).__name__}: {exc}")

        if self._cache_enabled:
            await self._cache.store(
                key,
                verdict={"value": score.value},
                raw=json.dumps({"value": score.value}, ensure_ascii=False),
            )
        raws.append(_raw_section(metric, {"value": score.value, "cache_hit": False}))
        return _graded(metric, score.value, cache_hit=False)


def _na(name: MetricName, detail: str) -> MetricResult:
    """构造不适用指标（不带分数）。"""
    return MetricResult(name=name, verdict=MetricVerdict.NOT_APPLICABLE, detail=detail)


def _na_all(detail: str) -> list[MetricResult]:
    """三项统一记为不适用。"""
    return [_na(metric, detail) for metric in _RAGAS_METRICS]


def _graded(name: MetricName, value: float, *, cache_hit: bool) -> MetricResult:
    """成功判分：> 0 记 PASS，0 记 FAIL；阈值由聚合层负责。"""
    detail = f"RAGAS {name.value}={value:.4f}"
    if cache_hit:
        detail += "（缓存命中）"
    verdict = MetricVerdict.PASS if value > 0 else MetricVerdict.FAIL
    return MetricResult(name=name, verdict=verdict, score=value, detail=detail)


def _measurement_error(name: MetricName, reason: str) -> MetricResult:
    """测量失败：``ERROR``、不带分数；本样例判不通过。"""
    return MetricResult(
        name=name,
        verdict=MetricVerdict.ERROR,
        detail=f"RAGAS {name.value} 未测出结果：{reason}（已从聚合分排除，本样例判不通过）",
    )


def _cached_value(payload: dict[str, object]) -> float | None:
    """从缓存条目取出分数；结构不符返回 ``None``（缓存失配不是错误）。"""
    verdict = payload.get("verdict")
    if not isinstance(verdict, dict):
        return None
    value = verdict.get("value")
    if not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not 0.0 <= value <= 1.0:
        return None
    return value


def _raw_section(metric: MetricName, payload: dict[str, object]) -> str:
    """给 RAGAS 留痕加标签（``judge_raw`` 的一段）。"""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return f"[ragas:{metric.value}]\n{body}"


__all__: list[str] = [
    "RagasJudge",
]
