"""RAGAS 0.4.3 进程内判分后端（ADR-0006）。

**为什么单独一层**：RAGAS 的调用细节（工厂函数、指标对象、私有方法覆写）只应
出现在这里；``ragas_judge.py`` 只负责适用性、缓存与四态语义。这样：
- 单测可以用 fake 后端把适用性逻辑跑满，不必装 RAGAS；
- 升级 RAGAS 时，需要重验的私有契约（``StrictAnswerCorrectness``）集中在一处。

**模型调用仍经网关**（NO-02）：LLM 与嵌入都用 ``openai.AsyncOpenAI`` 指向
``LITELLM_BASE_URL``，复用 ``core.models.build_http_async_client``（``trust_env=False``，
避免 Windows 上系统代理劫持本机请求）。

**遥测必须关死**：RAGAS 的 ``_analytics`` 在导入时启动守护线程，向厂商端点发送
含稳定 UUID 与模型名的事件；该库只在 ``RAGAS_DO_NOT_TRACK`` **字面等于** ``"true"``
时停止外发。因此本模块在 **import ragas 之前**强制覆盖该变量，且不提供关闭开关
（CLAUDE.md 10.1；NO-15 的立法意图）。环境里写 ``=false`` 也会被覆盖并记警告。
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Any, Protocol

from agent_platform.core.logging import get_logger
from agent_platform.core.models import build_http_async_client
from agent_platform.evaluation.schemas import MetricName
from agent_platform.settings import Settings

logger = get_logger(__name__)

TELEMETRY_ENV = "RAGAS_DO_NOT_TRACK"

_RAGAS_METRICS: tuple[MetricName, ...] = (
    MetricName.FAITHFULNESS,
    MetricName.ANSWER_RELEVANCY,
    MetricName.ANSWER_CORRECTNESS,
)

_SCORE_TOLERANCE = 1e-6
"""浮点余弦相似度的容差：归一化向量的点积可能出现 1+1e-16，按越界处理会误伤。"""


class RagasMeasurementError(Exception):
    """RAGAS 判分失败（解析失败、空断言、NaN、越界等）。

    这是**测量故障**而非质量结果：调用方必须映射为 ``MetricVerdict.ERROR``，
    不得带分数（ADR-0003 的教训，ADR-0006 沿用）。
    """


@dataclass(frozen=True)
class RagasRequest:
    """一次判分的输入（只含所需文本，不含证据图片等无关字段）。"""

    question: str
    answer: str
    contexts: tuple[str, ...] = ()
    reference: str = ""


@dataclass(frozen=True)
class RagasScore:
    """一次成功判分的结果。"""

    value: float


class RagasBackend(Protocol):
    """判分后端协议（真实 RAGAS 与离线 fake 均满足）。"""

    @property
    def library_version(self) -> str:
        """库版本（写进 ``JudgeSnapshot.library_version`` / ``ragas_version``）。"""
        ...

    @property
    def embedding_model(self) -> str:
        """嵌入模型逻辑名（Answer Relevancy / Correctness 的测量工具之一）。"""
        ...

    @property
    def telemetry_disabled(self) -> bool:
        """遥测是否已强制关闭；为假即数据出域风险（§10.1）。"""
        ...

    @property
    def metric_names(self) -> tuple[str, ...]:
        """本轮启用的 RAGAS 指标名（顺序稳定）。"""
        ...

    def cache_identity(self, metric: MetricName) -> str:
        """该指标的缓存身份：库版本 + 裁判 + 嵌入 + 指标配置。"""
        ...

    async def score(self, metric: MetricName, request: RagasRequest) -> RagasScore:
        """判一项指标。

        Raises:
            RagasMeasurementError: 没测出可信结果；调用方记 ``ERROR``。
        """
        ...

    async def aclose(self) -> None:
        """释放底层客户端。"""
        ...


def _force_telemetry_off() -> bool:
    """在 import ragas 之前强制关闭遥测；返回此前是否已经关闭。"""
    previous = os.environ.get(TELEMETRY_ENV)
    already_off = previous is not None and previous.lower() == "true"
    os.environ[TELEMETRY_ENV] = "true"
    return already_off


def _import_ragas() -> tuple[Any, Any, Any, Any]:
    """强制关闭遥测后导入 RAGAS 与三个指标类。

    Returns:
        ``(ragas_module, Faithfulness, AnswerRelevancy, AnswerCorrectness)``。

    Raises:
        RuntimeError: 默认环境未安装 RAGAS（``eval`` 依赖组未启用）。
    """
    already_off = _force_telemetry_off()
    if not already_off:
        logger.warning(
            "eval.ragas_telemetry_forced_off",
            extra={"event": "eval.ragas_telemetry_forced_off", "env": TELEMETRY_ENV},
        )
    try:
        import ragas
        from ragas.metrics.collections import (
            AnswerCorrectness,
            AnswerRelevancy,
            Faithfulness,
        )
    except ImportError as exc:
        raise RuntimeError(
            "RAGAS 未安装：评估需使用 `uv run --group eval`（ADR-0006 的 eval 依赖组）"
        ) from exc
    return ragas, Faithfulness, AnswerRelevancy, AnswerCorrectness


def _strict_correctness_class(correctness_cls: type[Any]) -> type[Any]:
    """构造 ``StrictAnswerCorrectness``：空断言视为测量失败，而非"完全正确"。

    RAGAS 0.4.3 的 ``AnswerCorrectness.ascore`` 在响应或参考答案抽不出断言时，
    直接把事实性分置为 ``1.0``——这不是"答案正确"，是"抽取失败被当成满分"。
    覆写 ``_generate_statements`` 抛错，让这次判分落到 ``ERROR``。

    **这依赖私有方法**，因此 ``ragas==0.4.3`` 精确锁定；升级前必须重验
    （ADR-0006 风险节）。
    """

    # 基类是可选依赖 ragas 的类，静态检查只能看到 Any；动态继承正是本函数的意图，
    # 故只在这一行豁免，不放宽整个模块。
    class StrictAnswerCorrectness(correctness_cls):  # type: ignore[misc]
        async def _generate_statements(self, question: str, text: str) -> list[str]:
            statements: list[str] = list(await super()._generate_statements(question, text))
            if not statements:
                raise RagasMeasurementError("AnswerCorrectness 未从文本中抽出任何断言")
            return statements

    return StrictAnswerCorrectness


class RagasScorer:
    """真实 RAGAS 后端（构造函数完成全部导入与客户端装配）。"""

    def __init__(self, settings: Settings) -> None:
        """
        Args:
            settings: 平台配置；LLM 用 ``judge_model`` 逻辑名经网关调用，
                嵌入用 ``embedding_model``。

        Raises:
            RuntimeError: RAGAS 未安装（默认环境）。
        """
        ragas, faithfulness_cls, relevancy_cls, correctness_cls = _import_ragas()
        import openai
        from ragas.embeddings.base import embedding_factory
        from ragas.llms import llm_factory

        self._ragas = ragas
        self._embedding_model = settings.embedding_model
        self._telemetry_disabled = os.environ.get(TELEMETRY_ENV, "").lower() == "true"
        self._judge_model = settings.judge_model
        self._upstream_model = settings.judge_upstream_model

        # openai 3.3 的阴影类型只声明 httpx2.AsyncClient，运行时则接受 httpx 客户端
        # （见 openai._httpx2 的兼容层）；此处按实情放宽，而非伪造一个类型。
        http_client: Any = build_http_async_client(settings)
        self._client = openai.AsyncOpenAI(
            base_url=settings.litellm_base_url,
            api_key=settings.litellm_api_key.get_secret_value(),
            http_client=http_client,
            max_retries=settings.model_max_retries,
        )
        self._llm = llm_factory(
            settings.judge_model,
            provider="openai",
            client=self._client,
            temperature=0.0,
        )
        self._embeddings = embedding_factory(
            provider="openai",
            model=settings.embedding_model,
            client=self._client,
        )
        strict = _strict_correctness_class(correctness_cls)
        self._metrics: dict[MetricName, Any] = {
            MetricName.FAITHFULNESS: faithfulness_cls(llm=self._llm),
            MetricName.ANSWER_RELEVANCY: relevancy_cls(llm=self._llm, embeddings=self._embeddings),
            MetricName.ANSWER_CORRECTNESS: strict(
                llm=self._llm,
                embeddings=self._embeddings,
                weights=[0.75, 0.25],
                beta=1.0,
            ),
        }
        self._metric_configs: dict[MetricName, str] = {
            MetricName.FAITHFULNESS: "default",
            MetricName.ANSWER_RELEVANCY: "strictness=3",
            MetricName.ANSWER_CORRECTNESS: "weights=0.75/0.25,beta=1.0",
        }

    @property
    def library_version(self) -> str:
        """RAGAS 版本（来自库自身，不手写）。"""
        return str(self._ragas.__version__)

    @property
    def embedding_model(self) -> str:
        """嵌入模型逻辑名。"""
        return self._embedding_model

    @property
    def telemetry_disabled(self) -> bool:
        """导入后复核环境值；构造时已强制覆盖，故正常为 ``True``。"""
        return self._telemetry_disabled

    @property
    def metric_names(self) -> tuple[str, ...]:
        """启用的指标名（稳定顺序）。"""
        return tuple(metric.value for metric in _RAGAS_METRICS)

    def cache_identity(self, metric: MetricName) -> str:
        """缓存身份：版本 + 裁判 + 嵌入 + 指标配置（换任一即失效）。"""
        return "|".join(
            (
                f"ragas={self.library_version}",
                f"judge={self._judge_model}@{self._upstream_model}",
                f"embedding={self._embedding_model}",
                self._metric_configs[metric],
            )
        )

    async def score(self, metric: MetricName, request: RagasRequest) -> RagasScore:
        """按指标分派到 RAGAS 的 ``ascore``，并统一做数值校验。

        Raises:
            RagasMeasurementError: 输出为 NaN/越界，或 RAGAS 拒绝输入。
        """
        metric_obj = self._metrics[metric]
        try:
            if metric is MetricName.FAITHFULNESS:
                payload = await metric_obj.ascore(
                    user_input=request.question,
                    response=request.answer,
                    retrieved_contexts=list(request.contexts),
                )
            elif metric is MetricName.ANSWER_RELEVANCY:
                payload = await metric_obj.ascore(
                    user_input=request.question, response=request.answer
                )
            elif metric is MetricName.ANSWER_CORRECTNESS:
                payload = await metric_obj.ascore(
                    user_input=request.question,
                    response=request.answer,
                    reference=request.reference,
                )
            else:
                raise RagasMeasurementError(f"未配置的 RAGAS 指标: {metric.value}")
        except RagasMeasurementError:
            raise
        except (ValueError, TypeError) as exc:
            raise RagasMeasurementError(
                f"RAGAS {metric.value} 拒绝输入: {type(exc).__name__}: {exc}"
            ) from exc
        return RagasScore(value=_checked_value(metric, payload))

    async def aclose(self) -> None:
        """关闭 OpenAI 客户端及其底层 HTTP 连接池。"""
        await self._client.close()


def _checked_value(metric: MetricName, payload: Any) -> float:
    """取出 ``MetricResult.value`` 并校验有限性/范围。

    RAGAS 在抽不出断言时会返回 NaN（Faithfulness），余弦相似度也可能因浮点误差
    略超 1。前者是测量失败，后者按容差夹紧——两种处理都必须显式，不能把 NaN
    一路带进聚合分（``sum`` 遇 NaN 会污染整列）。
    """
    raw: Any = getattr(payload, "value", None)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RagasMeasurementError(
            f"RAGAS {metric.value} 返回了非数值结果: {type(payload).__name__}"
        ) from exc
    if not math.isfinite(value):
        raise RagasMeasurementError(f"RAGAS {metric.value} 返回非有限值: {value!r}")
    if value < -_SCORE_TOLERANCE or value > 1.0 + _SCORE_TOLERANCE:
        raise RagasMeasurementError(f"RAGAS {metric.value} 分数越界: {value}")
    return min(1.0, max(0.0, value))


def canonical_request(request: RagasRequest) -> str:
    """把请求序列化为稳定的 JSON（判分缓存键材料；键序与空白不影响结果）。"""
    return json.dumps(
        {
            "question": request.question,
            "answer": request.answer,
            "contexts": list(request.contexts),
            "reference": request.reference,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_ragas_backend(settings: Settings) -> RagasScorer:
    """构造真实 RAGAS 后端（脚本/测试装配入口）。"""
    return RagasScorer(settings)


def ragas_imported() -> bool:
    """``ragas`` 是否已在当前进程导入（遥测子进程测试用来断言副作用）。"""
    return "ragas" in sys.modules


__all__: list[str] = [
    "TELEMETRY_ENV",
    "RagasBackend",
    "RagasMeasurementError",
    "RagasRequest",
    "RagasScore",
    "RagasScorer",
    "build_ragas_backend",
    "canonical_request",
]
