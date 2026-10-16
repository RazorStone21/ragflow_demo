"""裁判层（C 层）：用网关裁判模型给答案打分（CLAUDE.md 9.2 / 5.8）。

**为什么裁判要单独一个逻辑模型名**：裁判与生成必须能各自换型。评测集评分要固定
版本以便跨版本比较，生成侧却要随产品迭代调型；共用一个逻辑名会让"只换生成模型、
不动裁判"做不到，分数因此失去可比性。网关侧的 ``judge`` 路由即为此存在。

**已知偏置，如实登记而不得掩饰**（见 ``SAME_SOURCE_CAVEAT``）：当前 ``judge`` 与
``primary-chat`` 指向同一上游，裁判与被评者是同一模型家族，存在自偏好
（self-preference）偏置，faithfulness / answer_relevancy 会偏乐观。这不是可以
"先忽略、以后再补"的细节——它直接决定这些分数能不能被拿来下结论。因此：

- 偏置说明写进每一份产物的 ``run.judge.caveat``；
- 报告里与分数并排展示；
- ADR-0003 登记为经授权的演示期偏离。

三处刻意的设计：

1. **faithfulness 的分数由断言清单算出，不采信模型自报的总分**。让模型逐条判
   "这条断言有没有证据支撑"，分数 = 有支撑条数 ÷ 总条数。模型可以自报一个好看的
   总分，却很难让每一条断言都撒得圆——分数因此有可核查的推导链，``judge_raw``
   里每一条断言都能被人复核。故 ``FaithfulnessVerdict`` **没有** ``score`` 字段。
2. **解析失败必须响**。裁判输出解析不了时不做"默认给个中间分"的处理，而是判失败
   并把原始输出完整留痕：那说明"这次没测出来"，与"测出来是 0 分"是两件事，
   混在一起会悄悄污染聚合分。
3. **逐例 verdict 对裁判指标刻意宽松**。faithfulness / relevancy 是分级分，"本次
   0.7"不是失败；逐例只把"完全无支撑 / 完全无关"判为 FAIL。是否达标交给聚合阈值
   （``tests/evals/config/thresholds.toml``）——若逐例以 0.8 划线，报告里会堆满
   名为"失败"的中间分，真正的失败反而看不见。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_platform.agents.gov_policy_qa.state import message_text
from agent_platform.core.logging import get_logger
from agent_platform.core.models import ChatModel
from agent_platform.evaluation.prompts import (
    FAITHFULNESS_JUDGE_PROMPT,
    JUDGE_PROMPT_NAMES,
    RELEVANCY_JUDGE_PROMPT,
    render_prompt,
)
from agent_platform.evaluation.schemas import (
    CaseResult,
    GoldenCase,
    JudgeSnapshot,
    MetricName,
    MetricResult,
    MetricVerdict,
)
from agent_platform.guardrails.sanitize import neutralize_untrusted
from agent_platform.knowledge.schemas import EvidenceItem

logger = get_logger(__name__)

SAME_SOURCE_CAVEAT = (
    "裁判与生成共用同一上游模型（网关 judge 与 primary-chat 指向同一型号，"
    "见 deploy/docker/litellm/config.yaml），存在自偏好（self-preference）偏置："
    "faithfulness / answer_relevancy 会偏乐观，**不得当中性分数**使用。"
    "生产须换独立家族的私有化裁判模型（CLAUDE.md 5.8，登记于 ADR-0003）。"
)

_REPAIR_INSTRUCTION = (
    "你上一次的输出不是合法的 JSON 对象。请只输出那个 JSON 对象本身，"
    "不要任何解释、前后缀或代码块标记。"
)

_CACHE_SCHEMA_VERSION = 1
"""缓存条目结构版本；判分契约变化时旧缓存自动失效。"""

V = TypeVar("V", bound=BaseModel)


class JudgeParseError(Exception):
    """裁判输出无法解析为约定结构（内部异常，不外泄到 HTTP 边界）。"""


class FaithfulnessClaim(BaseModel):
    """裁判拆出的一条断言及其判定。"""

    model_config = ConfigDict(frozen=True)

    claim: str = Field(min_length=1, description="断言原文")
    supported: bool = Field(description="能否由证据支撑")
    evidence_index: int | None = Field(
        default=None, ge=1, description="支撑证据编号（1 起）；无支撑时为 None"
    )


class FaithfulnessVerdict(BaseModel):
    """faithfulness 的裁判结构（**无** ``score`` 字段，见模块 docstring 第 1 条）。"""

    model_config = ConfigDict(frozen=True)

    claims: list[FaithfulnessClaim] = Field(default_factory=list)
    reason: str = Field(default="", description="判定依据（人读）")

    def score(self) -> float:
        """有支撑断言占比。``claims`` 非空由 ``_parse_faithfulness`` 保证。"""
        supported = sum(1 for item in self.claims if item.supported)
        return supported / len(self.claims)


class RelevancyVerdict(BaseModel):
    """relevancy 的裁判结构（相关程度无法从子项推导，只能采信模型给的分数）。"""

    model_config = ConfigDict(frozen=True)

    score: float = Field(ge=0.0, le=1.0, description="0=完全不相关，1=完全切题并正面回答")
    reason: str = Field(default="", description="判定理由（人读）")


@dataclass(frozen=True)
class JudgeOutcome:
    """一次判分的产物（指标 + 可复核的原始输出）。"""

    metrics: tuple[MetricResult, ...]
    raw: str
    cache_hit: bool = False


@dataclass(frozen=True)
class _AskResult(Generic[V]):  # noqa: UP046 —— PEP 695 泛型语法需 3.12+，而
    # ``requires-python`` 声明 ``>=3.11,<3.13``；按声明的地板写，不用只在新解释器上
    # 才合法的语法。（ruff 的 ``target-version`` 是 py312，故此处会提示升级。）
    """裁判调用的结果载体。

    ``verdict is None`` 表示两次都没解析出来，**同时**带着 ``raw``——
    失败路径也必须交出原始输出，否则"详见 judge_raw"就成了一句空话
    （人工排查最需要看到的恰恰是解析失败那一次的输出）。
    """

    verdict: V | None
    raw: str
    cache_hit: bool


def extract_json_object(text: str) -> dict[str, Any]:
    """从模型输出里取出第一个完整的 JSON 对象。

    不用 ``json.loads`` 直接解析全文：裁判模型常会在 JSON 前后加一句
    "好的，以下是结果："或用代码块包裹。用括号配平扫描（并正确处理字符串内的
    引号与转义）比正则可靠——正则数不清嵌套花括号。

    Args:
        text: 模型原始输出。

    Returns:
        解析出的字典。

    Raises:
        JudgeParseError: 找不到配平的 JSON 对象，或顶层不是对象。
    """
    start = text.find("{")
    if start < 0:
        raise JudgeParseError("输出中找不到 JSON 对象")
    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(text)):
        char = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return _load_object(text[start : position + 1])
    raise JudgeParseError("JSON 对象的括号未配平（输出可能被截断）")


def _load_object(fragment: str) -> dict[str, Any]:
    """解析一段 JSON 文本并确认顶层是对象。"""
    try:
        parsed: Any = json.loads(fragment)
    except json.JSONDecodeError as exc:
        raise JudgeParseError(f"JSON 解析失败: {exc.msg}（位置 {exc.pos}）") from exc
    if not isinstance(parsed, dict):
        raise JudgeParseError(f"JSON 顶层不是对象: {type(parsed).__name__}")
    return parsed


def _evidence_blocks(evidence: Sequence[EvidenceItem]) -> list[dict[str, Any]]:
    """把证据转成模板变量；正文与文档名一律先中和（CLAUDE.md 10.3）。

    裁判同样是注入目标：让裁判去判断"证据里有没有注入指令"，不能成为
    "裁判可以被注入"的理由。
    """
    return [
        {
            "index": position,
            "document_name": neutralize_untrusted(item.document_name),
            "content": neutralize_untrusted(item.content),
        }
        for position, item in enumerate(evidence, start=1)
    ]


def _parse_faithfulness(payload: dict[str, Any]) -> FaithfulnessVerdict:
    """校验 faithfulness 裁判结构。

    Raises:
        JudgeParseError: 结构不符，或**一条断言都没拆出来**。后者按解析失败处理
            而不是"得 0 分"：没拆出断言说明裁判没照做，不是答案全无依据；
            放进评分会把它错记成一次内容缺陷。
    """
    try:
        verdict = FaithfulnessVerdict.model_validate(payload)
    except ValidationError as exc:
        raise JudgeParseError(f"faithfulness 裁判结构不符: {exc.error_count()} 处") from exc
    if not verdict.claims:
        raise JudgeParseError("faithfulness 裁判未拆出任何事实性断言")
    return verdict


def _parse_relevancy(payload: dict[str, Any]) -> RelevancyVerdict:
    """校验 relevancy 裁判结构。

    Raises:
        JudgeParseError: 结构不符（含 score 越界）。
    """
    try:
        return RelevancyVerdict.model_validate(payload)
    except ValidationError as exc:
        raise JudgeParseError(f"relevancy 裁判结构不符: {exc.error_count()} 处") from exc


class JudgeCache:
    """判分缓存：键 = 提示词名 + **渲染后完整提示词**的指纹（``local_data/``，不入仓）。

    为什么要缓存：裁判调用是整条评估链上最贵的一步，而"改了一条黄金集题目却要
    重判全部 28 条"会让人不敢动评测集。

    键为什么是"渲染后的完整提示词"而不是"样例 ID + 答案"：**答案不决定分数**。
    faithfulness 的判定输入是（问题 + 证据 + 答案），relevancy 是（问题 + 答案）；
    若按答案指纹缓存，两条答案文字相同、问题或证据不同的样例会撞键，后者会直接
    拿到前者算出的分——一个安静、且只出现在巧合场景下的错误分数。渲染后的提示词
    是判定输入的超集，用它做键不含歧义。附带好处：提示词模板改一个字（``v1`` → 内容
    变更但文件名未变）也会命中不了旧缓存。

    命中缓存时显式标记（``JudgeOutcome.cache_hit``）——"这次分数是真跑出来的还是
    从缓存里翻出来的"必须能从留痕里读出来。
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @staticmethod
    def key(*, prompt_name: str, prompt: str) -> str:
        """算出缓存键（各段用 ``\\x1f`` 分隔，避免拼接歧义）。"""
        material = "\x1f".join((prompt_name, prompt))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path:
        """缓存文件路径（按前两位分目录，避免单目录堆数千文件）。"""
        return self._directory / key[:2] / f"{key}.json"

    async def load(self, key: str) -> dict[str, Any] | None:
        """读缓存；缺失、损坏或版本不符一律返回 ``None``（缓存失配不是错误）。"""
        path = self.path_for(key)
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "eval.judge_cache_corrupt",
                extra={"event": "eval.judge_cache_corrupt", "key": key[:12]},
            )
            return None
        if not isinstance(payload, dict) or payload.get("v") != _CACHE_SCHEMA_VERSION:
            return None
        if not isinstance(payload.get("verdict"), dict) or not isinstance(payload.get("raw"), str):
            return None
        return payload

    async def store(self, key: str, *, verdict: dict[str, Any], raw: str) -> None:
        """写缓存；写不进去只记警告（缓存是加速手段，不是正确性依赖）。"""
        body = json.dumps(
            {"v": _CACHE_SCHEMA_VERSION, "verdict": verdict, "raw": raw},
            ensure_ascii=False,
            indent=2,
        )
        try:
            await asyncio.to_thread(_write_text, self.path_for(key), body)
        except OSError as exc:
            logger.warning(
                "eval.judge_cache_write_failed",
                extra={
                    "event": "eval.judge_cache_write_failed",
                    "key": key[:12],
                    "error": type(exc).__name__,
                },
            )


def _write_text(path: Path, body: str) -> None:
    """同步写文件（经 ``asyncio.to_thread`` 调用，见 ``JudgeCache.store``）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class Judge:
    """网关裁判模型的封装（CLAUDE.md 9.2）。"""

    def __init__(
        self,
        model: ChatModel,
        *,
        logical_name: str,
        cache_dir: Path,
        max_tokens: int = 4096,
    ) -> None:
        """
        Args:
            model: 经网关构造的裁判模型（``temperature=0``，CLAUDE.md 5.3）。
            logical_name: 网关逻辑模型名（埋点与快照记录用；不得写死型号，NO-03）。
            cache_dir: 判分缓存目录。
            max_tokens: 单次裁判输出上限。

                **补记（2026-10-16）**：原值 1024，实测偏小——faithfulness 的裁判
                要为答案里的**每一条断言**输出一条判定，答案长（实测 1138 字）时
                JSON 会在半途被截断，表现为"括号未配平"→ 两次都解析不出来。
                2048 仍会在更长答案上撞墙，故直接给 4096：裁判输出是结构化短句，
                上限给宽不增加正常开销，只把撞墙点推远；真撞上也不会静默——
                解析失败会记 ``MetricVerdict.ERROR`` 并在报告里显式列出。
        """
        self._model = model
        self._logical_name = logical_name
        self._cache = JudgeCache(cache_dir)
        self._max_tokens = max_tokens

    @property
    def logical_name(self) -> str:
        """``logical_name``：网关逻辑模型名（写进 ``ModelSnapshot.judge``）。

        §9.2 要求"裁判模型与标准固定并记录版本"，两者都得到产物里，缺一不可。
        这个名字必须由**这里**提供而不是调用方另写一遍：调用方另写一份的话，
        产物里的"谁在判"与真正判分的那个模型就可能悄悄分家，而那正是最需要
        追溯的情形。

        **补记（2026-10-16）**：此前本属性不存在，``_logical_name`` 存下后无人读取，
        于是 ``ModelSnapshot.judge`` 一直留空——已入库的首份基线里就有三列裁判分数
        而 ``models.judge=""``。它还会让首次基线对比凭空报一条
        ``models.judge: "" → "judge"`` 的配置漂移（见 tests/evals/README.md 第五节）。
        """
        return self._logical_name

    def snapshot(self) -> JudgeSnapshot:
        """本次裁判配置的快照（写进产物，保证分数可追溯到"哪版裁判"）。

        ``max_tokens`` 一并记录（2026-10-17 补）：它是 D8 的诱因之一，而"输出上限"
        这种旋钮**改了不会体现在任何一处分数上**——只有当某条样例恰好被截断、
        从 ``pass`` 变成 ``error`` 时才会显形，而那时离"该改哪一个参数"已经隔了
        一次排查。记进快照后，改上限会在下一次 ``--baseline`` 对比里报成一条配置
        漂移，与改提示词、改温度同等可见（tests/evals/README.md 第五节 D9）。
        """
        return JudgeSnapshot(
            backend="in-house",
            library_version="",
            temperature=0.0,
            max_tokens=self._max_tokens,
            prompt_version="+".join(JUDGE_PROMPT_NAMES),
            caveat=SAME_SOURCE_CAVEAT,
        )

    async def score(self, case: GoldenCase, result: CaseResult) -> CaseResult:
        """对一条样例补上裁判指标，返回**追加了指标**的新结果。

        适用性由内容决定而非类别：没有证据就判不了 faithfulness（无据可核），
        没有答案就判不了 relevancy。不适用时记 ``NOT_APPLICABLE``——
        那是一等结果，不需要硬凑一个分数。

        Args:
            case: 黄金集样例（提供问题原文）。
            result: 图/检索层的执行结果。

        Returns:
            新的 ``CaseResult``：``metrics`` 追加裁判指标，``judge_raw`` 填原始输出。
        """
        metrics: list[MetricResult] = []
        raws: list[str] = []

        if result.evidence:
            outcome = await self._judge_faithfulness(case.question, result)
        else:
            outcome = JudgeOutcome(
                metrics=(
                    MetricResult(
                        name=MetricName.FAITHFULNESS,
                        verdict=MetricVerdict.NOT_APPLICABLE,
                        detail="本题无检索证据，无据可核",
                    ),
                ),
                raw="",
            )
        metrics.extend(outcome.metrics)
        raws.append(_raw_section(FAITHFULNESS_JUDGE_PROMPT, outcome.raw))

        if result.answer:
            outcome = await self._judge_relevancy(case.question, result.answer)
        else:
            outcome = JudgeOutcome(
                metrics=(
                    MetricResult(
                        name=MetricName.ANSWER_RELEVANCY,
                        verdict=MetricVerdict.NOT_APPLICABLE,
                        detail="本题无答案产出，无可判内容",
                    ),
                ),
                raw="",
            )
        metrics.extend(outcome.metrics)
        raws.append(_raw_section(RELEVANCY_JUDGE_PROMPT, outcome.raw))

        handled = {metric.name for metric in metrics}
        preserved = [metric for metric in result.metrics if metric.name not in handled]
        return result.model_copy(
            update={
                "metrics": [*preserved, *metrics],
                "judge_raw": "\n\n".join(section for section in raws if section),
            }
        )

    async def _judge_faithfulness(self, question: str, result: CaseResult) -> JudgeOutcome:
        """逐条核对断言是否有证据支撑，分数 = 有支撑条数 ÷ 总条数。"""
        rendered = render_prompt(
            FAITHFULNESS_JUDGE_PROMPT,
            question=neutralize_untrusted(question),
            evidence=_evidence_blocks(result.evidence),
            answer=neutralize_untrusted(result.answer),
        )
        ask = await self._ask(
            prompt_name=FAITHFULNESS_JUDGE_PROMPT,
            prompt=rendered,
            parse=_parse_faithfulness,
        )
        if ask.verdict is None:
            return _unparseable_outcome(
                MetricName.FAITHFULNESS, FAITHFULNESS_JUDGE_PROMPT, raw=ask.raw
            )

        claims = ask.verdict.claims
        supported = sum(1 for claim in claims if claim.supported)
        unsupported = [claim.claim for claim in claims if not claim.supported][:3]
        detail = f"{supported}/{len(claims)} 条断言有证据支撑"
        if unsupported:
            detail += f"；无支撑示例：{'；'.join(unsupported)}"
        return JudgeOutcome(
            metrics=(_graded_metric(MetricName.FAITHFULNESS, ask.verdict.score(), detail=detail),),
            raw=ask.raw,
            cache_hit=ask.cache_hit,
        )

    async def _judge_relevancy(self, question: str, answer: str) -> JudgeOutcome:
        """判定答案是否正面回答了问题。"""
        rendered = render_prompt(
            RELEVANCY_JUDGE_PROMPT,
            question=neutralize_untrusted(question),
            answer=neutralize_untrusted(answer),
        )
        ask = await self._ask(
            prompt_name=RELEVANCY_JUDGE_PROMPT,
            prompt=rendered,
            parse=_parse_relevancy,
        )
        if ask.verdict is None:
            return _unparseable_outcome(
                MetricName.ANSWER_RELEVANCY, RELEVANCY_JUDGE_PROMPT, raw=ask.raw
            )
        return JudgeOutcome(
            metrics=(
                _graded_metric(
                    MetricName.ANSWER_RELEVANCY,
                    ask.verdict.score,
                    detail=ask.verdict.reason or "裁判未给出理由",
                ),
            ),
            raw=ask.raw,
            cache_hit=ask.cache_hit,
        )

    async def _ask(
        self,
        *,
        prompt_name: str,
        prompt: str,
        parse: Callable[[dict[str, Any]], V],
    ) -> _AskResult[V]:
        """带缓存与一次纠偏重试的裁判调用。

        重试只有**一次**：第二次仍失败说明问题不在"偶发的格式抖动"，再试下去
        只是把一次调试事故拖成一条慢链路，并掩盖真正该看的原始输出。

        Returns:
            ``_AskResult``；``verdict is None`` 时 ``raw`` 仍完整携带两次的输出。
        """
        key = JudgeCache.key(prompt_name=prompt_name, prompt=prompt)
        cached = await self._cache.load(key)
        if cached is not None:
            try:
                cached_verdict = parse(cached["verdict"])
            except JudgeParseError:
                # 缓存内容解析不了说明它已失效，落到真实调用而不是当成失败
                logger.warning(
                    "eval.judge_cache_unusable",
                    extra={"event": "eval.judge_cache_unusable", "prompt": prompt_name},
                )
            else:
                return _AskResult(verdict=cached_verdict, raw=cached["raw"], cache_hit=True)

        first = await self._invoke(prompt)
        verdict: V | None = _try_parse(first, parse, prompt_name)
        raw = first
        if verdict is None:
            logger.warning(
                "eval.judge_repair_attempt",
                extra={"event": "eval.judge_repair_attempt", "prompt": prompt_name},
            )
            second = await self._invoke(prompt, repair=True)
            verdict = _try_parse(second, parse, prompt_name)
            raw = f"【首次输出】\n{first}\n\n【纠偏重试输出】\n{second}"
        if verdict is None:
            return _AskResult(verdict=None, raw=raw, cache_hit=False)

        await self._cache.store(key, verdict=verdict.model_dump(mode="json"), raw=raw)
        return _AskResult(verdict=verdict, raw=raw, cache_hit=False)

    async def _invoke(self, prompt: str, *, repair: bool = False) -> str:
        """调用裁判模型并返回纯文本输出（``repair`` 时附上纠偏指令）。"""
        content = prompt if not repair else f"{prompt}\n\n{_REPAIR_INSTRUCTION}"
        message = await self._model.ainvoke(
            [HumanMessage(content=content)], max_tokens=self._max_tokens
        )
        return message_text(message)


def _try_parse(  # noqa: UP047 —— 同 _AskResult：requires-python 声明支持 3.11
    raw: str, parse: Callable[[dict[str, Any]], V], prompt_name: str
) -> V | None:
    """尝试解析一段裁判输出；失败返回 ``None``（不抛，交由调用方决定是否重试）。"""
    try:
        return parse(extract_json_object(raw))
    except JudgeParseError as exc:
        logger.info(
            "eval.judge_parse_failed",
            extra={"event": "eval.judge_parse_failed", "prompt": prompt_name, "reason": str(exc)},
        )
        return None


def _raw_section(prompt_name: str, raw: str) -> str:
    """给原始输出加上提示词名标签（留痕的一段）；空输出返回空串。"""
    return f"[{prompt_name}]\n{raw}" if raw else ""


def _graded_metric(name: MetricName, score: float, *, detail: str) -> MetricResult:
    """构造裁判指标（分级分）。

    逐例 verdict 刻意宽松（见模块 docstring 第 3 条）：只有**完全没有**支撑或
    完全无关才判 FAIL，其余记 PASS，是否达标由聚合阈值判定。若在此处以 0.8
    划线，报告里会堆满名为"失败"的中间分，把真正的失败淹掉。
    """
    verdict = MetricVerdict.PASS if score > 0.0 else MetricVerdict.FAIL
    return MetricResult(name=name, verdict=verdict, score=score, detail=detail)


def _unparseable_outcome(name: MetricName, prompt_name: str, *, raw: str) -> JudgeOutcome:
    """裁判输出两次都无法解析时的结果：记 ``ERROR``（**没测出来**），不默认给分。

    **补记（2026-10-16）**：这里早先是 ``FAIL`` + ``score=0.0``，理由是"没测出来
    必须是可见的坏消息，放过它会让裁判故障表现成'这轮没考这项'、聚合分反而更好看"。
    该理由的前提是对的，**手段是错的**：``0.0`` 是一个测量结果，把它写进产物等于
    凭空造了一个数据点。首次真实基线对比就撞上了——一条样例的 faithfulness 因
    裁判输出括号不配平被判 ``0.0000``，聚合分被压低 1/23，报告里读作
    "faithfulness 0.9392 → 0.8770，跌破"，该样例还进了"新失败样例"，而模型
    什么都没做错。**一次测量故障伪装成了一次质量回归**，正是这套留痕机制要防的事。

    现改为 ``MetricVerdict.ERROR``：不带分数、不计入聚合（所以聚合分不被污染），
    但**照样让该样例判不通过**（``CaseResult.passed`` 把 ERROR 算作未通过）——
    可见性一分不减，伪造的数据点没有了。

    ``raw`` 原样携带两次的输出——人工排查最需要看到的恰恰是解析失败那一次的结果。
    """
    return JudgeOutcome(
        metrics=(
            MetricResult(
                name=name,
                verdict=MetricVerdict.ERROR,
                detail=(
                    f"裁判输出两次均无法解析为约定结构（{prompt_name}）：此项**未采集到结果**，"
                    "已从聚合中排除，本样例因此判不通过。原始输出见 judge_raw。"
                ),
            ),
        ),
        raw=raw,
    )


__all__: list[str] = [
    "SAME_SOURCE_CAVEAT",
    "FaithfulnessClaim",
    "FaithfulnessVerdict",
    "Judge",
    "JudgeCache",
    "JudgeOutcome",
    "JudgeParseError",
    "RelevancyVerdict",
    "extract_json_object",
]
